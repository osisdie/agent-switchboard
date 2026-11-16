#!/usr/bin/env python3
"""tests/test_tickets.py — offline tests for lib/tickets (tickets core), `board ticket get|doctor`
and board-serve's GET /api/tickets/<ref>. A fake tracker (tests/fake_tracker.py) runs on
127.0.0.1; nothing reaches the network and ~/.agent-switchboard is never touched.

  python3 tests/test_tickets.py [-v]
"""
import glob
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(REPO, "tests"))

TMP = tempfile.mkdtemp(prefix="switchboard-tickets-test.")
HOME = os.path.join(TMP, "home")
CONF = os.path.join(TMP, "config")
os.environ["BOARD_HOME"] = HOME
os.environ["BOARD_CONFIG_DIR"] = CONF
for k in ("BOARD_MOCK", "TMUX", "BOARD_PUBLIC_URL"):
    os.environ.pop(k, None)
os.makedirs(HOME, mode=0o700)
os.makedirs(CONF)

# Built from pieces, lower-case letters only, so no secret-guard pattern catches it by shape:
# only the tickets module's own literal redaction can keep it out of the output.
SENT = "".join(["tracker", "token", "sentinel", "value"])
TOKEN_ENV = "EXAMPLE_TRACKER_TOKEN"
os.environ[TOKEN_ENV] = SENT

from fake_tracker import FakeTracker  # noqa: E402
import tickets  # noqa: E402
from tickets import TicketError, limits, net, tpl  # noqa: E402

ISSUES = {
    "WEB-1": {"id": "u-1", "project": "p1", "data": {
        "title": "Login button does nothing", "status": "in_progress", "assigneeName": "Alex Example",
        "description": "<p>Clicking <b>Log in</b> on Safari does nothing.</p>", "updatedAt": "2026-11-10T10:00:00Z"}},
    "WEB-2": {"id": "u-2", "project": "p1", "data": {"title": "Second", "status": "backlog"}},
    "WEB-7": {"id": "u-7", "project": "p1", "data": {"title": "Leaky", "status": "done", "echo_auth": True,
                                                     "description": "steps to reproduce"}},
    "WEB-8": {"id": "redirect", "project": "p1", "data": {}},
    "WEB-9": {"id": "boom", "project": "p1", "data": {}},
    "WEB-10": {"id": "slow", "project": "p1", "data": {"title": "Slow", "status": "todo"}},
    "WEB-11": {"id": "slower", "project": "p1", "data": {"title": "Slower", "status": "todo"}},
    "WEB-12": {"id": "..", "project": "p1", "data": {}},
    "WEB-13": {"id": "u-13", "project": "../../admin@evil.example.com", "data": {"title": "x"}},
}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_json_tracker(port, name="tracker", pattern="^(WEB|API)-[0-9]+$", extra=""):
    return """    - name: %s
      type: http-json
      base_url: http://127.0.0.1:%d
      auth: {header: Authorization, scheme: Bearer, env: %s}
      key_pattern: '%s'
      retry_wait_max_seconds: 2
      resolve:
        method: GET
        path: /api/v1/issues/resolve
        query: {displayId: "{key}"}
        set: {id: "$.data.id", project: "$.data.project"}
      get:
        method: GET
        path: "/api/v1/projects/{project}/issues/{id}"
        map: {title: $.data.title, status: $.data.status, assignee: $.data.assigneeName,
              description: $.data.description, updated_at: $.data.updatedAt}
        link: "https://tracker.example.com/projects/{project}/issues/{key}"
        status_category: {todo: [backlog, todo], doing: [in_progress], done: [done, cancelled]}
%s""" % (name, port, TOKEN_ENV, pattern, extra)


def write_fleet(text):
    with open(os.path.join(CONF, "fleet.yaml"), "w", encoding="utf-8") as fh:
        fh.write(text)


