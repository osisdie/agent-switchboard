#!/usr/bin/env python3
"""tests/test_dev.py — the developer portal (/dev, /dev/keys) and its key routes (/api/apikeys) on
board-serve (lib/serve_dev.py): the page's CSP and caching, what /api/dev returns (the live OpenAPI
document, the MCP tool table, snippets with Cloudflare headers only through the tunnel), who may do
what with keys (owner, approver, asker, viewer; an API key alone never), a new key in exactly one
response and nowhere else (no file, log or audit row), request -> approve, PII masking for
non-owners, the CSRF checks, GET /api/me's api_keys, and the health-check snippet run for real.

Identities come from verified Cloudflare Access JWTs (a throwaway RSA key) on the tunnel listener;
`local` on the loopback listener. Everything lives in a temp BOARD_HOME / BOARD_CONFIG_DIR.

  python3 tests/test_dev.py [-v]
  DEV_REQUIRE_CRYPTO=1 makes a missing python3 cryptography a failure instead of a skip (CI).
"""
import base64
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

TMP = tempfile.mkdtemp(prefix="switchboard-dev-test.")
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
os.makedirs(CFG)
os.makedirs(HOME, mode=0o700)
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_NOW", "BOARD_API", "CF_ACCESS")) \
            or k in ("TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL"):
        os.environ.pop(k, None)
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "ALERT_DESKTOP": "0",
                   "CLAUDE_JSON": os.path.join(TMP, "no-claude.json"),
                   "CLAUDE_SESSIONS_DIR": os.path.join(TMP, "sessions")})

import api_spec  # noqa: E402
import apikeys  # noqa: E402
import mcp_tools  # noqa: E402

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - CI installs python3-cryptography
    HAVE_CRYPTO = False

OWN, BOSS, RM, ANN, VIEWER, STRANGER = ("own@example.com", "boss@example.com", "rm@example.com",
                                        "ann@example.com", "viewer@example.com", "nobody@example.org")
PEOPLE = (OWN, BOSS, RM, ANN, VIEWER)
HOSTNAME = "board.example.com"
SVC = "svc1.access"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT, TPORT = free_port(), free_port()


def write_fleet():
    with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
        fh.write("""owners: [%s]
approvers: [%s, %s]
release_managers: [%s]
askers: [%s]
sessions:
  - code: API
    cwd: /w
serve:
  port: %d
  tunnel:
    enabled: true
    port: %d
    hostname: %s
    team_domain: team.example.com
    aud: AUD123
    certs_url: file://%s/certs.json
    allowed_emails: [%s]
    api_service_tokens: [%s]
""" % (OWN, OWN, BOSS, RM, ANN, PORT, TPORT, HOSTNAME, TMP, ", ".join(PEOPLE), SVC))


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwts():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    num = lambda n: b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))  # noqa: E731
    wj(os.path.join(TMP, "certs.json"), {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256",
                                                   "n": num(pub.n), "e": num(pub.e)}]})

    def tok(claims):
        now = int(time.time())
        h = b64(json.dumps({"alg": "RS256", "kid": "k1"}).encode())
        p = b64(json.dumps(dict({"aud": ["AUD123"], "iss": "https://team.example.com", "exp": now + 3600,
                                 "iat": now}, **claims)).encode())
        return h + "." + p + "." + b64(key.sign((h + "." + p).encode(), padding.PKCS1v15(), hashes.SHA256()))
    out = {e: tok({"email": e}) for e in PEOPLE}
    out["svc"] = tok({"common_name": SVC, "sub": "", "type": "app"})
    return out


JWT = {}
SRV = None
ERR = []
NOW = int(time.time() * 1000)


def setUpModule():
    global SRV
    if not HAVE_CRYPTO:
        return
    write_fleet()
    wj(os.path.join(HOME, "snapshot.json"), {"generated_at": NOW, "sessions": [
        {"code": "API", "aliases": ["API"], "state": "working", "updated_at": NOW}]})
    JWT.update(make_jwts())
    ERR.append(open(os.path.join(TMP, "serve.err"), "w"))
    SRV = subprocess.Popen([sys.executable, os.path.join(REPO, "lib", "serve.py")],
                           stdout=subprocess.DEVNULL, stderr=ERR[0])
    for port in (PORT, TPORT):
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("board-serve did not start: " + open(os.path.join(TMP, "serve.err")).read())


