#!/usr/bin/env python3
"""tests/test_pii.py — other people's emails are for owners only (lib/pii.py): board-serve's state,
SSE frames and lessons, /api/v1, `board task ls --json --viewer` and `board mask` serve role labels
("owner", "approver", "release manager", "asker", "viewer", "you", "someone") to everyone else.
Raw values stay on disk and in the CLI. With no owners configured, approvers count as owners.

  python3 tests/test_pii.py [-v]
  PII_REQUIRE_CRYPTO=1 makes a missing python3 cryptography a failure instead of a skip (CI).
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

TMP = tempfile.mkdtemp(prefix="switchboard-pii-test.")
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
os.makedirs(CFG)
os.makedirs(HOME, mode=0o700)
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_NOW")) or k in ("TMUX", "TMUX_PANE",
                                                                                        "BOARD_PUBLIC_URL"):
        os.environ.pop(k, None)
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "ALERT_DESKTOP": "0",
                   "CLAUDE_JSON": os.path.join(TMP, "no-claude.json")})

import fleet  # noqa: E402
import pii  # noqa: E402

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - CI installs python3-cryptography
    HAVE_CRYPTO = False

OWN, BOSS, RM, ANN, VIEWER = ("own@example.com", "boss@example.com", "rm@example.com", "ann@example.com",
                              "viewer@example.com")
STRANGER = "stranger@example.com"
EVERYONE = (OWN, BOSS, RM, ANN, VIEWER, STRANGER)
HOSTNAME = "board.example.com"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]+")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT, TPORT = free_port(), free_port()


def write_fleet(owners=True):
    with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
        fh.write(("owners: [%s]\n" % OWN if owners else "") + """approvers: [%s, %s, local]
release_managers: [%s]
askers: [%s]
sessions:
  - code: API
    cwd: /w
serve:
  port: %d
  tunnel:
    enabled: %s
    port: %d
    hostname: %s
    team_domain: team.example.com
    aud: AUD123
    certs_url: file://%s/certs.json
    allowed_emails: [%s, %s, %s, %s, %s]
