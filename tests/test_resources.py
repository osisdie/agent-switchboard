#!/usr/bin/env python3
"""tests/test_resources.py — offline tests for lib/resources.py (the resources sampler) and its
collector hook. Uses a fixture /proc tree (BOARD_PROC_ROOT), monkeypatched statvfs, a fake `ps`
on PATH and a fake clock; never reads the live machine's numbers and never touches
~/.agent-switchboard.

  python3 tests/test_resources.py [-v]
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

TMP = tempfile.mkdtemp(prefix="switchboard-resources-test.")
os.environ["BOARD_HOME"] = os.path.join(TMP, "home")
os.environ["BOARD_CONFIG_DIR"] = os.path.join(TMP, "config")
os.environ.pop("TMUX", None)
os.environ.pop("BOARD_NOW_MS", None)
os.makedirs(os.environ["BOARD_HOME"])
os.makedirs(os.environ["BOARD_CONFIG_DIR"])
open(os.path.join(os.environ["BOARD_CONFIG_DIR"], "fleet.yaml"), "w").close()

import resources as R  # noqa: E402

sys.path.insert(0, os.path.join(REPO, "tests"))
from fake_proc import FakeProc  # noqa: E402

T0 = 1794650400000  # 2026-11-14 10:00 UTC
GIB = 1024 * 1024 * 1024
SECRET = "verify-key-must-never-be-stored"


def slurp(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def paths_in(obj):
    """Every string in obj that looks like a filesystem path."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += paths_in(k) + paths_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out += paths_in(v)
    elif isinstance(obj, str) and (obj.startswith(("/", "~")) or ":/" in obj or "/" in obj):
        out.append(obj)
    return out


def teammate_argv(name, parent_sid, team="team-x"):
    return ["/opt/claude/bin/claude.exe", "--agent-id", "%s@%s" % (name, team), "--agent-name", name,
            "--team-name", team, "--parent-session-id", parent_sid, "--lead-verify-key", SECRET]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.fp = FakeProc(os.path.join(self.dir, "proc"))
        os.environ["BOARD_PROC_ROOT"] = self.fp.root
        self.state = {}
        self.cfg = R.settings({})
        self.cfg["disks"] = []
        self.sessions = [{"code": "CP", "pid": 100, "session_id": "sid-cp", "cwd": None, "aliases": ["CP"]},
                         {"code": "API", "pid": 300, "session_id": "sid-api", "cwd": None, "aliases": ["API", "api-main"]}]

    def tearDown(self):
        os.environ.pop("BOARD_PROC_ROOT", None)

    def tree(self):
        fp = self.fp
        fp.proc(1, 0, "init", ["/sbin/init"])
        fp.proc(50, 1, "tmux: server", ["tmux", "new-session"])
        fp.proc(100, 50, "claude.exe", ["/opt/claude/bin/claude.exe"], rss_mb=400, ticks=100)
        fp.proc(101, 100, "bash", ["/bin/bash", "-c", "python -m pytest -q"], rss_mb=4)
        fp.proc(102, 101, "python3", ["python3", "-m", "pytest", "-q"], rss_mb=900, ticks=50)
        fp.proc(103, 100, "node", ["node", "/x/node_modules/@playwright/mcp/cli.js"], rss_mb=80)
        fp.proc(104, 103, "chrome", ["/x/chromium/chrome", "--headless"], rss_mb=300)
        fp.proc(105, 104, "chrome", ["/x/chromium/chrome", "--type=renderer"], rss_mb=200)
        # Teammates run in tmux panes (ppid = tmux server), tied to CP by --parent-session-id.
        fp.proc(200, 50, "claude.exe", teammate_argv("busy-one", "sid-cp"), rss_mb=350, ticks=1000)
        fp.proc(201, 50, "claude.exe", teammate_argv("idle-one", "sid-cp"), rss_mb=300, ticks=500)
        fp.proc(202, 201, "bash", ["/bin/bash"], rss_mb=3)
        fp.proc(210, 50, "claude.exe", teammate_argv("lost-one", "sid-gone"), rss_mb=250, ticks=10)
        fp.proc(300, 50, "claude.exe", ["/opt/claude/bin/claude.exe"], rss_mb=380, ticks=10)
        fp.proc(301, 300, "cargo", ["cargo", "build", "--release"], rss_mb=1500)

    def run_sample(self, now):
        return R.sample(self.cfg, self.home, self.sessions, self.state, now)


