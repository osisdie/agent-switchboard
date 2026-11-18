#!/usr/bin/env python3
"""serve_api: board-serve's public API, /api/v1 (docs/contract.md "Public API (/api/v1)").

Routes from the table in lib/api_spec.py, authenticated by a board API key (lib/apikeys.py):
`Authorization: Bearer asb_live_…`. A key is the only identity here: the local listener's `local`
and a tunnel email never count on their own. A key never carries authority: writes are task
bookkeeping and messages (relayed as information), and a ticket take only creates a pending board
decision for an approver.

Order of checks, for every request (after board-serve's framing rules):
  0. a browser's cross-site request: `Sec-Fetch-Site: cross-site|same-site` (403 cross_site), then
     after the Host check an `Origin` that is not this listener's own (403 cross_site). API clients
     send neither, so a hostile page can't make the browser call the API at all;
  1. method: the route's own (GET routes take HEAD too), else 405; Host allowlist (421); a POST
     must be Content-Type application/json (415, before the key);
  2. the listener: local = a direct loopback request (403 otherwise); tunnel = serve.api.remote
     not false (else 403 api_remote_disabled) and a verified Cloudflare Access JWT (401) that is
     either a service token whose common_name is in serve.tunnel.api_service_tokens or an email in
     the tunnel's allowed emails (else 403); a token bound to people ({cn: owner(s)}) also needs
     the key's owner among them (step 4, after the key: 403 token_key_mismatch), and an email login
     only carries keys owned by that email (step 4: 403 identity_key_mismatch);
  3. the route (404), its query parameters (400);
  4. the key: no Authorization header is 401 unauthenticated at once (not counted, not audited:
     a page firing keyless requests can't burn anyone's budget or flood the audit). A malformed or
     wrong key is 401 invalid_key (403 key_wrong_env for a test key), audited, and counted per kid
     when it has one, else per source (429 past serve.api.auth_fail_per_min). Then the route's
     scope (403 insufficient_scope), the key's rate (rate_per_min, burst 20: 429 with Retry-After);
  5. the handler; a POST body is at most 16 KiB (413) and must match the route's body schema
     (400). Free text goes to the CLI only in a 0600 temp file under $BOARD_HOME.
No response ever carries an Access-Control-* header (no CORS).
Audited in events.jsonl as type "api": every refusal at step 4 (but a keyless request), every
write (with its result and the ids it touched), and each (key, read route) success at most once a
minute. Never a key.

POST /mcp (handle_mcp) is the MCP server's HTTP transport (lib/mcp_tools.py): the same steps 0-4 for
the key, then each tool call runs as its /api/v1 route here: grant() (scope, rate, audit with
"mcp": true) and the route's own Api handler (a write tool: the route's write handler).
"""
import glob
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.parse

import api_spec
import apikeys
import fleet

BURST = 20
AUTH_FAIL_PER_MIN = 10
TAKE_KIND = "ticket_take_request"   # a decision only an approver answers; never relayed to a session
MAX_PENDING_TAKES = 3               # per key
MAX_API_SSE = 8      # per listener, apart from the page's own streams (serve.MAX_SSE)
AUDIT_EVERY_S = 60
ROUTES = [dict(r, rx=api_spec.compile_path(r["path"])) for r in api_spec.ROUTES]
QUERY_RX = {k: re.compile(v[0]) for k, v in api_spec.QUERY.items()}
BEARER_RE = re.compile(r"(?i)bearer[ \t]+(\S+)")
_lock = threading.Lock()
_buckets = {}        # kid -> [tokens, last monotonic]
_fails = {}          # source -> [tokens, last monotonic]
_audited = {}        # (kid, handler) -> monotonic of the last audited success
keyless = [0]        # requests without an Authorization header (a count only, never audited)


class ApiError(Exception):
    def __init__(self, status, error, detail="", headers=None, extra=None):
        super().__init__(error)
        self.status, self.error, self.detail, self.headers = status, error, detail, headers or {}
        self.extra = extra or {}     # more fields for the error body (and its audit row): {"reason": ...}


def is_v1(raw_path):
    p = urllib.parse.urlsplit(raw_path).path
    return p == api_spec.PREFIX or p.startswith(api_spec.PREFIX + "/")


def api_settings(cfg):
    s = cfg.get("serve") if isinstance(cfg.get("serve"), dict) else {}
    a = s.get("api") if isinstance(s.get("api"), dict) else {}
    fails = a.get("auth_fail_per_min", AUTH_FAIL_PER_MIN)
    if not isinstance(fails, int) or isinstance(fails, bool) or not 1 <= fails <= 10000:
        fails = AUTH_FAIL_PER_MIN
    return {"enabled": a.get("enabled") is not False, "remote": a.get("remote") is not False,
            "service_tokens": fleet.service_tokens(cfg),     # {cn: frozenset(owners) | None (unbound)}
            "auth_fail_per_min": fails, "request_ttl_ms": apikeys.request_ttl_ms(cfg)}


IDLE_S = 60          # every bucket is full again after a minute: an idle entry carries nothing
PRUNE_EVERY_S = 30
_pruned = [0.0]


def _prune(now):
    """Drop bucket entries idle for a minute (they are full again) and audit marks older than
    AUDIT_EVERY_S, so the tables don't grow with every tunnel IP or kid. Caller holds _lock."""
    if now - _pruned[0] < PRUNE_EVERY_S:
        return
    _pruned[0] = now
    for table in (_fails, _buckets):
        for k in [k for k, (_t, last) in table.items() if now - last >= IDLE_S]:
            del table[k]
    for k in [k for k, t in _audited.items() if now - t >= AUDIT_EVERY_S]:
        del _audited[k]


