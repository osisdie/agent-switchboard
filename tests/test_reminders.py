#!/usr/bin/env python3
"""tests/test_reminders.py — the owner's reminders (lib/reminders.py, `board remind`, the
collector's firing pass, notify's breakthrough, board-serve's /api/reminders). A fake clock
(BOARD_NOW_MS / now=) and a fixed, DST-free timezone (reminders.timezone: Asia/Taipei); notify is
stubbed and ALERT_DESKTOP=0: no real notification is ever sent.

  python3 tests/test_reminders.py [-v]
"""
import datetime as dt
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
TMP = tempfile.mkdtemp(prefix="switchboard-reminders-test.")
HOME = os.path.join(TMP, "home")
CFG = os.path.join(TMP, "config")
os.environ.update({"BOARD_HOME": HOME, "BOARD_CONFIG_DIR": CFG})
for k in list(os.environ):
    if k.startswith(("SLACK_", "SMTP_", "ALERT_", "LINE_", "AWS_", "BOARD_NOW")) or k in ("TMUX", "TMUX_PANE"):
        os.environ.pop(k)
os.environ["ALERT_DESKTOP"] = "0"
os.makedirs(HOME, mode=0o700)
os.makedirs(CFG)

from zoneinfo import ZoneInfo  # noqa: E402

TZ = ZoneInfo("Asia/Taipei")


