#!/usr/bin/env python3
"""tests/test_pomodoro.py — the pomodoro timer engine (lib/pomodoro.py) on synthetic state
timelines: continuous work, short gaps inside the grace, a needs-you pause, resets, local midnight,
a collector restart mid-pomodoro (pomodoro.json carries it), several sessions, the fleet milestone
notification (off by default), the board view and `board pomodoro`.

  python3 tests/test_pomodoro.py [-v]
"""
import datetime as dt
import io
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

TMP = tempfile.mkdtemp(prefix="switchboard-pomo-test.")
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG, "ALERT_DESKTOP": "0"})
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "LINE_", "AWS_")) or k in ("TMUX", "TMUX_PANE", "BOARD_PUBLIC_URL"):
        os.environ.pop(k)
for d in (HOME, CFG):
    os.makedirs(d)

import pomodoro  # noqa: E402

MIN = 60 * 1000
SEC = 1000
# 2026-11-15 08:00:00 UTC
T0 = int(dt.datetime(2026, 11, 15, 8, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
ST = pomodoro.settings({"pomodoro": {"timezone": "UTC", "mode": "strict"}})   # the human pomodoro
ACC = pomodoro.settings({"pomodoro": {"timezone": "UTC"}})                     # the default: accumulate


def play(timeline, doc=None, st=ST, start=T0, tick=3 * SEC):
    """timeline: [(minutes, {code: state}), ...] — each segment observed every `tick`.
    Returns (doc, events, end time)."""
    t, events = start, []
    for minutes, states in timeline:
        doc, ev = pomodoro.step(doc, states, t, st)  # the change is seen right at the boundary
        events += ev
        end = t + int(minutes * MIN)
        while t < end:
            t = min(end, t + tick)
            doc, ev = pomodoro.step(doc, states, t, st)
            events += ev
    return doc, events, t


def done_on(doc, day, code):
    return doc["days"].get(day, {}).get(code, {}).get("done", 0)


DAY = "2026-11-15"
W = {"API": "working"}


class Engine(unittest.TestCase):
    def test_25_minutes_is_one(self):
        doc, ev, _ = play([(25, W)])
        self.assertEqual(done_on(doc, DAY, "API"), 1)
        self.assertEqual(doc["days"][DAY]["API"]["focus_ms"], 25 * MIN)
        self.assertEqual(doc["sessions"]["API"]["acc"], 0)
        self.assertEqual([e["at"] for e in ev if e["type"] == "done"], [T0 + 25 * MIN])

    def test_24_minutes_is_none_yet(self):
        doc, _, _ = play([(24.5, W)])
        self.assertEqual(done_on(doc, DAY, "API"), 0)
        self.assertEqual(doc["sessions"]["API"]["acc"], int(24.5 * MIN))

    def test_50_minutes_is_two(self):
        doc, ev, _ = play([(50, W)])
        self.assertEqual(done_on(doc, DAY, "API"), 2)
        self.assertEqual([e["at"] for e in ev if e["type"] == "done"], [T0 + 25 * MIN, T0 + 50 * MIN])

    def test_long_steps_split_exactly(self):
        # one 60-minute interval between runs (within a huge grace) still completes at the crossings
        st = dict(ST, grace_ms=120 * MIN)
        doc, ev = pomodoro.step(None, W, T0, st)
        doc, ev = pomodoro.step(doc, W, T0 + 60 * MIN, st)
        self.assertEqual(done_on(doc, DAY, "API"), 2)
        self.assertEqual(doc["sessions"]["API"]["acc"], 10 * MIN)

    def test_short_gaps_inside_grace_still_count(self):
        doc, ev, _ = play([(10, W), (1.5, {"API": "done"}), (8, W), (1, {"API": "rest"}), (7, W)])
        self.assertEqual(done_on(doc, DAY, "API"), 1)
        self.assertFalse([e for e in ev if e["type"] == "reset"])
        # the pause does not count as focus: 10 + 8 + 7 = 25
        self.assertEqual(doc["days"][DAY]["API"]["focus_ms"], 25 * MIN)

    def test_gap_longer_than_grace_resets(self):
        doc, ev, _ = play([(10, W), (3, {"API": "done"}), (15, W)])
        self.assertEqual(done_on(doc, DAY, "API"), 0)
        self.assertEqual(doc["sessions"]["API"]["acc"], 15 * MIN)
        resets = [e for e in ev if e["type"] == "reset"]
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0]["lost_ms"], 10 * MIN)
        self.assertEqual(resets[0]["at"], T0 + 12 * MIN)  # break start + grace

    def test_needs_you_pauses_and_keeps_progress(self):
        doc, ev, _ = play([(10, W), (30, {"API": "needs-you"}), (15, W)])
        self.assertEqual(done_on(doc, DAY, "API"), 1)
        self.assertEqual(doc["days"][DAY]["API"]["focus_ms"], 25 * MIN)
        self.assertFalse([e for e in ev if e["type"] == "reset"])

    def test_needs_you_between_short_breaks(self):
        # done 1 min, needs-you, done 1.5 min: each done run is under the grace, nothing resets
        doc, _, _ = play([(10, W), (1, {"API": "done"}), (5, {"API": "needs-you"}), (1.5, {"API": "done"}), (15, W)])
        self.assertEqual(done_on(doc, DAY, "API"), 1)

    def test_offline_resets(self):
        doc, ev, _ = play([(10, W), (0.1, {}), (15, W)])
        self.assertEqual(done_on(doc, DAY, "API"), 0)
        self.assertEqual(doc["sessions"]["API"]["acc"], 15 * MIN)
        self.assertEqual(len([e for e in ev if e["type"] == "reset"]), 1)

    def test_offline_idle_code_is_dropped(self):
        doc, _, t = play([(1, {"API": "done"})])
        doc, _ = pomodoro.step(doc, {"API": "offline"}, t + 3 * SEC, ST)
        self.assertNotIn("API", doc["sessions"])

    def test_no_data_gap_resets(self):
        doc, _, t = play([(10, W)])
        doc, ev = pomodoro.step(doc, W, t + 5 * MIN, ST)   # nothing seen for 5 minutes
        self.assertEqual(doc["sessions"]["API"]["acc"], 0)
        self.assertEqual(ev[0]["type"], "reset")
        self.assertEqual(doc["days"][DAY]["API"]["focus_ms"], 10 * MIN)  # the gap is not focus

    def test_clock_going_back_credits_nothing(self):
        doc, _, t = play([(10, W)])
        doc2, ev = pomodoro.step(doc, W, t - 5 * MIN, ST)
        self.assertEqual(doc2["sessions"]["API"]["acc"], 10 * MIN)
        self.assertFalse(ev)

    def test_midnight_rollover(self):
        start = int(dt.datetime(2026, 11, 15, 23, 50, tzinfo=dt.timezone.utc).timestamp() * 1000)
        doc, ev, _ = play([(30, W)], start=start)
        self.assertEqual(done_on(doc, "2026-11-15", "API"), 0)
        self.assertEqual(done_on(doc, "2026-11-16", "API"), 1)
        self.assertEqual(doc["days"]["2026-11-15"]["API"]["focus_ms"], 10 * MIN)
        self.assertEqual(doc["days"]["2026-11-16"]["API"]["focus_ms"], 20 * MIN)
        self.assertEqual(ev[0]["day"], "2026-11-16")
        view = pomodoro.board_view(dict(doc, settings={"tz": "UTC", "focus_ms": 25 * MIN}), start + 30 * MIN)
        self.assertEqual(view["today"], "2026-11-16")
        self.assertEqual(view["fleet"]["done"], 1)
        self.assertEqual(view["days"][-2]["focus_ms"], 10 * MIN)

    def test_midnight_in_another_timezone(self):
        # 15:50 UTC is 23:50 in UTC+8: the day boundary follows pomodoro.timezone
        st = pomodoro.settings({"pomodoro": {"timezone": "Asia/Taipei"}})
        start = int(dt.datetime(2026, 11, 15, 15, 50, tzinfo=dt.timezone.utc).timestamp() * 1000)
        doc, _, _ = play([(30, W)], start=start, st=st)
        self.assertEqual(done_on(doc, "2026-11-16", "API"), 1)
        self.assertEqual(doc["days"]["2026-11-15"]["API"]["focus_ms"], 10 * MIN)

    def test_history_keeps_14_days(self):
        doc = None
        for i in range(20):
            start = T0 + i * 24 * 60 * MIN
            doc, _, _ = play([(25, W), (1, {"API": "done"})], doc=doc, start=start)
        self.assertEqual(len(doc["days"]), 14)
        self.assertEqual(min(doc["days"]), "2026-11-21")
        self.assertEqual(max(doc["days"]), "2026-12-04")

    def test_multiple_sessions(self):
        tl = [(25, {"API": "working", "WEB": "working", "OPS": "needs-you"}),
              (25, {"API": "done", "WEB": "working", "OPS": "working"})]
        doc, _, t = play(tl)
        self.assertEqual(done_on(doc, DAY, "API"), 1)
        self.assertEqual(done_on(doc, DAY, "WEB"), 2)
        self.assertEqual(done_on(doc, DAY, "OPS"), 1)
        view = pomodoro.board_view(dict(doc, settings={"tz": "UTC"}), t)
        self.assertEqual(view["fleet"], {"done": 4, "focus_ms": 100 * MIN, "teammates_done": 0})
        self.assertEqual(view["sessions"]["WEB"]["done"], 2)
        self.assertFalse(view["sessions"]["API"]["running"])
        self.assertTrue(view["sessions"]["OPS"]["running"])
        self.assertEqual(view["days"][-1]["sessions"], {"API": 1, "OPS": 1, "WEB": 2})

    def test_clock_back_across_midnight_keeps_the_later_day(self):
        start = int(dt.datetime(2026, 11, 16, 0, 10, tzinfo=dt.timezone.utc).timestamp() * 1000)
        doc, _, t = play([(26, W)], start=start)
        self.assertEqual(done_on(doc, "2026-11-16", "API"), 1)
        doc, _ = pomodoro.step(doc, W, start - 30 * MIN, ST)   # the clock jumps back to 23:40 the day before
        self.assertEqual(done_on(doc, "2026-11-16", "API"), 1)
        doc, _, _ = play([(5, W)], doc=doc, start=start - 30 * MIN)
        self.assertEqual(done_on(doc, "2026-11-16", "API"), 1)

    def test_timezone_moving_west_keeps_today(self):
        # 2026-11-16 03:00 in Asia/Taipei is still 2026-11-15 in UTC
        tpe = pomodoro.settings({"pomodoro": {"timezone": "Asia/Taipei"}})
        start = int(dt.datetime(2026, 11, 15, 19, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
        doc, _, t = play([(25, W)], start=start, st=tpe)
        self.assertEqual(done_on(doc, "2026-11-16", "API"), 1)
        doc, _ = pomodoro.step(doc, W, t + 3 * SEC, ST)        # pomodoro.timezone: UTC from now on
        self.assertEqual(done_on(doc, "2026-11-16", "API"), 1)

    def test_old_days_still_go(self):
        doc = {"days": {"2026-10-01": {"API": {"done": 1, "focus_ms": 1}}, "2026-11-02": {"API": {"done": 1, "focus_ms": 1}},
                        "2026-11-01": {"API": {"done": 1, "focus_ms": 1}}}}
        doc, _ = pomodoro.step(doc, W, T0, ST)
        self.assertEqual(sorted(doc["days"]), ["2026-11-02"])

    def test_away_since_never_after_now(self):
        doc, _, t = play([(10, W), (1, {"API": "done"})])
        doc, _ = pomodoro.step(doc, {"API": "done"}, t - 5 * MIN, ST)   # the clock moved back
        self.assertLessEqual(doc["sessions"]["API"]["away_since"], t - 5 * MIN)
        doc, _ = pomodoro.step(doc, W, t - 5 * MIN + 30 * SEC, ST)
        self.assertEqual(doc["sessions"]["API"]["acc"], 10 * MIN)        # a 30 s break: kept

    def test_no_data_threshold_follows_poll_interval(self):
        st = pomodoro.settings({"poll_interval": 60, "pomodoro": {"timezone": "UTC", "mode": "strict"}})
        self.assertEqual(pomodoro.nodata_ms(st), 140 * SEC)
        self.assertEqual(pomodoro.nodata_ms(ST), 2 * MIN)
        doc, _, t = play([(10, W)], st=st, tick=60 * SEC)
        doc, ev = pomodoro.step(doc, W, t + 100 * SEC, st)              # one slow run: not a gap
        self.assertEqual(doc["sessions"]["API"]["acc"], 10 * MIN + 100 * SEC)
        doc, ev = pomodoro.step(doc, W, t + 100 * SEC + 150 * SEC, st)  # longer: a gap
        self.assertEqual(doc["sessions"]["API"]["acc"], 0)

    def test_bad_input_is_ignored(self):
        doc, _ = pomodoro.step({"sessions": {"../x": {"acc": 5}, "OK": "nope"}, "days": {"x": {}}, "at": "1"},
                               {"bad code!": "working", "API": "working"}, T0, ST)
        self.assertEqual(sorted(doc["sessions"]), ["API"])
        self.assertEqual(doc["days"], {})


class Settings(unittest.TestCase):
    def test_defaults_and_clamps(self):
        st = pomodoro.settings({})
        self.assertEqual((st["focus_ms"], st["grace_ms"], st["notify"], st["notify_every"], st["enabled"]),
                         (25 * MIN, 2 * MIN, False, 10, True))
        st = pomodoro.settings({"pomodoro": {"focus_minutes": 0, "grace_minutes": 999, "timezone": "Nowhere/City",
                                             "notify": "yes", "enabled": False}})
        self.assertEqual((st["focus_ms"], st["grace_ms"], st["tz"], st["notify"], st["enabled"]),
                         (1 * MIN, 60 * MIN, "", False, False))

    def test_custom_focus(self):
        st = pomodoro.settings({"pomodoro": {"focus_minutes": 50, "timezone": "UTC"}})
        doc, _, _ = play([(50, W)], st=st)
        self.assertEqual(done_on(doc, DAY, "API"), 1)

    def test_grace_zero_still_survives_collector_ticks(self):
        st = pomodoro.settings({"pomodoro": {"grace_minutes": 0, "timezone": "UTC"}})
        doc, _, _ = play([(25, W)], st=st, tick=20 * SEC)
        self.assertEqual(done_on(doc, DAY, "API"), 1)

    def test_ordinal(self):
        self.assertEqual([pomodoro.ordinal(n) for n in (1, 2, 3, 4, 10, 11, 12, 13, 21, 22, 101, 111)],
                         ["1st", "2nd", "3rd", "4th", "10th", "11th", "12th", "13th", "21st", "22nd", "101st", "111th"])


def write_cfg(text):
    with open(os.path.join(CFG, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write(text)


class Collector(unittest.TestCase):
    """pomodoro.run(): what the collector calls, with pomodoro.json on disk (strict mode)."""

    def setUp(self):
        for f in os.listdir(HOME):
            os.remove(os.path.join(HOME, f))
        self.cfg = {"pomodoro": {"timezone": "UTC", "mode": "strict"}}
        self.sent = []
        self.writes = 0
        real = pomodoro.write_doc

        def counting(home, doc):
            self.writes += 1
            real(home, doc)
        pomodoro.write_doc = counting
        self.addCleanup(setattr, pomodoro, "write_doc", real)

    def send(self, *a, **kw):
        self.sent.append((a, kw))
        return {"status": "sent"}

    def feed(self, minutes, states, start, tick=3 * SEC, cfg=None, notify_ok=True):
        t, end = start, start + int(minutes * MIN)
        rows = [{"code": c, "state": s} for c, s in states.items()]
        while t < end:
            t = min(end, t + tick)
            pomodoro.run(cfg or self.cfg, HOME, rows, t, notify_ok=notify_ok, send=self.send)
        return t

    def stored(self):
        with open(os.path.join(HOME, "pomodoro.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_restart_mid_pomodoro_carries_on(self):
        rows = [{"code": "API", "state": "working"}]
        pomodoro.run(self.cfg, HOME, rows, T0, send=self.send)
        t = self.feed(10, W, T0)
        # the collector stops for 40 s and starts again: a new process that only has pomodoro.json
        t = self.feed(15, W, t + 40 * SEC)
        doc = self.stored()
        self.assertEqual(doc["days"][DAY]["API"]["done"], 1)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(HOME, "pomodoro.json")).st_mode), 0o600)

    def test_long_restart_resets(self):
        pomodoro.run(self.cfg, HOME, [{"code": "API", "state": "working"}], T0, send=self.send)
        t = self.feed(10, W, T0)
        t = self.feed(15, W, t + 5 * MIN)
        doc = self.stored()
        self.assertEqual(doc["days"][DAY]["API"].get("done"), 0)
        self.assertGreater(doc["sessions"]["API"]["acc"], 14 * MIN)

    def test_writes_are_rate_limited_and_lossless(self):
        pomodoro.run(self.cfg, HOME, [{"code": "API", "state": "working"}], T0, send=self.send)
        self.writes = 0
        t = self.feed(10, W, T0)  # 200 runs
        self.assertLess(self.writes, 40)  # a heartbeat every 20 s, not every 3 s run
        # the skipped writes lost nothing: integrating from the stored point gives the same numbers
        doc, _ = pomodoro.step(self.stored(), W, t, ST)
        self.assertEqual(doc["sessions"]["API"]["acc"], 10 * MIN)

    def test_state_change_is_written_at_once(self):
        pomodoro.run(self.cfg, HOME, [{"code": "API", "state": "working"}], T0, send=self.send)
        self.feed(1, W, T0)
        self.writes = 0
        pomodoro.run(self.cfg, HOME, [{"code": "API", "state": "needs-you"}], T0 + MIN + 3 * SEC, send=self.send)
        self.assertEqual(self.writes, 1)
        self.assertEqual(self.stored()["sessions"]["API"]["state"], "needs-you")

    def test_idle_fleet_is_not_rewritten(self):
        pomodoro.run(self.cfg, HOME, [{"code": "API", "state": "rest"}], T0, send=self.send)
        self.writes = 0
        self.feed(5, {"API": "rest"}, T0)
        self.assertEqual(self.writes, 0)

    def test_notify_is_off_by_default(self):
        pomodoro.run(self.cfg, HOME, [], T0, send=self.send)
        self.feed(251, {"A": "working", "B": "working"}, T0, tick=20 * SEC)
        self.assertEqual(self.stored()["days"][DAY]["A"]["done"], 10)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.stored()["notified"], {DAY: 20})

    def test_notify_milestone_once(self):
        cfg = {"pomodoro": {"timezone": "UTC", "notify": True, "notify_every": 4}}
        pomodoro.run(cfg, HOME, [], T0, send=self.send)
        self.feed(60, {"A": "working", "B": "working"}, T0, tick=20 * SEC, cfg=cfg)
        self.assertEqual(len(self.sent), 1)  # 4 done at 50 min; nothing more by 60 min
        (level, key, title), kw = self.sent[0]
        self.assertEqual((level, key, title), ("info", "pomodoro:%s:4" % DAY, "AI fleet finished its 4th 🍅 today"))
        self.assertEqual(kw["kind"], "pomodoro")
        self.assertIs(kw["escalate"], False)
        self.feed(50, {"A": "working", "B": "working"}, T0 + 60 * MIN, tick=20 * SEC, cfg=cfg)
        self.assertEqual([a[1] for a, _ in self.sent], ["pomodoro:%s:4" % DAY, "pomodoro:%s:8" % DAY])

    def test_no_notify_flag_holds_it(self):
        cfg = {"pomodoro": {"timezone": "UTC", "notify": True, "notify_every": 1}}
        pomodoro.run(cfg, HOME, [], T0, send=self.send)
        self.feed(26, {"A": "working"}, T0, cfg=cfg, notify_ok=False)
        self.assertEqual(self.sent, [])

    def test_disabled_removes_the_file(self):
        pomodoro.run(self.cfg, HOME, [{"code": "API", "state": "working"}], T0, send=self.send)
        self.assertTrue(os.path.exists(os.path.join(HOME, "pomodoro.json")))
        doc, _ = pomodoro.run({"pomodoro": {"enabled": False}}, HOME, [], T0 + SEC, send=self.send)
        self.assertIsNone(doc)
        self.assertFalse(os.path.exists(os.path.join(HOME, "pomodoro.json")))


class StableView(unittest.TestCase):
    """The board view changes only when something a viewer sees changes, never with a heartbeat."""

    def setUp(self):
        for f in os.listdir(HOME):
            os.remove(os.path.join(HOME, f))
        self.cfg = {"pomodoro": {"timezone": "UTC"}}

    def views(self, minutes, states, start, tick=3 * SEC):
        rows = [{"code": c, "state": s} for c, s in states.items()]
        t, end, out = start, start + int(minutes * MIN), []
        while t < end:
            t += tick
            pomodoro.run(self.cfg, HOME, rows, t)
            out.append((t, pomodoro.board_view(pomodoro.read_doc(HOME), t)))
        return out

    def test_working_changes_only_with_the_seen_bucket(self):
        pomodoro.run(self.cfg, HOME, [{"code": "API", "state": "working"}], T0 + 30 * SEC)
        vs = self.views(12, W, T0 + 30 * SEC)
        changes = [(a, b) for (_, a), (_, b) in zip(vs, vs[1:]) if a != b]
        self.assertTrue(changes)
        for a, b in changes:   # each change is the 5-minute freshness bucket, nothing else
            self.assertNotEqual(a["seen"], b["seen"])
            self.assertEqual(dict(a, seen=None), dict(b, seen=None))
        self.assertLessEqual(len(changes), 3)
        r = vs[-1][1]["sessions"]["API"]
        self.assertEqual((r["running"], r["progress_ms"], r["started_at"]), (True, None, T0 + 30 * SEC))
        self.assertNotIn("at", vs[-1][1])

    def test_waiting_does_not_change_at_all(self):
        pomodoro.run(self.cfg, HOME, [{"code": "API", "state": "working"}], T0)
        t = self.views(5, W, T0)[-1][0]
        vs = self.views(12, {"API": "needs-you"}, t)
        self.assertEqual(len({json.dumps(v, sort_keys=True) for _, v in vs}), 1)
        r = vs[-1][1]["sessions"]["API"]
        self.assertEqual((r["running"], r["progress_ms"], r["started_at"], vs[-1][1]["seen"]), (False, 5 * MIN + 3 * SEC, None, None))  # seen 3 s after the change

    def test_a_completion_is_a_change(self):
        pomodoro.run(self.cfg, HOME, [{"code": "API", "state": "working"}], T0)
        vs = self.views(26, W, T0, tick=20 * SEC)
        dones = sorted({v["fleet"]["done"] for _, v in vs})
        self.assertEqual(dones, [0, 1])

    def test_page_formula_gives_the_focus(self):
        # base + now - max(started_at, day_start) == the focus the collector counted, across midnight
        start = int(dt.datetime(2026, 11, 15, 23, 50, tzinfo=dt.timezone.utc).timestamp() * 1000)
        doc, _, t = play([(10, W), (20, {"API": "needs-you"}), (15, W)], start=start)
        v = pomodoro.board_view(dict(doc, settings={"tz": "UTC"}), t)
        r = v["sessions"]["API"]
        live = t - max(r["started_at"], v["day_start"])
        self.assertEqual(r["focus_ms"] + live, doc["days"]["2026-11-16"]["API"]["focus_ms"])
        self.assertEqual(v["fleet"]["focus_ms"] + live, 15 * MIN)
        self.assertEqual(t - r["started_at"], doc["sessions"]["API"]["acc"])


class Cli(unittest.TestCase):
    def setUp(self):
        for f in os.listdir(HOME):
            os.remove(os.path.join(HOME, f))
        write_cfg("pomodoro:\n  timezone: UTC\n")
        self.addCleanup(write_cfg, "")
        rows = [{"code": "API", "state": "working"}, {"code": "WEB", "state": "working"}]
        t = T0
        pomodoro.run({"pomodoro": {"timezone": "UTC"}}, HOME, rows, t)
        while t < T0 + 35 * MIN:
            t += 20 * SEC
            pomodoro.run({"pomodoro": {"timezone": "UTC"}}, HOME, rows, t)
        self.now = t

    def out(self, *args):
        buf = io.StringIO()
        rc = pomodoro.show(list(args), home=HOME, now=self.now, out=buf)
        return rc, buf.getvalue()

    def test_today_text(self):
        rc, text = self.out()
        self.assertEqual(rc, 0)
        self.assertIn("AI fleet: 2 🍅 · 1h 10m focus", text)
        self.assertRegex(text, r"API\s+1 🍅\s+35m  now 10:00 / 25:00")

    def test_week_text_and_json(self):
        rc, text = self.out("week")
        self.assertIn("Pomodoro, last 7 days (UTC)", text)
        self.assertIn("2026-11-15    2 🍅", text)
        rc, raw = self.out("week", "--json")
        self.assertEqual(len(json.loads(raw)["days"]), 7)

    def test_json_and_mirror(self):
        v = json.loads(self.out("--json")[1])
        self.assertEqual(set(v), {"enabled", "focus_ms", "grace_ms", "tz", "mode", "at", "today", "fleet", "sessions", "days"})
        self.assertEqual(len(v["days"]), 14)
        self.assertEqual(set(v["sessions"]["API"]), {"done", "focus_ms", "progress_ms", "running", "state", "done_at"})
        m = json.loads(self.out("--mirror")[1])
        self.assertEqual(set(m), {"today", "tz", "focus_ms", "mode", "fleet", "sessions", "days", "updated_at"})
        self.assertEqual(m["sessions"]["API"], {"done": 1, "focus_ms": 35 * MIN})

    def test_usage_error(self):
        self.assertEqual(self.out("month")[0], 2)

    def test_board_command(self):
        env = dict(os.environ, BOARD_HOME=HOME, BOARD_CONFIG_DIR=CFG)
        r = subprocess.run([os.path.join(REPO, "bin", "board"), "pomodoro", "today", "--json"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fleet", json.loads(r.stdout))
        r = subprocess.run([os.path.join(REPO, "bin", "board"), "pomodoro", "--help"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("board pomodoro", r.stdout)
        r = subprocess.run([os.path.join(REPO, "bin", "board"), "pomodoro", "year"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 2)


# ------------------------------------------------------------------ v2: accumulate mode, teammate streams

def res_doc(at, teammates, code="API"):
    """A resources.json with one session's teammates: {name: idle_s} (None = first sight)."""
    return {"at": at, "level": "ok", "sessions": [{"code": code, "teammates": [
        {"name": n, "pid": 100 + i, "rss_mb": 300, "cpu_pct": 0.0, "age_s": 60, "idle_s": v,
         "idle": v is not None and v >= 1800} for i, (n, v) in enumerate(sorted(teammates.items()))]}]}


def write_res(doc):
    with open(os.path.join(HOME, "resources.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def clear_home():
    for f in os.listdir(HOME):
        os.remove(os.path.join(HOME, f))


class Accumulate(unittest.TestCase):
    def test_default_mode_is_accumulate(self):
        self.assertEqual((pomodoro.settings({})["mode"], ACC["mode"], ST["mode"]), ("accumulate", "accumulate", "strict"))
        self.assertEqual(pomodoro.settings({"pomodoro": {"mode": "bogus"}})["mode"], "accumulate")

    def test_ten_minute_break_carries_progress(self):
        tl = [(15, W), (10, {"API": "rest"}), (15, W)]
        doc, ev, _ = play(tl, st=ACC)
        self.assertEqual(done_on(doc, DAY, "API"), 1)
        self.assertEqual(doc["sessions"]["API"]["acc"], 5 * MIN)          # the remainder carries over
        self.assertEqual(doc["days"][DAY]["API"]["focus_ms"], 30 * MIN)
        self.assertEqual([e for e in ev if e["type"] == "reset"], [])
        self.assertEqual([e["at"] for e in ev if e["type"] == "done"], [T0 + 35 * MIN])

    def test_strict_mode_unchanged(self):
        doc, ev, _ = play([(15, W), (10, {"API": "rest"}), (15, W)], st=ST)
        self.assertEqual(done_on(doc, DAY, "API"), 0)
        self.assertEqual(doc["sessions"]["API"]["acc"], 15 * MIN)
        self.assertEqual([e["lost_ms"] for e in ev if e["type"] == "reset"], [15 * MIN])

    def test_needs_you_still_pauses(self):
        doc, _, _ = play([(10, W), (30, {"API": "needs-you"}), (15, W)], st=ACC)
        self.assertEqual(done_on(doc, DAY, "API"), 1)
        self.assertEqual(doc["days"][DAY]["API"]["focus_ms"], 25 * MIN)

    def test_no_data_gap_keeps_progress_and_is_not_credited(self):
        doc, _, t = play([(10, W)], st=ACC)
        doc, ev = pomodoro.step(doc, W, t + 120 * MIN, ACC)      # the collector was down for 2 hours
        self.assertEqual(doc["sessions"]["API"]["acc"], 10 * MIN)
        self.assertEqual(doc["days"][DAY]["API"]["focus_ms"], 10 * MIN)
        self.assertEqual(ev, [])

    def test_offline_still_resets(self):
        doc, ev, _ = play([(10, W), (1, {})], st=ACC)
        self.assertNotIn("API", doc["sessions"])
        self.assertEqual([e["lost_ms"] for e in ev if e["type"] == "reset"], [10 * MIN])


class Teammates(unittest.TestCase):
    def test_states_from_resources(self):
        res = res_doc(T0, {"dev one": 0, "rev": 120, "old": 3000, "new": None, "../x": 0, "": 0})
        self.assertEqual(pomodoro.teammate_states(res, T0, ACC),
                         {"API/dev-one": "working", "API/rev": "rest", "API/old": "rest", "API/new": "rest",
                          "API/x": "working"})
        st = pomodoro.settings({"pomodoro": {"teammate_idle_s": 180}})
        self.assertEqual(pomodoro.teammate_states(res, T0, st)["API/rev"], "working")
        self.assertIsNone(pomodoro.teammate_states(res, T0 + 10 * MIN, ACC))           # stale
        self.assertIsNone(pomodoro.teammate_states(dict(res, enabled=False), T0, ACC))
        self.assertIsNone(pomodoro.teammate_states(None, T0, ACC))
        bad = {"at": T0, "sessions": [{"code": "bad code!", "teammates": [{"name": "a", "idle_s": 0}]}]}
        self.assertEqual(pomodoro.teammate_states(bad, T0, ACC), {})

    def feed(self, minutes, lead, tms, start, tick=3 * SEC, cfg=None):
        """Run the collector with the lead's state and its teammates' idle_s, resources.json fresh."""
        cfg = cfg or {"pomodoro": {"timezone": "UTC"}}
        t, end = start, start + int(minutes * MIN)
        write_res(res_doc(t, tms))                          # the change is seen right at the boundary
        pomodoro.run(cfg, HOME, [{"code": "API", "state": lead}], t)
        while t < end:
            t = min(end, t + tick)
            write_res(res_doc(t, tms))
            self.doc, _ = pomodoro.run(cfg, HOME, [{"code": "API", "state": lead}], t)
        return t   # self.doc: the state after the last run (the file may lag it by < HEARTBEAT_MS)

    def setUp(self):
        clear_home()

    def test_teammate_stream_accumulates_while_the_lead_rests(self):
        t = self.feed(20, "rest", {"dev": 0}, T0)
        t = self.feed(10, "rest", {"dev": 300}, t)          # idle 5 min: a break, progress kept
        t = self.feed(10, "rest", {"dev": 0}, t)
        doc = self.doc
        self.assertEqual(doc["version"], 2)
        self.assertEqual(done_on(doc, DAY, "API/dev"), 1)
        self.assertEqual(doc["sessions"]["API/dev"]["acc"], 5 * MIN)
        self.assertEqual(done_on(doc, DAY, "API"), 0)          # the lead's own stream: its own state only
        self.assertNotIn("API", doc["days"][DAY])
        v = pomodoro.board_view(doc, t)
        self.assertEqual((v["fleet"]["done"], v["fleet"]["teammates_done"]), (1, 1))

    def test_today_sums_every_stream(self):
        t = self.feed(25, "working", {"dev": 0, "rev": 0}, T0, tick=20 * SEC)
        v = pomodoro.raw_view(pomodoro.read_doc(HOME), t)
        self.assertEqual(v["fleet"], {"done": 3, "focus_ms": 75 * MIN, "teammates_done": 2})
        self.assertEqual(list(v["sessions"]), ["API", "API/dev", "API/rev"])
        rc_buf = io.StringIO()
        pomodoro.show([], home=HOME, now=t, out=rc_buf)
        self.assertIn("AI fleet: 3 🍅 · 1h 15m focus (teammates 2 🍅)", rc_buf.getvalue())
        self.assertIn("  + dev", rc_buf.getvalue())

    def test_vanished_teammate_pauses_then_is_pruned(self):
        t = self.feed(10, "rest", {"dev": 0, "rev": 0}, T0)
        t = self.feed(1, "rest", {"dev": 0}, t)               # rev ended
        t = self.feed(5, "rest", {}, t)                       # dev ended too
        doc = self.doc
        self.assertEqual(doc["sessions"]["API/rev"]["state"], "gone")
        self.assertEqual(doc["sessions"]["API/rev"]["acc"], 10 * MIN)          # paused, kept
        self.assertEqual(doc["sessions"]["API/dev"]["acc"], 11 * MIN)
        doc2, _ = pomodoro.step(doc, {"API": "rest"}, t + 15 * 24 * 60 * MIN, ACC)
        self.assertNotIn("API/rev", doc2["sessions"])                          # gone > HISTORY_DAYS
        doc3, _ = pomodoro.step(doc, {"API": "rest", "API/rev": "working"}, t + 3 * SEC, ACC)
        self.assertEqual(doc3["sessions"]["API/rev"]["acc"], 10 * MIN)         # back: carries on

    def test_stale_resources_pause_teammates(self):
        t = self.feed(10, "rest", {"dev": 0}, T0)
        for i in range(1, 60):                                 # resources.json stops being refreshed
            pomodoro.run({"pomodoro": {"timezone": "UTC"}}, HOME, [{"code": "API", "state": "rest"}], t + i * 5 * SEC)
        doc = pomodoro.read_doc(HOME)
        self.assertEqual(doc["sessions"]["API/dev"]["state"], "gone")
        self.assertLess(doc["sessions"]["API/dev"]["acc"], 10 * MIN + 4 * MIN)  # stopped within the stale limit

    def test_gone_without_progress_is_dropped(self):
        doc, _ = pomodoro.step(None, {"API/dev": "rest"}, T0, ACC)
        self.assertIn("API/dev", doc["sessions"])
        doc, _ = pomodoro.step(doc, {}, T0 + 3 * SEC, ACC)
        self.assertNotIn("API/dev", doc["sessions"])

    def test_migrates_a_v1_file(self):
        v1 = {"version": 1, "at": T0, "notified": {DAY: 0},
              "sessions": {"API": {"state": "working", "acc": 5 * MIN, "away_since": None, "done_at": T0 - MIN}},
              "days": {DAY: {"API": {"done": 3, "focus_ms": 80 * MIN}}, "2026-11-14": {"WEB": {"done": 2, "focus_ms": 50 * MIN}}},
              "settings": {"focus_ms": 25 * MIN, "grace_ms": 2 * MIN, "tz": "UTC"}}
        pomodoro.write_doc(HOME, v1)
        self.assertEqual(pomodoro.raw_view(v1, T0)["mode"], "strict")      # what the v1 file ran with
        self.feed(1, "working", {"dev": 0}, T0)
        doc = self.doc
        self.assertEqual(doc["version"], 2)
        self.assertEqual(doc["settings"]["mode"], "accumulate")
        self.assertEqual(done_on(doc, DAY, "API"), 3)
        self.assertEqual(doc["days"][DAY]["API"]["focus_ms"], 81 * MIN)
        self.assertEqual(doc["days"]["2026-11-14"]["WEB"]["done"], 2)
        self.assertEqual(doc["sessions"]["API"]["acc"], 6 * MIN)
        self.assertIn("seen_at", doc["sessions"]["API"])
        self.assertEqual(pomodoro.raw_view(doc, T0 + MIN)["fleet"]["done"], 3)

    def test_view_is_stable_with_teammates(self):
        # resources.json is rewritten with a changing idle_s (0, 15, 30 s) and a fresh `at`: the view
        # still changes only with the 5-minute seen bucket while everyone keeps working
        cfg = {"pomodoro": {"timezone": "UTC"}}
        write_res(res_doc(T0, {"dev": 0}))
        pomodoro.run(cfg, HOME, [{"code": "API", "state": "working"}], T0)
        out, t = [], T0
        while t < T0 + 12 * MIN:
            t += 3 * SEC
            write_res(res_doc(t, {"dev": (t // (15 * SEC)) % 3 * 15}))
            pomodoro.run(cfg, HOME, [{"code": "API", "state": "working"}], t)
            out.append(pomodoro.board_view(pomodoro.read_doc(HOME), t))
        changes = [(a, b) for a, b in zip(out, out[1:]) if a != b]
        for a, b in changes:
            self.assertNotEqual(a["seen"], b["seen"])
            self.assertEqual(dict(a, seen=None), dict(b, seen=None))
        self.assertLessEqual(len(changes), 3)
        r = out[-1]["sessions"]["API/dev"]
        self.assertEqual((r["running"], r["started_at"]), (True, T0))

    def test_privacy_of_the_file(self):
        res = res_doc(T0, {"dev": 0})
        res["sessions"][0].update({"title": "secret prompt", "cwd": "/somewhere/private"})
        res["sessions"][0]["teammates"][0]["title"] = "another prompt"
        write_res(res)
        pomodoro.run({"pomodoro": {"timezone": "UTC"}}, HOME, [{"code": "API", "state": "working",
                                                                 "title": "x prompt", "cwd": "/private"}], T0)
        with open(os.path.join(HOME, "pomodoro.json"), encoding="utf-8") as fh:
            text = fh.read()
        for word in ("prompt", "private", "somewhere"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1, argv=[sys.argv[0]])
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
