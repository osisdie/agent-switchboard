#!/usr/bin/env python3
"""tests/test_api_v1.py — board-serve's public API (/api/v1: lib/serve_api.py, lib/api_spec.py)
and its tunnel exposure: keys (lib/apikeys.py) as the only identity, the scope matrix, 401 / 403 /
429, the spec equal to the router, no CORS, the framing rules of every other route, and on the
tunnel an Access service token (common_name allowlist) or an allowed email AND a key. JWTs are
signed with a throwaway RSA key. Every key and token is made at run time.

  python3 tests/test_api_v1.py [-v]
  API_REQUIRE_CRYPTO=1 makes a missing python3 cryptography a failure instead of a skip (CI).
"""
import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

TMP = tempfile.mkdtemp(prefix="switchboard-api-test.")
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
os.makedirs(CFG)
os.makedirs(HOME, mode=0o700)
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "ALERT_DESKTOP": "0"})
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_NOW")) or k in ("TMUX", "TMUX_PANE"):
        os.environ.pop(k, None)
os.environ["ALERT_DESKTOP"] = "0"

import api_spec  # noqa: E402
import apikeys  # noqa: E402
import serve_api  # noqa: E402

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - CI installs python3-cryptography
    HAVE_CRYPTO = False

J = "".join
OWNER, BOSS, DEV = "owner@example.com", "boss@example.com", "dev@example.com"
SVC, SVC_OTHER = "0a1b2c3d4e5f.access", "ffffeeee0000.access"
HOSTNAME = "board.example.com"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT, TPORT = free_port(), free_port()


def write_fleet(remote=True, fail_per_min=1000):
    with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
        fh.write("""owners: [%s]
approvers: [%s]
serve:
  port: %d
  api:
    remote: %s
    auth_fail_per_min: %d
  tunnel:
    enabled: true
    port: %d
    hostname: %s
    team_domain: team.example.com
    aud: AUD123
    certs_url: file://%s/certs.json
    allowed_emails: [%s]
    api_service_tokens: [%s]
""" % (OWNER, BOSS, PORT, "true" if remote else "false", fail_per_min, TPORT, HOSTNAME, TMP, BOSS, SVC.upper()))


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


NOW = int(time.time() * 1000)
TODAY = time.strftime("%Y-%m-%d", time.localtime(NOW / 1000))
FAKE_TOKEN = J(["ghp_", "abcdefghij", "ABCDEFGHIJ", "0123456789"])


def fixtures():
    wj(os.path.join(HOME, "snapshot.json"), {"generated_at": NOW, "sessions": [
        {"code": "API", "aliases": ["API"], "state": "working", "status": "busy", "updated_at": NOW, "task_id": "t-1"},
        {"code": "WEB", "aliases": ["WEB"], "state": "idle", "updated_at": NOW}]})
    wj(os.path.join(HOME, "cards.json"), {"sessions": {"API": {"prompt": "private prompt", "doing": "private text",
                                                               "branch": "feat/x"}}})
    for did, status in (("d-1-a", "pending"), ("d-1-b", "answered")):
        wj(os.path.join(HOME, "decisions", did + ".json"), {
            "id": did, "kind": "question", "from": "API", "question": "Ship?", "options": ["yes", "no"],
            "status": status, "created_at": NOW, "secret_field": "x"})
    wj(os.path.join(HOME, "tasks", "t-1.json"), {"id": "t-1", "title": "Ship it", "owner": "API", "stage": "build",
                                                 "artifacts": {}, "gates": [], "updated_at": NOW})
    wj(os.path.join(HOME, "messages", "m-1-a.json"), {"id": "m-1-a", "to": "API", "by": "local", "status": "replied",
                                                      "body": "use token %s" % FAKE_TOKEN, "created_at": NOW})
    wj(os.path.join(HOME, "messages", "m-1-a", "replies", "r-1-a.json"), {"from": "API", "body": "done",
                                                                          "created_at": NOW})
    wj(os.path.join(HOME, "pomodoro.json"), {"version": 2, "at": NOW, "settings": {"mode": "accumulate"},
                                             "sessions": {}, "days": {TODAY: {"API": {"done": 2, "focus_ms": 3000000}}},
                                             "notified": {}})
    wj(os.path.join(HOME, "scheduler.json"), {"mode": "normal", "lessons": {"applied": [], "hints": []}})
    wj(os.path.join(HOME, "operator.json"), {"heartbeat_at": NOW, "version": "t", "operator_session": "BOARD",
                                             "account_hint": "second laptop"})
    wj(os.path.join(HOME, "account.json"), {"email": "account-holder@example.com", "org": "x"})


