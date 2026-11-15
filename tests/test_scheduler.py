#!/usr/bin/env python3
"""tests/test_scheduler.py — the scheduler core (lib/scheduler.py): resource levels ok, warn, high
and critical; queueing per session; fairness of heavy slots; stuck tasks; finish-first and
rebase-needed; advisory-text invariants; idempotence and the collector step's write-on-change.

  python3 tests/test_scheduler.py [-v]
"""
import json
import os
import random
import shutil
import stat
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

TMP = tempfile.mkdtemp(prefix="switchboard-sched-test.")
os.environ.update({"BOARD_HOME": os.path.join(TMP, "home"), "BOARD_CONFIG_DIR": os.path.join(TMP, "config")})

import scheduler  # noqa: E402

NOW = 1_800_000_000_000
MIN = 60 * 1000
ST = scheduler.settings({})


def task(i, owner="API", stage="build", ago=MIN, **kw):
    t = {"id": i, "owner": owner, "title": "x", "stage": stage, "artifacts": {}, "gates": [],
         "created_at": NOW - ago, "updated_at": NOW - ago}
    t.update(kw)
    return t


def sess(code, state="done", ago=0):
    return {"code": code, "aliases": [code], "state": state, "updated_at": NOW - ago}


def res(level="ok", heavy_running=0, sessions=(), total_mb=32768, at=NOW):
    return {"at": at, "level": level, "mem": {"total_mb": total_mb}, "heavy_running": heavy_running,
            "sessions": list(sessions)}


def row(doc, tid):
    return next(r for r in doc["queue"] if r["task"] == tid)


def kinds(doc, kind):
    return [a for a in doc["advisories"] if a["kind"] == kind]


class Levels(unittest.TestCase):
    def setUp(self):
        self.tasks = [task("t-h", stage="test", heavy=True), task("t-l", owner="WEB", stage="build")]
        self.sessions = [sess("API"), sess("WEB")]

    def run_at(self, level, **kw):
        return scheduler.decide(self.tasks, self.sessions, res(level, **kw), ST, NOW)

    def test_ok_heavy_may_start(self):
        d = self.run_at("ok")
        self.assertEqual(d["mode"], "normal")
        self.assertEqual(sorted(d["start"]), ["t-h", "t-l"])
        self.assertEqual(d["advisories"], [])

    def test_warn_ranks_heavy_lower_but_lets_it_start(self):
        ok, warn = self.run_at("ok"), self.run_at("warn")
        self.assertEqual(row(ok, "t-h")["score"] - row(warn, "t-h")["score"], 15)
        self.assertEqual(row(warn, "t-l")["score"], row(ok, "t-l")["score"])
        self.assertIn("t-h", warn["start"])

    def test_high_defers_heavy_and_advises(self):
        d = self.run_at("high")
        self.assertEqual(row(d, "t-h")["state"], "deferred")
        self.assertEqual(row(d, "t-h")["blocked_by"], [{"kind": "resource", "ref": "high"}])
        self.assertEqual(d["start"], ["t-l"])
        [a] = kinds(d, "defer")
        self.assertEqual((a["to"], a["task"], a["severity"]), ("API", "t-h", "warn"))
        self.assertEqual(kinds(d, "stop-own-heavy"), [])

    def test_high_lets_a_session_take_lighter_work(self):
        self.tasks.append(task("t-l2", stage="plan", ago=2 * MIN))
        d = self.run_at("high")
        self.assertEqual(row(d, "t-l2")["state"], "ready")
        self.assertEqual(row(d, "t-h")["state"], "deferred")

    def test_critical_degraded_and_stop_own_heavy(self):
        procs = [
            {"code": "API", "heavy_n": 2, "children": [
                {"class": "test", "comm": "pytest", "pid": 11, "rss_mb": 900, "heavy": True, "via": None},
                {"class": "browser", "comm": "chrome", "pid": 12, "rss_mb": 1500, "heavy": True, "via": "qa"},
                {"class": "other", "comm": "bash", "pid": 13, "rss_mb": 3000, "heavy": False, "via": None}]},
            {"code": "WEB", "heavy_n": 1, "children": [
                {"class": "build", "comm": "tsc", "pid": 21, "rss_mb": 1200, "heavy": True, "via": None}]},
            {"code": "OPS", "heavy_n": 1, "children": [
                {"class": "test", "comm": "jest", "pid": 31, "rss_mb": 400, "heavy": True, "via": None}]},
        ]
        d = self.run_at("critical", sessions=procs, heavy_running=3)
        self.assertEqual(d["mode"], "degraded")
        stops = kinds(d, "stop-own-heavy")
        self.assertEqual([(a["to"], a["pid"]) for a in stops], [("API", 12), ("WEB", 21)])   # largest first, stop_max 2
        self.assertIn("teammate qa", stops[0]["text"])
        self.assertIn("Never stop a process you did not start", stops[0]["text"])
        self.assertEqual(stops[0]["severity"], "high")
        off = scheduler.decide(self.tasks, self.sessions, res("critical", sessions=procs),
                               scheduler.settings({"schedule": {"stop_own_heavy": False}}), NOW)
        self.assertEqual(kinds(off, "stop-own-heavy"), [])

    def test_stale_or_missing_resources_apply_no_gate(self):
        for r, state in ((res("critical", at=NOW - 10 * MIN), "stale"), (None, "missing"),
                         ({"enabled": False, "at": NOW}, "disabled")):
            d = scheduler.decide(self.tasks, self.sessions, r, ST, NOW)
            self.assertEqual((d["resources"], d["level"], d["mode"]), (state, None, "normal"))
            self.assertIn("t-h", d["start"])

    def test_max_heavy_auto_follows_memory(self):
        self.assertEqual(self.run_at("ok", total_mb=8000)["max_heavy"], 1)
        self.assertEqual(self.run_at("ok")["max_heavy"], 2)


