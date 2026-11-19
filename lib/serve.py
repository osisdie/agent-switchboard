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
  GET  /api/state                      the board state (sessions, decisions, tasks, resources,
                                       pomodoro, ...)
  GET  /api/events                     Server-Sent Events: the full state on every change
  GET  /api/me                         who is asking: identity, how it was verified, roles, and
                                       the API keys issued to them (never a key or a hash)
  GET  /dev, /dev/keys                 the developer portal (artifact/dev.html) and its key page;
       /api/dev, /api/apikeys          GET /api/apikeys and POST /api/apikeys[/<kid>] run `board
                                       apikey ... --by board:<identity>` (lib/serve_dev.py)
  GET  /icon.svg, /favicon.png, /favicon.ico, /apple-touch-icon.png, /icons/icon-{192,512}.png,
       /manifest.webmanifest           icons and the web app manifest (fixed list, same checks)
  POST /api/decisions/<id>/answer      {"answer": "..."}   (via `board answer` / `board press`)
  POST /api/decisions/<id>/resolve     {"confirm": "resolve", "note": "..."}  (approvers only: a
                                       board ask answered in the session; `board resolve --in-pane`)
  POST /api/messages                   {"to": "CODE"|"triage"|"all", "body": "..."}  (screened by the
                                       secret guard: a secret request is stored "held", never relayed;
                                       "triage": BOARD routes it; an asker may not send to "all")
  POST /api/messages/<id>/release      {"confirm": "release"}  (owners only: release a held
                                       message after a false positive; audited)
  POST /api/away                       {"state": "on"|"off", "said": "..."}   (owners only;
                                       via `board away on|off --by board:<identity>`)
  POST /api/dnd                        {"state": "on"|"off"}   (owners only: do not disturb; askers'
                                       messages are declined with a canned reply, lib/availability.py)
  POST /api/notify-prefs               {"op": "mute"|"unmute"|"level"|"presence", ...}  (approvers
                                       only; via `board mute|unmute|notify-level|notify-presence`)
  POST /api/presence                   {"visible": bool, "idle_ms": ms}  (owners only, else 204 ignored;
                                       the board page's heartbeat, recorded in presence.json)
  POST /api/alerts/ack                 {"scope": "all"}  (owners only; the Deck panel's
                                       "acknowledge all", via `board ack --by board:<identity> '*'`)
  POST /api/sessions/<code>/action     {"action": "compact"|"rc_on"|"rc_off"}  (owners only;
                                       stored via `board session CODE compact --queue-only` /
                                       `board rc on|off CODE --no-apply`, then `board session
                                       apply` types the fixed command in the background)
  POST /api/rc                         {"code": "CODE"|"all", "state": "on"|"off"}  (the same for
                                       Remote Control, also for all sessions)
  POST /api/resume                     {"code": "CODE"} | {"codes": ["CODE", ...]}  (owners only;
                                       `board resume CODE...` in the background, exactly the codes
                                       the owner confirmed: a session stuck on a usage limit gets
                                       the fewest keys that resume it, nothing when its screen is
                                       no known limit state; lib/resume.py)
  GET  /api/lessons[?q=TEXT]           the scheduler's lessons (also in /api/state), searchable
  POST /api/lessons                    {"title", "rule"?, "cause"?, "resolution"?}  (owners only;
                                       via `board lesson add --by board:<identity> --file <0600 tmp>`)
  POST /api/lessons/<id>               {"action": "edit"|"activate"|"retire"|"delete", ...}  (owners
                                       only; edit takes title/rule/cause/resolution; delete needs
                                       "confirm": "delete" and appends a tombstone; audited)
  GET  /api/messages?before=&limit=&to=&q=   older message threads, a page at a time (the page's
                                       History view): X-Board: 1 and no cross-site Sec-Fetch-Site
                                       (else 403 csrf), the same viewer filter as /api/state
  GET  /api/pomodoro/day?d=YYYY-MM-DD one kept day's per-stream pomodoro breakdown (read-only,
                                       on demand; the pushed view has daily totals only)
  GET  /api/v1/... (POST for writes)   the public API (lib/serve_api.py, routes in
                                       lib/api_spec.py, spec at /api/v1/openapi.json): a board API
                                       key (Authorization: Bearer asb_live_…) on either listener;
                                       on the tunnel also an allowed Access service token or email
  GET  /api/reminders                  the owner's reminders (owners, else approvers; X-Board: 1 and
                                       no cross-site Sec-Fetch-Site); 403 for everyone else
  POST /api/reminders                  {"title", "at", "date"?, "lead_min"?, "level"?, "repeat"?,
                                       "breakthrough"?}  (owner only; lib/reminders.py)
  POST /api/reminders/<id>             {"action": "ack"|"snooze"|"delete", "minutes"?}  (owner only)
  POST /mcp                            the MCP server over Streamable HTTP (JSON responses; tools and
                                       resources from lib/mcp_tools.py): the same key, listener and
                                       cross-site rules as /api/v1, each tool call checked as its route
  GET  /api/tickets/<ref>              one tracker ticket (lib/tickets): X-Board: 1 and no cross-site
                                       Sec-Fetch-Site (else 403 csrf, before anything else);
                                       approvers ∪ release_managers only, checked before any
                                       tracker call; cached, redacted
  POST /api/tickets/take               {"ref", "session", "comment"?, "transition"?, "assign"?}
                                       (approvers only): task + link + a ticket relay, then the
                                       tracker's take writes; 409 already_taken
  POST /api/tickets/<ref>/comment|transition|assign|release   {"text"}|{"to"}|{"who"}|{}
                                       (approvers only; comments carry "(via agent-switchboard)")

--check prints the resolved settings and exits. --watch serves nothing: it prints one line per
item the local-mode operator must relay (see prompts/board-operator.md, "Local mode").
"""
import base64
import glob
import hashlib
import http.server
import json
import math
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
import availability  # noqa: E402
import away  # noqa: E402
import desk  # noqa: E402
import fleet  # noqa: E402
import guard  # noqa: E402
import lessons  # noqa: E402
import pomodoro  # noqa: E402
import limit  # noqa: E402
import messages  # noqa: E402
import notify  # noqa: E402
import actions  # noqa: E402
import prefs  # noqa: E402
import rc  # noqa: E402
import reminders  # noqa: E402
import resources  # noqa: E402
import pii  # noqa: E402
import serve_api  # noqa: E402
import serve_dev  # noqa: E402

_SELF = sys.modules[__name__]   # handed to serve_api for its helpers (no second import of this file)
import tickets  # noqa: E402

REPO = fleet.REPO
BOARD = os.path.join(REPO, "bin", "board")
PAGE = os.path.join(REPO, "artifact", "board.html")
# board.html opens with its own charset line (for a page opened from disk); the served head
# declares the charset itself, so that line is dropped instead of repeated.
OWN_CHARSET_RE = re.compile(r"\A<meta charset=\"utf-8\">[^\n]*\n")
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
# GET /api/tickets/<ref>: also needs X-Board: 1 and no cross-site Sec-Fetch-Site (Handler.check_fetch).
TICKET_GET_RE = re.compile(r"/api/tickets/([^/]+)")

DEFAULT_PORT = 39410
MAX_BODY = 16 * 1024
# A refused POST whose body was not read: read (and drop) up to this much, for at most this long,
# before the reply, so closing the socket does not reset it and the client gets the refusal.
DRAIN_MAX = 64 * 1024
DRAIN_TIMEOUT_S = 2.0       # in all, not per read: a client trickling bytes cannot hold a handler
DRAIN_BAD_FRAMING_S = 0.5   # a request whose length is unknown (Transfer-Encoding, a bad Content-Length)
MAX_ANSWER = 500
MAX_FREE_TEXT = 200     # typed into "Type something" (bin/board FREE_TEXT_MAX)
# What a typed answer loses before it is checked and typed: control and escape characters, line
# breaks (Enter would submit early or move the focus), zero-width and bidi controls.
FREE_TEXT_STRIP_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]+")


def clean_free_text(text):
    return re.sub(r"\s+", " ", FREE_TEXT_STRIP_RE.sub(" ", text)).strip(" ")


def is_free_text_label(text):
    """Does TEXT read as the picker's own "Type something." row (press.free_text_label)? Such a
    text could not be told apart from that row on screen, so it is refused."""
    label = str(fleet.get_key(fleet.load_fleet(), "press.free_text_label") or "")
    try:
        return bool(label) and re.fullmatch("(?:%s)[.…]*" % label, text, re.IGNORECASE) is not None
    except re.error:
        return True
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
MAX_RESUME = 64          # POST /api/resume: at most this many codes at once
SESSION_KEYS = ("code", "aliases", "state", "status", "tmux", "remote_url", "pending", "updated_at",
                "title", "repo", "branch", "last_event", "notification_type", "recent", "rc", "compact", "limit")
# Card extras from cards.json (lib/cards.py). CARD_PRIVATE ones reach approvers only (state_for).
CARD_KEYS = ("teammates", "meter", "done", "doing", "prompt", "branch", "state_since")
CARD_PRIVATE = ("doing", "prompt")
DECISION_KEYS = ("id", "kind", "from", "question", "options", "context", "tool_use_id", "tool_name",
                 "summary", "status", "answer", "answered_by", "answered_at", "relayed_at",
                 "hint", "hint_at", "resolved_by", "resolved_via", "resolved_at", "resolve_note",
                 "created_at", "updated_at", "task_id", "stage", "gate", "artifact_ref",
                 "escalation_level", "notified_via", "summary_truncated",
                 "labels_clipped", "answer_options", "press_note", "press_note_code", "press_not_visible",
                 "status_reason",
                 # settled from the session's own report (lib/reconcile.py)
                 "delivered_via", "reconciled_from", "reconcile_result",
                 # pane questions (an AskUserQuestion picker): source "pane"
                 "source", "questions", "questions_clipped", "pressable", "answer_index", "relay")
OPERATOR_KEYS = ("heartbeat_at", "version", "operator_session", "account_hint")
BURN_STATUS = ("danger", "ok", "unknown", "stale")


def burn_view(samples_file):
    """operator.usage.burn from usage-samples.json: a known status and numbers only, else None."""
    b = samples_file.get("burn") if isinstance(samples_file, dict) else None
    if not isinstance(b, dict) or b.get("status") not in BURN_STATUS:
        return None
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None  # noqa: E731
    return {"status": b["status"], "pct_per_hour": num(b.get("pct_per_hour")), "hit_at": num(b.get("hit_at")),
            "n": num(b.get("n"))}


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


def owner_ids(cfg):
    """Who owns the board: fleet.yaml `owners`, else `approvers` (a single-user setup keeps
    working). For the board's own owner-only surfaces; API key administration never falls back
    (lib/apikeys.py fails closed with empty owners)."""
    return fleet.owner_ids(cfg)


def is_owner(cfg, ident):
    return match_id(ident, owner_ids(cfg)) is not None


def askers(cfg):
    """`askers`: board users who may only send messages (and read the replies). Never deciders:
    they answer nothing, release nothing, read no tickets."""
    return fleet.id_list(cfg, "askers")


def messagers(cfg):
    """Who may POST /api/messages: deciders ∪ askers."""
    return deciders(cfg) | set(askers(cfg))


def asker_only(cfg, ident):
    """An asker who is not also an approver or release manager: their messages are labelled and
    rate limited, and the page gives them a composer but no answer buttons."""
    return match_id(ident, askers(cfg)) is not None and match_id(ident, sorted(deciders(cfg))) is None


def roles_of(cfg, ident):
    """The board roles of a verified identity (GET /api/me). is_release_manager is effective: it
    is true for an approver too when release_managers is empty (the fallback of allowed_ids)."""
    is_approver = match_id(ident, fleet.id_list(cfg, "approvers")) is not None
    is_rm = match_id(ident, allowed_ids(cfg, {"gate": "production"})) is not None
    is_asker = match_id(ident, askers(cfg)) is not None
    owner = is_owner(cfg, ident)
    roles = [r for r, on in (("owner", owner), ("approver", is_approver), ("release_manager", is_rm),
                             ("asker", is_asker)) if on] or ["viewer"]
    return {"roles": roles, "is_owner": owner, "is_approver": is_approver, "is_release_manager": is_rm,
            "is_asker": is_asker}


def self_release_refused(rec, ident, approvers):
    """Four eyes on a held message: with more than one possible releaser (the owners, see
    owner_ids; the caller passes that list), its sender may not release it.
    Except `local`: every request on the loopback listener is `local`, i.e. the owner at this
    machine, who could edit $BOARD_HOME anyway; refusing it would only lock the owner out."""
    who = str(ident or "").strip().lower()
    return (len(approvers) > 1 and who != "local"
            and str(rec.get("by") or "").strip().lower() == who)


def own_keys(home, ident, cfg):
    """The API keys issued to one identity, for GET /api/me (the profile menu): id, name, scopes,
    times and status only. Never a key, never a hash, never anyone else's key."""
    import apikeys
    who, now = str(ident or "").strip().lower(), now_ms()
    ttl = apikeys.request_ttl_ms(cfg)
    rows = []
    for rec in apikeys.load_all(home):
        if str(rec.get("owner") or "").strip().lower() != who or rec.get("kind", "live") != "live":
            continue
        v = apikeys.view(rec, now, ttl)
        rows.append({k: v[k] for k in ("id", "name", "scopes", "created_at", "expires_at", "last_used_at", "status")})
    rows.sort(key=lambda r: (-(r.get("created_at") or 0), r["id"]))
    return rows[:50]


