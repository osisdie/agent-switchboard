#!/usr/bin/env python3
"""tests/test_resume.py — resuming sessions stuck on a usage limit (lib/resume.py, `board resume`,
the collector's `limit` on session rows, board-serve's POST /api/resume).

tmux is tests/fake_tmux.py on PATH for most tests: scripted screens, every call logged, never a
tmux server. The end-to-end class runs tests/fake_claude_tui.py in a pane of a PRIVATE tmux
server (`tmux -L <test socket> -f /dev/null`, through a wrapper on PATH, TMUX unset) and kills
only that server. Never reads ~/.agent-switchboard, ~/.claude or a real config.

  python3 tests/test_resume.py [-v]
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

TMP = tempfile.mkdtemp(prefix="switchboard-resume-test.")


def remove_tmp(timeout=10.0):
    """Remove TMP once no other process points into it (board-serve's background `board resume`)."""
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


atexit.register(remove_tmp, 3.0)
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
SESS = os.path.join(TMP, "sessions")
BIN = os.path.join(TMP, "bin")
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "CLAUDE_SESSIONS_DIR": SESS,
                   "CLAUDE_JSON": os.path.join(TMP, "claude.json"), "CLAUDE_SETTINGS": os.path.join(TMP, "settings.json"),
                   "ALERT_DESKTOP": "0", "BOARD_PRESS_SETTLE": "0", "BOARD_ACTIONS_AUTO": "0", "BOARD_ACTION_POLL": "0.05"})
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

import fleet  # noqa: E402
import resume  # noqa: E402

BOARD = os.path.join(REPO, "bin", "board")
RULE = "─" * 40
TOP = RULE + " CP ─"
NB = "\u00a0"
HEAD = ["Claude Code v2", "", "❯ fix the flaky upload test", "● Looking at the upload handler first."]
FOOT = "  ⏵⏵ auto mode on (shift+tab to cycle)"
PROMPT = "Quota is available again. Continue where you left off."


def screen(above=(), box="❯" + NB, below=(FOOT,)):
    return "\n".join(HEAD + list(above) + [TOP, box, RULE] + list(below))


LIMIT_MSG = "  ⎿  Usage limit reached · limit resets 3pm (UTC)"
IDLE = screen([LIMIT_MSG])                                               # state: idle
HEALTHY = screen(["● Done: the upload test passes 50 runs in a row."])    # no limit
DIALOG = "\n".join(HEAD + [LIMIT_MSG, " ▔▔▔▔", "   What do you want to do?",
                           "   ❯ 1. Stop and wait for limit to reset",
                           "     2. Wait here, then continue automatically at 3pm",
                           "     3. Upgrade your plan", "", "   Enter to confirm · Esc to cancel"])
WAIT = screen(["  ⎿  Usage limit reached · continuing automatically at 3pm · esc to cancel"], box="❯ continue",
              below=["  Usage limit reached · limit resets 3pm", "  Continuing automatically at 3pm · Esc to cancel"])
CANCELLED = screen([LIMIT_MSG, "  ⎿  Automatic continue cancelled · /rate-limit-options to re-arm"])
READY = screen([LIMIT_MSG], box="❯ continue", below=["  Your usage limit has reset · Press enter to continue"])
TYPED = screen([LIMIT_MSG], box="❯ " + PROMPT)
WORKING = screen([LIMIT_MSG, "❯ " + PROMPT, "✻ Thinking… (3s · esc to interrupt)"],
                 below=[FOOT + " · esc to interrupt"])
OTHER_PICKER = "\n".join(HEAD + ["   Something else entirely", "   ❯ Yes", "     No", "   Enter to confirm · Esc to cancel"])
# Scrollback that mentions the limit, an old wait and the dialog's title, above a healthy box.
OLD_NEWS = screen(["  ⎿  Usage limit reached · continuing automatically at 3pm · esc to cancel",
                   "   What do you want to do?", "❯ go on", "● Done."])
USER_TEXT = screen([LIMIT_MSG], box="❯ my own half-typed idea")
# An AskUserQuestion picker that happens to share the title and have a "Stop" option.
ASK_STOP = "\n".join(HEAD + ["   What do you want to do?", "   ❯ 1. Continue", "     2. Stop", "",
                             "   Enter to select · ↑/↓ to navigate · Esc to cancel"])
# The limit dialog's title and rows, but no limit notice anywhere (someone's own menu).
DIALOG_NO_NOTICE = "\n".join(HEAD + ["   What do you want to do?", "   ❯ 1. Stop and wait for limit to reset",
                                     "     2. Upgrade your plan", "", "   Enter to confirm · Esc to cancel"])
