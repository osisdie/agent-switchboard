#!/usr/bin/env python3
"""tests/test_advisories.py — scheduler PR 2a: usage modes (conserve, wait-reset), jam conditions
J1-J8 and their episodes, advisory delivery (shadow mode, kinds, `to`, re-advise, the hourly cap,
quiet hours), the [BOARD schedule] text, the collector step with schedule-state.json, and the
jam:* alerts.

  python3 tests/test_advisories.py [-v]
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

TMP = tempfile.mkdtemp(prefix="switchboard-adv-test.")
os.environ.update({"BOARD_HOME": os.path.join(TMP, "home"), "BOARD_CONFIG_DIR": os.path.join(TMP, "config")})
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "LINE_", "AWS_")):
        os.environ.pop(k)

import advisories  # noqa: E402
import scheduler  # noqa: E402

NOW = 1_800_000_000_000
MIN = 60 * 1000
HOUR = 60 * MIN
ST = scheduler.settings({})


def task(i, owner="API", stage="build", ago=MIN, **kw):
    t = {"id": i, "owner": owner, "title": "x", "stage": stage, "artifacts": {}, "gates": [],
         "created_at": NOW - ago, "updated_at": NOW - ago}
    t.update(kw)
    return t


def sess(code, state="done", ago=0, **kw):
    return dict({"code": code, "aliases": [code], "state": state, "updated_at": NOW - ago}, **kw)


def res(level="ok", **kw):
    return dict({"at": NOW, "level": level, "mem": {"total_mb": 32768}, "heavy_running": 0, "sessions": []}, **kw)


def usage(p5=10, pw=10, r5=NOW + 2 * HOUR, rw=NOW + 3 * 24 * HOUR, at=NOW):
    return {"five_hour": {"pct": p5, "resets_at": r5}, "seven_day": {"pct": pw, "resets_at": rw}, "updated_at": at}


def decide(ts, ss, r=None, ctx=None, st=ST):
    return scheduler.decide(ts, ss, r if r is not None else res(), st, NOW, ctx or {})


def conds(doc, rule):
    return [c for c in doc["conditions"] if c["rule"] == rule]


class Modes(unittest.TestCase):
    def mode(self, **ctx):
        return decide([task("t-a")], [sess("API")], ctx=ctx)

    def test_modes(self):
        cases = [({}, "normal", None),
                 ({"limit": {"hit_at": NOW, "resets_at": NOW + HOUR}}, "wait-reset", "limit"),
                 ({"usage": usage(p5=96)}, "wait-reset", "5h"),
                 ({"usage": usage(p5=86)}, "conserve", "5h"),
                 ({"usage": usage(p5=50), "burn": {"status": "danger", "hit_at": NOW + 30 * MIN}}, "conserve", "burn"),
                 ({"usage": usage(p5=50), "burn": {"status": "danger", "hit_at": NOW + 110 * MIN}}, "normal", None),
                 ({"usage": usage(pw=80, rw=NOW + 5 * 24 * HOUR)}, "conserve", "weekly"),   # 2/7 elapsed, 80% used
                 ({"usage": usage(pw=40, rw=NOW + 5 * 24 * HOUR)}, "normal", None),
                 ({"usage": usage(p5=99, at=NOW - 20 * MIN)}, "normal", None)]               # stale usage
        for ctx, mode, why in cases:
            d = self.mode(**ctx)
            self.assertEqual((d["mode"], (d["usage"] or {}).get("why")), (mode, why), ctx)

    def test_wait_reset_holds_new_starts_only(self):
        ts = [task("t-run", ago=MIN), task("t-new", owner="WEB")]
        ctx = {"limit": {"hit_at": NOW, "resets_at": NOW + HOUR}}
        d = decide(ts, [sess("API", "working"), sess("WEB")], ctx=ctx)
        rows = {r["task"]: r for r in d["queue"]}
        self.assertEqual(rows["t-run"]["state"], "run")
        self.assertEqual((rows["t-new"]["state"], rows["t-new"]["blocked_by"]), ("deferred", [{"kind": "reset", "ref": NOW + HOUR}]))
        self.assertEqual(d["start"], [])
        [a] = [a for a in d["advisories"] if a["kind"] == "wait-for-reset"]
        self.assertEqual((a["to"], a["task"]), ("WEB", "t-new"))

    def test_limit_past_its_reset_and_huge_times(self):
        late = {"limit": {"hit_at": NOW - 3 * HOUR, "resets_at": NOW - 11 * MIN}}
        self.assertEqual(self.mode(**late)["mode"], "normal")
        self.assertEqual(self.mode(limit={"hit_at": NOW, "resets_at": NOW - 9 * MIN})["mode"], "wait-reset")
        d = decide([task("t-a")], [sess("API")], ctx={"limit": {"hit_at": NOW, "resets_at": 1e20}})
        self.assertEqual(d["mode"], "wait-reset")
        self.assertIn("before the reset", [a for a in d["advisories"] if a["kind"] == "wait-for-reset"][0]["text"])

    def test_conserve_prefers_nearly_finished_work(self):
        ts = [task("t-plan", stage="plan", priority=3), task("t-test", stage="test", priority=0)]
        self.assertEqual(decide(ts, [sess("API")])["start"], ["t-plan"])
        self.assertEqual(decide(ts, [sess("API")], ctx={"usage": usage(p5=90)})["start"], ["t-test"])


class Jams(unittest.TestCase):
    def test_j1_everyone_waits_and_answer_first(self):
        g = lambda d: [{"gate": "owner", "status": "pending", "decision_id": d}]  # noqa: E731
        ts = [task("t-a", gates=g("d-1-a")), task("t-b", owner="WEB", gates=g("d-1-a")),
              task("t-c", owner="OPS", gates=g("d-2-b")), task("t-d", owner="OPS", ago=9 * MIN, depends_on=[{"task": "t-c"}])]
        ss = [sess("API", "needs-you"), sess("WEB", "done"), sess("OPS", "done")]
        d = decide(ts, ss)
        [j] = conds(d, "J1")
        self.assertEqual((j["severity"], j["decision"], j["hold_ms"]), ("warn", "d-1-a", 10 * MIN))
        self.assertEqual(d["answer_first"][0], {"decision": "d-1-a", "unblocks_sessions": 2, "unblocks_tasks": ["t-a", "t-b"]})
        self.assertEqual(d["answer_first"][1]["unblocks_tasks"], ["t-c", "t-d"])
        self.assertEqual(conds(decide(ts, ss, ctx={"away": True}), "J1")[0]["severity"], "info")
        ts.append(task("t-free", owner="QA"))
        self.assertEqual(conds(decide(ts, ss + [sess("QA")]), "J1"), [])
        ts[-1] = task("t-other", owner="WEB", ago=30 * MIN)           # WEB could take another task
        self.assertEqual(conds(decide(ts, ss), "J1"), [])

    def test_j2_cycle(self):
        ts = [task("t-a", depends_on=[{"task": "t-b"}]), task("t-b", owner="WEB", depends_on=[{"task": "t-a"}])]
        [j] = conds(decide(ts, [sess("API"), sess("WEB")]), "J2")
        self.assertEqual((j["severity"], j["tasks"], j["sessions"]), ("high", ["t-a", "t-b"], ["API", "WEB"]))

    def test_j3a_same_branch_and_j3b_two_prs(self):
        ss = [sess("API", "working", repo="shop", branch="feat/x"), sess("WEB", "done", repo="shop", branch="feat/x"),
              sess("OPS", "working", repo="shop", branch="main"), sess("QA", "working", repo="shop", branch="main"),
              sess("T1", "working", repo="shop", branch="detached"), sess("T2", "working", repo="shop", branch="detached"),
              sess("API/helper", "working", repo="shop", branch="feat/x")]
        [j] = conds(decide([], ss), "J3a")                       # default, detached and odd codes skipped
        self.assertEqual((j["sessions"], j["evals"], j["alert"]), (["API", "WEB"], 1, None))   # board-only
        pr = {"url": "https://example.test/1", "state": "open"}
        ts = [task("t-a", repo="shop", pr=pr), task("t-b", owner="WEB", repo="shop", pr=pr)]
        [j] = conds(decide(ts, [sess("API"), sess("WEB")]), "J3b")
        self.assertEqual(j["tasks"], ["t-a", "t-b"])

    def test_j4_j5_j6_j7(self):
        d = decide([task("t-a", ago=100 * MIN), task("t-b", owner="WEB")],
                   [sess("API", "working"), sess("WEB", "rest", 40 * MIN)])
        self.assertEqual(sorted((c["id"], c["severity"], c["alert"]) for c in conds(d, "J4")),
                         [("j-J4-API", "high", "high"), ("j-J4-WEB-idle", "info", None)])
        self.assertEqual([(c["alert"], c["evals"]) for c in conds(decide([], [], res("critical")), "J5")], [(None, 2)])
        self.assertEqual([c["evals"] for c in conds(decide([], [], res("ok", oom_recent=True)), "J5")], [1])
        self.assertEqual([c["text"] for c in conds(decide([], [], res("ok", oom_recent=True)), "J5")],
                         ["Memory is critical after an OOM kill."])
        self.assertEqual([c["text"] for c in conds(decide([], [], res("critical")), "J5")], ["Resources are critical."])
        self.assertEqual(len(conds(decide([], [], ctx={"operator_stale": True}), "J6")), 1)
        ss = [sess("API", "working"), sess("WEB", "working")]
        [j] = conds(decide([], ss, ctx={"usage": usage(p5=90, r5=NOW + HOUR)}), "J7")
        self.assertEqual(j["id"], "j-J7-%d" % (NOW + HOUR))
        self.assertEqual(conds(decide([], ss[:1], ctx={"usage": usage(p5=90)}), "J7"), [])


class Episodes(unittest.TestCase):
    def c(self, rule="J2", evals=2, hold=0):
        return scheduler._cond(rule, "x", "high", ["API"], "text", hold_ms=hold, evals=evals)  # noqa: SLF001

    def test_open_after_two_close_after_two(self):
        st, op, _ = advisories.episodes({}, [self.c()], NOW)
        self.assertEqual(op, [])
        st, op, _ = advisories.episodes(st, [self.c()], NOW + 3000)
        self.assertEqual([j["id"] for j in op], ["j-J2-x"])
        st2, op, res_ = advisories.episodes(st, [], NOW + 6000)
        self.assertEqual(([j["id"] for j in op], res_), (["j-J2-x"], []))
        st3, op, res_ = advisories.episodes(st2, [], NOW + 9000)
        self.assertEqual(op, [])
        self.assertEqual((res_[0]["id"], res_[0]["lost_min"]), ("j-J2-x", 0))
        st4, _, _ = advisories.episodes(st, [], NOW + 9000)
        self.assertEqual(advisories.episodes(st4, [self.c()], NOW + 9500)[1][0]["id"], "j-J2-x")  # a blip keeps it open

    def test_immediate_and_held(self):
        self.assertEqual(len(advisories.episodes({}, [self.c(evals=1)], NOW)[1]), 1)
        st, op, _ = advisories.episodes({}, [self.c(hold=10 * MIN)], NOW)
        st, op, _ = advisories.episodes(st, [self.c(hold=10 * MIN)], NOW + 5 * MIN)
        self.assertEqual(op, [])
        self.assertEqual(len(advisories.episodes(st, [self.c(hold=10 * MIN)], NOW + 10 * MIN)[1]), 1)

    def test_state_is_stable_while_nothing_changes(self):
        st, _, _ = advisories.episodes({}, [self.c()], NOW)
        st, _, _ = advisories.episodes(st, [self.c()], NOW + 3000)
        self.assertEqual(advisories.episodes(st, [self.c()], NOW + 6000)[0], st)


class Delivery(unittest.TestCase):
    def advs(self):
        return [{"id": "a-%012x" % n, "to": to, "kind": kind, "task": None, "severity": sev, "text": "t"}
                for n, (to, kind, sev) in enumerate([("API", "defer", "warn"), ("API", "resume", "info"),
                                                     ("WEB", "stop-own-heavy", "high"), ("X/y", "defer", "warn"),
                                                     ("GONE", "defer", "warn")])]

    def run_(self, sent=None, batches=None, st=ST, quiet=False, paused=False):
        a = self.advs()
        due = advisories.deliver(a, sent or {}, batches or {}, st, {"API", "WEB"}, NOW, quiet, paused)
        return due, [x["delivery"]["state"] for x in a]

    def test_kinds_codes_and_shadow(self):
        self.assertEqual(self.run_(), ({"API": ["a-000000000000"], "WEB": ["a-000000000002"]},
                                       ["due", "board", "due", "board", "board"]))
        shadow = scheduler.settings({"schedule": {"advise": False}})
        self.assertEqual(self.run_(st=shadow), ({}, ["shadow", "board", "shadow", "board", "board"]))
        self.assertEqual(self.run_(paused=True)[0], {})

    def test_readvise_reply_cap_quiet(self):
        sent = {"a-000000000000": {"to": "API", "sent_at": NOW - 10 * MIN, "n": 1, "reply": None}}
        self.assertEqual(self.run_(sent=sent)[1][0], "sent")
        sent["a-000000000000"]["sent_at"] = NOW - 61 * MIN
        self.assertEqual(self.run_(sent=sent)[1][0], "due")
        sent["a-000000000000"]["reply"] = "cannot"
        self.assertEqual(self.run_(sent=sent)[1][0], "sent")
        self.assertEqual(self.run_(batches={"API": [NOW - n * MIN for n in range(4)]})[1][0], "held")
        self.assertEqual(self.run_(batches={"API": [NOW - 70 * MIN] * 4})[1][0], "due")
        self.assertEqual(self.run_(quiet=True)[1][:3], ["held", "board", "due"])     # high still goes
        a = self.advs()
        a[0].update(severity="high", raised_from="warn")                # raised by a lesson: quiet still holds it
        advisories.deliver(a, {}, {}, ST, {"API", "WEB"}, NOW, quiet=True)
        self.assertEqual(a[0]["delivery"]["state"], "held")

    def test_offline_held_and_send_cap(self):
        self.assertEqual(self.run_()[1][:3], ["due", "board", "due"])
        a = self.advs()
        advisories.deliver(a, {}, {}, ST, {"API", "WEB"}, NOW, live={"WEB"})
        self.assertEqual([x["delivery"]["state"] for x in a][:3], ["held", "board", "due"])   # API offline
        sent = {"a-000000000000": {"to": "API", "sent_at": NOW - 61 * MIN, "n": 3, "reply": None}}
        self.assertEqual(self.run_(sent=sent)[1][0], "board")                    # sent 3 times: board-only

    def test_j8(self):
        a = [{"id": "a-000000000009", "to": "WEB", "kind": "rebase-needed", "task": "t-x", "severity": "warn", "text": "t"}]
        sent = {"a-000000000009": {"to": "WEB", "sent_at": NOW - 61 * MIN, "reply": "ack"}}
        [c] = advisories.j8(a, sent, NOW)
        self.assertEqual((c["rule"], c["alert"]), ("J8", None))
        sent["a-000000000009"]["reply"] = "done"
        self.assertEqual(advisories.j8(a, sent, NOW), [])


class Hostile(unittest.TestCase):
    BAD = ["A\r\nB", "x\x1b[31mred", "o\x1b]8;;http://e.test\x07l", "\x9bz"]

    def test_control_characters_never_reach_texts_ids_or_keys(self):
        import notify
        for bad in self.BAD:
            ts = [task("t-a", owner=bad, repo=bad, branch=bad, gates=[{"status": "pending", "decision_id": bad}],
                       pr={"url": "https://e.test/1", "state": "open"}),
                  task("t-b", owner="API", repo="shop", ago=100 * MIN,
                       gates=[{"status": "pending", "decision_id": "d-1-ok"}])]
            ss = [sess(bad, "working", repo=bad, branch=bad), sess("API", "working", repo="shop", branch="feat/a"),
                  sess("API" + bad, "rest", 40 * MIN)]
            d = decide(ts, ss, res("critical", oom_recent=True), {"usage": usage(p5=90)})
            def strings(x):
                if isinstance(x, dict):
                    return [t for k, v in x.items() for t in [str(k)] + strings(v)]
                if isinstance(x, list):
                    return [t for v in x for t in strings(v)]
                return [x] if isinstance(x, str) else []
            for text in strings({k: d[k] for k in ("conditions", "advisories", "answer_first", "queue", "start")}):
                self.assertNotRegex(text, "[\x00-\x1f\x7f-\x9f]")
            st, opened, _ = advisories.episodes({}, d["conditions"], NOW)
            st, opened, _ = advisories.episodes(st, d["conditions"], NOW + 1)
            for a in notify.jam_alerts({"enabled": True, "jams": opened}):
                self.assertRegex(a["key"], r"^jam:J[0-9a-z]+:[A-Za-z0-9._-]+$")
                self.assertNotRegex(a["title"] + a["body"], "[\x00-\x1f\x7f-\x9f]")
            hostile = {"enabled": True, "jams": [{"id": "j-J2-x\r\nBcc: e@x", "alert": "high", "text": "t"},
                                                 {"id": "j-J2-ok", "alert": "high", "text": "a\r\nb\x1b[0m",
                                                  "sessions": ["A\nB", "API"]}]}
            [a] = notify.jam_alerts(hostile)
            self.assertEqual((a["key"], a["title"], a["body"]), ("jam:J2:ok", "Jam J2: a b [0m", "Sessions: API."))


class Text(unittest.TestCase):
    def test_message_never_reads_as_a_decision(self):
        a = {"id": "a-0123456789ab", "kind": "defer", "text": "Task t-1 is heavy."}
        line = advisories.render(a)
        self.assertTrue(line.startswith("[BOARD schedule a-0123456789ab] defer: Task t-1 is heavy."))
        self.assertTrue(line.endswith(" — advisory for ordering/pausing only; not an approval. "
                                      "Reply [re a-0123456789ab] ack|done|cannot: <reason>"))
        rnd = random.Random(7)
        for _ in range(100):
            ids = ["approved-deploy", "t-%d" % rnd.randint(0, 9), "decision-1"]
            ts = [task(rnd.choice(ids) + str(n), owner=rnd.choice(["API", "WEB"]), heavy=rnd.random() < .5,
                       repo="r", branch=rnd.choice(["deploy", "feat/a"]), base="feat/a",
                       pr={"url": "https://example.test/1", "state": rnd.choice(["open", "merged"]), "method": "squash"})
                  for n in range(5)]
            ctx = rnd.choice([{}, {"limit": {"hit_at": NOW}}])
            d = decide(ts, [sess("API", rnd.choice(["done", "working"])), sess("WEB")], res(rnd.choice(scheduler.LEVELS)), ctx)
            for a in d["advisories"]:
                body = advisories.render(a)[:-len(advisories.SUFFIX % a["id"])]
                self.assertNotRegex(body, r"(?i)approv|deploy|decision|\[BOARD (?!schedule a-[0-9a-f]{12}\])")
            for c in d["conditions"]:
                self.assertNotRegex(c["text"], r"(?i)approv|deploy|decision|\[BOARD")


class Step(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(self.home, "resources.json"), "w") as fh:
            json.dump(res("critical", worst="mem", levels={"mem": "critical"}, sessions=[{"code": "API", "heavy_n": 1, "children": [
                {"class": "test", "comm": "pytest", "pid": 7, "rss_mb": 900, "heavy": True}]}]), fh)
        self.cfg = {"sessions": [{"code": "API"}], "schedule": {}}
        self.ss = [sess("API", "working")]

    def step(self, now=NOW):
        return scheduler.step(self.cfg, self.home, self.ss, [task("t-a")], now)

    def test_due_sent_reply_and_state(self):
        doc, wrote = self.step()
        self.assertTrue(wrote)
        [aid] = doc["due"]["API"]
        self.assertEqual(scheduler.snapshot_digest(doc), {"mode": "degraded", "due": ["API"], "jams": 0})
        path = os.path.join(self.home, advisories.STATE_FILE)
        doc, _ = self.step(NOW + 3000)
        self.assertEqual([j["rule"] for j in doc["jams"]], ["J5"])           # critical: 2 samples
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        def text():
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        before = text()
        self.assertFalse(self.step(NOW + 6000)[1])                           # nothing new: no writes at all
        self.assertEqual(text(), before)
        env = dict(os.environ, BOARD_HOME=self.home, BOARD_NOW_MS=str(NOW + 7000))
        import subprocess
        run = lambda *a: subprocess.run([sys.executable, os.path.join(REPO, "lib", "advisories.py")] + list(a),  # noqa: E731
                                        env=env, capture_output=True, text=True)
        out = run("outbox", "--json")
        self.assertIn("[BOARD schedule %s] stop-own-heavy:" % aid, json.loads(out.stdout)["API"]["text"])
        self.assertEqual(run("sent", aid).returncode, 0)
        self.assertEqual(json.loads(run("outbox", "--json").stdout), {})
        self.assertFalse(self.step(NOW + 8000)[1])                           # the collector agrees with `sent`
        self.assertEqual(run("reply", aid, "--from", "WEB", "ack").returncode, 4)
        self.assertEqual(run("reply", "a-ffffffffffff", "--from", "API", "ack").returncode, 1)
        self.assertEqual(run("reply", aid, "--from", "API", "done").returncode, 0)
        doc, _ = self.step(NOW + 9000)
        self.assertEqual(doc["advisories"][0]["delivery"]["reply"], "done")
        self.assertEqual(run("off", "--by", "owner").returncode, 0)
        doc, _ = self.step(NOW + 10000)
        self.assertEqual((doc["mode"], doc.get("paused"), doc["due"]), ("off", True, {}))
        self.assertEqual(scheduler.snapshot_digest(doc), {"mode": "off", "due": [], "jams": 0})
        run("on", "--by", "owner")
        self.assertEqual(self.step(NOW + 11000)[0]["mode"], "degraded")

    def write_res(self, **kw):
        kw.setdefault("sessions", [{"code": "API", "heavy_n": 1, "children": [
            {"class": "test", "comm": "vitest", "pid": 7, "rss_mb": 900, "heavy": True}]}])
        with open(os.path.join(self.home, "resources.json"), "w") as fh:
            json.dump(res("critical", **kw), fh)

    def test_load_critical_sends_no_stop(self):
        def load(x):
            return {"worst": "load", "levels": {"mem": "ok", "load": "critical"}, "cpu": {"ncpu": 4, "load_per_cpu": x},
                    "mem": {"total_mb": 32768, "available_pct": 53.0, "psi_some_avg60": 1.0}, "swap": {"used_pct": 49}}
        self.write_res(**load(4.86))
        doc, _ = self.step()
        self.assertEqual(doc["mode"], "degraded")
        self.assertEqual([a["kind"] for a in doc["advisories"]], [])          # memory is fine: nobody is asked to stop
        self.assertEqual(doc["due"], {})
        doc, _ = self.step(NOW + 3000)
        [j5] = doc["jams"]
        self.assertEqual(j5["text"], "CPU load is critical (above 3.0 per CPU).")
        for n, x in enumerate((4.98, 5.01, 4.48, 4.52, 5.03, 4.49)):          # jitter at any edge: no write
            self.write_res(**load(x))
            self.assertFalse(self.step(NOW + 4000 + n * 1000)[1], x)
        self.write_res(worst="mem", levels={"mem": "critical", "load": "critical"}, cpu={"load_per_cpu": 4.62},
                       mem={"total_mb": 32768, "available_pct": 4.2, "psi_some_avg60": 30})
        doc, _ = self.step(NOW + 9000)
        [a] = doc["advisories"]
        self.assertEqual((a["kind"], a["to"], a["pid"]), ("stop-own-heavy", "API", 7))
        self.assertTrue(a["text"].startswith("Memory is critical (available below 8%): if you started"))
        self.assertEqual(doc["due"], {"API": [a["id"]]})

    def cli(self, now, *a):
        import subprocess
        env = dict(os.environ, BOARD_HOME=self.home, BOARD_NOW_MS=str(now))
        return subprocess.run([sys.executable, os.path.join(REPO, "lib", "advisories.py")] + list(a),
                              env=env, capture_output=True, text=True)

    def write_mem(self, pid=None, at=NOW):
        self.write_res(at=at, worst="mem", levels={"mem": "critical"}, mem={"total_mb": 32768, "available_pct": 4},
                       sessions=[{"code": "API", "heavy_n": 1, "children": [
                           {"class": "test", "comm": "vitest", "pid": pid, "rss_mb": 900, "heavy": True, "via": "qa"}]}]
                       if pid else [])

    def test_sent_after_the_process_exited(self):
        self.write_mem(pid=7)
        doc, _ = self.step()
        [aid] = doc["due"]["API"]
        out = json.loads(self.cli(NOW + 1000, "outbox", "API", "--json").stdout)
        self.assertEqual(out["API"]["ids"], [aid])
        self.write_mem(pid=None)                                              # the test run ended
        doc, _ = self.step(NOW + 2000)
        self.assertEqual((doc["advisories"], doc["due"]), ([], {}))
        r = self.cli(NOW + 3000, "sent", aid)
        self.assertEqual(r.returncode, 0, r.stderr)
        st = advisories.load_state(self.home)
        self.assertEqual((st["sent"][aid]["to"], st["sent"][aid]["kind"], st["batches"]["API"]),
                         ("API", "stop-own-heavy", [NOW + 3000]))             # counts toward the cap
        self.assertEqual(st["printed"], {})
        self.assertEqual(self.cli(NOW + 3500, "sent", aid).returncode, 1)     # once
        self.step(NOW + 4000)
        self.assertEqual(self.cli(NOW + 5000, "reply", aid, "--from", "API", "cannot").returncode, 0)
        self.assertEqual(advisories.load_state(self.home)["sent"][aid]["reply"], "cannot")
        self.assertEqual(self.cli(NOW + 5000, "sent", "a-ffffffffffff").returncode, 1)   # never printed

    def test_off_never_waits_for_the_lock(self):
        import subprocess
        self.write_mem(pid=7)
        doc, _ = self.step()
        [aid] = doc["due"]["API"]
        self.cli(NOW + 1000, "outbox", "--json")
        env = dict(os.environ, BOARD_HOME=self.home, BOARD_NOW_MS=str(NOW + 2000), BOARD_SCHEDULE_LOCK_WAIT="0.3")
        run = lambda *a: subprocess.run([sys.executable, os.path.join(REPO, "lib", "advisories.py")] + list(a),  # noqa: E731
                                        env=env, capture_output=True, text=True)
        with advisories.locked(self.home, wait_s=1):                         # a stuck collector step
            r = run("off", "--by", "owner")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(advisories.paused(self.home))
            self.assertIn("busy", r.stderr)
            r = run("on", "--by", "owner")
            self.assertEqual(r.returncode, 8)                                 # on waits: stays off
            self.assertTrue(advisories.paused(self.home))
        self.assertIn(aid, advisories.load_state(self.home)["printed"])      # off could not clear it
        self.assertEqual(run("sent", aid).returncode, 1)                     # ... but off refuses sent
        self.assertEqual(run("on", "--by", "owner").returncode, 0)
        self.assertFalse(advisories.paused(self.home))
        self.assertEqual(advisories.load_state(self.home)["printed"], {})    # on clears it

    def test_sent_twice_in_one_call_counts_once(self):
        self.write_mem(pid=7)
        doc, _ = self.step()
        [aid] = doc["due"]["API"]
        self.cli(NOW + 1000, "outbox", "--json")
        self.assertEqual(self.cli(NOW + 1500, "sent", aid, aid).returncode, 0)
        st = advisories.load_state(self.home)
        self.assertEqual((st["sent"][aid]["n"], st["batches"]["API"]), (1, [NOW + 1500]))

    def test_done_then_a_new_episode_is_asked_again(self):
        self.write_mem(pid=7)
        doc, _ = self.step()
        [aid] = doc["due"]["API"]
        self.cli(NOW + 1000, "outbox", "--json")
        self.assertEqual(self.cli(NOW + 1500, "sent", aid).returncode, 0)
        self.assertEqual(self.cli(NOW + 2000, "reply", aid, "--from", "API", "done").returncode, 0)
        self.write_mem(pid=None)                                              # stopped: the advisory goes
        self.step(NOW + 3000)
        self.write_mem(pid=8)                                                 # re-run soon: within readvise
        doc, _ = self.step(NOW + 5 * MIN)
        self.assertEqual((doc["due"], doc["advisories"][0]["delivery"]["state"]), ({}, "sent"))
        rec = advisories.load_state(self.home)["sent"][aid]
        self.assertEqual((rec["reply"], rec["n"], rec["sent_at"], "gone_at" in rec), (None, 1, NOW + 1500, False))
        self.write_mem(pid=9, at=NOW + 1500 + HOUR)
        doc, _ = self.step(NOW + 1500 + HOUR)                                 # after readvise: asked again
        self.assertEqual(doc["due"], {"API": [aid]})

    def test_printed_too_long_ago_or_off_is_refused(self):
        self.write_mem(pid=7)
        doc, _ = self.step()
        [aid] = doc["due"]["API"]
        self.cli(NOW + 1000, "outbox", "--json")
        self.write_mem(pid=None)
        self.step(NOW + 2000)
        late = NOW + 1000 + advisories.PRINTED_MS + 1
        self.assertEqual(self.cli(late, "sent", aid).returncode, 1)
        self.assertEqual(self.cli(late, "reply", aid, "--from", "API", "ack").returncode, 1)
        self.write_mem(pid=8)
        doc, _ = self.step(NOW + 3000)
        self.assertEqual(doc["due"], {"API": [aid]})                         # same job, new pid: same id
        self.cli(NOW + 3100, "outbox", "--json")
        self.assertIn(aid, advisories.load_state(self.home)["printed"])
        self.cli(NOW + 3500, "off", "--by", "owner")
        self.assertEqual(advisories.load_state(self.home)["printed"], {})    # off forgets what was printed
        self.assertEqual(json.loads(self.cli(NOW + 4000, "outbox", "--json").stdout), {})
        self.assertEqual(advisories.load_state(self.home)["printed"], {})    # ... and records nothing
        self.assertEqual(self.cli(NOW + 4500, "sent", aid).returncode, 1)     # off: refused
        self.cli(NOW + 4600, "on", "--by", "owner")
        self.assertEqual(advisories.load_state(self.home)["printed"], {})

    def test_respawned_run_is_not_sent_again(self):
        self.write_mem(pid=7)
        doc, _ = self.step()
        [aid] = doc["due"]["API"]
        self.cli(NOW + 1000, "outbox", "--json")
        self.assertEqual(self.cli(NOW + 1500, "sent", aid).returncode, 0)
        for n, pid in enumerate((None, 8, None, 9, 10)):                    # the run re-spawns; gaps between
            self.write_mem(pid=pid)
            doc, _ = self.step(NOW + 2000 + n * MIN)
            self.assertEqual(doc["due"], {})
            if pid:
                self.assertEqual(([a["id"] for a in doc["advisories"]], doc["advisories"][0]["delivery"]["state"]),
                                 ([aid], "sent"))
        self.write_mem(pid=None)
        self.step(NOW + 10 * MIN)
        self.step(NOW + 10 * MIN + advisories.GONE_KEEP_MS)                  # gone long enough: forgotten
        self.assertNotIn(aid, advisories.load_state(self.home)["sent"])

    def test_off_gates_outbox_and_sent_at_once(self):
        import subprocess
        doc, _ = self.step()
        [aid] = doc["due"]["API"]
        env = dict(os.environ, BOARD_HOME=self.home, BOARD_NOW_MS=str(NOW + 1000), BOARD_SCHEDULE_LOCK_WAIT="0.3")
        run = lambda *a: subprocess.run([sys.executable, os.path.join(REPO, "lib", "advisories.py")] + list(a),  # noqa: E731
                                        env=env, capture_output=True, text=True)
        self.assertEqual(run("sent", "a-ffffffffffff").returncode, 1)          # not due: nothing recorded
        self.assertEqual(run("off", "--by", "owner").returncode, 0)
        self.assertEqual(json.loads(run("outbox", "--json").stdout), {})      # before any collector pass
        self.assertEqual(run("sent", aid).returncode, 1)
        run("on", "--by", "owner")
        with advisories.locked(self.home, wait_s=1):
            self.assertEqual(run("sent", aid).returncode, 8)
            self.assertEqual(run("reply", aid, "--from", "API", "ack").returncode, 8)
        self.assertEqual(run("sent", aid).returncode, 0)
        note = os.path.join(self.home, "note.txt")
        with open(note, "w") as fh:
            fh.write("busy\x1b[31m with\r\n tests " + "x" * 300)
        self.assertEqual(run("reply", aid, "--from", "API", "cannot", "--note-file", note).returncode, 0)
        rec = advisories.load_state(self.home)["sent"][aid]
        self.assertEqual((rec["reply"], rec["note"][:16], len(rec["note"])), ("cannot", "busy [31m with t", 200))

    def test_sync_jams_opens_and_resolves(self):
        import notify
        os.environ["BOARD_HOME"] = self.home
        try:
            path = os.path.join(self.home, "scheduler.json")
            with open(path, "w") as fh:
                json.dump({"enabled": True, "jams": [{"id": "j-J2-ab", "rule": "J2", "alert": "high", "text": "c",
                                                      "sessions": ["API"]}]}, fh)
            data = {}
            notify._sync_jams(data, NOW)                                     # noqa: SLF001
            self.assertEqual((data["jam:J2:ab"]["level"], data["jam:J2:ab"]["resolved"]), ("high", False))
            notify._sync_jams(data, NOW + 1)                                 # noqa: SLF001
            self.assertEqual(data["jam:J2:ab"]["first_at"], NOW)             # not reopened
            with open(path, "w") as fh:
                json.dump({"enabled": True, "jams": []}, fh)
            notify._sync_jams(data, NOW + 2)                                 # noqa: SLF001
            self.assertEqual((data["jam:J2:ab"]["resolved"], data["jam:J2:ab"]["acked_by"]), (True, "auto"))
        finally:
            os.environ["BOARD_HOME"] = os.path.join(TMP, "home")

    def test_jam_alerts(self):
        import notify
        doc = {"enabled": True, "jams": [
            {"id": "j-J2-ab12", "rule": "J2", "severity": "high", "alert": "high", "sessions": ["API"], "text": "cycle"},
            {"id": "j-J5-fleet", "rule": "J5", "severity": "high", "alert": None, "sessions": [], "text": "x"},
            {"id": "j-J4-API-idle", "rule": "J4", "severity": "info", "alert": None, "sessions": ["API"], "text": "x"}]}
        self.assertEqual([(a["key"], a["level"], a["escalate"]) for a in notify.jam_alerts(doc)],
                         [("jam:J2:ab12", "high", False)])
        self.assertEqual(notify.jam_alerts({"enabled": False, "jams": doc["jams"]}), [])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[sys.argv[0]])
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
