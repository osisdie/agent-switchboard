#!/usr/bin/env python3
"""board-serve: the self-hosted board (the "local adapter" of artifact/board.html).

Usage: serve.py [--port PORT] [--check] [--quiet]
       serve.py --watch [--once] [--tick SECONDS]

Serves the board page and a small JSON API from $BOARD_HOME, on 127.0.0.1 only, through two
listeners (docs/contract.md, "Local adapter API"):

  local   127.0.0.1:serve.port         identity "local": any direct request from this machine
                                       (other OS users included; list "local" only on a
                                       single-user host)
  tunnel  127.0.0.1:serve.tunnel.port  (serve.tunnel.enabled) the cloudflared ingress target;
                                       identity = the email in a verified Cloudflare Access JWT,
                                       never "local"

  GET  /                               the board page, in local mode
  GET  /api/state                      the board state (sessions, decisions, tasks, resources, ...)
  GET  /api/events                     Server-Sent Events: the full state on every change
  GET  /api/me                         who is asking: identity, how it was verified, roles
  GET  /icon.svg, /favicon.png, /favicon.ico, /apple-touch-icon.png, /icons/icon-{192,512}.png,
       /manifest.webmanifest           icons and the web app manifest (fixed list, same checks)
  POST /api/decisions/<id>/answer      {"answer": "..."}   (via `board answer` / `board press`)
  POST /api/messages                   {"to": "CODE"|"all", "body": "..."}  (screened by the secret
                                       guard: a secret request is stored "held", never relayed)
  POST /api/messages/<id>/release      {"confirm": "release"}  (approvers only: release a held
                                       message after a false positive; audited)
  POST /api/away                       {"state": "on"|"off", "said": "..."}   (approvers only;
                                       via `board away on|off --by board:<identity>`)
  POST /api/notify-prefs               {"op": "mute"|"unmute"|"level"|"presence", ...}  (approvers
                                       only; via `board mute|unmute|notify-level|notify-presence`)
  POST /api/presence                   {"visible": bool, "idle_ms": ms}  (approvers only;
                                       the board page's heartbeat, recorded in presence.json)

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
import tempfile
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import away  # noqa: E402
import fleet  # noqa: E402
import guard  # noqa: E402
import limit  # noqa: E402
import messages  # noqa: E402
import notify  # noqa: E402
import prefs  # noqa: E402
import resources  # noqa: E402

REPO = fleet.REPO
BOARD = os.path.join(REPO, "bin", "board")
PAGE = os.path.join(REPO, "artifact", "board.html")
# Static files: a fixed map from URL path to (file under artifact/, content type). Nothing else
# under artifact/ is served, and a request path is never joined onto a directory.
STATIC = {
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/apple-touch-icon-precomposed.png": ("apple-touch-icon.png", "image/png"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}
STATIC_CACHE = "private, max-age=86400"  # private: behind Access, never cached by a shared proxy

DEFAULT_PORT = 39410
MAX_BODY = 16 * 1024
MAX_ANSWER = 500
MAX_MESSAGE = 4000
MAX_IDLE_MS = 24 * 3600 * 1000   # presence: idle_ms above a day is refused (400)
MAX_SSE = 32            # per listener, so the tunnel can't starve the local browser
MAX_CONN = 64           # concurrent connections per listener (SSE streams included)
SOCKET_TIMEOUT_S = 60   # per-connection socket timeout (SSE writes every <= 25 s)
KEEPALIVE_S = 25
POLL_S = 1.0
LOOPBACK_PEERS = ("127.0.0.1", "::ffff:127.0.0.1")
DEC_ID_RE = re.compile(r"^d-[0-9]+-[a-z0-9]+$")
MSG_ID_RE = re.compile(r"^m-[0-9]+-[a-z0-9]+$")
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# On the local listener, any of these means the request did not come straight from a browser on
# this machine (cloudflared, a proxy, or something pretending to be one): refused.
FORWARD_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "Forwarded", "X-Forwarded-Host",
                   "X-Forwarded-Proto", "X-Real-Ip", "Cf-Visitor", "Cdn-Loop",
                   "Cf-Access-Authenticated-User-Email", "Cf-Access-Jwt-Assertion")
SESSION_KEYS = ("code", "aliases", "state", "status", "tmux", "remote_url", "pending", "updated_at",
                "title", "repo", "branch", "last_event", "notification_type", "recent")
DECISION_KEYS = ("id", "kind", "from", "question", "options", "context", "tool_use_id", "tool_name",
                 "summary", "status", "answer", "answered_by", "answered_at", "relayed_at",
                 "created_at", "updated_at", "task_id", "stage", "gate", "artifact_ref",
                 "escalation_level", "notified_via", "summary_truncated",
                 "labels_clipped",
                 # pane questions (an AskUserQuestion picker): source "pane"
                 "source", "questions", "questions_clipped", "pressable", "answer_index", "relay")
OPERATOR_KEYS = ("heartbeat_at", "version", "operator_session", "account_hint")


def log(msg):
    sys.stderr.write("board-serve: %s\n" % msg)


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
    t = s.get("tunnel") if isinstance(s.get("tunnel"), dict) else {}
    try:
        port = int(s.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    try:
        tport = int(t.get("port") or port + 1)
    except (TypeError, ValueError):
        tport = port + 1
    team = re.sub(r"^https?://", "", str(t.get("team_domain") or "").strip().lower()).rstrip("/")
    return {
        "enabled": s.get("enabled") is True,
        "port": port,
        "tunnel": t.get("enabled") is True,
        "tunnel_port": tport,
        "hostname": str(t.get("hostname") or "").strip().lower(),
        "team_domain": team,
        "aud": str(t.get("aud") or "").strip(),
        # Test hook / private mirror; normally derived from team_domain.
        "certs_url": str(t.get("certs_url") or "").strip()
        or ("https://%s/cdn-cgi/access/certs" % team if team else ""),
    }


def tunnel_problems(cfg):
    """Why the tunnel listener must not start (empty list = it may)."""
    out = []
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", cfg["hostname"]):
        out.append("serve.tunnel.hostname is missing or not a host name")
    if not cfg["team_domain"]:
        out.append("serve.tunnel.team_domain is missing (e.g. yourteam.cloudflareaccess.com)")
    if not cfg["aud"]:
        out.append("serve.tunnel.aud is missing (the Access application's AUD tag)")
    if not have_crypto():
        out.append("the python3 'cryptography' module is not installed (needed to verify Access JWTs)")
    if cfg["tunnel_port"] == cfg["port"]:
        out.append("serve.tunnel.port must differ from serve.port")
    return out


def is_production(dec):
    """Same rule as bin/board: gate or stage, trimmed and lowercased, is "production"."""
    return any(str(dec.get(k) or "").strip().lower() == "production" for k in ("gate", "stage"))


def match_id(ident, ids):
    """The entry of ids that is ident, compared case-insensitively (emails, "local"); else None.
    Returns the configured spelling, which is what `board answer` compares exactly."""
    want = str(ident or "").strip().lower()
    return next((i for i in ids if str(i).strip().lower() == want), None) if want else None


def allowed_ids(cfg, dec):
    """Who may answer this decision (the rule of bin/board allowed_ids). Empty = nobody."""
    if is_production(dec):
        return fleet.id_list(cfg, "release_managers") or fleet.id_list(cfg, "approvers")
    return fleet.id_list(cfg, "approvers")


def deciders(cfg):
    """approvers ∪ release_managers: who may act on the board at all."""
    return set(fleet.id_list(cfg, "approvers")) | set(fleet.id_list(cfg, "release_managers"))


def roles_of(cfg, ident):
    """The board roles of a verified identity (GET /api/me). is_release_manager is effective: it
    is true for an approver too when release_managers is empty (the fallback of allowed_ids)."""
    is_approver = match_id(ident, fleet.id_list(cfg, "approvers")) is not None
    is_rm = match_id(ident, allowed_ids(cfg, {"gate": "production"})) is not None
    roles = [r for r, on in (("approver", is_approver), ("release_manager", is_rm)) if on] or ["viewer"]
    return {"roles": roles, "is_approver": is_approver, "is_release_manager": is_rm}


def self_release_refused(rec, ident, approvers):
    """Four eyes on a held message: with more than one approver, its sender may not release it.
    Except `local`: every request on the loopback listener is `local`, i.e. the owner at this
    machine, who could edit $BOARD_HOME anyway; refusing it would only lock the owner out."""
    who = str(ident or "").strip().lower()
    return (len(approvers) > 1 and who != "local"
            and str(rec.get("by") or "").strip().lower() == who)


def tunnel_allowed(cfg):
    """Emails the tunnel listener serves: serve.tunnel.allowed_emails, else approvers ∪
    release_managers (emails only; "local" never applies on the tunnel)."""
    s = cfg.get("serve") if isinstance(cfg.get("serve"), dict) else {}
    t = s.get("tunnel") if isinstance(s.get("tunnel"), dict) else {}
    explicit = t.get("allowed_emails")
    if isinstance(explicit, str):
        explicit = [explicit]
    ids = explicit if isinstance(explicit, list) else sorted(deciders(cfg))
    return {str(x).strip().lower() for x in ids if "@" in str(x or "")}


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
            raise JwtError("certs_url must be https:// (file:// for tests)")
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
                    log("cannot fetch Access signing keys from %s: %r" % (self.url, exc))
                    if not self.keys:
                        raise JwtError("signing keys unavailable") from exc
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
        paths = [self.p(n) for n in ("snapshot.json", "usage.json", "account.json", "limit.json", "operator.json",
                                    "ledger.json", "events.jsonl", "owner-away", prefs.PREFS_FILE)]
        paths.append(self.p("resources.json"))  # rewritten on a bucketed change, else every 60 s
        for sub in ("decisions", "tasks", "consensus"):
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
            if not MSG_ID_RE.fullmatch(str(m["id"])):
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
        operator["approver_ids"] = approvers  # empty = nobody may answer (fails closed)
        operator["release_manager_ids"] = rms
        usage = read_json(self.p("usage.json"))
        if isinstance(usage, dict):
            operator["usage"] = {k: usage.get(k) for k in ("five_hour", "seven_day", "updated_at") if k in usage}
        lim = limit.active()
        operator["limit"] = {k: lim.get(k) for k in ("hit_at", "kind", "resets_at", "note")} if lim else None
        ledger = read_json(self.p("ledger.json"))

        # The secret guard runs over every string the board serves (docs/contract.md, "Secret
        # guardrails"): whatever reached $BOARD_HOME, no secret value leaves through the page.
        return guard.scrub_tree({"sessions": sessions, "decisions": decisions, "tasks": tasks[:50],
                "approvals": approvals[:100], "messages": messages, "replies": replies,
                "operator": operator, "ledger": ledger if isinstance(ledger, dict) else None,
                "owner_away": away.away_record(self.home),
                "consensus": away.consensus_items(self.home, now_ms())[-50:],
                "notify": notify_state(self.home),
                "resources": resources.board_view(read_json(self.p("resources.json"))),
                "generated_at": snap.get("generated_at")})


def notify_state(home):
    """The notification controls for the page (the shape of meta/notify): mutes, floors,
    presence policy and which channels are configured (booleans only, never values). Who is
    watching is left out: presence.json changes every heartbeat and is nobody else's business."""
    try:
        cfg = notify.load_config()
        out = prefs.status(cfg, now_ms(), home, with_present=False)
        env = notify.load_env()
        out["channels"] = {c: notify.enabled(c, env) for c in prefs.CHANNELS}
        return out
    except Exception as exc:  # noqa: BLE001 - the board keeps working without this panel
        log("notify state: %r" % exc)
        return None


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