def mkkey(scopes, owner=DEV, *extra):
    r = subprocess.run([os.path.join(REPO, "bin", "board"), "apikey", "create", "--name", "t " + scopes[:40],
                        "--owner", owner, "--by", OWNER, "--scopes", scopes, "--json"] + list(extra),
                       capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    return d["id"], d["key"]


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwts():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    num = lambda n: b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))  # noqa: E731
    wj(os.path.join(TMP, "certs.json"), {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256",
                                                   "n": num(pub.n), "e": num(pub.e)}]})

    def tok(claims, k=key):
        h = b64(json.dumps({"alg": "RS256", "kid": "k1"}).encode())
        p = b64(json.dumps(claims).encode())
        return h + "." + p + "." + b64(k.sign((h + "." + p).encode(), padding.PKCS1v15(), hashes.SHA256()))
    now = int(time.time())
    base = {"aud": ["AUD123"], "iss": "https://team.example.com", "exp": now + 3600, "iat": now}
    svc = dict(base, common_name=SVC, sub="", type="app")
    return {"svc": tok(svc), "svc_other": tok(dict(svc, common_name=SVC_OTHER)),
            "svc_badsig": tok(svc, other), "svc_expired": tok(dict(svc, exp=now - 3600)),
            "email": tok(dict(base, email=BOSS)), "email_other": tok(dict(base, email="intruder@example.com")),
            "neither": tok(base)}


class Http:
    @staticmethod
    def req(path, key=None, method="GET", port=None, host=None, headers=None, body=None):
        port = port or PORT
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        hdrs = {"Host": host or "127.0.0.1:%d" % port}
        if key:
            hdrs["Authorization"] = "Bearer " + key
        hdrs.update(headers or {})
        c.request(method, path, body=body, headers=hdrs)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            js = json.loads(data) if data else None
        except ValueError:
            js = None
        return r.status, {k.lower(): v for k, v in r.getheaders()}, js

    @staticmethod
    def raw(data, port=None):
        s = socket.create_connection(("127.0.0.1", port or PORT), timeout=5)
        s.sendall(data.encode())
        out = b""
        try:
            while True:
                c = s.recv(65536)
                if not c:
                    break
                out += c
        except socket.timeout:
            pass
        s.close()
        return out.decode("latin-1")


def events():
    with open(os.path.join(HOME, "events.jsonl"), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


SRV = None
KEYS = {}


def setUpModule():
    global SRV
    write_fleet()
    fixtures()
    if HAVE_CRYPTO:
        KEYS["jwt"] = make_jwts()
    for name, scopes in (("all", ",".join(apikeys.SCOPES)), ("state", "read:state"), ("decisions", "read:decisions"),
                         ("tasks", "read:tasks"), ("messages", "read:messages"), ("tickets", "tickets:read")):
        KEYS[name] = mkkey(scopes)
    KEYS["slow"] = mkkey("read:state", DEV, "--rate", "1")
    KEYS["test"] = mkkey("read:state", DEV, "--test")
    if not HAVE_CRYPTO:
        with open(os.path.join(CFG, "fleet.yaml")) as fh:
            txt = fh.read()
        with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
            fh.write(txt.replace("enabled: true", "enabled: false"))
    SRV = subprocess.Popen([sys.executable, os.path.join(REPO, "lib", "serve.py"), "--quiet"],
                           stdout=subprocess.DEVNULL, stderr=open(os.path.join(TMP, "serve.err"), "w"))
    for _ in range(150):
        try:
            Http.req("/api/v1/openapi.json")
            if HAVE_CRYPTO:
                socket.create_connection(("127.0.0.1", TPORT), timeout=1).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("board-serve did not start: " + open(os.path.join(TMP, "serve.err")).read())


def tearDownModule():
    if SRV:
        SRV.terminate()
        SRV.wait(10)
    shutil.rmtree(TMP, ignore_errors=True)


def example_path(route):
    """A concrete path for a route, with the fixture's ids."""
    p = route["path"].format(code="API", decision_id="d-1-a", task_id="t-1", message_id="m-1-a")
    return p + ("?d=" + TODAY if route["handler"] == "pomodoro_day" else "")


ROUTES = [r for r in api_spec.ROUTES if r["handler"] != "events"]


def open_stream(path, key=None, port=None):
    """An open SSE connection that got its 200 and first frame; the caller closes it."""
    port = port or PORT
    s_ = socket.create_connection(("127.0.0.1", port), timeout=10)
    s_.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n%s\r\n"
                % (path, port, "Authorization: Bearer %s\r\n" % key if key else "")).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s_.recv(65536)
        if not c:
            break
        buf += c
    if not buf.startswith(b"HTTP/1.1 200"):
        s_.close()
        raise AssertionError(buf[:60])
    while b"\n\n" not in buf.split(b"\r\n\r\n", 1)[1]:
        c = s_.recv(65536)
        if not c:
            break
        buf += c
    return s_


