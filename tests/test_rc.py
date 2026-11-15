#!/usr/bin/env python3
"""tests/test_rc.py — Remote Control on/off (lib/rc.py, `board rc`, fleet-up, the collector and
board-serve's POST /api/rc). tmux is tests/fake_tmux.py on PATH: scripted screens, every call
logged, never a real tmux server. Never reads ~/.agent-switchboard, ~/.claude or a real config.

  python3 tests/test_rc.py [-v]
"""
import atexit
import glob
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


def remove_tmp(timeout=10.0):
    """Remove TMP once no other process points into it. Detached children of the code under test
    (actions, board session apply) can outlive the test and would write into TMP after the rmtree."""
    needle = (TMP.rstrip("/") + "/").encode()
    deadline = time.time() + timeout
    while time.time() < deadline:
        busy = False
        for path in glob.glob("/proc/[0-9]*/environ"):
            if path == "/proc/%d/environ" % os.getpid():
                continue
            try:
                with open(path, "rb") as fh:
                    busy = needle in fh.read()
            except OSError:
                continue
            if busy:
                break
        if not busy:
            break
        time.sleep(0.1)
    shutil.rmtree(TMP, ignore_errors=True)


atexit.register(remove_tmp, 3.0)   # also on Ctrl-C or a failing setUpModule; tearDownModule waits longer
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
SESS = os.path.join(TMP, "sessions")
BIN = os.path.join(TMP, "bin")
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "CLAUDE_SESSIONS_DIR": SESS,
                   "CLAUDE_JSON": os.path.join(TMP, "claude.json"), "CLAUDE_SETTINGS": os.path.join(TMP, "settings.json"),
                   "ALERT_DESKTOP": "0", "BOARD_PRESS_SETTLE": "0", "BOARD_ACTIONS_AUTO": "1", "BOARD_ACTION_POLL": "0.05"})
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

import actions  # noqa: E402
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


COMPACT_TYPED = screen("❯ /compact", above=["  ❯ /compact     Clear conversation history but keep a summary in context"])
COMPACTING = screen(above=["❯ /compact", "✻ Compacting conversation… (2s · ↓ 162 tokens)"],
                    below=["  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt"])
COMPACTED = screen(above=["❯ /compact", "  ⎿  Compacted (ctrl+o to see full summary)"])
NOTHING = screen(above=["❯ /compact", "  ⎿  Not enough messages to compact."])
CONSENT = "\n".join(HEAD + ["   Remote Control", "   Take this session with you and pick up right where you left off.",
                             "   ❯ Enable Remote Control   Opens a secure connection to claude.ai.",
                             "     Never mind              You can always enable it later with /remote-control.",
                             "   Enter to confirm · Esc to cancel"])
OTHER_PICKER = "\n".join(HEAD + ["   Something else entirely", "   ❯ Yes", "     No", "   Enter to confirm · Esc to cancel"])
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
        fh.write("approvers: [local]\nsession_actions:\n  rc_confirm_seconds: 1\n  compact_start_seconds: 1\n  compact_seconds: 2\n%s\nsessions:\n%s" % (extra, sessions))


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


def proc_start(pid):
    """The process start time (clock ticks since boot), as Claude Code records it (procStart)."""
    with open("/proc/%d/stat" % pid) as fh:
        data = fh.read()
    return str(int(data[data.rindex(")") + 2:].split()[19]))


LIVES = []


def close_lives():
    while LIVES:
        LIVES.pop().close()