class SystemMetrics(Base):
    def test_linux_metrics(self):
        self.tree()
        doc = self.run_sample(T0)
        self.assertEqual(doc["platform"], "linux")
        self.assertEqual(doc["mem"], {"total_mb": 16384, "available_mb": 8192, "available_pct": 50, "psi_some_avg60": 1.5})
        self.assertEqual(doc["swap"], {"total_mb": 4096, "used_pct": 0})
        self.assertEqual(doc["cpu"]["ncpu"], 4)
        self.assertEqual(doc["cpu"]["load"], [1.0, 0.8, 0.5])
        self.assertEqual(doc["cpu"]["load_per_cpu"], 0.25)
        self.assertIsNone(doc["cpu"]["pct"])  # no previous sample yet
        self.assertEqual(doc["oom_kills"], 2)
        self.assertEqual(doc["oom_kills_delta"], 0)
        self.assertEqual(doc["unavailable"], [])
        self.assertEqual(doc["level"], "ok")
        self.fp.cpu(busy=3000, idle=8000)  # busy 2000 -> 3000, idle unchanged: 100%
        doc = self.run_sample(T0 + 15000)
        self.assertEqual(doc["cpu"]["pct"], 100)

    def test_wsl_and_missing_metrics(self):
        self.fp.write("version", "Linux version 6.6.0-microsoft-standard-WSL2\n")
        os.remove(os.path.join(self.fp.root, "pressure", "memory"))
        self.fp.write("vmstat", "nr_free_pages 1\n")
        self.fp.mem(16384, 8192, swap_total_mb=0, swap_free_mb=0)
        doc = self.run_sample(T0)
        self.assertEqual(doc["platform"], "wsl")
        self.assertEqual(doc["unavailable"], ["oom", "psi", "swap"])
        self.assertIsNone(doc["mem"]["psi_some_avg60"])
        self.assertIsNone(doc["oom_kills"])
        self.assertNotIn("swap", doc["levels"])

    def test_swap_alone_is_not_pressure(self):
        self.fp.mem(16384, 8192, swap_total_mb=4096, swap_free_mb=400)   # 90% swap, 50% memory free
        for i in range(4):
            doc = self.run_sample(T0 + i * 15000)
        self.assertEqual(doc["levels"]["swap"], "ok")
        self.assertEqual(doc["level"], "ok")
        self.fp.mem(16384, 1500, swap_total_mb=4096, swap_free_mb=400)   # memory at warn: swap counts
        for i in range(4, 8):
            doc = self.run_sample(T0 + i * 15000)
        self.assertEqual(doc["levels"]["mem"], "warn")
        self.assertEqual(doc["levels"]["swap"], "critical")
        self.assertEqual(doc["worst"], "swap")

    def test_oom_counter_reset(self):
        self.fp.write("vmstat", "oom_kill 5\n")
        self.run_sample(T0)
        self.fp.write("vmstat", "oom_kill 1\n")                        # a reboot: the counter restarted
        doc = self.run_sample(T0 + 15000)
        self.assertEqual(doc["oom_kills_delta"], 1)
        self.assertTrue(doc["oom_recent"])

    def test_bad_threshold_does_not_stop_the_sampler(self):
        cfg = R.settings({"resources": {"thresholds": {"mem_available_pct": "abc", "swap_used_pct": [50, "x"]}}})
        self.assertEqual(cfg["thresholds"]["mem_available_pct"], [15.0, 8.0])
        self.assertEqual(len(cfg["config_errors"]), 2)
        cfg["disks"] = []
        doc = R.sample(cfg, self.home, [], {}, T0)
        self.assertEqual(doc["level"], "ok")
        self.assertEqual(len(doc["config_errors"]), 2)
        # and the raw evaluator skips a bad value instead of raising
        self.assertEqual(R.metric_candidate(5, ["x", 8], True, "ok"), "high")

    def test_bad_process_classes_do_not_stop_the_sampler(self):
        self.tree()
        for bad in (["test", "pytest"], "pytest", {"test": "("}, {"test": 5}):
            cfg = R.settings({"resources": {"process_classes": bad}})
            self.assertEqual(len(cfg["config_errors"]), 1, bad)
            self.assertIn("process_classes", cfg["config_errors"][0])
            cfg["disks"] = []
            doc = R.sample(cfg, self.home, self.sessions, {}, T0)
            cp = next(x for x in doc["sessions"] if x["code"] == "CP")
            self.assertEqual({c["pid"]: c["class"] for c in cp["children"]}[102], "test")  # defaults still apply
            self.assertEqual(doc["config_errors"], cfg["config_errors"])
        self.assertEqual(R.compile_classes("x")[0][0], "test")  # the raw compiler tolerates it too
        ok = R.settings({"resources": {"process_classes": {"container": r"\bpython3\b"}}})
        self.assertEqual((ok["process_classes"], ok["config_errors"]), ({"container": r"\bpython3\b"}, []))

    def test_oom_delta_is_critical(self):
        self.run_sample(T0)
        self.fp.write("vmstat", "oom_kill 3\n")
        doc = self.run_sample(T0 + 15000)
        self.assertEqual(doc["oom_kills_delta"], 1)
        self.assertTrue(doc["oom_recent"])
        self.assertEqual((doc["level"], doc["worst"]), ("critical", "oom"))
        doc = self.run_sample(T0 + 30000)
        self.assertEqual(doc["oom_kills_delta"], 0)
        self.assertEqual(doc["level"], "critical")  # held for OOM_HOLD_MS
        doc = self.run_sample(T0 + 15000 + R.OOM_HOLD_MS + 1)
        self.assertEqual(doc["level"], "ok")
        self.assertFalse(doc["oom_recent"])


