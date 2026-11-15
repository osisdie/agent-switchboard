#!/usr/bin/env python3
"""tests/test_cards.py — card extras (lib/cards.py, the collector's cards.json and board-serve's
per-viewer view of it): teammates and their pane titles, the statusline meter, the completed-tool
ring, the transcript tail (doing / prompt), ahead/behind, time in state, the per-run caps, and that
a sentinel secret planted in a transcript never reaches cards.json, its cache, the snapshot or
/api/state. Fixture transcripts, statusline files and /proc only; never reads ~/.claude.

  python3 tests/test_cards.py [-v]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(REPO, "tests"))

TMP = tempfile.mkdtemp(prefix="switchboard-cards-test.")
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
SESS = os.path.join(TMP, "sessions")
PROJ = os.path.join(TMP, "projects")
PROC = os.path.join(TMP, "proc")
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "CLAUDE_SESSIONS_DIR": SESS,
                   "CLAUDE_PROJECTS_DIR": PROJ, "BOARD_PROC_ROOT": PROC, "CLAUDE_JSON": os.path.join(TMP, "claude.json"),
                   "ALERT_DESKTOP": "0"})
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_")) or k in ("TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL"):
        os.environ.pop(k)
os.environ["ALERT_DESKTOP"] = "0"
for d in (HOME, CFG, SESS, PROJ):
    os.makedirs(d)

# Sentinels, built from pieces so no scanner sees a secret-looking literal: a vendor-shaped token
# (caught by pattern) and the literal value of a secret in the board's .env (caught by guard).
J = "".join
SENT_TOKEN = J(["gh", "p_", "Sentinel", "Card", "Token", "0123456789", "abcdef"])
SENT_LITERAL = J(["card", "-sentinel-", "Lit3ral", "-77x", "Q9"])
with open(os.path.join(CFG, ".env"), "w", encoding="utf-8") as fh:
    fh.write("BOARD_CARD_TEST_" + J(["TO", "KEN"]) + "=%s\n" % SENT_LITERAL)

import cards  # noqa: E402
import collect  # noqa: E402
import guard  # noqa: E402
import serve  # noqa: E402
from fake_proc import FakeProc  # noqa: E402

HOSTS = {"testhost"}
NOW = 1_800_000_000_000


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def jl(*entries):
    return "".join(json.dumps(e) + "\n" for e in entries)


def assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def user(content, **kw):
    e = {"type": "user", "message": {"role": "user", "content": content}}
    e.update(kw)
    return e


def tool_use():
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}}


def tool_result():
    return user([{"type": "tool_result", "tool_use_id": "t", "content": "ok"}])


def fleet(extra=""):
    write(os.path.join(CFG, "fleet.yaml"), "approvers: [local]\nsessions:\n  - code: CP\n    cwd: /w\n" + extra)
    guard.LITERALS.reset()


def cfg():
    return collect.fleet.load_fleet()


def step(rows, now=NOW, panes=None):
    return cards.step(cfg(), HOME, rows, now, panes or {}, collect.clean_text, collect.clean_title, HOSTS)


def row(code="CP", sid="s-cp", state="working", cwd=None, updated=0):
    return {"code": code, "session_id": sid, "pid": os.getpid(), "state": state, "cwd": cwd, "updated_at": updated}


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("cards.json", "cards-state.json", "resources.json"):
            try:
                os.unlink(os.path.join(HOME, name))
            except OSError:
                pass
        shutil.rmtree(PROJ, ignore_errors=True)
        shutil.rmtree(os.path.join(HOME, "statusline"), ignore_errors=True)
        shutil.rmtree(os.path.join(HOME, "state"), ignore_errors=True)
        os.makedirs(PROJ)
        fleet()


class Transcript(Base):
    def test_doing_and_prompt(self):
        write(os.path.join(PROJ, "-w", "s-cp.jsonl"), jl(
            user("Fix the login form\nand the second line stays out"),
            assistant("**Looking** at `login.py` in /home/someone/private/app/login.py now."),
            tool_use(), tool_result(),
            user("<system-reminder>not a prompt</system-reminder>", isMeta=True),
            user([{"type": "text", "text": "<local-command-stdout>x</local-command-stdout>"}])))
        c = step([row()])["sessions"]["CP"]
        self.assertEqual(c["doing"], "Looking at login.py in …/login.py now.")
        self.assertEqual(c["prompt"], "Fix the login form")

    def test_teammate_message_is_a_prompt(self):
        write(os.path.join(PROJ, "-w", "s-cp.jsonl"), jl(
            user('<teammate-message teammate_id="team-lead">\nShip the card fields\nmore</teammate-message>'),
            assistant("On it.")))
        self.assertEqual(step([row()])["sessions"]["CP"]["prompt"], "Ship the card fields")

    def test_clipped_to_280_one_line(self):
        write(os.path.join(PROJ, "-w", "s-cp.jsonl"), jl(user("go"), assistant("word\n" * 200)))
        d = step([row()])["sessions"]["CP"]["doing"]
        self.assertLessEqual(len(d), 280)
        self.assertNotIn("\n", d)
        self.assertTrue(d.endswith("…"))

    def test_show_prompt_off(self):
        fleet("cards:\n  show_prompt: false\n")
        write(os.path.join(PROJ, "-w", "s-cp.jsonl"), jl(user("a private prompt"), assistant("ok")))
        c = step([row()])["sessions"]["CP"]
        self.assertIsNone(c["prompt"])
        self.assertNotIn("a private prompt", read(os.path.join(HOME, "cards-state.json")))

    def test_show_prompt_switched_off_purges_the_cache(self):
        write(os.path.join(PROJ, "-w", "s-cp.jsonl"), jl(user("an earlier prompt"), assistant("ok")))
        self.assertEqual(step([row()])["sessions"]["CP"]["prompt"], "an earlier prompt")
        fleet("cards:\n  show_prompt: false\n")
        self.assertIsNone(step([row()], now=NOW + 1000)["sessions"]["CP"]["prompt"])
        self.assertNotIn("an earlier prompt", read(os.path.join(HOME, "cards-state.json")))

    def test_tail_only_and_previous_values_kept(self):
        path = os.path.join(PROJ, "-w", "s-cp.jsonl")
        write(path, jl(user("the first prompt"), assistant("first answer")))
        step([row()])
        # A long run of tool calls pushes the prompt out of the last 64 KB: the cached prompt stays.
        filler = jl(*([tool_use(), tool_result()] * 800))
        self.assertGreater(len(filler), cards.TAIL_BYTES)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(filler + jl(assistant("still going")))
        seen = []
        orig = cards.read_tail
        cards.read_tail = lambda p, size: seen.append(size) or orig(p, size)
        try:
            c = step([row()])["sessions"]["CP"]
        finally:
            cards.read_tail = orig
        self.assertEqual(c["doing"], "still going")
        self.assertEqual(c["prompt"], "the first prompt")
        self.assertEqual(len(seen), 1)

    def test_cached_by_mtime_and_capped_per_run(self):
        rows = []
        for i in range(12):
            write(os.path.join(PROJ, "-w", "s-%d.jsonl" % i), jl(user("p%d" % i), assistant("a%d" % i)))
            rows.append(row(code="S%d" % i, sid="s-%d" % i))
        calls = []
        orig = cards.read_tail
        cards.read_tail = lambda p, size: calls.append(p) or orig(p, size)
        try:
            doc = step(rows)
            self.assertEqual(len(calls), cards.TRANSCRIPTS_PER_RUN)
            self.assertEqual(sum(1 for c in doc["sessions"].values() if c.get("doing")), cards.TRANSCRIPTS_PER_RUN)
            calls.clear()
            doc = step(rows)  # the rest now; the first 8 are unchanged: no read
            self.assertEqual(len(calls), 12 - cards.TRANSCRIPTS_PER_RUN)
            self.assertEqual(sum(1 for c in doc["sessions"].values() if c.get("doing")), 12)
            calls.clear()
            step(rows)
            self.assertEqual(calls, [])
        finally:
            cards.read_tail = orig

    def test_no_transcript_retried_later(self):
        globs = []
        orig = cards.find_transcript
        cards.find_transcript = lambda sid: globs.append(sid) or orig(sid)
        try:
            step([row()])
            step([row()], now=NOW + 1000)
            self.assertEqual(len(globs), 1)
            step([row()], now=NOW + cards.MISS_RETRY_MS + 1)
            self.assertEqual(len(globs), 2)
        finally:
            cards.find_transcript = orig

    def test_direct_path_needs_no_glob(self):
        write(os.path.join(PROJ, "-w-my-app", "s-cp.jsonl"), jl(user("go"), assistant("direct")))
        globs = []
        orig = cards.find_transcript
        cards.find_transcript = lambda sid: globs.append(sid) or orig(sid)
        try:
            step([row(cwd="/w/my.app")])  # "/w/my.app" and "/w/my_app" both live in -w-my-app
            self.assertEqual(step([row(cwd="/w/my_app")])["sessions"]["CP"]["doing"], "direct")
        finally:
            cards.find_transcript = orig
        self.assertEqual(globs, [])

    def test_unsafe_session_id(self):
        c = step([row(sid="../x")])["sessions"]["CP"]
        self.assertIsNone(c["doing"])


class Sentinel(Base):
    """A secret in a transcript never leaves: not in cards.json, its cache, the snapshot, nor
    /api/state for an approver or anyone else."""

    def test_sentinel_never_appears(self):
        sid = "s-sentinel"
        write(os.path.join(PROJ, "-w", sid + ".jsonl"), jl(
            user("deploy with %s and %s please" % (SENT_TOKEN, SENT_LITERAL)),
            assistant("Using the key %s and %s to deploy now." % (SENT_TOKEN, SENT_LITERAL))))
        write(os.path.join(HOME, "state", sid + ".json"), json.dumps({
            "session_id": sid, "state": "working", "updated_at": NOW,
            "recent_done": [{"tool": "Bash", "summary": "echo " + SENT_LITERAL, "ms": 5, "ok": True, "at": NOW}]}))
        write(os.path.join(SESS, "%d.json" % os.getpid()), json.dumps({
            "pid": os.getpid(), "sessionId": sid, "cwd": TMP, "name": "CP", "status": "busy", "startedAt": 1}))
        try:
            self.assertEqual(collect.main(["--no-notify"]), 0)
        finally:
            os.unlink(os.path.join(SESS, "%d.json" % os.getpid()))
        c = json.loads(read(os.path.join(HOME, "cards.json")))["sessions"]["CP"]
        self.assertTrue(c["doing"].startswith("Using the key"))
        self.assertIn("[REDACTED", c["doing"])
        self.assertTrue(c["prompt"].startswith("deploy with"))
        self.assertEqual(len(c["done"]), 1)
        b = serve.StateBuilder(HOME)
        state = b.build(cfg())
        appr = serve.cards_for(state["sessions"], True)
        viewer = serve.cards_for(state["sessions"], False)
        vc = next(s for s in viewer if s["code"] == "CP")["card"]
        self.assertNotIn("doing", vc)
        self.assertNotIn("prompt", vc)
        self.assertEqual(vc["private"], "hidden")
        ac = next(s for s in appr if s["code"] == "CP")["card"]
        self.assertTrue(ac["doing"])
        # The hub's shared state still carries them for the next approver.
        self.assertIn("doing", next(s for s in state["sessions"] if s["code"] == "CP")["card"])
        blobs = {name: read(os.path.join(HOME, name))
                 for name in ("cards.json", "cards-state.json", "snapshot.json")}
        blobs["approver"] = json.dumps(appr, ensure_ascii=False)
        blobs["viewer"] = json.dumps(viewer, ensure_ascii=False)
        for name, text in blobs.items():
            for s in (SENT_TOKEN, SENT_LITERAL):
                self.assertNotIn(s, text, "%s leaks a sentinel" % name)
        self.assertNotIn("doing", blobs["snapshot.json"])
        self.assertNotIn("Using the key", blobs["viewer"])
        self.assertEqual(os.stat(os.path.join(HOME, "cards.json")).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.join(HOME, "cards-state.json")).st_mode & 0o777, 0o600)


class Teammates(Base):
    def test_titles_from_panes(self):
        fp = FakeProc(PROC)
        fp.proc(500, 1, "tmux", ["tmux"])
        fp.proc(600, 500, "sh", ["sh", "-c", "claude"])
        fp.proc(601, 600, "claude", ["claude", "--agent-name", "dev"])
        fp.proc(700, 500, "claude", ["claude", "--agent-name", "rev"])
        write(os.path.join(HOME, "resources.json"), json.dumps({"sessions": [{"code": "CP", "teammates": [
            {"name": "dev", "pid": 601, "rss_mb": 310, "idle": False, "idle_s": 4},
            {"name": "rev", "pid": 700, "rss_mb": 280, "idle": True, "idle_s": 2400},
            {"name": "lost", "pid": 999, "rss_mb": 100, "idle": False, "idle_s": None}]}]}))
        panes = {"%1": ("⠐ Owner away mode in /home/someone/x.py", 600), "%2": ("✳ Review the diff", 700),
                 "%3": ("testhost", 1)}
        tm = step([row()], panes=panes)["sessions"]["CP"]["teammates"]
        self.assertEqual([t["name"] for t in tm], ["dev", "lost", "rev"])
        dev, lost, rev = tm
        self.assertEqual(dev["title"], "Owner away mode in …/x.py")
        self.assertTrue(dev["busy"])
        self.assertEqual(dev["rss_mb"], 310)
        self.assertEqual(rev["title"], "Review the diff")
        self.assertFalse(rev["busy"])
        self.assertTrue(rev["idle"])
        self.assertIsNone(lost["title"])
        self.assertIsNone(lost["busy"])


class Meter(Base):
    def test_meter(self):
        write(os.path.join(HOME, "statusline", "s-cp.json"),
              json.dumps({"ctx_pct": 34.2, "cost_usd": 2.1034, "model": "Model <b>Large</b>", "at": NOW - 1000}))
        m = step([row()])["sessions"]["CP"]["meter"]
        self.assertEqual(m, {"ctx_pct": 34.2, "cost_usd": 2.1, "model": "Model bLargeb", "at": NOW - 1000})

    def test_stale_or_bad(self):
        write(os.path.join(HOME, "statusline", "s-cp.json"), json.dumps({"ctx_pct": 34, "at": NOW - cards.METER_MAX_AGE_MS - 1}))
        self.assertIsNone(step([row()])["sessions"]["CP"]["meter"])
        write(os.path.join(HOME, "statusline", "s-cp.json"), json.dumps({"ctx_pct": 340, "cost_usd": "x", "at": NOW}))
        self.assertIsNone(step([row()])["sessions"]["CP"]["meter"])


class Recent(Base):
    def test_done_ring(self):
        write(os.path.join(HOME, "state", "s-cp.json"), json.dumps({"recent_done": [
            {"tool": "Read", "summary": "/home/someone/private/a.txt", "ms": 12, "ok": True, "at": NOW - 5000},
            {"tool": "Bash", "summary": "x" * 300, "ms": -1, "ok": False, "at": NOW},
            "junk"]}))
        done = step([row()])["sessions"]["CP"]["done"]
        self.assertEqual(done[0], {"tool": "Read", "summary": "…/a.txt", "ms": 12, "ok": True, "at": NOW - 5000})
        self.assertFalse(done[1]["ok"])
        self.assertIsNone(done[1]["ms"])
        self.assertLessEqual(len(done[1]["summary"]), 120)
        self.assertEqual(len(done), 2)


class Branch(Base):
    def test_ahead_behind(self):
        repo = os.path.join(TMP, "repo")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_NOSYSTEM="1")

        def git(*a):
            subprocess.run(["git", "-C", repo] + list(a), check=True, capture_output=True, env=env)
        os.makedirs(repo)
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "base")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        git("commit", "-q", "--allow-empty", "-m", "one")
        git("commit", "-q", "--allow-empty", "-m", "feat: the card fields with " + SENT_TOKEN)
        b = step([row(cwd=repo)])["sessions"]["CP"]["branch"]
        self.assertEqual((b["base"], b["ahead"], b["behind"]), ("origin/main", 2, 0))
        self.assertTrue(b["subject"].startswith("feat: the card fields with"))
        self.assertNotIn(SENT_TOKEN, b["subject"])
        # Cached for GIT_TTL_MS: a new commit shows only after that.
        git("commit", "-q", "--allow-empty", "-m", "three")
        self.assertEqual(step([row(cwd=repo)], now=NOW + 1000)["sessions"]["CP"]["branch"]["ahead"], 2)
        self.assertEqual(step([row(cwd=repo)], now=NOW + cards.GIT_TTL_MS + 1)["sessions"]["CP"]["branch"]["ahead"], 3)

    def test_not_a_repo(self):
        self.assertIsNone(step([row(cwd=TMP)])["sessions"]["CP"]["branch"])


class Since(Base):
    def test_state_since(self):
        self.assertEqual(step([row(state="working")])["sessions"]["CP"]["state_since"], NOW)
        self.assertEqual(step([row(state="working")], now=NOW + 5000)["sessions"]["CP"]["state_since"], NOW)
        self.assertEqual(step([row(state="done")], now=NOW + 9000)["sessions"]["CP"]["state_since"], NOW + 9000)

    def test_offline_rows_only_since(self):
        r = {"code": "GONE", "session_id": None, "pid": None, "state": "offline", "cwd": None}
        self.assertEqual(step([r])["sessions"]["GONE"], {"state_since": NOW})

    def test_unchanged_doc_not_rewritten(self):
        step([row()])
        m1 = os.stat(os.path.join(HOME, "cards.json")).st_mtime_ns
        m2 = os.stat(os.path.join(HOME, "cards-state.json")).st_mtime_ns
        os.utime(os.path.join(HOME, "cards.json"), ns=(1, 1))
        os.utime(os.path.join(HOME, "cards-state.json"), ns=(1, 1))
        step([row()], now=NOW + 100)
        self.assertEqual(os.stat(os.path.join(HOME, "cards.json")).st_mtime_ns, 1)
        self.assertEqual(os.stat(os.path.join(HOME, "cards-state.json")).st_mtime_ns, 1)
        self.assertTrue(m1 and m2)


class PaneList(unittest.TestCase):
    def test_parse(self):
        fake = os.path.join(TMP, "fakebin")
        os.makedirs(fake, exist_ok=True)
        write(os.path.join(fake, "tmux"), "#!/bin/sh\nprintf '%%1\\t\\342\\234\\263 Task\\t4242\\n%%2\\tno pid column\\n%%3\\ta\\tb\\t77\\n'\n")
        os.chmod(os.path.join(fake, "tmux"), 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = fake + os.pathsep + old
        try:
            got = collect.pane_list()
        finally:
            os.environ["PATH"] = old
        self.assertEqual(got, {"%1": ("✳ Task", 4242), "%2": ("no pid column", None), "%3": ("a\tb", 77)})


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[sys.argv[0]])