class Live:
    """One running session: a live pid, its registry file and hook state."""

    def __init__(self, code="CP", status="idle", bridge=None, state="done", sid="s-cp", pane="%42", env=None):
        self.proc = subprocess.Popen(["sleep", "600"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env=dict(os.environ, **(env or {})))
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
        d.update({"pid": self.pid, "sessionId": self.sid, "cwd": "/w", "startedAt": 1, "statusUpdatedAt": 5,
                  "procStart": proc_start(self.pid)})
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


def script(screens, on_send=None, on_capture=None, session="CP", fail_send=None, seq=None):
    cfg = {"session": session, "screens": screens, "on_send": on_send or {}, "on_capture": on_capture or {},
           "seq": seq or {}, "pane_pid": os.getpid()}  # the test runner: parent of every Live process
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
    for n in os.listdir(HOME):
        if n.startswith("actions-") and n.endswith(".lock"):
            os.remove(os.path.join(HOME, n))
    for n in ("rc-prefs.json", "actions-last.json", "session-actions.json", "events.jsonl", "snapshot.json"):
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
        ev = [e for e in events("session-action") if e["op"] == "run"][-1]
        self.assertEqual((ev["result"], ev["action"], ev["pane"]), ("confirmed", "rc_on", "%42"))
        att = jload(os.path.join(HOME, "actions-last.json"))["CP"]["rc_on"]
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
        att = actions.load_last(HOME)["CP"]["rc_on"]
        self.assertFalse(actions.auto_allowed(cfg, att, "s-cp", rc.now_ms() + 10 ** 9))
        self.assertTrue(actions.auto_allowed(cfg, att, "s-new", rc.now_ms()))

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
        self.assertEqual(actions.auto_candidates(sessions, rc.fleet.load_fleet(), HOME, rc.now_ms()), ["CP"])
        self.assertEqual(actions.auto_candidates(sessions, {"session_actions": {"auto_apply": False}}, HOME, rc.now_ms()), [])
        rc_, out, _ = board("rc", "apply", "--pending", "--auto")
        self.assertEqual(rc_, 0, out)
        self.assertEqual(sent(), ["-l|--|/remote-control", "Enter"])
        self.assertEqual(self.live.bridge(), "session_y")
        self.assertTrue([e for e in events("session-action") if e["op"] == "run"][-1]["auto"])

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
        cfg = {"session_actions": {"retry_minutes": 10, "max_tries": 2}}
        att = {"sid": "s", "result": "refused", "at": 1000, "tries": 1}
        self.assertFalse(actions.auto_allowed(cfg, att, "s", 1000 + 60000))
        self.assertTrue(actions.auto_allowed(cfg, att, "s", 1000 + 600000))
        self.assertFalse(actions.auto_allowed(cfg, dict(att, tries=2), "s", 10 ** 12))
        self.assertTrue(actions.auto_allowed(cfg, att, "s-other", 1000))
        self.assertFalse(actions.auto_allowed(cfg, dict(att, result="partial"), "s", 10 ** 12))

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
            if not actions.run_busy(HOME, "CP"):
                break
            time.sleep(0.1)

    def test_offline(self):
        script([IDLE])
        rc_, out, _ = self.go("on", "CP")
        self.assertEqual(rc_, 0)
        self.assertIn("offline", out)
        self.assertEqual(sent(), [])


class ReviewTest(unittest.TestCase):
    """Dialog escapes, the board choice vs. a change made by hand, locks, pane identity."""

    def setUp(self):
        reset_home()
        write_fleet()

    def tearDown(self):
        close_lives()

    def test_rc_on_opens_disconnect_dialog(self):
        # The registry says off, Claude is on: /remote-control opens the Disconnect dialog.
        Live()
        script([IDLE, TYPED, dialog("Continue"), IDLE])
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual(rc_, 3, out)
        self.assertIn("already on", out)
        self.assertEqual(sent(), ["-l|--|/remote-control", "Enter", "Escape"])

    def test_rc_on_consent(self):
        Live()
        script([IDLE, TYPED, CONSENT, IDLE])
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual(rc_, 6, out)
        self.assertIn("consent", out)
        self.assertEqual(sent(), ["-l|--|/remote-control", "Enter", "Escape"])

    def test_rc_on_unknown_picker_gets_no_escape(self):
        # Not one of Claude's Remote Control dialogs: it may be the user's own; nothing is sent.
        Live()
        script([IDLE, TYPED, OTHER_PICKER, IDLE])
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual(rc_, 6, out)
        self.assertEqual(sent(), ["-l|--|/remote-control", "Enter"])

    def test_rc_on_unconfirmed_with_box_sends_nothing_more(self):
        Live()
        script([IDLE, TYPED, IDLE])
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual(rc_, 6, out)
        self.assertEqual(sent(), ["-l|--|/remote-control", "Enter"])

    def test_rc_off_consent(self):
        # The registry says on, Claude is off and never consented: the consent dialog opens.
        live = Live(bridge="session_x")
        script([IDLE, TYPED, CONSENT, IDLE])
        rc_, out, _ = board("rc", "off", "CP")
        self.assertEqual(rc_, 6, out)
        self.assertIn("consent", out)
        self.assertEqual(sent(), ["-l|--|/remote-control", "Enter", "Escape"])
        self.assertEqual(live.bridge(), "session_x")

    def test_rc_off_unknown_picker_gets_no_escape(self):
        Live(bridge="session_x")
        script([IDLE, TYPED, OTHER_PICKER, IDLE])
        rc_, out, _ = board("rc", "off", "CP")
        self.assertEqual(rc_, 6, out)
        self.assertEqual(sent(), ["-l|--|/remote-control", "Enter"])

    def test_disconnect_label_matches_the_focused_row_only(self):
        c = dict(rc.fleet.DEFAULTS["session_actions"])
        label = actions._rx(c, "rc_disconnect_label")
        self.assertTrue(label.search("Disconnect this session"))
        self.assertFalse(label.search("Show QR code   Disconnect this session later"))
        self.assertFalse(label.search("Do not Disconnect this session"))

    def snap_row(self):
        subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], check=True)
        return [s for s in jload(os.path.join(HOME, "snapshot.json"))["sessions"] if s["code"] == "CP"][0]

    def test_choice_before_the_session_started_is_not_forced(self):
        live = Live()
        board("rc", "on", "CP", "--no-apply")
        live.write(startedAt=rc.now_ms() + 10 ** 6)   # the process started after the choice
        row = self.snap_row()
        self.assertEqual((row["rc"]["want"], row["rc"]["on"], row["rc"]["pending"]), ("on", False, False))
        self.assertEqual(actions.auto_candidates([row], rc.fleet.load_fleet(), HOME, rc.now_ms()), [])

    def test_manual_change_wins_after_one_try(self):
        live = Live()
        script([IDLE, TYPED, ACTIVE], on_send={"2": {"path": live.reg, "set": {"bridgeSessionId": "session_1"}}})
        board("rc", "on", "CP", "--no-apply")
        self.assertTrue(self.snap_row()["rc"]["pending"])
        rc_, out, _ = board("rc", "apply", "--pending", "--auto")
        self.assertEqual(live.bridge(), "session_1")
        # The owner turns it off by hand in the session (e.g. from the phone): the board leaves it.
        live.write(bridgeSessionId=None)
        row = self.snap_row()
        self.assertEqual((row["rc"]["on"], row["rc"]["pending"]), (False, False))
        self.assertEqual(actions.auto_candidates([row], rc.fleet.load_fleet(), HOME, rc.now_ms()), [])
        # A new choice (or an explicit action) forces it again.
        time.sleep(0.01)
        script([IDLE, TYPED, ACTIVE], on_send={"2": {"path": live.reg, "set": {"bridgeSessionId": "session_2"}}})
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual((rc_, live.bridge()), (0, "session_2"), out)

    def test_gave_up_clears_pending(self):
        Live()
        board("rc", "on", "CP", "--no-apply")
        cfg = rc.fleet.load_fleet()
        at = rc.load(HOME)["sessions"]["CP"]["at"]
        for _ in range(3):
            actions.record(HOME, "CP", "rc_on", "s-cp", "refused", "x", rc.now_ms(), at)
        self.assertFalse(self.snap_row()["rc"]["pending"])
        self.assertTrue(actions.rc_settled(cfg, actions.load_last(HOME)["CP"]["rc_on"], "s-cp", at))

    def test_busy_session_lock(self):
        Live()
        script([IDLE])
        lock = actions.SessionLock(HOME, "CP")
        self.assertTrue(lock.acquire(0))
        try:
            with open(os.path.join(CFG, "fleet.yaml")) as fh:
                body = fh.read().replace("session_actions:\n", "session_actions:\n  lock_wait_seconds: 0.2\n")
            with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
                fh.write(body)
            rc_, out, _ = board("rc", "on", "CP")
            self.assertEqual(rc_, 0, out)
            self.assertIn("will apply when the session is free", out)
            self.assertEqual(sent(), [])
            self.assertEqual(rc.load(HOME)["sessions"]["CP"]["state"], "on")   # stored
            subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], check=True)
            snap = jload(os.path.join(HOME, "snapshot.json"))["sessions"]
            self.assertEqual(actions.auto_candidates(snap, rc.fleet.load_fleet(), HOME, rc.now_ms()), [])
        finally:
            lock.release()
        snap = jload(os.path.join(HOME, "snapshot.json"))["sessions"]
        self.assertEqual(actions.auto_candidates(snap, rc.fleet.load_fleet(), HOME, rc.now_ms()), ["CP"])

    def test_pane_must_run_the_session(self):
        Live()
        script([IDLE])
        with open(TCFG) as fh:
            cfgj = json.load(fh)
        cfgj["pane_pid"] = 1   # init: never an ancestor through a live parent chain of our test
        with open(TCFG, "w") as fh:
            json.dump(cfgj, fh)
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual(rc_, 3, out)
        self.assertIn("does not run this session's process", out)
        self.assertEqual(sent(), [])

    def test_pane_checked_without_tmux_in_registry(self):
        live = Live()
        live.write(tmux=None)
        with open(live.state, "w") as fh:   # the pane comes from the hook state
            json.dump({"session_id": "s-cp", "state": "done", "tmux_pane": "%42", "updated_at": 1}, fh)
        script([IDLE])
        with open(TCFG) as fh:
            cfgj = json.load(fh)
        cfgj["pane_pid"] = 1
        with open(TCFG, "w") as fh:
            json.dump(cfgj, fh)
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual(rc_, 3, out)
        self.assertIn("does not run this session's process", out)

    def test_vim_mode_refused(self):
        Live()
        script([screen(below=["  -- NORMAL --"])])
        rc_, out, _ = board("session", "CP", "compact")
        self.assertEqual((rc_, sent()), (3, []), out)
        self.assertIn("vim mode", out)

    def test_queue_without_sid_is_dropped(self):
        with open(os.path.join(HOME, "session-actions.json"), "w") as fh:
            json.dump({"CP": {"action": "compact", "by": "x", "at": rc.now_ms(), "sid": None}}, fh)
        self.assertEqual(actions.load_queue(HOME, {}), {})
        rc_, out, _ = board("session", "CP", "compact")
        self.assertIn("nothing queued", out)          # CP is not running
        self.assertEqual(actions.load_queue(HOME, {}), {})

    def test_code_all_is_reserved(self):
        write_fleet(sessions="  - code: all\n    cwd: /w\n  - code: CP\n    cwd: /w\n")
        self.assertEqual([s["code"] for s in rc.fleet.load_fleet()["sessions"]], ["CP"])

    def test_compact_interrupted(self):
        live = Live()
        # busy while compacting, then idle again with no "Compacted" (Esc pressed in the session)
        script([IDLE, COMPACT_TYPED], seq={"2": [COMPACTING, IDLE]},
               on_send={"2": {"path": live.reg, "set": {"status": "busy", "statusUpdatedAt": 7}}},
               on_capture={"4": {"path": live.reg, "set": {"status": "idle", "statusUpdatedAt": 9}}})
        with open(os.path.join(CFG, "fleet.yaml")) as fh:
            body = fh.read().replace("compact_seconds: 2", "compact_seconds: 30\n  compact_idle_seconds: 0.3")
        with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
            fh.write(body)
        t0 = time.time()
        rc_, out, _ = board("session", "CP", "compact")
        self.assertEqual(rc_, 6, out)
        self.assertIn("interrupted", out)
        self.assertLess(time.time() - t0, 20)


