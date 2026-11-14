#!/usr/bin/env python3
"""board-serve: the self-hosted board (the "local adapter" of artifact/board.html).

Usage: serve.py [--port PORT] [--check] [--quiet]
       serve.py --watch [--once] [--tick SECONDS]

Serves the board page and a small JSON API from $BOARD_HOME, bound to 127.0.0.1 only.
See docs/contract.md, "Local adapter API", for the endpoints and the security model.

  GET  /                               the board page, in local mode
  GET  /api/state                      the board state (sessions, decisions, tasks, ...)
  GET  /api/events                     Server-Sent Events: the full state on every change
  POST /api/decisions/<id>/answer      {"answer": "..."}   (via `board answer` / `board press`)
  POST /api/messages                   {"to": "CODE"|"all", "body": "..."}

--check prints the resolved settings and exits. --watch serves nothing: it prints one line per
item the local-mode operator must relay (see prompts/board-operator.md, "Local mode").
"""
import base64
import glob
import hashlib
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402
import limit  # noqa: E402

REPO = fleet.REPO
BOARD = os.path.join(REPO, "bin", "board")
PAGE = os.path.join(REPO, "artifact", "board.html")

DEFAULT_PORT = 39410
MAX_BODY = 16 * 1024
MAX_ANSWER = 500
MAX_MESSAGE = 4000
MAX_SSE = 32
KEEPALIVE_S = 25
POLL_S = 1.0
LOOPBACK_PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Any of these means the request did not come straight from a local browser or script.
FORWARD_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded",
                   "X-Real-Ip", "Cf-Connecting-Ip", "Cf-Ray", "Cf-Visitor", "Cdn-Loop",
                   "Cf-Access-Authenticated-User-Email", "Cf-Access-Jwt-Assertion")
SESSION_KEYS = ("code", "aliases", "state", "status", "tmux", "remote_url", "pending", "updated_at")
DECISION_KEYS = ("id", "kind", "from", "question", "options", "context", "tool_use_id", "tool_name",
                 "summary", "status", "answer", "answered_by", "answered_at", "relayed_at",
                 "created_at", "updated_at", "task_id", "stage", "gate", "artifact_ref",
                 "escalation_level", "notified_via")
OPERATOR_KEYS = ("heartbeat_at", "version", "operator_session", "account_hint")


