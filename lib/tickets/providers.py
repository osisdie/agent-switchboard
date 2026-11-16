"""Ticket providers: the interface, `http-json` (a generic JSON tracker) and `fake` (fixtures).

Provider interface (read in this version; the write methods arrive with ticket take):
  name, type, key_re, capabilities ⊆ {read, comment, transition, assign}
  matches(key) -> bool         get(key) -> raw ticket dict (see tickets.finalize)
  secrets() -> [(name, value)] doctor() -> dict (never a secret value)
  comment / transitions / transition / assign: not implemented yet
"""
import json
import os
import re
import stat
import time
import urllib.parse

import fleet  # noqa: E402

from . import NAME_RE, TicketError, net, tpl
from .limits import Limiter, parse_retry_after

RESERVED_HEADERS = ("host", "content-length", "content-type", "transfer-encoding", "connection", "accept",
                    "user-agent", "upgrade", "te", "trailer", "proxy-authorization", "cookie")
SAFE_VALUE_RE = re.compile(r"[A-Za-z0-9._~-]{1,200}")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
HEADER_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,31}")
DEFAULT_RATE = 30
DEFAULT_RETRY_WAIT_MAX = 2   # a 429 asking for at most this many seconds is waited out once
FAKE_FIXTURES = os.path.join(fleet.REPO, "tests", "fixtures", "tickets", "fake.json")
MAP_FIELDS = ("id", "title", "status", "assignee", "description", "updated_at")


def build(spec, index):
    kind = str(spec.get("type") or "")
    cls = {"http-json": HttpJsonProvider, "fake": FakeProvider}.get(kind)
    if cls is None:
        p = Provider(spec, index)
        p.problems.append("unknown type %r (known: http-json, fake)" % kind[:40])
        return p
    return cls(spec, index)


def read_token(auth, cfg_dir):
    """(value or None, source, problem or None). Never logs the value."""
    env = auth.get("env")
    tfile = auth.get("token_file")
    if env and tfile:
        return None, None, "auth: set env or token_file, not both"
    if env:
        if not isinstance(env, str) or not ENV_NAME_RE.fullmatch(env):
            return None, None, "auth.env is not a variable name"
        v = os.environ.get(env)
        if v:
            return v, "env %s" % env, None
        v = fleet.read_dotenv(os.path.join(cfg_dir, ".env")).get(env)
        if v:
            return v, ".env %s" % env, None
        return None, "env %s" % env, "%s is not set (process env or <config dir>/.env)" % env
    if tfile:
        if not isinstance(tfile, str):
            return None, None, "auth.token_file is not a path"
        p = os.path.expanduser(tfile)
        p = p if os.path.isabs(p) else os.path.join(cfg_dir, p)
        try:
            st = os.stat(p)
        except OSError:
            return None, "token_file", "auth.token_file does not exist or cannot be read"
        if not stat.S_ISREG(st.st_mode):
            return None, "token_file", "auth.token_file is not a regular file"
        if st.st_mode & 0o077:
            return None, "token_file", "auth.token_file must not be readable by group or others (chmod 600)"
        try:
            with open(p, encoding="utf-8") as fh:
                v = next((ln.strip() for ln in fh if ln.strip()), "")
        except (OSError, UnicodeDecodeError):
            return None, "token_file", "auth.token_file cannot be read"
        if not v:
            return None, "token_file", "auth.token_file is empty"
        return v, "token_file", None
    return None, None, "auth needs env or token_file (or auth: none)"


class Provider:
    type = ""
    capabilities = ()
    default_key_pattern = None

    def __init__(self, spec, index):
        self.spec = spec
        self.problems = []
        name = str(spec.get("name") or "")
        if not NAME_RE.fullmatch(name):
            self.problems.append("name must match [a-z][a-z0-9_-]{0,31}")
            name = "tracker%d" % index
        self.name = name
        self.home = None
        self.cfg_dir = fleet.config_dir()
        pat = spec.get("key_pattern") or self.default_key_pattern
        self.key_pattern = pat if isinstance(pat, str) else None
        self.key_re = None
        if not self.key_pattern:
            self.problems.append("key_pattern is missing")
        else:
            try:
                self.key_re = re.compile(self.key_pattern)
            except re.error as exc:
                self.problems.append("key_pattern does not compile: %s" % exc)
        try:
            self.rate = max(1, min(6000, int(spec.get("rate_per_min") or DEFAULT_RATE)))
        except (TypeError, ValueError):
            self.rate = DEFAULT_RATE
            self.problems.append("rate_per_min is not a number")

    def matches(self, key):
        return bool(self.key_re and self.key_re.fullmatch(key))

    def secrets(self):
        return []

    def usable(self):
        if self.problems:
            raise TicketError("ticket_unconfigured", "tracker %s is not configured correctly (board ticket doctor)" % self.name)

    def get(self, key, home=None):
        self.usable()
        raise TicketError("ticket_unconfigured", "tracker %s cannot read tickets" % self.name)

    def doctor(self):
        return {"name": self.name, "type": self.type or str(self.spec.get("type") or "")[:40], "warnings": [],
                "key_pattern": self.key_pattern, "capabilities": list(self.capabilities),
                "rate_per_min": self.rate, "ok": not self.problems, "problems": list(self.problems)}


