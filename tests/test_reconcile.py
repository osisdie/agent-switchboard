#!/usr/bin/env python3
"""tests/test_reconcile.py — a pane prompt's decision settled from what the session reported
(lib/reconcile.py, hooks/board-hook.sh, lib/collect.py process_decisions).

Drives the real hook with Claude Code's event shapes and runs the collector's decision pass:
answered in the pane, a press stopped at Submit then submitted in the pane (delivered), different
answers (answered in the pane), the PostToolUse arriving after the decision went stale, permission
allow / deny, never touching relayed / unauthorized / timeout, and the bounded events.jsonl backfill.

  python3 tests/test_reconcile.py [-v]
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
sys.path.insert(0, os.path.join(REPO, "lib"))
TMP = tempfile.mkdtemp(prefix="switchboard-reconcile-test.")
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_")) or k in ("TMUX", "TMUX_PANE"):
        os.environ.pop(k, None)
os.environ.update({"BOARD_HOME": os.path.join(TMP, "home0"), "BOARD_CONFIG_DIR": os.path.join(TMP, "config"),
                   "ALERT_DESKTOP": "0"})
os.makedirs(os.environ["BOARD_CONFIG_DIR"])

import collect  # noqa: E402
import reconcile  # noqa: E402

HOOK = os.path.join(REPO, "hooks", "board-hook.sh")
SID = "11111111-2222-3333-4444-555555555555"
FREE = "deploy to the staging cluster first"
Q1 = [{"question": "Which color do you prefer for the test?", "header": "Color", "multiSelect": False,
       "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"},
                   {"label": "Green", "description": "natural"}]}]
Q2 = Q1 + [{"question": "Where should it go?", "header": "Target", "multiSelect": False,
            "options": [{"label": "Production", "description": "live"}, {"label": "Nowhere", "description": "skip"}]}]

# Multi-select questions, as recorded from Claude Code 2.1.32x (tests/fixtures/multiselect/
# posttooluse-*.json): the session reports the checked boxes as their labels joined by ", " in the
# order they were checked; "" when boxes were checked and cleared, no entry when never touched.
FIX = os.path.join(REPO, "tests", "fixtures", "multiselect")


def fixture(name):
    with open(os.path.join(FIX, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


QM = fixture("posttooluse-form-cleared")["tool_input"]["questions"]   # Toppings (multi), Crust, Sides (multi)
QF = fixture("posttooluse-single")["tool_input"]["questions"]          # Fruits (multi)


def now_ms():
    return int(time.time() * 1000)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(dir=TMP)

    # ---- hook
    def hook(self, payload):
        env = dict(os.environ, BOARD_HOME=self.home, TMUX_PANE="%42")
        r = subprocess.run(["bash", HOOK], input=json.dumps(dict(payload, session_id=SID)), env=env,
                           capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def ask(self, tu, qs):
        self.hook({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_use_id": tu,
                   "tool_input": {"questions": qs}})
        self.hook({"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion",
                   "tool_input": {"questions": qs}})
        return self.dec_for(tu)

    def perm(self, tu, cmd="make deploy"):
        self.hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": tu,
                   "tool_input": {"command": cmd}})
        self.hook({"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": cmd}})
        return self.dec_for(tu)

    def submit(self, tu, qs, answers):
        """The picker submitted in the pane: Claude Code's PostToolUse for AskUserQuestion."""
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_use_id": tu,
                   "tool_input": {"questions": qs, "answers": answers},
                   "tool_response": {"questions": qs, "answers": answers, "annotations": {}}})

    def ran(self, tu, ok=True, error=None, tool="Bash"):
        p = {"hook_event_name": "PostToolUse" if ok else "PostToolUseFailure", "tool_name": tool,
             "tool_use_id": tu, "tool_input": {"command": "make deploy"}}
        if not ok:
            p["error"] = error or "Exit code 1"
        self.hook(p)

    # ---- files
    def dec_for(self, tu):
        for f in os.listdir(os.path.join(self.home, "decisions")):
            d = self.read(f[:-5])
            if d.get("tool_use_id") == tu:
                return d["id"]
        self.fail("no decision for %s" % tu)

    def read(self, did):
        with open(os.path.join(self.home, "decisions", did + ".json"), encoding="utf-8") as fh:
            return json.load(fh)

    def board_answer(self, did, *args, path_prefix=None):
        """The real `board answer DID ARGS --by user-1` (an approver, so an owner) on this home."""
        cfg = os.path.join(self.home, "cfg")
        if not os.path.exists(os.path.join(cfg, "fleet.yaml")):
            os.makedirs(cfg, exist_ok=True)
            with open(os.path.join(cfg, "fleet.yaml"), "w", encoding="utf-8") as fh:
                fh.write("approvers: [user-1]\n")
        env = dict(os.environ, BOARD_HOME=self.home, BOARD_CONFIG_DIR=cfg,
                   CLAUDE_SESSIONS_DIR=os.path.join(self.home, "sessions"))
        if path_prefix:
            env["PATH"] = path_prefix + os.pathsep + os.environ["PATH"]
        r = subprocess.run([os.path.join(REPO, "bin", "board"), "answer", did] + list(args) + ["--by", "user-1"],
                           env=env, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.read(did)

    def form_file(self, entries):
        path = os.path.join(self.home, "form.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
        return path

    def patch(self, did, **kw):
        d = self.read(did)
        d.update(kw)
        with open(os.path.join(self.home, "decisions", did + ".json"), "w", encoding="utf-8") as fh:
            json.dump(d, fh)

    def state(self):
        with open(os.path.join(self.home, "state", SID + ".json"), encoding="utf-8") as fh:
            return json.load(fh)

    def events(self):
        with open(os.path.join(self.home, "events.jsonl"), encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def everything(self):
        out = []
        for root, _, files in os.walk(self.home):
            for f in files:
                with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                    out.append(fh.read())
        return "\n".join(out)

    def collect(self, now=None):
        st = self.state()
        sessions = [{"session_id": SID, "code": "CP", "aliases": ["CP"], "pid": 4242,
                     "state": st.get("state"), "pending": st.get("pending") if st.get("state") == "needs-you" else None}]
        return collect.process_decisions({}, self.home, sessions, now or now_ms())


class HookRecords(Base):
    def test_picks_and_hashes_never_the_text(self):
        self.ask("tu_h1", Q2)
        self.submit("tu_h1", Q2, {Q2[0]["question"]: "Blue", Q2[1]["question"]: FREE})
        ev = [e for e in self.events() if e.get("event") == "PostToolUse"][-1]
        self.assertEqual(ev["tool_use_id"], "tu_h1")
        self.assertEqual(ev["outcome"]["picks"], [2, None])
        self.assertEqual(ev["outcome"]["kid"], reconcile.key_id(reconcile.hash_key(self.home, create=False)))
        self.assertIsNone(ev["outcome"]["text_sha"][0])
        key = reconcile.hash_key(self.home, create=False)
        self.assertEqual(ev["outcome"]["text_sha"][1], reconcile.text_sha(key, "tu_h1", FREE))
        self.assertNotIn("staging cluster", self.everything())
        self.assertIsNone(self.state()["pending"])

    def test_label_match_is_whitespace_and_case_insensitive(self):
        self.ask("tu_h2", Q1)
        self.submit("tu_h2", Q1, {Q1[0]["question"]: "  green "})
        ev = [e for e in self.events() if e.get("event") == "PostToolUse"][-1]
        self.assertEqual(ev["outcome"]["picks"], [3])

    def test_hash_key_is_private_and_per_install(self):
        self.ask("tu_k1", Q1)
        self.submit("tu_k1", Q1, {Q1[0]["question"]: FREE})
        path = os.path.join(self.home, reconcile.KEY_FILE)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o077, 0)
        mine = [e for e in self.events() if e.get("event") == "PostToolUse"][-1]["outcome"]["text_sha"][0]
        mine_home, self.home = self.home, tempfile.mkdtemp(dir=TMP)  # another install, same answer
        self.submit("tu_k1", Q1, {Q1[0]["question"]: FREE})
        theirs = [e for e in self.events() if e.get("event") == "PostToolUse"][-1]["outcome"]["text_sha"][0]
        self.home = mine_home
        self.assertTrue(mine and theirs and mine != theirs, (mine, theirs))
        # a key readable by others is never used
        os.chmod(path, 0o644)
        self.assertIsNone(reconcile.hash_key(self.home, create=False))

    def test_hash_key_without_hard_links(self):
        import errno as _errno
        real_link = os.link

        def no_link(*a, **k):
            raise OSError(_errno.EPERM, "Operation not permitted")
        os.link = no_link
        try:
            key = reconcile.hash_key(self.home)
            self.assertEqual(len(key or b""), reconcile.KEY_BYTES)
            self.assertEqual(reconcile.hash_key(self.home), key)  # the same key is read back
            self.assertEqual(os.stat(os.path.join(self.home, reconcile.KEY_FILE)).st_mode & 0o777, 0o600)

            def io_error(*a, **k):
                raise OSError(_errno.EIO, "I/O error")
            os.link = io_error
            other = tempfile.mkdtemp(dir=TMP)
            self.assertIsNone(reconcile.hash_key(other))  # any other failure: no key, nothing hashed
            self.assertEqual([f for f in os.listdir(os.path.join(other, "secrets"))], [])
        finally:
            os.link = real_link

    def test_email_or_phone_gets_no_hash(self):
        for i, text in enumerate(("mail it to someone@example.com", "call +1 (555) 010-9999 tomorrow")):
            self.ask("tu_e%d" % i, Q1)
            self.submit("tu_e%d" % i, Q1, {Q1[0]["question"]: text})
            ev = [e for e in self.events() if e.get("event") == "PostToolUse"][-1]
            self.assertEqual(ev["outcome"], {"picks": [None], "text_sha": [None]}, text)

    def test_labels_differing_by_case_need_the_exact_label(self):
        qs = [{"question": "Which mode?", "header": "Mode", "multiSelect": False,
               "options": [{"label": "Fast", "description": ""}, {"label": "FAST", "description": ""},
                           {"label": "Slow", "description": ""}]}]
        self.ask("tu_c1", qs)
        self.submit("tu_c1", qs, {"Which mode?": "FAST"})
        self.assertEqual([e for e in self.events() if e.get("event") == "PostToolUse"][-1]["outcome"]["picks"], [2])
        self.ask("tu_c2", qs)
        self.submit("tu_c2", qs, {"Which mode?": "fast"})  # matches two labels case-insensitively: free text
        self.assertEqual([e for e in self.events() if e.get("event") == "PostToolUse"][-1]["outcome"]["picks"], [None])
        self.ask("tu_c3", qs)
        self.submit("tu_c3", qs, {"Which mode?": "slow"})  # one case-insensitive match
        self.assertEqual([e for e in self.events() if e.get("event") == "PostToolUse"][-1]["outcome"]["picks"], [3])

    def test_secret_looking_free_text_gets_no_hash(self):
        self.ask("tu_h3", Q1)
        value = "Zx81kq2Lm" + "Pq7Rt5Vw3Yb9Nc4"  # built at run time: no secret-looking literal in the repo
        self.submit("tu_h3", Q1, {Q1[0]["question"]: "use " + "token=" + value})
        ev = [e for e in self.events() if e.get("event") == "PostToolUse"][-1]
        self.assertEqual(ev["outcome"], {"picks": [None], "text_sha": [None]})
        self.assertNotIn(value, self.everything())

    def test_failure_records_denied_without_the_error_text(self):
        self.perm("tu_h4")
        self.ran("tu_h4", ok=False, error="The user doesn't want to proceed with this tool use. "
                                          "The tool use was rejected (MARKER-xyz).")
        ev = [e for e in self.events() if e.get("event") == "PostToolUseFailure"][-1]
        self.assertIs(ev.get("denied"), True)
        self.assertNotIn("MARKER-xyz", self.everything())
        self.perm("tu_h5")
        self.ran("tu_h5", ok=False, error="Exit code 2: permission denied on /srv")
        ev = [e for e in self.events() if e.get("event") == "PostToolUseFailure"][-1]
        self.assertNotIn("denied", ev)

    def test_other_tools_carry_no_outcome(self):
        self.perm("tu_h6")
        self.ran("tu_h6")
        ev = [e for e in self.events() if e.get("event") == "PostToolUse"][-1]
        self.assertNotIn("outcome", ev)


class PaneQuestions(Base):
    def test_no_board_answer_answered_in_pane(self):
        did = self.ask("tu_p1", Q1)
        self.collect()
        self.assertEqual(self.read(did)["status"], "pending")
        self.submit("tu_p1", Q1, {Q1[0]["question"]: "Red"})
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d["resolved_via"], d["resolved_by"], d["reconcile_result"]),
                         ("answered_in_pane", "pane", "CP", "no_answer"))
        self.assertNotIn("status_reason", d)
        self.assertNotIn("delivered_via", d)
        last = [e for e in self.events() if e.get("type") == "decision"][-1]
        self.assertEqual((last["status"], last["reason"]), ("answered_in_pane", "session_outcome"))

    def test_partial_press_then_submitted_in_pane_is_delivered(self):
        did = self.ask("tu_p2", Q2)
        d = self.board_answer(did, "--form-file", self.form_file([{"option": 2}, {"text": FREE}]))
        # as bin/board stores a form: the composed answer, the text row as option count + 1
        self.assertEqual((d["answer"], d["answer_options"], d["answer_texts"]),
                         ("Color: Blue · Target: " + FREE, [2, 3], [None, FREE]))
        # board press typed both answers, then stopped at Submit: stale, partial
        self.patch(did, status="stale", status_reason="partial")
        self.collect()
        self.assertEqual(self.read(did)["status"], "stale")
        self.submit("tu_p2", Q2, {Q2[0]["question"]: "Blue", Q2[1]["question"]: "  deploy to the   staging cluster first"})
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d["delivered_via"], d["reconciled_from"], d["reconcile_result"]),
                         ("relayed", "session", "partial", "match"))
        self.assertNotIn("status_reason", d)
        self.assertTrue(d["relayed_at"])

    def test_different_answers_answered_in_pane(self):
        did = self.ask("tu_p3", Q2)
        self.board_answer(did, "--form-file", self.form_file([{"option": 2}, {"text": FREE}]))
        self.patch(did, status="stale", status_reason="partial")
        self.submit("tu_p3", Q2, {Q2[0]["question"]: "Blue", Q2[1]["question"]: "Nowhere"})
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d["resolved_via"], d["reconciled_from"], d["reconcile_result"]),
                         ("answered_in_pane", "pane", "partial", "differs"))
        self.assertEqual(d["answer_options"], [2, 3])  # the board's answer is kept as it was

    def test_form_options_then_submitted_in_pane(self):
        for tu, pane, want in (("tu_f1", ("Blue", "Production"), ("relayed", "match")),
                               ("tu_f2", ("Red", "Production"), ("answered_in_pane", "differs")),
                               ("tu_f3", ("Blue", "Nowhere"), ("answered_in_pane", "differs"))):
            did = self.ask(tu, Q2)
            d = self.board_answer(did, "--options", "2,1")
            self.assertEqual((d["answer"], d["answer_options"], d.get("answer_texts")),
                             ("Color: Blue · Target: Production", [2, 1], None))
            self.patch(did, status="stale", status_reason="partial")
            self.submit(tu, Q2, {Q2[0]["question"]: pane[0], Q2[1]["question"]: pane[1]})
            self.collect()
            d = self.read(did)
            self.assertEqual((d["status"], d["reconcile_result"]), want, pane)

    def test_a_replaced_key_is_unknown_not_differs(self):
        for tu, replace, want in (("tu_r1", False, ("relayed", "match")), ("tu_r2", True, ("answered_in_pane", "unknown"))):
            did = self.ask(tu, Q1)
            self.board_answer(did, FREE)
            self.patch(did, status="stale", status_reason="partial")
            self.submit(tu, Q1, {Q1[0]["question"]: FREE})
            ev = [e for e in self.events() if e.get("event") == "PostToolUse"][-1]["outcome"]
            key = reconcile.hash_key(self.home, create=False)
            self.assertEqual(ev["kid"], reconcile.key_id(key))
            self.assertNotEqual(ev["kid"], key.hex()[:8])  # an id, not a piece of the key
            if replace:  # the key file deleted (or restored from elsewhere): a new key is made
                os.remove(os.path.join(self.home, reconcile.KEY_FILE))
                self.assertNotEqual(reconcile.key_id(reconcile.hash_key(self.home)), ev["kid"])
            self.collect()
            d = self.read(did)
            self.assertEqual((d["status"], d["reconcile_result"]), want, tu)

    def test_form_free_text_differs(self):
        did = self.ask("tu_f4", Q2)
        self.board_answer(did, "--form-file", self.form_file([{"option": 2}, {"text": FREE}]))
        self.patch(did, status="stale", status_reason="partial")
        self.submit("tu_f4", Q2, {Q2[0]["question"]: "Blue", Q2[1]["question"]: "deploy to production first"})
        self.collect()
        self.assertEqual(self.read(did)["reconcile_result"], "differs")

    def test_late_post_tool_use_after_stale(self):
        did = self.ask("tu_p4", Q1)
        self.patch(did, answer="Blue", answer_index=2, relay="press", status="answered")
        # the session moved on before it reported the picker (no PostToolUse yet)
        self.hook({"hook_event_name": "UserPromptSubmit", "prompt": "x"})
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d["status_reason"]), ("stale", "moved_on"))
        self.submit("tu_p4", Q1, {Q1[0]["question"]: "Blue"})
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d.get("delivered_via"), d.get("reconciled_from")), ("relayed", "session", "moved_on"))

    def test_single_free_text_answer_delivered(self):
        did = self.ask("tu_p5", Q1)
        self.patch(did, answer="Purple, please", answer_index=None, relay="press", status="stale", status_reason="partial")
        self.submit("tu_p5", Q1, {Q1[0]["question"]: "Purple,  please"})
        self.collect()
        self.assertEqual(self.read(did)["status"], "relayed")

    def test_declined_in_pane(self):
        did = self.ask("tu_p6", Q1)
        self.patch(did, answer="Blue", answer_index=2, status="stale", status_reason="screen")
        self.ran("tu_p6", ok=False, tool="AskUserQuestion", error="User declined to answer questions")
        self.collect()
        d = self.read(did)
        # a failed / declined picker cannot say what the session got: unknown, never "different"
        self.assertEqual((d["status"], d["reconciled_from"], d["reconcile_result"]), ("answered_in_pane", "screen", "unknown"))

    def test_free_text_that_got_no_hash_is_unknown(self):
        did = self.ask("tu_p9", Q1)
        self.patch(did, answer="mail someone@example.com", answer_index=None, status="stale", status_reason="partial")
        self.submit("tu_p9", Q1, {Q1[0]["question"]: "mail someone@example.com"})
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d["reconcile_result"]), ("answered_in_pane", "unknown"))

    def test_inconsistent_answer_fields_wait_then_unknown(self):
        did = self.ask("tu_p10", Q1)
        # "Red" next to the option number of an earlier answer ("Blue" = 2): a write in progress
        self.patch(did, answer="Red", answer_index=2, status="answered", updated_at=now_ms())
        self.submit("tu_p10", Q1, {Q1[0]["question"]: "Blue"})
        self.collect()
        d = self.read(did)
        self.assertNotIn("reconcile_result", d)  # not settled against the stale option number
        self.assertEqual(d["status"], "stale")
        self.collect(now_ms() + 2 * reconcile.INCONSISTENT_GRACE_MS)
        d = self.read(did)
        self.assertEqual((d["status"], d["reconcile_result"]), ("answered_in_pane", "unknown"))

    def test_a_write_time_ahead_of_the_clock_never_waits(self):
        did = self.ask("tu_p12", Q1)
        self.patch(did, answer="Red", answer_index=2, status="answered", updated_at=now_ms() + 3600 * 1000)
        self.submit("tu_p12", Q1, {Q1[0]["question"]: "Blue"})
        self.collect()
        self.assertEqual(self.read(did).get("reconcile_result"), "unknown")

    def test_board_answer_is_one_write(self):
        shim = os.path.join(self.home, "shim")
        os.makedirs(shim)
        log = os.path.join(self.home, "mv.log")
        real = shutil.which("mv")
        with open(os.path.join(shim, "mv"), "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nfor a; do last="$a"; done\ncase "$last" in */decisions/*.json) echo "$last" >> "%s";; esac\nexec %s "$@"\n' % (log, real))
        os.chmod(os.path.join(shim, "mv"), 0o755)
        did = self.ask("tu_p11", Q2)
        for args in (["--options", "2,1"], ["Blue"]):
            if os.path.exists(log):
                os.remove(log)
            self.board_answer(did, *args, path_prefix=shim)
            with open(log, encoding="utf-8") as fh:
                writes = [x for x in fh.read().split() if x.endswith(did + ".json")]
            self.assertEqual(len(writes), 1, (args, writes))
        d = self.read(did)
        # the re-answer cleared the form's options in the same write
        self.assertEqual((d["answer"], d.get("answer_options"), d.get("answer_texts")), ("Blue", None, None))

    def test_an_answer_by_press_in_flight_is_confirmed(self):
        did = self.ask("tu_p7", Q1)
        self.patch(did, answer="Blue", answer_index=2, relay="press", status="answered")
        self.submit("tu_p7", Q1, {Q1[0]["question"]: "Blue"})
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d["delivered_via"]), ("relayed", "session"))
        self.assertNotIn("reconciled_from", d)  # it was not stale: nothing to say about it


    def test_a_settle_that_loses_a_race_is_retried(self):
        did = self.ask("tu_p8", Q1)
        self.patch(did, answer="Blue", answer_index=2, relay="press", status="answered")
        self.submit("tu_p8", Q1, {Q1[0]["question"]: "Blue"})
        orig = reconcile.settle

        def racing(d, oc, *rest):
            # board press marks it stale (partial) between the collector's read and its write
            self.patch(did, status="stale", status_reason="partial")
            return orig(d, oc, *rest)
        reconcile.settle = racing
        try:
            self.collect()
        finally:
            reconcile.settle = orig
        self.assertEqual(self.read(did)["status"], "stale")  # the other writer won this run
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d.get("reconciled_from")), ("relayed", "partial"))