class Queueing(unittest.TestCase):
    def test_busy_session_runs_its_current_task_others_queue(self):
        ts = [task("t-a", ago=5 * MIN), task("t-b", ago=MIN), task("t-c", ago=9 * MIN)]
        d = scheduler.decide(ts, [sess("API", "working")], res(), ST, NOW)
        self.assertEqual(row(d, "t-b")["state"], "run")                    # touched last
        self.assertEqual({row(d, i)["state"] for i in ("t-a", "t-c")}, {"queued"})
        self.assertIn("after t-b", row(d, "t-a")["why"])
        self.assertEqual(d["start"], [])

    def test_free_session_gets_one_ready_task(self):
        ts = [task("t-a", stage="plan"), task("t-b", stage="test")]
        d = scheduler.decide(ts, [sess("API", "rest")], res(), ST, NOW)
        self.assertEqual(d["start"], ["t-b"])                              # higher stage weight
        self.assertEqual(row(d, "t-a")["state"], "queued")

    def test_gates(self):
        ts = [task("t-dep", owner="WEB"),
              task("t-m", depends_on=[{"task": "t-dep", "kind": "merge"}]),
              task("t-g", owner="OPS", gates=[{"stage": "build", "gate": "owner", "status": "pending", "decision_id": "d-1-a"}]),
              task("t-p", owner="QA", stage="production"),
              task("t-z", owner="DOC", status="paused"),
              task("t-x", owner="GONE"),
              task("t-done", owner="WEB", status="done")]
        sessions = [sess(c) for c in ("API", "WEB", "OPS", "QA", "DOC")]
        d = scheduler.decide(ts, sessions, res(), ST, NOW)
        self.assertEqual(row(d, "t-m")["blocked_by"], [{"kind": "task", "ref": "t-dep"}])
        self.assertEqual(row(d, "t-g")["blocked_by"], [{"kind": "human", "ref": "d-1-a"}])
        self.assertEqual(row(d, "t-p")["blocked_by"], [{"kind": "redline", "ref": "production"}])
        self.assertEqual(row(d, "t-z")["state"], "paused")
        self.assertEqual((row(d, "t-x")["state"], row(d, "t-x")["blocked_by"][0]["kind"]), ("deferred", "session"))
        self.assertNotIn("t-done", [r["task"] for r in d["queue"]])
        self.assertIn("unblocks 1", row(d, "t-dep")["why"])
        ts[0]["pr"] = {"url": "https://example.test/pr/1", "state": "merged"}
        self.assertEqual(row(scheduler.decide(ts, sessions, res(), ST, NOW), "t-m")["state"], "ready")

    def test_deploy_and_done_deps(self):
        base = task("t-1", owner="WEB")
        dep = task("t-2", depends_on=[{"task": "t-1", "kind": "deploy"}])
        sessions = [sess("API"), sess("WEB", "working")]
        self.assertEqual(row(scheduler.decide([base, dep], sessions, res(), ST, NOW), "t-2")["state"], "blocked")
        base["gates"] = [{"stage": "deploy", "gate": "production", "status": "approved"}]
        self.assertEqual(row(scheduler.decide([base, dep], sessions, res(), ST, NOW), "t-2")["state"], "ready")
        dep["depends_on"] = [{"task": "t-1"}]                              # kind done by default
        self.assertEqual(row(scheduler.decide([base, dep], sessions, res(), ST, NOW), "t-2")["state"], "blocked")
        base["status"] = "done"
        self.assertEqual(row(scheduler.decide([base, dep], sessions, res(), ST, NOW), "t-2")["state"], "ready")

    def test_blocked_current_task_frees_the_session(self):
        ts = [task("t-a", ago=MIN, gates=[{"status": "pending", "decision_id": "d-2-b"}]), task("t-b", ago=5 * MIN)]
        d = scheduler.decide(ts, [sess("API", "needs-you")], res(), ST, NOW)
        self.assertEqual(row(d, "t-a")["state"], "blocked")
        self.assertEqual(d["start"], ["t-b"])

    def test_alias_owner(self):
        s = dict(sess("API"), aliases=["API", "api-main"])
        d = scheduler.decide([task("t-a", owner="api-main")], [s], res(), ST, NOW)
        self.assertEqual((row(d, "t-a")["session"], d["start"]), ("API", ["t-a"]))

    def test_resume_advice_after_idle(self):
        ts = [task("t-a")]
        self.assertEqual(kinds(scheduler.decide(ts, [sess("API", "done", 5 * MIN)], res(), ST, NOW), "resume"), [])
        [a] = kinds(scheduler.decide(ts, [sess("API", "done", 31 * MIN)], res(), ST, NOW), "resume")
        self.assertEqual((a["to"], a["task"], a["severity"]), ("API", "t-a", "info"))