class Processes(Base):
    def test_tree_classes_and_heavy(self):
        self.tree()
        doc = self.run_sample(T0)
        cp = next(s for s in doc["sessions"] if s["code"] == "CP")
        self.assertEqual(cp["rss_mb"], 400)
        classes = {c["pid"]: c["class"] for c in cp["children"]}
        self.assertEqual(classes[102], "test")
        self.assertEqual(classes[104], "browser")
        self.assertEqual(classes[105], "browser")
        self.assertEqual(classes[103], "other")   # the MCP server itself is not a browser
        self.assertEqual(cp["classes"]["browser"], {"n": 2, "rss_mb": 500, "cpu_pct": 0})
        self.assertEqual(cp["classes"]["test"]["n"], 1)
        api = next(s for s in doc["sessions"] if s["code"] == "API")
        self.assertEqual(api["children"][0]["class"], "build")
        self.assertTrue(api["children"][0]["heavy"])
        # pytest (1), the chromium tree (1: its renderer's parent is heavy too), cargo build (1)
        self.assertEqual(doc["heavy_running"], 3)
        self.assertEqual(cp["heavy_n"], 3)

    def test_teammates(self):
        self.tree()
        doc = self.run_sample(T0)
        cp = next(s for s in doc["sessions"] if s["code"] == "CP")
        self.assertEqual([t["name"] for t in cp["teammates"]], ["busy-one", "idle-one"])
        self.assertEqual(cp["teammates_n"], 2)
        self.assertEqual(cp["teammates_rss_mb"], 350 + 303)  # a teammate's RSS includes its subtree
        self.assertTrue(all(t["idle_s"] is None for t in cp["teammates"]))  # first sight: unknown
        self.assertEqual([t["name"] for t in doc["orphan_teammates"]], ["lost-one"])
        self.assertTrue(any(c["class"] == "other" and c["via"] == "idle-one" for c in cp["children"]))
        raw = json.dumps(doc)
        self.assertNotIn(SECRET, raw)            # argv is matched, never stored
        self.assertNotIn("--agent-id", raw)
        # 31 minutes of samples: busy-one burns CPU, idle-one and lost-one do not.
        now = T0
        for _ in range(125):
            now += 15000
            self.fp.tick(200, 300)               # 2 s CPU per 15 s = 13%
            doc = self.run_sample(now)
        cp = next(s for s in doc["sessions"] if s["code"] == "CP")
        busy, idle = cp["teammates"]
        self.assertFalse(busy["idle"])
        self.assertEqual(busy["idle_s"], 0)
        self.assertGreaterEqual(busy["cpu_pct"], 10)
        self.assertTrue(idle["idle"])
        self.assertGreaterEqual(idle["idle_s"], 30 * 60)
        self.assertEqual(cp["idle_teammates_n"], 1)
        self.assertTrue(doc["orphan_teammates"][0]["idle"])
        self.assertEqual([(c, [t["name"] for t in ts]) for c, ts in R.idle_teammates(doc)],
                         [("CP", ["idle-one"]), (None, ["lost-one"])])
        # a new process in an idle teammate's tree counts as activity
        self.fp.proc(203, 201, "git", ["git", "status"], rss_mb=5)
        doc = self.run_sample(now + 15000)
        idle = next(s for s in doc["sessions"] if s["code"] == "CP")["teammates"][1]
        self.assertFalse(idle["idle"])
        self.assertEqual(idle["idle_s"], 0)

    def test_pid_reuse_resets_teammate(self):
        self.tree()
        self.run_sample(T0)
        self.run_sample(T0 + 15000)
        self.fp.kill(201)
        self.fp.proc(201, 50, "claude.exe", teammate_argv("idle-one", "sid-cp"), rss_mb=300, start_s=99000)
        doc = self.run_sample(T0 + 30000)
        t = next(s for s in doc["sessions"] if s["code"] == "CP")["teammates"][1]
        self.assertIsNone(t["idle_s"])  # a new process under an old pid: idle time starts over

    def test_teammate_matched_by_team_suffix_or_descendant(self):
        self.tree()
        self.fp.proc(220, 50, "claude.exe", ["/opt/claude/bin/claude.exe", "--agent-id", "w@api-main"], rss_mb=100)
        self.fp.proc(221, 100, "claude.exe", ["/opt/claude/bin/claude.exe", "--agent-name", "child"], rss_mb=100)
        doc = self.run_sample(T0)
        by = {s["code"]: [t["name"] for t in s["teammates"]] for s in doc["sessions"]}
        self.assertEqual(by["API"], ["w"])
        self.assertIn("child", by["CP"])
        # A teammate that also shows up as a registry session is listed once, as a teammate.
        self.sessions.append({"code": "CP/w", "pid": 220, "session_id": "sid-w", "aliases": ["CP/w"]})
        doc = self.run_sample(T0 + 15000)
        self.assertEqual([s["code"] for s in doc["sessions"]], ["CP", "API"])

    def test_playwright_mcp_is_not_a_browser(self):
        self.tree()
        self.fp.proc(106, 100, "node", ["node", "/x/.bin/playwright-mcp", "--browser", "chrome"], rss_mb=90)
        self.fp.proc(107, 101, "node", ["node", "/x/.bin/playwright", "test", "--project=chromium"], rss_mb=200)
        doc = self.run_sample(T0)
        cp = next(s for s in doc["sessions"] if s["code"] == "CP")
        cls = {c["pid"]: c["class"] for c in cp["children"]}
        self.assertEqual(cls.get(106, "other"), "other")
        self.assertEqual(cls[107], "test")
        self.assertEqual(cls[104], "browser")                    # the browser itself, by comm

    def test_descendants_terminate_on_a_loop(self):
        self.assertEqual(sorted(R._descendants({1: [2], 2: [3, 1], 3: [2]}, 1)), [2, 3])

    def test_class_override(self):
        self.tree()
        self.cfg["process_classes"] = {"container": r"\bpython3\b"}
        self.cfg["process_classes"]["test"] = r"\bnever-matches\b"
        doc = self.run_sample(T0)
        cp = next(s for s in doc["sessions"] if s["code"] == "CP")
        self.assertEqual({c["pid"]: c["class"] for c in cp["children"]}[102], "container")

    def test_operator_watch(self):
        self.tree()
        doc = self.run_sample(T0)
        self.assertFalse(doc["operator"]["watch"])
        self.fp.proc(400, 1, "python3", ["python3", "/repo/lib/serve.py", "--watch"])
        self.assertTrue(self.run_sample(T0 + 15000)["operator"]["watch"])
        self.fp.kill(400)
        self.fp.proc(401, 1, "python3", ["/usr/bin/python3", "/repo/lib/serve.py", "--watch", "--tick", "480"])
        self.assertTrue(self.run_sample(T0 + 30000)["operator"]["watch"])
        self.fp.kill(401)
        self.fp.proc(402, 1, "python3", ["python3", "/repo/lib/serve.py", "--port", "1"])
        self.assertFalse(self.run_sample(T0 + 45000)["operator"]["watch"])


