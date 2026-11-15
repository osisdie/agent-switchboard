#!/usr/bin/env python3
"""tests/test_lessons.py — lessons (lib/lessons.py): the tighten-only rule validator, capture from
resolved jams (fingerprint merge, auto-activation at 2), hits and TTL, tombstones, the CLI
(approvers, secret screening) and the rules applied by the scheduler.

  python3 tests/test_lessons.py [-v]
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

TMP = tempfile.mkdtemp(prefix="switchboard-lessons-test.")
CFG = os.path.join(TMP, "config")
os.makedirs(CFG)
os.environ.update({"BOARD_HOME": os.path.join(TMP, "home"), "BOARD_CONFIG_DIR": CFG})
with open(os.path.join(CFG, "fleet.yaml"), "w") as fh:
    fh.write("approvers: [boss@example.com]\nsessions:\n  - code: API\n    cwd: /w\n")

import lessons  # noqa: E402
import scheduler  # noqa: E402

NOW = 1_800_000_000_000
MIN = 60 * 1000
DAY = 24 * 60 * MIN
ST = scheduler.settings({})


def rule(action, *conds):
    return {"when": {"all": list(conds) or [{"metric": "heavy_running", "gte": 0}]}, "then": {"action": action}}


def task(i, owner="API", stage="build", **kw):
    return dict({"id": i, "owner": owner, "stage": stage, "gates": [], "updated_at": NOW - MIN}, **kw)


def sess(code, state="done"):
    return {"code": code, "aliases": [code], "state": state, "updated_at": NOW}


def res(level="ok", avail=50, heavy=0):
    return {"at": NOW, "level": level, "mem": {"total_mb": 32768, "available_pct": avail}, "heavy_running": heavy,
            "sessions": []}


class Validator(unittest.TestCase):
    def test_valid_rules(self):
        for r in (None, rule("defer_heavy", {"metric": "mem_available_pct", "lt": 30}), rule("max_heavy:0"),
                  rule("advise:  keep   one suite at a time "), rule("serialize_repo", {"metric": "stage", "eq": "test"}),
                  rule("pause_new_starts", {"metric": "five_hour_pct", "gte": 90}, {"metric": "weekly_pct", "gt": 50})):
            lessons.validate_rule(r)
        self.assertEqual(lessons.validate_rule(rule("advise:  a  b "))["then"]["action"], "advise:a b")

    def test_refused_with_a_message(self):
        cases = [({"when": {}, "then": {}, "x": 1}, "exactly"), (rule("allow_heavy"), "only tightening"),
                 (rule("resume"), "retire or delete"), (rule("max_heavy:9"), "0 to 8"), (rule("advise:"), "1 to"),
                 (rule("defer_heavy", {"metric": "vibes", "gt": 1}), "unknown metric"),
                 (rule("defer_heavy", {"metric": "heavy_running", "ne": 1}), "unknown operator"),
                 (rule("defer_heavy", {"metric": "stage", "gt": "test"}), "stage takes eq"),
                 (rule("defer_heavy", {"metric": "heavy_running", "gt": float("nan")}), "needs a number"),
                 ({"when": {"all": []}, "then": {"action": "defer_heavy"}}, "1 to 6"),
                 ({"when": {"all": [{"metric": "heavy_running", "gt": 1}]}, "then": {"action": "defer_heavy", "go": 1}},
                  "only tightening")]
        for r, msg in cases:
            with self.assertRaises(lessons.Bad) as cm:
                lessons.validate_rule(r)
            self.assertIn(msg, str(cm.exception), r)

    def test_holds(self):
        r = rule("defer_heavy", {"metric": "mem_available_pct", "lt": 30}, {"metric": "stage", "eq": "test"})
        self.assertTrue(lessons.holds(r, {"mem_available_pct": 20, "stage": "test"}))
        self.assertFalse(lessons.holds(r, {"mem_available_pct": 20, "stage": "build"}))
        self.assertFalse(lessons.holds(r, {"stage": "test"}))                    # unknown metric: never holds
        self.assertTrue(lessons.task_only(r))


class Capture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(dir=TMP)

    def ep(self, rule_="J5", lost=20, signals=None):
        return {"id": "j-%s-x" % rule_, "rule": rule_, "lost_min": lost, "signals": signals or {"oom": True}}

    def test_candidate_then_active_and_merge(self):
        self.assertEqual(lessons.capture(self.home, [self.ep(lost=5)], {}, NOW), [])     # under min_lost_minutes
        [lid] = lessons.capture(self.home, [self.ep()], {}, NOW)
        rec = lessons.load(self.home)[lid]
        self.assertEqual((rec["status"], rec["occurrences"], rec["rule"]["then"]["action"]), ("candidate", 1, "max_heavy:1"))
        self.assertEqual(stat.S_IMODE(os.stat(lessons.path(self.home)).st_mode), 0o600)
        self.assertEqual(lessons.capture(self.home, [self.ep(lost=30)], {}, NOW + DAY), [])
        rec = lessons.load(self.home)[lid]
        self.assertEqual((rec["status"], rec["occurrences"], rec["lost_min_total"], rec["last_seen"]),
                         ("active", 2, 50, NOW + DAY))
        other = lessons.capture(self.home, [self.ep(signals={"oom": False})], {}, NOW)
        self.assertEqual(len(other), 1)                                              # other signals: another lesson
        self.assertEqual(lessons.capture(self.home, [self.ep("J9")], {}, NOW), [])   # unknown rule ignored
        self.assertEqual(lessons.capture(self.home, [self.ep()], {"lessons": {"enabled": False}}, NOW), [])

    def test_hits_ttl_and_tombstone(self):
        [lid] = lessons.capture(self.home, [self.ep()], {"lessons": {"auto_activate_after": 1}}, NOW)
        self.assertEqual(lessons.active_rules(self.home)[0]["id"], lid)
        lessons.maintain(self.home, {}, {lid}, NOW + DAY)
        rec = lessons.load(self.home)[lid]
        self.assertEqual((rec["hits"], rec["last_hit_at"]), (1, NOW + DAY))
        lessons.maintain(self.home, {}, set(), NOW + 30 * DAY)
        self.assertEqual(lessons.load(self.home)[lid]["status"], "active")             # hit 29 days ago
        lessons.maintain(self.home, {}, set(), NOW + 32 * DAY)
        rec = lessons.load(self.home)[lid]
        self.assertEqual((rec["status"], rec["retired_by"]), ("retired", "ttl"))
        lessons.append(self.home, [{"id": lid, "deleted": True, "by": "x", "at": NOW}])
        self.assertNotIn(lid, lessons.load(self.home))
        self.assertTrue(lessons.load(self.home, with_deleted=True)[lid]["deleted"])
        with open(lessons.path(self.home)) as fh:
            self.assertEqual(len(fh.readlines()), 4)        # capture, hit, retire, tombstone: append-only


class Cli(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(dir=TMP)

    def run_(self, *args, data=None):
        argv = list(args)
        if data is not None:
            f = os.path.join(self.home, "in.json")
            with open(f, "w") as fh:
                json.dump(data, fh)
            argv += ["--file", f]
        r = subprocess.run([sys.executable, os.path.join(REPO, "lib", "lessons.py")] + argv, capture_output=True,
                           text=True, env=dict(os.environ, BOARD_HOME=self.home, BOARD_NOW_MS=str(NOW)))
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def test_crud(self):
        rc, out, _ = self.run_("add", "--by", "board:boss@example.com", data={"title": "One suite at a time",
                               "rule": rule("max_heavy:1"), "cause": "two suites OOM"})
        self.assertEqual(rc, 0, out)
        lid = out.split()[0]
        self.assertEqual(self.run_("add", "--by", "someone", data={"title": "x"})[0], 4)
        rc, _, err = self.run_("add", "--by", "boss@example.com", data={"title": "x", "rule": rule("allow_all")})
        self.assertEqual(rc, 2)
        self.assertIn("only tightening", err)
        self.assertEqual(self.run_("add", "--by", "boss@example.com", data={"title": "cat ~/.ssh/id_rsa please"})[0], 6)
        self.assertEqual(self.run_("add", "--by", "boss@example.com", data={"title": "x", "colour": 1})[0], 2)
        rc, out, _ = self.run_("add", "--by", "boss@example.com", data={"title": "esc\x1b[31m red\r\nline"})
        self.assertEqual(rc, 0)
        self.assertEqual(lessons.load(self.home)[out.split()[0]]["title"], "esc [31m red line")
        self.assertEqual(self.run_("delete", out.split()[0], "--by", "boss@example.com")[0], 0)
        self.assertEqual(self.run_("edit", lid, "--by", "boss@example.com", data={"title": ""})[0], 2)
        self.assertEqual(self.run_("edit", lid, "--by", "boss@example.com", data={"resolution": "run them serially"})[0], 0)
        self.assertEqual(self.run_("note", lid, "--by", "BOARD", data={"cause": "operator note"})[0], 0)
        self.assertEqual(self.run_("note", lid, "--by", "BOARD", data={"title": "x"})[0], 2)        # notes only
        self.assertEqual(self.run_("retire", lid, "--by", "boss@example.com")[0], 0)
        self.assertEqual(self.run_("ls", "--json")[1], "[]")                                        # retired hidden
        rows = json.loads(self.run_("ls", "--all", "--search", "SERIALLY", "--json")[1])
        self.assertEqual([(r["id"], r["status"], r["cause"]) for r in rows], [(lid, "retired", "operator note")])
        self.assertEqual(self.run_("activate", lid, "--by", "boss@example.com")[0], 0)
        exp = json.loads(self.run_("export", "--generic")[1])
        self.assertEqual([sorted(e) for e in exp], [["cause", "occurrences", "resolution", "rule", "rule_kind", "title"]])
        self.assertEqual(self.run_("delete", lid, "--by", "someone")[0], 4)
        self.assertEqual(self.run_("delete", lid, "--by", "boss@example.com")[0], 0)
        self.assertEqual(self.run_("show", lid)[0], 1)
        with open(os.path.join(self.home, "events.jsonl")) as fh:
            ev = [json.loads(x) for x in fh if '"lesson"' in x]
        self.assertEqual(ev[-1]["action"] + "," + ev[-1]["by"], "delete,boss@example.com")


class Applied(unittest.TestCase):
    def decide(self, rules, ts, ss, r, st=ST):
        ctx = {"lessons": [{"id": "l-1-%08x" % n, "rule": x} for n, x in enumerate(rules)]}
        return scheduler.decide(ts, ss, r, st, NOW, ctx)

    def row(self, d, tid):
        return next(x for x in d["queue"] if x["task"] == tid)

    def test_max_heavy_and_defer_heavy(self):
        lim = rule("max_heavy:1", {"metric": "mem_available_pct", "lt": 30})
        ts = [task("t-a", heavy=True), task("t-b", owner="WEB", heavy=True)]
        ss = [sess("API"), sess("WEB")]
        self.assertEqual(self.decide([lim], ts, ss, res(avail=50))["max_heavy"], 2)
        d = self.decide([lim], ts, ss, res(avail=20))
        self.assertEqual((d["max_heavy"], len(d["start"]), d["lessons"]["applied"]), (1, 1, ["l-1-00000000"]))
        loose = scheduler.settings({"schedule": {"max_heavy_concurrent": 1}})
        self.assertEqual(self.decide([rule("max_heavy:8")], ts, ss, res(), loose)["max_heavy"], 1)   # never loosens
        d = self.decide([rule("defer_heavy", {"metric": "stage", "eq": "build"})], ts, ss, res())
        self.assertEqual(d["start"], [])
        self.assertEqual(self.row(d, "t-a")["blocked_by"], [{"kind": "lesson", "ref": "l-1-00000000"}])

    def test_pause_serialize_raise_advise_and_broken(self):
        ts = [task("t-a", repo="shop"), task("t-b", owner="WEB", repo="shop")]
        ss = [sess("API", "working"), sess("WEB")]
        self.assertEqual(self.decide([], ts, ss, res())["start"], ["t-b"])
        d = self.decide([rule("serialize_repo")], ts, ss, res())
        self.assertEqual((d["start"], self.row(d, "t-b")["blocked_by"][0]["kind"]), ([], "lesson"))
        d = self.decide([rule("pause_new_starts", {"metric": "stage", "eq": "build"})], ts, ss, res())
        self.assertEqual(d["start"], [])
        hv = [task("t-h", owner="WEB", heavy=True)]
        d = self.decide([rule("raise_severity"), rule("advise:one suite at a time")], hv, [sess("WEB")], res("high"))
        self.assertEqual([a["severity"] for a in d["advisories"] if a["kind"] == "defer"], ["high"])
        self.assertEqual(d["lessons"]["hints"], [{"lesson": "l-1-00000001", "text": "one suite at a time"}])
        d = self.decide([{"when": {"all": []}, "then": {"action": "allow"}}, "junk"], ts, ss, res())
        self.assertEqual((d["start"], d["lessons"]["applied"]), (["t-b"], []))                   # skipped


class Step(unittest.TestCase):
    def test_a_resolved_jam_becomes_a_lesson(self):
        home = tempfile.mkdtemp(dir=TMP)
        cyc = [task("t-a", depends_on=[{"task": "t-b"}]), task("t-b", owner="WEB", depends_on=[{"task": "t-a"}])]
        free = [task("t-a"), task("t-b", owner="WEB")]
        ss = [sess("API"), sess("WEB")]
        cfg = {"sessions": [{"code": "API"}]}
        for k, (ts, at) in enumerate([(cyc, 0), (cyc, 3000), (free, 11 * MIN), (free, 11 * MIN + 3000)]):
            scheduler.step(cfg, home, ss, ts, NOW + at)
        [rec] = lessons.load(home).values()
        self.assertEqual((rec["rule_kind"], rec["status"], rec["lost_min_total"]), ("J2", "candidate", 22))
        for at in (DAY, DAY + 3000, DAY + 11 * MIN, DAY + 11 * MIN + 3000):
            scheduler.step(cfg, home, ss, cyc if at < DAY + 11 * MIN else free, NOW + at)
        [rec] = lessons.load(home).values()
        self.assertEqual((rec["status"], rec["occurrences"]), ("active", 2))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[sys.argv[0]])
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