class Fairness(unittest.TestCase):
    def test_heavy_slot_goes_to_the_session_holding_fewer(self):
        ts = [task("t-a", stage="deploy", heavy=True), task("t-b", owner="WEB", stage="plan", heavy=True)]
        procs = [{"code": "API", "heavy_n": 1, "children": []}, {"code": "WEB", "heavy_n": 0, "children": []}]
        d = scheduler.decide(ts, [sess("API"), sess("WEB")], res(heavy_running=1, sessions=procs), ST, NOW)
        self.assertGreater(row(d, "t-a")["score"], row(d, "t-b")["score"])
        self.assertEqual(d["start"], ["t-b"])
        self.assertEqual(row(d, "t-a")["blocked_by"], [{"kind": "resource", "ref": "max_heavy"}])
        [a] = kinds(d, "defer")
        self.assertEqual(a["to"], "API")

    def test_equal_holders_go_by_score_then_wait(self):
        ts = [task("t-a", stage="test", heavy=True), task("t-b", owner="WEB", stage="test", heavy=True, ago=9 * MIN),
              task("t-c", owner="OPS", stage="test", heavy=True, ago=3 * MIN)]
        sessions = [sess("API"), sess("WEB"), sess("OPS")]
        d = scheduler.decide(ts, sessions, res(heavy_running=1), ST, NOW)
        self.assertEqual(d["start"], ["t-b"])                              # one free slot: the longest wait

    def test_aging_lifts_a_waiting_low_priority_task(self):
        old = task("t-old", stage="build", priority=0, ago=10 * 60 * MIN)
        new = task("t-new", stage="build", priority=1, ago=MIN)
        d = scheduler.decide([old, new], [sess("API")], res(), ST, NOW)
        self.assertEqual(row(d, "t-old")["score"], row(d, "t-new")["score"])   # +10 aging = +1 priority
        self.assertEqual(d["start"], ["t-old"])
        fresh = scheduler.decide([dict(old, updated_at=NOW - MIN, created_at=NOW - MIN), new], [sess("API")], res(), ST, NOW)
        self.assertEqual(fresh["start"], ["t-new"])