def now_ms():
    return int(time.time() * 1000)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, data):
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def append_event(home, ev):
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def as_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------------- settings
def settings(cfg=None):
    cfg = cfg if cfg is not None else fleet.load_fleet()
    s = cfg.get("serve") if isinstance(cfg.get("serve"), dict) else {}
    cf = s.get("cf_access") if isinstance(s.get("cf_access"), dict) else {}
    try:
        port = int(s.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    hostnames = s.get("hostnames") or []
    if not isinstance(hostnames, list):
        hostnames = [hostnames]
    team = str(cf.get("team_domain") or "").strip().lower()
    team = re.sub(r"^https?://", "", team).rstrip("/")
    return {
        "enabled": s.get("enabled") is True,
        "port": port,
        "hostnames": [str(h).strip().lower() for h in hostnames if str(h or "").strip()],
        "trust_cf_headers": s.get("trust_cf_headers") is True,
        "team_domain": team,
        "aud": str(cf.get("aud") or "").strip(),
        "certs_url": str(cf.get("certs_url") or "").strip()
        or ("https://%s/cdn-cgi/access/certs" % team if team else ""),
    }


def have_crypto():
    try:
        import cryptography.hazmat.primitives.asymmetric.rsa  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------------- Cloudflare Access JWT
def b64url(s):
    s = s.encode() if isinstance(s, str) else s
    return base64.urlsafe_b64decode(s + b"=" * (-len(s) % 4))


class JwtError(Exception):
    pass


class Jwks:
    """Cloudflare Access signing keys, fetched from certs_url and cached."""

    TTL_S = 3600
    MIN_REFETCH_S = 60

    def __init__(self, url):
        self.url = url
        self.keys = {}
        self.fetched = 0.0
        self.lock = threading.Lock()

    def _fetch(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        if not self.url.startswith(("https://", "file://")):
            raise JwtError("certs_url must be https://")
        with urllib.request.urlopen(self.url, timeout=10) as resp:  # noqa: S310 (scheme checked)
            data = json.loads(resp.read(256 * 1024).decode("utf-8"))
        keys = {}
        for k in data.get("keys") or []:
            if k.get("kty") != "RSA" or not k.get("kid"):
                continue
            n = int.from_bytes(b64url(k["n"]), "big")
            e = int.from_bytes(b64url(k["e"]), "big")
            keys[k["kid"]] = rsa.RSAPublicNumbers(e, n).public_key()
        self.keys = keys
        self.fetched = time.time()

    def get(self, kid):
        with self.lock:
            age = time.time() - self.fetched
            if age > self.TTL_S or (kid not in self.keys and age > self.MIN_REFETCH_S):
                try:
                    self._fetch()
                except Exception as exc:  # noqa: BLE001
                    if not self.keys:
                        raise JwtError("cannot fetch signing keys: %s" % exc) from exc
            key = self.keys.get(kid)
        if key is None:
            raise JwtError("unknown signing key")
        return key


def verify_jwt(token, jwks, aud, team_domain, now=None):
    """Verify a Cloudflare Access JWT (RS256). Returns the claims, raises JwtError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        h64, p64, s64 = token.split(".")
        header = json.loads(b64url(h64))
        claims = json.loads(b64url(p64))
        sig = b64url(s64)
    except Exception as exc:  # noqa: BLE001
        raise JwtError("malformed token") from exc
    if header.get("alg") != "RS256":
        raise JwtError("unexpected alg")
    key = jwks.get(str(header.get("kid") or ""))
    try:
        key.verify(sig, (h64 + "." + p64).encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise JwtError("bad signature") from exc
    now = time.time() if now is None else now
    leeway = 60
    auds = claims.get("aud")
    auds = auds if isinstance(auds, list) else [auds]
    if aud not in auds:
        raise JwtError("wrong audience")
    if claims.get("iss") != "https://" + team_domain:
        raise JwtError("wrong issuer")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - leeway:
        raise JwtError("expired")
    if isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] > now + leeway:
        raise JwtError("not yet valid")
    if not claims.get("email"):
        raise JwtError("no email claim")
    return claims


# ------------------------------------------------------------------------- state
class StateBuilder:
    """Builds the page state from $BOARD_HOME. Approvals are read incrementally from events.jsonl."""

    def __init__(self, home):
        self.home = home
        self.ev_offset = 0
        self.ev_inode = None
        self.approvals = []
        self.lock = threading.Lock()

    def p(self, *parts):
        return os.path.join(self.home, *parts)

    def signature(self):
        h = hashlib.sha1()
        paths = [self.p(n) for n in ("snapshot.json", "usage.json", "limit.json", "operator.json",
                                    "ledger.json", "events.jsonl")]
        for sub in ("decisions", "tasks"):
            paths.append(self.p(sub))
            paths.extend(glob.glob(self.p(sub, "*.json")))
        for root, dirs, files in os.walk(self.p("messages")):
            dirs.sort()
            paths.append(root)
            paths.extend(os.path.join(root, f) for f in sorted(files) if f.endswith(".json"))
        for path in paths:
            try:
                st = os.stat(path)
                h.update(("%s|%d|%d\n" % (path, st.st_mtime_ns, st.st_size)).encode())
            except OSError:
                h.update(("%s|-\n" % path).encode())
        return h.hexdigest()

    def _read_approvals(self):
        path = self.p("events.jsonl")
        try:
            st = os.stat(path)
        except OSError:
            self.ev_offset, self.approvals = 0, []
            return
        if st.st_ino != self.ev_inode or st.st_size < self.ev_offset:
            self.ev_inode, self.ev_offset, self.approvals = st.st_ino, 0, []
        if st.st_size == self.ev_offset:
            return
        with open(path, "rb") as fh:
            fh.seek(self.ev_offset)
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1  # only whole lines; a partial last line is read next time
        self.ev_offset += end
        for line in chunk[:end].splitlines():
            if b'"approval"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "approval":
                self.approvals.append(ev)
        self.approvals = self.approvals[-500:]

    def build(self, cfg):
        with self.lock:
            self._read_approvals()
            approvals_raw = list(self.approvals)
        snap = read_json(self.p("snapshot.json")) or {}
        tasks = []
        for path in glob.glob(self.p("tasks", "*.json")):
            t = read_json(path)
            if isinstance(t, dict) and t.get("id"):
                tasks.append(t)
        tasks.sort(key=lambda t: as_int(t.get("updated_at")), reverse=True)
        titles = {t["id"]: t.get("title") for t in tasks}

        sessions = []
        for s in snap.get("sessions") or []:
            if not isinstance(s, dict) or not s.get("code"):
                continue
            row = {k: s.get(k) for k in SESSION_KEYS}
            row["task"] = titles.get(s.get("task_id")) or ""
            sessions.append(row)
        by_sid = {s.get("session_id"): s.get("code") for s in snap.get("sessions") or []
                  if isinstance(s, dict) and s.get("session_id")}

        decisions = []
        for path in glob.glob(self.p("decisions", "*.json")):
            d = read_json(path)
            if not isinstance(d, dict) or not d.get("id"):
                continue
            row = {k: d[k] for k in DECISION_KEYS if k in d}
            if not row.get("from") and d.get("session_id") in by_sid:
                row["from"] = by_sid[d["session_id"]]
            decisions.append(row)
        decisions.sort(key=lambda d: as_int(d.get("created_at")), reverse=True)
        decisions = decisions[:200]
        relayed = {d["id"]: d.get("relayed_at") for d in decisions}

        approvals = []
        for i, a in enumerate(approvals_raw):
            row = {k: a.get(k) for k in ("decision_id", "task_id", "stage", "gate", "answer", "result",
                                        "answered_by", "answered_at", "relayed_at")}
            row["id"] = "a-%s-%d" % (a.get("decision_id"), i)
            row["relayed_at"] = row["relayed_at"] or relayed.get(a.get("decision_id"))
            approvals.append(row)
        approvals.sort(key=lambda a: as_int(a.get("answered_at")), reverse=True)

        messages, replies = [], {}
        for path in glob.glob(self.p("messages", "*.json")):
            m = read_json(path)
            if isinstance(m, dict) and m.get("id"):
                messages.append(m)
        messages.sort(key=lambda m: as_int(m.get("created_at")), reverse=True)
        messages = messages[:20]
        for m in messages:
            if not ID_RE.match(str(m["id"])):
                continue
            rs = []
            for f in glob.glob(self.p("messages", m["id"], "replies", "*.json")):
                r = read_json(f)
                if isinstance(r, dict):
                    r.setdefault("id", os.path.basename(f)[:-5])
                    rs.append(r)
            rs.sort(key=lambda r: as_int(r.get("created_at")))
            if rs:
                replies[m["id"]] = rs[:50]

        op = read_json(self.p("operator.json")) or {}
        operator = {k: op.get(k) for k in OPERATOR_KEYS if k in op}
        approvers = fleet.id_list(cfg, "approvers")
        rms = fleet.id_list(cfg, "release_managers")
        if approvers:
            operator["approver_ids"] = approvers
        operator["release_manager_ids"] = rms
        usage = read_json(self.p("usage.json"))
        if isinstance(usage, dict):
            operator["usage"] = {k: usage.get(k) for k in ("five_hour", "seven_day", "updated_at") if k in usage}
        lim = limit.active()
        operator["limit"] = {k: lim.get(k) for k in ("hit_at", "kind", "resets_at", "note")} if lim else None
        ledger = read_json(self.p("ledger.json"))

        return {"sessions": sessions, "decisions": decisions, "tasks": tasks[:50],
                "approvals": approvals[:100], "messages": messages, "replies": replies,
                "operator": operator, "ledger": ledger if isinstance(ledger, dict) else None,
                "generated_at": snap.get("generated_at")}


class Hub:
    """Polls $BOARD_HOME once a second and wakes the SSE streams when the state changed."""

    def __init__(self, home):
        self.builder = StateBuilder(home)
        self.cond = threading.Condition()
        self.version = 0
        self.state = None
        self.state_json = None
        self.sig = None
        self.stopping = False
        self.sse_count = 0
        self.refresh(force=True)

    def refresh(self, force=False):
        sig = self.builder.signature()
        if not force and sig == self.sig:
            return False
        self.sig = sig
        state = self.builder.build(fleet.load_fleet())
        js = json.dumps(state, sort_keys=True, ensure_ascii=False)
        with self.cond:
            if js == self.state_json:
                return False
            self.state, self.state_json = state, js
            self.version += 1
            self.cond.notify_all()
        return True

    def run(self):
        while not self.stopping:
            time.sleep(POLL_S)
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - keep serving
                sys.stderr.write("board-serve: refresh failed: %s\n" % exc)

    def stop(self):
        with self.cond:
            self.stopping = True
            self.cond.notify_all()


# ------------------------------------------------------------------------- page
RESET_CSS = (":root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);"
             "padding-bottom:env(safe-area-inset-bottom,0px)}"
             "body{margin:0;font:14px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}"
             "img{max-width:100%}[hidden]{display:none!important}")


def render_page(nonce):
    with open(PAGE, encoding="utf-8") as fh:
        body = fh.read()
    # The only inline script gets the CSP nonce; nothing else in the page is executable.
    body = body.replace("<script>", '<script nonce="%s">' % nonce)
    head = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<meta name="board-mode" content="local">'
            '<style nonce="%s">%s</style>\n' % (nonce, RESET_CSS))
    return (head + body + "\n</html>\n").encode("utf-8")


def csp(nonce):
    return ("default-src 'none'; script-src 'nonce-%s'; style-src 'self' 'unsafe-inline' "
            "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % nonce)


# ------------------------------------------------------------------------- HTTP
class Refused(Exception):
    def __init__(self, status, error, detail=""):
        super().__init__(error)
        self.status, self.error, self.detail = status, error, detail


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "board-serve"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing
    def log_message(self, fmt, *args):  # noqa: D401 - quieter access log on stderr
        if not self.server.quiet:
            sys.stderr.write("board-serve: %s %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status, obj, extra=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.common_headers()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def common_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def refuse(self, exc):
        self.send_json(exc.status, {"error": exc.error, "detail": exc.detail})

    # -- who is asking
    def is_forwarded(self):
        return any(self.headers.get(h) is not None for h in FORWARD_HEADERS)

    def check_host(self):
        """Only our own host names: blocks DNS rebinding from a hostile page."""
        host = (self.headers.get("Host") or "").strip().lower()
        name, _, port = host.rpartition(":") if not host.endswith("]") else (host, "", "")
        if not name:
            name, port = host, ""
        cfg = self.server.cfg
        if name in LOOPBACK_HOSTS and port == str(self.server.server_address[1]):
            return host
        if name in cfg["hostnames"] and port in ("", "443"):
            return host
        raise Refused(421, "bad_host", "Host %r is not served here (serve.hostnames)" % host)

    def identity(self):
        """Returns (identity, via). Raises Refused."""
        cfg = self.server.cfg
        peer = self.client_address[0]
        if peer in LOOPBACK_PEERS and not self.is_forwarded():
            return "local", "loopback"
        jwt = self.headers.get("Cf-Access-Jwt-Assertion")
        hdr_email = (self.headers.get("Cf-Access-Authenticated-User-Email") or "").strip().lower()
        if cfg["team_domain"] and cfg["aud"] and self.server.jwks is not None:
            if not jwt:
                raise Refused(401, "unauthenticated", "Cloudflare Access token missing")
            try:
                claims = verify_jwt(jwt, self.server.jwks, cfg["aud"], cfg["team_domain"])
            except JwtError as exc:
                raise Refused(401, "unauthenticated", "Cloudflare Access token rejected: %s" % exc) from exc
            email = str(claims["email"]).strip().lower()
            if hdr_email and hdr_email != email:
                raise Refused(401, "unauthenticated", "email header does not match the token")
            return email, "cf-jwt"
        if cfg["trust_cf_headers"]:
            if not hdr_email:
                raise Refused(401, "unauthenticated", "Cf-Access-Authenticated-User-Email missing")
            return hdr_email, "cf-header"
        raise Refused(403, "remote_refused",
                      "non-local request: configure serve.cf_access (team_domain, aud; needs the "
                      "cryptography module) or set serve.trust_cf_headers: true")

    def check_csrf(self, host):
        if self.headers.get("X-Board") != "1":
            raise Refused(403, "csrf", "X-Board: 1 header required")
        origin = self.headers.get("Origin")
        if not origin or urllib.parse.urlsplit(origin).netloc.lower() != host:
            raise Refused(403, "csrf", "cross-origin request refused")

    def read_body(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise Refused(415, "json_only", "Content-Type must be application/json")
        try:
            n = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            n = -1
        if n < 0:
            raise Refused(411, "length_required", "Content-Length required")
        if n > MAX_BODY:
            raise Refused(413, "too_large", "body over %d bytes" % MAX_BODY)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Refused(400, "bad_json", "body is not JSON") from exc
        if not isinstance(body, dict):
            raise Refused(400, "bad_json", "body must be a JSON object")
        return body

    def audit(self, ident, via, action, **kw):
        ev = {"ts": now_ms(), "type": "serve", "action": action, "by": ident, "via": via}
        ev.update(kw)
        append_event(self.server.home, ev)

    # -- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.check_host()
            ident, _via = self.identity()
        except Refused as exc:
            return self.refuse(exc)
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self.page()
        if path == "/api/state":
            self.hub_kick()
            return self.send_json(200, self.state_for(ident))
        if path == "/api/events":
            return self.events(ident)
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.common_headers()
            self.end_headers()
            return None
        return self.send_json(404, {"error": "not_found"})

    def state_for(self, ident):
        with self.server.hub.cond:
            state = dict(self.server.hub.state)
        state.update({"mode": "local", "me": ident, "writable": True})
        return state

    def page(self):
        nonce = secrets.token_urlsafe(16)
        data = render_page(nonce)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", csp(nonce))
        self.common_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def events(self, ident):
        hub = self.server.hub
        with hub.cond:
            if hub.sse_count >= MAX_SSE:
                return self.send_json(503, {"error": "too_many_streams"})
            hub.sse_count += 1
        try:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("X-Accel-Buffering", "no")
            self.common_headers()
            self.end_headers()
            seen = -1
            while True:
                with hub.cond:
                    hub.cond.wait_for(lambda: hub.stopping or hub.version != seen, timeout=KEEPALIVE_S)
                    if hub.stopping:
                        return None
                    changed = hub.version != seen
                    seen = hub.version
                if changed:
                    data = json.dumps(self.state_for(ident), ensure_ascii=False)
                    self.wfile.write(("event: state\ndata: %s\n\n" % data).encode("utf-8"))
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return None
        finally:
            with hub.cond:
                hub.sse_count -= 1

    # -- POST
    def do_POST(self):
        try:
            host = self.check_host()
            ident, via = self.identity()
            self.check_csrf(host)
            path = urllib.parse.urlsplit(self.path).path
            m = re.fullmatch(r"/api/decisions/([^/]+)/answer", path)
            if m:
                body = self.read_body()
                return self.answer(m.group(1), body, ident, via)
            if path == "/api/messages":
                body = self.read_body()
                return self.message(body, ident, via)
            raise Refused(404, "not_found")
        except Refused as exc:
            return self.refuse(exc)
        except Exception as exc:  # noqa: BLE001 - answer with JSON, never drop the connection
            sys.stderr.write("board-serve: %s %s failed: %r\n" % (self.command, self.path, exc))
            return self.send_json(500, {"error": "internal"})

    def board(self, *args):
        env = dict(os.environ, BOARD_HOME=self.server.home)
        try:
            r = subprocess.run([BOARD] + list(args), env=env, capture_output=True, text=True,
                               timeout=60, stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            return 124, "", "timed out"
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def answer(self, did, body, ident, via):
        if not ID_RE.match(did):
            raise Refused(400, "bad_id", "invalid decision id")
        answer = body.get("answer")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER:
            raise Refused(400, "bad_answer", "answer must be a non-empty string (max %d chars)" % MAX_ANSWER)
        answer = answer.strip()
        home = self.server.home
        with self.server.write_lock:
            dec = read_json(os.path.join(home, "decisions", did + ".json"))
            if not isinstance(dec, dict):
                raise Refused(404, "not_found", "no such decision")
            kind = dec.get("kind")
            if dec.get("status") not in ("pending", "unauthorized", "answered"):
                self.audit(ident, via, "answer", id=did, result="gone", status=dec.get("status"))
                raise Refused(409, "gone", "decision is already %s" % dec.get("status"))
            if kind == "permission" and answer not in ("allow", "deny"):
                raise Refused(400, "bad_answer", "a permission answer is allow or deny")
            code = dec.get("from")
            if kind == "permission" and not code:
                snap = read_json(os.path.join(home, "snapshot.json")) or {}
                code = next((s.get("code") for s in snap.get("sessions") or []
                             if isinstance(s, dict) and s.get("session_id") == dec.get("session_id")), None)
            rc, out, err = self.board("answer", did, answer, "--by", ident)
            if rc == 4:
                self.audit(ident, via, "answer", id=did, result="unauthorized")
                raise Refused(403, "not_approver", err or "not an approver")
            if rc != 0:
                self.audit(ident, via, "answer", id=did, result="error", rc=rc)
                raise Refused(500, "answer_failed", err or out)
            if kind != "permission":
                self.hub_kick()
                self.audit(ident, via, "answer", id=did, result="answered")
                return self.send_json(200, {"id": did, "status": "answered", "note": out})
            if not code or not dec.get("tool_use_id"):
                self.board("mark", did, "stale")
                self.audit(ident, via, "press", id=did, result="stale", reason="no session or tool_use_id")
                raise Refused(409, "stale", "the session for this prompt is unknown")
            rc, out, err = self.board("press", str(code), answer, "--expect", str(dec["tool_use_id"]))
            self.hub_kick()
            if rc == 0:
                self.audit(ident, via, "press", id=did, code=code, key=answer, result="sent")
                return self.send_json(200, {"id": did, "status": "relayed", "note": out})
            if rc == 3:
                self.audit(ident, via, "press", id=did, code=code, key=answer, result="stale")
                raise Refused(409, "stale", err or "the prompt is no longer showing")
            self.audit(ident, via, "press", id=did, code=code, key=answer, result="error", rc=rc)
            raise Refused(502, "press_failed", err or out)

    def hub_kick(self):
        try:
            self.server.hub.refresh()
        except Exception:  # noqa: BLE001
            pass

    def message(self, body, ident, via):
        to, text = body.get("to"), body.get("body")
        if not isinstance(to, str) or not (to == "all" or CODE_RE.match(to)):
            raise Refused(400, "bad_to", "to must be a session code or \"all\"")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE:
            raise Refused(400, "bad_body", "body must be a non-empty string (max %d chars)" % MAX_MESSAGE)
        now = now_ms()
        mid = "m-%d-%s" % (now, secrets.token_hex(3))
        d = os.path.join(self.server.home, "messages")
        with self.server.write_lock:
            os.makedirs(d, exist_ok=True)
            write_json_atomic(os.path.join(d, mid + ".json"),
                              {"id": mid, "to": to, "body": text.strip(), "by": ident,
                               "created_at": now, "status": "new"})
            self.audit(ident, via, "message", id=mid, to=to)
        self.hub_kick()
        return self.send_json(201, {"id": mid, "status": "new"})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, home, cfg, quiet=False):
        super().__init__(("127.0.0.1", port), Handler)
        self.home = home
        self.cfg = cfg
        self.quiet = quiet
        self.write_lock = threading.Lock()
        self.hub = Hub(home)
        self.jwks = None
        if cfg["team_domain"] and cfg["aud"] and have_crypto():
            self.jwks = Jwks(cfg["certs_url"])


def actionable(home):
    """What a local-mode operator has to relay: answered question/gate decisions, new messages."""
    out = []
    for path in glob.glob(os.path.join(home, "decisions", "*.json")):
        d = read_json(path)
        if isinstance(d, dict) and d.get("status") == "answered" and d.get("kind") != "permission":
            out.append((as_int(d.get("answered_at")), "decision %s answered" % d.get("id"),
                        ("d", d.get("id"), d.get("answered_at"))))
    for path in glob.glob(os.path.join(home, "messages", "*.json")):
        m = read_json(path)
        if isinstance(m, dict) and m.get("status") == "new":
            out.append((as_int(m.get("created_at")), "message %s new" % m.get("id"), ("m", m.get("id"))))
    out.sort(key=lambda x: x[0])
    return [(line, key) for _, line, key in out]


def watch(home, once=False, tick_s=480):
    """Print one line per new actionable item (oldest first), and "tick" every tick_s seconds.

    Meant for the operator's Monitor: waiting costs no tokens, and a line is only printed when
    there is something to relay (or a heartbeat is due)."""
    seen = set()
    last_tick = time.time()
    while True:
        items = actionable(home)
        for line, key in items:
            if key not in seen:
                print(line, flush=True)
        seen = {key for _, key in items}
        if once:
            return 0
        if tick_s > 0 and time.time() - last_tick >= tick_s:
            print("tick", flush=True)
            last_tick = time.time()
        time.sleep(POLL_S)


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="board-serve", description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, help="port (default: fleet.yaml serve.port or %d)" % DEFAULT_PORT)
    ap.add_argument("--check", action="store_true", help="print the resolved settings and exit")
    ap.add_argument("--quiet", action="store_true", help="no access log")
    ap.add_argument("--watch", action="store_true",
                    help="do not serve: print 'decision ID answered' / 'message ID new' lines for the "
                         "local-mode operator's Monitor, plus 'tick' every --tick seconds")
    ap.add_argument("--once", action="store_true", help="with --watch: print what is open now and exit")
    ap.add_argument("--tick", type=int, default=480, help="with --watch: heartbeat tick seconds (0 = off)")
    args = ap.parse_args(argv[1:])
    if args.watch:
        try:
            return watch(fleet.board_home(), once=args.once, tick_s=args.tick)
        except KeyboardInterrupt:
            return 0
    cfg = settings()
    if args.port:
        cfg["port"] = args.port
    home = fleet.board_home()
    jwt_mode = "off"
    if cfg["team_domain"] and cfg["aud"]:
        jwt_mode = "verify (RS256, %s)" % cfg["certs_url"] if have_crypto() else "UNAVAILABLE (no cryptography module)"
    remote = ("JWT-verified" if jwt_mode.startswith("verify") else
              "trusted Cf-Access-Authenticated-User-Email header" if cfg["trust_cf_headers"] else "refused")
    info = ["listen   http://127.0.0.1:%d" % cfg["port"], "home     %s" % home,
            "config   %s" % fleet.config_dir(), "hosts    %s" % (", ".join(cfg["hostnames"]) or "(loopback only)"),
            "cf jwt   %s" % jwt_mode, "remote   %s" % remote]
    if args.check:
        print("\n".join(info))
        return 0
    os.umask(0o077)  # everything under BOARD_HOME is private to this user
    os.makedirs(home, exist_ok=True)
    srv = Server(cfg["port"], home, cfg, quiet=args.quiet)
    threading.Thread(target=srv.hub.run, daemon=True).start()
    sys.stderr.write("board-serve: " + "\nboard-serve: ".join(info) + "\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.hub.stop()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