def page_events_status():
    s_ = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    s_.sendall(("GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % PORT).encode())
    head = s_.recv(200)
    s_.close()
    return int(head.split(b" ")[1]), head


def serve_api_reset_local():
    """The local listener's junk budget refills at the restored rate; wait for one token."""
    time.sleep(0.2)


class Fresh(unittest.TestCase, Http):
    def setUp(self):
        # a fresh all-scope key per test: the burst (20) is per key
        self.all = mkkey(",".join(apikeys.SCOPES), DEV, "--rate", "600")[1]


class Local(Fresh):
    def test_local_is_not_an_identity(self):
        for r in api_spec.ROUTES:
            if r["scope"] == "public":
                continue
            st, hdr, js = self.req(example_path(r))
            self.assertEqual((st, js["error"]), (401, "unauthenticated"), r["path"])
            self.assertIn("Bearer", hdr.get("www-authenticate", ""))
        for bad in ("Bearer local", "Basic bG9jYWw6eA==", "Bearer", "Token " + self.all):
            st, _, js = self.req("/api/v1/whoami", headers={"Authorization": bad})
            self.assertEqual(st, 401, bad)

    def test_scope_matrix(self):
        for r in ROUTES:
            for name in ("all", "state", "decisions", "tasks", "messages", "tickets"):
                want = 200 if r["scope"] in (None, "public") or name == "all" or \
                    apikeys.load(HOME, KEYS[name][0])["scopes"] == [r["scope"]] else 403
                with self.subTest(route=r["path"], key=name):
                    st, _, js = self.req(example_path(r), KEYS[name][1])
                    self.assertEqual(st, want, js)
                    if want == 403:
                        self.assertEqual(js["error"], "insufficient_scope")
                        self.assertIn(r["scope"], js["detail"])

    def test_whoami(self):
        st, _, js = self.req("/api/v1/whoami", KEYS["state"][1])
        self.assertEqual(st, 200)
        self.assertEqual((js["key_id"], js["owner"], js["scopes"], js["mode"]),
                         (KEYS["state"][0], DEV, ["read:state"], "live"))

    def test_invalid_keys(self):
        kid, key = mkkey("read:state")
        tampered = key[:-1] + ("A" if key[-1] != "A" else "B")
        rev_id, revoked = mkkey("read:state")
        subprocess.run([os.path.join(REPO, "bin", "board"), "apikey", "revoke", rev_id, "--by", OWNER], check=True,
                       capture_output=True)
        exp_id, expired = mkkey("read:state")
        rec = apikeys.load(HOME, exp_id)
        rec["expires_at"] = NOW - 1000
        apikeys.save(HOME, rec)
        req = subprocess.run([os.path.join(REPO, "bin", "board"), "apikey", "request", "--name", "p", "--by", DEV,
                              "--json"], capture_output=True, text=True, check=True)
        pending = json.loads(req.stdout)["key"]
        for name, k in (("tampered", tampered), ("revoked", revoked), ("expired", expired), ("pending", pending),
                        ("garbage", "asb_live_nope"), ("other", "ghp_" + "x" * 36)):
            st, _, js = self.req("/api/v1/whoami", k)
            self.assertEqual((st, js["error"]), (401, "invalid_key"), name)
        st, _, js = self.req("/api/v1/whoami", KEYS["test"][1])
        self.assertEqual((st, js["error"]), (403, "key_wrong_env"))
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        c.putrequest("GET", "/api/v1/whoami", skip_host=True)
        c.putheader("Host", "127.0.0.1:%d" % PORT)
        c.putheader("Authorization", "Bearer " + key)
        c.putheader("Authorization", "Bearer " + key)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 401)       # two Authorization headers
        c.close()
        fails = [e for e in events() if e.get("type") == "api" and e.get("action") == "auth_fail"]
        reasons = {(e.get("key_id"), e["reason"]) for e in fails}
        for k_id, reason in ((rev_id, "revoked"), (exp_id, "expired"), (kid, "mismatch"),
                             (KEYS["test"][0], "wrong_env")):
            self.assertIn((k_id, reason), reasons)
        with open(os.path.join(HOME, "events.jsonl"), encoding="utf-8") as fh:
            raw = fh.read()
        for k in (key, revoked, expired, pending, self.all, KEYS["all"][1]):
            self.assertNotIn(k[len("asb_live_") + 13:], raw)

    def test_rate_limit(self):
        st, _, _ = self.req("/api/v1/whoami", KEYS["slow"][1])
        self.assertEqual(st, 200)
        st, hdr, js = self.req("/api/v1/whoami", KEYS["slow"][1])
        self.assertEqual((st, js["error"]), (429, "rate_limited"))
        self.assertGreaterEqual(int(hdr["retry-after"]), 1)

    def test_no_cors(self):
        st, hdr, _ = self.req("/api/v1/state", method="OPTIONS",
                              headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET",
                                       "Access-Control-Request-Headers": "authorization"})
        self.assertEqual(st, 405)
        self.assertFalse([h for h in hdr if h.startswith("access-control-")])
        for path in ("/api/v1/state", "/api/v1/openapi.json", "/api/v1/whoami"):
            st, hdr, js = self.req(path, self.all, headers={"Origin": "https://evil.example"})
            self.assertEqual((st, js["error"]), (403, "cross_site"))
            self.assertFalse([h for h in hdr if h.startswith("access-control-")], path)
            st, hdr, _ = self.req(path, self.all)
            self.assertEqual(st, 200)
            self.assertFalse([h for h in hdr if h.startswith("access-control-")], path)

    def test_cross_site_browser_requests_refused(self):
        before = len(events())
        own = "http://127.0.0.1:%d" % PORT
        for hdrs, want in (({"Sec-Fetch-Site": "cross-site"}, 403), ({"Sec-Fetch-Site": "same-site"}, 403),
                           ({"Origin": "https://evil.example"}, 403), ({"Origin": "null"}, 403),
                           ({"Origin": "http://localhost:%d" % PORT, "Sec-Fetch-Site": "cross-site"}, 403),
                           ({"Sec-Fetch-Site": "same-origin", "Origin": own}, 200),
                           ({"Sec-Fetch-Site": "none"}, 200), ({"Origin": own}, 200)):
            for key in (self.all, None):
                st, _, js = self.req("/api/v1/whoami", key, headers=hdrs)
                self.assertEqual(st, want if key or want == 403 else 401, (hdrs, bool(key)))
                if want == 403:
                    self.assertEqual(js["error"], "cross_site")
        self.assertEqual([e for e in events()[before:] if e.get("action") == "auth_fail"], [])

    def test_keyless_requests_cost_nothing(self):
        """An <img src=…/api/v1/state> flood: 401 each, no audit row, no failure budget used."""
        write_fleet(fail_per_min=3)
        try:
            before = len(events())
            for _ in range(30):
                st, hdr, js = self.req("/api/v1/state")
                self.assertEqual((st, js["error"]), (401, "unauthenticated"))
                self.assertIn("Bearer", hdr.get("www-authenticate", ""))
            self.assertEqual([e for e in events()[before:] if e.get("type") == "api"], [])
            self.assertEqual(self.req("/api/v1/whoami", self.all)[0], 200)
            # the budget is intact: junk counts (and is audited) until the limit
            got = [self.req("/api/v1/whoami", headers={"Authorization": "Bearer junk"})[0] for _ in range(4)]
            self.assertEqual(got, [401, 401, 401, 429])
            self.assertEqual(self.req("/api/v1/whoami", self.all)[0], 200)        # a real key is not locked out
            rows = [e for e in events()[before:] if e.get("action") == "auth_fail"]
            self.assertEqual([(r["reason"], r["key_id"]) for r in rows], [("malformed", None)] * 3)
        finally:
            write_fleet()
            serve_api_reset_local()

    def test_keyless_events_hold_no_slot(self):
        socks = []
        try:
            for _ in range(40):                          # more than the page's 32 slots
                self.assertEqual(self.req("/api/v1/events")[0], 401)
            st, _ = page_events_status()
            self.assertEqual(st, 200)                    # the page's stream still gets a slot
            for _ in range(serve_api.MAX_API_SSE):        # the API's own cap
                socks.append(open_stream("/api/v1/events", self.all))
            st, _, js = self.req("/api/v1/events", self.all)
            self.assertEqual((st, js["error"]), (503, "too_many_streams"))
            self.assertEqual(page_events_status()[0], 200)
        finally:
            for s_ in socks:
                s_.close()
            # the server notices a closed stream on its next writes: make the state change until a
            # slot is free again, so later tests are not left without one
            deadline = time.time() + 20
            i = 0
            while time.time() < deadline:
                i += 1
                wj(os.path.join(HOME, "decisions", "d-9%d-z.json" % i), {"id": "d-9%d-z" % i, "kind": "question",
                                                                        "status": "stale", "question": "tick",
                                                                        "created_at": NOW - 1})
                time.sleep(1.2)
                st, _, _ = self.req("/api/v1/whoami", self.all)
                try:
                    open_stream("/api/v1/events", self.all).close()
                    break
                except AssertionError:
                    continue

    def test_read_only_and_framing(self):
        st, hdr, js = self.req("/api/v1/state", self.all, method="POST", body=b'{"x": 1}',
                               headers={"Content-Type": "application/json"})
        self.assertEqual((st, js["error"], hdr.get("connection")), (405, "method_not_allowed", "close"))
        host = "127.0.0.1:%d" % PORT
        smuggled = "GET /api/v1/openapi.json HTTP/1.1\r\nHost: %s\r\n\r\n" % host
        for method in ("GET", "HEAD", "PUT", "OPTIONS", "POST"):
            r = self.raw("%s /api/v1/whoami HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer %s\r\n"
                         "Content-Length: %d\r\n\r\n%s" % (method, host, self.all, len(smuggled), smuggled))
            self.assertEqual(r.count("HTTP/1.1 "), 1, method)
        for hdrs in ("Transfer-Encoding: chunked\r\nContent-Length: 4\r\n", "Content-Length: 2\r\nContent-Length: 2\r\n",
                     "Content-Length: +2\r\n"):
            for method in ("GET", "POST"):
                r = self.raw("%s /api/v1/whoami HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer %s\r\n%s\r\n{}"
                             % (method, host, self.all, hdrs))
                self.assertTrue(r.startswith("HTTP/1.1 400 "), (method, hdrs, r[:40]))
        st, _, _ = self.req("/api/v1/whoami", self.all, host="evil.example")
        self.assertEqual(st, 421)
        st, _, js = self.req("/api/v1/whoami", self.all, headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual((st, js["error"]), (403, "forwarded_refused"))

    def test_viewer_state_and_scrub(self):
        st, _, js = self.req("/api/v1/state", KEYS["state"][1])
        self.assertEqual(st, 200)
        api = [s for s in js["sessions"] if s["code"] == "API"][0]
        self.assertEqual(api["card"].get("private"), "hidden")
        self.assertNotIn("prompt", api["card"])
        self.assertEqual((js["account"], js["writable"], js["can_message"], js["mode"]), (None, False, False, "api"))
        self.assertIsNone(js["operator"]["account_hint"])
        self.assertNotIn("account-holder@example.com", json.dumps(js))
        st, _, js = self.req("/api/v1/messages/m-1-a", KEYS["messages"][1])
        self.assertEqual(st, 200)
        self.assertNotIn(FAKE_TOKEN, json.dumps(js))
        self.assertIn("[REDACTED]", js["body"])
        self.assertEqual([r["body"] for r in js["replies"]], ["done"])
        st, _, js = self.req("/api/v1/decisions/d-1-a", KEYS["decisions"][1])
        self.assertNotIn("secret_field", js)

    def test_queries_and_not_found(self):
        k = self.all
        st, _, js = self.req("/api/v1/decisions?status=pending", k)
        self.assertEqual([d["id"] for d in js["decisions"]], ["d-1-a"])
        for path in ("/api/v1/decisions?status=bogus", "/api/v1/decisions?status=pending&status=answered",
                     "/api/v1/tasks?x=1", "/api/v1/pomodoro/day", "/api/v1/pomodoro/day?d=today"):
            self.assertEqual(self.req(path, k)[0], 400, path)
        for path in ("/api/v1/decisions/d-9-zz", "/api/v1/tasks/nope", "/api/v1/sessions/NOPE",
                     "/api/v1/messages/m-9-z", "/api/v1/pomodoro/day?d=2001-01-01", "/api/v1", "/api/v1/",
                     "/api/v1/state/x", "/api/v1/decisions/..%2F..%2Fx", "/api/v1/tasks/..", "/api/v1/env"):
            self.assertEqual(self.req(path, k)[0], 404, path)
        self.assertEqual(self.req("/api/v1/tasks/t-1", k)[2]["title"], "Ship it")
        self.assertEqual(self.req("/api/v1/sessions/WEB", k)[2]["state"], "idle")
        self.assertEqual(self.req("/api/v1/pomodoro/day?d=" + TODAY, k)[0], 200)

    def test_mark_used_and_audit(self):
        kid, key = mkkey("read:tasks", BOSS)
        self.assertIsNone(apikeys.load(HOME, kid)["last_used_at"])
        for _ in range(3):
            self.assertEqual(self.req("/api/v1/tasks", key)[0], 200)
        self.assertIsInstance(apikeys.load(HOME, kid)["last_used_at"], int)
        rows = [e for e in events() if e.get("type") == "api" and e.get("key_id") == kid]
        self.assertEqual(len(rows), 1)                       # one audited success per minute and route
        self.assertEqual((rows[0]["owner"], rows[0]["name"], rows[0]["action"], rows[0]["result"], rows[0]["via"]),
                         (BOSS, "t read:tasks", "tasks", 200, "local"))

    def test_sse_ends_when_the_key_is_revoked(self):
        kid, key = mkkey("read:state")
        s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        s.sendall(("GET /api/v1/events HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n\r\n"
                   % (PORT, key)).encode())
        buf = b""
        while b"event: state" not in buf:
            buf += s.recv(65536)
        self.assertIn(b"200 OK", buf)
        subprocess.run([os.path.join(REPO, "bin", "board"), "apikey", "revoke", kid, "--by", OWNER], check=True,
                       capture_output=True)
        wj(os.path.join(HOME, "decisions", "d-2-a.json"), {"id": "d-2-a", "kind": "question", "status": "pending",
                                                         "question": "again?", "created_at": NOW + 1})
        deadline = time.time() + 10
        while b"event: revoked" not in buf and time.time() < deadline:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        self.assertIn(b"event: revoked", buf)
        self.assertEqual(buf.count(b"event: state"), 1)


class TicketText(Fresh):
    """Owner decision Q3 on /api/v1: an API key is never an approver or release manager, so ticket
    text (a take's task title, ticket.title/url, the session's task title, a ticket relay message's
    ticket) never reaches it, through any route or the event stream."""

    TITLE = J(["TKT-", "SENTINEL-", "9v2"]) + " tracker title"

    def setUp(self):
        super().setUp()
        self.snap = os.path.join(HOME, "snapshot.json")
        with open(self.snap, encoding="utf-8") as fh:
            self.snap_doc = json.load(fh)
        snap = json.loads(json.dumps(self.snap_doc))
        for s_ in snap["sessions"]:
            if s_["code"] == "WEB":
                s_["task_id"] = "t-9"
        wj(self.snap, snap)
        wj(os.path.join(HOME, "tasks", "t-9.json"), {
            "id": "t-9", "title": "WEB-9: " + self.TITLE, "owner": "WEB", "stage": "plan", "artifacts": {}, "gates": [],
            "updated_at": NOW + 5, "ticket": {"provider": "tracker", "key": "WEB-9", "title_at_take": self.TITLE,
                                              "url": "https://tracker.example.com/p/WEB-9"}})
        wj(os.path.join(HOME, "messages", "m-9-t.json"), {
            "id": "m-9-t", "kind": "ticket", "event": "taken", "to": "WEB", "by": BOSS, "status": "new", "task_id": "t-9",
            "ticket": {"provider": "tracker", "key": "WEB-9", "title": self.TITLE, "url": "https://tracker.example.com/p/WEB-9"},
            "body": "ticket WEB-9 taken for WEB (task t-9)", "created_at": NOW + 5})
        time.sleep(1.5)     # the hub picks the files up

    def tearDown(self):
        wj(self.snap, self.snap_doc)
        for f in (os.path.join(HOME, "tasks", "t-9.json"), os.path.join(HOME, "messages", "m-9-t.json")):
            os.remove(f)
        time.sleep(1.2)

    def assertClean(self, text, where):
        self.assertNotIn("SENTINEL", text, where)
        self.assertNotIn("tracker.example.com/p/", text, where)

    def test_no_ticket_text_to_a_key(self):
        paths = {example_path(r) for r in ROUTES} | {"/api/v1/tasks/t-9", "/api/v1/messages/m-9-t", "/api/v1/sessions/WEB"}
        for path in sorted(paths):
            with self.subTest(path=path):
                st, _, js = self.req(path, self.all)
                self.assertEqual(st, 200, (path, js))
                self.assertClean(json.dumps(js, ensure_ascii=False), path)
        st, _, js = self.req("/api/v1/tasks/t-9", self.all)
        self.assertEqual((js["title"], js["ticket"]), ("WEB-9", {"provider": "tracker", "key": "WEB-9"}))
        st, _, js = self.req("/api/v1/sessions/WEB", self.all)
        self.assertEqual(js["task"], "WEB-9")
        st, _, js = self.req("/api/v1/messages/m-9-t", self.all)
        self.assertEqual(js["ticket"], {"provider": "tracker", "key": "WEB-9"})
        st, _, js = self.req("/api/v1/state", self.all)
        self.assertIn("t-9", [t["id"] for t in js["tasks"]], "the ticket task is there, without its text")
        # The event stream's first frame.
        s_ = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        s_.sendall(("GET /api/v1/events HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n\r\n"
                    % (PORT, self.all)).encode())
        buf = b""
        while b"event: state" not in buf or not buf.split(b"event: state", 1)[1].count(b"\n\n"):
            c = s_.recv(65536)
            if not c:
                break
            buf += c
        s_.close()
        frame = buf.decode("utf-8", "replace")
        self.assertIn("event: state", frame)
        self.assertIn("WEB-9", frame)
        self.assertClean(frame, "/api/v1/events")
        # Only the views are filtered: the file on disk keeps the text.
        with open(os.path.join(HOME, "tasks", "t-9.json"), encoding="utf-8") as fh:
            self.assertIn("SENTINEL", fh.read())


class Unit(unittest.TestCase):
    def test_limiter_tables_are_pruned(self):
        serve_api.reset_limits()
        t0 = 1000.0
        for i in range(5000):
            serve_api._fails["tunnel:203.0.113.%d" % i] = (0.0, t0)
            serve_api._buckets["k%d" % i] = (0.0, t0)
            serve_api._audited[("k%d" % i, "state")] = t0
        serve_api._take(serve_api._fails, "fresh", 10, 10, t0 + 30)     # nothing idle a minute yet
        self.assertEqual(len(serve_api._fails), 5001)
        serve_api._take(serve_api._fails, "fresh2", 10, 10, t0 + 61)
        self.assertEqual(set(serve_api._fails), {"fresh", "fresh2"})
        self.assertEqual(serve_api._buckets, {})
        self.assertEqual(serve_api._audited, {})
        serve_api.reset_limits()

    def test_junk_never_reaches_a_file_lookup(self):
        real = apikeys.load

        def boom(*_a, **_k):
            raise AssertionError("load called")
        apikeys.load = boom
        try:
            for junk in ("junk", "asb_live_x", "asb_live_" + "a" * 12 + "_short", "../../etc/passwd", ""):
                self.assertEqual(apikeys.verify(junk, home=HOME), (None, "malformed"))
        finally:
            apikeys.load = real


class Spec(Fresh):
    def test_spec_equals_router(self):
        st, _, served = self.req("/api/v1/openapi.json")
        self.assertEqual(st, 200)
        self.assertEqual(served, json.loads(json.dumps(api_spec.openapi())))
        spec_ops = {(m.upper(), p) for p, ops in served["paths"].items() for m in ops}
        router_ops = {(r["method"], r["path"]) for r in serve_api.ROUTES}
        self.assertEqual(spec_ops, router_ops)
        self.assertEqual({r["handler"] for r in serve_api.ROUTES},
                         {op["operationId"] for ops in served["paths"].values() for op in ops.values()})
        for r in serve_api.ROUTES:
            self.assertTrue(callable(getattr(serve_api.Api, r["handler"], None)), r["handler"])
            self.assertTrue(r["rx"].fullmatch(example_path(r).split("?")[0]), r["path"])
            op = served["paths"][r["path"]][r["method"].lower()]
            self.assertEqual(op.get("x-scope", "public"), r["scope"] if r["scope"] != "public" else "public")
        for r in ROUTES:
            st, _, _ = self.req(example_path(r), self.all)
            self.assertEqual(st, 200, r["path"])
        self.assertFalse([s for r in api_spec.ROUTES for s in [r["scope"]] if s and s not in apikeys.SCOPES
                          and s != "public"])
        self.assertFalse([r for r in api_spec.ROUTES if r["method"] != "GET"])      # A2 is read-only

    def test_board_routes_unchanged(self):
        """The board's own routes still need a person, never a key."""
        st, _, js = self.req("/api/state", self.all, port=PORT)
        self.assertEqual(st, 200)                                 # local listener: `local`, as before
        self.assertEqual(js["me"], "local")


class Tunnel(Fresh):
    def setUp(self):
        if not HAVE_CRYPTO:
            if os.environ.get("API_REQUIRE_CRYPTO") == "1":
                self.fail("python3 cryptography is required (API_REQUIRE_CRYPTO=1)")
            self.skipTest("python3 cryptography missing")
        super().setUp()

    def t(self, path, jwt=None, key=None, ip=None, **kw):
        h = dict(kw.pop("headers", {}))
        if jwt:
            h["Cf-Access-Jwt-Assertion"] = KEYS["jwt"][jwt]
        h["Cf-Connecting-Ip"] = ip or "198.51.100.7"
        return self.req(path, key, port=TPORT, host=kw.pop("host", HOSTNAME), headers=h, **kw)

    def test_service_token_and_key(self):
        k = self.all
        self.assertEqual(self.t("/api/v1/whoami", "svc", k)[0], 200)
        st, _, js = self.t("/api/v1/whoami", "svc")
        self.assertEqual((st, js["error"]), (401, "unauthenticated"))          # token without a key
        st, _, js = self.t("/api/v1/whoami", "svc_other", k)
        self.assertEqual((st, js["error"]), (403, "not_allowed"))              # not on the allowlist
        for bad in ("svc_badsig", "svc_expired", "neither"):
            self.assertEqual(self.t("/api/v1/whoami", bad, k)[0], 401, bad)
        st, _, js = self.t("/api/v1/whoami", None, k)
        self.assertEqual((st, js["detail"]), (401, "Cloudflare Access token missing"))
        self.assertEqual(self.t("/api/v1/state", "svc", KEYS["decisions"][1])[0], 403)   # scopes still apply
        self.assertEqual(self.t("/api/v1/whoami", "svc", k, host="127.0.0.1:%d" % TPORT)[0], 421)
        row = [e for e in events() if e.get("type") == "api" and e.get("via") == "tunnel" and e.get("result") == 200]
        self.assertEqual(row[-1]["client"], "service:" + SVC)

    def test_email_jwt(self):
        st, _, js = self.t("/api/v1/whoami", "email")
        self.assertEqual((st, js["error"]), (401, "unauthenticated"))          # an email JWT without a key
        self.assertEqual(self.t("/api/v1/whoami", "email", self.all)[0], 200)
        self.assertEqual(self.t("/api/v1/whoami", "email_other", self.all)[0], 403)

    def test_service_token_is_not_a_person(self):
        """On the board's own routes a service token is no identity: they still need an email."""
        for path in ("/api/state", "/api/me", "/"):
            self.assertEqual(self.t(path, "svc", self.all)[0], 401, path)
        self.assertEqual(self.t("/api/me", "email")[0], 200)

    def test_access_cookie_is_not_a_credential(self):
        st, _, js = self.t("/api/v1/whoami", None, None, headers={"Cookie": "CF_Authorization=" + KEYS["jwt"]["email"]})
        self.assertEqual((st, js["detail"]), (401, "Cloudflare Access token missing"))
        st, _, js = self.t("/api/v1/whoami", "email", None, headers={"Cookie": "CF_Authorization=" + KEYS["jwt"]["email"]})
        self.assertEqual((st, js["error"]), (401, "unauthenticated"))       # signed in, still no key

    def test_remote_off(self):
        write_fleet(remote=False)
        try:
            st, _, js = self.t("/api/v1/whoami", "svc", self.all)
            self.assertEqual((st, js["error"]), (403, "api_remote_disabled"))
            self.assertEqual(self.req("/api/v1/whoami", self.all)[0], 200)   # local still works
        finally:
            write_fleet()

    def test_failed_auth_limited_per_source(self):
        write_fleet(fail_per_min=3)
        try:
            ip = "203.0.113.9"
            got = [self.t("/api/v1/whoami", "svc", "asb_live_bad", ip=ip)[0] for _ in range(5)]
            self.assertEqual(got[:3], [401, 401, 401])
            self.assertEqual(got[3:], [429, 429])
            st, hdr, _ = self.t("/api/v1/whoami", "svc", "asb_live_bad", ip=ip)
            self.assertIn("retry-after", hdr)
            self.assertEqual(self.t("/api/v1/whoami", "svc", "asb_live_bad", ip="203.0.113.10")[0], 401)
            self.assertEqual(self.t("/api/v1/whoami", "svc", self.all, ip=ip)[0], 200)   # junk counts per source
            # a guessed secret counts against its kid, from anywhere
            kid, key = mkkey("read:state")
            wrong = key[:-1] + ("A" if key[-1] != "A" else "B")
            got = [self.t("/api/v1/whoami", "svc", wrong, ip="198.51.100.%d" % i)[0] for i in range(5)]
            self.assertEqual(got, [401, 401, 401, 429, 429])
            rows = [e for e in events() if e.get("action") == "auth_fail" and e.get("key_id") == kid]
            self.assertEqual(len(rows), 3)                            # the 429s are not audited
            self.assertEqual(self.t("/api/v1/whoami", "svc", key, ip="198.51.100.9")[0], 200)   # the holder gets in
            self.assertEqual(self.t("/api/v1/whoami", "svc", wrong, ip="198.51.100.9")[0], 429)
            self.assertEqual(self.t("/api/v1/whoami", "svc", self.all, ip="198.51.100.9")[0], 200)
        finally:
            write_fleet()


if __name__ == "__main__":
    unittest.main()