class Stuck(unittest.TestCase):
    def test_stuck_levels_and_progress(self):
        for ago, want in ((10 * MIN, None), (50 * MIN, "warn"), (100 * MIN, "high")):
            d = scheduler.decide([task("t-a", ago=ago)], [sess("API", "working")], res(), ST, NOW)
            self.assertEqual(row(d, "t-a").get("stuck"), want)
            self.assertEqual([s["severity"] for s in d["stuck"]], [want] if want else [])
        t = task("t-a", ago=100 * MIN, progress_at=NOW - MIN)
        self.assertEqual(scheduler.decide([t], [sess("API", "working")], res(), ST, NOW)["stuck"], [])

    def test_waiting_on_the_human_is_not_stuck(self):
        d = scheduler.decide([task("t-a", ago=100 * MIN)], [sess("API", "needs-you")], res(), ST, NOW)
        self.assertEqual(d["stuck"], [])

    def test_stall_minutes_setting(self):
        st = scheduler.settings({"schedule": {"stall_minutes": [5, 10]}})
        d = scheduler.decide([task("t-a", ago=7 * MIN)], [sess("API", "working")], res(), st, NOW)
        self.assertEqual(d["stuck"][0]["severity"], "warn")


class Repo(unittest.TestCase):
    def test_finish_first(self):
        ts = [task("t-a", repo="shop", stage="test", pr={"url": "https://example.test/1", "state": "open"}),
              task("t-b", owner="WEB", repo="shop"), task("t-c", owner="OPS", repo="other")]
        d = scheduler.decide(ts, [sess("API", "working"), sess("WEB"), sess("OPS")], res(), ST, NOW)
        [a] = kinds(d, "finish-first")
        self.assertEqual((a["to"], a["task"]), ("WEB", "t-b"))
        self.assertIn("t-a", a["text"])
        off = scheduler.settings({"schedule": {"one_pr_per_repo": False}})
        self.assertEqual(kinds(scheduler.decide(ts, [sess("API"), sess("WEB")], res(), off, NOW), "finish-first"), [])

    def test_rebase_after_squash(self):
        x = task("t-x", owner="WEB", repo="shop", branch="feat/a", base="main", status="done",
                 pr={"url": "https://example.test/1", "state": "merged", "method": "squash", "head_sha": "abc1234"})
        y = task("t-y", repo="shop", branch="feat/b", base="feat/a")
        d = scheduler.decide([x, y], [sess("API", "working")], res(), ST, NOW)
        [a] = kinds(d, "rebase-needed")
        self.assertIn("git rebase --onto origin/main abc1234 feat/b", a["text"])
        self.assertIn("--force-with-lease", a["text"])
        x["pr"]["method"] = "merge"
        self.assertEqual(kinds(scheduler.decide([x, y], [sess("API")], res(), ST, NOW), "rebase-needed"), [])