def tearDownModule():
    if SRV:
        SRV.terminate()
        SRV.wait(10)
    for fh in ERR:
        fh.close()
    shutil.rmtree(TMP, ignore_errors=True)


def call(method, path, who=None, body=None, headers=None, board=True, origin=True):
    """who: an email (tunnel, its Access JWT), "svc" (a service token JWT), or None (local)."""
    if who is None:
        port, host = PORT, "127.0.0.1:%d" % PORT
        hdrs = {"Host": host}
        own_origin = "http://" + host
    else:
        port, host = TPORT, HOSTNAME
        hdrs = {"Host": host, "Cf-Access-Jwt-Assertion": JWT[who]}
        own_origin = "https://" + host
    if board:
        hdrs["X-Board"] = "1"
    if origin and method == "POST":
        hdrs["Origin"] = own_origin
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    c.request(method, path, body=data, headers=hdrs)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        js = json.loads(raw) if raw else None
    except ValueError:
        js = None
    return r.status, js, raw.decode("utf-8", "replace"), {k.lower(): v for k, v in r.getheaders()}


def events():
    try:
        with open(os.path.join(HOME, "events.jsonl"), encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]
    except FileNotFoundError:
        return []


def everything_on_disk():
    """Every byte under the temp tree (BOARD_HOME, config, the server's log), as text."""
    out = []
    for root, _dirs, files in os.walk(TMP):
        for f in files:
            try:
                with open(os.path.join(root, f), "rb") as fh:
                    out.append(fh.read().decode("utf-8", "replace"))
            except OSError:
                pass
    return "\n".join(out)


def create(who, name, owner=None, scopes=("read:state",), action="create", expires="30d"):
    body = {"action": action, "name": name, "scopes": list(scopes), "expires": expires}
    if action == "create":
        body["owner"] = owner or (who if who else "local")
    return call("POST", "/api/apikeys", who, body)


def whoami(key, who=None):
    hdrs = {"Authorization": "Bearer " + key}
    return call("GET", "/api/v1/whoami", who, headers=hdrs, board=False)[:2]


class Base(unittest.TestCase):
    def setUp(self):
        if not HAVE_CRYPTO:
            if os.environ.get("DEV_REQUIRE_CRYPTO") == "1":
                self.fail("python3 cryptography is required (DEV_REQUIRE_CRYPTO=1)")
            self.skipTest("python3 cryptography missing")


class Page(Base):
    def test_csp_nonce_and_no_store(self):
        for who in (None, VIEWER, ANN):
            for path in ("/dev", "/dev/keys"):
                with self.subTest(who=who, path=path):
                    st, _, raw, hd = call("GET", path, who, board=False)
                    self.assertEqual(st, 200, raw[:200])
                    csp = hd["content-security-policy"]
                    m = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", csp)
                    self.assertTrue(m, csp)
                    self.assertIn("default-src 'none'", csp)
                    self.assertIn("connect-src 'self'", csp)
                    self.assertIn("frame-ancestors 'none'", csp)
                    self.assertNotIn("unsafe-eval", csp)
                    self.assertEqual(hd["cache-control"], "no-store")
                    self.assertEqual(hd["x-frame-options"], "DENY")
                    scripts = re.findall(r"<script[^>]*>", raw)
                    self.assertGreaterEqual(len(scripts), 2)
                    self.assertTrue(all('nonce="%s"' % m.group(1) in s for s in scripts), scripts)
                    self.assertNotIn(" on" + "click=", raw)        # no inline handlers (the CSP would block them)
        a = call("GET", "/dev", VIEWER, board=False)[3]["content-security-policy"]
        b = call("GET", "/dev", VIEWER, board=False)[3]["content-security-policy"]
        self.assertNotEqual(a, b)                                  # a fresh nonce per response
        self.assertEqual(call("HEAD", "/dev", None, board=False)[0], 200)

    def test_same_gate_as_the_board(self):
        c = http.client.HTTPConnection("127.0.0.1", TPORT, timeout=30)
        c.request("GET", "/dev", headers={"Host": HOSTNAME})     # no Access JWT
        self.assertEqual(c.getresponse().status, 401)
        c.close()
        self.assertEqual(call("GET", "/dev", "svc", board=False)[0], 401)   # a service token is not a person
        self.assertEqual(call("GET", "/dev", None, board=False, headers={"X-Forwarded-For": "203.0.113.9"})[0], 403)

    def test_board_page_links_to_dev(self):
        st, _, raw, _ = call("GET", "/", VIEWER, board=False)
        self.assertEqual(st, 200)
        self.assertIn('href="/dev"', raw)
        self.assertIn('href="/dev/keys"', raw)

    def test_error_codes_are_the_apis_own(self):
        with open(os.path.join(REPO, "artifact", "dev.html"), encoding="utf-8") as fh:
            page = fh.read()
        with open(os.path.join(REPO, "lib", "serve_api.py"), encoding="utf-8") as fh:
            src = fh.read()
        codes = re.findall(r"\['(\d{3})', '([a-z_]+)'", page)
        self.assertGreaterEqual(len(codes), 10)
        for status, code in codes:
            self.assertIn('"%s"' % code, src, code)
        for need in ("unauthenticated", "token_key_mismatch", "identity_key_mismatch", "insufficient_scope",
                     "rate_limited"):
            self.assertIn(need, [c for _, c in codes])