class Disks(Base):
    def test_statvfs_grouping_and_levels(self):
        class V:
            def __init__(self, blocks, avail):
                self.f_blocks, self.f_bavail, self.f_frsize = blocks, avail, 4096
        a, b = os.path.join(self.dir, "a"), os.path.join(self.dir, "b")
        os.makedirs(a)
        os.makedirs(b)
        mounts = {self.home: "/", a: "/", b: "/data"}
        vfs = {"/": V(25 * 1024 * 256, 20 * 1024 * 256), "/data": V(100 * 1024 * 256, 2 * 1024 * 256)}
        orig_m, orig_v = R.mount_of, R.statvfs
        R.mount_of = lambda p: mounts[p]
        R.statvfs = lambda p: vfs[p]
        try:
            self.cfg["disks"] = "auto"
            self.sessions = [{"code": "A", "pid": None, "cwd": a}, {"code": "B", "pid": 7, "cwd": b},
                             {"code": "C", "pid": 8, "cwd": a}]
            doc = self.run_sample(T0)
            self.run_sample(T0 + 15000)
            doc = self.run_sample(T0 + 30000)
        finally:
            R.mount_of, R.statvfs = orig_m, orig_v
        disks = {d["mount"]: d for d in doc["disks"]}
        self.assertEqual(disks["/"]["labels"], ["$BOARD_HOME", "C"])  # A is offline: not sampled
        self.assertEqual((disks["/"]["free_pct"], disks["/"]["free_mb"], disks["/"]["total_mb"]), (80, 20480, 25600))
        self.assertEqual(disks["/data"]["labels"], ["B"])
        self.assertEqual((disks["/data"]["free_pct"], disks["/data"]["free_mb"]), (2, 2048))
        self.assertEqual(disks["/data"]["level"], "critical")   # high (2% < 3%) for 2 samples
        self.assertEqual({d["mount"]: d["id"] for d in doc["disks"]}, {"/": "disk1", "/data": "disk2"})
        self.assertEqual(doc["worst"], "disk:disk2")            # opaque ids, never mount paths
        self.assertEqual(set(doc["levels"]) - {"mem", "swap", "load"}, {"disk:disk1", "disk:disk2"})
        self.assertNotIn(self.dir, json.dumps(doc))
        self.assertEqual(paths_in(R.board_view(doc)), [])
        self.assertEqual(paths_in(R.mirror_view(doc)), [])

    def patched(self, probe=None, mounts=None):
        orig = (R.mount_of, R.statvfs, R.DISK_TIMEOUT_S)
        R.mount_of = mounts or (lambda p: "/")
        R.statvfs = probe or (lambda p: type("V", (), {"f_blocks": 1000, "f_bavail": 500, "f_frsize": 4096})())
        R.DISK_TIMEOUT_S = 0.3
        return orig

    def restore(self, orig):
        R.mount_of, R.statvfs, R.DISK_TIMEOUT_S = orig

    def test_hung_disk_times_out_and_is_skipped(self):
        slow = os.path.join(self.dir, "slow")
        os.makedirs(slow)
        calls = []
        V = type("V", (), {"f_blocks": 1000, "f_bavail": 500, "f_frsize": 4096})

        def probe(p):
            calls.append(p)
            if p == "/slow":
                time.sleep(3)
            return V()
        orig = self.patched(probe, lambda p: "/slow" if p == slow else "/")
        try:
            self.cfg["disks"] = "auto"
            self.sessions = [{"code": "S", "pid": 7, "cwd": slow}]
            t = time.monotonic()
            doc = self.run_sample(T0)
            took = time.monotonic() - t
            self.assertLess(took, 2.0)
            stuck = [d for d in doc["disks"] if d.get("unresponsive")]
            self.assertEqual([(d["labels"], d["level"]) for d in stuck], [(["S"], None)])
            self.assertIn("disk", doc["unavailable"])
            self.assertNotIn("disk:" + stuck[0]["id"], doc["levels"])
            n = calls.count("/slow")
            doc = self.run_sample(T0 + 15000)                     # remembered: not probed again
            self.assertEqual(calls.count("/slow"), n)
            self.assertTrue(any(d.get("unresponsive") for d in doc["disks"]))
            self.assertTrue(R.board_view(doc)["disks"][-1]["unresponsive"])
            self.run_sample(T0 + R.DISK_HUNG_MS + 1000)           # retried after DISK_HUNG_MS
            self.assertEqual(calls.count("/slow"), n + 1)
        finally:
            self.restore(orig)

    def test_network_mounts_skipped_unless_configured(self):
        net = os.path.join(self.dir, "net")
        os.makedirs(os.path.join(net, "repo"))
        self.fp.write("self/mountinfo", "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
                      "40 22 0:50 / %s rw - 9p drvfs rw\n" % net.replace(" ", "\\040"))
        seen = []
        orig = self.patched(mounts=lambda p: seen.append(p) or "/")
        try:
            self.cfg["disks"] = "auto"
            self.sessions = [{"code": "N", "pid": 7, "cwd": os.path.join(net, "repo")}]
            doc = self.run_sample(T0)
            self.assertEqual([d["labels"] for d in doc["disks"]], [["$BOARD_HOME"]])
            self.assertNotIn(os.path.join(net, "repo"), seen)        # never touched
            self.cfg["disks"] = [os.path.join(net, "repo")]
            doc = self.run_sample(T0 + 15000)
            self.assertEqual([d["labels"] for d in doc["disks"]], [["disks[0]"]])  # explicit: probed
        finally:
            self.restore(orig)


