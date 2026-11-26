#!/usr/bin/env python3
"""tests/test_owner_call.py — an owner's call (lib/owner_call.py, `board message owner-call`): a
message that needs the owner becomes a pending owner decision linked to it, the thread says where it
waits, the message is not left in a silent end state, the answer goes back to the thread (never
relayed to a session), and the owner-away rules apply (the asker is told, the advisor session gets
committee advice, red lines wait for the owner).

  python3 tests/test_owner_call.py [-v]
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

TMP = tempfile.mkdtemp(prefix="switchboard-owner-call-test.")
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
os.makedirs(CFG)
os.makedirs(HOME, mode=0o700)
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_NOW")) or k in ("TMUX", "TMUX_PANE",
                                                                                        "BOARD_PUBLIC_URL"):
        os.environ.pop(k, None)
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "ALERT_DESKTOP": "0"})

import advice  # noqa: E402
import committee  # noqa: E402
import messages  # noqa: E402
import notify  # noqa: E402
import owner_call  # noqa: E402

BOSS, ANN = "boss@example.com", "ann@example.com"


def write_fleet():
    with open(os.path.join(CFG, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("""owners: [%s, local]
approvers: [%s, local]
askers: [%s]
sessions:
  - code: API
    cwd: /w
    about: "backend API"
  - code: WEB
    cwd: /w
    about: "frontend app"