_PAGE_VERSION = [(None, None)]  # [((mtime_ns, size), version)]: one tuple, replaced whole


def file_version(raw):
    """The version of board.html: a short hash of its bytes as they are on disk (CRLF included)."""
    return hashlib.sha256(raw).hexdigest()[:12]


def page_version():
    """The version of artifact/board.html as it is now (cached by mtime and size). An open page
    compares it with the version it was served with, to offer a reload after an update."""
    try:
        st = os.stat(PAGE)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    cached_key, cached = _PAGE_VERSION[0]
    if cached_key != key:
        with open(PAGE, "rb") as fh:
            cached = file_version(fh.read())
        _PAGE_VERSION[0] = (key, cached)
    return cached


def render_page(nonce):
    # The bytes are hashed as they are, then decoded: a text-mode read would turn CRLF into LF and
    # give a version that never equals page_version(), so every load would offer a reload.
    with open(PAGE, "rb") as fh:
        raw = fh.read()
    version = file_version(raw)
    body = raw.decode("utf-8")
    # Every inline <script> (the pre-paint theme script and the app) gets the CSP nonce;
    # nothing else in the page is executable.
    body = body.replace("<script>", '<script nonce="%s">' % nonce)
    head = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<meta name="board-mode" content="local">'
            '<meta name="board-version" content="%s">' % version +
            # Home-screen app (local mode only: the Artifact page cannot serve these files).
            '<link rel="icon" href="/favicon.png" sizes="32x32" type="image/png">'
            '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
            '<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">'
            '<meta name="theme-color" content="#edf0f4" media="(prefers-color-scheme: light)">'
            '<meta name="theme-color" content="#0f141a" media="(prefers-color-scheme: dark)">'
            '<meta name="mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
            '<meta name="apple-mobile-web-app-title" content="Agent Switchboard">'
            '<style nonce="%s">%s</style>\n' % (nonce, RESET_CSS))
    return (head + body + "\n</html>\n").encode("utf-8")