def tunnel_allowed(cfg):
    """Emails the tunnel listener serves: serve.tunnel.allowed_emails, else approvers ∪
    release_managers ∪ askers ∪ owners (emails only; "local" never applies on the tunnel). An explicit
    allowed_emails stays authoritative: an asker missing from it cannot reach the board."""
    s = cfg.get("serve") if isinstance(cfg.get("serve"), dict) else {}
    t = s.get("tunnel") if isinstance(s.get("tunnel"), dict) else {}
    explicit = t.get("allowed_emails")
    if isinstance(explicit, str):
        explicit = [explicit]
    ids = explicit if isinstance(explicit, list) else sorted(messagers(cfg) | set(fleet.id_list(cfg, "owners")))
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


def verify_jwt(token, jwks, aud, team_domain, now=None, require_email=True):
    """Verify a Cloudflare Access JWT (RS256). Returns the claims, raises JwtError. The board
    routes need a person (an email claim); /api/v1 also takes a service token (require_email=False:
    an email or a common_name claim)."""
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
    if not claims.get("email") and (require_email or not claims.get("common_name")):
        raise JwtError("no email claim" if require_email else "no email or common_name claim")
    return claims


# ------------------------------------------------------------------------- state
class StateBuilder:
    """Builds the page state from $BOARD_HOME. Approvals are read incrementally from events.jsonl."""

    def __init__(self, home):
        self.home = home
        self.ev_offset = 0
        self.ev_inode = None
        self.approvals = []
        self.mark_reasons = {}  # decision id -> status_reason code, from mark / unauthorized events
        self.lock = threading.Lock()

    def p(self, *parts):
        return os.path.join(self.home, *parts)

    def signature(self):
        h = hashlib.sha1()
        paths = [self.p(n) for n in ("snapshot.json", "usage.json", "usage-samples.json", "account.json",
                                    "limit.json", "operator.json",
                                    "ledger.json", "events.jsonl", "owner-away", availability.DND_FILE, prefs.PREFS_FILE,
                                    rc.PREFS_FILE, actions.QUEUE_FILE, actions.LAST_FILE)]
        paths.append(self.p("resources.json"))  # rewritten on a bucketed change, else every 60 s
        paths.append(self.p("cards.json"))      # card extras (lib/cards.py), rewritten when changed
        paths.append(self.p("pomodoro.json"))   # pomodoro tallies (lib/pomodoro.py)
        paths.append(self.p(lessons.FILE))      # lessons (lib/lessons.py), append-only
        paths.append(self.p("scheduler.json"))  # rewritten only when the decision changes
        paths.append(self.p("desk", "health.json"))   # the local asker desk's endpoint (lib/desk.py)
        paths.append(os.path.join(fleet.config_dir(), "fleet.yaml"))   # sessions' `about`, approver lists
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
            self.ev_inode, self.ev_offset, self.approvals, self.mark_reasons = st.st_ino, 0, [], {}
        if st.st_size == self.ev_offset:
            return
        with open(path, "rb") as fh:
            fh.seek(self.ev_offset)
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1  # only whole lines; a partial last line is read next time
        self.ev_offset += end
        for line in chunk[:end].splitlines():
            if b'"approval"' not in line and b'"mark"' not in line and b'"unauthorized"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "approval":
                self.approvals.append(ev)
            elif ev.get("type") in ("mark", "unauthorized") and isinstance(ev.get("id"), str):
                code = mark_reason_code(ev)
                if code:
                    self.mark_reasons[ev["id"]] = code
                elif ev.get("type") == "mark":
                    self.mark_reasons.pop(ev["id"], None)
        self.approvals = self.approvals[-500:]
        if len(self.mark_reasons) > 2000:
            self.mark_reasons = dict(list(self.mark_reasons.items())[-1000:])

    def build(self, cfg):
        with self.lock:
            self._read_approvals()
            approvals_raw = list(self.approvals)
            mark_reasons = dict(self.mark_reasons)
        snap = read_json(self.p("snapshot.json")) or {}
        tasks = []
        for path in glob.glob(self.p("tasks", "*.json")):
            t = read_json(path)
            if isinstance(t, dict) and t.get("id"):
                tasks.append(t)
        tasks.sort(key=lambda t: as_int(t.get("updated_at")), reverse=True)
        titles = {t["id"]: t.get("title") for t in tasks}

        cards = read_json(self.p("cards.json"))
        cards = cards.get("sessions") if isinstance(cards, dict) and isinstance(cards.get("sessions"), dict) else {}
        sessions = []
        # what each session is responsible for (fleet.yaml `about`, already one clean capped line);
        # a second instance ("API/name") is the same session's
        abouts = {f["code"]: f.get("about") or "" for f in cfg.get("sessions") or []}
        for s in snap.get("sessions") or []:
            if not isinstance(s, dict) or not s.get("code"):
                continue
            row = {k: s.get(k) for k in SESSION_KEYS}
            row["task"] = titles.get(s.get("task_id")) or ""
            row["about"] = abouts.get(s["code"]) or abouts.get(str(s["code"]).split("/")[0]) or ""
            card = cards.get(s["code"])
            row["card"] = {k: card.get(k) for k in CARD_KEYS if k in card} if isinstance(card, dict) else None
            sessions.append(row)
        by_sid = {s.get("session_id"): s.get("code") for s in snap.get("sessions") or []
                  if isinstance(s, dict) and s.get("session_id")}

        decisions = []
        for path in glob.glob(self.p("decisions", "*.json")):
            d = read_json(path)
            if not isinstance(d, dict) or not d.get("id"):
                continue
            row = {k: d[k] for k in DECISION_KEYS if k in d}
            # Why an answer was not delivered: the record's own code, else (a record from before the
            # code existed) the last mark/press event's reason, as a fixed code, never its text.
            if row.get("status") in UNDELIVERED and not row.get("status_reason") and d["id"] in mark_reasons:
                row["status_reason"] = mark_reasons[d["id"]]
            if row.get("status_reason") not in REASON_CODES:
                row.pop("status_reason", None)
            # fixed codes only, never free text a record may carry
            if row.get("delivered_via") != "session":
                row.pop("delivered_via", None)
            if row.get("reconciled_from") not in REASON_CODES + ("stale",):
                row.pop("reconciled_from", None)
            if row.get("reconcile_result") not in RECONCILE_RESULTS:
                row.pop("reconcile_result", None)
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

        # The recent window (docs/contract.md "Messages: recent and history"): the 20 threads with
        # the latest activity, every held one besides, and how many older threads there are.
        threads = message_threads(self.home)
        window = message_window(threads)
        messages = [t["m"] for t in window]
        replies = {}
        for m in messages:
            rs = thread_replies(self.home, m["id"])
            if rs:
                replies[m["id"]] = rs

        op = read_json(self.p("operator.json")) or {}
        operator = {k: op.get(k) for k in OPERATOR_KEYS if k in op}
        approvers = fleet.id_list(cfg, "approvers")
        rms = fleet.id_list(cfg, "release_managers")
        operator["approver_ids"] = approvers  # empty = nobody may answer (fails closed)
        operator["release_manager_ids"] = rms
        usage = read_json(self.p("usage.json"))
        if isinstance(usage, dict):
            operator["usage"] = {k: usage.get(k) for k in ("five_hour", "seven_day", "updated_at") if k in usage}
            burn = burn_view(read_json(self.p("usage-samples.json")))
            if burn:
                operator["usage"]["burn"] = burn
        lim = limit.active()
        operator["limit"] = {k: lim.get(k) for k in ("hit_at", "kind", "resets_at", "note")} if lim else None
        ledger = read_json(self.p("ledger.json"))

        # The secret guard runs over every string the board serves (docs/contract.md, "Secret
        # guardrails"): whatever reached $BOARD_HOME, no secret value leaves through the page.
        return guard.scrub_tree({"sessions": sessions, "decisions": decisions, "tasks": tasks[:50],
                "approvals": approvals[:100], "messages": messages, "replies": replies,
                "messages_older": len(threads) - len(window),
                "operator": operator, "ledger": ledger if isinstance(ledger, dict) else None,
                "owner_away": away.away_record(self.home),
                # online / dnd / away; state_for computes it again per viewer (the clock, the dnd record)
                "availability": availability.view(self.home, cfg),
                # the local asker desk (lib/desk.py): up|down|disabled; state_for adds the owner's details
                "local_model": desk.view(self.home, cfg),
                "consensus": away.consensus_items(self.home, now_ms())[-50:],
                "notify": notify_state(self.home),
                "rc": rc_state(self.home),
                "resources": resources.board_view(read_json(self.p("resources.json"))),
                "pomodoro": pomodoro.board_view(read_json(self.p("pomodoro.json")), now_ms()),
                "lessons": lessons.board_view(self.home, 100),
                "scheduler": scheduler_view(read_json(self.p("scheduler.json"))),
                "generated_at": snap.get("generated_at")})