class HttpJsonProvider(Provider):
    """A tracker with a JSON API, described entirely by config: base_url, auth, an optional
    `resolve` (display key -> internal ids), and `get` (path, field map, link, status categories)."""
    type = "http-json"
    capabilities = ("read",)

    def __init__(self, spec, index):
        super().__init__(spec, index)
        self.base, err = net.check_base(spec.get("base_url"))
        if err:
            self.problems.append("base_url: %s" % err)
        self.token, self.token_source, self.header, self.scheme = None, None, None, None
        auth = spec.get("auth")
        if auth == "none":
            self.auth = None
        elif isinstance(auth, dict):
            self.auth = auth
            self.header = str(auth.get("header") or "Authorization")
            self.scheme = auth.get("scheme", "Bearer")
            self.scheme = "" if self.scheme in (None, "") else str(self.scheme)
            if not HEADER_RE.fullmatch(self.header):
                self.problems.append("auth.header is not a header name")
            elif self.header.lower() in RESERVED_HEADERS:
                self.problems.append("auth.header %s is reserved (it would break the request)" % self.header)
            if self.scheme and not SCHEME_RE.fullmatch(self.scheme):
                self.problems.append("auth.scheme is not a word")
            self.token, self.token_source, problem = read_token(auth, self.cfg_dir)
            if problem:
                self.problems.append(problem)
            elif any(c in self.token for c in "\r\n\0"):
                self.problems.append("the token contains a line break")
                self.token = None
        else:
            self.auth = None
            self.problems.append("auth is missing (a mapping, or the word none)")
        try:
            self.retry_wait_max = max(0, min(30, int(spec.get("retry_wait_max_seconds", DEFAULT_RETRY_WAIT_MAX))))
        except (TypeError, ValueError):
            self.retry_wait_max = DEFAULT_RETRY_WAIT_MAX
        self.resolve_spec = self._call(spec.get("resolve"), "resolve", optional=True)
        self.get_spec = self._call(spec.get("get"), "get")
        if self.get_spec is not None:
            m = self.get_spec.get("map")
            if not isinstance(m, dict) or "title" not in m:
                self.problems.append("get.map needs at least title")
            else:
                for f, expr in m.items():
                    if f not in MAP_FIELDS:
                        self.problems.append("get.map.%s is not a field (%s)" % (str(f)[:30], ", ".join(MAP_FIELDS)))
                    elif tpl.check_path_expr(expr):
                        self.problems.append("get.map.%s: %s" % (f, tpl.check_path_expr(expr)))
            sets = (self.resolve_spec or {}).get("set") or {}
            known = {"key", "id"} | set(sets)
            for where in (self.get_spec.get("path"), self.get_spec.get("link")):
                missing = tpl.placeholders(where) - known
                if missing:
                    self.problems.append("get uses {%s} that nothing sets" % ", ".join(sorted(missing)))

    def _call(self, c, what, optional=False):
        if c is None:
            if not optional:
                self.problems.append("%s is missing" % what)
            return None
        if not isinstance(c, dict):
            self.problems.append("%s must be a mapping" % what)
            return None
        method = str(c.get("method") or "GET").upper()
        if method != "GET":
            self.problems.append("%s.method must be GET (reads only)" % what)
        path = c.get("path")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") \
                or any(s in path for s in ("://", "?", "#", "\\", "/../", "/./")) or path.endswith(("/..", "/.")) \
                or fleet.CONTROL_RE.search(path) or " " in path:
            self.problems.append("%s.path must be an absolute path on the tracker (no //, scheme, query, ..)" % what)
        q = c.get("query") or {}
        if not isinstance(q, dict) or not all(isinstance(v, (str, int)) for v in q.values()):
            self.problems.append("%s.query must map names to strings" % what)
        sets = c.get("set") or {}
        if not isinstance(sets, dict):
            self.problems.append("%s.set must be a mapping" % what)
        else:
            for var, expr in sets.items():
                if not tpl.PLACEHOLDER_RE.fullmatch("{%s}" % var):
                    self.problems.append("%s.set: %s is not a variable name" % (what, str(var)[:30]))
                elif tpl.check_path_expr(expr):
                    self.problems.append("%s.set.%s: %s" % (what, var, tpl.check_path_expr(expr)))
        link = c.get("link")
        if link is not None and (not isinstance(link, str) or not re.match(r"https?://", link)):
            self.problems.append("%s.link must be an http(s) URL template" % what)
        return dict(c, method=method)

    def secrets(self):
        if self.token:
            return [(self.auth.get("env") or "%s_token" % self.name, self.token)]
        return []

    def headers(self):
        if not self.token:
            return {}
        return {self.header: ("%s %s" % (self.scheme, self.token)) if self.scheme else self.token}

    def fetch(self, call, ctx):
        """GET one templated URL, with the rate limits; returns the parsed JSON."""
        url = net.build_url(self.base, call["path"], ctx, call.get("query"))
        lim = Limiter(self.home or fleet.board_home(), self.name, self.rate)
        for attempt in (1, 2):
            lim.before()
            try:
                resp = net.request(self.base, "GET", url, self.headers())
            except TicketError as exc:
                if exc.code == "ticket_upstream":
                    lim.failure()
                raise
            if resp.status == 429:
                wait = parse_retry_after(resp.headers.get("Retry-After"))
                if attempt == 1 and wait <= self.retry_wait_max:
                    time.sleep(wait)
                    continue
                lim.throttled(wait)
                raise TicketError("ticket_rate_limited", "%s is rate limiting us" % self.name, retry_after=wait)
            break
        if 200 <= resp.status < 300:
            lim.success()
            return resp.json()
        net.log_error_body(self.name, resp)
        if resp.status in (401, 403):
            raise TicketError("ticket_auth", "%s refused our credentials" % self.name)
        if resp.status in (404, 410):
            raise TicketError("ticket_not_found", "not found on %s" % self.name)
        if resp.status >= 500:
            lim.failure()
        raise TicketError("ticket_upstream", "%s answered HTTP %d" % (self.name, resp.status))

    def get(self, key, home=None):
        self.usable()
        self.home = home
        ctx = {"key": key, "id": key}
        try:
            r = self.resolve_spec
            if r is not None:
                doc = self.fetch(r, ctx)
                for var, expr in (r.get("set") or {}).items():
                    val = tpl.lookup(doc, expr, ctx)
                    if val is None:
                        raise TicketError("ticket_not_found", "%s: %s did not resolve" % (self.name, key))
                    if isinstance(val, bool) or not isinstance(val, (str, int, float)):
                        raise TicketError("ticket_upstream", "%s: resolve gave a non-scalar %s" % (self.name, var))
                    val = str(val)
                    # The tracker's answer feeds later path segments: plain id characters only.
                    if not SAFE_VALUE_RE.fullmatch(val) or val in (".", ".."):
                        raise TicketError("ticket_upstream", "%s: resolve gave an unsafe %s" % (self.name, var))
                    ctx[var] = val
            g = self.get_spec
            doc = self.fetch(g, ctx)
            raw = {f: tpl.lookup(doc, expr, ctx) for f, expr in g["map"].items()}
            if raw.get("title") is None:
                raise TicketError("ticket_upstream", "%s: the answer has no title at %s" % (self.name, g["map"]["title"]))
            raw.setdefault("id", ctx.get("id"))
            raw["url"] = tpl.fill_link(g["link"], ctx) if g.get("link") else None
            raw["status_category_map"] = g.get("status_category")
            return raw
        except tpl.TemplateError as exc:
            raise TicketError("ticket_upstream", "%s: %s" % (self.name, exc)) from exc

    def doctor(self):
        d = super().doctor()
        d["host"] = urllib.parse.urlsplit(self.base).netloc if self.base else None
        host = urllib.parse.urlsplit(self.base).hostname if self.base else ""
        if host and re.fullmatch(r"[0-9.]+", host) and host != "127.0.0.1":
            d["warnings"] = ["base_url is an IP address: the certificate cannot match a host name, and the "
                             "address may change; prefer the tracker's DNS name"]
        if self.auth is None and self.spec.get("auth") == "none":
            d["token"] = "none"
        else:
            d["token"] = "set" if self.token else "unset"
            d["token_source"] = self.token_source
        return d