def _take(table, key, per_min, burst, now):
    """Token bucket: (allowed, seconds to wait)."""
    cap = max(1, min(burst, per_min))
    rate = per_min / 60.0
    with _lock:
        _prune(now)
        tokens, last = table.get(key, (float(cap), now))
        tokens = min(cap, tokens + (now - last) * rate)
        if tokens >= 1:
            table[key] = (tokens - 1, now)
            return True, 0
        table[key] = (tokens, now)
        return False, max(1, math.ceil((1 - tokens) / rate))


def _peek_fails(source, per_min, now):
    with _lock:
        tokens, last = _fails.get(source, (float(per_min), now))
        return min(per_min, tokens + (now - last) * per_min / 60.0) >= 1


def reset_limits():
    with _lock:
        _buckets.clear()
        _fails.clear()
        _audited.clear()
        _pruned[0] = 0.0


# ------------------------------------------------------------------ the request
def handle(h, S):
    """Serve one /api/v1 request on handler h (S: the serve module, for its helpers)."""
    via = "tunnel" if h.server.kind == "tunnel" else "local"
    try:
        _browser_checks(h, S)
        if h.command not in ("GET", "HEAD", "POST"):
            raise ApiError(405, "method_not_allowed", "", {"Allow": "GET, HEAD, POST"})
        if h.command == "POST" and \
                (h.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(415, "json_only", "Content-Type must be application/json")
        cfg = fleet.load_fleet()
        st = api_settings(cfg)
        if not st["enabled"]:
            raise ApiError(404, "not_found", "the API is off (serve.api.enabled)")
        client = listener_check(h, S, cfg, st)
        split = urllib.parse.urlsplit(h.path)
        route, params = match(h.command, split.path)
        query = parse_query(route, split.query)
        rec = None
        if route["scope"] != "public":
            rec = authenticate(h, st, route, via, client)
        api = Api(h, S, rec, params, query, via, route)
        if route["method"] == "GET":
            return getattr(api, route["handler"])()
        try:
            return getattr(api, route["handler"])()
        except ApiError as exc:
            api.audit_write(exc.status, error=exc.error, **exc.extra)
            raise
    except ApiError as exc:
        if h.command == "POST" and not getattr(h, "body_read", False):
            h.drain_body()
        extra = dict(exc.headers)
        return h.send_json(exc.status, dict({"error": exc.error, "detail": exc.detail}, **exc.extra), extra)
    except Exception as exc:  # noqa: BLE001 - answer with JSON, never drop the connection
        S.log("%s %s failed: %r" % (h.command, urllib.parse.urlsplit(h.path).path, exc))
        return h.send_json(500, {"error": "internal"})


def listener_check(h, S, cfg, st):
    """Who reaches the API at all; returns a label for the audit ("loopback", "service:<cn>",
    "email:<e>"). Never an identity: the key is that."""
    if h.server.kind == "local":
        try:
            h.identity()  # loopback peer, no forwarding headers
        except S.Refused as exc:
            raise ApiError(exc.status, exc.error, exc.detail) from None
        return "loopback"
    if not st["remote"]:
        raise ApiError(403, "api_remote_disabled", "the API is served on the local listener only (serve.api.remote)")
    jwt = h.headers.get("Cf-Access-Jwt-Assertion")
    if not jwt:
        raise ApiError(401, "unauthenticated", "Cloudflare Access token missing")
    try:
        claims = S.verify_jwt(jwt.strip(), h.server.jwks, h.server.cfg["aud"], h.server.cfg["team_domain"],
                              require_email=False)
    except S.JwtError as exc:
        S.log("api: Access token rejected (%s)" % exc)
        raise ApiError(401, "unauthenticated", "Cloudflare Access token rejected") from None
    email = str(claims.get("email") or "").strip().lower()
    cn = str(claims.get("common_name") or "").strip().lower()
    if email:
        if email in S.tunnel_allowed(cfg):
            return "email:" + email
    elif cn and cn in st["service_tokens"]:
        return "service:" + cn
    S.log("api: Access %s is not allowed on the API" % ("email" if email else "service token"))
    raise ApiError(403, "not_allowed", "this Access identity may not use the API (serve.tunnel.api_service_tokens)")


def match(method, path):
    want = "GET" if method == "HEAD" else method
    allowed = []
    for r in ROUTES:
        m = r["rx"].fullmatch(path)
        if m:
            if r["method"] == want:
                return r, m.groupdict()
            allowed.append(r["method"])
    if allowed:
        allow = ", ".join(sorted(set(allowed) | ({"HEAD"} if "GET" in allowed else set())))
        raise ApiError(405, "method_not_allowed", "this route takes %s" % allow, {"Allow": allow})
    raise ApiError(404, "not_found")


def check_body(schema, v, where="body"):
    """The subset of JSON Schema the route table uses; raises ApiError(400) with a short reason."""
    def bad(msg):
        raise ApiError(400, "bad_body", "%s: %s" % (where, msg))
    if "anyOf" in schema:
        for alt in schema["anyOf"]:
            try:
                return check_body(alt, v, where)
            except ApiError:
                pass
        bad("not an accepted value")
    if "const" in schema and v != schema["const"]:
        bad("must be %s" % json.dumps(schema["const"]))
    if "enum" in schema and v not in schema["enum"]:
        bad("one of %s" % ", ".join(str(x) for x in schema["enum"]))
    t = schema.get("type")
    if t == "object":
        if not isinstance(v, dict):
            bad("an object")
        for k in schema.get("required", []):
            if k not in v:
                bad("%s is required" % k)
        if len(v) > schema.get("maxProperties", 64) or len(v) < schema.get("minProperties", 0):
            bad("%s fields" % ("too many" if len(v) > schema.get("maxProperties", 64) else "too few"))
        props, extra = schema.get("properties", {}), schema.get("additionalProperties", True)
        names = schema.get("propertyNames")
        for k, x in v.items():
            if names is not None:
                check_body(names, k, where + " key")
            if k in props:
                check_body(props[k], x, "%s.%s" % (where, k))
            elif extra is False:
                bad("unknown field %s" % str(k)[:40])
            elif isinstance(extra, dict):
                check_body(extra, x, "%s.%s" % (where, str(k)[:40]))
    elif t == "string":
        if not isinstance(v, str):
            bad("a string")
        if not schema.get("minLength", 0) <= len(v) <= schema.get("maxLength", 10 ** 6):
            bad("length %d to %d" % (schema.get("minLength", 0), schema.get("maxLength", 10 ** 6)))
        pat = schema.get("pattern")
        # an anchored pattern must match the whole value: in Python "$" also matches before a
        # trailing newline, so ^...$ is checked with fullmatch
        if pat and not (re.fullmatch(pat[1:-1], v) if pat.startswith("^") and pat.endswith("$") else re.search(pat, v)):
            bad("does not match %s" % pat)
    elif t == "integer":
        if not isinstance(v, int) or isinstance(v, bool) or not schema.get("minimum", -2 ** 63) <= v \
                <= schema.get("maximum", 2 ** 63):
            bad("an integer from %s to %s" % (schema.get("minimum"), schema.get("maximum")))
    elif t == "boolean" and not isinstance(v, bool):
        bad("true or false")
    return v


def parse_query(route, raw):
    try:
        qs = urllib.parse.parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=8)
    except ValueError:
        raise ApiError(400, "bad_request", "bad query string") from None
    allowed = route.get("query", [])
    out = {}
    for k, vals in qs.items():
        if k not in allowed or len(vals) != 1 or not QUERY_RX[k].fullmatch(vals[0]):
            raise ApiError(400, "bad_request", "query: %s" % (", ".join(allowed) or "none accepted"))
        out[k] = vals[0]
    for k in route.get("query_required", []):
        if k not in out:
            raise ApiError(400, "bad_request", "query: %s is required" % k)
    return out


