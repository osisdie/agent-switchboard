#!/usr/bin/env python3
"""tests/test_press_tall.py — `board press` on a shortened question taller than the pane, through a
real tmux: tests/fake_claude_tui.py (state question) draws an AskUserQuestion picker whose boxed
question scrolls off the top of a short pane, into tmux's history. The start of the question is
then only in the scrollback; press finds it there (press.scrollback_lines) and sends the option's
key, but not with the scrollback switched off, and not with another turn drawn in between.

The pane runs in a PRIVATE tmux server (`tmux -L <test socket> -f /dev/null`, its socket under the
test's temporary directory, TMUX unset); only that server is killed. Never reads
~/.agent-switchboard, ~/.claude or a real config. Skipped without tmux.

  python3 tests/test_press_tall.py [-v]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TMP = tempfile.mkdtemp(prefix="switchboard-press-tall.")
HOME, CFG, SESS, BIN = (os.path.join(TMP, x) for x in ("home", "config", "sessions", "bin"))
for _d in (HOME, CFG, SESS, BIN):
    os.makedirs(_d)
for _k in list(os.environ):
    if _k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_")) or _k in ("TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL"):
        os.environ.pop(_k)
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "CLAUDE_SESSIONS_DIR": SESS,
                   "CLAUDE_JSON": os.path.join(TMP, "claude.json"), "CLAUDE_SETTINGS": os.path.join(TMP, "settings.json"),
                   "ALERT_DESKTOP": "0", "BOARD_PRESS_SETTLE": "0.3"})
subprocess.run([sys.executable, os.path.join(REPO, "tests", "fake_proc.py"), os.path.join(TMP, "proc")], check=True)
os.environ["BOARD_PROC_ROOT"] = os.path.join(TMP, "proc")
TMUX_REAL = shutil.which("tmux")
# tmux's socket path must fit a unix socket address (~100 bytes): a short directory when TMP is long.
SOCKDIR = os.path.join(TMP, "s") if len(TMP) < 60 else tempfile.mkdtemp(prefix="sbpt.", dir="/tmp")
os.makedirs(SOCKDIR, mode=0o700, exist_ok=True)
if TMUX_REAL:
    with open(os.path.join(BIN, "tmux"), "w") as fh:
        fh.write('#!/bin/sh\nunset TMUX TMUX_PANE\nTMUX_TMPDIR=%s\nexport TMUX_TMPDIR\nexec %s -u -L press-tall-test -f /dev/null "$@"\n'
                 % (SOCKDIR, TMUX_REAL))
    os.chmod(os.path.join(BIN, "tmux"), 0o755)
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]
BOARD = os.path.join(REPO, "bin", "board")
HOOK = os.path.join(REPO, "hooks", "board-hook.sh")
QUESTION = "Which color do you prefer for the test? " + "context " * 60
sys.path.insert(0, os.path.join(REPO, "tests"))
from fake_claude_tui import QUESTION_CJK  # noqa: E402


def questions(text):
    return [{"question": text.strip(), "header": "Color", "multiSelect": False,
             "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"},
                         {"label": "Green", "description": "natural"}]}]


def tmux(*args):
    return subprocess.run([os.path.join(BIN, "tmux")] + list(args), capture_output=True, text=True, check=False).stdout


def board(*args):
    r = subprocess.run([BOARD] + list(args), capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def proc_start(pid):
    with open("/proc/%d/stat" % pid) as fh:
        data = fh.read()
    return str(int(data[data.rindex(")") + 2:].split()[19]))


@unittest.skipUnless(TMUX_REAL, "tmux is not installed")
class TallQuestion(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(HOME, ignore_errors=True)
        os.makedirs(HOME)
        for f in os.listdir(SESS):
            os.unlink(os.path.join(SESS, f))
        tmux("kill-server")

    def tearDown(self):
        tmux("kill-server")   # only the private server of this test

    def start(self, state, extra=""):
        with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
            # the fake TUI runs as python3: the TUI command check is off here
            fh.write("approvers: [local]\npress:\n  tui_command_regex: ''\n%s\nsessions:\n  - code: CP\n    cwd: /w\n" % extra)
        d = tempfile.mkdtemp(dir=TMP)
        cmd = "%s %s %s %s" % (sys.executable, os.path.join(REPO, "tests", "fake_claude_tui.py"), d, state)
        for _ in range(3):   # a server still shutting down refuses new-session
            tmux("new-session", "-d", "-s", "CP", "-x", "100", "-y", "20", cmd)
            for _ in range(100):
                if os.path.exists(os.path.join(d, "pid")) and "Enter to select" in tmux("capture-pane", "-p", "-t", "CP"):
                    break
                time.sleep(0.05)
            else:
                continue
            break
        with open(os.path.join(d, "pid")) as fh:
            pid = int(fh.read())
        pane = tmux("display-message", "-p", "-t", "CP", "#{pane_id}").strip()
        sid = "s-cp"
        with open(os.path.join(SESS, "%d.json" % pid), "w") as fh:
            json.dump({"pid": pid, "sessionId": sid, "cwd": "/w", "startedAt": 1, "statusUpdatedAt": 5,
                       "procStart": proc_start(pid), "name": "CP", "tmux": "CP:@1.%s" % pane, "status": "waiting"}, fh)
        for ev in ({"hook_event_name": "PreToolUse", "tool_use_id": "tu_tall"}, {"hook_event_name": "PermissionRequest"}):
            ev.update({"session_id": sid, "permission_mode": "default", "tool_name": "AskUserQuestion",
                       "tool_input": {"questions": questions(QUESTION_CJK if state == "question-cjk" else QUESTION)}})
            subprocess.run([HOOK], input=json.dumps(ev), text=True, capture_output=True, check=True,
                           env=dict(os.environ, TMUX_PANE=pane))
        decs = []
        for f in os.listdir(os.path.join(HOME, "decisions")):
            with open(os.path.join(HOME, "decisions", f)) as fh:
                decs.append(json.load(fh))
        dec = [x for x in decs if x.get("tool_use_id") == "tu_tall"][0]
        self.assertTrue(dec["questions_clipped"] and dec["pressable"], dec)
        rc, out = board("answer", dec["id"], "Blue", "--by", "local")
        self.assertEqual(rc, 0, out)
        return d, dec["id"]

    def keys(self, d):
        try:
            with open(os.path.join(d, "keys")) as fh:
                return [json.loads(ln)["key"] for ln in fh]
        except OSError:
            return []

    def status(self, did):
        with open(os.path.join(HOME, "decisions", did + ".json")) as fh:
            return json.load(fh)["status"]

    def test_start_in_scrollback_pressed(self):
        d, did = self.start("question")
        visible = tmux("capture-pane", "-p", "-t", "CP")
        self.assertNotIn("Which color", visible, "the start of the question is off the screen")
        self.assertIn("Which color", tmux("capture-pane", "-p", "-t", "CP", "-S", "-200"), "...and in the history")
        rc, out = board("press", "CP", "answer", "--expect", "tu_tall")
        self.assertEqual(rc, 0, out + visible)
        for _ in range(40):
            if self.keys(d):
                break
            time.sleep(0.05)
        self.assertEqual(self.keys(d), ["2"])
        self.assertEqual(self.status(did), "relayed")

    def test_cjk_full_width_space(self):
        # The start holds a full-width space (U+3000): the scrollback text is stripped like the
        # question (ASCII whitespace only), so it is found.
        d, did = self.start("question-cjk")
        self.assertNotIn("請選擇", tmux("capture-pane", "-p", "-t", "CP"), "the start is off the screen")
        rc, out = board("press", "CP", "answer", "--expect", "tu_tall")
        self.assertEqual(rc, 0, out)
        for _ in range(40):
            if self.keys(d):
                break
            time.sleep(0.05)
        self.assertEqual(self.keys(d), ["2"])
        self.assertEqual(self.status(did), "relayed")

    def test_scrollback_off_refused(self):
        d, did = self.start("question", "  scrollback_lines: 0")
        rc, out = board("press", "CP", "answer", "--expect", "tu_tall")
        self.assertEqual(rc, 3, out)
        time.sleep(0.3)
        self.assertEqual(self.keys(d), [])
        self.assertEqual(self.status(did), "stale")

    def test_other_turn_between_refused(self):
        d, did = self.start("question-turn")
        rc, out = board("press", "CP", "answer", "--expect", "tu_tall")
        self.assertEqual(rc, 3, out)
        self.assertIn("another turn", out)
        time.sleep(0.3)
        self.assertEqual(self.keys(d), [])


def tearDownModule():
    tmux("kill-server")
    shutil.rmtree(SOCKDIR, ignore_errors=True)
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[a for a in sys.argv if a != "-v"])
