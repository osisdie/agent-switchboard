#!/usr/bin/env python3
"""tests/test_rc.py — Remote Control on/off (lib/rc.py, `board rc`, fleet-up, the collector and
board-serve's POST /api/rc). tmux is tests/fake_tmux.py on PATH: scripted screens, every call
logged, never a real tmux server. Never reads ~/.agent-switchboard, ~/.claude or a real config.

  python3 tests/test_rc.py [-v]
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

TMP = tempfile.mkdtemp(prefix="switchboard-rc-test.")
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
SESS = os.path.join(TMP, "sessions")
BIN = os.path.join(TMP, "bin")
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "CLAUDE_SESSIONS_DIR": SESS,
                   "CLAUDE_JSON": os.path.join(TMP, "claude.json"), "CLAUDE_SETTINGS": os.path.join(TMP, "settings.json"),
                   "ALERT_DESKTOP": "0", "BOARD_PRESS_SETTLE": "0", "BOARD_RC_AUTO": "1"})
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_")) or k in ("TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL", "DO_NOT_TRACK"):
        if k != "ALERT_DESKTOP":
            os.environ.pop(k)
for d in (HOME, CFG, SESS, BIN):
    os.makedirs(d)
subprocess.run([sys.executable, os.path.join(REPO, "tests", "fake_proc.py"), os.path.join(TMP, "proc")], check=True)
os.environ["BOARD_PROC_ROOT"] = os.path.join(TMP, "proc")
with open(os.path.join(BIN, "tmux"), "w") as fh:
    fh.write('#!/bin/sh\nexec %s %s "$@"\n' % (sys.executable, os.path.join(REPO, "tests", "fake_tmux.py")))
os.chmod(os.path.join(BIN, "tmux"), 0o755)
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]
LOG = os.path.join(TMP, "tmux.log")
TCFG = os.path.join(TMP, "tmux.json")
os.environ["FAKE_TMUX_LOG"] = LOG

import rc  # noqa: E402

BOARD = os.path.join(REPO, "bin", "board")
RULE = "─" * 40
TOP = RULE + " CP ─"
NB = "\u00a0"
HEAD = ["Claude Code v2", "", "● done with the last task"]


def screen(prompt="❯" + NB, above=(), below=("  ⏵⏵ auto mode on (shift+tab to cycle)",)):
    return "\n".join(HEAD + list(above) + [TOP, prompt, RULE] + list(below))


IDLE = screen()
TYPED = screen("❯ /remote-control", above=["  ❯ /remote-control             Control this session from your phone"])
ACTIVE = screen(above=["❯ /remote-control", "  /remote-control is active · Continue here, on your phone, or at",
                       "  https://claude.ai/code/session_x"])


def dialog(focus):
    rows = ["Disconnect this session", "Show QR code   Scan with your phone to open this session", "Continue"]
    body = ["❯ /remote-control", " ▔▔▔▔", "   Remote Control",
            "   This session is available in the Claude mobile app and at https://claude.ai/code/session_x."]
    for r in rows:
        body.append(("   ❯ " if r.startswith(focus) else "     ") + r)
    body.append("   Enter to select · Esc to continue")
    return "\n".join(HEAD + body)


DISCONNECTED = screen(above=["❯ /remote-control", "  ⎿  Remote Control disconnected."])
UNKNOWN = screen(above=["● Unknown command: /remote-control"])


def run(*args, env=None, inp=None):
    e = dict(os.environ, **(env or {}))
    r = subprocess.run(list(args), capture_output=True, text=True, env=e, input=inp, timeout=120)
    return r.returncode, r.stdout, r.stderr


def board(*args, **kw):
    return run(BOARD, *args, **kw)


def write_fleet(extra="", sessions=None):
    sessions = sessions if sessions is not None else "  - code: CP\n    cwd: %s\n    autostart: true\n" % TMP
    with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
        fh.write("approvers: [local]\nrc:\n  confirm_seconds: 1\n%s\nsessions:\n%s" % (extra, sessions))


def jload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def jdump(obj, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


SERVE_ERR = open(os.path.join(TMP, "serve.err"), "a")  # noqa: SIM115 - closed at exit


def events(kind="rc"):
    out = []
    try:
        with open(os.path.join(HOME, "events.jsonl")) as fh:
            for ln in fh:
                ev = json.loads(ln)
                if ev.get("type") == kind:
                    out.append(ev)
    except OSError:
        pass
    return out


LIVES = []


def close_lives():
    while LIVES:
        LIVES.pop().close()


class Live:
    """One running session: a live pid, its registry file and hook state."""

    def __init__(self, code="CP", status="idle", bridge=None, state="done", sid="s-cp", pane="%42"):
        self.proc = subprocess.Popen(["sleep", "600"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        LIVES.append(self)
        self.pid = self.proc.pid
        self.sid = sid
        self.reg = os.path.join(SESS, "%d.json" % self.pid)
        self.state = os.path.join(HOME, "state", "%s.json" % sid)
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        self.write(status=status, bridgeSessionId=bridge, name=code, tmux="%s:@1.%s" % (code, pane))
        with open(self.state, "w") as fh:
            json.dump({"session_id": sid, "state": state, "tmux_pane": pane, "updated_at": 1}, fh)

    def write(self, **kw):
        d = {}
        if os.path.exists(self.reg):
            with open(self.reg) as fh:
                d = json.load(fh)
        d.update({"pid": self.pid, "sessionId": self.sid, "cwd": "/w", "startedAt": 1, "statusUpdatedAt": 5})
        d.update(kw)
        with open(self.reg, "w") as fh:
            json.dump(d, fh)

    def bridge(self):
        with open(self.reg) as fh:
            return json.load(fh).get("bridgeSessionId")

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for p in (self.reg, self.state):
            if os.path.exists(p):
                os.remove(p)


def script(screens, on_send=None, on_capture=None, session="CP", fail_send=None):
    cfg = {"session": session, "screens": screens, "on_send": on_send or {}, "on_capture": on_capture or {}}
    if fail_send:
        cfg["fail_send"] = fail_send
    with open(TCFG, "w") as fh:
        json.dump(cfg, fh)
    for p in (TCFG + ".state", LOG):
        if os.path.exists(p):
            os.remove(p)
    os.environ["FAKE_TMUX_CFG"] = TCFG


def sent():
    """The send-keys calls, as "key|key" strings, in order."""
    try:
        with open(LOG) as fh:
            return ["|".join(ln.rstrip("\n").split("|")[3:]) for ln in fh if ln.startswith("send-keys|")]
    except OSError:
        return []


def reset_home():
    for n in ("rc-prefs.json", "rc-apply.json", "events.jsonl", "snapshot.json"):
        p = os.path.join(HOME, n)
        if os.path.exists(p):
            os.remove(p)


# ----------------------------------------------------------------------------- prefs + CLI
class PrefsTest(unittest.TestCase):
    def setUp(self):
        reset_home()
        write_fleet()

    def test_set_and_status(self):
        rc_, out, _ = board("rc", "off", "CP", "--by", "tester", "--no-apply")
        self.assertEqual(rc_, 0, out)
        path = os.path.join(HOME, "rc-prefs.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        data = jload(path)
        self.assertEqual(data["sessions"]["CP"]["state"], "off")
        self.assertEqual(data["sessions"]["CP"]["by"], "tester")
        self.assertEqual(run(sys.executable, os.path.join(REPO, "lib", "rc.py"), "want", "CP")[1].strip(), "off")
        rc_, out, _ = board("rc", "status", "--json")
        self.assertEqual(rc_, 0)
        st = json.loads(out)
        self.assertEqual([s["want"] for s in st["sessions"] if s["code"] == "CP"], ["off"])
        ev = events()[-1]
        self.assertEqual((ev["action"], ev["code"], ev["want"], ev["by"]), ("set", "CP", "off", "tester"))

    def test_all_replaces_sessions(self):
        board("rc", "off", "CP", "--no-apply")
        board("rc", "off", "OTHER", "--no-apply")
        rc_, out, _ = board("rc", "on", "all", "--no-apply")
        self.assertEqual(rc_, 0)
        self.assertIn("replaces the choice for CP, OTHER", out)
        d = rc.load(HOME)
        self.assertEqual((d["all"]["state"], d["sessions"]), ("on", {}))
        board("rc", "off", "CP", "--no-apply")
        cfg = {"sessions": []}
        self.assertEqual(rc.wanted("CP", cfg, rc.load(HOME)), ("off", "session"))
        self.assertEqual(rc.wanted("ZZ", cfg, rc.load(HOME)), ("on", "all"))
        board("rc", "clear", "CP")
        self.assertEqual(rc.wanted("CP", cfg, rc.load(HOME)), ("on", "all"))
        board("rc", "clear", "all")
        self.assertEqual(rc.wanted("CP", cfg, rc.load(HOME)), ("on", "default"))

    def test_fleet_defaults(self):
        empty = rc.load(HOME)
        cfg = {"remote_control": False, "sessions": [{"code": "A", "remote_control": True}, {"code": "B", "remote_control": None}]}
        self.assertEqual(rc.wanted("A", cfg, empty), ("on", "fleet"))
        self.assertEqual(rc.wanted("B", cfg, empty), ("off", "fleet"))
        self.assertEqual(rc.wanted("C", {"sessions": []}, empty), ("on", "default"))
        self.assertFalse(rc.explicit("fleet"))
        self.assertTrue(rc.explicit("all"))

    def test_usage_errors(self):
        for args in (["rc"], ["rc", "maybe", "CP"], ["rc", "on"], ["rc", "on", "bad code"], ["rc", "on", "../x"],
                     ["rc", "on", "CP", "--by", "a b"], ["rc", "apply", "CP", "--pending"]):
            rc_, _, err = board(*args)
            self.assertEqual(rc_, 2, (args, err))
        self.assertFalse(os.path.exists(os.path.join(HOME, "rc-prefs.json")))

    def test_help(self):
        rc_, out, _ = board("rc", "--help")
        self.assertEqual(rc_, 0)
        self.assertIn("board rc on|off CODE|all", out)

    def test_malformed_prefs(self):
        with open(os.path.join(HOME, "rc-prefs.json"), "w") as fh:
            fh.write('{"all": {"state": "maybe"}, "sessions": {"CP": {"state": "off"}, "x y": {"state": "on"}, "Q": 3}}')
        d = rc.load(HOME)
        self.assertIsNone(d["all"])
        self.assertEqual(list(d["sessions"]), ["CP"])


# ----------------------------------------------------------------------------- fleet-up
class FleetUpTest(unittest.TestCase):
    def setUp(self):
        reset_home()
        script([""])

    def cmd(self, code):
        rc_, out, err = run(os.path.join(REPO, "bin", "fleet-up"), "--dry-run", "--only", code)
        self.assertEqual(rc_, 0, err)
        lines = [ln for ln in out.splitlines() if "%s: starting in" % code in ln]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def test_default_passes_flag(self):
        write_fleet(sessions="  - code: CP\n    cwd: %s\n" % TMP)
        self.assertIn("claude -n CP --remote-control CP", self.cmd("CP"))

    def test_pref_off_omits_flag(self):
        write_fleet(sessions="  - code: CP\n    cwd: %s\n    resume_name: CP\n" % TMP)
        board("rc", "off", "CP", "--no-apply")
        line = self.cmd("CP")
        self.assertNotIn("--remote-control", line)
        self.assertIn("claude -n CP -r 'CP'", line)

    def test_fleet_yaml_off_and_board_override(self):
        write_fleet(sessions="  - code: CP\n    cwd: %s\n    remote_control: false\n" % TMP)
        self.assertNotIn("--remote-control", self.cmd("CP"))
        board("rc", "on", "all", "--no-apply")
        self.assertIn("--remote-control CP", self.cmd("CP"))

    def test_top_level_default(self):
        write_fleet("remote_control: false", sessions="  - code: CP\n    cwd: %s\n" % TMP)
        self.assertNotIn("--remote-control", self.cmd("CP"))

    def test_do_not_track_warning(self):
        write_fleet(sessions="  - code: CP\n    cwd: %s\n" % TMP)
        rc_, out, _ = run(os.path.join(REPO, "bin", "fleet-up"), "--dry-run", "--only", "CP", env={"DO_NOT_TRACK": "1"})
        self.assertIn("DO_NOT_TRACK is set", out)


# ----------------------------------------------------------------------------- apply
class ApplyTest(unittest.TestCase):
    def setUp(self):
        reset_home()
        write_fleet()
        self.live = None

    def tearDown(self):
        close_lives()

    def go(self, *args):
        return board("rc", *args)

    def test_on_idle_empty_prompt(self):
        self.live = Live()
        script([IDLE, TYPED, ACTIVE], on_send={"2": {"path": self.live.reg, "set": {"bridgeSessionId": "session_x"}}})
        rc_, out, err = self.go("on", "CP")
        self.assertEqual(rc_, 0, out + err)
        self.assertIn("confirmed", out)
        self.assertEqual(sent(), ["-l|--|/remote-control", "Enter"])
        ev = [e for e in events() if e["action"] == "apply"][-1]
        self.assertEqual((ev["result"], ev["want"], ev["pane"]), ("confirmed", "on", "%42"))
        att = jload(os.path.join(HOME, "rc-apply.json"))["CP"]
        self.assertEqual((att["result"], att["sid"]), ("confirmed", "s-cp"))
        snap = jload(os.path.join(HOME, "snapshot.json"))
        row = [s for s in snap["sessions"] if s["code"] == "CP"][0]
        self.assertEqual((row["rc"]["on"], row["rc"]["want"], row["rc"]["pending"]), (True, "on", False))

    def test_off_walks_the_dialog(self):
        self.live = Live(bridge="session_x")
        script([IDLE, TYPED, dialog("Continue"), dialog("Show QR"), dialog("Disconnect"), DISCONNECTED],
               on_send={"5": {"path": self.live.reg, "set": {"bridgeSessionId": None}}})
        rc_, out, err = self.go("off", "CP")
        self.assertEqual(rc_, 0, out + err)
        self.assertIn("confirmed", out)
        self.assertEqual(sent(), ["-l|--|/remote-control", "Enter", "Up", "Up", "Enter"])
        self.assertIsNone(self.live.bridge())

    def test_off_dialog_unexpected_escapes(self):
        self.live = Live(bridge="session_x")
        # the focus never reaches Disconnect within the allowed moves: Escape, Remote Control stays on
        script([IDLE, TYPED] + [dialog("Continue")] * 8)
        rc_, out, _ = self.go("off", "CP")
        self.assertEqual(rc_, 3, out)
        self.assertEqual(sent()[-1], "Escape")
        self.assertNotIn("Enter", sent()[2:])
        self.assertEqual(self.live.bridge(), "session_x")

    def test_busy_is_deferred(self):
        self.live = Live(status="busy", state="working")
        script([IDLE])
        rc_, out, _ = self.go("on", "CP")
        self.assertEqual(rc_, 0, out)
        self.assertIn("deferred", out)
        self.assertIn("will apply when idle", out)
        self.assertEqual(sent(), [])
        rc_, out, _ = board("rc", "status", "--json")
        row = [s for s in json.loads(out)["sessions"] if s["code"] == "CP"][0]
        self.assertTrue(row["pending"])

    def test_needs_you_is_deferred(self):
        self.live = Live(status="waiting", state="needs-you")
        script([IDLE])
        rc_, out, _ = self.go("on", "CP")
        self.assertIn("deferred", out)
        self.assertEqual(sent(), [])

    def test_unexpected_screens_send_nothing(self):
        cases = {
            "dialog open": dialog("Continue"),
            "prompt not empty": screen("❯ half a message"),
            "working": screen(above=["✻ Working… (esc to interrupt)"]),
            "no rules": "\n".join(HEAD + ["❯ "]),
            "": "",
        }
        for name, scr in cases.items():
            with self.subTest(name):
                reset_home()
                self.live = Live()
                script([scr])
                rc_, out, _ = self.go("on", "CP")
                self.assertEqual(rc_, 3, (name, out))
                self.assertIn("refused", out)
                self.assertEqual(sent(), [])
                close_lives()

    def test_pane_of_another_session(self):
        self.live = Live()
        script([IDLE], session="OTHER")
        rc_, out, _ = self.go("on", "CP")
        self.assertEqual(rc_, 3)
        self.assertIn("belongs to tmux session 'OTHER'", out)
        self.assertEqual(sent(), [])

    def test_typed_text_not_shown_no_enter(self):
        self.live = Live()
        script([IDLE, screen("❯ /remote-controlxyz")])
        rc_, out, _ = self.go("on", "CP")
        self.assertEqual(rc_, 5, out)
        self.assertIn("Enter not pressed", out)
        self.assertEqual(sent(), ["-l|--|/remote-control"])

    def test_session_starts_working_while_typing(self):
        self.live = Live()
        # capture 1 = the check before typing, capture 2 = after typing: the session turned busy
        script([IDLE, TYPED], on_capture={"2": {"path": self.live.reg, "set": {"status": "busy", "statusUpdatedAt": 9}}})
        rc_, out, _ = self.go("on", "CP")
        self.assertEqual(rc_, 5, out)
        self.assertEqual(sent(), ["-l|--|/remote-control"])

    def test_unavailable(self):
        self.live = Live()
        script([IDLE, TYPED, UNKNOWN])
        rc_, out, _ = self.go("on", "CP")
        self.assertEqual(rc_, 6, out)
        self.assertIn("unavailable", out)
        cfg = rc.fleet.load_fleet()
        att = rc.load_attempts(HOME)["CP"]
        self.assertFalse(rc.auto_allowed(cfg, att, "s-cp", "on", rc.now_ms() + 10 ** 9))
        self.assertTrue(rc.auto_allowed(cfg, att, "s-new", "on", rc.now_ms()))

    def test_already(self):
        self.live = Live(bridge="session_x")
        script([IDLE])
        rc_, out, _ = self.go("on", "CP")
        self.assertEqual(rc_, 0)
        self.assertIn("already", out)
        self.assertEqual(sent(), [])

    def test_no_apply_and_auto(self):
        self.live = Live()
        script([IDLE, TYPED, ACTIVE], on_send={"2": {"path": self.live.reg, "set": {"bridgeSessionId": "session_y"}}})
        board("rc", "on", "CP", "--no-apply")
        self.assertEqual(sent(), [])
        subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], check=True)
        snap = jload(os.path.join(HOME, "snapshot.json"))
        sessions = snap["sessions"]
        self.assertTrue([s for s in sessions if s["code"] == "CP"][0]["rc"]["pending"])
        self.assertEqual(rc.auto_candidates(sessions, rc.fleet.load_fleet(), HOME, rc.now_ms()), ["CP"])
        self.assertEqual(rc.auto_candidates(sessions, {"rc": {"auto_apply": False}}, HOME, rc.now_ms()), [])
        rc_, out, _ = board("rc", "apply", "--pending", "--auto")
        self.assertEqual(rc_, 0, out)
        self.assertEqual(sent(), ["-l|--|/remote-control", "Enter"])
        self.assertEqual(self.live.bridge(), "session_y")
        self.assertTrue([e for e in events() if e["action"] == "apply"][-1]["auto"])

    def test_auto_ignores_fleet_yaml_only(self):
        write_fleet(sessions="  - code: CP\n    cwd: %s\n    remote_control: true\n" % TMP)
        self.live = Live()
        script([IDLE])
        board("rc", "apply", "--pending", "--auto")
        self.assertEqual(sent(), [])
        subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], check=True)
        snap = jload(os.path.join(HOME, "snapshot.json"))
        row = [s for s in snap["sessions"] if s["code"] == "CP"][0]
        self.assertEqual((row["rc"]["source"], row["rc"]["pending"]), ("fleet", False))

    def test_auto_retry_rules(self):
        cfg = {"rc": {"retry_minutes": 10, "max_tries": 2}}
        att = {"sid": "s", "want": "on", "result": "refused", "at": 1000, "tries": 1}
        self.assertFalse(rc.auto_allowed(cfg, att, "s", "on", 1000 + 60000))
        self.assertTrue(rc.auto_allowed(cfg, att, "s", "on", 1000 + 600000))
        self.assertFalse(rc.auto_allowed(cfg, dict(att, tries=2), "s", "on", 10 ** 12))
        self.assertTrue(rc.auto_allowed(cfg, att, "s", "off", 1000))
        self.assertFalse(rc.auto_allowed(cfg, dict(att, result="partial"), "s", "on", 10 ** 12))

    def test_collector_loop_flag_spawns(self):
        self.live = Live()
        script([IDLE, TYPED, ACTIVE], on_send={"2": {"path": self.live.reg, "set": {"bridgeSessionId": "session_z"}}})
        board("rc", "on", "CP", "--no-apply")
        run(sys.executable, os.path.join(REPO, "lib", "collect.py"), "--no-notify", "--apply-rc")
        for _ in range(100):
            if self.live.bridge():
                break
            time.sleep(0.1)
        self.assertEqual(self.live.bridge(), "session_z")
        # the background applier finishes (lock released) before the next test
        for _ in range(50):
            if not rc.run_busy(HOME):
                break
            time.sleep(0.1)

    def test_offline(self):
        script([IDLE])
        rc_, out, _ = self.go("on", "CP")
        self.assertEqual(rc_, 0)
        self.assertIn("offline", out)
        self.assertEqual(sent(), [])


class ScreenTest(unittest.TestCase):
    cfg = dict(rc.fleet.DEFAULTS["rc"])

    def lines(self, s):
        return s.split("\n")

    def test_idle(self):
        self.assertIsNone(rc.idle_screen(self.lines(IDLE), self.cfg))

    def test_typed_box(self):
        i, text = rc.prompt_box(self.lines(TYPED), self.cfg)
        self.assertEqual(text, "/remote-control")

    def test_history_prompt_is_not_the_box(self):
        # the dialog hides the input box; the echoed "❯ /remote-control" above it is history
        i, why = rc.prompt_box(self.lines(dialog("Continue")), self.cfg)
        self.assertIsNone(i)

    def test_focus(self):
        self.assertEqual(rc._focused(self.lines(dialog("Show QR"))), "Show QR code   Scan with your phone to open this session")


# ----------------------------------------------------------------------------- board-serve
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ServeTest(unittest.TestCase):
    srv = None

    @classmethod
    def start(cls, approvers):
        cls.stop()
        cls.port = free_port()
        write_fleet("serve:\n  port: %d" % cls.port, sessions="  - code: CP\n    cwd: /w\n")
        with open(os.path.join(CFG, "fleet.yaml")) as fh:
            body = fh.read().replace("approvers: [local]", "approvers: %s" % approvers)
        with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
            fh.write(body)
        cls.srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"],
                                   stdout=subprocess.DEVNULL, stderr=SERVE_ERR)
        for _ in range(100):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/api/state" % cls.port, timeout=1)
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        raise RuntimeError("board-serve did not start")

    @classmethod
    def stop(cls):
        if cls.srv:
            cls.srv.terminate()
            cls.srv.wait()
            cls.srv = None

    @classmethod
    def tearDownClass(cls):
        cls.stop()

    def setUp(self):
        reset_home()
        script([IDLE])

    def post(self, body, headers=None, raw=None, path="/api/rc"):
        base = "http://127.0.0.1:%d" % self.port
        h = {"Content-Type": "application/json", "X-Board": "1", "Origin": base}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = raw if raw is not None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def serve_events(self):
        return [e for e in events("serve") if e.get("action") == "rc"]

    def test_matrix(self):
        self.start("[local]")
        ok = {"code": "CP", "state": "off"}
        cases = [
            ("no X-Board", ok, {"X-Board": None}, None, 403),
            ("cross origin", ok, {"Origin": "https://evil.example"}, None, 403),
            ("no origin", ok, {"Origin": None}, None, 403),
            ("text/plain", ok, {"Content-Type": "text/plain"}, None, 415),
            ("not JSON", None, None, b"rc off CP", 400),
            ("over the body cap", {"code": "CP", "state": "off", "pad": "x" * 20000}, None, None, 413),
            ("forwarded", ok, {"X-Forwarded-For": "203.0.113.9"}, None, 403),
            ("foreign Host", ok, {"Host": "evil.example"}, None, 421),
            ("bad state", {"code": "CP", "state": "maybe"}, None, None, 400),
            ("bad code", {"code": "a b", "state": "on"}, None, None, 400),
            ("code not a string", {"code": ["CP"], "state": "on"}, None, None, 400),
            ("unknown session", {"code": "NOPE", "state": "on"}, None, None, 400),
            ("array body", None, None, b"[1]", 400),
        ]
        for name, body, hdrs, raw, want in cases:
            with self.subTest(name):
                status, resp = self.post(body, hdrs, raw)
                self.assertEqual(status, want, (name, resp))
                self.assertNotIn("Traceback", json.dumps(resp))
        self.assertFalse(os.path.exists(os.path.join(HOME, "rc-prefs.json")))
        status, resp = self.post(ok)
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["rc"]["sessions"], {"CP": "off"})
        d = jload(os.path.join(HOME, "rc-prefs.json"))
        self.assertEqual(d["sessions"]["CP"]["by"], "board:local")
        ev = self.serve_events()[-1]
        self.assertEqual((ev["by"], ev["code"], ev["state"], ev["result"]), ("local", "CP", "off", "ok"))
        status, resp = self.post({"code": "all", "state": "on"})
        self.assertEqual(status, 200, resp)
        self.assertEqual((resp["rc"]["all"], resp["rc"]["sessions"]), ("on", {}))
        # the page state carries the choices
        with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % self.port, timeout=5) as r:
            st = json.loads(r.read())
        self.assertEqual(st["rc"]["all"], "on")

    def test_not_approver_and_no_approvers(self):
        for approvers, err in (("[boss@example.com]", "not_approver"), ("[]", "no_approvers")):
            with self.subTest(approvers):
                reset_home()
                self.start(approvers)
                status, resp = self.post({"code": "CP", "state": "off"})
                self.assertEqual((status, resp.get("error")), (403, err))
                self.assertFalse(os.path.exists(os.path.join(HOME, "rc-prefs.json")))
                ev = self.serve_events()[-1]
                self.assertEqual(ev["result"], "refused")

    def test_tunnel_jwt(self):
        try:
            import base64
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError:
            self.skipTest("no cryptography module")

        def b64(b):
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

        def num(n):
            return b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub = key.public_key().public_numbers()
        certs = os.path.join(TMP, "certs.json")
        jdump({"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "n": num(pub.n), "e": num(pub.e)}]}, certs)

        def tok(email):
            now = int(time.time())
            claims = {"aud": ["AUD1"], "iss": "https://team.example.com", "exp": now + 600, "iat": now, "email": email}
            hd = b64(json.dumps({"alg": "RS256", "kid": "k1"}).encode())
            pl = b64(json.dumps(claims).encode())
            sig = key.sign((hd + "." + pl).encode(), padding.PKCS1v15(), hashes.SHA256())
            return hd + "." + pl + "." + b64(sig)
        self.stop()
        port, tport = free_port(), free_port()
        type(self).port = port
        write_fleet("serve:\n  port: %d\n  tunnel:\n    enabled: true\n    port: %d\n    hostname: board.example.com\n"
                    "    team_domain: team.example.com\n    aud: AUD1\n    certs_url: file://%s\n"
                    "    allowed_emails: [boss@example.com, viewer@example.com]" % (port, tport, certs),
                    sessions="  - code: CP\n    cwd: /w\n")
        with open(os.path.join(CFG, "fleet.yaml")) as fh:
            body = fh.read().replace("approvers: [local]", "approvers: [boss@example.com]")
        with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
            fh.write(body)
        type(self).srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"],
                                          stdout=subprocess.DEVNULL, stderr=SERVE_ERR)
        time.sleep(1.5)

        def tpost(hdrs):
            h = {"Content-Type": "application/json", "X-Board": "1", "Host": "board.example.com",
                 "Origin": "https://board.example.com"}
            h.update(hdrs)
            req = urllib.request.Request("http://127.0.0.1:%d/api/rc" % tport, data=b'{"code":"CP","state":"off"}',
                                         headers={k: v for k, v in h.items() if v is not None}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                with e:
                    return e.code
        self.assertEqual(tpost({}), 401)                                              # no token: never local
        self.assertEqual(tpost({"Cf-Access-Authenticated-User-Email": "boss@example.com"}), 401)
        self.assertEqual(tpost({"Cf-Access-Jwt-Assertion": tok("viewer@example.com")}), 403)  # viewer, not approver
        self.assertEqual(tpost({"Cf-Access-Jwt-Assertion": tok("boss@example.com"), "Origin": "http://127.0.0.1:%d" % tport}), 403)
        self.assertFalse(os.path.exists(os.path.join(HOME, "rc-prefs.json")))
        self.assertEqual(tpost({"Cf-Access-Jwt-Assertion": tok("boss@example.com")}), 200)
        d = jload(os.path.join(HOME, "rc-prefs.json"))
        self.assertEqual(d["sessions"]["CP"]["by"], "board:boss@example.com")


def tearDownModule():
    close_lives()
    ServeTest.stop()
    SERVE_ERR.close()
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