def run(*args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run([os.path.join(REPO, "bin", "board"), "ticket"] + list(args), capture_output=True,
                       text=True, env=e, timeout=60)
    return p.returncode, p.stdout, p.stderr


def home_text():
    """Every file under BOARD_HOME, concatenated (events, cache, state)."""
    out = []
    for p in glob.glob(os.path.join(HOME, "**", "*"), recursive=True):
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                out.append(fh.read())
    return "\n".join(out)


def events():
    try:
        with open(os.path.join(HOME, "events.jsonl"), encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        return []


def reset_home():
    for sub in ("tickets",):
        for p in sorted(glob.glob(os.path.join(HOME, sub, "**", "*"), recursive=True), reverse=True):
            (os.rmdir if os.path.isdir(p) else os.remove)(p)
    limits._BUCKETS.clear()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tr = FakeTracker(SENT, ISSUES)
        cls.other = FakeTracker(SENT, {})   # "another host": must never be reached
        cls.tr.redirect_to = "http://localhost:%d/steal" % cls.other.port
        cls.tr.throttle = {}

    @classmethod
    def tearDownClass(cls):
        cls.tr.stop()
        cls.other.stop()

    def setUp(self):
        reset_home()
        write_fleet("tickets:\n  trackers:\n" + http_json_tracker(self.tr.port))

    def assertNoSentinel(self, *texts):
        import base64
        forms = [SENT, base64.b64encode(SENT.encode()).decode().rstrip("=")]
        for t in texts:
            for f in forms:
                self.assertNotIn(f, t)


class GetTests(Base):
    def test_get_http_json(self):
        n = self.tr.count()
        rc, out, err = run("get", "WEB-1", "--json")
        self.assertEqual(rc, 0, err)
        t = json.loads(out)
        self.assertEqual(t["provider"], "tracker")
        self.assertEqual(t["key"], "WEB-1")
        self.assertEqual(t["id"], "u-1")
        self.assertEqual(t["title"], "Login button does nothing")
        self.assertEqual(t["status"], "in_progress")
        self.assertEqual(t["status_category"], "doing")
        self.assertEqual(t["assignee"], {"id": None, "name": "Alex Example"})
        self.assertEqual(t["excerpt"], "Clicking Log in on Safari does nothing.")
        self.assertEqual(t["url"], "https://tracker.example.com/projects/p1/issues/WEB-1")
        self.assertEqual(t["updated_at"], "2026-11-10T10:00:00Z")
        reqs = self.tr.requests[n:]
        self.assertEqual([r["path"] for r in reqs], ["/api/v1/issues/resolve", "/api/v1/projects/p1/issues/u-1"])
        self.assertIn("displayId=WEB-1", reqs[0]["raw"])
        ev = events()[-1]
        self.assertEqual((ev["type"], ev["action"], ev["provider"], ev["key"], ev["result"]),
                         ("ticket", "get", "tracker", "WEB-1", "ok"))
        rc, out, _ = run("get", "WEB-1")
        self.assertEqual(rc, 0)
        self.assertIn("tracker:WEB-1  in_progress (doing)", out)
        self.assertIn("title:    Login button does nothing", out)

    def test_cache_ttl_refresh_and_not_found(self):
        rc, _, _ = run("get", "WEB-2")
        self.assertEqual(rc, 0)
        n = self.tr.count()
        rc, out, _ = run("get", "WEB-2", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(self.tr.count(), n, "a fresh cache entry is served without a request")
        self.assertEqual(events()[-1]["result"], "cache")
        f = os.path.join(HOME, "tickets", "cache", "tracker__WEB-2.json")
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        self.assertEqual(json.load(open(f))["title"], "Second")
        rc, _, _ = run("get", "WEB-2", "--refresh")
        self.assertEqual(self.tr.count(), n + 2, "--refresh fetches again")
        # Expired: fetched_at older than the TTL.
        doc = json.load(open(f))
        doc["fetched_at"] -= 61000
        json.dump(doc, open(f, "w"))
        run("get", "WEB-2")
        self.assertEqual(self.tr.count(), n + 4)
        # TTL 0: never served from the cache.
        write_fleet("tickets:\n  cache_ttl_seconds: 0\n  trackers:\n" + http_json_tracker(self.tr.port))
        run("get", "WEB-2")
        self.assertEqual(self.tr.count(), n + 6)
        # A 404 is remembered (30 s).
        rc, _, err = run("get", "WEB-404")
        self.assertEqual(rc, 3, err)
        self.assertIn("ticket_not_found", err)
        m = self.tr.count()
        rc, _, _ = run("get", "WEB-404")
        self.assertEqual(rc, 3)
        self.assertEqual(self.tr.count(), m)
        self.assertTrue(events()[-1].get("cached"))

    def test_cache_purge_keeps_linked(self):
        from tickets import cache
        c = cache.Cache(HOME)
        c.put("tracker", "WEB-50", {"provider": "tracker", "key": "WEB-50", "fetched_at": 0})
        c.put("tracker", "WEB-51", {"provider": "tracker", "key": "WEB-51", "fetched_at": 0})
        old = time.time() - 8 * 86400
        for k in ("WEB-50", "WEB-51"):
            os.utime(c.path("tracker", k), (old, old))
        os.makedirs(c.links, exist_ok=True)
        open(os.path.join(c.links, "tracker__WEB-51.json"), "w").close()
        c.put("tracker", "WEB-52", {"provider": "tracker", "key": "WEB-52", "fetched_at": 0})
        self.assertFalse(os.path.exists(c.path("tracker", "WEB-50")))
        self.assertTrue(os.path.exists(c.path("tracker", "WEB-51")))
        self.assertEqual(os.path.basename(c.path("t", "A/../b#1")), "t__A%2F..%2Fb%231.json")

    def test_fake_provider_and_mock(self):
        write_fleet("tickets:\n  trackers:\n    - name: demo\n      type: fake\n" + http_json_tracker(self.tr.port))
        rc, out, err = run("get", "DEMO-2", "--json")
        self.assertEqual(rc, 0, err)
        t = json.loads(out)
        self.assertEqual((t["status_category"], t["assignee"]["name"]), ("doing", "Alex Example"))
        rc, _, err = run("get", "DEMO-9")
        self.assertEqual(rc, 1)
        self.assertIn("ticket_auth", err)
        # BOARD_MOCK=1: only fake trackers load.
        n = self.tr.count()
        rc, _, err = run("get", "WEB-1", env={"BOARD_MOCK": "1"})
        self.assertEqual(rc, 2)
        self.assertIn("unknown_ticket_ref", err)
        self.assertEqual(self.tr.count(), n)

    def test_doctor(self):
        tf = os.path.join(CONF, "tok")
        with open(tf, "w") as fh:
            fh.write(SENT + "\n")
        os.chmod(tf, 0o644)
        write_fleet("tickets:\n  trackers:\n" + http_json_tracker(self.tr.port) + """    - name: filetok
      type: http-json
      base_url: https://tracker.example.com
      auth: {token_file: tok}
      key_pattern: '^OPS-[0-9]+$'
      get: {path: "/i/{id}", map: {title: $.title}}
    - name: plain
      type: http-json
      base_url: http://tracker.example.com
      auth: {env: UNSET_TRACKER_TOKEN_X}
      key_pattern: '^PL-[0-9]+$'
      get: {path: "/i/{id}", map: {title: $.title}}
    - name: weird
      type: carrier-pigeon
      key_pattern: '('
""")
        rc, out, err = run("doctor", "--json")
        self.assertEqual(rc, 1, err)
        rows = {r["name"]: r for r in json.loads(out)}
        self.assertTrue(rows["tracker"]["ok"], rows["tracker"])
        self.assertEqual((rows["tracker"]["token"], rows["tracker"]["token_source"]), ("set", "env " + TOKEN_ENV))
        self.assertEqual(rows["tracker"]["host"], "127.0.0.1:%d" % self.tr.port)
        self.assertFalse(rows["filetok"]["ok"])
        self.assertTrue(any("chmod 600" in p for p in rows["filetok"]["problems"]))
        self.assertTrue(any("https" in p for p in rows["plain"]["problems"]))
        self.assertTrue(any("UNSET_TRACKER_TOKEN_X is not set" in p for p in rows["plain"]["problems"]))
        self.assertEqual(rows["plain"]["token"], "unset")
        self.assertTrue(any("unknown type" in p for p in rows["weird"]["problems"]))
        self.assertTrue(any("does not compile" in p for p in rows["weird"]["problems"]))
        rc, text, _ = run("doctor")
        self.assertIn("FAIL filetok", text)
        self.assertIn("ok   tracker", text)
        self.assertNoSentinel(out, text, err)
        os.chmod(tf, 0o600)
        rc, out, _ = run("doctor", "--json")
        rows = {r["name"]: r for r in json.loads(out)}
        self.assertTrue(rows["filetok"]["ok"], rows["filetok"])
        self.assertEqual(rows["filetok"]["token"], "set")
        self.assertNoSentinel(out)

    def test_token_from_file_and_dotenv(self):
        tf = os.path.join(CONF, "tok2")
        fd = os.open(tf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, (SENT + "\n").encode())
        os.close(fd)
        write_fleet("tickets:\n  trackers:\n" + http_json_tracker(self.tr.port).replace(
            "env: %s}" % TOKEN_ENV, "token_file: tok2}"))
        rc, out, err = run("get", "WEB-7", "--json", env={TOKEN_ENV: ""})
        self.assertEqual(rc, 0, err)
        self.assertIn("[redacted:tracker_token]", out)
        self.assertNoSentinel(out, err, home_text())
        # From <config dir>/.env when the process env lacks it.
        write_fleet("tickets:\n  trackers:\n" + http_json_tracker(self.tr.port))
        with open(os.path.join(CONF, ".env"), "w") as fh:
            fh.write("%s=%s\n" % (TOKEN_ENV, SENT))
        try:
            rc, out, err = run("get", "WEB-1", "--refresh", env={TOKEN_ENV: ""})
            self.assertEqual(rc, 0, err)
        finally:
            os.remove(os.path.join(CONF, ".env"))


class RoutingTests(Base):
    def test_ambiguous_and_explicit(self):
        write_fleet("tickets:\n  trackers:\n" + http_json_tracker(self.tr.port) +
                    http_json_tracker(self.tr.port, name="mirror", pattern="^WEB-[0-9]+$"))
        n = self.tr.count()
        rc, _, err = run("get", "WEB-1")
        self.assertEqual(rc, 2)
        self.assertIn("ticket_ambiguous", err)
        self.assertIn("candidates: tracker:WEB-1, mirror:WEB-1", err)
        self.assertEqual(self.tr.count(), n, "an ambiguous ref never reaches a tracker")
        rc, out, err = run("get", "mirror:WEB-1", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["provider"], "mirror")
        rc, out, err = run("get", "API-3")  # only tracker's pattern takes API-
        self.assertEqual(rc, 3, err)
        with self.assertRaises(TicketError) as cm:
            tickets.load().route("WEB-1")
        self.assertEqual((cm.exception.status, cm.exception.to_json()["candidates"]),
                         (409, ["tracker:WEB-1", "mirror:WEB-1"]))

    def test_unknown_and_bad_refs(self):
        for ref, code in (("OPS-1", "unknown_ticket_ref"), ("nope:WEB-1", "unknown_ticket_ref"),
                          ("tracker:OPS-1", "unknown_ticket_ref"), ("WEB 1", "bad_ticket_ref"),
                          ("WEB-1\n", "bad_ticket_ref"), ("../x", "bad_ticket_ref"), ("W" * 200, "bad_ticket_ref"),
                          ("tracker:", "bad_ticket_ref")):
            with self.subTest(ref=ref):
                rc, _, err = run("get", ref)
                self.assertEqual(rc, 2, err)
                self.assertIn(code, err)
        # A pattern with $ must not accept a trailing newline: fullmatch is used.
        self.assertFalse(tickets.load().providers[0].matches("WEB-1\n"))

    def test_path_language(self):
        doc = {"data": [{"displayId": "WEB-1", "id": 11}, {"displayId": "WEB-2", "id": 12}], "n": [5, 6]}
        self.assertEqual(tpl.lookup(doc, "$.data[?displayId=={key}].id", {"key": "WEB-2"}), 12)
        self.assertEqual(tpl.lookup(doc, "$.n[1]"), 6)
        self.assertIsNone(tpl.lookup(doc, "$.data[?displayId=={key}].id", {"key": "WEB-3"}))
        self.assertIsNone(tpl.lookup(doc, "$.missing.deeper"))
        for bad in ("data.id", "$.a()", "$[__import__]", "$.a;b"):
            with self.assertRaises(tpl.TemplateError):
                tpl.lookup(doc, bad)
        self.assertEqual(tpl.fill_body({"body": "{text}", "n": "{n}", "s": "id {n}"}, {"text": "a\"b", "n": 3}),
                         {"body": "a\"b", "n": 3, "s": "id 3"})


class SsrfTests(Base):
    def test_http_refused_and_bad_base(self):
        for base, why in (("http://tracker.example.com", "https"), ("https://user:pw@tracker.example.com", "user name"),
                          ("https://tracker.example.com/?x=1", "query"), ("ftp://tracker.example.com", "https"),
                          ("https://tracker.example.com\\@evil.example.com", "")):
            with self.subTest(base=base):
                write_fleet("tickets:\n  trackers:\n" + http_json_tracker(self.tr.port).replace(
                    "http://127.0.0.1:%d" % self.tr.port, base))
                rc, _, err = run("get", "WEB-1")
                self.assertEqual(rc, 1, err)
                self.assertIn("ticket_unconfigured", err)
                p = tickets.load().providers[0]
                self.assertTrue(any(pr.startswith("base_url") for pr in p.problems), p.problems)
                if why:
                    self.assertTrue(any(why in pr for pr in p.problems), p.problems)

    def test_redirect_not_followed(self):
        n = self.other.count()
        rc, _, err = run("get", "WEB-8")
        self.assertEqual(rc, 1)
        self.assertIn("ticket_upstream", err)
        self.assertIn("redirect", err)
        self.assertEqual(self.other.count(), n, "the redirect target was never contacted")

    def test_host_pinned(self):
        # A resolved value is one encoded segment: it cannot climb the path or name another host.
        n = self.tr.count()
        rc, _, err = run("get", "WEB-13")
        self.assertEqual(rc, 0, err)
        last = self.tr.requests[-1]
        self.assertEqual(last["host"], "127.0.0.1:%d" % self.tr.port)
        self.assertEqual(last["raw"], "/api/v1/projects/..%2F..%2Fadmin%40evil.example.com/issues/u-13")
        self.assertEqual(self.tr.count(), n + 2)
        # ".." as a whole segment is refused before any request.
        n = self.tr.count()
        rc, _, err = run("get", "WEB-12")
        self.assertEqual(rc, 1)
        self.assertIn("ticket_upstream", err)
        self.assertEqual(self.tr.count(), n + 1, "only the resolve call went out")
        base = "https://tracker.example.com"
        for ctx in ({"id": "@evil.example.com"}, {"id": "//evil.example.com"}, {"id": "x?y#z"}, {"id": "%2e%2e"}):
            url = net.build_url(base, "/i/{id}", ctx)
            self.assertTrue(url.startswith(base + "/i/"), url)
            self.assertEqual(urllib.parse.urlsplit(url).hostname, "tracker.example.com")
        with self.assertRaises(TicketError):
            net.build_url(base, "/i/{id}", {"id": ".."})
        with self.assertRaises(TicketError):
            net.build_url(base, "@evil.example.com/{id}", {"id": "1"})

    def test_bad_path_templates_refused(self):
        for path in ("//evil.example.com/x/{id}", "https://evil.example.com/{id}", "/a/../b/{id}", "/a?x={id}",
                     "relative/{id}", "/a/{id}#f"):
            with self.subTest(path=path):
                write_fleet("tickets:\n  trackers:\n" + http_json_tracker(self.tr.port).replace(
                    "/api/v1/projects/{project}/issues/{id}", path))
                p = tickets.load().providers[0]
                self.assertTrue(any("get.path" in pr for pr in p.problems), p.problems)


class SecretTests(Base):
    def test_sentinel_never_leaks(self):
        rc, out, err = run("get", "WEB-7", "--json")
        self.assertEqual(rc, 0, err)
        t = json.loads(out)
        self.assertIn("[redacted:%s]" % TOKEN_ENV, t["excerpt"])
        self.assertIn("[redacted:%s]" % TOKEN_ENV, t["title"])
        rc2, out2, err2 = run("get", "WEB-7")
        # Error bodies that echo the token: 404 on resolve, 500, and a 401 with a wrong token.
        rc3, out3, err3 = run("get", "WEB-404")
        rc4, out4, err4 = run("get", "WEB-9")
        self.assertIn("HTTP 500", err4)
        rc5, out5, err5 = run("get", "WEB-1", "--refresh", env={TOKEN_ENV: SENT + "x"})
        self.assertEqual(rc5, 1)
        self.assertIn("ticket_auth", err5)
        rc6, out6, err6 = run("doctor", "--json")
        self.assertNoSentinel(out, err, out2, err2, out3, err3, out4, err4, out5, err5, out6, err6, home_text())
        self.assertNotIn(SENT + "x", out5 + err5)


class LimitTests(Base):
    def test_429_short_retry_after_waited_once(self):
        self.tr.throttle["slow"] = [1]
        n = self.tr.count()
        t0 = time.time()
        rc, out, err = run("get", "WEB-10")
        self.assertEqual(rc, 0, err)
        self.assertGreaterEqual(time.time() - t0, 0.9)
        self.assertEqual(self.tr.count(), n + 3)  # resolve, 429, retry

    def test_429_long_retry_after_backs_off(self):
        self.tr.throttle["slower"] = [120]
        rc, _, err = run("get", "WEB-11")
        self.assertEqual(rc, 5, err)
        self.assertIn("ticket_rate_limited", err)
        self.assertIn("retry_after: 120 s", err)
        n = self.tr.count()
        rc, _, err = run("get", "WEB-1", "--refresh")
        self.assertEqual(rc, 5, err)
        self.assertEqual(self.tr.count(), n, "while backing off, nothing is sent")
        st = json.load(open(os.path.join(HOME, "tickets", "state", "tracker.json")))
        self.assertGreater(st["blocked_until"], time.time() + 100)
        self.assertEqual(limits.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0)
        self.assertEqual(limits.parse_retry_after("junk"), 60)

    def test_token_bucket(self):
        write_fleet("tickets:\n  trackers:\n" + http_json_tracker(self.tr.port, extra="      rate_per_min: 3\n"))
        reg = tickets.load()
        tickets.get("WEB-1", registry=reg, home=HOME)          # 2 requests (resolve + get)
        with self.assertRaises(TicketError) as cm:
            tickets.get("WEB-2", registry=reg, home=HOME)      # 1 left: resolve passes, get is refused
        self.assertEqual((cm.exception.code, cm.exception.status), ("ticket_rate_limited", 429))
        self.assertGreaterEqual(cm.exception.retry_after, 1)
        # The cache still answers.
        self.assertEqual(tickets.get("WEB-1", registry=reg, home=HOME)["key"], "WEB-1")

    def test_circuit_breaker(self):
        reg = tickets.load()
        for _ in range(limits.BREAKER_FAILURES):   # WEB-99: the resolve call itself fails (500)
            with self.assertRaises(TicketError):
                tickets.get("WEB-99", registry=reg, home=HOME)
        n = self.tr.count()
        with self.assertRaises(TicketError) as cm:
            tickets.get("WEB-2", registry=reg, home=HOME)
        self.assertEqual(cm.exception.code, "ticket_upstream")
        self.assertIn("paused", cm.exception.detail)
        self.assertEqual(self.tr.count(), n)


class RouteTests(Base):
    """GET /api/tickets/<ref> on board-serve: approvers ∪ release_managers only."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.port = free_port()
        write_fleet(cls.fleet(approvers="[local]"))
        cls.err = open(os.path.join(TMP, "serve.err"), "w", encoding="utf-8")
        cls.srv = subprocess.Popen([os.path.join(REPO, "bin", "board-serve"), "--quiet"], stderr=cls.err,
                                   stdout=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls.get("/api/me")
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
        super().tearDownClass()

    @classmethod
    def fleet(cls, approvers="[]", rms="[]"):
        return ("serve:\n  port: %d\napprovers: %s\nrelease_managers: %s\ntickets:\n  trackers:\n"
                % (cls.port, approvers, rms) + http_json_tracker(cls.tr.port) +
                http_json_tracker(cls.tr.port, name="mirror", pattern="^API-[0-9]+$"))

    def setUp(self):
        reset_home()

    @classmethod
    def get(cls, path):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (cls.port, path))
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode(), dict(e.headers)

    def test_non_approver_403_before_any_tracker_call(self):
        write_fleet(self.fleet(approvers="[owner@example.com]"))
        n = self.tr.count()
        st, body, _ = self.get("/api/tickets/WEB-1")
        self.assertEqual(st, 403, body)
        self.assertEqual(json.loads(body)["error"], "not_approver")
        self.assertEqual(self.tr.count(), n)
        ev = events()[-1]
        self.assertEqual((ev["type"], ev["result"], ev["by"]), ("ticket", "refused", "local"))
        write_fleet(self.fleet())
        st, body, _ = self.get("/api/tickets/WEB-1")
        self.assertEqual((st, json.loads(body)["error"]), (403, "no_approvers"))
        self.assertEqual(self.tr.count(), n)

    def test_approver_and_release_manager_200(self):
        for appr, rms, by in (("[local]", "[]", "local"), ("[owner@example.com]", "[LOCAL]", "LOCAL")):
            with self.subTest(approvers=appr, release_managers=rms):
                reset_home()
                write_fleet(self.fleet(approvers=appr, rms=rms))
                st, body, hdrs = self.get("/api/tickets/WEB-7")
                self.assertEqual(st, 200, body)
                t = json.loads(body)
                self.assertEqual((t["provider"], t["key"], t["status_category"]), ("tracker", "WEB-7", "done"))
                self.assertIn("[redacted:", t["excerpt"])
                self.assertEqual(hdrs.get("Cache-Control"), "no-store")
                self.assertNoSentinel(body)
                ev = events()[-1]
                self.assertEqual((ev["by"], ev["via"], ev["result"]), (by, "loopback", "ok"))

    def test_errors(self):
        write_fleet(self.fleet(approvers="[local]") +
                    http_json_tracker(self.tr.port, name="mirror2", pattern="^WEB-[0-9]+$"))
        st, body, _ = self.get("/api/tickets/WEB-1")
        self.assertEqual(st, 409, body)
        self.assertEqual(json.loads(body)["candidates"], ["tracker:WEB-1", "mirror2:WEB-1"])
        st, body, _ = self.get("/api/tickets/tracker%3AWEB-1")
        self.assertEqual(st, 200, body)
        st, body, _ = self.get("/api/tickets/OPS-1")
        self.assertEqual((st, json.loads(body)["error"]), (400, "unknown_ticket_ref"))
        st, body, _ = self.get("/api/tickets/tracker:WEB-404")
        self.assertEqual((st, json.loads(body)["error"]), (404, "ticket_not_found"))
        st, body, _ = self.get("/api/tickets/tracker:WEB-9")
        self.assertEqual((st, json.loads(body)["error"]), (502, "ticket_upstream"))
        self.assertNoSentinel(body)
        self.tr.throttle["slower"] = [300]
        st, body, hdrs = self.get("/api/tickets/tracker:WEB-11")
        self.assertEqual((st, json.loads(body)["error"]), (429, "ticket_rate_limited"))
        self.assertEqual(hdrs.get("Retry-After"), "300")
        st, body, _ = self.get("/api/tickets/a%2F..%2Fb")
        self.assertEqual((st, json.loads(body)["error"]), (400, "bad_ticket_ref"))
        st, body, _ = self.get("/api/tickets/")
        self.assertEqual(st, 404)

    def test_zz_serve_stderr_clean(self):
        self.err.flush()
        with open(os.path.join(TMP, "serve.err"), encoding="utf-8") as fh:
            self.assertNoSentinel(fh.read())


if __name__ == "__main__":
    unittest.main()