class Levels(unittest.TestCase):
    """Hysteresis (enter at 15% available, leave at 20%) and 2-sample dwell."""

    def run_table(self, metric, key, low, seq, thresholds=None):
        thr = thresholds or R.DEFAULTS["thresholds"]
        st, out = {}, []
        for v in seq:
            out.append(R.evaluate({metric: [(v, key, low)]}, thr, st)[metric])
        return out

    def test_mem_hysteresis_and_dwell(self):
        seq = [50, 14, 14, 14, 17, 19, 21, 21, 14, 21, 14]
        want = ["ok", "ok", "warn", "warn", "warn", "warn", "warn", "ok", "ok", "ok", "ok"]
        self.assertEqual(self.run_table("mem", "mem_available_pct", True, seq), want)

    def test_high_then_critical_then_recover(self):
        seq = [7, 7, 7, 7, 9, 9, 12, 12, 30, 30]
        want = ["ok", "high", "critical", "critical", "critical", "critical", "critical", "warn", "warn", "ok"]
        # 9 < 8 * 4/3 keeps high; 12 leaves high (> 10.67) but stays under warn's leave (20)
        self.assertEqual(self.run_table("mem", "mem_available_pct", True, seq), want)

    def test_swinging_values_still_move(self):
        # From ok, 7% / 9% available alternate between high and warn candidates: both are worse
        # than ok, so the level moves up to the step closest to ok that the streak sustained.
        seq = [7, 9, 7, 9, 7, 9]
        want = ["ok", "warn", "warn", "warn", "warn", "warn"]
        self.assertEqual(self.run_table("mem", "mem_available_pct", True, seq), want)
        # From high, 19% / 21% alternate between warn and ok candidates: both better than high,
        # so it comes down (to warn, the closer step) instead of latching critical.
        st = {"mem": {"level": "high", "high_n": 5}}
        out = [R.evaluate({"mem": [(v, "mem_available_pct", True)]}, R.DEFAULTS["thresholds"], st)["mem"]
               for v in [19, 21, 19, 21, 19, 21]]
        self.assertEqual(out, ["critical", "warn", "warn", "warn", "warn", "warn"])

    def test_higher_is_worse(self):
        seq = [10, 55, 55, 45, 35, 35, 85, 85, 85, 70, 59, 59]   # warn leaves under 37.5, high under 60
        want = ["ok", "ok", "warn", "warn", "warn", "ok", "ok", "high", "critical", "critical", "critical", "warn"]
        self.assertEqual(self.run_table("swap", "swap_used_pct", False, seq), want)

    def test_worse_of_sub_metrics_and_overall(self):
        thr = R.DEFAULTS["thresholds"]
        st = {}
        for _ in range(2):  # 40% available but only 900 MB: the MB threshold is high
            lv = R.evaluate({"mem": [(40, "mem_available_pct", True), (900, "mem_available_mb", True),
                                     (0.0, "psi_mem_some_avg60", False)],
                             "load": [(2.0, "load_per_cpu", False)]}, thr, st)
        self.assertEqual(lv, {"mem": "high", "load": "warn"})
        self.assertEqual(R.overall(lv), ("high", "mem"))
        self.assertEqual(R.overall({"load": "warn", "disk:/": "warn"}), ("warn", "disk:/"))
        self.assertEqual(R.overall({"mem": "ok"}), ("ok", None))
        R.evaluate({"load": [(0.1, "load_per_cpu", False)]}, thr, st)
        self.assertNotIn("mem", st)  # a metric that disappears drops its state