class Round2Test(unittest.TestCase):
    """The recovery Escape only goes into a recognized Remote Control dialog; scrollback text
    never decides; a choice that already matches counts as tried; pid reuse; compact never twice."""

    def setUp(self):
        reset_home()
        write_fleet()

    def tearDown(self):
        close_lives()

    WORKING = screen(above=["❯ /remote-control", "✻ Working… (3s · esc to interrupt)"],
                     below=["  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt"])
    TYPING = screen("❯ the user is typing here", above=["❯ /remote-control"])
    MODEL_PICKER = "\n".join(HEAD + ["❯ /model", "   Select model", "   ❯ 1. Default (recommended)",
                                     "     2. Other model", "   Enter to confirm · Esc to exit"])

    def test_no_escape_into_the_users_work(self):
        for name, scr in (("a running turn", self.WORKING), ("text in the box", self.TYPING),
                          ("the user's own picker", self.MODEL_PICKER)):
            for direction, bridge in (("on", None), ("off", "session_x")):
                with self.subTest(name=name, direction=direction):
                    reset_home()
                    Live(bridge=bridge)
                    script([IDLE, TYPED, scr])
                    rc_, out, _ = board("rc", direction, "CP")
                    self.assertEqual(rc_, 6, out)
                    self.assertIn("unconfirmed", out)
                    self.assertEqual(sent(), ["-l|--|/remote-control", "Enter"], (name, direction))
                    close_lives()

    def test_scrollback_never_decides(self):
        old = ["● Unknown command: /remote-control", "  Never mind   You can always enable it later", "  Enable Remote Control",
               "  older output", "  more older output", "  and more", "  and even more"]
        idle = screen(above=old)
        typed = screen("❯ /remote-control", above=old + ["  ❯ /remote-control   Control this session"])
        # on: the bridge appears; the scrollback phrases are not "unavailable" or "consent"
        live = Live()
        script([idle, typed, screen(above=old + ["❯ /remote-control", "  /remote-control is active"])],
               on_send={"2": {"path": live.reg, "set": {"bridgeSessionId": "session_s"}}})
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual((rc_, sent()), (0, ["-l|--|/remote-control", "Enter"]), out)
        close_lives()
        # on, never confirmed: unconfirmed, no Escape, not a sticky "unavailable"
        reset_home()
        Live()
        script([idle, typed, idle])
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual((rc_, sent()), (6, ["-l|--|/remote-control", "Enter"]), out)
        self.assertIn("unconfirmed", out)
        close_lives()
        # off: the same scrollback above the box is not the consent dialog
        reset_home()
        Live(bridge="session_x")
        script([idle, typed, idle])
        rc_, out, _ = board("rc", "off", "CP")
        self.assertEqual((rc_, sent()), (6, ["-l|--|/remote-control", "Enter"]), out)

    def test_unavailable_fresh_above_box(self):
        Live()
        script([IDLE, TYPED, UNKNOWN])
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual((rc_, sent()), (6, ["-l|--|/remote-control", "Enter"]), out)
        self.assertIn("unavailable", out)

    def test_matching_choice_counts_as_tried(self):
        live = Live(bridge="session_on")
        script([IDLE])
        rc_, out, _ = board("rc", "on", "all", "--no-apply")
        self.assertEqual(rc_, 0, out)
        rec = actions.load_last(HOME)["CP"]["rc_on"]
        self.assertEqual((rec["result"], rec["choice_at"]), ("already", rc.load(HOME)["all"]["at"]))
        live.write(bridgeSessionId=None)   # turned off by hand, e.g. from the phone
        subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], check=True)
        row = [x for x in jload(os.path.join(HOME, "snapshot.json"))["sessions"] if x["code"] == "CP"][0]
        self.assertEqual((row["rc"]["on"], row["rc"]["pending"]), (False, False))
        self.assertEqual(actions.auto_candidates([row], rc.fleet.load_fleet(), HOME, rc.now_ms()), [])
        self.assertEqual(sent(), [])

    def test_reused_pid_is_not_the_session(self):
        live = Live()
        live.write(procStart="1")   # the registry describes an earlier process with this pid
        script([IDLE])
        rc_, out, _ = board("rc", "on", "CP")
        self.assertIn("offline", out)
        self.assertEqual(sent(), [])
        self.assertFalse(actions.same_process({"procStart": "1"}, live.pid))
        self.assertTrue(actions.same_process({"procStart": proc_start(live.pid)}, live.pid))
        # Without procStart only the zombie check applies: a wall-clock startedAt is never compared
        # (it drifts after the host sleeps).
        self.assertTrue(actions.same_process({"startedAt": 1}, live.pid))
        self.assertFalse(actions.same_process({"procStart": proc_start(live.pid)}, 999999999))

    def test_compact_unconfirmed_never_retried(self):
        Live()
        script([IDLE, COMPACT_TYPED, IDLE])
        rc_, out, _ = board("session", "CP", "compact")
        self.assertEqual(rc_, 6, out)
        self.assertEqual(actions.load_queue(HOME, rc.fleet.load_fleet()), {})   # done: never twice
        rec = actions.load_last(HOME)["CP"]["compact"]
        self.assertFalse(actions.auto_allowed(rc.fleet.load_fleet(), rec, "s-cp", 10 ** 13, None, "compact"))
        self.assertTrue(actions.auto_allowed(rc.fleet.load_fleet(), dict(rec, result="refused"), "s-cp", 10 ** 13, None, "compact"))

    def test_idle_sessions_get_no_lock_files(self):
        Live()
        subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], check=True)
        snap = jload(os.path.join(HOME, "snapshot.json"))["sessions"]
        self.assertEqual(actions.auto_candidates(snap, rc.fleet.load_fleet(), HOME, rc.now_ms()), [])
        self.assertFalse(os.path.exists(actions.lock_path(HOME, "CP")))