class FakeProvider(Provider):
    """Tickets from a JSON fixture file (`fixtures:`, relative to the config dir; default
    tests/fixtures/tickets/fake.json): {"tickets": {"DEMO-1": {title, status, assignee,
    description, updated_at, url} | {"error": "<ticket error code>"}}}. Never touches the network."""
    type = "fake"
    capabilities = ("read",)
    default_key_pattern = r"^DEMO-[0-9]+$"

    def __init__(self, spec, index):
        super().__init__(spec, index)
        f = spec.get("fixtures")
        if f is None:
            self.fixtures = FAKE_FIXTURES
        elif isinstance(f, str):
            f = os.path.expanduser(f)
            self.fixtures = f if os.path.isabs(f) else os.path.join(self.cfg_dir, f)
        else:
            self.fixtures = None
            self.problems.append("fixtures must be a path")
        if self.fixtures and not os.path.isfile(self.fixtures):
            self.problems.append("fixtures file not found")

    def get(self, key, home=None):
        self.usable()
        lim = Limiter(home or fleet.board_home(), self.name, self.rate)
        lim.before()
        try:
            with open(self.fixtures, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            raise TicketError("ticket_upstream", "fake: fixtures cannot be read") from exc
        item = (doc.get("tickets") or {}).get(key) if isinstance(doc, dict) else None
        if not isinstance(item, dict):
            raise TicketError("ticket_not_found", "not found on %s" % self.name)
        if item.get("error"):
            raise TicketError(str(item["error"]), "fake: %s" % item["error"], retry_after=item.get("retry_after"))
        raw = {f: item.get(f) for f in MAP_FIELDS}
        raw["id"] = item.get("id", key)
        raw["url"] = item.get("url")
        raw["status_category_map"] = doc.get("status_category")
        return raw
