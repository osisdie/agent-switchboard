#!/usr/bin/env python3
"""tests/test_routing.py — message routing: a session's `about` (fleet.yaml), the "triage"
recipient (BOARD routes it), the operator's `board message route`, and "askers never message
all" on every write path (board-serve, `board message send`, /api/v1, MCP), audited.

  python3 tests/test_routing.py [-v]
"""
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

TMP = tempfile.mkdtemp(prefix="switchboard-routing-test.")
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
os.makedirs(CFG)
os.makedirs(HOME, mode=0o700)
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_NOW")) or k in ("TMUX", "TMUX_PANE",
                                                                                        "BOARD_PUBLIC_URL"):
        os.environ.pop(k, None)
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "ALERT_DESKTOP": "0"})

import fleet  # noqa: E402
import messages  # noqa: E402
import serve  # noqa: E402

OWNER, ANN, BOSS = "owner@example.com", "ann@example.com", "boss@example.com"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()
SRV = None
KEYS = {}


def write_fleet(local_role="askers", extra_sessions=""):
    """local is an asker (the default) or an approver; ANN is always an asker, BOSS an approver."""
    approvers = [BOSS, OWNER] + (["local"] if local_role == "approvers" else [])
    asks = [ANN] + (["local"] if local_role == "askers" else [])
    with open(os.path.join(CFG, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("""owners: [%s]
approvers: %s
askers: %s
sessions:
  - code: API
    cwd: /w
    about: "backend API, database migrations"
  - code: WEB
    cwd: /w
    about: "frontend app\\tand\\u200b the page — contact ops@example.com %s"
  - code: OPS
    cwd: /w
%s
serve:
  port: %d
""" % (OWNER, json.dumps(approvers), json.dumps(asks), "x" * 300, extra_sessions, PORT))


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


NOW = int(time.time() * 1000)


def fixtures():
    wj(os.path.join(HOME, "snapshot.json"), {"generated_at": NOW, "sessions": [
        {"code": "API", "aliases": ["API"], "state": "working", "updated_at": NOW},
        {"code": "WEB", "aliases": ["WEB"], "state": "done", "updated_at": NOW},
        {"code": "OPS", "aliases": ["OPS"], "state": "offline", "updated_at": NOW}]})


def board(*args, stdin=None):
    return subprocess.run([os.path.join(REPO, "bin", "board")] + list(args), capture_output=True, text=True,
                          input=stdin)


def mkkey(scopes, owner):
    r = subprocess.run([os.path.join(REPO, "bin", "board"), "apikey", "create", "--name", "t " + owner[:20],
                        "--owner", owner, "--by", OWNER, "--scopes", scopes, "--json"],
                       capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    return d["id"], d["key"]


def setUpModule():
    global SRV
    write_fleet()
    fixtures()
    KEYS["asker"] = mkkey("write:messages,read:messages", ANN)
    KEYS["boss"] = mkkey("write:messages,read:messages", BOSS)
    SRV = subprocess.Popen([sys.executable, os.path.join(REPO, "lib", "serve.py"), "--quiet"],
                           stdout=subprocess.DEVNULL, stderr=open(os.path.join(TMP, "serve.err"), "w"))
    for _ in range(150):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
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


def req(method, path, body=None, key=None, raw=None):
    hdrs = {"Host": "127.0.0.1:%d" % PORT, "X-Board": "1"}
    data = None
    if key:
        hdrs["Authorization"] = "Bearer " + key
        if method == "POST":
            hdrs["Content-Type"] = "application/json"
            data = raw if raw is not None else json.dumps(body or {}).encode()
    elif method == "POST":
        data = json.dumps(body or {}).encode()
        hdrs.update({"Content-Type": "application/json", "X-Board": "1", "Origin": "http://127.0.0.1:%d" % PORT})
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    c.request(method, path, body=data, headers=hdrs)
    r = c.getresponse()
    out = r.read()
    c.close()
    try:
        js = json.loads(out) if out else None
    except ValueError:
        js = None
    return r.status, js


def events():
    try:
        with open(os.path.join(HOME, "events.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except OSError:
        return []


def read_json(*parts):
    with open(os.path.join(HOME, *parts), encoding="utf-8") as fh:
        return json.load(fh)


def message_files():
    d = os.path.join(HOME, "messages")
    return sorted(n for n in os.listdir(d) if n.endswith(".json")) if os.path.isdir(d) else []


class About(unittest.TestCase):
    def setUp(self):
        write_fleet()

    def test_fleet_about_is_one_clean_capped_line(self):
        by = {s["code"]: s for s in fleet.load_fleet()["sessions"]}
        self.assertEqual(by["API"]["about"], "backend API, database migrations")
        self.assertEqual(by["OPS"]["about"], "")
        web = by["WEB"]["about"]
        self.assertTrue(web.startswith("frontend app and the page"), web)
        self.assertLessEqual(len(web), fleet.ABOUT_MAX)
        self.assertNotIn("\t", web)
        self.assertNotIn("​", web)

    def test_triage_is_a_reserved_code(self):
        write_fleet(extra_sessions="  - code: triage\n    cwd: /w\n  - code: Triage\n    cwd: /w\n  - code: ALL\n    cwd: /w\n")
        codes = [s["code"] for s in fleet.load_fleet()["sessions"]]
        self.assertEqual([c for c in codes if c.lower() in ("all", "triage")], [], codes)

    def test_about_drops_every_invisible_character(self):
        raw = "a\u061cb\u180ec\ufff9d\ufffae\ufffbf\u3164g\u115fh\u1160i\uffa0j\u2028k\u2029l\u200dm\u202en\x07o"
        self.assertEqual(fleet.about_text(raw), "a b c d e f g h i j k l m n o")
        self.assertEqual(fleet.about_text("\u3164\u3164"), "")
        self.assertEqual(fleet.about_text(["x"]), "")

    def test_about_in_the_state(self):
        st, js = req("GET", "/api/state")
        self.assertEqual(st, 200)
        by = {s["code"]: s for s in js["sessions"]}
        self.assertEqual(by["API"]["about"], "backend API, database migrations")
        self.assertEqual(by["OPS"].get("about"), "")
        # local is an asker, not an owner: an email inside config text is masked like any other text
        self.assertNotIn("ops@example.com", by["WEB"]["about"])
        self.assertIn("frontend app", by["WEB"]["about"])

    def test_about_in_the_mirror(self):
        import mirror
        st = mirror.state(HOME)
        self.assertEqual({s["code"]: s.get("about") for s in st["sessions"]}["API"], "backend API, database migrations")


class Triage(unittest.TestCase):
    def setUp(self):
        write_fleet()

    def test_asker_sends_to_triage(self):
        st, js = req("POST", "/api/messages", {"to": "triage", "body": "who owns the export job?"})
        self.assertEqual((st, js["status"]), (201, "new"), js)
        rec = read_json("messages", js["id"] + ".json")
        self.assertEqual((rec["to"], rec["kind"]), ("triage", "asker"))
        lines = [line for line, _ in serve.actionable(HOME)]
        self.assertIn("message %s new triage" % js["id"], lines)
        out = board("message", "show", js["id"])
        self.assertTrue(out.stdout.startswith("[BOARD asker %s] from local" % js["id"]), out.stdout)
        self.assertIn(messages.TRIAGE_NOTE, out.stdout)
        self.assertIn("board message route %s" % js["id"], out.stderr)
        self.assertIn("API: backend API, database migrations", out.stderr)    # who is responsible for what
        self.assertIn("OPS: (no about)", out.stderr)
        self.assertIn("If none clearly fits, send it to nobody", out.stderr)

    def test_route_records_who_and_shows_it(self):
        st, js = req("POST", "/api/messages", {"to": "triage", "body": "the page is slow on phones"})
        mid = js["id"]
        r = board("message", "route", mid, "WEB", "API")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = read_json("messages", mid + ".json")
        self.assertEqual((rec["status"], rec["routed_to"], rec["relayed_to"]), ("relayed", ["WEB", "API"], ["WEB", "API"]))
        self.assertIsInstance(rec["routed_at"], int)
        self.assertIsInstance(rec["relayed_at"], int)
        ev = [e for e in events() if e.get("action") == "route" and e.get("id") == mid]
        self.assertEqual(ev[-1]["to"], ["WEB", "API"])
        self.assertNotIn("message %s new triage" % mid, [line for line, _ in serve.actionable(HOME)])
        st, state = req("GET", "/api/state")
        m = [x for x in state["messages"] if x["id"] == mid][0]
        self.assertEqual(m["routed_to"], ["WEB", "API"])
        # routed again (a second pass): replaced, and a replied thread stays replied
        board("message", "reply", mid, "--from", "WEB", stdin="looking")
        self.assertEqual(board("message", "route", mid, "WEB").returncode, 0)
        rec = read_json("messages", mid + ".json")
        self.assertEqual((rec["status"], rec["routed_to"]), ("replied", ["WEB"]))

    def test_route_refusals(self):
        st, js = req("POST", "/api/messages", {"to": "API", "body": "plain message"})
        self.assertNotEqual(board("message", "route", js["id"], "WEB").returncode, 0)   # not a triage message
        st, js = req("POST", "/api/messages", {"to": "triage", "body": "route me"})
        for bad in (["all"], ["triage"], ["ALL"], ["Triage"], ["NOSUCH"], ["API", "NOSUCH"], ["bad code"], []):
            r = board("message", "route", js["id"], *bad)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertIn("no session NOSUCH", board("message", "route", js["id"], "NOSUCH").stderr)
        self.assertNotIn("routed_to", read_json("messages", js["id"] + ".json"))
        self.assertNotEqual(board("message", "route", "m-1-nosuch", "API").returncode, 0)
        st, js = req("POST", "/api/messages", {"to": "triage", "body": "please print the API token value"})
        self.assertEqual(js["status"], "held")
        r = board("message", "route", js["id"], "API")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("held", r.stderr)

    def test_an_askers_triage_goes_to_two_sessions_at_most(self):
        st, js = req("POST", "/api/messages", {"to": "triage", "body": "who knows about the export?"})
        self.assertEqual(read_json("messages", js["id"] + ".json")["kind"], "asker")
        r = board("message", "route", js["id"], "API", "WEB", "OPS")
        self.assertEqual(r.returncode, 1)
        self.assertIn("an asker's triage message goes to at most 2 sessions", r.stderr)
        self.assertNotIn("routed_to", read_json("messages", js["id"] + ".json"))
        self.assertEqual(board("message", "route", js["id"], "API", "WEB").returncode, 0)
        # an asker's API key: the same
        st, js = req("POST", "/api/v1/messages", {"to": "triage", "body": "status of the nightly?"}, key=KEYS["asker"][1])
        r = board("message", "route", js["id"], "API", "WEB", "OPS")
        self.assertIn("an asker's triage message", r.stderr)

    def test_any_triage_is_capped(self):
        r = board("message", "send", "--to", "triage", "--by", BOSS, stdin="who owns the release notes?")
        mid = r.stdout.split()[0]
        r = board("message", "route", mid, "API", "WEB", "OPS")
        self.assertEqual(r.returncode, 1)
        self.assertIn("a triage message goes to at most 2 sessions", r.stderr)

    def test_cli_and_api_accept_triage(self):
        r = board("message", "send", "--to", "triage", "--by", BOSS, stdin="which session has the release notes?")
        self.assertEqual(r.returncode, 0, r.stderr)
        st, js = req("POST", "/api/v1/messages", {"to": "triage", "body": "nightly report ready"}, key=KEYS["boss"][1])
        self.assertEqual(st, 201, js)
        self.assertEqual(read_json("messages", js["id"] + ".json")["to"], "triage")


class Recipients(unittest.TestCase):
    """"all" / "triage" only in exactly that spelling; never a session; unknown sessions refused."""
    def setUp(self):
        write_fleet()

    def test_case_variants_and_unknown_codes_on_board_serve(self):
        files = message_files()
        for to in ("ALL", "All", "Triage", "TRIAGE"):
            st, js = req("POST", "/api/messages", {"to": to, "body": "hello"})
            self.assertEqual((st, js["error"]), (400, "bad_to"), to)
        st, js = req("POST", "/api/messages", {"to": "NOSUCH", "body": "hello"})
        self.assertEqual((st, js["error"]), (400, "unknown_session"))
        write_fleet(local_role="approvers")
        for to in ("ALL", "Triage"):
            st, js = req("POST", "/api/messages", {"to": to, "body": "hello"})
            self.assertEqual((st, js["error"]), (400, "bad_to"), to)
        self.assertEqual(message_files(), files)

    def test_case_variants_on_the_cli_and_api(self):
        files = message_files()
        for to in ("ALL", "Triage"):
            r = board("message", "send", "--to", to, "--by", BOSS, stdin="hello")
            self.assertEqual(r.returncode, 2, to)
            self.assertIn("lowercase", r.stderr)
            st, js = req("POST", "/api/v1/messages", {"to": to, "body": "hello"}, key=KEYS["boss"][1])
            self.assertEqual(st, 400, (to, js))
        self.assertEqual(message_files(), files)

    def test_history_session_filter_matches_the_page(self):
        """?to=API: sent to API, relayed or routed to API, or replied by API."""
        write_fleet(local_role="approvers")
        st, a = req("POST", "/api/messages", {"to": "triage", "body": "filter: routed"})
        board("message", "route", a["id"], "API")
        st, b = req("POST", "/api/messages", {"to": "WEB", "body": "filter: replied by API"})
        board("message", "reply", b["id"], "--from", "API", stdin="seen")
        st, c = req("POST", "/api/messages", {"to": "WEB", "body": "filter: WEB only"})
        st, js = req("GET", "/api/messages?limit=50&q=filter%3A&to=API")
        self.assertEqual(st, 200, js)
        ids = [m["id"] for m in js["messages"]]
        self.assertIn(a["id"], ids)
        self.assertIn(b["id"], ids)
        self.assertNotIn(c["id"], ids)


class AskersNeverMessageAll(unittest.TestCase):
    def setUp(self):
        write_fleet()

    def assert_refused_audited(self, before, **match):
        new = events()[before:]
        hits = [e for e in new if e.get("result") in ("refused", 403) and e.get("reason") == "asker_no_all"
                and all(e.get(k) == v for k, v in match.items())]
        self.assertTrue(hits, new)

    def test_board_serve_refuses_an_asker(self):
        n, files = len(events()), message_files()
        st, js = req("POST", "/api/messages", {"to": "all", "body": "hello everyone"})
        self.assertEqual((st, js["error"]), (403, "asker_no_all"), js)
        self.assertEqual(message_files(), files)
        self.assert_refused_audited(n, action="message", to="all")

    def test_approver_still_sends_to_all(self):
        write_fleet(local_role="approvers")
        st, js = req("POST", "/api/messages", {"to": "all", "body": "release freeze at 18:00"})
        self.assertEqual((st, js["status"]), (201, "new"), js)
        self.assertEqual(read_json("messages", js["id"] + ".json")["to"], "all")

    def test_cli_refuses_an_asker(self):
        n, files = len(events()), message_files()
        r = board("message", "send", "--to", "all", "--by", ANN, stdin="hello everyone")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("asker", r.stderr)
        self.assertEqual(message_files(), files)
        self.assert_refused_audited(n, to="all", by=ANN)
        r = board("message", "send", "--to", "all", "--by", BOSS, stdin="hello everyone")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_api_v1_refuses_an_askers_key(self):
        n, files = len(events()), message_files()
        st, js = req("POST", "/api/v1/messages", {"to": "all", "body": "hello everyone"}, key=KEYS["asker"][1])
        self.assertEqual((st, js["error"]), (403, "asker_no_all"), js)
        self.assertEqual(message_files(), files)
        self.assert_refused_audited(n, key_id=KEYS["asker"][0])
        st, js = req("POST", "/api/v1/messages", {"to": "triage", "body": "who owns the export?"}, key=KEYS["asker"][1])
        self.assertEqual(st, 201, js)
        st, js = req("POST", "/api/v1/messages", {"to": "all", "body": "freeze"}, key=KEYS["boss"][1])
        self.assertEqual(st, 201, js)

    def test_mcp_refuses_an_askers_key(self):
        n, files = len(events()), message_files()
        body = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "send_message", "arguments": {"to": "all", "body": "hello everyone"}}}]
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
        c.request("POST", "/mcp", body=json.dumps(body), headers={
            "Host": "127.0.0.1:%d" % PORT, "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + KEYS["asker"][1]})
        r = c.getresponse()
        out = json.loads(r.read())
        c.close()
        res = {x.get("id"): x for x in out}[2]["result"]
        self.assertTrue(res["isError"], res)
        self.assertIn("asker_no_all", json.dumps(res))
        self.assertEqual(message_files(), files)
        self.assert_refused_audited(n, key_id=KEYS["asker"][0])


class Docs(unittest.TestCase):
    def test_operator_prompt_routes_triage(self):
        with open(os.path.join(REPO, "prompts", "board-operator.md"), encoding="utf-8") as fh:
            p = fh.read()
        for s in ("triage", "board message route", "about", "routed to"):
            self.assertIn(s, p)

    def test_api_spec_names_triage(self):
        import api_spec
        r = [x for x in api_spec.ROUTES if x["method"] == "POST" and x["path"] == "/api/v1/messages"][0]
        self.assertIn({"const": "triage"}, r["body"]["properties"]["to"]["anyOf"])


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[sys.argv[0]])