class Round3Test(unittest.TestCase):
    """rc_dialog without an echo on screen, and what counts as freshly printed (review round 3)."""

    def setUp(self):
        reset_home()
        write_fleet()

    def tearDown(self):
        close_lives()

    c = {}
    U = "● Unknown command: /remote-control"

    def lines(self, s):
        return s.split("\n")

    def test_no_echo_scrollback_and_user_picker(self):
        # The echo scrolled away: old consent words in the scrollback and a picker of the user's own.
        noecho = "\n".join(HEAD + ["  To turn it on: Enable Remote Control, or pick Never mind", "   Select model",
                                    "   ❯ 1. Default", "     2. Other", "   Enter to confirm · Esc to exit"])
        self.assertIsNone(actions.rc_dialog(self.lines(noecho), self.c))
        noecho2 = "\n".join(HEAD + ["  Disconnect this session", "  older", "   Pick one", "   ❯ Yes", "     No",
                                     "   Enter to select · Esc"])
        self.assertIsNone(actions.rc_dialog(self.lines(noecho2), self.c))
        withecho = "\n".join(HEAD + ["  Disconnect this session", "❯ do it", "   Pick one", "   ❯ Yes",
                                      "   Enter to select · Esc"])
        self.assertIsNone(actions.rc_dialog(self.lines(withecho), self.c))
        odd = dialog("Continue").replace("Enter to select · Esc to continue", "↑↓ to navigate · Esc to go back")
        self.assertIsNone(actions.rc_dialog(self.lines(odd), self.c))
        self.assertIsNone(actions.rc_dialog(self.lines(dialog("Continue") + "\n  esc to interrupt"), self.c))
        self.assertEqual(actions.rc_dialog(self.lines(CONSENT), self.c), "consent")
        self.assertEqual(actions.rc_dialog(self.lines(dialog("Continue")), self.c), "disconnect")
        # The dialog itself, its echo gone and a blank line above its footer, is still recognized.
        bare = "\n".join(["  older output", "", "   Remote Control", "   ❯ Disconnect this session",
                           "     Show QR code   Scan with your phone", "     Continue", "", "   Enter to select · Esc to continue"])
        self.assertEqual(actions.rc_dialog(self.lines(bare), self.c), "disconnect")
        # ... and the flow sends no Escape into the user's picker.
        Live()
        script([IDLE, TYPED, noecho])
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual((rc_, sent()), (6, ["-l|--|/remote-control", "Enter"]), out)
        self.assertNotIn("consent", out)

    def test_fresh_counts_not_positions(self):
        L = self.lines
        before = screen("❯ /remote-control", above=[self.U, "  s1", "  s2", "  s3", "  s4"])
        after = screen(above=[self.U, "❯ /remote-control", "  ⎿  Remote Control connecting…"])
        self.assertFalse(actions.fresh_above_box(L(after), L(before), self.c, "rc_unavailable_regex"))
        before2 = screen("❯ /remote-control", above=[self.U])
        after2 = screen(above=[self.U, "❯ /remote-control", self.U])
        self.assertTrue(actions.fresh_above_box(L(after2), L(before2), self.c, "rc_unavailable_regex"))
        before3 = screen("❯ /remote-control", above=[self.U, "a", "b", "c", "d"])
        after3 = screen(above=[self.U, "a", "b", "c", "d", "❯ /remote-control"])
        self.assertFalse(actions.fresh_above_box(L(after3), L(before3), self.c, "rc_unavailable_regex"))

    def test_fresh_counts_over_the_whole_pane(self):
        # A small window: before Enter, lines below the box push an old "Unknown command" just out
        # of the last-8-lines window; after Enter they are gone and the window reaches it. Counted
        # over the whole visible pane, it was there before: not fresh, not "unavailable".
        U = self.U
        before = HEAD + [U, "  a"] + [TOP, "❯ /remote-control", RULE] + ["  s%d" % i for i in range(1, 6)]
        after = HEAD + [U, "  a", "❯ /remote-control"] + [TOP, "❯" + NB, RULE] + ["  ⏵⏵ auto mode on"]
        self.assertTrue(actions.fresh_above_box(after[-8:], before[-8:], self.c, "rc_unavailable_regex"))  # the old way
        self.assertFalse(actions.fresh_above_box(after, before, self.c, "rc_unavailable_regex"))
        with open(os.path.join(CFG, "fleet.yaml")) as fh:
            body = fh.read().replace("session_actions:\n", "session_actions:\n  lines: 8\n")
        with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
            fh.write(body)
        Live()
        idle = "\n".join(HEAD + [U, "  a"] + [TOP, "❯" + NB, RULE] + ["  ⏵⏵ auto mode on"])
        script([idle, "\n".join(before), "\n".join(after)])
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual((rc_, sent()), (6, ["-l|--|/remote-control", "Enter"]), out)
        self.assertIn("unconfirmed", out)
        self.assertNotIn("unavailable", out)

    def test_late_dialog_is_left_alone(self):
        # A dialog that opens after rc_confirm_seconds: unconfirmed, nothing more sent; a second
        # attempt finds no prompt box and sends nothing.
        Live()
        after = screen(above=["❯ /remote-control"])
        script([IDLE, TYPED, CONSENT], seq={"2": [after] * 40 + [CONSENT]})
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual((rc_, sent()), (6, ["-l|--|/remote-control", "Enter"]), out)
        script([CONSENT])
        rc_, out, _ = board("rc", "apply", "CP")
        self.assertEqual((rc_, sent()), (3, []), out)