class NonLinux(Base):
    def test_fake_ps(self):
        os.environ["BOARD_PROC_ROOT"] = os.path.join(self.dir, "no-proc")
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "ps"), "w") as fh:
            fh.write("#!/bin/sh\ncat <<'EOF'\n"
                     "  100     1  409600  3.0 00:01:00.50 02:00:00 /opt/claude/bin/claude\n"
                     "  101   100  921600 95.0 00:10:00 05:00 python3 -m pytest -q\n"
                     "  200     1  358400  0.0 00:00:10 1-02:00:00 /opt/claude/bin/claude --agent-id t1@team "
                     "--agent-name t1 --parent-session-id sid-cp --lead-verify-key " + SECRET + "\n"
                     "  300     1   10240  0.0 00:00:00 00:10 python3 /repo/lib/serve.py --watch\n"
                     "  102   100  512000 40.0 00:01:00 03:00 /Applications/Google Chrome.app/Contents/MacOS/"
                     "Google Chrome --type=renderer --lang=en\n"
                     "EOF\n")
        os.chmod(os.path.join(bindir, "ps"), 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = bindir + os.pathsep + old
        try:
            doc = self.run_sample(T0)
        finally:
            os.environ["PATH"] = old
        self.assertNotEqual(doc["platform"], "linux")
        self.assertIn(doc["platform"], ("darwin", "other"))
        for m in ("mem", "swap", "psi", "oom"):
            self.assertIn(m, doc["unavailable"])
        self.assertIsNone(doc["mem"]["available_mb"])  # never 0
        cp = next(s for s in doc["sessions"] if s["code"] == "CP")
        self.assertEqual((cp["rss_mb"], cp["cpu_pct"]), (400, 3))
        by = {c["pid"]: c for c in cp["children"]}
        self.assertEqual(by[101]["class"], "test")
        self.assertEqual(by[101]["age_s"], 300)
        # an executable path with spaces (a macOS app bundle) keeps its whole name
        self.assertEqual((by[102]["comm"], by[102]["class"]), ("Google Chrome", "browser"))
        self.assertTrue(by[102]["heavy"])
        self.assertEqual(cp["teammates"][0]["name"], "t1")
        self.assertEqual(cp["teammates"][0]["age_s"], 26 * 3600)
        self.assertTrue(doc["operator"]["watch"])
        self.assertNotIn(SECRET, json.dumps(doc))
        self.assertEqual(R.parse_clock("1-02:03:04"), 93784)
        self.assertEqual(R.parse_clock("00:01.5"), 1.5)


class OperatorHealth(unittest.TestCase):
    cfg = {"operator_stale_minutes": 10, "interval": 15, "serve_enabled": True}

    def res(self, watch, at=T0):
        return {"at": at, "operator": {"watch": watch}}

    def test_cases(self):
        H = R.operator_health
        fresh = {"heartbeat_at": T0 - 60000}
        old = {"heartbeat_at": T0 - 11 * 60000}
        self.assertFalse(H(fresh, self.res(True), self.cfg, 3, T0)["stale"])
        h = H(old, self.res(True), self.cfg, 3, T0)
        self.assertTrue(h["stale"])
        self.assertEqual(h["reasons"], ["heartbeat 11 min old"])
        self.assertFalse(H(old, self.res(True), self.cfg, 0, T0)["stale"])          # nothing pending
        h = H(fresh, self.res(False), self.cfg, 1, T0)
        self.assertEqual(h["reasons"], ["board-serve --watch is not running"])
        self.assertTrue(h["stale"])
        off = dict(self.cfg, serve_enabled=False)
        self.assertFalse(H(fresh, self.res(False), off, 1, T0)["stale"])            # serve disabled
        self.assertFalse(H(None, self.res(False), off, 1, T0)["stale"])             # Artifact mode: unknown
        self.assertTrue(H(None, self.res(True), self.cfg, 1, T0)["stale"])          # serve on, never checked in
        self.assertFalse(H(fresh, self.res(False, at=T0 - 600000), self.cfg, 1, T0)["stale"])  # sample too old
        self.assertTrue(H({"heartbeat_at": (T0 - 20 * 60000) / 1000}, None, self.cfg, 1, T0)["stale"])  # seconds ok
        # Artifact mode (serve off): the heartbeat is in meta/operator, not judged here at all.
        self.assertEqual(H({"heartbeat_at": T0 - 60 * 60000}, self.res(False), off, 1, T0)["reasons"], [])


class Collector(Base):
    """collect.py with a fixture proc root: the snapshot only changes when a level changes."""

    def setUp(self):
        super().setUp()
        self.sleeper = subprocess.Popen(["sleep", "600"])
        self.pid = self.sleeper.pid
        self.env = dict(os.environ, BOARD_HOME=self.home, CLAUDE_JSON=os.path.join(self.dir, "no-claude.json"), CLAUDE_SESSIONS_DIR=os.path.join(self.dir, "sessions"),
                        BOARD_CONFIG_DIR=os.path.join(self.dir, "config"))
        os.makedirs(self.env["CLAUDE_SESSIONS_DIR"])
        os.makedirs(self.env["BOARD_CONFIG_DIR"])
        with open(os.path.join(self.env["BOARD_CONFIG_DIR"], "fleet.yaml"), "w") as fh:
            fh.write("sessions:\n  - code: CP\n    cwd: /w\nresources:\n  disks: []\n")
        with open(os.path.join(self.env["CLAUDE_SESSIONS_DIR"], "%d.json" % self.pid), "w") as fh:
            json.dump({"pid": self.pid, "sessionId": "sid-cp", "cwd": "/w", "tmux": "CP:@1.%1", "name": "CP",
                       "status": "busy", "startedAt": 1}, fh)
        self.fp.proc(self.pid, 1, "claude.exe", ["/opt/claude/bin/claude.exe"], rss_mb=400)
        self.fp.proc(self.pid + 100000, self.pid, "python3", ["python3", "-m", "pytest"], rss_mb=700)

    def tearDown(self):
        self.sleeper.kill()
        self.sleeper.wait()
        super().tearDown()

    def collect(self, now):
        env = dict(self.env, BOARD_NOW_MS=str(now), BOARD_PROC_ROOT=self.fp.root)
        r = subprocess.run([sys.executable, os.path.join(REPO, "lib", "collect.py"), "--no-notify"], env=env,
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        snap = os.path.join(self.home, "snapshot.json")
        with open(snap) as fh:
            body = json.load(fh)
        with open(os.path.join(self.home, "resources.json")) as fh:
            res = json.load(fh)
        return body, os.stat(snap).st_mtime_ns, res

    def test_snapshot_changes_only_on_level_change(self):
        snap1, m1, res1 = self.collect(T0)
        self.assertEqual(snap1["resources"], {"level": "ok", "worst": None})
        self.assertEqual(res1["sessions"][0]["code"], "CP")
        self.assertEqual(res1["sessions"][0]["children"][0]["class"], "test")
        # Within the interval: no new sample, nothing rewritten.
        _, m2, res2 = self.collect(T0 + 5000)
        self.assertEqual(res2, res1)
        self.assertEqual(m2, m1)
        # Raw numbers move (same level): resources.json changes, snapshot.json does not.
        self.fp.mem(16384, 7000)
        self.fp.write("loadavg", "1.70 0.80 0.50 2/300 4242\n")
        snap3, m3, res3 = self.collect(T0 + 16000)
        self.assertEqual(res3["mem"]["available_mb"], 7000)
        self.assertNotEqual(res3, res1)
        self.assertEqual(m3, m1)
        self.assertEqual(snap3, snap1)
        # A level change (after the 2-sample dwell) does change the snapshot digest.
        self.fp.mem(16384, 1500)
        _, m4, _ = self.collect(T0 + 32000)
        self.assertEqual(m4, m1)
        snap5, m5, res5 = self.collect(T0 + 48000)
        self.assertEqual(res5["level"], "warn")
        self.assertEqual(snap5["resources"], {"level": "warn", "worst": "mem"})
        self.assertNotEqual(m5, m1)
        # A new sample whose numbers only move inside the board's buckets is not written (so the
        # board's SSE stays quiet) until the 60 s refresh.
        rpath = os.path.join(self.home, "resources.json")
        rm5 = os.stat(rpath).st_mtime_ns
        self.fp.mem(16384, 1480)
        _, _, r = self.collect(T0 + 64000)
        self.assertEqual((r["at"], r["mem"]["available_mb"], os.stat(rpath).st_mtime_ns), (T0 + 48000, 1500, rm5))
        _, _, r = self.collect(T0 + 112000)
        self.assertEqual((r["at"], r["mem"]["available_mb"]), (T0 + 112000, 1480))

    def test_disabled(self):
        with open(os.path.join(self.env["BOARD_CONFIG_DIR"], "fleet.yaml"), "a") as fh:
            fh.write("  enabled: false\n")
        snap, _, res = self.collect(T0)
        self.assertIsNone(snap["resources"])
        self.assertEqual(res["enabled"], False)
        self.assertEqual(res["operator"], {"watch": False})
        self.assertNotIn("mem", res)
        # The disabled doc never changes, yet it is rewritten every minute so `at` stays fresh and
        # the operator-watch input does not go stale (operator:stale would flap).
        rcfg = {"operator_stale_minutes": 10, "interval": 15, "serve_enabled": True}
        for t in range(16000, 200001, 16000):
            _, _, res = self.collect(T0 + t)
            self.assertLessEqual(T0 + t - res["at"], R.REWRITE_MS + 16000)
            h = R.operator_health({"heartbeat_at": T0 + t}, res, rcfg, 1, T0 + t)
            self.assertEqual(h["watch"], False, t)
            self.assertTrue(h["stale"])

    def test_cli(self):
        self.collect(T0)
        env = dict(self.env, BOARD_NOW_MS=str(T0 + 1000), BOARD_PROC_ROOT=self.fp.root)
        board = os.path.join(REPO, "bin", "board")
        out = subprocess.run([board, "resources"], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("level: ok", out.stdout)
        self.assertIn("mem 8.0 GB available of 16.0 GB (50%)", out.stdout)
        self.assertIn("test python3", out.stdout)
        js = subprocess.run([board, "resources", "--json"], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(json.loads(js.stdout)["level"], "ok")
        mirror = json.loads(subprocess.run([board, "resources", "--mirror"], env=env, capture_output=True,
                                           text=True, check=False).stdout)
        self.assertEqual(mirror["mem"]["available_mb"], 8200)   # bucketed to 100 MB
        self.assertEqual(mirror["mem"]["available_pct"], 50)
        self.assertNotIn("pid", json.dumps(mirror["sessions"]))
        bad = subprocess.run([board, "resources", "--bogus"], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(bad.returncode, 2)
        doc = subprocess.run([sys.executable, os.path.join(REPO, "lib", "resources.py"), "doctor"], env=env,
                             capture_output=True, text=True, check=False).stdout
        self.assertIn("ok    platform linux", doc)
        self.assertIn("ok    all metrics available", doc)
        # --refresh samples now but writes nothing: the collector's dwell state does not advance.
        files = [os.path.join(self.home, f) for f in ("resources.json", "resources-state.json")]
        before = [slurp(f) for f in files]
        self.fp.mem(16384, 500)
        env2 = dict(env, BOARD_NOW_MS=str(T0 + 20000))
        out = subprocess.run([board, "resources", "--refresh"], env=env2, capture_output=True, text=True, check=False)
        self.assertIn("mem 500 MB available", out.stdout)
        self.assertEqual([slurp(f) for f in files], before)


class Views(Base):
    def test_board_view_has_no_pids_or_paths(self):
        self.tree()
        doc = self.run_sample(T0)
        v = R.board_view(doc)
        raw = json.dumps(v)
        self.assertNotIn('"pid"', raw)
        self.assertEqual(v["sessions"][0]["code"], "CP")
        self.assertEqual(set(v["sessions"][0]["teammates"][0]), set(R.BOARD_TM_KEYS))
        self.assertIsNone(R.board_view(None))
        self.assertEqual(R.board_view({"enabled": False, "at": 1, "operator": {"watch": True}})["enabled"], False)
        m = R.mirror_view(doc)
        self.assertEqual(m["sessions"][0]["rss_mb"], 400)
        self.assertEqual(m["at"] % 60000, 0)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
