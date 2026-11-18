#!/usr/bin/env python3
"""tests/test_press_form.py — `board press` on a form of several questions, through a real tmux:
tests/fake_claude_tui.py (states form2, form3, form3-q2) draws the form as Claude Code 2.1.3xx
does, each question in a box ("│ ..." on every line, a wrapped line too) and the picker footer
wrapped onto two lines. press answers it question by question (digits, a "Type something" text)
and submits it on the review screen, whose questions are boxed too; with another question showing
it sends nothing.

The pane runs in a PRIVATE tmux server (`tmux -L <test socket> -f /dev/null`, its socket under the
test's temporary directory, TMUX unset); only that server is killed. Never reads
~/.agent-switchboard, ~/.claude or a real config. Skipped without tmux.

  python3 tests/test_press_form.py [-v]
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
TMP = tempfile.mkdtemp(prefix="switchboard-press-form.")
HOME, CFG, SESS, BIN = (os.path.join(TMP, x) for x in ("home", "config", "sessions", "bin"))
for _d in (HOME, CFG, SESS, BIN):
    os.makedirs(_d)
for _k in list(os.environ):
    if _k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_")) or _k in ("TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL"):
        os.environ.pop(_k)
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "CLAUDE_SESSIONS_DIR": SESS,
                   "CLAUDE_JSON": os.path.join(TMP, "claude.json"), "CLAUDE_SETTINGS": os.path.join(TMP, "settings.json"),
                   "ALERT_DESKTOP": "0", "BOARD_PRESS_SETTLE": "0.2"})
subprocess.run([sys.executable, os.path.join(REPO, "tests", "fake_proc.py"), os.path.join(TMP, "proc")], check=True)
os.environ["BOARD_PROC_ROOT"] = os.path.join(TMP, "proc")
TMUX_REAL = shutil.which("tmux")
# tmux's socket path must fit a unix socket address (~100 bytes): a short directory when TMP is long.
SOCKDIR = os.path.join(TMP, "s") if len(TMP) < 60 else tempfile.mkdtemp(prefix="sbpf.", dir="/tmp")
os.makedirs(SOCKDIR, mode=0o700, exist_ok=True)
if TMUX_REAL:
    with open(os.path.join(BIN, "tmux"), "w") as fh:
        fh.write('#!/bin/sh\nunset TMUX TMUX_PANE\nTMUX_TMPDIR=%s\nexport TMUX_TMPDIR\nexec %s -u -L press-form-test -f /dev/null "$@"\n'
                 % (SOCKDIR, TMUX_REAL))
    os.chmod(os.path.join(BIN, "tmux"), 0o755)
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]
BOARD = os.path.join(REPO, "bin", "board")
HOOK = os.path.join(REPO, "hooks", "board-hook.sh")
sys.path.insert(0, os.path.join(REPO, "tests"))
from fake_claude_tui import FORM  # noqa: E402


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
class BoxedForm(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(HOME, ignore_errors=True)
        os.makedirs(HOME)
        for f in os.listdir(SESS):
            os.unlink(os.path.join(SESS, f))
        tmux("kill-server")

    def tearDown(self):
        tmux("kill-server")   # only the private server of this test

    def start(self, state, nq, answer):
        """The fake TUI in STATE, the hook told about the first NQ questions of FORM, and the
        decision answered with ANSWER (board answer arguments). Returns (dir, decision id)."""
        with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
            # the fake TUI runs as python3: the TUI command check is off here
            fh.write("approvers: [local]\npress:\n  tui_command_regex: ''\nsessions:\n  - code: CP\n    cwd: /w\n")
        d = tempfile.mkdtemp(dir=TMP)
        cmd = "%s %s %s %s" % (sys.executable, os.path.join(REPO, "tests", "fake_claude_tui.py"), d, state)
        for _ in range(3):   # a server still shutting down refuses new-session
            tmux("new-session", "-d", "-s", "CP", "-x", "60", "-y", "40", cmd)
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
        for ev in ({"hook_event_name": "PreToolUse", "tool_use_id": "tu_form"}, {"hook_event_name": "PermissionRequest"}):
            ev.update({"session_id": sid, "permission_mode": "default", "tool_name": "AskUserQuestion",
                       "tool_input": {"questions": FORM[:nq]}})
            subprocess.run([HOOK], input=json.dumps(ev), text=True, capture_output=True, check=True,
                           env=dict(os.environ, TMUX_PANE=pane))
        decs = []
        for f in os.listdir(os.path.join(HOME, "decisions")):
            with open(os.path.join(HOME, "decisions", f)) as fh:
                decs.append(json.load(fh))
        dec = [x for x in decs if x.get("tool_use_id") == "tu_form"][0]
        self.assertTrue(dec["pressable"] and not dec["questions_clipped"], dec)
        if answer[0] == "--form-file":
            ff = os.path.join(TMP, "form.json")
            with open(ff, "w") as fh:
                json.dump(answer[1], fh)
            answer = ["--form-file", ff]
        rc, out = board("answer", dec["id"], *answer, "--by", "local")
        self.assertEqual(rc, 0, out)
        return d, dec["id"]

    def keys(self, d):
        try:
            with open(os.path.join(d, "keys")) as fh:
                return [json.loads(ln)["key"] for ln in fh]
        except OSError:
            return []

    def answers(self, d):
        for _ in range(40):
            if os.path.exists(os.path.join(d, "answers")):
                with open(os.path.join(d, "answers"), encoding="utf-8") as fh:
                    return json.load(fh)
            time.sleep(0.05)
        return None

    def dec(self, did):
        with open(os.path.join(HOME, "decisions", did + ".json")) as fh:
            return json.load(fh)

    def test_two_questions(self):
        d, did = self.start("form2", 2, ["--options", "2,1"])
        screen = tmux("capture-pane", "-p", "-t", "CP")
        self.assertIn("│ TICKET-12", screen, "the question is drawn in a box")
        self.assertIn("\ncancel", screen, "the footer wraps")
        rc, out = board("press", "CP", "answer", "--expect", "tu_form")
        self.assertEqual(rc, 0, out + screen)
        self.assertEqual(self.answers(d), ["人層", "Small"])
        self.assertEqual(self.keys(d), ["2", "1", "1"])
        self.assertEqual(self.dec(did)["status"], "relayed")

    def test_three_questions_with_free_text(self):
        # the first question wraps in its box (mid-word, as CJK text does); the second is answered
        # with free text on its "Type something" row
        d, did = self.start("form3", 3, ["--form-file", [{"option": 1}, {"text": "teal green"}, {"option": 2}]])
        rc, out = board("press", "CP", "answer", "--expect", "tu_form")
        self.assertEqual(rc, 0, out + tmux("capture-pane", "-p", "-t", "CP"))
        self.assertEqual(self.answers(d), ["公司層", "teal green", "Square"])
        self.assertEqual("".join(self.keys(d)), "13teal green" + "Enter" + "21")
        self.assertEqual(self.dec(did)["status"], "relayed")

    def test_other_question_showing_no_key(self):
        # question 1 was answered in the session: question 2 shows, the board expects question 1.
        # Nothing is sent; the prompt is still open, so the decision goes back to pending (exit 7)
        # and may be answered again.
        d, did = self.start("form3-q2", 3, ["--options", "1,2,1"])
        rc, out = board("press", "CP", "answer", "--expect", "tu_form")
        self.assertEqual(rc, 7, out)
        self.assertIn("question 1 is not the one showing", out)
        time.sleep(0.3)
        self.assertEqual(self.keys(d), [])
        dec = self.dec(did)
        self.assertEqual((dec["status"], dec.get("answer_options"), dec.get("press_not_visible"), dec.get("press_note_code")),
                         ("pending", None, 1, "form_elsewhere"))


def tearDownModule():
    tmux("kill-server")
    shutil.rmtree(SOCKDIR, ignore_errors=True)
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[a for a in sys.argv if a != "-v"])