""" % (OWN, BOSS, RM, ANN, PORT, "true" if HAVE_CRYPTO else "false", TPORT, HOSTNAME, TMP, OWN, BOSS, RM, ANN, VIEWER))


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


NOW = int(time.time() * 1000)


def fixtures():
    wj(os.path.join(HOME, "snapshot.json"), {"generated_at": NOW, "sessions": [
        {"code": "API", "aliases": ["API"], "state": "idle", "updated_at": NOW}]})
    wj(os.path.join(HOME, "decisions", "d-1-a.json"), {
        "id": "d-1-a", "kind": "question", "from": "API", "question": "Ship?", "options": ["yes", "no"],
        "status": "answered", "answer": "yes", "answered_by": BOSS, "answered_at": NOW, "relayed_at": None,
        "created_at": NOW})
    wj(os.path.join(HOME, "decisions", "d-1-b.json"), {
        "id": "d-1-b", "kind": "question", "from": "API", "question": "Again?", "options": ["yes"],
        "status": "answered_in_pane", "resolved_by": "board:" + OWN, "resolve_note": "asked " + STRANGER,
        "created_at": NOW})
    wj(os.path.join(HOME, "messages", "m-1-a.json"), {"id": "m-1-a", "to": "API", "body": "hello", "by": ANN,
                                                      "kind": "asker", "asker": ANN, "status": "new",
                                                      "created_at": NOW})
    wj(os.path.join(HOME, "messages", "m-1-b.json"), {"id": "m-1-b", "to": "API", "body": "released", "by": STRANGER,
                                                      "status": "new", "released_by": OWN, "created_at": NOW})
    wj(os.path.join(HOME, "tasks", "t-1.json"), {"id": "t-1", "title": "Ship it", "owner": "API", "stage": "build",
                                                 "gates": [{"stage": "plan", "gate": "owner", "status": "approved",
                                                            "approved_by": BOSS, "approved_at": NOW}],
                                                 "updated_at": NOW})
    with open(os.path.join(HOME, "events.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": NOW, "type": "approval", "decision_id": "d-1-g", "task_id": "t-1",
                             "stage": "plan", "gate": "owner", "answer": "approve", "result": "approved",
                             "answered_by": OWN, "answered_at": NOW, "relayed_at": None}) + "\n")
    wj(os.path.join(HOME, "owner-away"), {"since": NOW, "by": "board:" + OWN, "said": "back at 5"})


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
    return {e: tok(e) for e in (OWN, BOSS, RM, ANN, VIEWER)}


JWT = {}
SRV = None


def setUpModule():
    global SRV
    write_fleet()
    fixtures()
    if HAVE_CRYPTO:
        JWT.update(make_jwts())
    SRV = subprocess.Popen([sys.executable, os.path.join(REPO, "lib", "serve.py"), "--quiet"],
                           stdout=subprocess.DEVNULL, stderr=open(os.path.join(TMP, "serve.err"), "w"))
    for _ in range(150):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
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


def get(path, who=None, key=None):
    port = TPORT if who else PORT
    hdrs = {"Host": HOSTNAME if who else "127.0.0.1:%d" % PORT}
    if who:
        hdrs["Cf-Access-Jwt-Assertion"] = JWT[who]
    if key:
        hdrs["Authorization"] = "Bearer " + key
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request("GET", path, headers=hdrs)
    r = c.getresponse()
    raw = r.read().decode()
    c.close()
    return r.status, raw


def emails_in(raw, but=None):
    return sorted({e.lower() for e in EMAIL_RE.findall(raw)} - ({but} if but else set()))


class Unit(unittest.TestCase):
    def test_labels(self):
        m = pii.Masker(fleet.load_fleet(), viewer=ANN)
        self.assertEqual([m.label(x) for x in (OWN, "board:" + BOSS, RM.upper(), ANN, VIEWER, STRANGER)],
                         ["owner", "approver", "release manager", "you", "viewer", "someone"])
        for keep in ("local", "API", "apikey:abcdefghijkl", "u_0123456789abcdef", "board:local", None, 3):
            self.assertEqual(m.label(keep), keep)
        out = m.walk({"answered_by": BOSS, "me": ANN, "owner": "API", "ids": [OWN, "u_1", "local"],
                      "note": "ask %s or %s" % (OWN, STRANGER)})
        self.assertEqual(out, {"answered_by": "approver", "me": ANN, "owner": "API", "ids": ["owner", "u_1", "local"],
                               "note": "ask owner or someone"})

    def test_cli_mask_and_viewer_task_list(self):
        r = subprocess.run([os.path.join(REPO, "bin", "board"), "mask"], input=json.dumps(
            {"approvals": [{"answered_by": OWN}], "by": "board:" + BOSS}), capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout), {"approvals": [{"answered_by": "owner"}], "by": "approver"})
        r = subprocess.run([os.path.join(REPO, "bin", "board"), "task", "ls", "--json", "--viewer"],
                           capture_output=True, text=True)
        self.assertEqual(emails_in(r.stdout), [])
        self.assertIn('"approved_by": "approver"', r.stdout)
        raw = subprocess.run([os.path.join(REPO, "bin", "board"), "task", "ls", "--json"],
                             capture_output=True, text=True).stdout
        self.assertIn(BOSS, raw)                                   # the CLI's own view stays raw


class Served(unittest.TestCase):
    def setUp(self):
        if not HAVE_CRYPTO:
            if os.environ.get("PII_REQUIRE_CRYPTO") == "1":
                self.fail("python3 cryptography is required (PII_REQUIRE_CRYPTO=1)")
            self.skipTest("python3 cryptography missing")

    def test_owner_sees_raw(self):
        st, raw = get("/api/state", OWN)
        self.assertEqual(st, 200)
        for e in (OWN, BOSS, ANN, STRANGER):
            self.assertIn(e, raw)

    def test_everyone_else_sees_labels(self):
        for who in (BOSS, RM, ANN, VIEWER):
            with self.subTest(who=who):
                st, raw = get("/api/state", who)
                self.assertEqual(st, 200)
                s = json.loads(raw)
                self.assertEqual(s["me"], who)                     # their own identity, as it is
                self.assertEqual(emails_in(raw, but=who), [], who)  # nobody else's email
                d = {x["id"]: x for x in s["decisions"]}
                self.assertEqual(d["d-1-a"]["answered_by"], "you" if who == BOSS else "approver")
                self.assertEqual(d["d-1-b"]["resolved_by"], "owner")
                self.assertEqual(d["d-1-b"]["resolve_note"], "asked someone")
                m = {x["id"]: x for x in s["messages"]}
                self.assertEqual((m["m-1-a"]["by"], m["m-1-a"]["asker"]), ("you", "you") if who == ANN
                                 else ("asker", "asker"))
                self.assertEqual((m["m-1-b"]["by"], m["m-1-b"]["released_by"]), ("someone", "owner"))
                self.assertEqual(s["approvals"][0]["answered_by"], "owner")
                self.assertEqual(s["tasks"][0]["gates"][0]["approved_by"], "you" if who == BOSS else "approver")
                self.assertEqual(s["owner_away"]["by"], "owner")
                self.assertEqual(sorted(s["operator"]["approver_ids"]),
                                 sorted(["owner", "you" if who == BOSS else "approver", "local"]))

    def test_local_listener_and_lessons(self):
        # local is an approver, not an owner: labels, no "you" (everyone on it is `local`)
        st, raw = get("/api/state")
        self.assertEqual(emails_in(raw), [])
        self.assertEqual(json.loads(raw)["approver"], True)
        st, raw = get("/api/lessons", BOSS)
        self.assertEqual(st, 200)
        self.assertEqual(emails_in(raw, but=BOSS), [])

    def test_sse_frames(self):
        s = socket.create_connection(("127.0.0.1", TPORT), timeout=10)
        s.sendall(("GET /api/events HTTP/1.1\r\nHost: %s\r\nCf-Access-Jwt-Assertion: %s\r\n\r\n"
                   % (HOSTNAME, JWT[VIEWER])).encode())
        buf = b""
        while b"event: state" not in buf or not buf.rstrip().endswith(b"}"):
            c = s.recv(65536)
            if not c:
                break
            buf += c
        s.close()
        self.assertIn(b"event: state", buf)
        self.assertEqual(emails_in(buf.decode("utf-8", "replace"), but=VIEWER), [])

    def test_api_v1(self):
        r = subprocess.run([os.path.join(REPO, "bin", "board"), "apikey", "create", "--name", "reader", "--owner", OWN,
                            "--by", OWN, "--scopes", "read:state,read:decisions,read:tasks,read:messages", "--json"],
                           capture_output=True, text=True, check=True)
        key = json.loads(r.stdout)["key"]
        for path in ("/api/v1/state", "/api/v1/decisions", "/api/v1/decisions/d-1-a", "/api/v1/tasks",
                     "/api/v1/tasks/t-1", "/api/v1/messages/m-1-b"):
            st, raw = get(path, key=key)
            self.assertEqual(st, 200, (path, raw[:200]))
            self.assertEqual(emails_in(raw), [], path)
        st, raw = get("/api/v1/whoami", key=key)
        self.assertEqual(json.loads(raw)["owner"], OWN)            # the key's own owner, to its holder

    def test_no_owners_falls_back_to_approvers(self):
        write_fleet(owners=False)
        try:
            st, raw = get("/api/state", BOSS)
            for e in (OWN, ANN, STRANGER):
                self.assertIn(e, raw)
            st, raw = get("/api/state", ANN)
            self.assertEqual(emails_in(raw, but=ANN), [])
        finally:
            write_fleet()


if __name__ == "__main__":
    unittest.main()