def _audit(h, ev):
    ev = dict({"ts": int(time.time() * 1000), "type": "api"}, **ev)
    try:
        with open(os.path.join(h.server.home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def authenticate(h, st, route, via, client):
    now = time.monotonic()
    auth = h.headers.get_all("Authorization") or []
    if not auth:
        with _lock:
            keyless[0] += 1
        raise ApiError(401, "unauthenticated", "an API key is required: Authorization: Bearer asb_live_…",
                       {"WWW-Authenticate": 'Bearer realm="board"'})
    source = ("tunnel:%s" % (h.headers.get("Cf-Connecting-Ip") or "-")[:64] if via == "tunnel"
              else "local:%d" % h.server.server_address[1])
    m = BEARER_RE.fullmatch(auth[0].strip()) if len(auth) == 1 else None
    key = m.group(1) if m else ""
    km = apikeys.KEY_RE.fullmatch(key)          # the shape first: junk never reaches a file lookup
    kid = km.group(2) if km else None
    bucket = "kid:" + kid if kid else source     # guessing one key's secret, or junk from one place
    per_min = st["auth_fail_per_min"]
    base = {"action": "auth_fail", "route": route["path"], "via": via, "client": client}

    def fail(status, error, detail, reason):
        ok, wait = _take(_fails, bucket, per_min, per_min, now)
        if ok:
            _audit(h, dict(base, key_id=kid, reason=reason, result=status))
        hdr = {"WWW-Authenticate": 'Bearer realm="board"'} if status == 401 else {}
        if not ok:
            raise ApiError(429, "rate_limited", "too many failed attempts", {"Retry-After": str(wait)})
        raise ApiError(status, error, detail, hdr)

    spent = not _peek_fails(bucket, per_min, now)
    if not km:
        if spent:              # junk from one source past its budget: refused, not audited
            raise ApiError(429, "rate_limited", "too many failed attempts", {"Retry-After": "60"})
        fail(401, "invalid_key", "the key is not valid (Authorization: Bearer asb_live_…)", "malformed")
    rec, reason = apikeys.verify(key, "live", h.server.home, ttl_ms=st["request_ttl_ms"])
    if rec is None and spent:
        # someone keeps guessing this kid's secret: wrong guesses wait (not audited), but the
        # holder of the real key is never locked out by them
        raise ApiError(429, "rate_limited", "too many failed attempts", {"Retry-After": "60"})
    if rec is None:
        if reason == "wrong_env":
            fail(403, "key_wrong_env", "a test key works only on the mock board", reason)
        fail(401, "invalid_key", "the key is not valid", reason)
    # A service token bound to people (serve.tunnel.api_service_tokens {cn: owner(s)}) carries only
    # their keys: a leaked token plus someone else's valid key is refused. Checked after the key, so
    # a bad key is still 401 first, and before grant(); counted and audited like a failed key (cn
    # and kid, never a key). /mcp authenticates here too, so its tool calls inherit the check.
    if client.startswith("service:"):
        cn = client[len("service:"):]
        owners = st["service_tokens"].get(cn)
        if owners is not None:
            if str(rec.get("owner") or "").strip().lower() not in owners:
                base["token"] = cn
                fail(403, "token_key_mismatch", "this key's owner is not bound to this Access service token "
                     "(serve.tunnel.api_service_tokens)", "token_key_mismatch")
            rec["_token_owner_ok"] = True   # in memory only: grant() audits it
    # A person's own Access login (an email JWT) carries only that person's keys: the key's owner
    # must be that email (case-insensitive). A stolen key used from someone else's login, or a key
    # owned by `local`, is refused. Same place and same fail() path as the token binding above.
    elif client.startswith("email:"):
        if str(rec.get("owner") or "").strip().lower() != client[len("email:"):]:
            fail(403, "identity_key_mismatch", "this key is not issued to the Access identity that uses it",
                 "identity_key_mismatch")
        rec["_identity_owner_ok"] = True    # in memory only: grant() audits it
    grant(h, rec, route, via, client, now)
    rec["_key"] = key  # for the SSE stream's re-check; never written or logged
    rec["_ttl"] = st["request_ttl_ms"]
    return rec


def grant(h, rec, route, via, client, now, mcp=False):
    """A verified key on a route: its scope (403 insufficient_scope, audited), its rate (429), and
    the success audit (at most once a minute per key and route). /mcp runs every tool call through
    this too (mcp=True: audited with "mcp": true)."""
    who = {"key_id": rec["id"], "owner": rec.get("owner"), "name": rec.get("name"), "route": route["path"],
           "via": via, "client": client}
    if mcp:
        who["mcp"] = True
    if rec.get("_token_owner_ok"):
        who["token_owner_ok"] = True
    if rec.get("_identity_owner_ok"):
        who["identity_owner_ok"] = True
    scope = route["scope"]
    if scope not in (None, "public") and scope not in (rec.get("scopes") or []):
        _audit(h, dict(who, action=route["handler"], result=403, reason="insufficient_scope"))
        raise ApiError(403, "insufficient_scope", "this route needs the %s scope" % scope)
    rate = rec.get("rate_per_min") if isinstance(rec.get("rate_per_min"), int) else apikeys.DEFAULT_RATE
    ok, wait = _take(_buckets, rec["id"], max(1, rate), BURST, now)
    if not ok:
        raise ApiError(429, "rate_limited", "over %d requests a minute" % rate, {"Retry-After": str(wait)})
    apikeys.mark_used(rec["id"], h.server.home)
    rec["_audit"] = who
    if route.get("method", "GET") != "GET":
        return                                       # writes: one audit row each, by the handler
    mark = (rec["id"], route["handler"], mcp)
    with _lock:
        last = _audited.get(mark)
        due = last is None or now - last >= AUDIT_EVERY_S
        if due:
            _audited[mark] = now
    if due:
        _audit(h, dict(who, action=route["handler"], result=200))


# ------------------------------------------------------------------ MCP (POST /mcp)
MCP_PATH = "/mcp"
MCP_ROUTE = {"path": MCP_PATH, "scope": None, "handler": "mcp"}


def is_mcp(raw_path):
    return urllib.parse.urlsplit(raw_path).path == MCP_PATH


def _browser_checks(h, S):
    """Step 0 and the Host allowlist, as for /api/v1: no cross-site browser request."""
    if (h.headers.get("Sec-Fetch-Site") or "").strip().lower() in ("cross-site", "same-site"):
        raise ApiError(403, "cross_site", "the API is not for cross-site browser requests")
    try:
        origins = h.check_host()
    except S.Refused as exc:
        raise ApiError(exc.status, exc.error, exc.detail) from None
    origin = h.headers.get("Origin")
    if origin is not None and origin.strip().lower() not in origins:
        raise ApiError(403, "cross_site", "a foreign Origin; the API is not for cross-site browser requests")


class _Capture:
    """The request handler as an Api handler sees it, but send_json keeps the answer instead of
    writing it: /mcp calls the very /api/v1 handlers in process."""

    def __init__(self, h):
        self._h, self.out = h, None

    def __getattr__(self, name):
        return getattr(self._h, name)

    def send_json(self, status, obj, extra=None):
        self.out = (status, obj)


class _McpBackend:
    """mcp_tools' backend inside board-serve: each call is the /api/v1 route itself (match, query,
    scope, rate, audit, handler), for the key that authenticated this /mcp request."""

    def __init__(self, h, S, rec, via, client):
        self.h, self.S, self.rec, self.via, self.client = h, S, rec, via, client

    def scopes(self):
        return list(self.rec.get("scopes") or [])

    def get(self, path):
        split = urllib.parse.urlsplit(path)
        try:
            route, params = match("GET", split.path)
            if route["method"] != "GET" or route["handler"] == "events":
                raise ApiError(404, "not_found")
            query = parse_query(route, split.query)
            grant(self.h, self.rec, route, self.via, self.client, time.monotonic(), mcp=True)
            cap = _Capture(self.h)
            getattr(Api(cap, self.S, self.rec, params, query, self.via), route["handler"])()
            return cap.out or (500, {"error": "internal"})
        except ApiError as exc:
            detail = exc.detail
            if exc.headers.get("Retry-After"):
                detail = "%s (retry after %s s)" % (detail, exc.headers["Retry-After"])
            return exc.status, {"error": exc.error, "detail": detail}


def _mcp_post(self, path, body):
    """A write tool: the /api/v1 POST route itself (match, scope, rate, the route's body schema and
    handler, one audit row with "mcp": true), its body being the tool's arguments."""
    split = urllib.parse.urlsplit(path)
    try:
        route, params = match("POST", split.path)
        if split.query:
            raise ApiError(400, "bad_request", "query: none accepted")
        grant(self.h, self.rec, route, self.via, self.client, time.monotonic(), mcp=True)
        cap = _Capture(self.h)
        api = Api(cap, self.S, self.rec, params, {}, self.via, route)
        api.given_body = body if isinstance(body, dict) else {}
        try:
            getattr(api, route["handler"])()
        except ApiError as exc:
            api.audit_write(exc.status, error=exc.error, **exc.extra)
            raise
        return cap.out or (500, {"error": "internal"})
    except ApiError as exc:
        detail = exc.detail
        if exc.headers.get("Retry-After"):
            detail = "%s (retry after %s s)" % (detail, exc.headers["Retry-After"])
        return exc.status, dict({"error": exc.error, "detail": detail}, **exc.extra)


_McpBackend.post = _mcp_post


def _read_mcp_body(h, S):
    try:
        n = int(h.headers.get("Content-Length") or "-1")
    except ValueError:
        n = -1
    if n < 0:
        raise ApiError(411, "length_required", "Content-Length required")
    if n > S.MAX_BODY:
        raise ApiError(413, "too_large", "body over %d bytes" % S.MAX_BODY)
    raw = h.rfile.read(n)
    h.body_read = True
    return raw


def handle_mcp(h, S):
    """POST /mcp: MCP Streamable HTTP, JSON responses only (no SSE stream, no session id). The same
    checks as /api/v1, in the same order, then the key (any valid key: each tool call is checked
    against its own route's scope), then the JSON-RPC body (application/json, at most 16 KiB)."""
    import mcp_tools   # here: board-serve needs it only when /mcp is used
    via = "tunnel" if h.server.kind == "tunnel" else "local"
    try:
        _browser_checks(h, S)
        if h.command != "POST":
            raise ApiError(405, "method_not_allowed", "MCP over HTTP: POST JSON-RPC (no SSE stream)",
                           {"Allow": "POST"})
        if (h.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(415, "json_only", "Content-Type must be application/json")
        pv = h.headers.get("MCP-Protocol-Version")
        if pv is not None and pv.strip() not in mcp_tools.PROTOCOL_VERSIONS:
            raise ApiError(400, "bad_protocol_version", "MCP-Protocol-Version: one of %s"
                           % ", ".join(mcp_tools.PROTOCOL_VERSIONS))
        cfg = fleet.load_fleet()
        st = api_settings(cfg)
        if not st["enabled"]:
            raise ApiError(404, "not_found", "the API is off (serve.api.enabled)")
        client = listener_check(h, S, cfg, st)
        if urllib.parse.urlsplit(h.path).query:
            raise ApiError(400, "bad_request", "query: none accepted")
        rec = authenticate(h, st, MCP_ROUTE, via, client)
        raw = _read_mcp_body(h, S)
        out = mcp_tools.Server(_McpBackend(h, S, rec, via, client)).handle_raw(raw)
        if out is None:
            h.send_response(202)
            h.send_header("Content-Length", "0")
            h.common_headers()
            h.end_headers()
            return None
        data = out.encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.common_headers()
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(data)
        return None
    except ApiError as exc:
        if h.command == "POST" and not getattr(h, "body_read", False):
            h.drain_body()
        return h.send_json(exc.status, {"error": exc.error, "detail": exc.detail}, dict(exc.headers))
    except Exception as exc:  # noqa: BLE001 - answer with JSON, never drop the connection
        S.log("%s /mcp failed: %r" % (h.command, exc))
        return h.send_json(500, {"error": "internal"})


# ------------------------------------------------------------------ handlers
class Api:
    def __init__(self, h, S, rec, params, query, via, route=None):
        self.h, self.S, self.rec, self.params, self.query, self.via = h, S, rec, params, query, via
        self.route = route or {}
        self.given_body = None

    def ok(self, obj):
        """Every read is masked: a key is never an owner, so other people's emails become role
        labels (lib/pii.py; no "you": a key is not a verified person)."""
        return self.h.send_json(200, self.S.pii.mask(obj))

    def _st(self, kick=False):
        """The board state as a viewer sees it: no private card text, no account, nothing writable."""
        h, S = self.h, self.S
        if kick:
            h.hub_kick()
        with h.server.hub.cond:
            st = dict(h.server.hub.state or {})
        st["sessions"] = S.cards_for(st.get("sessions"), False)
        st["operator"] = S.owner_only_operator(st.get("operator"))     # a key is never an owner
        st.update({"mode": "api", "me": None, "writable": False, "can_message": False, "account": None})
        # Q3: an API key is never an approver or release manager, so no ticket text (task titles,
        # sessions' task, ticket relay messages) reaches it: /state, /events, /sessions, /tasks.
        return S.ticket_text_for(st, False)

    def state(self):
        return self.ok(self._st(kick=True))

    def openapi(self):
        return self.ok(api_spec.openapi())

    def whoami(self):
        r = self.rec
        # the key's own owner, shown to its holder: not masked
        return self.h.send_json(200, {"key_id": r["id"], "name": r.get("name"), "owner": r.get("owner"),
                        "scopes": list(r.get("scopes") or []), "expires_at": r.get("expires_at"), "mode": "live"})

    def sessions(self):
        return self.ok({"sessions": self._st().get("sessions") or []})

    def session(self):
        for s in self._st().get("sessions") or []:
            if s.get("code") == self.params["code"]:
                return self.ok(s)
        raise ApiError(404, "not_found", "no such session")

    def decisions(self):
        rows = self._st().get("decisions") or []
        if "status" in self.query:
            rows = [d for d in rows if d.get("status") == self.query["status"]]
        return self.ok({"decisions": rows})

    def decision(self):
        did = self.params["decision_id"]
        for d in self._st().get("decisions") or []:
            if d.get("id") == did:
                return self.ok(d)
        d = self.S.read_json(os.path.join(self.h.server.home, "decisions", did + ".json"))
        if not isinstance(d, dict) or d.get("id") != did:
            raise ApiError(404, "not_found", "no such decision")
        return self.ok(self.S.guard.scrub_tree({k: d[k] for k in self.S.DECISION_KEYS if k in d}))

    def tasks(self):
        return self.ok({"tasks": self._st().get("tasks") or []})

    def task(self):
        tid = self.params["task_id"]
        if tid in (".", ".."):
            raise ApiError(404, "not_found", "no such task")
        t = self.S.read_json(os.path.join(self.h.server.home, "tasks", tid + ".json"))
        if not isinstance(t, dict) or t.get("id") != tid:
            raise ApiError(404, "not_found", "no such task")
        return self.ok(self.S.guard.scrub_tree(self.S.tasks_for([t], False)[0]))   # Q3: the file is read raw

    def schedule(self):
        return self.ok(self._st().get("scheduler") or {})

    def pomodoro(self):
        return self.ok(self._st().get("pomodoro") or {})

    def pomodoro_day(self):
        S = self.S
        try:
            view = S.pomodoro.day_view(S.read_json(os.path.join(self.h.server.home, "pomodoro.json")),
                                       self.query["d"], S.now_ms())
        except ValueError:
            raise ApiError(400, "bad_request", "expected ?d=YYYY-MM-DD") from None
        if view is None:
            raise ApiError(404, "not_found", "not a kept day")
        return self.ok(S.guard.scrub_tree(view))

    def messages(self):
        """The message history, as GET /api/messages gives it to the page, through the viewer filter
        a key gets everywhere (Q3: no ticket text; other people's emails masked) before the search,
        so a search never matches what the key would not see."""
        S = self.S
        try:
            before, limit, to, q = S.history_query({k: [v] for k, v in self.query.items()})
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from None
        return self.ok(S.message_page(self.h.server.home, before, limit, to, q,
                                      view=lambda st: S.pii.mask(S.ticket_text_for(st, False))))

    def message(self):
        S, home, mid = self.S, self.h.server.home, self.params["message_id"]
        m = S.read_json(os.path.join(home, "messages", mid + ".json"))
        if not isinstance(m, dict) or m.get("id") != mid:
            raise ApiError(404, "not_found", "no such message")
        replies = []
        for f in sorted(glob.glob(os.path.join(home, "messages", mid, "replies", "*.json"))):
            r = S.read_json(f)
            if isinstance(r, dict):
                r.setdefault("id", os.path.basename(f)[:-5])
                replies.append(r)
        replies.sort(key=lambda r: S.as_int(r.get("created_at")))
        if m.get("kind") == "ticket" and isinstance(m.get("ticket"), dict):     # Q3: no tracker title or url
            m = dict(m, ticket={k: m["ticket"].get(k) for k in ("provider", "key")})
        return self.ok(S.guard.scrub_tree(dict(m, replies=replies[:50])))

    def events(self):
        """SSE like /api/events, with the key checked again before every frame and keepalive: a
        revoked or expired key ends the stream (event: revoked)."""
        h, S = self.h, self.S
        if h.command == "HEAD":
            raise ApiError(405, "method_not_allowed", "GET only", {"Allow": "GET"})
        hub, srv, key = h.server.hub, h.server, self.rec["_key"]
        with srv.sse_lock:      # the key was checked already; API streams have their own cap
            n = getattr(srv, "api_sse_count", 0)
            if n >= MAX_API_SSE:
                raise ApiError(503, "too_many_streams", "at most %d API event streams" % MAX_API_SSE)
            srv.api_sse_count = n + 1
        try:
            h.close_connection = True
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream; charset=utf-8")
            h.send_header("X-Accel-Buffering", "no")
            h.common_headers()
            h.end_headers()
            seen = -1
            while True:
                with hub.cond:
                    hub.cond.wait_for(lambda: hub.stopping or hub.version != seen, timeout=S.KEEPALIVE_S)
                    if hub.stopping:
                        return None
                    changed = hub.version != seen
                    seen = hub.version
                if apikeys.verify(key, "live", srv.home, ttl_ms=self.rec["_ttl"])[0] is None:
                    h.wfile.write(b"event: revoked\ndata: {}\n\n")
                    h.wfile.flush()
                    return None
                if changed:      # the same masked view as every other read (lib/pii.py)
                    data = json.dumps(self.S.pii.mask(self._st()), ensure_ascii=False)
                    h.wfile.write(("event: state\ndata: %s\n\n" % data).encode("utf-8"))
                else:
                    h.wfile.write(b": keepalive\n\n")
                h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return None
        finally:
            with srv.sse_lock:
                srv.api_sse_count -= 1

    # ------------------------------------------------------------------ writes
    def audit_write(self, result, **kw):
        _audit(self.h, dict(self.rec["_audit"], action=self.route["handler"], result=result, **kw))

    def body(self):
        if self.given_body is not None:          # an MCP tool call: the JSON-RPC arguments, parsed
            return check_body(self.route["body"], self.given_body)
        try:
            b = self.h.read_body()
        except self.S.Refused as exc:
            raise ApiError(exc.status, exc.error, exc.detail) from None
        return check_body(self.route["body"], b)

    def text(self, v, name):
        """One line of free text from a key: tabs and newlines become spaces, other control or
        invisible characters are refused, then the secret guard scrubs it and "[BOARD" is defanged,
        so it never passes for a relay line."""
        v = re.sub(r"[\r\n\t]+", " ", v).strip()
        if not v or self.S.FREE_TEXT_STRIP_RE.search(v):
            raise ApiError(400, "bad_body", "%s: printable text only" % name)
        return self.S.messages.defang(self.S.guard.scrub(v))

    def run(self, args, files=()):
        """bin/board with these arguments (free text only in the 0600 temp files given as
        (option, text) pairs), under the server's write lock. Returns stdout."""
        home, paths, argv = self.h.server.home, [], list(args)
        try:
            for opt, txt in files:
                fd, path = tempfile.mkstemp(prefix=".api.", dir=home)   # 0600
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(txt)
                paths.append(path)
                argv += [opt, path]
            with self.h.server.write_lock:
                rc, out, err = self.h.board(*argv)
        finally:
            for path in paths:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self.h.hub_kick()
        if rc == 8:
            raise ApiError(409, "busy", "the task or store is busy; try again")
        if rc != 0:
            line = (err or out or "failed").strip().splitlines()[-1]
            line = re.sub(r"^board(?: [a-z]+)?: ", "", line)
            raise ApiError(400 if rc in (1, 2) else 500, "refused", self.S.guard.scrub(line)[:200])
        return out

    def known_session(self, code):
        if not self.h.known_code(code):
            raise ApiError(400, "unknown_session", "no session %s on this board" % code)

    def the_task(self):
        tid = self.params["task_id"]
        t = self.S.read_json(os.path.join(self.h.server.home, "tasks", tid + ".json"))
        if tid in (".", "..") or not isinstance(t, dict) or t.get("id") != tid:
            raise ApiError(404, "not_found", "no such task")
        return tid

    def no_new_work(self):
        """An asker's key creates no work while the owner is on do-not-disturb or compute is short
        (lib/availability.py): 409 declined, the canned text as the detail, {reason} in the body and
        the audit row. Nothing is stored. Keys of approvers, release managers and others are not
        affected; bookkeeping on an existing task never is."""
        S = self.S
        cfg = fleet.load_fleet()
        if not S.messages.asker_only_now(self.rec.get("owner"), cfg):
            return
        reason = S.availability.decline_reason(self.h.server.home, cfg)
        if reason:
            raise ApiError(409, "declined", S.availability.reply_text(cfg, reason), extra={"reason": reason})

    def needs_person(self, stage):
        if stage not in api_spec.API_STAGES:
            raise ApiError(403, "needs_approver", "a key may not move a task to %s; that needs a person" % stage)

    def task_new(self):
        b = self.body()
        self.known_session(b["owner"])
        self.no_new_work()
        stage = b.get("stage", "plan")
        self.needs_person(stage)
        out = self.run(["task", "new", "--owner", b["owner"], "--stage", stage],
                       [("--title-file", self.text(b["title"], "title"))])
        tid = out.strip().splitlines()[-1]
        self.audit_write(201, task_id=tid, stage=stage)
        return self.h.send_json(201, {"id": tid, "stage": stage})

    def task_stage(self):
        tid, b = self.the_task(), self.body()
        self.needs_person(b["stage"])
        args = ["task", "stage", tid, b["stage"]]
        for k, v in sorted((b.get("artifacts") or {}).items()):
            if not re.fullmatch(r"[^\s\x00-\x1f\x7f]{1,200}", v) or apikeys.invisible(v) \
                    or self.S.guard.changed(v)[1]:
                raise ApiError(400, "bad_body", "artifacts.%s: a ref (no spaces, no secrets)" % k)
            args += ["--artifact", "%s=%s" % (k, v)]
        self.run(args)
        self.audit_write(200, task_id=tid, stage=b["stage"])
        return self.ok({"id": tid, "stage": b["stage"]})

    def task_set(self):
        tid, b = self.the_task(), self.body()
        args = ["task", "set", tid]
        for k in ("repo", "branch", "base", "size", "heavy", "priority"):
            if k in b:
                v = b[k]
                args += ["--" + k, ("yes" if v else "no") if k == "heavy" else str(v)]
        self.run(args)
        self.audit_write(200, task_id=tid, fields=sorted(b))
        return self.ok({"id": tid, "set": sorted(b)})

    def task_progress(self):
        tid = self.the_task()
        self.body()
        self.run(["task", "progress", tid])
        self.audit_write(200, task_id=tid)
        return self.ok({"id": tid, "progress": True})

    def task_pr(self):
        tid, b = self.the_task(), self.body()
        args = ["task", "pr", tid, "--url", b["url"], "--state", b["state"]]
        for k in ("head", "method"):
            if k in b:
                args += ["--" + k, b[k]]
        if self.S.guard.changed(b["url"])[1]:
            raise ApiError(400, "bad_body", "url: looks like it carries a secret")
        self.run(args)
        self.audit_write(200, task_id=tid, state=b["state"])
        return self.ok({"id": tid, "pr": b["state"]})

    def message_new(self):
        b = self.body()
        if b["to"] not in ("all", self.S.messages.TRIAGE):
            self.known_session(b["to"])
        if b["to"] == "all" and self.S.messages.asker_only_now(self.rec.get("owner")):
            # a key acts for its owner: an asker's key never messages every session (lib/messages.py)
            self.audit_write(403, reason="asker_no_all", to="all")
            raise ApiError(403, "asker_no_all", self.S.messages.ASKER_NO_ALL)
        if not b["body"].strip():
            raise ApiError(400, "bad_body", "body: empty")
        out = self.run(["message", "send", "--to", b["to"], "--api-key", self.rec["id"]], [("--file", b["body"])])
        mid, status = (out.strip().splitlines()[-1].split() + ["", ""])[:2]
        res = {"id": mid, "status": status}
        if status in ("held", "declined"):
            rec = self.S.messages.read(self.h.server.home, mid) or {}
            res["reason"] = rec.get("held_reason" if status == "held" else "declined_reason")
        if status == "declined":       # an asker's key while the owner is on do-not-disturb or compute is short
            res["reply"] = self.S.availability.reply_text(fleet.load_fleet(), res["reason"])
        self.audit_write(201, message_id=mid, to=b["to"], status=status,
                         **({"reason": res["reason"]} if status == "declined" else {}))
        return self.h.send_json(201, res)

    def ticket_take(self):
        """Never runs a take: it asks. A pending board decision of its own kind,
        TAKE_KIND, from "api" (never from a session, so it is never relayed to one as a
        [BOARD decision]), written with its `request` in one atomic write. Only an approver answers
        it on the board; on approve the operator runs the take itself (`board ticket take ... --by
        <answered_by>`), which produces the verified [BOARD ticket] relay. A key can never answer it."""
        # the path segment is decoded once (a "#" arrives as %23), then routed: T1's KEY_RE fullmatch
        S, raw_ref, b = self.S, urllib.parse.unquote(self.params["ref"]), self.body()
        code = b["session"]
        self.known_session(code)
        self.no_new_work()
        # the ref must name a configured tracker's ticket (T1 routing): unknown -> 400, ambiguous -> 409;
        # stored as provider:KEY, the form `board ticket take` takes
        try:
            prov, key = S.tickets.load().route(raw_ref)
        except S.tickets.TicketError as exc:
            raise ApiError(exc.status, exc.code, exc.detail + (
                " (one of %s)" % ", ".join(exc.candidates) if exc.candidates else "")) from None
        ref = "%s:%s" % (prov.name, key)
        note = self.text(b["note"], "note") if b.get("note", "").strip() else ""
        home, kid = self.h.server.home, self.rec["id"]
        mine = 0
        for path in glob.glob(os.path.join(home, "decisions", "*.json")):
            d = S.read_json(path)
            req = d.get("request") if isinstance(d, dict) else None
            if not isinstance(req, dict) or d.get("kind") != TAKE_KIND or d.get("status") != "pending":
                continue
            if req.get("ref") == ref:
                raise ApiError(409, "already_requested", "a take of %s is already pending: %s" % (ref, d.get("id")))
            mine += req.get("key_id") == kid
        if mine >= MAX_PENDING_TAKES:
            raise ApiError(429, "too_many_requests", "this key has %d take requests waiting for an approver" % mine)
        name = S.messages.defang(S.guard.scrub(str(self.rec.get("name") or "")))
        owner = str(self.rec.get("owner") or "")
        now = S.now_ms()
        did = "d-%d-%s" % (now // 1000, os.urandom(4).hex())
        dec = S.guard.scrub_tree({
            "id": did, "kind": TAKE_KIND, "from": "api",
            "question": "Start ticket %s in %s?" % (ref, code), "options": ["approve", "decline"],
            "context": ('Requested through the API by key "%s" (%s).%s Approving runs the take for %s; '
                        'an API key cannot answer this.' % (name, owner, (" Note: " + note) if note else "", code)),
            "status": "pending", "answer": None, "answered_by": None, "answered_at": None, "relayed_at": None,
            "created_at": now,
            "request": {"kind": "ticket_take", "ref": ref, "session": code, "via": "api", "key_id": kid,
                        "key_name": name, "owner": owner, "at": now}})
        path = os.path.join(home, "decisions", did + ".json")
        with self.h.server.write_lock:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            tmp = "%s.tmp.%d" % (path, os.getpid())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(dec, fh, ensure_ascii=False, indent=1)
                fh.write("\n")
            if os.path.exists(path):          # a 1-in-4-billion clash within a second: never overwrite
                os.unlink(tmp)
                raise ApiError(409, "busy", "try again")
            os.replace(tmp, path)
            _audit(self.h, {"type": "ask", "id": did, "from": "api", "kind": TAKE_KIND})
        self.h.hub_kick()
        self.audit_write(202, decision_id=did, ref=ref, session=code)
        return self.h.send_json(202, {"decision_id": did, "status": "pending"})
