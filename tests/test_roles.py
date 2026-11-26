#!/usr/bin/env python3
"""tests/test_roles.py — who may do what on the self-hosted board: a route-by-role matrix for
owner, approver (not owner), release manager, asker and viewer, plus the fallback when fleet.yaml
`owners` is empty (approvers count as owners). Owner-only: notification settings, presence, alert
acks, owner-away, Remote Control, session actions, resuming sessions at a usage limit, lessons,
releasing a held message, and answering permission prompts and pane questions (keys pressed on
the owner's machine). Still
approvers: answering ask decisions, resolve, ticket reads. Askers: messages only.

Identities come from verified Cloudflare Access JWTs (a throwaway RSA key) on the tunnel listener.
No decision here names a live session, so an allowed prompt answer stops before any key press.

  python3 tests/test_roles.py [-v]
  ROLES_REQUIRE_CRYPTO=1 makes a missing python3 cryptography a failure instead of a skip (CI).
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

TMP = tempfile.mkdtemp(prefix="switchboard-roles-test.")
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
os.makedirs(CFG)
os.makedirs(HOME, mode=0o700)
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_NOW")) or k in ("TMUX", "TMUX_PANE",
                                                                                        "BOARD_PUBLIC_URL"):
        os.environ.pop(k, None)
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "ALERT_DESKTOP": "0",
                   "CLAUDE_JSON": os.path.join(TMP, "no-claude.json"),
                   "CLAUDE_SESSIONS_DIR": os.path.join(TMP, "sessions")})

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - CI installs python3-cryptography
    HAVE_CRYPTO = False

OWN, BOSS, RM, ANN, VIEWER = ("own@example.com", "boss@example.com", "rm@example.com", "ann@example.com",
                              "viewer@example.com")
ROLES = {"owner": OWN, "approver": BOSS, "release_manager": RM, "asker": ANN, "viewer": VIEWER}
HOSTNAME = "board.example.com"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT, TPORT = free_port(), free_port()


def write_fleet(owners=True):
    with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
        fh.write(("owners: [%s]\n" % OWN if owners else "") + """approvers: [%s, %s]
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
""" % (OWN, BOSS, RM, ANN, PORT, TPORT, HOSTNAME, TMP, ", ".join(ROLES.values())))


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


NOW = int(time.time() * 1000)
DEC = {"status": "pending", "answer": None, "answered_by": None, "answered_at": None, "relayed_at": None,
       "created_at": NOW}


def decision(did, **kw):
    wj(os.path.join(HOME, "decisions", did + ".json"), dict(DEC, id=did, **kw))


def fixtures():
    wj(os.path.join(HOME, "snapshot.json"), {"generated_at": NOW, "sessions": [
        {"code": "API", "aliases": ["API"], "state": "working", "updated_at": NOW}]})
    # a permission prompt and a pane question of a session that is not running: never pressed
    decision("d-1-perm", kind="permission", session_id="00000000-dead-beef-0000-000000000000",
             tool_use_id="tu_x", tool_name="Bash", summary="ls", summary_sha256="s", options=None)
    decision("d-1-pane", kind="question", source="pane", pressable=True, session_id="00000000-dead-beef-0000-000000000001",
             tool_use_id="tu_y", question="Which one?", options=["A", "B"], relay="press")


def held(n):
    mid = "m-%d-h" % n
    wj(os.path.join(HOME, "messages", mid + ".json"), {"id": mid, "to": "API", "body": "x", "by": ANN,
                                                     "status": "held", "held_reason": "secret request",
                                                     "created_at": NOW})
    return mid


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwts():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    num = lambda n: b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))  # noqa: E731
    wj(os.path.join(TMP, "certs.json"), {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256",
                                                   "n": num(pub.n), "e": num(pub.e)}]})

    def tok(email):
        now = int(time.time())
        h = b64(json.dumps({"alg": "RS256", "kid": "k1"}).encode())
        p = b64(json.dumps({"aud": ["AUD123"], "iss": "https://team.example.com", "exp": now + 3600, "iat": now,
                            "email": email}).encode())
        return h + "." + p + "." + b64(key.sign((h + "." + p).encode(), padding.PKCS1v15(), hashes.SHA256()))
    return {e: tok(e) for e in ROLES.values()}


JWT = {}
SRV = None


def setUpModule():
    global SRV
    if not HAVE_CRYPTO:
        return
    write_fleet()
    fixtures()
    JWT.update(make_jwts())
    SRV = subprocess.Popen([sys.executable, os.path.join(REPO, "lib", "serve.py"), "--quiet"],
                           stdout=subprocess.DEVNULL, stderr=open(os.path.join(TMP, "serve.err"), "w"))
    for _ in range(150):
        try:
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


def req(method, path, who, body=None):
    hdrs = {"Host": HOSTNAME, "Cf-Access-Jwt-Assertion": JWT[who], "X-Board": "1", "Origin": "https://" + HOSTNAME}
    data = None
    if method == "POST":
        data = json.dumps(body or {}).encode()
        hdrs["Content-Type"] = "application/json"
    c = http.client.HTTPConnection("127.0.0.1", TPORT, timeout=30)
    c.request(method, path, body=data, headers=hdrs)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        js = json.loads(raw) if raw else None
    except ValueError:
        js = None
    return r.status, js


def read(*parts):
    try:
        with open(os.path.join(HOME, *parts), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


class Base(unittest.TestCase):
    def setUp(self):
        if not HAVE_CRYPTO:
            if os.environ.get("ROLES_REQUIRE_CRYPTO") == "1":
                self.fail("python3 cryptography is required (ROLES_REQUIRE_CRYPTO=1)")
            self.skipTest("python3 cryptography missing")


# (name, path or a callable that makes a fresh path, body, roles allowed)
OWNER_ONLY = [
    ("notify-prefs", "/api/notify-prefs", {"op": "mute", "channel": "slack", "for": "1h"}),
    ("alerts ack", "/api/alerts/ack", {"scope": "all"}),
    ("away", "/api/away", {"state": "on", "said": "back at five"}),
    ("rc", "/api/rc", {"code": "API", "state": "on"}),
    ("session action", "/api/sessions/API/action", {"action": "compact"}),
    ("resume", "/api/resume", {"code": "API"}),
    ("lesson add", "/api/lessons", {"title": "hold heavy runs while memory is low"}),
    ("release", None, {"confirm": "release"}),
    ("decline", None, {"confirm": "decline", "reason": "not this one"}),
    ("permission answer", "/api/decisions/d-1-perm/answer", {"answer": "deny"}),
    ("pane question answer", "/api/decisions/d-1-pane/answer", {"answer": "A"}),
]


class Matrix(Base):
    def test_owner_only_routes(self):
        n = 0
        for name, path, body in OWNER_ONLY:
            for role in ("viewer", "asker", "release_manager", "approver", "owner"):   # the owner last
                n += 1
                p = path or "/api/messages/%s/%s" % (held(n), body["confirm"])
                with self.subTest(route=name, role=role):
                    st, js = req("POST", p, ROLES[role], body)
                    if role == "owner":
                        self.assertNotEqual(st, 403, (name, js))
                        self.assertLess(st, 500, (name, js))
                    else:
                        self.assertEqual(st, 403, (name, role, js))
                        if role == "approver":
                            self.assertEqual(js["error"], "not_owner", (name, js))
        # refused answers never touched the decisions
        for did in ("d-1-perm", "d-1-pane"):
            self.assertNotEqual(read("decisions", did + ".json")["answered_by"], BOSS)

    def test_presence_counts_only_the_owner(self):
        before = read("presence.json")
        for role in ("viewer", "asker", "release_manager", "approver"):
            st, js = req("POST", "/api/presence", ROLES[role], {"visible": False, "idle_ms": 5})
            self.assertEqual((st, js), (204, None), role)
        self.assertEqual(read("presence.json"), before)            # not stored, not counted
        self.assertEqual(req("POST", "/api/presence", OWN, {"visible": True, "idle_ms": 0})[0], 200)
        self.assertIn(OWN, json.dumps(read("presence.json")))

    def test_approver_routes_stay_approver(self):
        decision("d-2-q", kind="question", **{"from": "API"}, question="Ship?", options=["yes", "no"])
        self.assertEqual(req("POST", "/api/decisions/d-2-q/answer", ANN, {"answer": "yes"})[0], 403)
        self.assertEqual(req("POST", "/api/decisions/d-2-q/answer", RM, {"answer": "yes"})[0], 403)
        st, js = req("POST", "/api/decisions/d-2-q/answer", BOSS, {"answer": "yes"})
        self.assertEqual(st, 200, js)                     # an approver who is not an owner still answers
        decision("d-3-q", kind="question", **{"from": "API"}, question="Done?", options=["yes"])
        self.assertEqual(req("POST", "/api/decisions/d-3-q/resolve", VIEWER, {"confirm": "resolve"})[0], 403)
        st, js = req("POST", "/api/decisions/d-3-q/resolve", BOSS, {"confirm": "resolve"})
        self.assertEqual(st, 200, js)
        for role, ok in (("approver", True), ("release_manager", True), ("owner", True), ("asker", False),
                         ("viewer", False)):
            st, js = req("GET", "/api/tickets/PROJ-1", ROLES[role])
            self.assertEqual(st == 403, not ok, (role, st, js))

    def test_messages(self):
        for role in ("owner", "approver", "release_manager", "asker"):
            self.assertEqual(req("POST", "/api/messages", ROLES[role], {"to": "API", "body": "hello"})[0], 201, role)
        self.assertEqual(req("POST", "/api/messages", VIEWER, {"to": "API", "body": "hello"})[0], 403)

    def test_me_state_and_lessons(self):
        want = {"owner": ["owner", "approver"], "approver": ["approver"], "release_manager": ["release_manager"],
                "asker": ["asker"], "viewer": ["viewer"]}
        for role, who in ROLES.items():
            st, me = req("GET", "/api/me", who)
            self.assertEqual(me["roles"], want[role], role)
            self.assertEqual(me["is_owner"], role == "owner")
            st, s = req("GET", "/api/state", who)
            self.assertEqual(s["owner"], role == "owner", role)
            st, ls = req("GET", "/api/lessons", who)
            self.assertEqual(ls["can_edit"], role == "owner", role)


class Fallback(Base):
    """With no owners configured, approvers count as owners (a single-user board)."""

    def test_approvers_are_owners_when_owners_is_empty(self):
        write_fleet(owners=False)
        try:
            st, me = req("GET", "/api/me", BOSS)
            self.assertEqual(me["roles"], ["owner", "approver"])
            self.assertEqual(req("GET", "/api/state", BOSS)[1]["owner"], True)
            self.assertEqual(req("POST", "/api/notify-prefs", BOSS, {"op": "mute", "channel": "slack", "for": "1h"})[0], 200)
            self.assertEqual(req("POST", "/api/presence", BOSS, {"visible": True, "idle_ms": 0})[0], 200)
            self.assertEqual(req("POST", "/api/notify-prefs", RM, {"op": "unmute", "channel": "slack"})[0], 403)
            self.assertEqual(req("POST", "/api/presence", ANN, {"visible": True, "idle_ms": 0})[0], 204)
            st, js = req("POST", "/api/messages/%s/release" % held(999), BOSS, {"confirm": "release"})
            self.assertNotEqual(st, 403, js)
        finally:
            write_fleet()

    def test_apikeys_do_not_fall_back(self):
        """Board controls fall back to approvers; API key administration never does."""
        write_fleet(owners=False)
        try:
            env = dict(os.environ)
            r = subprocess.run([os.path.join(REPO, "bin", "board"), "apikey", "request", "--name", "x", "--by", RM,
                                "--json"], capture_output=True, text=True, env=env)
            kid = json.loads(r.stdout)["id"]
            r = subprocess.run([os.path.join(REPO, "bin", "board"), "apikey", "approve", kid, "--by", BOSS],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 4, r.stderr)
        finally:
            write_fleet()


if __name__ == "__main__":
    unittest.main()