class Info(Base):
    def test_fetch_checks(self):
        self.assertEqual(call("GET", "/api/dev", VIEWER, board=False)[:2],
                         (403, {"error": "csrf", "detail": "X-Board: 1 header required"}))
        st, js, _, _ = call("GET", "/api/dev", VIEWER, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((st, js["error"]), (403, "csrf"))

    def test_live_spec_and_mcp_table(self):
        st, js, raw, hd = call("GET", "/api/dev", VIEWER)
        self.assertEqual(st, 200, raw[:300])
        self.assertEqual(hd["cache-control"], "no-store")
        self.assertEqual(js["openapi"], json.loads(json.dumps(api_spec.openapi())))
        self.assertEqual([t["name"] for t in js["mcp"]["tools"]], [t["name"] for t in mcp_tools.TOOLS])
        self.assertEqual(js["scopes"], list(apikeys.SCOPES))
        self.assertIsNone(js["mock"])
        self.assertNotIn("access-control-allow-origin", hd)

    def test_snippets_tunnel_and_local(self):
        _, tun, _, _ = call("GET", "/api/dev", BOSS)
        _, loc, _, _ = call("GET", "/api/dev", None)
        self.assertEqual((tun["base_url"], tun["listener"]), ("https://" + HOSTNAME, "tunnel"))
        self.assertEqual((loc["base_url"], loc["listener"]), ("http://127.0.0.1:%d" % PORT, "local"))
        tsn = {s["id"]: s["text"] for s in tun["snippets"]}
        lsn = {s["id"]: s["text"] for s in loc["snippets"]}
        for sid in ("claude-code-http", "curl", "health"):
            self.assertIn("$CF_ACCESS_CLIENT_ID", tsn[sid], sid)
            self.assertIn("$CF_ACCESS_CLIENT_SECRET", tsn[sid], sid)
            self.assertIn("https://%s" % HOSTNAME, tsn[sid])
        self.assertIn("<CF_ACCESS_CLIENT_SECRET>", tsn["claude-desktop"])
        self.assertNotIn("claude-code-stdio", tsn)                 # the board's own path: local only
        self.assertIsNone(tun["board_mcp"])
        for sid, text in lsn.items():
            self.assertNotIn("CF_ACCESS", text, sid)
            self.assertNotIn("CF-Access", text, sid)
        self.assertIn("claude-code-stdio", lsn)
        self.assertIn('\nH=(-H "Authorization: Bearer $BOARD_API_KEY")\n', lsn["health"])   # the key only
        for snips in (tsn, lsn):
            for sid, text in snips.items():
                self.assertNotRegex(text, apikeys.KEY_RE, sid)
                self.assertTrue("$BOARD_API_KEY" in text or "<BOARD_API_KEY>" in text, sid)
        h = tsn["health"]
        self.assertEqual(len(re.findall(r'-H "(Authorization|CF-Access-Client-Id|CF-Access-Client-Secret):', h)), 3)
        self.assertIn("Accept: application/json, text/event-stream", h)
        for m in ("initialize", "tools/list", "tools/call", "/api/v1/whoami"):
            self.assertIn(m, h)

    def test_non_owner_sees_no_emails(self):
        for who in (VIEWER, ANN, BOSS):
            _, js, raw, _ = call("GET", "/api/dev", who)
            others = [p for p in PEOPLE if p != who]
            for p in others:
                self.assertNotIn(p, raw, (who, p))
            self.assertEqual(js["me"]["identity"], who)


class Keys(Base):
    def test_role_matrix_for_create(self):
        cases = [  # (who, owner, allowed)
            (OWN, STRANGER, True),            # owners issue to anyone
            (OWN, OWN, True),
            (BOSS, BOSS, True),               # approvers: themselves
            (BOSS, VIEWER, True),             # ... or a listed person
            (BOSS, STRANGER, False),          # ... never anyone else
            (RM, RM, False),                  # a release manager is not an approver
            (ANN, ANN, False),                # askers and viewers request instead
            (VIEWER, VIEWER, False),
        ]
        for who, owner, ok in cases:
            with self.subTest(who=who, owner=owner):
                before = len(events())
                st, js, raw, _ = create(who, "matrix %s" % who.split("@")[0], owner)
                if ok:
                    self.assertEqual(st, 201, raw)
                    self.assertRegex(js["key"], apikeys.KEY_RE)
                    self.assertEqual(js["status"], "active")
                else:
                    self.assertEqual((st, js["error"]), (403, "not_authorized"), raw)
                    self.assertNotIn("key", js)
                    rows = [e for e in events()[before:] if e.get("type") == "apikey"]
                    self.assertEqual([(e["action"], e["result"], e["via"], e["by"]) for e in rows],
                                     [("create", "refused", "board", who)])

    def test_an_api_key_alone_never_manages_keys(self):
        _, made, _, _ = create(OWN, "holder", OWN, scopes=("read:state",))
        key = made["key"]
        bearer = {"Authorization": "Bearer " + key}
        before = len(events())
        # the board's session plus a key: refused (the key is not the way in), audited
        for who in (None, OWN):
            st, js, _, _ = call("POST", "/api/apikeys", who, {"action": "create", "name": "x", "owner": OWN},
                                headers=bearer)
            self.assertEqual((st, js["error"]), (403, "api_key_refused"))
            st, js, _, _ = call("GET", "/api/apikeys", who, headers=bearer)
            self.assertEqual((st, js["error"]), (403, "api_key_refused"))
            st, js, _, _ = call("POST", "/api/apikeys/%s" % made["id"], who, {"action": "revoke"}, headers=bearer)
            self.assertEqual((st, js["error"]), (403, "api_key_refused"))
        rows = [e for e in events()[before:] if e.get("reason") == "api_key_refused"]
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(e["type"] == "apikey" and e["result"] == "refused" for e in rows))
        # a service token and a key, the way an API client comes in through the tunnel: no person
        st, _, _, _ = call("POST", "/api/apikeys", "svc", {"action": "request", "name": "x"}, headers=bearer)
        self.assertEqual(st, 401)
        # and /api/v1 has no key route at all
        for path in ("/api/v1/apikeys", "/api/v1/keys"):
            self.assertEqual(call("GET", path, None, headers=bearer, board=False)[0], 404)
        self.assertEqual(whoami(key)[0], 200)                       # the key itself still works

    def test_csrf(self):
        st, js, _, _ = call("POST", "/api/apikeys", OWN, {"action": "create", "name": "x", "owner": OWN}, board=False)
        self.assertEqual((st, js["error"]), (403, "csrf"))
        st, js, _, _ = call("POST", "/api/apikeys", OWN, {"action": "create", "name": "x", "owner": OWN},
                            origin=False, headers={"Origin": "https://evil.example.net"})
        self.assertEqual((st, js["error"]), (403, "csrf"))
        st, js, _, _ = call("GET", "/api/apikeys", OWN, board=False)
        self.assertEqual((st, js["error"]), (403, "csrf"))

    def test_key_in_exactly_one_response(self):
        st, js, raw, hd = create(BOSS, "once", BOSS, scopes=("read:state", "read:tasks"))
        self.assertEqual(st, 201, raw)
        key = js["key"]
        self.assertTrue(js["shown_once"])
        self.assertEqual(hd["cache-control"], "no-store")
        self.assertEqual(raw.count(key), 1)
        # every later read: no key
        for who in (BOSS, OWN):
            for path in ("/api/apikeys", "/api/me", "/api/state", "/api/dev"):
                self.assertNotIn(key, call("GET", path, who)[2], (who, path))
        self.assertNotIn(key, whoami(key)[1].__repr__())
        # nowhere on disk: not in the key store, events.jsonl (the audit), state, or the server's log
        disk = everything_on_disk()
        self.assertNotIn(key, disk)
        self.assertNotIn(key.split("_")[-1], disk)               # not even the secret part
        row = [e for e in events() if e.get("type") == "apikey" and e.get("key_id") == js["id"]]
        self.assertEqual([(e["action"], e["result"], e["via"]) for e in row], [("create", "ok", "board")])
        self.assertEqual(whoami(key)[1]["scopes"], ["read:state", "read:tasks"])

    def test_request_then_approve(self):
        st, js, raw, _ = create(VIEWER, "my script", action="request", scopes=("read:state", "read:tasks"))
        self.assertEqual(st, 201, raw)
        self.assertEqual(js["status"], "pending")
        kid, key = js["id"], js["key"]
        self.assertEqual(whoami(key)[0], 401)                      # pending: unusable
        # not an owner: no approve (the CLI's own refusal, audited)
        for who in (VIEWER, BOSS):
            st, js, _, _ = call("POST", "/api/apikeys/" + kid, who, {"action": "approve"})
            self.assertEqual((st, js["error"]), (403, "not_authorized"), (who, js))
        refused = [e for e in events() if e.get("action") == "approve" and e.get("key_id") == kid]
        self.assertEqual([e["result"] for e in refused], ["refused", "refused"])
        # wider than requested: refused
        st, js, _, _ = call("POST", "/api/apikeys/" + kid, OWN, {"action": "approve",
                                                                 "scopes": ["read:state", "tasks:write"]})
        self.assertEqual((st, js["error"]), (400, "bad_request"), js)
        # the owner sees the request with the requester's email, and narrows it
        _, lst, _, _ = call("GET", "/api/apikeys", OWN)
        row = next(k for k in lst["keys"] if k["id"] == kid)
        self.assertEqual((row["status"], row["owner"]), ("pending", VIEWER))
        st, js, _, _ = call("POST", "/api/apikeys/" + kid, OWN, {"action": "approve", "scopes": ["read:state"]})
        self.assertEqual(st, 200, js)
        self.assertNotIn(key, json.dumps(js))
        st, who = whoami(key)
        self.assertEqual((st, who["scopes"]), (200, ["read:state"]))
        ok = [e for e in events() if e.get("action") == "approve" and e.get("key_id") == kid and e["result"] == "ok"]
        self.assertEqual((ok[0]["by"], ok[0]["via"], ok[0]["requested_scopes"]), (OWN, "board", ["read:state", "read:tasks"]))
        # the requester revokes their own key
        st, js, _, _ = call("POST", "/api/apikeys/" + kid, VIEWER, {"action": "revoke"})
        self.assertEqual(st, 200, js)
        self.assertEqual(whoami(key)[0], 401)
        self.assertEqual(call("POST", "/api/apikeys/" + kid, VIEWER, {"action": "revoke"})[0], 409)

    def test_someone_elses_key_does_not_exist(self):
        _, made, _, _ = create(OWN, "owners own", OWN)
        for who in (VIEWER, ANN, BOSS):
            st, js, _, _ = call("POST", "/api/apikeys/" + made["id"], who, {"action": "revoke"})
            self.assertEqual((st, js["error"]), (404, "not_found"), who)
        self.assertEqual(call("POST", "/api/apikeys/zzzzzzzzzzzz", OWN, {"action": "revoke"})[0], 404)
        self.assertEqual(whoami(made["key"])[0], 200)

    def test_who_sees_what_and_pii(self):
        create(OWN, "for viewer", VIEWER)
        create(VIEWER, "viewer request", action="request")
        _, own, _, _ = call("GET", "/api/apikeys", OWN)
        self.assertTrue(own["me"]["key_owner"])
        self.assertIn(VIEWER, own["listed"])
        self.assertIn(VIEWER, {k["owner"] for k in own["keys"]})
        self.assertIn(OWN, {k["created_by"] for k in own["keys"]})       # an owner sees emails
        for who in (VIEWER, ANN, BOSS):
            st, js, raw, _ = call("GET", "/api/apikeys", who)
            self.assertEqual(st, 200)
            self.assertNotIn("listed", js)
            self.assertFalse(js["me"]["key_owner"])
            self.assertEqual(js["me"]["can_create"], who == BOSS)
            for k in js["keys"]:
                self.assertEqual(k["owner"], "you", (who, k))            # only their own keys
                self.assertNotIn("hash", k)
            body = json.dumps(js["keys"])
            for p in PEOPLE:
                self.assertNotIn(p, body, (who, p))                      # no email, not even their own
            for p in PEOPLE:
                if p != who:
                    self.assertNotIn(p, raw, (who, p))
        _, v, _, _ = call("GET", "/api/apikeys", VIEWER)
        mine = {k["name"]: k for k in v["keys"]}
        self.assertEqual(mine["for viewer"]["created_by"], "owner")      # the issuer as a role label
        # a refusal's detail names nobody either
        st, js, _, _ = create(BOSS, "x", STRANGER)
        self.assertEqual(st, 403)
        self.assertNotIn(STRANGER, js["detail"])

    def test_bad_bodies(self):
        for body in ({"action": "create", "name": "x", "owner": OWN, "scopes": ["admin"]},
                     {"action": "create", "name": "x", "owner": OWN, "expires": "never"},
                     {"action": "request", "name": "x", "owner": OWN},
                     {"action": "create", "name": "", "owner": OWN},
                     {"action": "rotate"},
                     {"action": "create", "name": "x", "owner": OWN, "test": True}):
            st, js, _, _ = call("POST", "/api/apikeys", OWN, body)
            self.assertEqual((st, js["error"]), (400, "bad_body"), body)
        st, js, _, _ = call("POST", "/api/apikeys", OWN, {"action": "create", "name": "x", "owner": OWN,
                                                          "expires": "999d"})
        self.assertEqual((st, js["error"]), (400, "bad_request"), js)     # the CLI's own limit (exit 2)
        self.assertEqual(call("POST", "/api/apikeys/ABC", OWN, {"action": "revoke"})[0], 404)

    def test_api_me_lists_own_keys(self):
        _, made, _, _ = create(BOSS, "listed in me", BOSS)
        st, js, raw, _ = call("GET", "/api/me", BOSS)
        self.assertEqual(st, 200)
        row = next(k for k in js["api_keys"] if k["id"] == made["id"])
        self.assertEqual(set(row), {"id", "name", "scopes", "created_at", "expires_at", "last_used_at", "status"})
        self.assertNotIn(made["key"], raw)
        self.assertNotIn("hash", raw)
        _, js, _, _ = call("GET", "/api/me", VIEWER)
        self.assertNotIn(made["id"], [k["id"] for k in js["api_keys"]])

    def test_local_listener(self):
        # `local` is in no role list here: it may only request
        st, js, _, _ = create(None, "local try", "local")
        self.assertEqual((st, js["error"]), (403, "not_authorized"))
        st, js, _, _ = create(None, "local request", action="request")
        self.assertEqual((st, js["status"]), (201, "pending"))
        _, lst, _, _ = call("GET", "/api/apikeys", None)
        self.assertEqual({k["owner"] for k in lst["keys"]}, {"local"})


class HealthCheck(Base):
    def test_the_snippet_runs(self):
        if not all(shutil.which(x) for x in ("bash", "curl", "jq")):
            self.skipTest("bash, curl and jq are needed")
        _, made, _, _ = create(OWN, "health", "local", scopes=("read:state",))
        _, info, _, _ = call("GET", "/api/dev", None)
        text = next(s["text"] for s in info["snippets"] if s["id"] == "health")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "BOARD_API_KEY": made["key"], "HOME": TMP}
        r = subprocess.run(["bash", "-c", "set -o pipefail\n" + text], capture_output=True, text=True, env=env,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [json.loads(x) for x in r.stdout.strip().splitlines()]
        self.assertEqual(lines[0], {"name": "health", "scopes": ["read:state"], "error": None})
        self.assertEqual(lines[1]["server"], "agent-switchboard")
        self.assertIsNone(lines[1]["error"])
        self.assertIn("list_sessions", lines[2])
        self.assertNotIn("list_tasks", lines[2])                     # filtered by the key's scopes
        self.assertEqual(lines[3], {"isError": False, "sessions": ["API"], "error": None})
        self.assertNotIn(made["key"], r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[sys.argv[0]])