class Permissions(Base):
    def test_allow_matches_post_tool_use(self):
        did = self.perm("tu_a1")
        self.patch(did, answer="allow", status="stale", status_reason="screen")
        self.ran("tu_a1")
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d["delivered_via"], d["reconciled_from"]), ("relayed", "session", "screen"))

    def test_deny_matches_a_rejection(self):
        did = self.perm("tu_a2")
        self.patch(did, answer="deny", status="stale", status_reason="screen")
        self.ran("tu_a2", ok=False, error="The user doesn't want to proceed with this tool use.")
        self.collect()
        self.assertEqual(self.read(did)["status"], "relayed")

    def test_deny_but_allowed_in_pane(self):
        did = self.perm("tu_a3")
        self.patch(did, answer="deny", status="stale", status_reason="screen")
        self.ran("tu_a3")
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d["resolved_via"], d["reconcile_result"]), ("answered_in_pane", "pane", "differs"))

    def test_a_failure_that_is_not_a_rejection_never_counts_as_deny(self):
        did = self.perm("tu_a4")
        self.patch(did, answer="deny", status="stale", status_reason="screen")
        self.ran("tu_a4", ok=False, error="Exit code 1")
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d["reconcile_result"]), ("answered_in_pane", "unknown"))

    def test_allow_then_the_command_failed_is_unknown_not_different(self):
        did = self.perm("tu_a6")
        self.patch(did, answer="allow", status="stale", status_reason="screen")
        self.ran("tu_a6", ok=False, error="Exit code 1")  # allowed in the pane, then the command failed
        self.collect()
        d = self.read(did)
        self.assertEqual((d["status"], d["reconcile_result"]), ("answered_in_pane", "unknown"))

    def test_no_board_answer(self):
        did = self.perm("tu_a5")
        self.ran("tu_a5")
        self.collect()
        self.assertEqual(self.read(did)["status"], "answered_in_pane")


