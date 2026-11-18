#!/usr/bin/env python3
"""tests/test_press_multiselect.py — `board press` on multi-select questions (AskUserQuestion with
multiSelect: true), alone and inside a form, through a real tmux: tests/fake_claude_tui.py (states
msel-NAME...) draws them as Claude Code 2.1.32x does. The drawing is first checked line for line
against real captures (tests/fixtures/multiselect/*.txt, recorded from a scratch session); then
press reads the checkboxes from the screen, toggles only the boxes that differ from the answer (a
digit toggles a box, the cursor stays), checks every flip, moves on with Right and submits on the
review screen, whose "→ A, B" lists the boxes in the order they were checked.

The pane runs in a PRIVATE tmux server (`tmux -L <test socket> -f /dev/null`, its socket under the
test's temporary directory, TMUX unset); only that server is killed. Never reads
~/.agent-switchboard, ~/.claude or a real config. Skipped without tmux.

  python3 tests/test_press_multiselect.py [-v]
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
FIX = os.path.join(REPO, "tests", "fixtures", "multiselect")
TMP = tempfile.mkdtemp(prefix="switchboard-press-msel.")
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
SOCKDIR = os.path.join(TMP, "s") if len(TMP) < 60 else tempfile.mkdtemp(prefix="sbpm.", dir="/tmp")
os.makedirs(SOCKDIR, mode=0o700, exist_ok=True)
if TMUX_REAL:
    with open(os.path.join(BIN, "tmux"), "w") as fh:
        fh.write('#!/bin/sh\nunset TMUX TMUX_PANE\nTMUX_TMPDIR=%s\nexport TMUX_TMPDIR\nexec %s -u -L press-msel-test -f /dev/null "$@"\n'
                 % (SOCKDIR, TMUX_REAL))
    os.chmod(os.path.join(BIN, "tmux"), 0o755)
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]
BOARD = os.path.join(REPO, "bin", "board")
HOOK = os.path.join(REPO, "hooks", "board-hook.sh")
sys.path.insert(0, os.path.join(REPO, "tests"))
from fake_claude_tui import MS_FORMS, Tui  # noqa: E402


def tmux(*args):
    return subprocess.run([os.path.join(BIN, "tmux")] + list(args), capture_output=True, text=True, check=False).stdout


def board(*args, env=None):
    r = subprocess.run([BOARD] + list(args), capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, **(env or {})))
    return r.returncode, r.stdout + r.stderr


def proc_start(pid):
    with open("/proc/%d/stat" % pid) as fh:
        data = fh.read()
    return str(int(data[data.rindex(")") + 2:].split()[19]))


def fixture(name):
    with open(os.path.join(FIX, name + ".txt"), encoding="utf-8") as fh:
        return fh.read().rstrip("\n").split("\n")


class Drawing(unittest.TestCase):
    """The fake TUI draws exactly what the real one showed (tests/fixtures/multiselect)."""

    def tui(self, state, keys=()):
        t = Tui(TMP, state)
        t.ms_init()
        for k in keys:
            t.ms_key(k)
        return t.ms_lines()

    def test_matches_real_captures(self):
        for fx, state, keys in (
                ("single-question", "msel-fruits", ()),
                ("single-toggled-2", "msel-fruits", ("2",)),            # a digit toggles, the cursor stays on 1
                ("single-review", "msel-fruits", ("1", "3", "Right")),
                ("single-review-toggle-order", "msel-fruits", ("4", "2", "Right")),   # "→ Grape, Banana"
                ("single-review-none", "msel-fruits", ("Right",)),       # nothing checked: not listed
                ("form-q1", "msel-pizza", ()),
                ("form-q2", "msel-pizza", ("2", "3", "Right")),
                ("form-q3", "msel-pizza", ("2", "3", "Right", "2")),
                ("form-review", "msel-pizza", ("2", "3", "Right", "2", "2", "Right")),
                ("form-review-unanswered", "msel-pizza", ("2", "3", "Right", "2", "Right")),
                ("boxed-question", "msel-checks-w60", ()),
                ("boxed-review", "msel-checks-w60", ("2", "Tab")),
                ("boxed-review-wrapped", "msel-checks-w60", ("1", "3", "Right"))):   # wraps 3 columns in
            with self.subTest(fx):
                self.assertEqual(self.tui(state, keys), fixture(fx))

    def test_posttooluse_fixtures(self):
        # how the session reports a multi-select answer: labels joined by ", " in the order the
        # boxes were checked; "" (boxes checked, then cleared) or no entry at all (never touched)
        def answers(name):
            with open(os.path.join(FIX, name + ".json"), encoding="utf-8") as fh:
                return json.load(fh)["tool_response"]["answers"]
        self.assertEqual(answers("posttooluse-single"), {MS_FORMS["fruits"][0]["question"]: "Cherry, Apple"})
        self.assertEqual(list(answers("posttooluse-form-cleared").values()), ["Olives, Peppers", "Thick", ""])
        self.assertEqual(list(answers("posttooluse-form-untouched").values()), ["Onions, Olives", "Thin"])


@unittest.skipUnless(TMUX_REAL, "tmux is not installed")
class Press(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(HOME, ignore_errors=True)
        os.makedirs(HOME)
        for f in os.listdir(SESS):
            os.unlink(os.path.join(SESS, f))
        tmux("kill-server")

    def tearDown(self):
        tmux("kill-server")   # only the private server of this test

    def start(self, state, form, rows=45, cols=100, env=None):
        """The fake TUI in STATE, the hook told about MS_FORMS[FORM]. Returns (dir, decision id)."""
        with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
            fh.write("approvers: [local]\npress:\n  tui_command_regex: ''\nsessions:\n  - code: CP\n    cwd: /w\n")
        d = tempfile.mkdtemp(dir=TMP)
        cmd = "%s %s %s %s" % (sys.executable, os.path.join(REPO, "tests", "fake_claude_tui.py"), d, state)
        if env:   # the fake TUI's drawing options (FAKE_TUI_*), plain words only
            cmd = "env %s %s" % (" ".join("%s='%s'" % kv for kv in sorted(env.items())), cmd)
        for _ in range(3):   # a server still shutting down refuses new-session
            tmux("new-session", "-d", "-s", "CP", "-x", str(cols), "-y", str(rows), cmd)
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
        for ev in ({"hook_event_name": "PreToolUse", "tool_use_id": "tu_ms"}, {"hook_event_name": "PermissionRequest"}):
            ev.update({"session_id": sid, "permission_mode": "default", "tool_name": "AskUserQuestion",
                       "tool_input": {"questions": MS_FORMS[form]}})
            subprocess.run([HOOK], input=json.dumps(ev), text=True, capture_output=True, check=True,
                           env=dict(os.environ, TMUX_PANE=pane))
        dec = [x for x in (json.load(open(os.path.join(HOME, "decisions", f))) for f in os.listdir(os.path.join(HOME, "decisions")))
               if x.get("tool_use_id") == "tu_ms"][0]
        self.assertTrue(dec["pressable"], dec)
        return d, dec["id"]

    def answer(self, did, entries):
        ff = os.path.join(TMP, "form.json")
        with open(ff, "w") as fh:
            json.dump(entries, fh)
        rc, out = board("answer", did, "--form-file", ff, "--by", "local")
        self.assertEqual(rc, 0, out)

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

    def press(self, **env):
        return board("press", "CP", "answer", "--expect", "tu_ms", env=env)

    def test_single_question(self):
        d, did = self.start("msel-fruits", "fruits")
        self.answer(did, [{"question_index": 0, "labels": ["Cherry", "Apple"]}])
        self.assertEqual(self.dec(did)["answer_options"], [[1, 3]])
        rc, out = self.press()
        self.assertEqual(rc, 0, out + tmux("capture-pane", "-p", "-t", "CP"))
        self.assertEqual(self.keys(d), ["1", "3", "Right", "1"])
        self.assertEqual(self.answers(d), ["Apple, Cherry"])
        self.assertEqual(self.dec(did)["status"], "relayed")

    def test_human_checked_boxes_are_read_and_only_differences_toggled(self):
        # Banana and Grape were checked in the pane (Grape first); the answer is Banana + Cherry:
        # only Cherry (on) and Grape (off) are toggled, and the review lists "Banana, Cherry"
        d, did = self.start("msel-fruits-pre42", "fruits")
        self.answer(did, [{"labels": ["Banana", "Cherry"]}])
        rc, out = self.press()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.keys(d), ["3", "4", "Right", "1"])
        self.assertEqual(self.answers(d), ["Banana, Cherry"])

    def test_review_in_checked_order_compares_as_a_set(self):
        # Grape checked in the pane first, Banana by the press: the review says "Grape, Banana"
        d, did = self.start("msel-fruits-pre4", "fruits")
        self.answer(did, [{"labels": ["Banana", "Grape"]}])
        rc, out = self.press()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.keys(d), ["2", "Right", "1"])
        self.assertEqual(self.answers(d), ["Grape, Banana"])

    def test_nothing_to_toggle(self):
        d, did = self.start("msel-fruits-pre31", "fruits")
        self.answer(did, [{"labels": ["Apple", "Cherry"]}])
        rc, out = self.press()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.keys(d), ["Right", "1"])
        self.assertEqual(self.answers(d), ["Cherry, Apple"])

    def test_empty_selection(self):
        # a box checked in the pane is cleared; the review lists nothing and still submits
        d, did = self.start("msel-fruits-pre2", "fruits")
        self.answer(did, [{"labels": []}])
        self.assertEqual(self.dec(did)["answer"], "Fruits: (none)")
        rc, out = self.press()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.keys(d), ["2", "Right", "1"])
        self.assertEqual(self.answers(d), [""])

    def test_mixed_form(self):
        d, did = self.start("msel-pizza", "pizza")
        self.answer(did, [{"labels": ["Onions", "Olives"]}, {"option": 1}, {"labels": []}])
        rc, out = self.press()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.keys(d), ["2", "4", "Right", "1", "Right", "1"])
        self.assertEqual(self.answers(d), ["Olives, Onions", "Thin", ""])
        self.assertEqual(self.dec(did)["status"], "relayed")

    def test_boxed_question_with_wrapped_label(self):
        d, did = self.start("msel-checks-w60", "checks", cols=60)
        label = MS_FORMS["checks"][0]["options"][0]["label"]
        self.answer(did, [{"labels": [label, "Docs build"]}])
        rc, out = self.press()
        self.assertEqual(rc, 0, out + tmux("capture-pane", "-p", "-t", "CP"))
        self.assertEqual(self.keys(d), ["1", "3", "Right", "1"])
        self.assertEqual(self.answers(d), [label + ", Docs build"])

    def test_labels_holding_commas(self):
        d, did = self.start("msel-commas", "commas")
        self.answer(did, [{"labels": ["East", "North, East"]}])
        rc, out = self.press()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.keys(d), ["1", "3", "Right", "1"])
        self.assertEqual(self.answers(d), ["North, East, East"])

    def test_box_that_does_not_flip_stops_before_submit(self):
        # digit 3 is ignored by the TUI: Apple was toggled, Cherry never flips -> exit 5, partial,
        # nothing more sent (no Right, no submit)
        d, did = self.start("msel-fruits-stuck3", "fruits")
        self.answer(did, [{"labels": ["Apple", "Cherry"]}])
        rc, out = self.press(BOARD_PRESS_TRIES="5", BOARD_PRESS_SETTLE="0.1")
        self.assertEqual(rc, 5, out)
        self.assertIn("box 3 did not flip", out)
        time.sleep(0.3)
        self.assertEqual(self.keys(d), ["1", "3"])
        self.assertIsNone(self.answers(d))
        dec = self.dec(did)
        self.assertEqual((dec["status"], dec.get("status_reason")), ("stale", "partial"))

    def test_type_something_focused_sends_nothing(self):
        # a digit with "Type something" focused would be typed into it: refused before any key
        d, did = self.start("msel-fruits-tsfocus", "fruits")
        self.answer(did, [{"labels": ["Apple"]}])
        rc, out = self.press()
        self.assertEqual(rc, 3, out)
        self.assertIn("Type something", out)
        time.sleep(0.3)
        self.assertEqual(self.keys(d), [])
        self.assertEqual(self.dec(did)["status"], "stale")

    def test_next_key_must_be_right_or_tab(self):
        d, did = self.start("msel-fruits", "fruits")
        with open(os.path.join(CFG, "fleet.yaml"), "a") as fh:
            fh.write("")
        with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
            fh.write("approvers: [local]\npress:\n  tui_command_regex: ''\n  next_key: Enter\nsessions:\n  - code: CP\n    cwd: /w\n")
        self.answer(did, [{"labels": ["Apple"]}])
        rc, out = self.press()
        self.assertEqual(rc, 3, out)
        self.assertIn("next_key", out)
        time.sleep(0.3)
        self.assertEqual(self.keys(d), [])

    def test_labelled_rule(self):
        # Claude Code 2.1.327: "───── CP ─" under the question screens and the review screen
        for state, form, ans, keys, want in (
                ("msel-fruits", "fruits", [{"labels": ["Apple", "Cherry"]}], ["1", "3", "Right", "1"], ["Apple, Cherry"]),
                ("msel-pizza", "pizza", [{"labels": ["Olives"]}, {"option": 2}, {"labels": ["Soup"]}],
                 ["2", "Right", "2", "3", "Right", "1"], ["Olives", "Thick", "Soup"])):
            with self.subTest(state):
                self.setUp()
                d, did = self.start(state, form, env={"FAKE_TUI_RULE_LABEL": "CP"})
                self.answer(did, ans)
                rc, out = self.press()
                self.assertEqual(rc, 0, out + tmux("capture-pane", "-p", "-t", "CP"))
                self.assertEqual(self.keys(d), keys)
                self.assertEqual(self.answers(d), want)

    def test_labelled_rule_with_another_name(self):
        d, did = self.start("msel-fruits", "fruits", env={"FAKE_TUI_RULE_LABEL": "OTHER"})
        self.answer(did, [{"labels": ["Apple"]}])
        rc, out = self.press()
        self.assertEqual(rc, 5, out)
        time.sleep(0.3)
        self.assertEqual(self.keys(d), ["1", "Right"])
        self.assertIsNone(self.answers(d))

    def test_review_three_columns_in(self):
        # rows right under the answers, 3 columns in: not part of "→ Apple, Cherry"; a line 3
        # columns in under it is (and then the boxes read otherwise: nothing submitted)
        d, did = self.start("msel-fruits", "fruits", env={"FAKE_TUI_REVIEW_TIGHT": "1"})
        self.answer(did, [{"labels": ["Apple", "Cherry"]}])
        rc, out = self.press()
        self.assertEqual(rc, 0, out + tmux("capture-pane", "-p", "-t", "CP"))
        self.assertEqual(self.answers(d), ["Apple, Cherry"])
        self.setUp()
        d, did = self.start("msel-fruits", "fruits", env={"FAKE_TUI_REVIEW_EXTRA": "   Banana"})
        self.answer(did, [{"labels": ["Apple", "Cherry"]}])
        rc, out = self.press()
        self.assertEqual(rc, 5, out)
        self.assertIn("other boxes for question 1", out)
        time.sleep(0.3)
        self.assertEqual(self.keys(d), ["1", "3", "Right"])
        self.assertIsNone(self.answers(d))

    def test_plain_answer_is_not_pressed(self):
        # an answer that is not labels (a [BOARD decision] message instead): press refuses, exit 4
        d, did = self.start("msel-fruits", "fruits")
        rc, out = board("answer", did, "Apple", "--by", "local")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.dec(did)["relay"], "operator")
        rc, out = self.press()
        self.assertEqual(rc, 4, out)
        self.assertEqual(self.keys(d), [])


def tearDownModule():
    tmux("kill-server")
    shutil.rmtree(SOCKDIR, ignore_errors=True)
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[a for a in sys.argv if a != "-v"])