# Assistant prose about limits: never a limit notice.
PROSE = [screen(["● The API enforces a weekly limit; I added a backoff."]),
         screen(["● Done. The rate limiter now logs when the limit resets."]),
         screen(["● Added docs for /rate-limit-options."]),
         screen(["● Usage limit reached is the message the client shows; I", "  matched it in the tests."]),
         screen(["● Bash(grep -rn limit docs)", "  ⎿  docs/x.md: Usage limit reached · limit resets 3pm"]),
         # later paragraphs of a reply: indented, no "●", after a blank line
         screen(["● I changed the classifier.", "", "  Usage limit reached is now matched only at line start."]),
         screen(["● Summary:", "", "  Weekly limit reached screens are handled."]),
         screen(["● The notice text we look for, after this change, is the one that reads", "  usage limit reached · resets 3pm."]),
         screen(["● Two paragraphs.", "", "  The first one.", "", "  You've hit your limit is what the client prints."])]


def run(*args, env=None):
    r = subprocess.run(list(args), capture_output=True, text=True, env=dict(os.environ, **(env or {})), timeout=120)
    return r.returncode, r.stdout, r.stderr


def board(*args, **kw):
    return run(BOARD, *args, **kw)


def write_fleet(extra="", sessions=None):
    sessions = sessions if sessions is not None else "  - code: CP\n    cwd: /w\n  - code: QA\n    cwd: /w\n"
    with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
        fh.write("approvers: [local]\nsession_actions:\n  lock_wait_seconds: 1\n"
                 "limit_resume:\n  confirm_seconds: 1\n  esc_gap_seconds: 0.3\n%s\nsessions:\n%s" % (extra, sessions))


def events(kind="resume"):
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
    with open("/proc/%d/stat" % pid) as fh:
        data = fh.read()
    return str(int(data[data.rindex(")") + 2:].split()[19]))


LIVES = []