def scheduler_view(doc):
    """What the board shows of scheduler.json for now: the mode and the lessons in force."""
    if not isinstance(doc, dict):
        return None
    ls = doc.get("lessons") if isinstance(doc.get("lessons"), dict) else {}
    return {"mode": doc.get("mode") if isinstance(doc.get("mode"), str) else None,
            "applied": [x for x in ls.get("applied") or [] if isinstance(x, str) and lessons.ID_RE.fullmatch(x)],
            "hints": [{"lesson": str(x.get("lesson")), "text": str(x.get("text"))[:160]}
                      for x in ls.get("hints") or [] if isinstance(x, dict)][:20]}


STATE_THREADS = 20      # the threads /api/state and SSE carry (plus every held one)
REPLIES_PER_THREAD = 50  # the latest replies of a thread that are served
HISTORY_LIMIT = 20       # GET /api/messages: the default page size (at most HISTORY_MAX)
HISTORY_MAX = 50
# The message and reply fields the board serves (an allowlist: anything else a writer or the
# operator put in a file stays on disk). The page's own fields, plus the kind / asker / ticket
# relay ones the contract documents (docs/contract.md "Messages"; ticket text is viewer-scoped).
MESSAGE_KEYS = ("id", "to", "body", "by", "created_at", "status", "relayed_to", "relayed_at", "held_reason",
                "held_at", "released_by", "released_at", "kind", "asker", "event", "ticket", "task_id", "api", "via",
                "routed_to", "routed_at", "declined_reason", "declined_at")
REPLY_KEYS = ("id", "from", "body", "created_at", "redacted", "canned", "local_model")
CURSOR_RE = re.compile(r"([0-9]{1,16})\.(m-[0-9]+-[a-z0-9]+)")


def _reply_times(home, mid):
    """When each reply was stored: from the reply ids (r-<ms>-<hex>, lib/messages.py), so ordering
    threads needs no reply file to be read."""
    out = []
    for f in glob.glob(os.path.join(home, "messages", mid, "replies", "r-*.json")):
        try:
            out.append(int(os.path.basename(f)[:-5].split("-")[1]))
        except (IndexError, ValueError):
            continue
    return out


def message_threads(home):
    """Every message thread, latest activity first: [{"m": record + last_activity_at, "at"}].
    Activity is the latest of the message's creation, its release and its replies, so a new reply
    brings an old thread back up. Ties: the higher id first (stable for the history cursor)."""
    out = []
    for path in glob.glob(os.path.join(home, "messages", "*.json")):
        m = read_json(path)
        if not isinstance(m, dict) or not MSG_ID_RE.fullmatch(str(m.get("id") or "")):
            continue
        at = max([as_int(m.get("created_at")), as_int(m.get("released_at"))] + _reply_times(home, m["id"]))
        row = {k: m[k] for k in MESSAGE_KEYS if k in m}
        if isinstance(m.get("desk"), dict):     # the local asker desk's progress (lib/desk.py)
            row["desk"] = desk.public(m["desk"])
        row["last_activity_at"] = at
        out.append({"m": row, "at": at})
    out.sort(key=lambda t: (t["at"], t["m"]["id"]), reverse=True)
    return out


def message_window(threads):
    """The threads the state carries: the STATE_THREADS latest, and every held one besides (a held
    message waits for an approver, however old). Order kept."""
    return [t for i, t in enumerate(threads) if i < STATE_THREADS or t["m"].get("status") == "held"]


def thread_replies(home, mid):
    """A thread's latest REPLIES_PER_THREAD replies, oldest first."""
    rs = []
    for f in glob.glob(os.path.join(home, "messages", mid, "replies", "*.json")):
        r = read_json(f)
        if isinstance(r, dict):
            r.setdefault("id", os.path.basename(f)[:-5])
            rs.append({k: r[k] for k in REPLY_KEYS if k in r})
    rs.sort(key=lambda r: as_int(r.get("created_at")))
    return rs[-REPLIES_PER_THREAD:]


def thread_sessions(home, m):
    """The sessions a thread involves (the page's session filter means the same): its `to`, the
    sessions it was relayed or routed to, and every session that replied."""
    out = {str(m.get("to") or "")}
    for k in ("relayed_to", "routed_to"):
        if isinstance(m.get(k), list):
            out |= {str(x) for x in m[k]}
    out |= {str(r.get("from") or "") for r in thread_replies(home, m["id"])}
    out.discard("")
    return out


def message_cursor(t):
    return "%d.%s" % (t["at"], t["m"]["id"])


def message_page(home, before=None, limit=HISTORY_LIMIT, to=None, q=None, view=None):
    """One page of the message history (GET /api/messages, /api/v1/messages): threads strictly
    older than the cursor `before` ("<activity ms>.<message id>", from a thread's
    last_activity_at and id), optionally only those involving session `to` (sent, relayed or
    routed to it, or replied by it; or to "all" / "triage"), and only those
    whose text matches `q` (case-insensitive, also in the replies). `view(state) -> state` is the
    viewer's filter; the search runs on what it returns (scrubbed, viewer-scoped), so a search can
    never find text the viewer would not see. Returns {messages, replies, next}: `next` is the
    cursor for the following page, or None at the end."""
    threads = message_threads(home)
    if before is not None:
        at, mid = before
        threads = [t for t in threads if (t["at"], t["m"]["id"]) < (at, mid)]
    if to:
        threads = [t for t in threads if to in thread_sessions(home, t["m"])]
    q = (q or "").strip().lower()
    out, reps, more = [], {}, False
    for t in threads:
        part = guard.scrub_tree({"messages": [t["m"]], "replies": {t["m"]["id"]: thread_replies(home, t["m"]["id"])}})
        if view:
            part = view(part)
        m, rs = part["messages"][0], part["replies"].get(t["m"]["id"]) or []
        if q and q not in json.dumps([m.get("to"), m.get("by"), m.get("body"), m.get("ticket")]
                                     + [[r.get("from"), r.get("body")] for r in rs], ensure_ascii=False).lower():
            continue
        if len(out) == limit:
            more = True
            break
        out.append(m)
        if rs:
            reps[m["id"]] = rs
    nxt = "%d.%s" % (as_int(out[-1].get("last_activity_at")), out[-1]["id"]) if more and out else None
    return {"messages": out, "replies": reps, "next": nxt}


def history_query(qs):
    """GET /api/messages query: before, limit, to, q (each at most once; anything else is a 400).
    Returns (before, limit, to, q) or raises ValueError(detail)."""
    if set(qs) - {"before", "limit", "to", "q"} or any(len(v) != 1 for v in qs.values()):
        raise ValueError("expected before, limit, to and q, each at most once")
    get = lambda k: qs[k][0] if k in qs else None  # noqa: E731
    before = None
    if get("before") is not None:
        m = CURSOR_RE.fullmatch(get("before"))
        if not m:
            raise ValueError("before must be a cursor from `next`")
        before = (int(m.group(1)), m.group(2))
    limit = HISTORY_LIMIT
    if get("limit") is not None:
        if not re.fullmatch(r"[0-9]{1,3}", get("limit")) or not 1 <= int(get("limit")) <= HISTORY_MAX:
            raise ValueError("limit must be 1..%d" % HISTORY_MAX)
        limit = int(get("limit"))
    to = get("to")
    if to is not None and to != "all" and not CODE_RE.fullmatch(to):
        raise ValueError("to must be a session code or all")
    q = get("q")
    if q is not None and len(q) > 200:
        raise ValueError("q is at most 200 characters")
    return before, limit, to or None, q


def owner_only_operator(op):
    """The operator block for anyone but an owner: no account_hint (free text about the account
    the operator runs on; owners only, like the account email). A new dict: the hub's shared state
    is never changed."""
    if not isinstance(op, dict):
        return op
    return dict(op, account_hint=None)


def cards_for(sessions, approver):
    """The session rows for one viewer: the private card extras (the last assistant text and the
    last prompt) only for an approver; everyone else gets `private: "hidden"` in their place. New
    row and card dicts: the hub's shared state is never changed."""
    out = []
    for s in sessions or []:
        card = s.get("card") if isinstance(s, dict) else None
        if isinstance(card, dict) and not approver:
            card = {k: v for k, v in card.items() if k not in CARD_PRIVATE}
            card["private"] = "hidden"
            s = dict(s, card=card)
        out.append(s)
    return out


# Why a recorded answer was not delivered (decision status_reason; the page words them).
# Q3 (ticket text for approvers and release managers only): lib/tickets/viewer.py, kept under these
# names here for the routes that build board state.
from tickets.viewer import tasks_for, ticket_text_for  # noqa: E402,F401


REASON_CODES = ("screen", "partial", "partial_box", "moved_on", "timeout", "not_approver", "unknown_session", "operator",
                "config")
UNDELIVERED = ("stale", "timeout", "unauthorized")
# How a pane prompt was settled from the session's report (lib/reconcile.py RESULTS).
RECONCILE_RESULTS = ("match", "differs", "unknown", "no_answer")


def mark_reason_code(ev):
    """A status_reason code for a mark / unauthorized event, from its fixed reason prefixes."""
    if ev.get("type") == "unauthorized":
        return "not_approver" if not ev.get("reason") else None
    r = str(ev.get("reason") or "")
    if ev.get("status") != "stale":
        return None
    if r.startswith("press partial:"):
        return "partial"
    if r.startswith("press refused:"):
        return "screen"
    return None


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