class CompactTest(unittest.TestCase):
    def setUp(self):
        reset_home()
        write_fleet()

    def tearDown(self):
        close_lives()

    def queue(self):
        return actions.load_queue(HOME, rc.fleet.load_fleet())

    def test_idle_compacts(self):
        live = Live()
        script([IDLE, COMPACT_TYPED], seq={"2": [COMPACTING, COMPACTED]},
               on_send={"2": {"path": live.reg, "set": {"status": "busy", "statusUpdatedAt": 7}}},
               on_capture={"4": {"path": live.reg, "set": {"status": "idle", "statusUpdatedAt": 9}}})
        rc_, out, err = board("session", "CP", "compact", "--by", "tester")
        self.assertEqual(rc_, 0, out + err)
        self.assertIn("compact: confirmed", out)
        self.assertEqual(sent(), ["-l|--|/compact", "Enter"])
        self.assertEqual(self.queue(), {})
        last = actions.load_last(HOME)["CP"]["compact"]
        self.assertEqual((last["result"], last["sid"]), ("confirmed", "s-cp"))
        self.assertTrue(last["done_at"])
        row = [s for s in jload(os.path.join(HOME, "snapshot.json"))["sessions"] if s["code"] == "CP"][0]
        self.assertEqual((row["compact"]["queued"], row["compact"]["done_at"]), (None, last["done_at"]))
        ops = [(e["op"], e.get("action"), e.get("by")) for e in events("session-action")]
        self.assertIn(("queue", "compact", "tester"), ops)
        self.assertIn(("run", "compact", "tester"), ops)

    def test_busy_queues_then_runs_when_idle(self):
        live = Live(status="busy", state="working")
        script([IDLE, COMPACT_TYPED], seq={"2": [COMPACTING, COMPACTED]},
               on_send={"2": {"path": live.reg, "set": {"status": "busy", "statusUpdatedAt": 7}}})
        rc_, out, _ = board("session", "CP", "compact")
        self.assertEqual(rc_, 0, out)
        self.assertIn("deferred", out)
        self.assertIn("runs when the session is idle", out)
        self.assertEqual(sent(), [])
        self.assertEqual(self.queue()["CP"]["action"], "compact")
        row = [s for s in jload(os.path.join(HOME, "snapshot.json"))["sessions"] if s["code"] == "CP"][0]
        self.assertTrue(row["compact"]["queued"])
        # the session goes idle; the collector's applier runs the queued compact
        live.write(status="idle", statusUpdatedAt=6)
        with open(live.state, "w") as fh:
            json.dump({"session_id": "s-cp", "state": "done", "tmux_pane": "%42", "updated_at": 1}, fh)
        script([IDLE, COMPACT_TYPED], seq={"2": [COMPACTING, COMPACTED]},
               on_send={"2": {"path": live.reg, "set": {"status": "busy", "statusUpdatedAt": 7}}},
               on_capture={"4": {"path": live.reg, "set": {"status": "idle", "statusUpdatedAt": 9}}})
        subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], check=True)
        snap = jload(os.path.join(HOME, "snapshot.json"))["sessions"]
        self.assertEqual(actions.auto_candidates(snap, rc.fleet.load_fleet(), HOME, rc.now_ms()), ["CP"])
        rc_, out, _ = board("session", "apply", "--pending", "--auto")
        self.assertEqual(rc_, 0, out)
        self.assertEqual(sent(), ["-l|--|/compact", "Enter"])
        self.assertEqual(self.queue(), {})

    def test_nothing_to_compact(self):
        Live()
        script([IDLE, COMPACT_TYPED, NOTHING])
        rc_, out, _ = board("session", "CP", "compact")
        self.assertEqual(rc_, 0, out)
        self.assertIn("nothing", out)
        self.assertEqual(self.queue(), {})

    def test_never_starts(self):
        Live()
        script([IDLE, COMPACT_TYPED, IDLE])
        rc_, out, _ = board("session", "CP", "compact")
        self.assertEqual(rc_, 6, out)
        self.assertIn("unconfirmed", out)
        self.assertEqual(self.queue(), {})   # Enter was pressed: never retried, never twice

    def test_still_running(self):
        live = Live()
        script([IDLE, COMPACT_TYPED, COMPACTING],
               on_send={"2": {"path": live.reg, "set": {"status": "busy", "statusUpdatedAt": 7}}})
        rc_, out, _ = board("session", "CP", "compact")
        self.assertEqual(rc_, 0, out)
        self.assertIn("started", out)
        self.assertEqual(self.queue(), {})

    def test_unexpected_screen_and_partial(self):
        Live()
        script([screen("❯ draft text")])
        rc_, out, _ = board("session", "CP", "compact")
        self.assertEqual((rc_, sent()), (3, []), out)
        reset_home()
        script([IDLE, screen("❯ /compactx")])
        rc_, out, _ = board("session", "CP", "compact")
        self.assertEqual((rc_, sent()), (5, ["-l|--|/compact"]), out)

    def test_cancel_and_restart(self):
        live = Live(status="busy", state="working")
        script([IDLE])
        board("session", "CP", "compact")
        rc_, out, _ = board("session", "CP", "cancel", "--by", "tester")
        self.assertEqual(rc_, 0)
        self.assertEqual(self.queue(), {})
        self.assertIn(("cancel", "tester"), [(e["op"], e.get("by")) for e in events("session-action")])
        board("session", "CP", "compact")
        live.close()
        LIVES.remove(live)
        Live(sid="s-new")
        rc_, out, _ = board("session", "apply", "--pending")
        self.assertIn("dropped", out)
        self.assertEqual((self.queue(), sent()), ({}, []))

    def test_usage(self):
        for args in (["session"], ["session", "CP"], ["session", "CP", "hack"], ["session", "CP", "rc", "maybe"],
                     ["session", "bad code", "compact"], ["session", "CP", "compact", "--by", "a b"],
                     ["session", "NOPE", "compact"]):
            rc_, _, err = board(*args)
            self.assertEqual(rc_, 2, (args, err))
        self.assertEqual(self.queue(), {})

    def test_session_rc_alias(self):
        live = Live()
        script([IDLE, TYPED, ACTIVE], on_send={"2": {"path": live.reg, "set": {"bridgeSessionId": "session_q"}}})
        rc_, out, _ = board("session", "CP", "rc", "on")
        self.assertEqual(rc_, 0, out)
        self.assertEqual(live.bridge(), "session_q")
        self.assertEqual(rc.load(HOME)["sessions"]["CP"]["state"], "on")