class Live:
    """One running session: a live pid (a sleep, child of this test), its registry file and hook state."""

    def __init__(self, code="CP", status="idle", state="done", sid="s-cp", pane="%42", pid=None):
        self.proc = None
        if pid is None:
            self.proc = subprocess.Popen(["sleep", "600"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pid = self.proc.pid
        LIVES.append(self)
        self.pid, self.sid = pid, sid
        self.reg = os.path.join(SESS, "%d.json" % pid)
        self.state = os.path.join(HOME, "state", "%s.json" % sid)
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        self.write(status=status, name=code, tmux="%s:@1.%s" % (code, pane))
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

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()      # only the sleep this test started (its recorded pid)
        if self.proc:
            self.proc.wait()
        for p in (self.reg, self.state):
            if os.path.exists(p):
                os.remove(p)


def close_lives():
    while LIVES:
        LIVES.pop().close()


def script(screens, on_send=None, seq=None, session="CP", panes=None, fail_send=None):
    cfg = {"session": session, "screens": screens, "on_send": on_send or {}, "seq": seq or {},
           "pane_pid": os.getpid(), "panes": panes or {}}
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


def sent_to(pane):
    try:
        with open(LOG) as fh:
            return [ln for ln in fh if ln.startswith("send-keys|-t|%s|" % pane)]
    except OSError:
        return []


def reset_home():
    for n in os.listdir(HOME):
        if n.endswith(".lock") or n in ("events.jsonl", "snapshot.json", "resume-scan.json", "resume-scan.spawned") or n.startswith("resume-esc-"):
            os.remove(os.path.join(HOME, n))


def C(extra=None):
    cfg = fleet.load_fleet()
    if extra:
        cfg["limit_resume"] = dict(cfg.get("limit_resume") or {}, **extra)
    return resume.conf(cfg)


# ----------------------------------------------------------------------------- classify
class ClassifyTest(unittest.TestCase):
    def setUp(self):
        write_fleet()

    def st(self, text, **kw):
        return resume.classify(text.split("\n"), C(kw or None))[0]

    def test_limit_states(self):
        self.assertEqual(self.st(IDLE), "idle")
        self.assertEqual(self.st(DIALOG), "dialog")
        self.assertEqual(self.st(WAIT), "wait")
        self.assertEqual(self.st(READY), "ready")
        self.assertEqual(self.st(CANCELLED), "idle")
        # "Stop" alone (usage-based billing) is the dialog's stop row too
        self.assertEqual(self.st(DIALOG.replace("1. Stop and wait for limit to reset", "1. Stop")), "dialog")

    def test_not_a_limit(self):
        self.assertEqual(self.st(HEALTHY), "none")
        self.assertEqual(self.st(WORKING), "busy")
        self.assertEqual(self.st(OTHER_PICKER), "unknown")
        self.assertEqual(self.st(OLD_NEWS), "none", "old text above an older prompt never counts")
        self.assertEqual(self.st(USER_TEXT), "unknown", "the owner's own typing is never touched")
        self.assertEqual(self.st(TYPED), "unknown")
        self.assertEqual(self.st(""), "unknown")
        self.assertEqual(resume.classify(None, C())[0], "unknown")
        # the dialog's title without one of its rows (another dialog that asks the same) is not it
        other = "\n".join(HEAD + ["   What do you want to do?", "   ❯ 1. Keep going", "     2. Rename it",
                                  "   Enter to confirm · Esc to cancel"])
        self.assertEqual(self.st(other), "unknown")
        # a wait line ABOVE the input box is a transcript notice, not the live wait (below the box)
        self.assertNotEqual(self.st(screen(["  Continuing automatically at 3pm · Esc to cancel"])), "wait")
        # "continue" in the box without a reset hint or a limit message: someone typed it
        self.assertEqual(self.st(screen(["● Done."], box="❯ continue")), "unknown")
        # vim NORMAL mode: Enter would not submit
        self.assertEqual(self.st(screen([LIMIT_MSG], below=["  -- NORMAL --"])), "unknown")

    def test_patterns_are_configurable(self):
        self.assertEqual(self.st(IDLE, limit_regex="nothing will match this"), "none")
        quota = screen(["  ⎿  Quota exhausted for today"])
        self.assertEqual(self.st(quota), "none")
        self.assertEqual(self.st(quota, limit_regex="quota exhausted"), "idle")
        self.assertEqual(self.st(HEALTHY, limit_regex="done"), "none", "a ● paragraph never counts")
        bad = self.st(IDLE, limit_regex="([")             # a broken pattern falls back to the default
        self.assertEqual(bad, "idle")

    def test_not_a_dialog(self):
        self.assertEqual(self.st(ASK_STOP), "unknown", "a picker with a Stop option is not the limit dialog")
        self.assertEqual(self.st(DIALOG_NO_NOTICE), "unknown", "no limit notice: not the limit dialog")
        only_stop = DIALOG.replace("     2. Wait here, then continue automatically at 3pm\n", "").replace("     3. Upgrade your plan\n", "")
        only_stop = only_stop.replace("1. Stop and wait for limit to reset", "1. Stop")
        self.assertEqual(self.st(only_stop), "unknown", "a bare Stop needs a limit-specific row beside it")

    def test_prose_is_not_a_notice(self):
        for scr in PROSE:
            self.assertEqual(self.st(scr), "none", scr.split("\n")[-5])
        # a plain notice line counts only with no "●" before it in the reply (up to a "⎿" line)
        def bare(reply):                                  # a reply that starts right after the echo
            return "\n".join(["Claude Code v2", "", "❯ fix the flaky upload test"] + reply + [TOP, "❯" + NB, RULE, FOOT])
        self.assertEqual(self.st(bare(["You've hit your limit · resets 3pm"])), "idle")
        self.assertEqual(self.st(screen(["You've hit your limit · resets 3pm"])), "none", "after HEAD's ● reply line")
        self.assertEqual(self.st(screen(["● Bash(ls)", "  ⎿  a.txt", "", "You've hit your limit · resets 3pm"])), "idle")
        self.assertEqual(self.st(screen(["● Working on it.", "", "You've hit your limit · resets 3pm"])), "none")
        self.assertEqual(self.st(screen(["You've hit your limit · resets 3pm", "", "● Something after it."])), "none")

    def test_wrapped_prompt_box(self):
        for cut in (26, 30):                                   # at a space, inside a word
            lines = (HEAD + [LIMIT_MSG, TOP, "❯ " + PROMPT[:cut], "  " + PROMPT[cut:], RULE, FOOT])
            box = resume.input_box(lines, C())
            self.assertTrue(resume.same_text(box[2], PROMPT), box)
        self.assertFalse(resume.same_text("Quota is available", PROMPT))

    def test_prompt_validation(self):
        self.assertEqual(resume.prompt_text({"prompt": " Go on. "}), "Go on.")
        for bad in ("", "  ", "x" * 501, "line\nbreak", "esc\x1b[2J", None, 5):
            self.assertIsNone(resume.prompt_text({"prompt": bad}), bad)


# ----------------------------------------------------------------------------- the CLI
class CliTest(unittest.TestCase):
    def setUp(self):
        reset_home()
        write_fleet()

    def tearDown(self):
        close_lives()

    def test_dry_run_sends_nothing(self):
        Live()
        for scr, state, keys in ((IDLE, "idle", "<prompt> Enter"), (DIALOG, "dialog", "Escape"), (WAIT, "wait", "Escape"),
                                 (READY, "ready", "Enter")):
            script([scr])
            rc, out, err = board("resume", "CP", "--dry-run")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("dry-run [%s]" % state, out)
            self.assertIn("would send: %s" % keys, out)
            self.assertEqual(sent(), [])
        ev = events()[-1]
        self.assertEqual((ev["result"], ev["dry_run"], ev["by"]), ("dry-run", True, "cli"))

    def test_not_limited_refuses_with_nothing_sent(self):
        Live()
        for scr in (HEALTHY, WORKING, OTHER_PICKER, USER_TEXT, OLD_NEWS, ASK_STOP, DIALOG_NO_NOTICE, *PROSE):
            script([scr])
            rc, out, err = board("resume", "CP")
            self.assertEqual(rc, 3, out + err)
            self.assertIn("refused", out)
            self.assertEqual(sent(), [], scr)
        ev = events()[-1]
        self.assertEqual((ev["result"], ev["reason_code"], ev["keys"]), ("refused", "not_limited", []))
        rc, out, _ = board("resume", "CP", "--dry-run")
        self.assertEqual(rc, 3, out)

    def test_idle_types_the_prompt(self):
        Live()
        script([IDLE, TYPED, WORKING])
        rc, out, err = board("resume", "CP", "--by", "tester")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("resumed [idle]", out)
        self.assertEqual(sent(), ["-l|--|" + PROMPT, "Enter"])
        ev = events()[-1]
        self.assertEqual((ev["result"], ev["state"], ev["keys"], ev["by"], ev["pane"]),
                         ("resumed", "idle", ["<prompt>", "Enter"], "tester", "%42"))
        self.assertNotIn(PROMPT, json.dumps(ev), "the prompt text itself is not logged")

    def test_custom_prompt_and_trailing_semicolon(self):
        write_fleet("  prompt: 'Back again;'")
        Live()
        typed = screen([LIMIT_MSG], box="❯ Back again;")
        script([IDLE, typed, WORKING])
        rc, out, err = board("resume", "CP")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sent(), ["-l|--|Back again\\;", "Enter"])

    def test_bad_prompt_refused(self):
        write_fleet("  prompt: ''")
        Live()
        script([IDLE])
        rc, out, _ = board("resume", "CP")
        self.assertEqual(rc, 3, out)
        self.assertEqual(sent(), [])
        self.assertEqual(events()[-1]["reason_code"], "config")

    def test_dialog_then_prompt(self):
        Live()
        script([DIALOG, CANCELLED, TYPED, WORKING])
        rc, out, err = board("resume", "CP")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sent(), ["Escape", "-l|--|" + PROMPT, "Enter"])
        self.assertNotIn("1", sent(), "never a row number")

    def test_wait_then_prompt(self):
        Live()
        script([WAIT, CANCELLED, TYPED, WORKING])
        rc, out, err = board("resume", "CP")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sent(), ["Escape", "-l|--|" + PROMPT, "Enter"])

    def test_dialog_wait_prompt_two_escapes_apart(self):
        Live()
        script([DIALOG, WAIT, CANCELLED, TYPED, WORKING])
        t0 = time.time()
        rc, out, err = board("resume", "CP")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sent(), ["Escape", "Escape", "-l|--|" + PROMPT, "Enter"])
        with open(LOG) as fh:
            self.assertTrue(fh.read())
        self.assertGreaterEqual(time.time() - t0, 0.3)

    def test_ready_presses_enter_only(self):
        Live()
        script([READY, WORKING])
        rc, out, err = board("resume", "CP")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sent(), ["Enter"])

    def test_unexpected_screen_after_escape_is_partial(self):
        Live()
        script([DIALOG, OTHER_PICKER])
        rc, out, _ = board("resume", "CP")
        self.assertEqual(rc, 5, out)
        self.assertEqual(sent(), ["Escape"], "nothing after the unexpected screen")
        self.assertEqual(events()[-1]["result"], "partial")

    def test_dialog_then_plain_box_types_nothing(self):
        Live()
        script([DIALOG, HEALTHY])
        rc, out, _ = board("resume", "CP")
        self.assertEqual(rc, 5, out)
        self.assertEqual(sent(), ["Escape"], "an empty box without a limit notice: nothing typed")
        self.assertIn("the screen shows none", out)

    def test_pane_text_is_not_logged(self):
        Live()
        for scr in (DIALOG, IDLE, WAIT, HEALTHY):
            script([scr])
            rc, out, _ = board("resume", "CP", "--dry-run")
        script([DIALOG])
        rc, out, _ = board("resume", "CP", "--dry-run")
        self.assertIn('screen: "1. Stop and wait for limit to reset"', out, "the snippet is for the terminal")
        with open(os.path.join(HOME, "events.jsonl")) as fh:
            raw = fh.read()
        for text in ("Usage limit reached", "3pm", "Stop and wait", "Continuing automatically"):
            self.assertNotIn(text, raw)
        script([""])
        rc, out, _ = board("resume", "CP")
        self.assertEqual((rc, events()[-1]["reason_code"]), (3, "not_limited"), "the pane is empty: not 'pane'")

    def test_escape_gap_across_runs(self):
        import actions
        write_fleet("  esc_gap_seconds: 0.5")
        times, real = [], actions._send  # noqa: SLF001
        actions._send = lambda *a, **k: times.append(time.time()) or True  # noqa: SLF001
        try:
            with open(resume.esc_path(HOME, "CP"), "w") as fh:
                fh.write("%.3f\n" % time.time())          # an earlier run's Escape, just now
            t0 = time.time()
            r = resume.Run(HOME, fleet.load_fleet(), {"code": "CP", "tmux_pane": "%42"})
            r.escape()
            self.assertGreaterEqual(times[0] - t0, 0.4)
            r2 = resume.Run(HOME, fleet.load_fleet(), {"code": "CP", "tmux_pane": "%42"})
            r2.escape()                                       # a second run right after
            self.assertGreaterEqual(times[1] - times[0], 0.45)
            self.assertEqual(os.stat(resume.esc_path(HOME, "CP")).st_mode & 0o777, 0o600)
        finally:
            actions._send = real  # noqa: SLF001

    def test_dialog_that_stays_open_is_partial(self):
        Live()
        script([DIALOG])
        rc, out, _ = board("resume", "CP")
        self.assertEqual(rc, 5, out)
        self.assertEqual(sent(), ["Escape"])
        self.assertIn("still there", out)

    def test_typed_text_not_shown_no_enter(self):
        Live()
        script([IDLE, IDLE])
        rc, out, _ = board("resume", "CP")
        self.assertEqual(rc, 5, out)
        self.assertEqual(sent(), ["-l|--|" + PROMPT], "never Enter on a guess")

    def test_still_limited_after_enter(self):
        Live()
        script([IDLE, TYPED, DIALOG])
        rc, out, _ = board("resume", "CP")
        self.assertEqual(rc, 6, out)
        self.assertIn("limited", out)
        self.assertEqual(sent(), ["-l|--|" + PROMPT, "Enter"])

    def test_not_confirmed(self):
        Live()
        script([IDLE, TYPED, TYPED])
        rc, out, _ = board("resume", "CP")
        self.assertEqual(rc, 6, out)
        self.assertIn("unconfirmed", out)

    def test_echo_counts_as_submitted(self):
        Live()
        done = screen([LIMIT_MSG, "❯ " + PROMPT, "● Picking up where I left off."])
        script([IDLE, TYPED, done])
        rc, out, _ = board("resume", "CP")
        self.assertEqual(rc, 0, out)

    def test_guards(self):
        # another tmux session's pane: nothing sent
        Live()
        script([IDLE], session="OTHER")
        rc, out, _ = board("resume", "CP")
        self.assertEqual(rc, 3, out)
        self.assertEqual(sent(), [])
        self.assertEqual(events()[-1]["reason_code"], "pane")
        close_lives()
        # not running
        script([IDLE])
        rc, out, _ = board("resume", "CP")
        self.assertEqual((rc, events()[-1]["reason_code"]), (3, "offline"), out)
        # usage errors
        self.assertEqual(board("resume")[0], 2)
        self.assertEqual(board("resume", "NOPE")[0], 2)
        self.assertEqual(board("resume", "CP", "--all")[0], 2)
        self.assertEqual(board("resume", "a b")[0], 2)
        self.assertEqual(board("resume", "CP", "--by", "bad who")[0], 2)
        self.assertEqual(board("resume", "CP", "CP")[0], 2)
        self.assertEqual(board("resume", "--scan", "CP")[0], 2)
        self.assertEqual(sent(), [])

    def test_lock_held_refuses(self):
        import actions
        Live()
        script([IDLE])
        lock = actions.SessionLock(HOME, "CP")
        self.assertTrue(lock.acquire(0))
        try:
            rc, out, _ = board("resume", "CP")
        finally:
            lock.release()
        self.assertEqual(rc, 3, out)
        self.assertEqual(events()[-1]["reason_code"], "locked")
        self.assertEqual(sent(), [])

    def test_all_only_touches_limited_sessions(self):
        Live()
        Live(code="QA", sid="s-qa", pane="%43")
        script([IDLE, TYPED, WORKING], panes={"%43": {"session": "QA", "screen": HEALTHY}})
        rc, out, err = board("resume", "--all")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("CP: resumed", out)
        self.assertIn("QA: skipped", out)
        self.assertEqual(sent_to("%43"), [])
        self.assertEqual(sent(), ["-l|--|" + PROMPT, "Enter"])
        # nothing limited anywhere: exit 0, nothing sent
        script([HEALTHY], panes={"%43": {"session": "QA", "screen": HEALTHY}})
        rc, out, _ = board("resume", "--all", "--json")
        self.assertEqual(rc, 0, out)
        self.assertEqual({r["code"]: r["result"] for r in json.loads(out)}, {"CP": "skipped", "QA": "skipped"})
        self.assertEqual(sent(), [])

    def test_several_codes(self):
        Live()
        Live(code="QA", sid="s-qa", pane="%43")
        script([IDLE, TYPED, WORKING], panes={"%43": {"session": "QA", "screen": HEALTHY}})
        rc, out, _ = board("resume", "CP", "QA")
        self.assertEqual(rc, 3, out)                     # QA refused (not at a limit), CP resumed
        self.assertIn("CP: resumed", out)
        self.assertIn("QA: refused", out)
        self.assertEqual(sent_to("%43"), [])

    def test_scan_merge_keeps_newer_entries(self):
        import actions
        reset_home()
        resume.remember(HOME, {"code": "CP", "session_id": "s-cp"}, "idle", now=2000)
        resume.scan(HOME, fleet.load_fleet(), [], now=1000)          # a look that started earlier
        self.assertEqual(resume.load_scan(HOME)["sessions"]["CP"]["state"], "idle")
        resume.remember(HOME, {"code": "QA", "session_id": "s-qa"}, "wait", now=500)
        lock = actions.SessionLock(HOME, "QA")
        self.assertTrue(lock.acquire(0))
        try:
            doc = resume.scan(HOME, fleet.load_fleet(), [{"code": "QA"}], now=3000)
        finally:
            lock.release()
        self.assertEqual(doc["sessions"]["QA"]["state"], "wait", "a busy session keeps its last look")
        self.assertNotIn("CP", resume.scan(HOME, fleet.load_fleet(), [], now=4000)["sessions"])

    def test_scan(self):
        Live()
        script([WAIT])
        rc, out, _ = board("resume", "--scan", "--json")
        self.assertEqual((rc, json.loads(out)), (0, {"CP": "wait"}))
        self.assertEqual(sent(), [])