def rc_state(home):
    """The owner's Remote Control choices for the page (rc-prefs.json): {all, sessions}."""
    try:
        return rc.page_state(home)
    except Exception as exc:  # noqa: BLE001 - the board keeps working without it
        log("rc state: %r" % exc)
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
    body = OWN_CHARSET_RE.sub("", raw.decode("utf-8"), count=1)
    # Every inline <script> (the pre-paint theme script, the Deck theme one and the app) gets the CSP nonce;
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
    def __init__(self, status, error, detail="", headers=None, extra=None):
        super().__init__(error)
        self.status, self.error, self.detail, self.headers = status, error, detail, headers or {}
        self.extra = extra or {}  # more fields of the JSON body (fixed keys, never free text)


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
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def common_headers(self, cache="no-store"):
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def refuse(self, exc):
        if self.command == "POST" and not getattr(self, "body_read", False):
            self.drain_body()
        body = dict(getattr(exc, "extra", None) or {})
        body.update({"error": exc.error, "detail": exc.detail})
        self.send_json(exc.status, body, getattr(exc, "headers", None) or None)

    def drain_body(self):
        """A POST refused before its body was read (too large, wrong type, CSRF ...): drop up to
        DRAIN_MAX bytes of it, within DRAIN_TIMEOUT_S in all, and close the connection after the
        reply. Unread data at close makes the kernel send a reset, and the client would see that
        instead of the refusal; on a kept-alive connection it would be parsed as the next request."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        self.drain(min(n, DRAIN_MAX), DRAIN_TIMEOUT_S)

    def drain(self, limit, seconds):
        self.close_connection = True
        self.body_read = True
        deadline = time.monotonic() + seconds
        try:
            while limit > 0:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    break
                self.connection.settimeout(rem)
                chunk = self.rfile.read1(min(limit, 16384))
                if not chunk:
                    break
                limit -= len(chunk)
        except OSError:      # timeouts included: the reply still goes out
            pass
        finally:
            try:
                self.connection.settimeout(SOCKET_TIMEOUT_S)   # for the reply
            except OSError:
                pass

    def framed(self):
        """Every method starts here. False when the request was answered already: Transfer-Encoding
        (not accepted: a body is framed by one Content-Length), several Content-Length headers or
        one that is not digits get 400 and a closed connection. A body on a method that takes none
        (GET, HEAD, PUT, OPTIONS ...) is dropped (bounded) and the connection closes after the
        reply, so it can never be read as a second request."""
        self.body_read = False
        cl = self.headers.get_all("Content-Length") or []
        bad = ("Transfer-Encoding is not accepted" if self.headers.get_all("Transfer-Encoding")
               else "more than one Content-Length" if len(cl) > 1
               else "Content-Length must be digits" if cl and not re.fullmatch(r"[0-9]{1,15}", cl[0].strip())
               else None)
        if bad:
            self.drain(DRAIN_MAX, DRAIN_BAD_FRAMING_S)
            self.send_json(400, {"error": "bad_request", "detail": bad})
            return False
        if self.command != "POST" and cl and int(cl[0]) > 0:
            self.drain_body()
        return True

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

    def check_fetch(self):
        """A GET with side effects (GET /api/tickets/<ref> spends the tracker token's rate limit):
        only the board page's own fetch. `X-Board: 1` is a custom header, so a cross-site page can
        only send it after a CORS preflight, which this server never grants; a browser's
        Sec-Fetch-Site other than same-origin (or none: typed in) is refused as well. An <img> or a
        link from another site to the loopback listener gets 403 before anything else runs."""
        if self.headers.get("X-Board") != "1":
            raise Refused(403, "csrf", "X-Board: 1 header required")
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            raise Refused(403, "csrf", "cross-site request refused")

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
        raw = self.rfile.read(n)
        self.body_read = True
        try:
            body = json.loads(raw.decode("utf-8"))
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
        if not self.framed():
            return
        self.send_json(405, {"error": "method_not_allowed"}, {"Allow": "GET, HEAD, POST"})

    do_OPTIONS = do_PUT = do_DELETE = do_PATCH = do_TRACE = do_CONNECT = not_allowed

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.framed():
            return
        if serve_api.is_v1(self.path):
            return serve_api.handle(self, _SELF)
        if serve_api.is_mcp(self.path):
            return serve_api.handle_mcp(self, _SELF)   # POST only: 405 after the cross-site checks
        try:
            self.check_host()
            path = urllib.parse.urlsplit(self.path).path
            if TICKET_GET_RE.fullmatch(path) or path == "/api/reminders" or path == "/api/messages" \
                    or serve_dev.fetch_checked(path):
                self.check_fetch()   # before identity, audit or any tracker call
            ident, via = self.identity()
        except Refused as exc:
            return self.refuse(exc)
        if path in ("/", "/index.html"):
            return self.page()
        if path == "/api/state":
            self.hub_kick()
            return self.send_json(200, self.state_for(ident, via))
        if path == "/api/events":
            return self.events(ident, via)
        if path == "/api/me":
            return self.send_json(200, self.me_for(ident, via))
        if path == "/api/reminders":
            return self.reminders_get(ident, via)
        if path == "/api/lessons":
            q = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("q") or [""])[0].lower()[:200]
            rows = lessons.board_view(self.server.home, 10 ** 6)
            if q:
                rows = [r for r in rows if q in json.dumps([r.get("title"), r.get("cause"), r.get("resolution"),
                                                             r.get("rule"), r.get("rule_kind")], ensure_ascii=False).lower()]
            cfg = fleet.load_fleet()
            return self.send_json(200, self.viewer_mask(guard.scrub_tree({
                "lessons": rows, "can_edit": match_id(ident, fleet.id_list(cfg, "approvers")) is not None
                and is_owner(cfg, ident)}), ident, via, cfg))
        if path == "/api/pomodoro/day":
            return self.pomodoro_day()
        if path == "/api/messages":
            return self.message_history(ident, via)
        m = TICKET_GET_RE.fullmatch(path)
        if m:
            return self.ticket_get(m.group(1), ident, via)
        if serve_dev.is_get(path):          # the developer portal and its key routes
            return serve_dev.get(self, _SELF, path, ident, via)
        if path in STATIC:
            return self.static(*STATIC[path])
        return self.send_json(404, {"error": "not_found"})

    def message_history(self, ident, via):
        """GET /api/messages?before=&limit=&to=&q=: older threads for the page's History view,
        through the same viewer filter as /api/state (viewer_for)."""
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        try:
            before, limit, to, q = history_query(qs)
        except ValueError as exc:
            return self.send_json(400, {"error": "bad_request", "detail": str(exc)})
        cfg = fleet.load_fleet()
        return self.send_json(200, message_page(self.server.home, before, limit, to, q,
                                                view=lambda st: self.viewer_for(st, ident, via, cfg)))

    def pomodoro_day(self):
        """GET /api/pomodoro/day?d=YYYY-MM-DD: one kept day's per-stream breakdown, fetched by the
        page when a past day is selected (the pushed view carries daily totals only, so SSE stays
        small). Exactly one `d`, nothing else; 400 for a bad date, 404 outside the 14 kept days."""
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        d = qs.get("d") or []
        if set(qs) != {"d"} or len(d) != 1:
            return self.send_json(400, {"error": "bad_request", "detail": "expected ?d=YYYY-MM-DD"})
        try:
            view = pomodoro.day_view(read_json(os.path.join(self.server.home, "pomodoro.json")), d[0], now_ms())
        except ValueError:
            return self.send_json(400, {"error": "bad_request", "detail": "expected ?d=YYYY-MM-DD"})
        if view is None:
            return self.send_json(404, {"error": "not_found"})
        return self.send_json(200, guard.scrub_tree(view))

    # -- reminders (lib/reminders.py): the owner's own; nobody else sees them, not even a count
    def reminder_owner(self, ident, via, action):
        """The configured spelling of an owner (fleet.yaml owners, else approvers), else 403."""
        if not reminders.owners():
            self.audit(ident, via, "reminder-" + action, result="refused", reason="no owners configured")
            raise Refused(403, "no_owners", "fleet.yaml owners and approvers are empty: nobody has reminders")
        by = reminders.owner_of(ident)
        if by is None:
            self.audit(ident, via, "reminder-" + action, result="refused", reason="not an owner")
            raise Refused(403, "not_owner", "reminders belong to the board's owner")
        return by

    def reminders_get(self, ident, via):
        """GET /api/reminders (X-Board, same-origin): the owner's reminders, each with its next
        time, lead window and phase, plus the server's `now` for the page's countdown."""
        try:
            self.reminder_owner(ident, via, "read")
        except Refused as exc:
            return self.refuse(exc)
        return self.send_json(200, reminders.board_view(self.server.home))

    def reminder_add(self, body, ident, via):
        """POST /api/reminders {title, at, date?, lead_min?, level?, repeat?, breakthrough?}."""
        by = self.reminder_owner(ident, via, "add")
        allowed = {"title", "at", "date", "lead_min", "level", "repeat", "breakthrough"}
        if set(body) - allowed or not isinstance(body.get("title"), str) or not isinstance(body.get("at"), str) \
                or not all(isinstance(body.get(k), (str, type(None))) for k in ("date", "level", "repeat")) \
                or not isinstance(body.get("lead_min"), (int, type(None))) or isinstance(body.get("lead_min"), bool) \
                or not isinstance(body.get("breakthrough", True), bool):
            raise Refused(400, "bad_body", "title and at (strings); date, level, repeat (strings); lead_min (int); "
                                           "breakthrough (bool)")
        try:
            r, note = reminders.add(self.server.home, body["title"], body["at"], body.get("date"), body.get("lead_min"),
                                    body.get("level"), body.get("repeat") or "once", body.get("breakthrough", True),
                                    "board:%s" % by)
        except reminders.Bad as exc:
            raise Refused(400, "bad_reminder", str(exc)) from exc
        except reminders.Busy as exc:
            raise Refused(409, "busy", str(exc)) from exc
        self.audit(ident, via, "reminder-add", id=r["id"], result="ok")
        return self.send_json(201, {"reminder": guard.scrub_tree(reminders.view(r, reminders.now_ms())), "note": note})

    def reminder_act(self, rid, body, ident, via):
        """POST /api/reminders/<id> {action: ack|snooze|delete, minutes?}."""
        by = self.reminder_owner(ident, via, "act")
        action = body.get("action")
        if action not in ("ack", "snooze", "delete") or set(body) - {"action", "minutes"}:
            raise Refused(400, "bad_action", "action is ack, snooze or delete")
        minutes = body.get("minutes", 5)
        if action == "snooze" and (not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 720):
            raise Refused(400, "bad_minutes", "minutes is 1-720")
        if not reminders.ID_RE.fullmatch(rid):
            raise Refused(404, "not_found", "no such reminder")
        try:
            r = reminders.act(self.server.home, rid, action, "board:%s" % by, minutes=minutes * 60000)
        except reminders.NotFound as exc:
            raise Refused(404, "not_found", "no such reminder") from exc
        except reminders.Bad as exc:
            raise Refused(409, "bad_state", str(exc)) from exc
        except reminders.Busy as exc:
            raise Refused(409, "busy", str(exc)) from exc
        self.audit(ident, via, "reminder-" + action, id=rid, result="ok")
        return self.send_json(200, {"status": "ok", "action": action,
                                    "reminder": guard.scrub_tree(reminders.view(r, reminders.now_ms())) if r else None})

    def ticket_get(self, raw_ref, ident, via):
        """GET /api/tickets/<ref>: approvers ∪ release_managers only (the tracker token reads what
        its owner can, so plain viewers never get tickets). Identity first, then the tracker."""
        cfg = fleet.load_fleet()
        readers = fleet.id_list(cfg, "approvers") + fleet.id_list(cfg, "release_managers")
        ref = urllib.parse.unquote(raw_ref)[:tickets.REF_MAX + 1]
        if not readers:
            tickets.audit(self.server.home, {"action": "get", "ref": ref, "by": ident, "via": via,
                                             "result": "refused", "reason": "no approvers configured"})
            return self.send_json(403, {"error": "no_approvers", "detail": "fleet.yaml approvers is empty: nobody may read tickets"})
        by = match_id(ident, readers)
        if by is None:
            tickets.audit(self.server.home, {"action": "get", "ref": ref, "by": ident, "via": via,
                                             "result": "refused", "reason": "not an approver or release manager"})
            return self.send_json(403, {"error": "not_approver", "detail": "only approvers and release managers read tickets"})
        try:
            reg = tickets.load(cfg)
            t = tickets.get(ref, by=by, via=via, registry=reg, home=self.server.home)
            t = dict(t, link=self.ticket_link(t), tracker=self.ticket_tracker(reg, t))
        except tickets.TicketError as exc:
            extra = {"Retry-After": str(int(exc.retry_after))} if exc.status == 429 and exc.retry_after else None
            return self.send_json(exc.status, exc.to_json(), extra)
        except Exception as exc:  # noqa: BLE001 - never a traceback (or a header) in a response
            log("tickets: unexpected %s" % type(exc).__name__)
            return self.send_json(502, {"error": "ticket_upstream", "detail": "unexpected error"})
        return self.send_json(200, self.viewer_mask(tickets.redact_tree(t), ident, via, cfg))

    def ticket_link(self, t):
        """The newest take of this ticket (tickets/links/), for the board's ticket panel: who has it."""
        from tickets import writes  # noqa: WPS433
        rows = writes.links(self.server.home, "%s:%s" % (t["provider"], t["key"]))
        if not rows:
            return None
        return {k: rows[0].get(k) for k in ("status", "task_id", "session", "taken_by", "taken_at")}

    @staticmethod
    def ticket_tracker(reg, t):
        """What the panel may offer: the tracker's writes, its states and what a take does upstream."""
        prov = reg.by_name(t["provider"])
        if prov is None:
            return None
        spec = prov.take_spec()
        return {"capabilities": list(prov.capabilities), "states": prov.transitions(t["key"]),
                "take": {"comment": bool(spec.get("comment")), "transition": spec.get("transition"),
                         "assign": spec.get("assign")}}

    def ticket_reply(self, fn, ident=None, via=None):
        """Run a ticket write; TicketError -> its status and {error, detail, ...}."""
        try:
            out = fn()
        except tickets.TicketError as exc:
            extra = {"Retry-After": str(int(exc.retry_after))} if exc.status == 429 and exc.retry_after else None
            return self.send_json(exc.status, exc.to_json(), extra)
        except Exception as exc:  # noqa: BLE001 - never a traceback (or a header) in a response
            log("tickets: unexpected %s" % type(exc).__name__)
            return self.send_json(500, {"error": "ticket_failed", "detail": "unexpected error"})
        self.hub_kick()
        # a take / release / write echoes the link (taken_by, released_by): emails for owners only
        return self.send_json(200, self.viewer_mask(tickets.redact_tree(out), ident, via, fleet.load_fleet()))

    def ticket_take(self, body, ident, via):
        """POST /api/tickets/take {ref, session, comment?, transition?, assign?}: approvers only,
        checked (and a refusal audited) before anything is fetched, sent or written. `comment`:
        a string (scrubbed, footer added), false to skip, absent for the tracker's take.comment."""
        from tickets import writes  # noqa: WPS433
        ref, session = body.get("ref"), body.get("session")
        c, tr, asg = body.get("comment"), body.get("transition", True), body.get("assign", True)

        def run():
            writes.checked(ident, "take", ref, via, self.server.home)
            if not isinstance(ref, str) or not isinstance(session, str):
                raise tickets.TicketError("bad_body", "ref and session must be strings")
            if not (c is None or c is False or isinstance(c, str)) or not isinstance(tr, bool) or not isinstance(asg, bool):
                raise tickets.TicketError("bad_body", "comment: string|false; transition, assign: booleans")
            return writes.take(ref, session, ident, via=via, home=self.server.home,
                               comment_text=c if isinstance(c, str) else None, do_comment=c is not False,
                               do_transition=tr, do_assign=asg)
        return self.ticket_reply(run, ident, via)

    def ticket_write(self, raw_ref, op, body, ident, via):
        """POST /api/tickets/<ref>/comment {text} | transition {to} | assign {who} | release {}:
        approvers only, checked (and a refusal audited) before anything is sent or written."""
        from tickets import writes  # noqa: WPS433
        ref = urllib.parse.unquote(raw_ref)[:tickets.REF_MAX + 1]

        def run():
            writes.checked(ident, op, ref, via, self.server.home)
            if op == "release":
                return writes.release(ref, ident, via=via, home=self.server.home)
            field = {"comment": "text", "transition": "to", "assign": "who"}[op]
            v = body.get(field)
            if not isinstance(v, str):
                raise tickets.TicketError("bad_body", "%s must be a string" % field)
            return getattr(writes, op)(ref, v, ident, via=via, home=self.server.home)
        return self.ticket_reply(run, ident, via)

    def viewer_mask(self, obj, ident, via, cfg):
        """Other people's emails are for owners only: anyone else gets role labels ("you" for their
        own verified email on the tunnel; on the local listener everyone is `local`)."""
        if is_owner(cfg, ident):
            return obj
        return pii.mask(obj, cfg, viewer=ident if via == "cf-jwt" else None)

    def state_for(self, ident, via=None):
        with self.server.hub.cond:
            state = dict(self.server.hub.state)
        cfg = fleet.load_fleet()
        state.update({"mode": "local", "me": ident, "writable": True,
                      "can_message": match_id(ident, sorted(messagers(cfg))) is not None,
                      "asker": asker_only(cfg, ident),
                      "approver": match_id(ident, fleet.id_list(cfg, "approvers")) is not None,
                      "owner": is_owner(cfg, ident) and match_id(ident, fleet.id_list(cfg, "approvers")) is not None,
                      "account": self.account_for(ident, cfg), "page_version": page_version()})
        state["sessions"] = cards_for(state.get("sessions"), match_id(ident, fleet.id_list(cfg, "approvers")) is not None)
        # online / dnd / away and whether askers are declined now (computed per viewer: the
        # compute-low check is two small file reads and depends on the clock); the dnd record for
        # owners only, never a usage number
        state["availability"] = availability.view(self.server.home, cfg, owner=is_owner(cfg, ident))
        state["local_model"] = desk.view(self.server.home, cfg, owner=is_owner(cfg, ident))
        if not is_owner(cfg, ident):
            state["operator"] = owner_only_operator(state.get("operator"))
        return self.viewer_for(state, ident, via, cfg)

    def viewer_for(self, state, ident, via, cfg):
        """What one viewer may see of board text, for /api/state, SSE and GET /api/messages alike
        (a part of the state, such as {messages, replies}, goes through it the same way): ticket
        text for deciders only, other people's emails for owners only (viewer_mask)."""
        state = ticket_text_for(state, match_id(ident, sorted(deciders(cfg))) is not None)
        return self.viewer_mask(state, ident, via, cfg)

    def account_for(self, ident, cfg):
        """The Claude account the sessions run on: {email, org} for owners (fleet.yaml owners, else
        approvers), {hidden: true} for everyone else, None when it is not known or switched off
        (collector, account.json). The email is personal data: approvers who are not owners,
        askers and viewers never get it."""
        if not fleet.account_enabled(cfg):  # also when a stale account.json is still there
            return None
        rec = read_json(os.path.join(self.server.home, "account.json"))
        if not isinstance(rec, dict) or not rec.get("email"):
            return None
        if not is_owner(cfg, ident):
            return {"hidden": True}
        return {"email": str(rec["email"]), "org": str(rec["org"]) if rec.get("org") else None}

    def me_for(self, ident, via):
        """GET /api/me: built only from the verified identity and fleet.yaml, never from request
        headers. api_keys: the keys issued to this identity, newest first (never a key or a hash)."""
        cfg = fleet.load_fleet()
        me = {"identity": ident, "via": "access" if via == "cf-jwt" else "local"}
        me.update(roles_of(cfg, ident))
        # Whether this identity has reminders (the owner): the page asks /api/reminders only then.
        me["reminders"] = reminders.owner_of(ident, cfg) is not None
        me["api_keys"] = own_keys(self.server.home, ident, cfg)
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

    def events(self, ident, via=None):
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
                    data = json.dumps(self.state_for(ident, via), ensure_ascii=False)
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
        if not self.framed():       # also resets body_read: one handler serves a kept-alive connection
            return
        if serve_api.is_v1(self.path):
            return serve_api.handle(self, _SELF)   # bearer key, not CSRF: its own checks
        if serve_api.is_mcp(self.path):
            return serve_api.handle_mcp(self, _SELF)
        try:
            origins = self.check_host()
            ident, via = self.identity()
            self.check_csrf(origins)
            path = urllib.parse.urlsplit(self.path).path
            if serve_dev.is_post(path):     # API keys from the portal: `board apikey --by board:<ident>`
                return serve_dev.post(self, _SELF, path, ident, via)
            m = re.fullmatch(r"/api/decisions/([^/]+)/answer", path)
            if m:
                body = self.read_body()
                return self.answer(m.group(1), body, ident, via)
            m = re.fullmatch(r"/api/decisions/([^/]+)/resolve", path)
            if m:
                body = self.read_body()
                return self.resolve(m.group(1), body, ident, via)
            m = re.fullmatch(r"/api/messages/([^/]+)/release", path)
            if m:
                body = self.read_body()
                return self.release(m.group(1), body, ident, via)
            if path == "/api/tickets/take":
                body = self.read_body()
                return self.ticket_take(body, ident, via)
            m = re.fullmatch(r"/api/tickets/([^/]+)/(comment|transition|assign|release)", path)
            if m:
                body = self.read_body()
                return self.ticket_write(m.group(1), m.group(2), body, ident, via)
            if path == "/api/reminders":
                body = self.read_body()
                return self.reminder_add(body, ident, via)
            m = re.fullmatch(r"/api/reminders/([^/]+)", path)
            if m:
                body = self.read_body()
                return self.reminder_act(m.group(1), body, ident, via)
            if path == "/api/messages":
                body = self.read_body()
                return self.message(body, ident, via)
            if path == "/api/away":
                body = self.read_body()
                return self.set_away(body, ident, via)
            if path == "/api/dnd":
                body = self.read_body()
                return self.set_dnd(body, ident, via)
            if path == "/api/notify-prefs":
                body = self.read_body()
                return self.notify_prefs(body, ident, via)
            if path == "/api/presence":
                body = self.read_body()
                return self.presence(body, ident, via)
            if path == "/api/alerts/ack":
                body = self.read_body()
                return self.ack_alerts(body, ident, via)
            if path == "/api/rc":
                body = self.read_body()
                return self.set_rc(body, ident, via)
            if path == "/api/resume":
                body = self.read_body()
                return self.resume(body, ident, via)
            if path == "/api/lessons":
                return self.lesson(None, self.read_body(), ident, via)
            m = re.fullmatch(r"/api/lessons/([^/]+)", path)
            if m:
                return self.lesson(m.group(1), self.read_body(), ident, via)
            m = re.fullmatch(r"/api/sessions/([^/]+)/action", path)
            if m:
                body = self.read_body()
                return self.action(urllib.parse.unquote(m.group(1)), body, ident, via)
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
        # A form of several questions (a pane question): {"options": [i, j, ...]}, one option number
        # per question, checked against the decision below; no answer text.
        options = body.get("options")
        # The same form with free text for some questions: {"answers": [{"question_index": 0,
        # "option": 2}, {"question_index": 1, "text": "..."}, ...]}, one per question, in order.
        # Text is cleaned like board answer does (control, escape and line-break characters become
        # spaces), capped, and screened by the secret guard; it reaches the CLI by file.
        # A multi-select question (alone, or in a form) is answered {"question_index": i,
        # "labels": ["A", "C"]}: exact option labels, no repeats, [] for none; checked against the
        # decision below and kept as its option numbers, ascending.
        answers = body.get("answers")
        texts = None
        labels = None
        if answers is not None:
            if options is not None or body.get("answer") is not None or body.get("option") is not None:
                raise Refused(400, "bad_answers", "answers go without options, answer or option")
            if not isinstance(answers, list) or not 1 <= len(answers) <= 4:
                raise Refused(400, "bad_answers", "answers must be one entry per question (1-4)")
            options, texts, labels = [], [], []
            for i, a in enumerate(answers):
                if not isinstance(a, dict) or set(a) - {"question_index", "option", "text", "labels"} \
                        or ("question_index" in a and (type(a["question_index"]) is not int or a["question_index"] != i)) \
                        or sum(k in a for k in ("option", "text", "labels")) != 1:
                    raise Refused(400, "bad_answers", "each answer is {question_index, option}, {question_index, text} "
                                  "or {question_index, labels}, in order")
                if "labels" in a:
                    ls = a["labels"]
                    if not isinstance(ls, list) or len(ls) > 8 or any(not isinstance(x, str) or len(x) > MAX_ANSWER for x in ls) \
                            or len(set(ls)) != len(ls):
                        raise Refused(400, "bad_answers", "labels are distinct option labels (at most 8)")
                    options.append([])   # its option numbers, set below from the question
                    texts.append(None)
                    labels.append(list(ls))
                    continue
                labels.append(None)
                if "option" in a:
                    o = a["option"]
                    if isinstance(o, bool) or not isinstance(o, int) or not 1 <= o <= 8:
                        raise Refused(400, "bad_answers", "an option is a number 1-8")
                    options.append(o)
                    texts.append(None)
                else:
                    t = a["text"]
                    if not isinstance(t, str) or len(t) > MAX_ANSWER:
                        raise Refused(400, "bad_answers", "a text answer is a string (max %d chars)" % MAX_ANSWER)
                    t = clean_free_text(t)
                    if not t or len(t) > MAX_FREE_TEXT or t.endswith("\\;"):
                        raise Refused(400, "bad_answers", "a text answer must be 1-%d characters of one line" % MAX_FREE_TEXT)
                    if is_free_text_label(t):
                        raise Refused(400, "bad_answers", "a text answer may not be the \"Type something\" row itself")
                    options.append(0)  # its "Type something" row, set below from the question
                    texts.append(t)
            body = dict(body, answer="(answers)")
        elif options is not None:
            if not isinstance(options, list) or not 2 <= len(options) <= 4 or any(
                    isinstance(o, bool) or not isinstance(o, int) or not 1 <= o <= 8 for o in options):
                raise Refused(400, "bad_options", "options must be one option number (1-8) per question")
            if body.get("answer") is not None or body.get("option") is not None:
                raise Refused(400, "bad_options", "options go without answer or option")
            body = dict(body, answer="(options)")
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
            # A permission prompt (allow/deny) or a pane question is answered by pressing keys in a
            # session on the owner's machine: owners only.
            if (kind == "permission" or (kind == "question" and dec.get("source") == "pane")) \
                    and not is_owner(cfg, ident):
                self.audit(ident, via, "answer", id=did, result="refused", reason="not an owner")
                raise Refused(403, "not_owner", "only the board's owner answers prompts in a session")
            if dec.get("status") not in ("pending", "unauthorized", "answered"):
                self.audit(ident, via, "answer", id=did, result="gone", status=dec.get("status"))
                raise Refused(409, "gone", "decision is already %s" % dec.get("status"))
            if kind == "permission" and answer not in ("allow", "deny"):
                raise Refused(400, "bad_answer", "a permission answer is allow or deny")
            if options is not None:
                qs = dec.get("questions") if isinstance(dec.get("questions"), list) else []
                is_ms = [isinstance(q, dict) and q.get("multiSelect") is True for q in qs]
                if kind != "question" or dec.get("source") != "pane" or not qs \
                        or (len(qs) < 2 and not (labels is not None and is_ms[0])):
                    raise Refused(400, "bad_options", "options are only for a form of several questions in a session")
                if len(options) != len(qs):
                    raise Refused(400, "bad_answers" if labels is not None else "bad_options",
                                  "%d answers for %d questions" % (len(options), len(qs)))
                if labels is None and any(is_ms):
                    raise Refused(400, "bad_options", "a multi-select question is answered by its labels")
                for i, (o, q) in enumerate(zip(options, qs)):
                    n = len(q.get("options") or []) if isinstance(q, dict) else 0
                    if labels is not None and (labels[i] is not None) != is_ms[i]:
                        raise Refused(400, "bad_answers", "question %d is answered by %s" % (
                            i + 1, "its labels" if is_ms[i] else "an option or a text"))
                    if labels is not None and labels[i] is not None:
                        names = [str(x.get("label")) if isinstance(x, dict) else str(x) for x in (q.get("options") or [])]
                        if any(x not in names for x in labels[i]):
                            raise Refused(400, "bad_answers", "question %d has no such option" % (i + 1))
                        options[i] = [k + 1 for k, x in enumerate(names) if x in labels[i]]
                    elif texts is not None and texts[i] is not None:
                        options[i] = n + 1  # the "Type something" row
                        if n + 1 > 9:
                            raise Refused(400, "bad_answers", "question %d has too many options for free text" % (i + 1))
                    elif o > n:
                        raise Refused(400, "bad_options", "question %d has %d options" % (i + 1, n))
            # A free-text answer is relayed as [BOARD decision], the most trusted channel: it must not
            # ask the session for a secret value. (Picking one of the session's own options is fine,
            # and a form answered by option numbers is nothing but the session's own options.)
            if texts is not None:
                qs = dec.get("questions") or []
                # A secret-shaped value in a text would be typed into the session and kept in the
                # decision file as it is: held, like a request for a secret (never typed redacted).
                reason = next(("a text answer holds a secret-shaped value" for t in texts
                               if t is not None and guard.scrub(t) != t), None) or next((r for r in (guard.screen_answer(t, [o.get("label") for o in (qs[i].get("options") or [])
                                                                  if isinstance(o, dict)])
                                           for i, t in enumerate(texts) if t is not None) if r), None)
            else:
                reason = None if kind == "permission" or options is not None else guard.screen_answer(raw_answer, dec.get("options"))
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
            # A pane question with clipped labels or repeated labels is read-only here.
            if pane_q and not dec.get("pressable"):
                self.audit(ident, via, "answer", id=did, result="refused", reason="not pressable")
                raise Refused(409, "answer_in_session",
                              "this question can only be answered in the session")
            if pane_q and len(dec.get("questions") or []) > 1 and options is None:
                raise Refused(400, "bad_options", "a form of several questions is answered with one option per question")
            if pane_q and options is None and any(isinstance(q, dict) and q.get("multiSelect") is True
                                                  for q in (dec.get("questions") or [])):
                raise Refused(400, "bad_answers", "a multi-select question is answered by its labels")
            code = dec.get("from")
            if (kind == "permission" or pane_q) and not code:
                snap = read_json(os.path.join(home, "snapshot.json")) or {}
                code = next((s.get("code") for s in snap.get("sessions") or []
                             if isinstance(s, dict) and s.get("session_id") == dec.get("session_id")), None)
            if option is not None and not pane_q:
                raise Refused(400, "bad_option", "option is only for a question shown in a session")
            # The answer goes by file, never as an argument: "-", "--help" or "--answer-file" typed on
            # the board are recorded literally and are never read as options.
            if texts is not None:
                fd, afile = tempfile.mkstemp(prefix=".form.", dir=home)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump([{"question_index": i, "text": t} if t is not None
                                   else {"question_index": i, "labels": labels[i]} if labels and labels[i] is not None
                                   else {"question_index": i, "option": o}
                                   for i, (o, t) in enumerate(zip(options, texts))], fh, ensure_ascii=False)
                    rc, out, err = self.board("answer", did, "--form-file", afile, "--by", by)
                finally:
                    try:
                        os.unlink(afile)
                    except OSError:
                        pass
            elif options is not None:
                rc, out, err = self.board("answer", did, "--options", ",".join(str(o) for o in options), "--by", by)
            else:
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
            if rc == 6 and texts is not None:
                self.audit(ident, via, "answer", id=did, result="held", reason="secret guard (board answer)")
                raise Refused(400, "held_answer", "not recorded or relayed: a text answer asks for a secret value")
            if rc == 2 and options is not None:
                self.audit(ident, via, "answer", id=did, result="refused", reason="options do not match")
                raise Refused(400, "bad_options", "those options do not match the questions; reload and try again")
            if rc == 2 and option is not None:
                self.audit(ident, via, "answer", id=did, result="refused", reason="option does not match")
                raise Refused(400, "bad_option", "that option does not match the question; reload and try again")
            if rc == 8:  # the gate's task was busy: nothing recorded, or the gate not yet updated
                raise Refused(409, "task_busy", "the task is busy; try again in a moment")
            if rc == 4:
                log("board answer %s refused: %s" % (did, err))
                self.audit(ident, via, "answer", id=did, result="unauthorized")
                raise Refused(403, "not_approver", "you may not answer this decision")
            # Never report success unless the file really holds this answer by this person.
            rec = read_json(os.path.join(home, "decisions", did + ".json")) or {}
            if options is not None:
                answer = rec.get("answer") if rec.get("answer_options") == options \
                    and (rec.get("answer_texts") or None) == (texts if texts is not None else None) else None
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
                self.board("mark", did, "stale", "--reason", "unknown_session")
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
            if rc == 7:  # the prompt still waits but is not on screen: pending again
                self.audit(ident, via, "press", id=did, code=code, key=answer, result="not_visible")
                # board press wrote press_note_code ("scrolled" only when the TUI's scrolled line
                # was still on screen) and press_not_visible (how many presses found it not visible).
                after = read_json(os.path.join(self.server.home, "decisions", did + ".json"))
                after = after if isinstance(after, dict) else {}
                scrolled = after.get("press_note_code") == "scrolled"
                elsewhere = after.get("press_note_code") == "form_elsewhere"
                n = after.get("press_not_visible")
                n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1
                raise Refused(409, "screen_not_ready",
                              "the session's screen is scrolled, so the prompt is not visible; try again, or answer in the session"
                              if scrolled else
                              "the session's form shows another question; nothing was pressed: try again, or answer in the session"
                              if elsewhere else
                              "the prompt is not visible on the session's screen (scrolled, covered or not redrawn yet); "
                              "nothing was pressed: answer in the session, or try again later",
                              extra={"scrolled": scrolled, "attempts": n, "question": bool(pane_q), "elsewhere": elsewhere})
            if rc == 5:  # free text typed into the picker, Enter not pressed
                self.audit(ident, via, "press", id=did, code=code, key=answer, result="partial")
                after = read_json(os.path.join(self.server.home, "decisions", did + ".json"))
                if isinstance(after, dict) and after.get("status_reason") == "partial_box":
                    # a box of a multi-select question did not flip: its digit may be in "Type something"
                    raise Refused(409, "partial", "a box did not flip, so nothing was submitted; a digit may be left in "
                                  "\"Type something\": clear it in the session", extra={"box": True})
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
        if not messages.valid_to(to):     # "ALL", "Triage" ...: refused, never a session code
            raise Refused(400, "bad_to", "to must be a session code, \"triage\" or \"all\" (lowercase)")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE:
            raise Refused(400, "bad_body", "body must be a non-empty string (max %d chars)" % MAX_MESSAGE)
        cfg = fleet.load_fleet()
        if match_id(ident, sorted(messagers(cfg))) is None:
            self.audit(ident, via, "message", result="refused", reason="not an approver or asker")
            raise Refused(403, "not_approver", "only approvers, release managers and askers send messages")
        asker = match_id(ident, askers(cfg)) if asker_only(cfg, ident) else None
        if asker is not None and to == "all":
            # an asker interrupts no one wholesale: "triage" lets BOARD pick the sessions
            self.audit(ident, via, "message", to=to, result="refused", reason="asker_no_all", asker=True)
            raise Refused(403, "asker_no_all", messages.ASKER_NO_ALL)
        if to not in ("all", messages.TRIAGE) and not self.known_code(to):
            # like /api/v1: a message to nobody would sit "new" forever (after the identity check,
            # so an outsider learns nothing about the session codes)
            raise Refused(400, "unknown_session", "no session %s on this board" % to)
        # lib/messages.py: scrubbed before it is stored; a secret request is stored "held" (never
        # relayed), audited and alerted without echoing the text.
        with self.server.write_lock:
            if asker is not None:
                wait = messages.asker_wait(self.server.home, ident, now_ms())
                if wait:
                    self.audit(ident, via, "message", to=to, result="refused", reason="rate_limited", asker=True)
                    raise Refused(429, "rate_limited", "at most %d messages in %d minutes; try again in %d s"
                                  % (messages.ASKER_MAX, messages.ASKER_WINDOW_MS // 60000, wait),
                                  headers={"Retry-After": str(wait)})
            # the owner's do-not-disturb, or compute is short: an asker's message is declined with a
            # canned reply (lib/availability.py), never relayed; still rate limited above
            decline = None
            if asker is not None:
                reason = availability.decline_reason(self.server.home, cfg)
                if reason:
                    decline = (reason, availability.reply_text(cfg, reason))
            rec = messages.create(self.server.home, to, text, ident, background_alert=True, asker=asker,
                                  decline=decline)
            self.audit(ident, via, "message", id=rec["id"], to=to, result=rec["status"],
                       **dict({"asker": True} if asker is not None else {},
                              **({"reason": rec["declined_reason"]} if rec["status"] == "declined" else {})))
        self.hub_kick()
        out = {"id": rec["id"], "status": rec["status"]}
        if rec["status"] == "held":
            out["reason"] = rec["held_reason"]
        elif rec["status"] == "declined":
            out.update({"reason": rec["declined_reason"], "reply": decline[1]})
        return self.send_json(201, out)

    def release(self, mid, body, ident, via):
        """Release a held message (a false positive of the secret guard): owners only, and the
        body must confirm it ({"confirm": "release"}, the page's second click). Audited; the
        relay then carries a reminder that this authorizes no secret values."""
        if not MSG_ID_RE.fullmatch(mid):
            raise Refused(400, "bad_id", "invalid message id")
        # Fails closed: only the two verified listeners (a direct local request, a verified Access
        # JWT) may release; any other channel (an API key, later) may not.
        if via not in ("loopback", "cf-jwt"):
            self.audit(ident, via, "message-release", id=mid, result="refused", reason="channel")
            raise Refused(403, "release_refused", "held messages are released only on the board itself")
        by = self.owner(ident, via, "message-release", what="release a held message", id=mid)
        if body.get("confirm") != "release":
            raise Refused(400, "confirm_required", "confirm the release: {\"confirm\": \"release\"}")
        with self.server.write_lock:
            rec = messages.read(self.server.home, mid) or {}
            # releasing is owner-only, so the four-eyes rule counts owners: a single owner may release
            # a message they sent themselves (nobody else could)
            releasers = owner_ids(fleet.load_fleet())
            if self_release_refused(rec, ident, releasers):
                self.audit(ident, via, "message-release", id=mid, result="refused", reason="self-release")
                raise Refused(403, "self_release", "another owner must release a message you sent")
            try:
                rel = messages.release(self.server.home, mid, by)
            except messages.MessageError as exc:
                self.audit(ident, via, "message-release", id=mid, result=exc.code)
                raise Refused(404 if exc.code == "not_found" else 409, exc.code, exc.detail) from exc
            # an asker's message while their requests are declined: released into "declined" (the
            # canned reply, never relayed), checked now, not when it was sent
            declined = rel.get("status") == "declined"
            self.audit(ident, via, "message-release", id=mid, result="declined" if declined else "released",
                       **({"reason": rel.get("declined_reason")} if declined else {}))
        self.hub_kick()
        out = {"id": mid, "status": rel.get("status")}
        if declined:
            out["reason"] = rel.get("declined_reason")
        return self.send_json(200, out)


    def resolve(self, did, body, ident, via):
        """A board ask answered in the session (or in another one): approvers only, with a confirm
        step, through `board resolve ID --in-pane --by <approver>` (the note by a 0600 file)."""
        if not DEC_ID_RE.fullmatch(did):
            raise Refused(400, "bad_id", "invalid decision id")
        by = self.approver(ident, via, "resolve", what="mark a question answered in the session", id=did)
        if body.get("confirm") != "resolve":
            raise Refused(400, "confirm_required", "confirm it: {\"confirm\": \"resolve\"}")
        note = body.get("note") or ""
        if not isinstance(note, str) or len(note) > MAX_ANSWER:
            raise Refused(400, "bad_note", "note must be a string (max %d chars)" % MAX_ANSWER)
        home = self.server.home
        with self.server.write_lock:
            if not isinstance(read_json(os.path.join(home, "decisions", did + ".json")), dict):
                raise Refused(404, "not_found", "no such decision")
            args = ["resolve", did, "--in-pane", "--by", by]
            nfile = None
            if note.strip():
                fd, nfile = tempfile.mkstemp(prefix=".note.", dir=home)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(note.strip())
                args += ["--note-file", nfile]
            try:
                rc, out, err = self.board(*args)
            finally:
                if nfile:
                    try:
                        os.unlink(nfile)
                    except OSError:
                        pass
        self.hub_kick()
        if rc == 0:
            self.audit(ident, via, "resolve", id=did, result="resolved")
            return self.send_json(200, {"id": did, "status": "answered_in_pane"})
        log("board resolve %s (rc %d): %s %s" % (did, rc, err, out))
        self.audit(ident, via, "resolve", id=did, result="refused", rc=rc)
        if rc == 2:
            raise Refused(409, "not_a_board_ask", "only a board ask or a pane question can be closed this way")
        if rc == 4:
            raise Refused(403, "not_approver", "you may not close this decision")
        if rc == 6:
            raise Refused(400, "held_note", "the note asks for a secret value: not recorded")
        raise Refused(409, "gone", "the decision is no longer open")

    def set_away(self, body, ident, via):
        """Owner-away mode from the board: owners only (an owner who is an approver)."""
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
        if not is_owner(cfg, ident):      # owner-away unlocks the cross-session consensus rules
            self.audit(ident, via, "away", state=state, result="refused", reason="not an owner")
            raise Refused(403, "not_owner", "only the board's owner changes owner-away mode")
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


    def set_dnd(self, body, ident, via):
        """Do not disturb from the board's profile menu: owners only (an owner who is an approver).
        {"state": "on"|"off"}. The owner stays present (not away): askers are declined with a canned
        reply while it is on (lib/availability.py). Audited."""
        state = body.get("state")
        if state not in ("on", "off"):
            raise Refused(400, "bad_state", "state must be \"on\" or \"off\"")
        cfg = fleet.load_fleet()
        by = match_id(ident, fleet.id_list(cfg, "approvers"))
        if by is None or not is_owner(cfg, ident):
            self.audit(ident, via, "dnd", state=state, result="refused", reason="not an owner")
            raise Refused(403, "not_owner", "only the board's owner changes do not disturb")
        with self.server.write_lock:
            changed, _rec = availability.set_dnd(self.server.home, state == "on", "board:%s" % by)
            self.audit(ident, via, "dnd", state=state, result=state if changed else "unchanged")
        self.hub_kick()
        return self.send_json(200, {"dnd": state == "on", "status": state if changed else "unchanged",
                                    "availability": availability.view(self.server.home, cfg, owner=True)})

    def lesson(self, lid, body, ident, via):
        """Add (lid None) or change a lesson. Approvers only; the body goes to `board lesson` by a
        0600 file, never argv; the CLI validates the rule, screens and scrubs the text."""
        action = "add" if lid is None else body.get("action")
        if lid is not None and (not lessons.ID_RE.fullmatch(lid) or action not in ("edit", "activate", "retire", "delete")):
            raise Refused(400, "bad_request", "a lesson id and action edit|activate|retire|delete")
        by = self.owner(ident, via, "lesson", what="change lessons", lesson=lid, op=action)
        if action == "delete" and body.get("confirm") != "delete":
            raise Refused(400, "confirm_required", "send \"confirm\": \"delete\" to delete a lesson")
        fields = {k: body[k] for k in ("title", "rule", "cause", "resolution") if k in body}
        if action in ("activate", "retire", "delete") and fields:
            raise Refused(400, "bad_request", "%s takes no fields" % action)
        args = ["lesson", action] + ([lid] if lid else []) + ["--by", "board:%s" % by]
        tmp = None
        try:
            if action in ("add", "edit"):
                fd, tmp = tempfile.mkstemp(prefix=".lesson.", dir=self.server.home)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(fields, fh)
                args += ["--file", tmp]
            with self.server.write_lock:
                rc, out, err = self.board(*args)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        msg = guard.scrub(err.split(": ", 2)[-1][:300]) if err else ""
        self.audit(ident, via, "lesson", op=action, lesson=lid or (out.split() or [None])[0], rc=rc)
        if rc == 0:
            self.hub_kick()
            return self.send_json(200, {"id": out.split()[0], "status": out.split()[-1]})
        raise Refused(*{1: (404, "not_found"), 2: (400, "bad_lesson"), 4: (403, "not_approver"),
                        6: (400, "held_text"), 8: (409, "busy")}.get(rc, (500, "lesson_failed")), msg or "not saved")

    def owner(self, ident, via, action, what="change notification settings", **kw):
        """Owner-only board controls: an approver (the board CLI checks that again) who is also an
        owner (fleet.yaml owners, else approvers). 403 not_owner otherwise, audited."""
        by = self.approver(ident, via, action, what=what, **kw)
        if not is_owner(fleet.load_fleet(), ident):
            self.audit(ident, via, action, result="refused", reason="not an owner", **kw)
            raise Refused(403, "not_owner", "only the board's owner may %s" % what)
        return by

    def send_empty(self, status=204):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.common_headers()
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()

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
        """Notification controls from the board: owners only, through the board CLI (argv)."""
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
        by = self.owner(ident, via, "notify-prefs", **kw)
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
        cfg = fleet.load_fleet()
        by = match_id(ident, fleet.id_list(cfg, "approvers"))
        if by is None or not is_owner(cfg, ident):
            # only the owner's presence may hold back the owner's pushes: anyone else's heartbeat is
            # ignored (not stored, not counted, not audited), with no error the page would repeat
            return self.send_empty(204)
        now = now_ms()
        seen = now - max(0, idle) if idle is not None else None
        changed = prefs.record_presence(self.server.home, by, visible, seen, via, now)
        if changed:
            self.audit(ident, via, "presence", visible=visible, result="ok")
        return self.send_json(200, {"status": "ok", "visible": visible})


    def ack_alerts(self, body, ident, via):
        """The Deck panel's "acknowledge all": stops the escalation of every open alert (what
        `board ack '*'` does in a terminal). Approvers only; the alerts stay on the board, and a
        condition that opens again alerts again."""
        if body.get("scope") != "all":
            raise Refused(400, "bad_scope", "scope must be \"all\"")
        by = self.owner(ident, via, "alerts-ack", what="acknowledge alerts", scope="all")
        with self.server.write_lock:
            rc, out, err = self.board("ack", "--by", "board:%s" % by, "*")
            if rc != 0 and not (rc == 1 and not err):  # 1 and silent: nothing was open
                log("board ack failed (rc %d): %s" % (rc, guard.scrub("%s %s" % (err, out))))
                self.audit(ident, via, "alerts-ack", scope="all", result="error", rc=rc)
                raise Refused(500, "ack_failed", "the alerts could not be acknowledged")
            n = sum(1 for line in out.splitlines() if line.startswith("acked "))
            self.audit(ident, via, "alerts-ack", scope="all", result="ok", acked=n)
        self.hub_kick()
        return self.send_json(200, {"status": "ok", "acked": n})

    def known_code(self, code):
        if not isinstance(code, str) or not CODE_RE.fullmatch(code):
            return False
        return code in fleet.known_codes(self.server.home)

    @staticmethod
    def _audit_str(v):
        """A request value for the audit log: a short string, or "?"."""
        return v if isinstance(v, str) and len(v) <= 64 and not fleet.CONTROL_RE.search(v) else "?"

    def set_rc(self, body, ident, via):
        """POST /api/rc {code|"all", state}: Remote Control for one session or all of them. For
        one session it is the same as POST /api/sessions/<code>/action {action: rc_on|rc_off}.
        Who is asking is checked first, so a refused probe is audited whatever it sent."""
        code, state = body.get("code"), body.get("state")
        by = self.owner(ident, via, "rc", what="change Remote Control",
                           code=self._audit_str(code), state=self._audit_str(state))
        if state not in ("on", "off"):
            raise Refused(400, "bad_state", "state must be \"on\" or \"off\"")
        if code != "all" and not self.known_code(code):
            raise Refused(400, "bad_code", "code must be a session on this board or \"all\"")
        return self.session_action(code, "rc_" + state, ident, via, route="rc", by=by)

    def resume(self, body, ident, via):
        """POST /api/resume {code: CODE} | {codes: [CODE, ...]}: resume sessions stuck on a usage
        limit, exactly the ones named (the page sends the codes it showed the owner; "all" is not
        accepted). Owners only, checked before the input (keys go into sessions on the owner's
        machine). `board resume` runs in the background: it reads each pane and sends nothing
        unless the screen is one of Claude's limit screens; each attempt is in events.jsonl."""
        code, codes = body.get("code"), body.get("codes")
        by = self.owner(ident, via, "resume", what="resume sessions", code=self._audit_str(code),
                        codes=[self._audit_str(c) for c in codes[:MAX_RESUME]] if isinstance(codes, list) else self._audit_str(codes))
        if (code is None) == (codes is None):
            raise Refused(400, "bad_code", "give code or codes")
        codes = [code] if codes is None else codes
        if (not isinstance(codes, list) or not codes or len(codes) > MAX_RESUME or len(set(map(str, codes))) != len(codes)
                or not all(self.known_code(c) for c in codes)):
            raise Refused(400, "bad_code", "codes must be 1-%d distinct sessions on this board" % MAX_RESUME)
        argv = [BOARD, "resume"] + list(codes) + ["--by", "board:%s" % by]
        try:
            subprocess.Popen(argv, env=dict(os.environ, BOARD_HOME=self.server.home),  # noqa: S603
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as exc:
            log("board resume could not start: %r" % exc)
            self.audit(ident, via, "resume", codes=codes, result="error")
            raise Refused(500, "resume_failed", "board resume could not start") from exc
        self.audit(ident, via, "resume", codes=codes, result="ok")
        self.hub_kick()
        return self.send_json(200, {"status": "started", "codes": codes})

    def action(self, code, body, ident, via):
        """POST /api/sessions/<code>/action {action}: an allowlisted session action. Approvers
        only, checked before the input."""
        name = body.get("action")
        by = self.owner(ident, via, "session-action", what="run session actions",
                           code=self._audit_str(code), name=self._audit_str(name))
        if name not in actions.ACTIONS:
            raise Refused(400, "bad_action", "action must be one of %s" % ", ".join(sorted(actions.ACTIONS)))
        if not self.known_code(code):
            raise Refused(404, "not_found", "no such session on this board")
        return self.session_action(code, name, ident, via, by=by)

    def session_action(self, code, name, ident, via, route="action", by=None):
        """The choice (rc) or the queue entry (compact) is stored at once through the board CLI
        (argv); typing into the pane (seconds, or until the session is idle) runs in the
        background (`board session apply`), and the page follows it in the snapshot."""
        audit_action = "rc" if route == "rc" else "session-action"
        kw = {"code": code, "state": name[3:]} if route == "rc" else {"code": code, "name": name}
        if name == "compact":
            argv = ["session", code, "compact", "--by", "board:%s" % by, "--queue-only"]
        else:
            argv = ["rc", name[3:], code, "--by", "board:%s" % by, "--no-apply"]
        with self.server.write_lock:
            rcode, out, err = self.board(*argv)
            if rcode != 0:
                log("board %s failed (rc %d): %s %s" % (" ".join(argv[:3]), rcode, err, out))
                self.audit(ident, via, audit_action, result="error", rc=rcode, **kw)
                raise Refused(500, "action_failed" if route != "rc" else "rc_failed", "the session action could not be stored")
            self.audit(ident, via, audit_action, result="ok", **kw)
        apply_argv = [BOARD, "session", "apply", "--by", "board:%s" % by,
                      "--only", "compact" if name == "compact" else "rc"] + (["--pending"] if code == "all" else [code])
        try:
            subprocess.Popen(apply_argv, env=dict(os.environ, BOARD_HOME=self.server.home),  # noqa: S603
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as exc:
            log("board session apply could not start: %r" % exc)
        self.hub_kick()
        return self.send_json(200, {"status": "ok", "action": name, "code": code, "rc": rc_state(self.server.home)})


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
    consensus status (`consensus <id> <status>`, mirrored, never relayed as a decision) and each
    session with scheduler advisories to send (`schedule <CODE> due`, board schedule outbox)."""
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
        if isinstance(d, dict) and d.get("kind") == desk.FORWARD_KIND:
            continue    # the local desk's "forward to CODE?": lib/desk.py applies it, never relayed
        if isinstance(d, dict) and d.get("status") == "answered" and d.get("kind") == serve_api.TAKE_KIND:
            # an API take request: never relayed as a [BOARD decision]; the operator runs the take
            # on approve (prompts/board-operator.md, "Take requests from the API")
            out.append((as_int(d.get("answered_at")), "take %s answered" % d.get("id"),
                        ("t", d.get("id"), d.get("answered_at"))))
        elif isinstance(d, dict) and d.get("status") == "answered" and d.get("kind") != "permission" \
                and not (d.get("source") == "pane" and d.get("relay") != "operator"):
            out.append((as_int(d.get("answered_at")), "decision %s answered" % d.get("id"),
                        ("d", d.get("id"), d.get("answered_at"))))
    for path in glob.glob(os.path.join(home, "messages", "*.json")):
        m = read_json(path)
        if isinstance(m, dict) and m.get("status") == "new":
            tk = m.get("ticket") if isinstance(m.get("ticket"), dict) else {}
            if m.get("kind") == "ticket" and m.get("event") in ("taken", "released") \
                    and tickets.KEY_RE.fullmatch(str(tk.get("key") or "")):
                # A ticket take/release: relay `board message show <id>` ([BOARD ticket KEY] ...).
                out.append((as_int(m.get("created_at")), "ticket %s %s %s" % (tk["key"], m["event"], m.get("id")),
                            ("m", m.get("id"))))
            else:
                # "triage": the operator picks the sessions (prompts/board-operator.md), then records
                # them with `board message route`
                out.append((as_int(m.get("created_at")), "message %s new%s" % (
                    m.get("id"), " triage" if m.get("to") == messages.TRIAGE else ""), ("m", m.get("id"))))
    for r in lessons.board_view(home, 10 ** 6):   # a new auto lesson waits for the operator's note
        if r.get("status") == "candidate" and r.get("source") == "auto" and not r.get("cause") and not r.get("resolution"):
            out.append((as_int(r.get("first_seen")), "lesson %s candidate" % r["id"], ("l", r["id"])))
    sched = read_json(os.path.join(home, "scheduler.json"))
    if isinstance(sched, dict) and isinstance(sched.get("due"), dict) \
            and not os.path.exists(os.path.join(home, "schedule-off")):     # off: nothing to deliver
        for code, ids in sorted(sched["due"].items()):    # advisories to send (board schedule outbox)
            if isinstance(ids, list) and ids and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", str(code)):
                out.append((as_int(sched.get("generated_at")), "schedule %s due" % code,
                            ("s", code, tuple(str(i) for i in ids))))
    out.sort(key=lambda x: x[0])
    return [(line, key) for _, line, key in out]


DESK_SWEEP_S = 15


def desk_sweep(home):
    """The local asker desk's fallback pass (lib/desk.py sweep, check_tick): run by board-serve and
    by --watch too, so a dead collector never strands an asker's message with the desk."""
    try:
        cfg = fleet.load_fleet()
        if desk.desk_messages(home):
            return desk.sweep(home, cfg, check_tick=True)
    except Exception as exc:  # noqa: BLE001 - never stops the server or the watch
        sys.stderr.write("board-serve: desk sweep: %s\n" % type(exc).__name__)
    return []


def desk_sweeper(home, stop):
    while not stop.wait(DESK_SWEEP_S):
        desk_sweep(home)


def watch(home, once=False, tick_s=480):
    """Print one line per new actionable item (oldest first), and "tick" every tick_s seconds.

    Meant for the operator's Monitor: waiting costs no tokens, and a line is only printed when
    there is something to relay (or a heartbeat is due)."""
    seen = set()
    last_tick, last_sweep = time.time(), 0.0
    while True:
        if time.time() - last_sweep >= DESK_SWEEP_S:
            desk_sweep(home)     # before listing: a message it puts back to "new" shows at once
            last_sweep = time.time()
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
                    help="do not serve: print 'decision ID answered' / 'message ID new' / 'schedule CODE due' lines for the "
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
    api = serve_api.api_settings(fcfg)
    toks = ", ".join("%s (%s)" % (cn, "unbound" if o is None else "bound: " + (", ".join(sorted(o)) or "nobody"))
                     for cn, o in sorted(api["service_tokens"].items()))
    info.append("api      %s" % ("off (serve.api.enabled)" if not api["enabled"] else
                                 "/api/v1 and /mcp with a board API key; tunnel: %s" % (
                                     "off" if not cfg["tunnel"] else "off (serve.api.remote: false)"
                                     if not api["remote"] else "Access service tokens %s or an allowed email" % (
                                         toks or "(none listed)"))))
    if fleet.id_list(fcfg, "askers"):
        info.append("askers   %s (message only)" % ", ".join(fleet.id_list(fcfg, "askers")))
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
    sweep_stop = threading.Event()
    threading.Thread(target=desk_sweeper, args=(home, sweep_stop), daemon=True).start()
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    sys.stderr.write("board-serve: " + "\nboard-serve: ".join(info) + "\n")
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        sweep_stop.set()
        for srv in servers[1:]:
            srv.shutdown()
        for srv in servers:
            srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