class DoNotTrackTest(unittest.TestCase):
    def setUp(self):
        reset_home()
        write_fleet()

    def tearDown(self):
        close_lives()

    def test_blocked(self):
        Live(env={"DO_NOT_TRACK": "1"})
        script([IDLE])
        rc_, out, _ = board("rc", "on", "CP")
        self.assertEqual(rc_, 6, out)
        self.assertIn("DO_NOT_TRACK", out)
        self.assertEqual(sent(), [])
        row = [s for s in jload(os.path.join(HOME, "snapshot.json"))["sessions"] if s["code"] == "CP"][0]
        self.assertEqual((row["rc"]["blocked"], row["rc"]["pending"]), ("do_not_track", False))
        self.assertEqual(actions.auto_candidates([row], rc.fleet.load_fleet(), HOME, rc.now_ms()), [])

    def test_fleet_up_option(self):
        write_fleet("rc:\n  unset_do_not_track: true", sessions="  - code: CP\n    cwd: %s\n" % TMP)
        script([""])
        rc_, out, _ = run(os.path.join(REPO, "bin", "fleet-up"), "--dry-run", "--only", "CP", env={"DO_NOT_TRACK": "1"})
        self.assertIn("env -u DO_NOT_TRACK claude -n CP --remote-control CP", out)
        write_fleet(sessions="  - code: CP\n    cwd: %s\n" % TMP)
        rc_, out, _ = run(os.path.join(REPO, "bin", "fleet-up"), "--dry-run", "--only", "CP", env={"DO_NOT_TRACK": "1"})
        self.assertNotIn("env -u", out)
        rc_, out, _ = run(os.path.join(REPO, "bin", "fleet-up"), "--dry-run", "--only", "CP")
        self.assertNotIn("env -u", out)