class Invariants(unittest.TestCase):
    def scenario(self, seed):
        rnd = random.Random(seed)
        owners = ["API", "WEB", "OPS"]
        ts = []
        for n in range(8):
            ts.append(task("t-%d" % n, owner=rnd.choice(owners + ["GONE"]),
                           stage=rnd.choice(["plan", "build", "test", "deploy", "production"]),
                           ago=rnd.randint(1, 600) * MIN, heavy=rnd.random() < .5, priority=rnd.randint(0, 3),
                           repo="shop", branch=rnd.choice(["deploy-fix", "approve-x", "feat/%d" % n]),
                           base=rnd.choice(["deploy-fix", "main"]),
                           status=rnd.choice(["active", "active", "paused", "done"]),
                           pr={"url": "https://example.test/%d" % n, "state": rnd.choice(["open", "merged"]),
                               "method": "squash", "head_sha": "abc1234"}))
        procs = [{"code": c, "heavy_n": 1, "children": [
            {"class": "test", "comm": rnd.choice(["deploy", "[BOARD", "pytest"]), "pid": i + 1, "rss_mb": 500 + i,
             "heavy": True, "via": rnd.choice([None, "approver"])}]} for i, c in enumerate(owners)]
        sessions = [sess(c, rnd.choice(["working", "done", "rest", "needs-you", "offline"]), rnd.randint(0, 90) * MIN)
                    for c in owners]
        return ts, sessions, res(rnd.choice(scheduler.LEVELS), heavy_running=rnd.randint(0, 3), sessions=procs)

    def test_text_never_approves_and_production_never_runs(self):
        allowed = {"defer", "resume", "finish-first", "rebase-needed", "stop-own-heavy"}
        for seed in range(200):
            d = scheduler.decide(*self.scenario(seed), ST, NOW)
            for a in d["advisories"]:
                self.assertIn(a["kind"], allowed)
                self.assertNotRegex(a["text"], r"(?i)approv|deploy|\[BOARD|decision")
            for r in d["queue"]:
                if r["state"] in ("run", "ready"):
                    t = next(t for t in self.scenario(seed)[0] if t["id"] == r["task"])
                    self.assertNotEqual(t["stage"], "production")
            self.assertEqual(len({a["id"] for a in d["advisories"]}), len(d["advisories"]))

    def test_deterministic_and_order_independent(self):
        for seed in range(50):
            ts, sessions, r = self.scenario(seed)
            a = scheduler.decide(ts, sessions, r, ST, NOW)
            b = scheduler.decide(list(reversed(ts)), list(reversed(sessions)), json.loads(json.dumps(r)), ST, NOW)
            self.assertEqual(a, b)
            self.assertEqual(a["digest"], scheduler.digest(a))


class Step(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(self.home, "resources.json"), "w") as fh:
            json.dump(res("ok"), fh)

    def test_writes_only_on_change_mode_0600(self):
        ts, ss = [task("t-a")], [sess("API")]
        doc, wrote = scheduler.step({}, self.home, ss, ts, NOW)
        path = os.path.join(self.home, "scheduler.json")
        self.assertTrue(wrote)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["generated_at"], NOW)
        self.assertEqual(on_disk["start"], ["t-a"])
        for later in (NOW + 3000, NOW + 4 * MIN):                          # no heartbeat
            self.assertFalse(scheduler.step({}, self.home, ss, ts, later)[1])
        with open(path) as fh:
            self.assertEqual(json.load(fh)["generated_at"], NOW)
        ss[0]["state"] = "working"
        self.assertTrue(scheduler.step({}, self.home, ss, ts, NOW + 4 * MIN)[1])

    def test_disabled_writes_an_off_doc(self):
        doc, _ = scheduler.step({"schedule": {"enabled": False}}, self.home, [sess("API")], [task("t-a")], NOW)
        self.assertEqual((doc["enabled"], doc["mode"], doc["queue"]), (False, "off", []))

    def test_show(self):
        import io
        scheduler.step({}, self.home, [sess("API")], [task("t-a")], NOW)
        out = io.StringIO()
        scheduler.show([], self.home, out)
        self.assertIn("start: t-a", out.getvalue())
        self.assertIn("ready", out.getvalue())


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[sys.argv[0]])
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