class NeverDowngraded(Base):
    def test_relayed_unauthorized_timeout_untouched(self):
        for tu, status in (("tu_n1", "relayed"), ("tu_n2", "unauthorized"), ("tu_n3", "timeout"),
                           ("tu_n4", "answered_in_pane")):
            did = self.ask(tu, Q1)
            self.patch(did, answer="Red", answer_index=1, status=status)
            self.collect()  # fills in `from`
            before = self.read(did)
            self.submit(tu, Q1, {Q1[0]["question"]: "Blue"})
            self.collect()
            self.assertEqual(self.read(did), before, status)


class Backfill(Base):
    def write_events(self, lines):
        with open(os.path.join(self.home, "events.jsonl"), "a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(json.dumps(ln) + "\n")

    def stale_dec(self, did, tu, created, **kw):
        os.makedirs(os.path.join(self.home, "decisions"), exist_ok=True)
        d = {"id": did, "kind": "permission", "tool_use_id": tu, "tool_name": "Bash", "session_id": SID,
             "status": "stale", "status_reason": "moved_on", "answer": None, "created_at": created,
             "updated_at": created}
        d.update(kw)
        with open(os.path.join(self.home, "decisions", did + ".json"), "w", encoding="utf-8") as fh:
            json.dump(d, fh)

    def test_backfill_recent_events_once(self):
        n = now_ms()
        day = 24 * 3600 * 1000
        os.makedirs(os.path.join(self.home, "state"))
        with open(os.path.join(self.home, "state", SID + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"state": "working", "pending": None}, fh)
        self.stale_dec("d-1-recent", "tu_b1", n - 2 * day, answer="allow")
        self.stale_dec("d-1-q", "tu_b2", n - 3 * day, kind="question", source="pane")
        self.stale_dec("d-1-old", "tu_b3", n - 9 * day)
        self.stale_dec("d-1-qa", "tu_b6", n - 3 * day, kind="question", source="pane", answer="Blue", answer_index=2,
                       questions=Q1)
        # lines from a hook that did not record outcomes yet (no outcome / denied fields)
        self.write_events([
            {"ts": n - 9 * day + 5, "type": "hook", "session_id": SID, "event": "PostToolUse", "tool_name": "Bash",
             "tool_use_id": "tu_b3", "state": "working"},
            {"ts": n - 2 * day + 5, "type": "hook", "session_id": SID, "event": "PostToolUse", "tool_name": "Bash",
             "tool_use_id": "tu_b1", "state": "working"},
            {"ts": n - 3 * day + 5, "type": "hook", "session_id": SID, "event": "PostToolUse",
             "tool_name": "AskUserQuestion", "tool_use_id": "tu_b2", "state": "working"},
            {"ts": n - 3 * day + 6, "type": "hook", "session_id": SID, "event": "PostToolUse",
             "tool_name": "AskUserQuestion", "tool_use_id": "tu_b6", "state": "working"},
        ])
        self.collect(n)
        self.assertEqual(self.read("d-1-recent")["status"], "relayed")
        # a picker without a recorded outcome: answered in the pane (its answers are unknown)
        self.assertEqual(self.read("d-1-q")["status"], "answered_in_pane")
        # a pre-feature picker with a board answer: the report cannot tell, never "different"
        self.assertEqual([self.read("d-1-qa")[k] for k in ("status", "reconcile_result")], ["answered_in_pane", "unknown"])
        self.assertEqual(self.read("d-1-old")["status"], "stale")  # outside the 7-day window
        with open(os.path.join(self.home, reconcile.CURSOR_FILE), encoding="utf-8") as fh:
            cur = json.load(fh)
        self.assertGreater(cur["off"], 0)
        # read incrementally from there on: nothing it has read is read again
        self.assertEqual(reconcile.read_outcomes(self.home, n)[0], {})
        self.stale_dec("d-1-next", "tu_b4", n - 1000)
        self.write_events([{"ts": n, "type": "hook", "session_id": SID, "event": "PostToolUseFailure",
                            "tool_name": "Bash", "tool_use_id": "tu_b4", "state": "working"}])
        outs, _ = reconcile.read_outcomes(self.home, n)
        self.assertEqual(list(outs), ["tu_b4"])
        self.collect(n + 1)
        self.assertEqual(self.read("d-1-next")["status"], "answered_in_pane")

    def test_backfill_is_bounded(self):
        n = now_ms()
        with open(os.path.join(self.home, "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": n, "type": "hook", "event": "PostToolUse", "tool_use_id": "tu_far"}) + "\n")
            fh.write(("x" * 1000 + "\n") * (reconcile.BACKFILL_BYTES // 1000 + 10))
            fh.write(json.dumps({"ts": n, "type": "hook", "event": "PostToolUse", "tool_use_id": "tu_near"}) + "\n")
        outs, _ = reconcile.read_outcomes(self.home, n)
        self.assertEqual(list(outs), ["tu_near"])

    def test_an_outcome_older_than_the_decision_is_ignored(self):
        n = now_ms()
        self.stale_dec("d-1-new", "tu_b5", n - 1000, answer="allow")
        self.write_events([{"ts": n - 5000, "type": "hook", "event": "PostToolUse", "tool_use_id": "tu_b5"}])
        collect.process_decisions({}, self.home, [], n)
        self.assertEqual(self.read("d-1-new")["status"], "stale")


class Compare(unittest.TestCase):
    def test_board_answers(self):
        self.assertIsNone(reconcile.board_answers({"answer": None}))
        self.assertEqual(reconcile.board_answers({"kind": "permission", "answer": "allow"}), "allow")
        self.assertEqual(reconcile.board_answers({"answer": "Blue", "answer_index": 2}), [("pick", 2)])
        self.assertEqual(reconcile.board_answers({"answer": "Blue", "answer_index": 2, "questions": Q1}), [("pick", 2)])
        self.assertEqual(reconcile.board_answers({"answer": "Red", "answer_index": 2, "questions": Q1}), reconcile.INCONSISTENT)
        self.assertEqual(reconcile.board_answers({"answer": "Red", "answer_index": None, "questions": Q1}), reconcile.INCONSISTENT)
        form = {"answer": "Color: Blue · Target: Production", "answer_options": [2, 1], "answer_texts": None,
                "questions": Q2}
        self.assertEqual(reconcile.board_answers(form), [("pick", 2), ("pick", 1)])
        # a single answer next to a form's options, or options of another answer
        self.assertEqual(reconcile.board_answers(dict(form, answer="Red")), reconcile.INCONSISTENT)
        self.assertEqual(reconcile.board_answers(dict(form, answer_options=[1, 1])), reconcile.INCONSISTENT)
        # a free-text row is that question's option count + 1, with its text
        ft = dict(form, answer="Color: Blue · Target: x y", answer_options=[2, 3], answer_texts=[None, "x y"])
        self.assertEqual(reconcile.board_answers(ft), [("pick", 2), ("text", "x y")])
        self.assertEqual(reconcile.board_answers(dict(ft, answer_options=[2, 1])), reconcile.INCONSISTENT)
        self.assertEqual(reconcile.board_answers(dict(form, answer_options=[2])), reconcile.INCONSISTENT)
        self.assertEqual(reconcile.board_answers({"answer": "x y"}), [("text", "x y")])

    def test_norm(self):
        self.assertEqual(reconcile.norm("  a　 b\n c\t"), "a b c")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class MultiSelect(Base):
    def outcome(self):
        return [e for e in self.events() if e.get("event") == "PostToolUse"][-1]["outcome"]

    def test_real_posttooluse_payloads(self):
        # the payloads as the session sent them (only the hook's own fields set here)
        for name, picks in (("posttooluse-single", [[1, 3]]),            # "Cherry, Apple"
                            ("posttooluse-form-cleared", [[2, 3], 2, []]),  # "Olives, Peppers", "Thick", ""
                            ("posttooluse-form-untouched", [[2, 4], 1, []])):  # "Onions, Olives", "Thin", (none)
            p = fixture(name)
            self.assertEqual(reconcile.hook_outcome(p, self.home), {"picks": picks, "text_sha": [None] * len(picks)}, name)
            self.ask(p["tool_use_id"], p["tool_input"]["questions"])
            self.hook(p)
            self.assertEqual(self.outcome()["picks"], picks, name)

    def test_multi_picks(self):
        opts = [{"label": "North, East"}, {"label": "North"}, {"label": "East"}]
        # labels holding ", ": read as a set; more than one way to read it is ambiguous
        self.assertEqual(reconcile.multi_picks("North, East", opts), reconcile.AMBIGUOUS)   # [1] or [2, 3]
        self.assertEqual(reconcile.multi_picks("East, North, East", opts), [1, 3])
        self.assertEqual(reconcile.multi_picks("North, East, East", opts), [1, 3])
        self.assertEqual(reconcile.multi_picks("North, East, North", opts), [1, 2])
        self.assertEqual(reconcile.multi_picks("East,  North", opts), [2, 3])
        self.assertEqual(reconcile.multi_picks("", opts), [])
        self.assertIsNone(reconcile.multi_picks("East, East", opts))         # a label used twice
        self.assertIsNone(reconcile.multi_picks("East, something else", opts))
        self.assertIsNone(reconcile.multi_picks("east", opts))               # labels are exact

    def test_ambiguous_labels_are_unknown_never_differs(self):
        qs = [{"question": "Which regions should the report cover?", "header": "Regions", "multiSelect": True,
               "options": [{"label": "North, East", "description": ""}, {"label": "North", "description": ""},
                           {"label": "East", "description": ""}]}]
        for tu, board_labels, pane, want in (
                ("tu_a1", ["North", "East"], "North, East", ("answered_in_pane", "unknown")),   # [2,3] or [1]
                ("tu_a2", ["North, East"], "North, East", ("answered_in_pane", "unknown")),
                ("tu_a3", ["North", "East"], "East, North", ("relayed", "match")),            # one way only
                ("tu_a4", ["North, East"], "East, North", ("answered_in_pane", "differs"))):
            did = self.ask(tu, qs)
            self.board_answer(did, "--form-file", self.form_file([{"labels": board_labels}]))
            self.submit(tu, qs, {qs[0]["question"]: pane})
            o = self.outcome()
            self.assertNotEqual(o["picks"][0] is None and o["text_sha"][0] is not None, True, "never hashed as free text")
            self.collect()
            d = self.read(did)
            self.assertEqual((d["status"], d["reconcile_result"]), want, (board_labels, pane))

    def test_empty_selection_is_an_answer(self):
        # "" (boxes checked, then cleared) and no entry at all both match an empty answer, never
        # no_answer; a box checked in the pane differs
        for tu, answers, want in (("tu_e1", {QF[0]["question"]: ""}, ("relayed", "match")),
                                  ("tu_e2", {}, ("relayed", "match")),
                                  ("tu_e3", {QF[0]["question"]: "Banana"}, ("answered_in_pane", "differs"))):
            did = self.ask(tu, QF)
            d = self.board_answer(did, "--form-file", self.form_file([{"labels": []}]))
            self.assertEqual((d["answer"], d["answer_options"]), ("Fruits: (none)", [[]]))
            self.submit(tu, QF, answers)
            self.assertEqual(self.outcome()["picks"], [[] if want[1] == "match" else [2]])
            self.collect()
            d = self.read(did)
            self.assertEqual((d["status"], d["reconcile_result"]), want, answers)

    def test_free_text_in_type_something_is_hashed(self):
        self.ask("tu_m0", QF)
        self.submit("tu_m0", QF, {QF[0]["question"]: "Apple, " + FREE})
        o = self.outcome()
        self.assertEqual(o["picks"], [None])
        self.assertTrue(o["text_sha"][0])

    def test_board_labels_then_session_outcome(self):
        # the board recorded Toppings Olives + Onions, Crust Thin, Sides none; a press stopped at
        # Submit, then the form was submitted in the pane
        for tu, pane, want in (
                ("tu_m1", ("Onions, Olives", "Thin", None), ("relayed", "match")),     # any order; Sides untouched
                ("tu_m2", ("Olives, Onions", "Thin", ""), ("relayed", "match")),       # Sides cleared
                ("tu_m3", ("Olives", "Thin", ""), ("answered_in_pane", "differs")),    # a box less
                ("tu_m4", ("Olives, Onions", "Thin", "Soup"), ("answered_in_pane", "differs")),
                ("tu_m5", ("Olives, Onions, " + FREE, "Thin", ""), ("answered_in_pane", "differs"))):
            did = self.ask(tu, QM)
            d = self.board_answer(did, "--form-file", self.form_file(
                [{"labels": ["Onions", "Olives"]}, {"option": 1}, {"labels": []}]))
            self.assertEqual((d["answer"], d["answer_options"], d["answer_texts"]),
                             ("Toppings: Olives, Onions · Crust: Thin · Sides: (none)", [[2, 4], 1, []], [None, None, None]))
            self.patch(did, status="stale", status_reason="partial")
            answers = {QM[0]["question"]: pane[0], QM[1]["question"]: pane[1]}
            if pane[2] is not None:
                answers[QM[2]["question"]] = pane[2]
            self.submit(tu, QM, answers)
            self.collect()
            d = self.read(did)
            self.assertEqual((d["status"], d["reconcile_result"]), want, pane)

    def test_single_multi_select_question(self):
        for tu, pane, want in (("tu_s1", "Cherry, Apple", ("relayed", "match")),
                               ("tu_s2", "Apple", ("answered_in_pane", "differs"))):
            did = self.ask(tu, QF)
            d = self.board_answer(did, "--form-file", self.form_file([{"question_index": 0, "labels": ["Apple", "Cherry"]}]))
            self.assertEqual((d["answer"], d["answer_options"]), ("Fruits: Apple, Cherry", [[1, 3]]))
            self.submit(tu, QF, {QF[0]["question"]: pane})
            self.collect()
            d = self.read(did)
            self.assertEqual((d["status"], d["reconcile_result"]), want, pane)

    def test_inconsistent_multi_fields(self):
        did = self.ask("tu_i1", QF)
        self.board_answer(did, "--form-file", self.form_file([{"labels": ["Apple"]}]))
        for bad in ([[3, 1]], [[1, 1]], [[9]], [1], [[True]]):
            self.patch(did, answer_options=bad)
            self.assertEqual(reconcile.board_answers(self.read(did)), reconcile.INCONSISTENT, bad)
        self.patch(did, answer_options=[[1]], answer="Fruits: Banana")   # another answer's options
        self.assertEqual(reconcile.board_answers(self.read(did)), reconcile.INCONSISTENT)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[sys.argv[0]])