# ----------------------------------------------------------------------------- collector
class CollectorTest(unittest.TestCase):
    def setUp(self):
        reset_home()
        write_fleet("  scan_seconds: 1")

    def tearDown(self):
        close_lives()

    def collect(self, *flags):
        rc, out, err = run(sys.executable, os.path.join(REPO, "lib", "collect.py"), "--no-notify", *flags)
        self.assertEqual(rc, 0, out + err)
        with open(os.path.join(HOME, "snapshot.json")) as fh:
            return {s["code"]: s for s in json.load(fh)["sessions"]}

    def scanned(self, after=0, timeout=15):
        """Runs the looping collector's step until the background look (started by it) has
        finished after AFTER (ms), then once more so the snapshot carries it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.collect("--scan-limits")
            if int(resume.load_scan(HOME).get("at") or 0) > after and not resume.scan_running(HOME):
                return self.collect()
            time.sleep(0.2)
        self.fail("the background look did not finish")

    def captures(self):
        if not os.path.exists(LOG):
            return []
        with open(LOG) as fh:
            return [ln for ln in fh if ln.startswith("capture-pane")]

    def test_limit_on_the_row(self):
        Live()
        script([DIALOG])
        rows = self.collect()
        self.assertNotIn("limit", rows["CP"], "--once without --scan-limits never reads panes")
        self.assertEqual(self.captures(), [])
        rows = self.scanned()
        self.assertEqual(rows["CP"]["limit"]["state"], "dialog")
        since = rows["CP"]["limit"]["since"]
        self.assertEqual(sent(), [], "the collector only looks")
        self.assertEqual(os.stat(os.path.join(HOME, "resume-scan.json")).st_mode & 0o777, 0o600)
        self.assertEqual(self.collect()["CP"]["limit"], {"state": "dialog", "since": since})
        time.sleep(1.1)
        rows = self.scanned(after=resume.load_scan(HOME)["at"])     # same state: `since` stays
        self.assertEqual(rows["CP"]["limit"]["since"], since)
        script([HEALTHY])
        time.sleep(1.1)
        rows = self.scanned(after=resume.load_scan(HOME)["at"])
        self.assertNotIn("limit", rows["CP"])

    def test_the_look_never_blocks_the_collector(self):
        Live()
        slow = os.path.join(TMP, "slowbin")
        os.makedirs(slow, exist_ok=True)
        with open(os.path.join(slow, "tmux"), "w") as fh:     # every tmux call takes 3 s
            fh.write('#!/bin/sh\nsleep 3\nexec %s %s "$@"\n' % (sys.executable, os.path.join(REPO, "tests", "fake_tmux.py")))
        os.chmod(os.path.join(slow, "tmux"), 0o755)
        script([DIALOG])
        self.collect()
        t0 = time.time()
        rc, out, err = run(sys.executable, os.path.join(REPO, "lib", "collect.py"), "--no-notify", "--scan-limits",
                           env={"PATH": slow + os.pathsep + os.environ["PATH"]})
        self.assertEqual(rc, 0, out + err)
        self.assertLess(time.time() - t0, 6, "the collector returns while the look runs in the background")
        deadline = time.time() + 30
        while time.time() < deadline and (resume.scan_running(HOME) or not resume.load_scan(HOME).get("at")):
            time.sleep(0.2)

    def test_failing_look_is_started_once_per_period(self):
        started = []
        real = resume.spawn_scan
        resume.spawn_scan = lambda home: started.append(home) or True   # a look that never finishes
        try:
            cfg = fleet.load_fleet()                                     # scan_seconds: 1
            for now in (10000, 10100, 10900):                            # "at" never advances
                resume.annotate([], cfg, HOME, now, do_scan=True)
            self.assertEqual(len(started), 1, "one start per scan_seconds, not one per collector run")
            resume.annotate([], cfg, HOME, 11000, do_scan=True)
            self.assertEqual(len(started), 2)
            self.assertEqual(os.stat(os.path.join(HOME, resume.SCAN_SPAWNED)).st_mode & 0o777, 0o600)
        finally:
            resume.spawn_scan = real
        # for real: a look that fails (resume-scan.json cannot be written) is not restarted every run
        os.remove(os.path.join(HOME, resume.SCAN_SPAWNED))
        os.makedirs(os.path.join(HOME, resume.SCAN_FILE))
        try:
            Live()
            script([DIALOG])
            for _ in range(4):
                self.collect("--scan-limits")
            time.sleep(1.5)
            self.assertLessEqual(len(self.captures()), 2, self.captures())
        finally:
            deadline = time.time() + 10
            while time.time() < deadline and resume.scan_running(HOME):
                time.sleep(0.1)
            os.rmdir(os.path.join(HOME, resume.SCAN_FILE))

    def test_scan_off(self):
        write_fleet("  scan_seconds: 0")
        Live()
        script([DIALOG])
        self.collect("--scan-limits")
        time.sleep(1)
        self.assertNotIn("limit", self.collect()["CP"])
        self.assertEqual(self.captures(), [])

    def test_resume_updates_the_row(self):
        Live()
        script([IDLE])
        self.assertEqual(self.scanned()["CP"]["limit"]["state"], "idle")
        script([IDLE, TYPED, WORKING])
        self.assertEqual(board("resume", "CP")[0], 0)
        self.assertNotIn("limit", self.collect()["CP"])

    def test_refusal_clears_a_stale_look(self):
        Live()
        script([IDLE])
        self.assertEqual(self.scanned()["CP"]["limit"]["state"], "idle")
        script([HEALTHY])                          # resumed by hand in the meantime
        self.assertEqual(board("resume", "CP")[0], 3)
        self.assertNotIn("limit", self.collect()["CP"])
        self.assertEqual(sent(), [])


# ----------------------------------------------------------------------------- board-serve
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


SERVE_ERR = open(os.path.join(TMP, "serve.err"), "a")  # noqa: SIM115 - closed at exit


class ServeTest(unittest.TestCase):
    srv = None

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        write_fleet("serve:\n  port: %d" % cls.port)
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
    def tearDownClass(cls):
        if cls.srv:
            cls.srv.terminate()       # the server this class started (its recorded pid)
            cls.srv.wait()

    def tearDown(self):
        close_lives()

    def post(self, body, headers=None, path="/api/resume"):
        base = "http://127.0.0.1:%d" % self.port
        h = {"Content-Type": "application/json", "X-Board": "1", "Origin": base}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def wait_event(self, n, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            evs = events()
            if len(evs) >= n:
                return evs
            time.sleep(0.2)
        return events()

    def test_route(self):
        reset_home()
        cases = [("no X-Board", {"code": "CP"}, {"X-Board": None}, 403),
                 ("cross origin", {"code": "CP"}, {"Origin": "https://evil.example"}, 403),
                 ("unknown session", {"code": "NOPE"}, None, 400),
                 ("bad code", {"code": "a b"}, None, 400),
                 ("not a string", {"code": ["CP"]}, None, 400),
                 ("missing", {}, None, 400),
                 ("all is not accepted", {"code": "all"}, None, 400),
                 ("all in codes", {"codes": ["all"]}, None, 400),
                 ("both", {"code": "CP", "codes": ["CP"]}, None, 400),
                 ("empty codes", {"codes": []}, None, 400),
                 ("repeated", {"codes": ["CP", "CP"]}, None, 400),
                 ("an option", {"codes": ["--all"]}, None, 400),
                 ("too many", {"codes": ["CP"] + ["S%d" % i for i in range(64)]}, None, 400),
                 ("codes not a list", {"codes": "CP"}, None, 400)]
        for name, body, hdrs, want in cases:
            with self.subTest(name):
                st, js = self.post(body, hdrs)
                self.assertEqual(st, want, (name, js))
        self.assertEqual(events(), [], "nothing ran for a refused request")
        Live()
        script([IDLE, TYPED, WORKING])
        st, js = self.post({"code": "CP"})
        self.assertEqual((st, js["status"]), (200, "started"), js)
        ev = self.wait_event(1)[-1]
        self.assertEqual((ev["result"], ev["by"]), ("resumed", "board:local"), ev)
        self.assertEqual(sent(), ["-l|--|" + PROMPT, "Enter"])
        audit = [e for e in events("serve") if e.get("action") == "resume"]
        self.assertEqual(audit[-1]["result"], "ok")
        # exactly the confirmed codes: QA is at a limit too, but only CP was named
        Live(code="QA", sid="s-qa", pane="%43")
        script([IDLE, TYPED, WORKING], panes={"%43": {"session": "QA", "screen": IDLE}})
        n = len(events())
        st, js = self.post({"codes": ["CP"]})
        self.assertEqual((st, js["codes"]), (200, ["CP"]), js)
        evs = self.wait_event(n + 1)
        self.assertEqual([e["code"] for e in evs[n:]], ["CP"])
        self.assertEqual(sent_to("%43"), [])
        self.assertEqual([e for e in events("serve") if e.get("action") == "resume"][-1]["codes"], ["CP"])

    def test_non_owner_403(self):
        # an approver who is not an owner: 403 not_owner, audited, nothing run
        with open(os.path.join(CFG, "fleet.yaml")) as fh:
            body = fh.read()
        try:
            with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
                fh.write("owners: [someone@example.com]\n" + body)
            reset_home()
            st, js = self.post({"code": "CP"})
            self.assertEqual((st, js["error"]), (403, "not_owner"))
            refused = [e for e in events("serve") if e.get("action") == "resume"]
            self.assertEqual(refused[-1]["result"], "refused")
            self.assertEqual(events(), [])
        finally:
            with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
                fh.write(body)


# ----------------------------------------------------------------------------- end to end
TMUX_REAL = shutil.which("tmux", path=os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if p != BIN))


@unittest.skipUnless(TMUX_REAL, "tmux is not installed")
class PrivateTmuxTest(unittest.TestCase):
    """A fake Claude screen in a pane of a private tmux server; `board resume` through real tmux."""

    @classmethod
    def setUpClass(cls):
        cls.sock = "resume-test"
        cls.bin = os.path.join(TMP, "realbin")
        os.makedirs(cls.bin, exist_ok=True)
        with open(os.path.join(cls.bin, "tmux"), "w") as fh:
            # the socket lives under TMP (TMUX_TMPDIR), so nothing is left behind in the system's tmux dir
            fh.write('#!/bin/sh\nunset TMUX TMUX_PANE\nTMUX_TMPDIR=%s\nexport TMUX_TMPDIR\nexec %s -L %s -f /dev/null "$@"\n'
                     % (os.path.join(TMP, "tmuxsock"), TMUX_REAL, cls.sock))
        os.makedirs(os.path.join(TMP, "tmuxsock"), mode=0o700, exist_ok=True)
        os.chmod(os.path.join(cls.bin, "tmux"), 0o755)
        cls.env = {"PATH": cls.bin + os.pathsep + os.environ["PATH"].replace(BIN + os.pathsep, ""),
                   "BOARD_PRESS_SETTLE": "0.3", "BOARD_ACTION_POLL": "0.2"}

    @classmethod
    def tearDownClass(cls):
        subprocess.run([os.path.join(cls.bin, "tmux"), "kill-server"], capture_output=True, check=False)

    def setUp(self):
        reset_home()
        write_fleet()
        subprocess.run([os.path.join(self.bin, "tmux"), "kill-server"], capture_output=True, check=False)

    def tearDown(self):
        close_lives()

    def tmux(self, *args):
        return subprocess.run([os.path.join(self.bin, "tmux")] + list(args), capture_output=True, text=True,
                              check=False, env=dict(os.environ, PATH=self.env["PATH"])).stdout.strip()

    def start(self, state):
        d = tempfile.mkdtemp(dir=TMP)
        cmd = "%s %s %s %s" % (sys.executable, os.path.join(REPO, "tests", "fake_claude_tui.py"), d, state)
        for _ in range(3):   # a server that is still shutting down (kill-server) refuses new-session
            self.tmux("new-session", "-d", "-s", "CP", "-x", "120", "-y", "40", cmd)
            for _ in range(100):
                if os.path.exists(os.path.join(d, "pid")) and "Claude Code" in self.tmux("capture-pane", "-p", "-t", "CP"):
                    break
                time.sleep(0.05)
            else:
                time.sleep(0.5)
                continue
            break
        with open(os.path.join(d, "pid")) as fh:
            pid = int(fh.read())
        pane = self.tmux("display-message", "-p", "-t", "CP", "#{pane_id}")
        Live(pid=pid, pane=pane)
        return d

    def keys(self, d):
        try:
            with open(os.path.join(d, "keys")) as fh:
                return [json.loads(ln) for ln in fh]
        except OSError:
            return []

    def screen(self):
        return self.tmux("capture-pane", "-p", "-t", "CP")

    def test_dialog_to_working(self):
        d = self.start("dialog")
        rc, out, err = board("resume", "CP", env=self.env)
        self.assertEqual(rc, 0, out + err + self.screen())
        ks = self.keys(d)
        esc = [k for k in ks if k["key"] == "Escape"]
        self.assertEqual(len(esc), 2, ks)                          # dialog, then the armed wait
        self.assertGreaterEqual(esc[1]["t"] - esc[0]["t"], 0.3)
        self.assertEqual("".join(k["key"] for k in ks if len(k["key"]) == 1), PROMPT)
        self.assertEqual(ks[-1]["key"], "Enter")
        self.assertIn("esc to interrupt", self.screen())

    def test_ready_and_healthy(self):
        d = self.start("ready")
        rc, out, err = board("resume", "CP", env=self.env)
        self.assertEqual(rc, 0, out + err + self.screen())
        self.assertEqual([k["key"] for k in self.keys(d)], ["Enter"])
        self.tmux("kill-server")
        close_lives()
        d = self.start("healthy")
        rc, out, _ = board("resume", "CP", env=self.env)
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.keys(d), [], "nothing reaches a healthy session")


def tearDownModule():
    close_lives()
    SERVE_ERR.close()
    remove_tmp()


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[a for a in sys.argv if a != "-v"])