class ScreenTest(unittest.TestCase):
    cfg = dict(rc.fleet.DEFAULTS["session_actions"])

    def lines(self, s):
        return s.split("\n")

    def test_idle(self):
        self.assertIsNone(actions.idle_screen(self.lines(IDLE), self.cfg))

    def test_typed_box(self):
        i, text = actions.prompt_box(self.lines(TYPED), self.cfg)
        self.assertEqual(text, "/remote-control")

    def test_history_prompt_is_not_the_box(self):
        # the dialog hides the input box; the echoed "❯ /remote-control" above it is history
        i, why = actions.prompt_box(self.lines(dialog("Continue")), self.cfg)
        self.assertIsNone(i)

    def test_focus(self):
        self.assertEqual(actions._focused(self.lines(dialog("Show QR"))), "Show QR code   Scan with your phone to open this session")


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
        # POST /api/sessions/<code>/action
        act = "/api/sessions/CP/action"
        for name, body, hdrs, path, want in (
                ("bad action", {"action": "rm -rf"}, None, act, 400),
                ("free text", {"action": "compact; ls"}, None, act, 400),
                ("unknown session", {"action": "compact"}, None, "/api/sessions/NOPE/action", 404),
                ("bad code", {"action": "compact"}, None, "/api/sessions/a%20b/action", 404),
                ("no X-Board", {"action": "compact"}, {"X-Board": None}, act, 403),
                ("cross origin", {"action": "compact"}, {"Origin": "https://evil.example"}, act, 403)):
            with self.subTest(name):
                status, resp = self.post(body, hdrs, path=path)
                self.assertEqual(status, want, (name, resp))
        self.assertEqual(actions.load_queue(HOME, {}), {})
        status, resp = self.post({"action": "compact"}, path=act)
        self.assertEqual(status, 200, resp)
        self.assertEqual(actions.load_queue(HOME, {}), {})          # CP is not running: nothing queued
        Live(status="busy", state="working")
        status, resp = self.post({"action": "compact"}, path=act)
        self.assertEqual(status, 200, resp)
        self.assertEqual(actions.load_queue(HOME, {})["CP"]["by"], "board:local")
        ev = [e for e in events("serve") if e.get("action") == "session-action"][-1]
        self.assertEqual((ev["code"], ev["name"], ev["result"]), ("CP", "compact", "ok"))
        status, resp = self.post({"action": "rc_off"}, path=act)
        self.assertEqual((status, resp["rc"]["sessions"]), (200, {"CP": "off"}))

    def test_not_approver_and_no_approvers(self):
        for approvers, err in (("[boss@example.com]", "not_approver"), ("[]", "no_approvers")):
            with self.subTest(approvers):
                reset_home()
                self.start(approvers)
                status, resp = self.post({"code": "CP", "state": "off"})
                self.assertEqual((status, resp.get("error")), (403, err))
                status, resp = self.post({"action": "compact"}, path="/api/sessions/CP/action")
                self.assertEqual((status, resp.get("error")), (403, err))
                self.assertEqual(actions.load_queue(HOME, {}), {})
                # authorization comes before input checks: a bad body is refused and audited too
                status, resp = self.post({"action": "rm -rf /"}, path="/api/sessions/NOPE/action")
                self.assertEqual((status, resp.get("error")), (403, err))
                ev = [e for e in events("serve") if e.get("action") == "session-action"][-1]
                self.assertEqual((ev["result"], ev["code"], ev["name"]), ("refused", "NOPE", "rm -rf /"))
                status, resp = self.post({"code": ["x"], "state": 7})
                self.assertEqual(status, 403)
                ev = self.serve_events()[-1]
                self.assertEqual((ev["code"], ev["state"]), ("?", "?"))
                self.assertFalse(os.path.exists(os.path.join(HOME, "rc-prefs.json")))
                ev = self.serve_events()[-1]
                self.assertEqual(ev["result"], "refused")

    def tearDown(self):
        close_lives()

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
    remove_tmp()


if __name__ == "__main__":
    unittest.main()