def write_fleet(extra=""):
    with open(os.path.join(CFG, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write("reminders:\n  timezone: Asia/Taipei\n" + extra)


write_fleet()
import notify  # noqa: E402
import prefs  # noqa: E402
import reminders as R  # noqa: E402

MIN = 60000


def at(y, mo, d, h, mi):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp() * 1000)


MON_14 = at(2026, 11, 16, 14, 0)    # Monday 2026-11-16 14:00 Asia/Taipei
FRI_18 = at(2026, 11, 20, 18, 0)


def reset():
    shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(HOME, mode=0o700)


class Stub:
    """Stands in for notify.send: records every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, level, key, title, body, breakthrough, now, cfg):
        self.calls.append({"level": level, "key": key, "title": title, "breakthrough": breakthrough, "now": now})
        return {"status": "sent"}


def run(*args, now=None, stdin=None):
    env = dict(os.environ)
    if now is not None:
        env["BOARD_NOW_MS"] = str(now)
    p = subprocess.run([os.path.join(REPO, "bin", "board"), "remind"] + list(args), capture_output=True, text=True,
                       env=env, timeout=30, input=stdin)
    return p.returncode, p.stdout, p.stderr


class TimeTests(unittest.TestCase):
    def test_parse_time(self):
        for s, want in (("15:00", (15, 0)), ("3pm", (15, 0)), ("3:30 pm", (15, 30)), ("3:30PM", (15, 30)), ("9", (9, 0)),
                        ("09:05", (9, 5)), ("12am", (0, 0)), ("12pm", (12, 0)), ("12:15 a.m.", (0, 15)), ("1530", (15, 30))):
            with self.subTest(s=s):
                self.assertEqual(R.parse_time(s), want)
        for bad in ("25:00", "13pm", "0am", "abc", "", "15:60", "3 pm extra"):
            with self.subTest(bad=bad):
                with self.assertRaises(R.Bad):
                    R.parse_time(bad)

    def test_rollover_date_and_weekdays(self):
        t, note = R.first_occurrence(15, 0, TZ, MON_14)
        self.assertEqual((t, note), (at(2026, 11, 16, 15, 0), None))
        t, note = R.first_occurrence(9, 30, TZ, MON_14)
        self.assertEqual(t, at(2026, 11, 17, 9, 30))
        self.assertEqual(note, "09:30 has passed today")
        t, _ = R.first_occurrence(9, 0, TZ, MON_14, date="2026-11-20")
        self.assertEqual(t, at(2026, 11, 20, 9, 0))
        with self.assertRaises(R.Bad):
            R.first_occurrence(9, 0, TZ, MON_14, date="2026-11-16")       # already over
        with self.assertRaises(R.Bad):
            R.first_occurrence(9, 0, TZ, MON_14, date="2026-13-01")
        # weekdays from Friday evening: Monday; a Saturday --date moves to Monday too.
        t, _ = R.first_occurrence(9, 0, TZ, FRI_18, repeat="weekdays")
        self.assertEqual(t, at(2026, 11, 23, 9, 0))
        t, _ = R.first_occurrence(9, 0, TZ, MON_14, date="2026-11-21", repeat="weekdays")
        self.assertEqual(t, at(2026, 11, 23, 9, 0))
        r = {"repeat": "weekdays", "tz": "Asia/Taipei", "at": "09:00"}
        self.assertEqual(R.next_after(r, at(2026, 11, 20, 9, 0)), at(2026, 11, 23, 9, 0))
        r["repeat"] = "daily"
        self.assertEqual(R.next_after(r, at(2026, 11, 20, 9, 0)), at(2026, 11, 21, 9, 0))
        self.assertIsNone(R.next_after(dict(r, repeat="once"), at(2026, 11, 20, 9, 0)))

    def test_cli_add_ls_and_errors(self):
        reset()
        rc, out, err = run("add", "Meeting", "--at", "3pm", now=MON_14)
        self.assertEqual(rc, 0, err)
        self.assertIn("Mon 2026-11-16 15:00", out)
        rc, out, err = run("add", "Standup", "--at", "9:30", "--repeat", "weekdays", "--lead", "10", now=MON_14)
        self.assertEqual(rc, 0, err)
        self.assertIn("09:30 has passed today: set for 2026-11-17", err)
        rc, out, _ = run("ls", "--json", now=MON_14)
        rows = json.loads(out)["reminders"]
        self.assertEqual([r["title"] for r in rows], ["Meeting", "Standup"])
        self.assertEqual((rows[1]["lead_min"], rows[1]["repeat"], rows[1]["level"], rows[1]["breakthrough"]),
                         (10, "weekdays", "high", True))
        self.assertEqual(oct(os.stat(os.path.join(HOME, "reminders.json")).st_mode & 0o777), "0o600")
        for args in (("add", "x", "--at", "25:00"), ("add", "", "--at", "15:00"), ("add", "x", "--at", "9", "--date", "2026-11-16"),
                     ("add", "x", "--at", "9", "--lead", "-1"), ("snooze", rows[0]["id"], "5x")):
            with self.subTest(args=args):
                rc, _, err = run(*args, now=MON_14)
                self.assertEqual(rc, 2, err)
        rc, _, _ = run("ack", "r-1-nope", now=MON_14)
        self.assertEqual(rc, 3)
        rc, _, _ = run("rm", rows[1]["id"], now=MON_14)
        self.assertEqual(rc, 0)
        self.assertEqual([r["title"] for r in json.loads(run("ls", "--json")[1])["reminders"]], ["Meeting"])
        with open(os.path.join(HOME, "events.jsonl"), encoding="utf-8") as fh:
            ev = [json.loads(x) for x in fh]
        self.assertTrue(all("Meeting" not in json.dumps(e) for e in ev), "the audit never carries the title")

    def test_title_scrubbed(self):
        reset()
        secret = "".join(["ghp_", "abcdefghij", "ABCDEFGHIJ", "0123456789"])
        r, _ = R.add(HOME, "rotate token %s now" % secret, "15:00", now=MON_14)
        self.assertNotIn(secret, json.dumps(R.load(HOME)))
        self.assertIn("[REDACTED]", r["title"])


class FiringTests(unittest.TestCase):
    def setUp(self):
        reset()
        self.r, _ = R.add(HOME, "Meeting", "15:00", now=MON_14)       # lead 5, high, once
        self.T = at(2026, 11, 16, 15, 0)

    def tick(self, now, stub):
        return R.tick(HOME, now=now, send=stub)

    def test_lead_then_due_once_across_restarts(self):
        s = Stub()
        self.assertEqual(self.tick(self.T - 6 * MIN, s), [])
        self.tick(self.T - 5 * MIN, s)
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(s.calls[0]["title"], "⏰ in 5 min: Meeting (15:00)")
        self.assertEqual((s.calls[0]["level"], s.calls[0]["breakthrough"]), ("high", True))
        for n in (self.T - 4 * MIN, self.T - 1 * MIN):
            self.tick(n, s)
        self.assertEqual(len(s.calls), 1, "the lead notice fires once")
        # A "restart": a fresh process reads the persisted state (no in-memory state anywhere).
        s2 = Stub()
        self.tick(self.T - 30000, s2)
        self.assertEqual(s2.calls, [])
        self.tick(self.T, s2)
        self.assertEqual([c["title"] for c in s2.calls], ["⏰ now: Meeting (15:00)"])
        self.tick(self.T + 30000, s2)
        self.tick(self.T + 3 * MIN, s2)
        self.assertEqual(len(s2.calls), 1, "the due nudge fires once")
        self.assertEqual(R.load(HOME)["reminders"][0]["state"], "active", "open for ack/snooze for a while")
        self.tick(self.T + 16 * MIN, s2)
        r = R.load(HOME)["reminders"][0]
        self.assertEqual((r["state"], r["history"][-1]["result"]), ("done", "done"))
        self.assertEqual(len(s2.calls), 1)
        keys = [c["key"] for c in s.calls + s2.calls]
        self.assertEqual(len(set(keys)), 2)

    def test_late_and_missed(self):
        s = Stub()
        self.tick(self.T + 10 * MIN, s)            # the collector was down through T-5 and T
        self.assertEqual([c["title"] for c in s.calls], ["⏰ now: Meeting (15:00) (late)"])
        reset()
        R.add(HOME, "Meeting", "15:00", now=MON_14)
        s = Stub()
        self.tick(self.T + 20 * MIN, s)            # beyond the late window: missed, nothing sent
        self.assertEqual(s.calls, [])
        r = R.load(HOME)["reminders"][0]
        self.assertEqual((r["state"], r["history"][-1]["result"]), ("missed", "missed"))

    def test_no_backlog_for_repeating(self):
        reset()
        R.add(HOME, "Standup", "09:00", repeat="daily", now=MON_14)       # first: Tue 09:00
        s = Stub()
        later = at(2026, 11, 20, 12, 0)            # down until Friday noon
        res = self.tick(later, s)
        self.assertEqual(s.calls, [], "never a backlog")
        self.assertEqual(res[0]["phase"], "missed")
        r = R.load(HOME)["reminders"][0]
        self.assertEqual((r["state"], r["occ"]["at"]), ("active", at(2026, 11, 21, 9, 0)))
        self.tick(at(2026, 11, 21, 8, 55), s)
        self.assertEqual([c["title"] for c in s.calls], ["⏰ in 5 min: Standup (09:00)"])

    def test_ack_and_snooze(self):
        s = Stub()
        self.tick(self.T - 5 * MIN, s)
        R.act(HOME, self.r["id"], "ack", by="me", now=self.T - 4 * MIN)
        self.tick(self.T, s)
        self.tick(self.T + 1 * MIN, s)
        self.assertEqual(len(s.calls), 1, "acked: no due nudge")
        self.assertEqual(R.load(HOME)["reminders"][0]["state"], "done")
        reset()
        r, _ = R.add(HOME, "Meeting", "15:00", repeat="daily", now=MON_14)
        s = Stub()
        with self.assertRaises(R.Bad):
            R.act(HOME, r["id"], "snooze", minutes="5m", now=MON_14)         # nothing to snooze yet
        self.tick(self.T, s)                                                 # due (the lead was missed: < 2 min, not late)
        R.act(HOME, r["id"], "snooze", minutes="5m", now=self.T + 1 * MIN)
        self.tick(self.T + 3 * MIN, s)
        self.tick(self.T + 6 * MIN, s)
        self.tick(self.T + 7 * MIN, s)
        self.assertEqual([c["title"] for c in s.calls], ["⏰ now: Meeting (15:00)", "⏰ snoozed: Meeting (15:00)"])
        R.act(HOME, r["id"], "ack", now=self.T + 8 * MIN)
        rr = R.load(HOME)["reminders"][0]
        self.assertEqual((rr["state"], rr["occ"]["at"], rr["history"][-1]["result"]),
                         ("active", at(2026, 11, 17, 15, 0), "acked"))

    def test_no_breakthrough_flag(self):
        reset()
        R.add(HOME, "Quiet one", "15:00", breakthrough=False, level="info", now=MON_14)
        s = Stub()
        self.tick(self.T - 5 * MIN, s)
        self.assertEqual((s.calls[0]["breakthrough"], s.calls[0]["level"]), (False, "info"))


class LockTests(unittest.TestCase):
    def test_bounded_wait(self):
        reset()
        R.add(HOME, "Meeting", "15:00", now=MON_14)
        import fcntl
        fd = os.open(os.path.join(HOME, "reminders.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            t0 = time.time()
            with self.assertRaises(R.Busy):
                with R.locked(HOME, wait_s=0.3):
                    pass
            self.assertLess(time.time() - t0, 2)
            self.assertEqual(R.tick(HOME, now=at(2026, 11, 16, 14, 55), send=Stub()), [], "the collector skips a busy tick")
            rc, _, err = run("add", "x", "--at", "16:00", now=MON_14)
            self.assertEqual(rc, 8, err)
            self.assertIn("busy", err)
        finally:
            os.close(fd)
        s = Stub()
        R.tick(HOME, now=at(2026, 11, 16, 14, 55), send=s)
        self.assertEqual(len(s.calls), 1, "and fires on the next one")


class DoctorTests(unittest.TestCase):
    def test_unknown_timezone_warned(self):
        try:
            with open(os.path.join(CFG, "fleet.yaml"), "w", encoding="utf-8") as fh:
                fh.write("reminders:\n  timezone: Mars/Phobos\n")
            p = subprocess.run([os.path.join(REPO, "bin", "board"), "doctor"], capture_output=True, text=True, timeout=60)
            self.assertIn("warn  reminders.timezone Mars/Phobos is not a known time zone", p.stdout)
            write_fleet()
            p = subprocess.run([os.path.join(REPO, "bin", "board"), "doctor"], capture_output=True, text=True, timeout=60)
            self.assertIn("ok    reminders.timezone Asia/Taipei", p.stdout)
        finally:
            write_fleet()


class BreakthroughTests(unittest.TestCase):
    """notify.send(..., breakthrough=True): urgent's rule for mutes, at any level."""

    def setUp(self):
        reset()
        self.sent = []
        self.orig = notify.http_post
        notify.http_post = lambda url, payload, headers=None: (self.sent.append(payload) or (True, None))
        self.cfg = notify.load_config()
        self.cfg["routes"] = dict(self.cfg["routes"], high=["webhook"], info=["webhook"])
        self.env = {"ALERT_WEBHOOK_URL": "https://hooks.example.invalid/x", "ALERT_DESKTOP": "0"}

    def tearDown(self):
        notify.http_post = self.orig

    def send(self, key, breakthrough, level="high"):
        return notify.send(level, key, "⏰ in 5 min: Meeting (15:00)", env=self.env, cfg=self.cfg,
                           breakthrough=breakthrough, now=MON_14, escalate=False, repeat=False)

    def test_muted_channel(self):
        prefs.mute(HOME, "all", None, "test", now=MON_14)
        self.assertEqual(self.send("reminder:a:1:lead", True)["status"], "sent")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.send("reminder:b:1:lead", False)["status"], "suppressed")
        self.assertEqual(len(self.sent), 1)
        prefs.mute(HOME, "all", None, "test", include_urgent=True, now=MON_14)
        self.assertEqual(self.send("reminder:c:1:lead", True)["status"], "suppressed", "include_urgent holds it too")

    def test_suppression_rule(self):
        p = {"mutes": {"all": {"until": None, "include_urgent": False}}, "min_level": {}, "presence": {}}
        self.assertEqual(prefs.suppression("slack", "high", {}, p, False, MON_14), "muted:all")
        self.assertIsNone(prefs.suppression("slack", "high", {}, p, False, MON_14, breakthrough=True))
        p["mutes"]["all"]["include_urgent"] = True
        self.assertEqual(prefs.suppression("slack", "high", {}, p, False, MON_14, breakthrough=True), "muted:all")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class RouteTests(unittest.TestCase):
    """/api/reminders: the owner only (owners, else approvers), CSRF on GET and POST, audited."""

    @classmethod
    def setUpClass(cls):
        reset()
        cls.port = free_port()
        cls.fleet(approvers="[local]")
        cls.err = open(os.path.join(TMP, "serve.err"), "w")
        cls.srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], stderr=cls.err,
                                   stdout=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/api/me" % cls.port, timeout=5).close()
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            raise RuntimeError("board-serve did not start")

    @classmethod
    def tearDownClass(cls):
        cls.srv.terminate()
        cls.srv.wait(10)
        cls.err.close()

    @classmethod
    def fleet(cls, owners="[]", approvers="[]"):
        write_fleet("owners: %s\napprovers: %s\nserve:\n  port: %d\n" % (owners, approvers, cls.port))

    def call(self, method, path, body=None, headers=None):
        b = "http://127.0.0.1:%d" % self.port
        h = {"X-Board": "1", "Origin": b} if headers is None else headers
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h = dict(h, **{"Content-Type": "application/json"})
        req = urllib.request.Request(b + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_owner_flow(self):
        self.fleet(approvers="[local]")              # no owners: the approvers fallback
        st, out = self.call("POST", "/api/reminders", {"title": "Meeting", "at": "23:59", "lead_min": 5, "repeat": "daily"})
        self.assertEqual(st, 201, out)
        rid = out["reminder"]["id"]
        self.assertEqual(out["reminder"]["created_by"], "board:local")
        st, out = self.call("GET", "/api/reminders")
        self.assertEqual(st, 200, out)
        self.assertIn(rid, [r["id"] for r in out["reminders"]])
        self.assertIsInstance(out["now"], int)
        st, out = self.call("POST", "/api/reminders/" + rid, {"action": "bogus"})
        self.assertEqual((st, out["error"]), (400, "bad_action"))
        st, out = self.call("POST", "/api/reminders/" + rid, {"action": "snooze", "minutes": 0})
        self.assertEqual((st, out["error"]), (400, "bad_minutes"))
        st, out = self.call("POST", "/api/reminders", {"title": "x", "at": "25:00"})
        self.assertEqual((st, out["error"]), (400, "bad_reminder"))
        st, out = self.call("POST", "/api/reminders", {"title": "x", "at": "10:00", "lead_min": True})
        self.assertEqual((st, out["error"]), (400, "bad_body"))
        st, out = self.call("POST", "/api/reminders/" + rid, {"action": "ack"})
        self.assertEqual((st, out["status"]), (200, "ok"))
        st, out = self.call("POST", "/api/reminders/" + rid, {"action": "delete"})
        self.assertEqual(st, 200, out)
        st, out = self.call("POST", "/api/reminders/" + rid, {"action": "ack"})
        self.assertEqual(st, 404)
        with open(os.path.join(HOME, "events.jsonl"), encoding="utf-8") as fh:
            ev = [json.loads(x) for x in fh]
        acts = [e["action"] for e in ev if e.get("type") == "serve" and e.get("id") == rid and e.get("result") == "ok"]
        self.assertEqual(acts, ["reminder-add", "reminder-ack", "reminder-delete"])


    def test_non_owner_and_viewer(self):
        self.fleet(approvers="[local]")
        st, out = self.call("POST", "/api/reminders", {"title": "Personal", "at": "23:58"})
        self.assertEqual(st, 201, out)
        rid = out["reminder"]["id"]
        with open(os.path.join(HOME, "reminders.json"), encoding="utf-8") as fh:
            before = fh.read()
        for owners, approvers in (("[owner@example.com]", "[local]"), ("[owner@example.com]", "[boss@example.com]"), ("[]", "[]")):
            with self.subTest(owners=owners, approvers=approvers):
                self.fleet(owners=owners, approvers=approvers)
                st, out = self.call("GET", "/api/reminders")
                self.assertEqual(st, 403, out)
                self.assertNotIn("Personal", json.dumps(out))
                self.assertNotIn("reminders", out, "not even a count")
                for path, body in (("/api/reminders", {"title": "x", "at": "10:00"}), ("/api/reminders/" + rid, {"action": "delete"})):
                    st, out = self.call("POST", path, body)
                    self.assertEqual(st, 403, out)
        with open(os.path.join(HOME, "reminders.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before, "nothing written")
        st, out = self.call("GET", "/api/state")
        self.assertNotIn("Personal", json.dumps(out), "never in the shared board state")

    def test_csrf(self):
        self.fleet(approvers="[local]")
        b = "http://127.0.0.1:%d" % self.port
        for hdrs in ({}, {"X-Board": "1", "Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-origin"}):
            with self.subTest(get=hdrs):
                st, out = self.call("GET", "/api/reminders", headers=hdrs)
                self.assertEqual((st, out["error"]), (403, "csrf"))
        for hdrs in ({"Origin": b}, {"X-Board": "1", "Origin": "https://evil.example.com"}, {"X-Board": "1"}):
            with self.subTest(post=hdrs):
                st, out = self.call("POST", "/api/reminders", {"title": "x", "at": "10:00"}, headers=hdrs)
                self.assertEqual((st, out["error"]), (403, "csrf"))


class CollectorTests(unittest.TestCase):
    def test_collector_fires_and_no_notify_does_not(self):
        reset()
        R.add(HOME, "Meeting", "15:00", now=MON_14)
        T = at(2026, 11, 16, 15, 0)
        env = dict(os.environ, BOARD_NOW_MS=str(T - 5 * MIN), CLAUDE_SESSIONS_DIR=os.path.join(TMP, "none"),
                   CLAUDE_JSON=os.path.join(TMP, "none.json"))
        subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--no-notify"], env=env, check=True,
                       capture_output=True)
        self.assertIsNone(R.load(HOME)["reminders"][0]["occ"]["lead_sent"], "--no-notify fires nothing")
        p = subprocess.run([os.path.join(REPO, "bin", "board-collect"), "--once", "--verbose"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("lead", p.stdout)
        self.assertEqual(R.load(HOME)["reminders"][0]["occ"]["lead_sent"], T - 5 * MIN)
        with open(os.path.join(HOME, "events.jsonl"), encoding="utf-8") as fh:
            ev = [json.loads(x) for x in fh]
        fired = [e for e in ev if e.get("type") == "reminder" and e.get("action") == "fire"]
        self.assertEqual([e["phase"] for e in fired], ["lead"])


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