def csp(nonce):
    return ("default-src 'none'; script-src 'nonce-%s'; style-src 'self' 'unsafe-inline' "
            "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; "
            "img-src 'self' data:; manifest-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'" % nonce)


# ------------------------------------------------------------------------- HTTP
class Refused(Exception):
    def __init__(self, status, error, detail=""):
        super().__init__(error)
        self.status, self.error, self.detail = status, error, detail


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "board-serve"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S  # slow or stuck clients are dropped

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

    def common_headers(self, cache="no-store"):
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def refuse(self, exc):
        self.send_json(exc.status, {"error": exc.error, "detail": exc.detail})

    # -- who is asking
    def check_host(self):
        """Only our own Host names (blocks DNS rebinding). Returns the allowed origins."""
        host = (self.headers.get("Host") or "").strip().lower()
        srv = self.server
        if srv.kind == "local":
            port = srv.server_address[1]
            allowed = {"127.0.0.1:%d" % port: "http://127.0.0.1:%d" % port,
                       "localhost:%d" % port: "http://localhost:%d" % port}
            origins = set(allowed.values())
        else:
            name = srv.cfg["hostname"]
            allowed = {name: "https://" + name, name + ":443": "https://" + name}
            origins = {"https://" + name}
        if host not in allowed:
            raise Refused(421, "bad_host", "Host %r is not served on this listener" % host)
        return origins

    def identity(self):
        """Returns (identity, via). Raises Refused.

        local listener: "local", but only for a direct request (no proxy/Cloudflare headers).
        tunnel listener: the email of a verified Cloudflare Access JWT; never "local". The
        Cf-Access-Authenticated-User-Email header alone is ignored."""
        srv = self.server
        if srv.kind == "local":
            if self.client_address[0] not in LOOPBACK_PEERS:
                raise Refused(403, "forbidden", "not a loopback peer")
            fwd = [h for h in FORWARD_HEADERS if self.headers.get(h) is not None]
            if fwd:
                raise Refused(403, "forwarded_refused",
                              "the local listener only serves direct requests from this machine")
            return "local", "loopback"
        jwt = self.headers.get("Cf-Access-Jwt-Assertion")
        if not jwt:
            raise Refused(401, "unauthenticated", "Cloudflare Access token missing")
        try:
            claims = verify_jwt(jwt.strip(), srv.jwks, srv.cfg["aud"], srv.cfg["team_domain"])
        except JwtError as exc:
            log("tunnel: Access token rejected (%s)" % exc)
            raise Refused(401, "unauthenticated", "Cloudflare Access token rejected") from exc
        email = str(claims["email"]).strip().lower()
        # Access already filters people; this is the second gate, on our own list.
        if email not in tunnel_allowed(fleet.load_fleet()):
            log("tunnel: %s is not in serve.tunnel.allowed_emails / approvers" % email)
            raise Refused(403, "not_allowed", "not allowed on this board")
        return email, "cf-jwt"

    def check_csrf(self, origins):
        if self.headers.get("X-Board") != "1":
            raise Refused(403, "csrf", "X-Board: 1 header required")
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin not in origins:  # also rejects a missing Origin and "null"
            raise Refused(403, "csrf", "Origin must be this board's own origin")

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

    # -- methods
    def not_allowed(self):
        self.send_json(405, {"error": "method_not_allowed"}, {"Allow": "GET, HEAD, POST"})

    do_OPTIONS = do_PUT = do_DELETE = do_PATCH = do_TRACE = do_CONNECT = not_allowed

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.check_host()
            ident, via = self.identity()
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
        if path == "/api/me":
            return self.send_json(200, self.me_for(ident, via))
        if path in STATIC:
            return self.static(*STATIC[path])
        return self.send_json(404, {"error": "not_found"})

    def state_for(self, ident):
        with self.server.hub.cond:
            state = dict(self.server.hub.state)
        cfg = fleet.load_fleet()
        state.update({"mode": "local", "me": ident, "writable": True,
                      "can_message": match_id(ident, sorted(deciders(cfg))) is not None,
                      "account": self.account_for(ident, cfg), "page_version": page_version()})
        return state

    def account_for(self, ident, cfg):
        """The Claude account the sessions run on: {email, org} for approvers, {hidden: true} for
        everyone else, None when it is not known or switched off (collector, account.json)."""
        if not fleet.account_enabled(cfg):  # also when a stale account.json is still there
            return None
        rec = read_json(os.path.join(self.server.home, "account.json"))
        if not isinstance(rec, dict) or not rec.get("email"):
            return None
        if match_id(ident, fleet.id_list(cfg, "approvers")) is None:
            return {"hidden": True}
        return {"email": str(rec["email"]), "org": str(rec["org"]) if rec.get("org") else None}

    def me_for(self, ident, via):
        """GET /api/me: built only from the verified identity and fleet.yaml, never from request
        headers. api_keys is added by the key backend once it exists (docs/contract.md)."""
        me = {"identity": ident, "via": "access" if via == "cf-jwt" else "local"}
        me.update(roles_of(fleet.load_fleet(), ident))
        return me

    def static(self, name, ctype):
        try:
            with open(os.path.join(REPO, "artifact", name), "rb") as fh:
                data = fh.read()
        except OSError:
            return self.send_json(404, {"error": "not_found"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.common_headers(cache=STATIC_CACHE)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        return None

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
        hub, srv = self.server.hub, self.server
        with srv.sse_lock:
            if srv.sse_count >= MAX_SSE:
                return self.send_json(503, {"error": "too_many_streams"})
            srv.sse_count += 1
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
            with srv.sse_lock:
                srv.sse_count -= 1

    # -- POST
    def do_POST(self):
        try:
            origins = self.check_host()
            ident, via = self.identity()
            self.check_csrf(origins)
            path = urllib.parse.urlsplit(self.path).path
            m = re.fullmatch(r"/api/decisions/([^/]+)/answer", path)
            if m:
                body = self.read_body()
                return self.answer(m.group(1), body, ident, via)
            m = re.fullmatch(r"/api/messages/([^/]+)/release", path)
            if m:
                body = self.read_body()
                return self.release(m.group(1), body, ident, via)
            if path == "/api/messages":
                body = self.read_body()
                return self.message(body, ident, via)
            if path == "/api/away":
                body = self.read_body()
                return self.set_away(body, ident, via)
            if path == "/api/notify-prefs":
                body = self.read_body()
                return self.notify_prefs(body, ident, via)
            if path == "/api/presence":
                body = self.read_body()
                return self.presence(body, ident, via)
            raise Refused(404, "not_found")
        except Refused as exc:
            return self.refuse(exc)
        except Exception as exc:  # noqa: BLE001 - answer with JSON, never drop the connection
            log("%s %s failed: %r" % (self.command, self.path, exc))
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
        if not DEC_ID_RE.fullmatch(did):
            raise Refused(400, "bad_id", "invalid decision id")
        cfg = fleet.load_fleet()
        # Fails closed: with nobody listed in approvers, nobody answers from the board.
        if not fleet.id_list(cfg, "approvers"):
            self.audit(ident, via, "answer", id=did, result="refused", reason="no approvers configured")
            raise Refused(403, "no_approvers", "fleet.yaml approvers is empty: nobody may answer")
        answer = body.get("answer")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER:
            raise Refused(400, "bad_answer", "answer must be a non-empty string (max %d chars)" % MAX_ANSWER)
        answer = raw_answer = answer.strip()
        if body.get("answer") not in ("allow", "deny"):
            answer = guard.scrub(answer)  # free text is shown on the board and relayed
        # A pane question's option button also sends its number; board answer checks it against the label.
        option = body.get("option")
        if option is not None and (isinstance(option, bool) or not isinstance(option, int) or not 1 <= option <= 10):
            raise Refused(400, "bad_option", "option must be an option number")
        home = self.server.home
        with self.server.write_lock:
            dec = read_json(os.path.join(home, "decisions", did + ".json"))
            if not isinstance(dec, dict):
                raise Refused(404, "not_found", "no such decision")
            kind = dec.get("kind")
            # Check the identity first, with the rule of `board answer`, so that a refused answer
            # never touches the file (it must not cancel an approver's answer awaiting relay).
            allowed = allowed_ids(cfg, dec)
            if not allowed:
                self.audit(ident, via, "answer", id=did, result="refused", reason="nobody configured")
                raise Refused(403, "no_approvers", "nobody may answer this decision (fleet.yaml)")
            by = match_id(ident, allowed)
            if by is None:
                self.audit(ident, via, "answer", id=did, result="refused", reason="not an approver")
                raise Refused(403, "not_approver", "you may not answer this decision")
            if dec.get("status") not in ("pending", "unauthorized", "answered"):
                self.audit(ident, via, "answer", id=did, result="gone", status=dec.get("status"))
                raise Refused(409, "gone", "decision is already %s" % dec.get("status"))
            if kind == "permission" and answer not in ("allow", "deny"):
                raise Refused(400, "bad_answer", "a permission answer is allow or deny")
            # A free-text answer is relayed as [BOARD decision], the most trusted channel: it must not
            # ask the session for a secret value. (Picking one of the session's own options is fine.)
            reason = None if kind == "permission" else guard.screen_answer(raw_answer, dec.get("options"))
            if reason:
                self.audit(ident, via, "answer", id=did, result="held", reason=reason)
                messages.audit(home, {"action": "held-answer", "id": did, "by": by, "reason": reason})
                messages.alert("guard:held-answer:%s" % did, messages.HELD_ANSWER_TITLE, did, background=True)
                raise Refused(400, "held_answer", "%s: not recorded or relayed. Answer without asking for a "
                              "secret value, or answer in the session" % reason)
            if kind == "permission" and answer == "allow" and dec.get("summary_truncated"):
                self.audit(ident, via, "answer", id=did, result="refused", reason="summary truncated")
                raise Refused(409, "review_in_session",
                              "the request is only partly shown here: review and allow it in the session")
            pane_q = kind == "question" and dec.get("source") == "pane"
            # A pane question with several questions, multi-select or clipped text is read-only here.
            if pane_q and not dec.get("pressable"):
                self.audit(ident, via, "answer", id=did, result="refused", reason="not pressable")
                raise Refused(409, "answer_in_session",
                              "this question can only be answered in the session (multi-question form)")
            code = dec.get("from")
            if (kind == "permission" or pane_q) and not code:
                snap = read_json(os.path.join(home, "snapshot.json")) or {}
                code = next((s.get("code") for s in snap.get("sessions") or []
                             if isinstance(s, dict) and s.get("session_id") == dec.get("session_id")), None)
            if option is not None and not pane_q:
                raise Refused(400, "bad_option", "option is only for a question shown in a session")
            # The answer goes by file, never as an argument: "-", "--help" or "--answer-file" typed on
            # the board are recorded literally and are never read as options.
            fd, afile = tempfile.mkstemp(prefix=".answer.", dir=home)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(answer)
                rc, out, err = self.board("answer", did, "--answer-file", afile, "--by", by,
                                          *(("--option", str(option)) if option is not None else ()))
            finally:
                try:
                    os.unlink(afile)
                except OSError:
                    pass
            if rc == 2 and option is not None:
                self.audit(ident, via, "answer", id=did, result="refused", reason="option does not match")
                raise Refused(400, "bad_option", "that option does not match the question; reload and try again")
            if rc == 4:
                log("board answer %s refused: %s" % (did, err))
                self.audit(ident, via, "answer", id=did, result="unauthorized")
                raise Refused(403, "not_approver", "you may not answer this decision")
            # Never report success unless the file really holds this answer by this person.
            rec = read_json(os.path.join(home, "decisions", did + ".json")) or {}
            if rc != 0 or rec.get("status") != "answered" or rec.get("answer") != answer \
                    or rec.get("answered_by") != by:
                log("board answer %s failed (rc %d): %s %s" % (did, rc, err, out))
                self.audit(ident, via, "answer", id=did, result="error", rc=rc)
                raise Refused(500, "answer_failed", "the answer could not be recorded")
            # Answers the picker cannot take as keys (relay "operator") go out as a message.
            if kind != "permission" and not (pane_q and rec.get("relay") == "press"):
                self.hub_kick()
                self.audit(ident, via, "answer", id=did, result="answered")
                return self.send_json(200, {"id": did, "status": "answered"})
            if not code or not dec.get("tool_use_id"):
                self.board("mark", did, "stale")
                self.audit(ident, via, "press", id=did, result="stale", reason="no session or tool_use_id")
                raise Refused(409, "stale", "the session for this prompt is unknown")
            rc, out, err = self.board("press", str(code), "answer" if pane_q else answer,
                                      "--expect", str(dec["tool_use_id"]))
            self.hub_kick()
            if rc == 0:
                self.audit(ident, via, "press", id=did, code=code, key=answer, result="sent")
                return self.send_json(200, {"id": did, "status": "relayed"})
            log("board press %s %s (rc %d): %s %s" % (did, answer, rc, err, out))
            if rc == 3:
                self.audit(ident, via, "press", id=did, code=code, key=answer, result="stale")
                raise Refused(409, "stale", "the prompt is no longer showing; answer it in the session")
            if rc == 5:  # free text typed into the picker, Enter not pressed
                self.audit(ident, via, "press", id=did, code=code, key=answer, result="partial")
                raise Refused(409, "partial", "keys were sent but Enter was not pressed; finish in the session")
            if rc == 4:  # board press: no matching approver answer, a truncated summary, not pressable
                self.audit(ident, via, "press", id=did, code=code, key=answer, result="refused")
                raise Refused(403, "press_refused", "the key press was refused; answer it in the session")
            self.audit(ident, via, "press", id=did, code=code, key=answer, result="error", rc=rc)
            raise Refused(502, "press_failed", "the key press failed; answer it in the session")

    def hub_kick(self):
        try:
            self.server.hub.refresh()
        except Exception:  # noqa: BLE001
            pass

    def message(self, body, ident, via):
        to, text = body.get("to"), body.get("body")
        if not isinstance(to, str) or not (to == "all" or CODE_RE.fullmatch(to)):
            raise Refused(400, "bad_to", "to must be a session code or \"all\"")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE:
            raise Refused(400, "bad_body", "body must be a non-empty string (max %d chars)" % MAX_MESSAGE)
        if match_id(ident, sorted(deciders(fleet.load_fleet()))) is None:
            self.audit(ident, via, "message", result="refused", reason="not an approver")
            raise Refused(403, "not_approver", "only approvers and release managers send messages")
        # lib/messages.py: scrubbed before it is stored; a secret request is stored "held" (never
        # relayed), audited and alerted without echoing the text.
        with self.server.write_lock:
            rec = messages.create(self.server.home, to, text, ident, background_alert=True)
            self.audit(ident, via, "message", id=rec["id"], to=to, result=rec["status"])
        self.hub_kick()
        out = {"id": rec["id"], "status": rec["status"]}
        if rec["status"] == "held":
            out["reason"] = rec["held_reason"]
        return self.send_json(201, out)

    def release(self, mid, body, ident, via):
        """Release a held message (a false positive of the secret guard): approvers only, and the
        body must confirm it ({"confirm": "release"}, the page's second click). Audited; the
        relay then carries a reminder that this authorizes no secret values."""
        if not MSG_ID_RE.fullmatch(mid):
            raise Refused(400, "bad_id", "invalid message id")
        # Fails closed: only the two verified listeners (a direct local request, a verified Access
        # JWT) may release; any other channel (an API key, later) may not.
        if via not in ("loopback", "cf-jwt"):
            self.audit(ident, via, "message-release", id=mid, result="refused", reason="channel")
            raise Refused(403, "release_refused", "held messages are released only on the board itself")
        by = self.approver(ident, via, "message-release", what="release a held message", id=mid)
        if body.get("confirm") != "release":
            raise Refused(400, "confirm_required", "confirm the release: {\"confirm\": \"release\"}")
        with self.server.write_lock:
            rec = messages.read(self.server.home, mid) or {}
            approvers = fleet.id_list(fleet.load_fleet(), "approvers")
            if self_release_refused(rec, ident, approvers):
                self.audit(ident, via, "message-release", id=mid, result="refused", reason="self-release")
                raise Refused(403, "self_release", "another approver must release a message you sent")
            try:
                messages.release(self.server.home, mid, by)
            except messages.MessageError as exc:
                self.audit(ident, via, "message-release", id=mid, result=exc.code)
                raise Refused(404 if exc.code == "not_found" else 409, exc.code, exc.detail) from exc
            self.audit(ident, via, "message-release", id=mid, result="released")
        self.hub_kick()
        return self.send_json(200, {"id": mid, "status": "new"})


    def set_away(self, body, ident, via):
        """Owner-away mode from the board: approvers only (the rule of an ordinary answer)."""
        state, said = body.get("state"), body.get("said", "")
        if state not in ("on", "off"):
            raise Refused(400, "bad_state", "state must be \"on\" or \"off\"")
        if said is None:
            said = ""
        if not isinstance(said, str) or len(said) > MAX_ANSWER:
            raise Refused(400, "bad_said", "said must be a string (max %d chars)" % MAX_ANSWER)
        said = said.strip()
        cfg = fleet.load_fleet()
        approvers = fleet.id_list(cfg, "approvers")
        reason = guard.screen(said) if said else None
        if not approvers:
            self.audit(ident, via, "away", state=state, result="refused", reason="no approvers configured")
            raise Refused(403, "no_approvers", "fleet.yaml approvers is empty: nobody may change this")
        by = match_id(ident, approvers)
        if by is None:
            self.audit(ident, via, "away", state=state, result="refused", reason="not an approver")
            raise Refused(403, "not_approver", "only approvers change owner-away mode")
        home = self.server.home
        if reason:  # `said` is broadcast to every session
            self.audit(ident, via, "away", state=state, result="held", reason=reason)
            messages.audit(home, {"action": "held-said", "by": by, "reason": reason})
            messages.alert("guard:held-said:%d" % now_ms(), messages.HELD_SAID_TITLE, None, background=True)
            raise Refused(400, "held_said", "%s: owner-away mode was not changed" % reason)
        with self.server.write_lock:
            was = away.away_record(home) is not None
            args = ["away", state, "--by", "board:%s" % by]
            if state == "on":
                args += ["--said", said or "away (set on the board)"]
            elif said:
                args += ["--said", said]
            rc, out, err = self.board(*args)
            now_away = away.away_record(home) is not None
            if rc != 0 or now_away != (state == "on"):
                log("board away %s failed (rc %d): %s %s" % (state, rc, err, out))
                self.audit(ident, via, "away", state=state, result="error", rc=rc)
                raise Refused(500, "away_failed", "owner-away mode could not be changed")
            result = "unchanged" if was == now_away else state
            self.audit(ident, via, "away", state=state, result=result)
        self.hub_kick()
        return self.send_json(200, {"away": now_away, "status": result})


    def approver(self, ident, via, action, what="change notification settings", **kw):
        """The configured spelling of an approver identity; fails closed (403) otherwise."""
        approvers = fleet.id_list(fleet.load_fleet(), "approvers")
        if not approvers:
            self.audit(ident, via, action, result="refused", reason="no approvers configured", **kw)
            raise Refused(403, "no_approvers", "fleet.yaml approvers is empty: nobody may change this")
        by = match_id(ident, approvers)
        if by is None:
            self.audit(ident, via, action, result="refused", reason="not an approver", **kw)
            raise Refused(403, "not_approver", "only approvers %s" % what)
        return by

    def notify_prefs(self, body, ident, via):
        """Notification controls from the board: approvers only, through the board CLI (argv)."""
        op, target = body.get("op"), body.get("channel")
        if op not in ("mute", "unmute", "level", "presence"):
            raise Refused(400, "bad_op", "op must be mute, unmute, level or presence")
        if op != "presence" and target not in prefs.TARGETS:
            raise Refused(400, "bad_channel", "channel must be one of %s" % ", ".join(prefs.TARGETS))
        args = []
        if op == "mute":
            dur, until, urgent = body.get("for"), body.get("until"), body.get("include_urgent", False)
            if dur is not None and until is not None:
                raise Refused(400, "bad_duration", "give for or until, not both")
            try:
                if dur is not None:
                    prefs.parse_for(dur if isinstance(dur, str) else "")
                    args = ["--for", dur]
                elif until is not None:
                    if not isinstance(until, str) or len(until) > 64:
                        raise prefs.UsageError("until")
                    prefs.parse_until(until, now_ms())
                    args = ["--until", until]
            except prefs.UsageError as exc:
                raise Refused(400, "bad_duration", "for is 30m/1h/4h/2d (max 30d); until is HH:MM or "
                              "an ISO time in the next 30 days") from exc
            if not isinstance(urgent, bool):
                raise Refused(400, "bad_body", "include_urgent must be true or false")
            argv = ["mute", target] + args + (["--include-urgent"] if urgent else [])
        elif op == "unmute":
            argv = ["unmute", target]
        elif op == "level":
            level = body.get("level")
            if level not in prefs.LEVELS + ["default"]:
                raise Refused(400, "bad_level", "level must be info, warn, high, urgent or default")
            argv = ["notify-level", target, level]
        else:
            reduce, level = body.get("reduce"), body.get("level")
            if not isinstance(reduce, bool):
                raise Refused(400, "bad_body", "reduce must be true or false")
            if level is not None and level not in prefs.LEVELS[1:]:
                raise Refused(400, "bad_level", "level must be warn, high or urgent")
            argv = ["notify-presence", "on" if reduce else "off"] + (["--level", level] if level else [])
        kw = {"op": op, "channel": target} if op != "presence" else {"op": op}
        by = self.approver(ident, via, "notify-prefs", **kw)
        with self.server.write_lock:
            rc, out, err = self.board(*(argv + ["--by", "board:%s" % by]))
            if rc != 0:
                log("board %s failed (rc %d): %s %s" % (argv[0], rc, err, out))
                self.audit(ident, via, "notify-prefs", result="error", rc=rc, **kw)
                raise Refused(500, "prefs_failed", "the notification settings could not be changed")
            self.audit(ident, via, "notify-prefs", result="ok", **kw)
        self.hub_kick()
        return self.send_json(200, {"status": "ok", "notify": notify_state(self.server.home)})

    def presence(self, body, ident, via):
        """The board page's heartbeat: this approver has the board open (visible or not). The
        identity is the listener's verified one; only visible and how long ago the viewer last
        interacted come from the page. idle_ms (a duration) is turned into a time with the
        server's clock, so a page whose clock runs fast can't keep "watching" alive. A time sent
        by the page (interaction_at) is ignored: without idle_ms the interaction is unknown, so
        the viewer does not count as watching. Audited when visibility changes, not on every
        heartbeat."""
        visible, idle = body.get("visible"), body.get("idle_ms")
        if not isinstance(visible, bool):
            raise Refused(400, "bad_body", "visible must be true or false")
        if idle is not None and (not isinstance(idle, int) or isinstance(idle, bool) or idle > MAX_IDLE_MS):
            raise Refused(400, "bad_body", "idle_ms must be milliseconds (at most a day) or null")
        by = self.approver(ident, via, "presence")
        now = now_ms()
        seen = now - max(0, idle) if idle is not None else None
        changed = prefs.record_presence(self.server.home, by, visible, seen, via, now)
        if changed:
            self.audit(ident, via, "presence", visible=visible, result="ok")
        return self.send_json(200, {"status": "ok", "visible": visible})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, kind, port, home, cfg, hub, write_lock, quiet=False):
        super().__init__(("127.0.0.1", port), Handler)
        self.kind = kind  # "local" | "tunnel"
        self.home = home
        self.cfg = cfg
        self.quiet = quiet
        self.write_lock = write_lock
        self.hub = hub
        self.jwks = Jwks(cfg["certs_url"]) if kind == "tunnel" else None
        self.slots = threading.BoundedSemaphore(MAX_CONN)
        self.sse_lock = threading.Lock()
        self.sse_count = 0

    def process_request(self, request, client_address):
        """At most MAX_CONN connections per listener; the rest get a 503 and are closed."""
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def handle_error(self, request, client_address):
        """One line instead of a traceback; dropped and timed-out clients are routine."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        log("request from %s failed: %r" % (client_address[0], exc))

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def actionable(home):
    """What a local-mode operator has to relay: answered question/gate decisions, new messages,
    the owner-away state (`away on|off`, broadcast to every session when it changes) and each
    consensus status (`consensus <id> <status>`, mirrored, never relayed as a decision)."""
    out = []
    rec = away.away_record(home)
    if rec:
        out.append((as_int(rec.get("since")), "away on", ("a", "on", rec.get("since"))))
    else:
        out.append((0, "away off", ("a", "off")))
    for c in away.consensus_items(home, now_ms()):
        out.append((as_int(c.get("updated_at")), "consensus %s %s" % (c["id"], c["status"]),
                    ("c", c["id"], c["status"])))
    for path in glob.glob(os.path.join(home, "decisions", "*.json")):
        d = read_json(path)
        # board-serve presses permission prompts and pane questions itself (relay "press").
        if isinstance(d, dict) and d.get("status") == "answered" and d.get("kind") != "permission" \
                and not (d.get("source") == "pane" and d.get("relay") != "operator"):
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
    ap.add_argument("--port", type=int, help="local port (default: fleet.yaml serve.port or %d)" % DEFAULT_PORT)
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
    problems = tunnel_problems(cfg) if cfg["tunnel"] else []
    info = ["local    http://127.0.0.1:%d   (identity: local)" % cfg["port"], "home     %s" % home,
            "config   %s" % fleet.config_dir()]
    if not cfg["tunnel"]:
        info.append("tunnel   off (serve.tunnel.enabled)")
    elif problems:
        info.append("tunnel   REFUSED: " + "; ".join(problems))
    else:
        info.append("tunnel   http://127.0.0.1:%d   Host %s, identity: verified Access JWT "
                    "(aud %s..., keys %s)" % (cfg["tunnel_port"], cfg["hostname"], cfg["aud"][:8], cfg["certs_url"]))
    fcfg = fleet.load_fleet()
    approvers = fleet.id_list(fcfg, "approvers")
    if cfg["tunnel"]:
        info.append("emails   %s" % (", ".join(sorted(tunnel_allowed(fcfg))) or "(none: every tunnel request refused)"))
    info.append("answers  %s" % ("approvers: " + ", ".join(approvers) if approvers
                                 else "REFUSED: fleet.yaml approvers is empty"))
    if args.check:
        print("\n".join(info))
        return 2 if problems else 0
    if problems:
        sys.stderr.write("board-serve: the tunnel listener refuses to start:\n  - %s\n"
                         "Fix fleet.yaml serve.tunnel, or set serve.tunnel.enabled: false.\n"
                         % "\n  - ".join(problems))
        return 2
    os.umask(0o077)  # everything under BOARD_HOME is private to this user
    os.makedirs(home, exist_ok=True)
    hub, lock = Hub(home), threading.Lock()
    servers = [Server("local", cfg["port"], home, cfg, hub, lock, quiet=args.quiet)]
    if cfg["tunnel"]:
        servers.append(Server("tunnel", cfg["tunnel_port"], home, cfg, hub, lock, quiet=args.quiet))
    threading.Thread(target=hub.run, daemon=True).start()
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    sys.stderr.write("board-serve: " + "\nboard-serve: ".join(info) + "\n")
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        for srv in servers[1:]:
            srv.shutdown()
        for srv in servers:
            srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