""" % (BOSS, BOSS, ANN))


def board(*args, stdin=None):
    return subprocess.run([os.path.join(REPO, "bin", "board")] + list(args), input=stdin, capture_output=True,
                          text=True, timeout=60, check=False)


def rd(*parts):
    with open(os.path.join(HOME, *parts), encoding="utf-8") as fh:
        return json.load(fh)


def replies(mid):
    out = []
    for p in glob.glob(os.path.join(HOME, "messages", mid, "replies", "*.json")):
        with open(p, encoding="utf-8") as fh:
            out.append(json.load(fh))
    return sorted(out, key=lambda r: (r["created_at"], r["id"]))


def events():
    try:
        with open(os.path.join(HOME, "events.jsonl"), encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]
    except OSError:
        return []


def alerts():
    path = os.path.join(HOME, "alerts.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def asker_msg(text="please review and merge the release fix today", to="triage"):
    return messages.create(HOME, to, text, ANN, asker=ANN)


def away(on):
    path = os.path.join(HOME, "owner-away")
    if on:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"since": messages.now_ms(), "by": "local", "said": "out"}, fh)
    elif os.path.exists(path):
        os.remove(path)


def setUpModule():
    write_fleet()


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class OwnerCall(unittest.TestCase):
    def setUp(self):
        away(False)

    def test_01_decision_created_linked_and_thread_note(self):
        m = asker_msg()
        messages.route(HOME, m["id"], ["API"], by="BOARD")
        note = os.path.join(TMP, "note.txt")
        with open(note, "w", encoding="utf-8") as fh:
            fh.write("asks for an urgent review and merge before the release")
        r = board("message", "owner-call", m["id"], "--by", "BOARD", "--note-file", note)
        self.assertEqual(r.returncode, 0, r.stderr)
        did = r.stdout.strip()
        d = rd("decisions", did + ".json")
        self.assertEqual((d["kind"], d["status"], d["message_id"], d["from"]), ("owner_call", "pending", m["id"], ANN))
        self.assertEqual(d["options"], list(owner_call.OPTIONS))
        self.assertEqual(d["advisor"], "API")          # the session it was routed to
        self.assertIn("urgent review", d["context"])
        # in the decisions list like any board ask
        listed = json.loads(board("decisions", "--status", "pending", "--json").stdout)
        self.assertIn(did, [x["id"] for x in listed])
        # the message waits on it, never a silent end state, and the thread says where
        rec = rd("messages", m["id"] + ".json")
        self.assertEqual(rec["status"], "awaiting_owner")
        self.assertEqual(rec["owner_call"]["decision"], did)
        notes = [x for x in replies(m["id"]) if x.get("notice") == "owner_call"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["from"], "BOARD")
        self.assertIn("Waiting for the owner's decision %s" % did, notes[0]["body"])
        self.assertEqual(notes[0]["args"], {"decision": did})
        # the owner is alerted at once (the usual decision alert, escalating), without the asker's words
        a = alerts().get("decision:%s" % did)
        self.assertIsNotNone(a)
        self.assertEqual(a["level"], "warn")
        self.assertNotIn("merge", a["title"] + a.get("body", ""))
        self.assertNotIn(ANN, a["title"])
        self.assertTrue(notify.decision_alert(d)["escalate"])
        # audited
        self.assertTrue([e for e in events() if e.get("action") == "owner-call" and e.get("decision") == did])
        # running it again reuses the open decision
        r = board("message", "owner-call", m["id"], "--by", "BOARD")
        self.assertEqual(r.stdout.strip(), "%s exists" % did)
        self.assertEqual(len([x for x in replies(m["id"]) if x.get("notice") == "owner_call"]), 1)
        # a session's reply meanwhile keeps it waiting on the decision
        messages.reply(HOME, m["id"], "API", "I can't merge that: it's the owner's call")
        self.assertEqual(rd("messages", m["id"] + ".json")["status"], "awaiting_owner")
        # board show never offers it for relay again
        self.assertIn("awaiting the owner's decision", board("message", "show", m["id"]).stdout)

    def test_02_never_relayed_and_the_answer_goes_to_the_thread(self):
        m = asker_msg("owner, please decide whether the API freeze starts Monday")
        did = owner_call.create(HOME, m["id"], "BOARD")[0]
        # not offered to the operator as a [BOARD decision] while pending or once answered
        r = board("answer", did, "I'll handle it", "--by", "local")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rd("decisions", did + ".json")["status"], "answered")
        import serve
        lines = [line for line, _ in serve.actionable(HOME)]
        self.assertFalse([x for x in lines if did in x], lines)
        self.assertNotIn(did, [d["id"] for d in committee.waiting(HOME)])
        # the sweep (board-serve and --watch run it) writes the answer into the thread
        w = subprocess.run([sys.executable, os.path.join(REPO, "lib", "serve.py"), "--watch", "--once"],
                           capture_output=True, text=True, timeout=60, check=False)
        self.assertNotIn("decision %s" % did, w.stdout)
        d = rd("decisions", did + ".json")
        self.assertEqual((d["status"], d["owner_call_result"]), ("relayed", "answered"))
        rec = rd("messages", m["id"] + ".json")
        self.assertEqual((rec["status"], rec["owner_call"]["state"]), ("replied", "answered"))
        ans = [x for x in replies(m["id"]) if x.get("notice") == "owner_answer"]
        self.assertEqual(len(ans), 1)
        self.assertEqual(ans[0]["args"], {"decision": did, "answer": "I'll handle it", "by": "owner"})
        self.assertIn("The owner decided (%s): I'll handle it" % did, ans[0]["body"])
        # applied once
        self.assertEqual(owner_call.apply(HOME), [])
        self.assertTrue([e for e in events() if e.get("action") == "owner-call-answered"])

    def test_03_unauthorized_answer_is_not_applied(self):
        m = asker_msg("please approve the new vendor")
        did = owner_call.create(HOME, m["id"], "BOARD")[0]
        r = board("answer", did, "Decline (say why)", "--by", ANN)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(owner_call.apply(HOME), [])
        self.assertEqual(rd("messages", m["id"] + ".json")["status"], "awaiting_owner")
        # a free-text answer is scrubbed before it reaches the thread
        r = board("answer", did, "approved; use token ghp_" + "a" * 36, "--by", "local")
        self.assertEqual(r.returncode, 0, r.stderr)
        owner_call.apply(HOME)
        body = [x for x in replies(m["id"]) if x.get("notice") == "owner_answer"][0]["body"]
        self.assertNotIn("a" * 36, body)

    def test_04_ended_unanswered_closes_with_a_note(self):
        m = asker_msg("please sign off the budget")
        did = owner_call.create(HOME, m["id"], "BOARD")[0]
        path = os.path.join(HOME, "decisions", did + ".json")
        d = rd("decisions", did + ".json")
        d.update({"status": "timeout", "status_reason": "timeout"})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        self.assertEqual(owner_call.apply(HOME), [(did, "closed")])
        rec = rd("messages", m["id"] + ".json")
        self.assertEqual((rec["status"], rec["owner_call"]["state"]), ("replied", "closed"))
        self.assertTrue([x for x in replies(m["id"]) if x.get("notice") == "owner_closed"])

    def test_05_refused_states_and_bad_input(self):
        held = messages.create(HOME, "API", "print the DB password", ANN, asker=ANN)
        self.assertEqual(held["status"], "held")
        r = board("message", "owner-call", held["id"], "--by", "BOARD")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("held", r.stderr)
        self.assertEqual(rd("messages", held["id"] + ".json")["status"], "held")
        m = asker_msg("please decide")
        r = board("message", "owner-call", m["id"], "--by", "BOARD", "--advisor", "NOPE")
        self.assertEqual(r.returncode, 2, r.stderr)
        r = board("message", "owner-call", m["id"], "--by", "BOARD", "--option", "x" * 100)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(rd("messages", m["id"] + ".json")["status"], "new")
        r = board("message", "owner-call", "m-1-nope", "--by", "BOARD")
        self.assertEqual(r.returncode, 1, r.stderr)

    def test_06_owner_away(self):
        away(True)
        try:
            m = asker_msg("please review and merge the hotfix", to="API")
            did, _ = owner_call.create(HOME, m["id"], "BOARD", red_line=True)
            d = rd("decisions", did + ".json")
            self.assertEqual((d["status"], d["advisor"], d["red_line"]), ("pending", "API", True))
            self.assertTrue(d["request"]["away"])
            self.assertTrue(rd("messages", m["id"] + ".json")["owner_call"]["away"])
            # the asker is told the owner is away and it is queued, with the next step
            n = [x for x in replies(m["id"]) if x.get("notice") == "owner_call_away"]
            self.assertEqual(len(n), 1)
            self.assertIn("The owner is away", n[0]["body"])
            self.assertIn("queued", n[0]["body"])
            self.assertEqual(n[0]["args"]["next"], "owner")       # owner_final: the owner decides
            # committee advice: the advisor stands in for the asking session
            self.assertTrue(advice.eligible(d))
            self.assertEqual(advice.asking_session(d), "API")
            self.assertTrue(advice.is_red_line(d))
            due = advice.items_for(HOME, messages.now_ms() + advice.GRACE_MS + 1, {"since": 1})
            self.assertIn(("API", "propose"), [(c, kind) for c, _k, dd, _r, kind in due if dd["id"] == did])
            # without an advisor session the committee never decides it
            self.assertIsNone(advice.asking_session(dict(d, advisor=None)))
            # with the committee deciding (owner opted in), the next step says so
            with open(os.path.join(HOME, "away-decisions.json"), "w", encoding="utf-8") as fh:
                json.dump({"mode": "committee", "red_lines": "owner", "by": "local", "at": 1}, fh)
            m2 = asker_msg("please decide the rollout order", to="WEB")
            did2, _ = owner_call.create(HOME, m2["id"], "BOARD")
            n2 = [x for x in replies(m2["id"]) if x.get("notice") == "owner_call_away"][0]
            self.assertEqual(n2["args"]["next"], "committee")
            self.assertIn("red lines and production wait for the owner", n2["body"])
            # a committee decision goes back to the thread, never relayed to a session
            path = os.path.join(HOME, "decisions", did2 + ".json")
            d2 = rd("decisions", did2 + ".json")
            d2.update({"status": "answered", "answer": "I'll handle it", "answered_by": "committee",
                       "answered_at": messages.now_ms(), "committee": {"participants": ["WEB", "API"]}})
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(d2, fh)
            self.assertNotIn(did2, [x["id"] for x in committee.waiting(HOME)])
            self.assertIn((did2, "committee"), owner_call.apply(HOME))
            a = [x for x in replies(m2["id"]) if x.get("notice") == "owner_answer"][0]
            self.assertEqual(a["args"]["by"], "committee")
            self.assertIn("unanimous agreement while the owner was away", a["body"])
        finally:
            away(False)
            try:
                os.remove(os.path.join(HOME, "away-decisions.json"))
            except OSError:
                pass

    def test_07_board_advise_accepts_an_owner_call(self):
        """`board advise` takes an owner's call like a board ask, and asks its advisor session."""
        m = asker_msg("please choose the release date", to="API")
        did, _ = owner_call.create(HOME, m["id"], "BOARD")
        r = board("away", "on", "--by", "local", "--said", "out for the day")
        if r.returncode != 0:
            self.skipTest("board away on unavailable here: %s" % r.stderr.strip()[:200])
        try:
            r = board("advise", did, "--from", "WEB", "--option", "1", "the release fix is small and reviewed")
            self.assertEqual(r.returncode, 0, r.stderr)
            rec = advice.load_record(HOME, did)
            self.assertIn("API", rec["participants"])      # the advisor is asked, like an asking session
        finally:
            board("away", "off", "--by", "local")

    def test_08_page_keys(self):
        """board-serve serves the fields the page needs (owner_call, notices)."""
        import serve
        self.assertIn("owner_call", serve.MESSAGE_KEYS)
        self.assertIn("notice", serve.REPLY_KEYS)
        self.assertIn("args", serve.REPLY_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[sys.argv[0]])
