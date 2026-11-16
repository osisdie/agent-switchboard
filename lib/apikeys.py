#!/usr/bin/env python3
"""API keys: the key store behind the board's /api/v1 (docs/contract.md "API keys").

A key looks like asb_live_<kid>_<secret> (asb_test_ for the mock): kid is 12 characters [a-z0-9],
secret 43 characters base64url (256 random bits). It is shown once, when it is made, and never
again. The store keeps only HMAC-SHA256(pepper, key); the pepper is 32 random bytes in its own
0600 file, so a copy of apikeys/ alone is useless.

  $BOARD_HOME/apikeys/<kid>.json    one key (0600, written atomically under apikeys.lock)
  $BOARD_HOME/secrets/apikey.pepper the pepper (0600, made on the first create or request)

Every key is issued to a person (`owner`: an email, or `local`). The owner field is attribution,
never approval: a key never answers decisions and never manages keys.

Who may do what (--by WHO; "board:<who>" when the board page asks on someone's behalf):
  owners (fleet.yaml `owners`)   everything, on every key: create (to anyone), approve a request,
                                 revoke, rotate (with a grace window), expire (set or extend),
                                 scopes (revoke + reissue), ls/show of all keys
  approvers                      create for themselves or a listed person (owners, approvers,
                                 release_managers, serve.tunnel.allowed_emails); ls/show their own
                                 keys; revoke their own keys or keys they issued
  anyone else                    ls/show/revoke their own keys; request a key for themselves (it
                                 stays pending, and unusable, until an owner approves it; a request
                                 lapses after apikeys.request_ttl_days, default 14)
Someone else's key does not exist for a non-owner: show and revoke answer "no such key".
Matching is case-insensitive. Every change, and every refusal, is audited in events.jsonl
(type "apikey"); never a key, never a hash.

Usage (through `board apikey ...`):
  apikeys.py create --name N --owner WHO --by WHO [--scopes a,b] [--expires 90d|YYYY-MM-DD]
                    [--rate N] [--test] [--json]
  apikeys.py request --name N --by WHO [--scopes a,b] [--expires 90d|YYYY-MM-DD] [--json]
  apikeys.py approve ID --by OWNER [--scopes a,b] [--expires 90d|YYYY-MM-DD]
                    (--scopes: the requested scopes or fewer)
  apikeys.py ls --by WHO [--owner WHO] [--all] [--json]
  apikeys.py show ID --by WHO [--json]
  apikeys.py revoke ID --by WHO
  apikeys.py rotate ID --by OWNER [--grace 0|30m|24h|7d] [--expires 90d|YYYY-MM-DD] [--json]
  apikeys.py expire ID --expires 90d|YYYY-MM-DD --by OWNER
  apikeys.py scopes ID --scopes a,b --by OWNER [--json]
  --rate N: requests a minute (1-%(maxrate)d, default %(rate)d); --test: an asb_test_ key for the mock board.
create, request, rotate and scopes print the new key alone on stdout (or {"id", "key", ...} with
--json) and a note on stderr: copy it now, it is never shown again. ls hides revoked and expired
keys unless --all.
Scopes: %(scopes)s. There is no scope to answer decisions, press prompts, set away, vote, pass a
production gate or manage keys.
Expiry: default %(default)dd, at most %(max)dd from now; there is no "never". Grace: at most %(grace)dd.
Exit: 0 ok, 1 unknown key or a key in the wrong state, 2 usage (an unknown scope, a bad date),
4 not authorized (nothing written), 8 the key store is busy (try again).
"""
import base64
import contextlib
import datetime
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402

SCHEMA_VERSION = 1
DIR = "apikeys"
LOCK = "apikeys.lock"
PEPPER = os.path.join("secrets", "apikey.pepper")
PEPPER_BYTES = 32
PREFIX = {"live": "asb_live_", "test": "asb_test_"}
KID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
KID_LEN = 12
# The whole key; the same shape as the gitleaks rule "agent-switchboard-api-key" (.gitleaks.toml).
KEY_RE = re.compile(r"asb_(live|test)_([0-9a-z]{12})_([A-Za-z0-9_-]{43})")
KID_RE = re.compile(r"[0-9a-z]{12}")
SCOPES = ("read:state", "read:decisions", "read:tasks", "read:messages", "tasks:write", "write:messages",
          "tickets:read", "tickets:write", "tickets:take")
DEFAULT_SCOPES = ("read:state",)
DAY_MS = 86400 * 1000
DEFAULT_DAYS, MAX_DAYS, MAX_GRACE_DAYS = 90, 365, 7
DEFAULT_RATE, MAX_RATE = 60, 600
MAX_PENDING_PER_OWNER = 5
DEFAULT_REQUEST_TTL_DAYS, MAX_REQUEST_TTL_DAYS = 14, 90
NAME_MAX = 60
EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s.]{1,63}")
CTL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")
USED_FLUSH_MS = 60 * 1000
STATUSES = ("active", "pending", "revoked", "expired")


class Bad(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


def now_ms():
    v = os.environ.get("BOARD_NOW_MS")
    return int(v) if v and v.isdigit() else int(time.time() * 1000)


# ------------------------------------------------------------------ identities and roles
def identity(raw):
    """(id, via): "board:<who>" is a request from the board page on <who>'s behalf."""
    s = str(raw or "").strip()
    via = "cli"
    if s.lower().startswith("board:"):
        s, via = s[len("board:"):].strip(), "board"
    s = s.lower()
    if s != "local" and not (len(s) <= 254 and EMAIL_RE.fullmatch(s) and not CTL_RE.search(s)
                             and not invisible(s)):
        raise Bad("--by/--owner: an email or \"local\" (got %r)" % str(raw)[:60])
    return s, via


def invisible(s):
    """Unicode format and separator characters (bidi controls, zero-width, line/paragraph
    separators): text that reads differently from what it is."""
    return any(unicodedata.category(c) in ("Cf", "Zl", "Zp") for c in s)


def request_ttl_ms(cfg=None):
    """How long a key request may wait for an owner (fleet.yaml apikeys.request_ttl_days)."""
    cfg = fleet.load_fleet() if cfg is None else cfg
    a = cfg.get("apikeys") if isinstance(cfg.get("apikeys"), dict) else {}
    d = a.get("request_ttl_days", DEFAULT_REQUEST_TTL_DAYS)
    if not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= MAX_REQUEST_TTL_DAYS:
        d = DEFAULT_REQUEST_TTL_DAYS
    return d * DAY_MS


def _ids(cfg, key):
    return {i.strip().lower() for i in fleet.id_list(cfg, key)}


def roles(cfg, who):
    out = set()
    if who in _ids(cfg, "owners"):
        out.add("owner")
    if who in _ids(cfg, "approvers"):
        out.add("approver")
    return out


def listed(cfg):
    """The people a key may be issued to by an approver."""
    t = fleet.get_key(cfg, "serve.tunnel.allowed_emails")
    extra = {str(e).strip().lower() for e in t} if isinstance(t, list) else set()
    return _ids(cfg, "owners") | _ids(cfg, "approvers") | _ids(cfg, "release_managers") | extra


# ------------------------------------------------------------------ files
def home_dir():
    return fleet.ensure_home()


def _mkdir_private(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _key_path(home, kid):
    if not isinstance(kid, str) or not KID_RE.fullmatch(kid):
        raise Bad("no such key: %s" % str(kid)[:40], 1)
    return os.path.join(home, DIR, kid + ".json")


def _write_json(path, obj):
    """Atomic: a fresh 0600 temp file in the same directory, then rename over the target."""
    tmp = "%s.tmp.%d.%s" % (path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked(home, wait_s=5.0):
    fd = os.open(os.path.join(home, LOCK), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        end = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise Bad("the key store is busy; try again", 8) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def pepper(home, create=False):
    """The pepper bytes, or None when there is none yet (and create is False). Never follows a
    symlink; a pepper file not owned by this user, readable by others, or of the wrong size is
    refused rather than used. A new one is made only while no key exists (a lost pepper is
    restored from a backup, never silently replaced: every key would stop working), and atomically
    (a full temp file, then a hard link), so a crash never leaves an empty pepper."""
    path = os.path.join(home, PEPPER)
    if create and not os.path.lexists(path):
        n = len(load_all(home))
        if n:
            raise Bad("%s is missing but %d key(s) exist: restore it from your backup (without it no key can be "
                      "checked), or move apikeys/ aside to start over; nothing written" % (PEPPER, n), 1)
        _mkdir_private(os.path.dirname(path))
        tmp = "%s.tmp.%d.%s" % (path, os.getpid(), secrets.token_hex(4))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(secrets.token_bytes(PEPPER_BYTES))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, path)          # fails if another writer won: theirs is kept
            except FileExistsError:
                pass
            except OSError:
                # a filesystem without hard links: rename instead. Every creator holds
                # apikeys.lock, so nobody can slip in between the check and the rename.
                if not os.path.lexists(path):
                    os.rename(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise Bad("%s is not a regular file (a symlink?); refused" % PEPPER, 1) from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise Bad("%s is not a regular file owned by this user; refused" % PEPPER, 1)
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise Bad("%s is readable by others; chmod 600 it (and consider every key exposed)" % PEPPER, 1)
        data = fh.read(PEPPER_BYTES + 1)
    if len(data) != PEPPER_BYTES:
        raise Bad("%s is not %d bytes; keys cannot be checked" % (PEPPER, PEPPER_BYTES), 1)
    return data


def key_hash(pep, key):
    return hmac.new(pep, key.encode("ascii"), hashlib.sha256).hexdigest()


def load(home, kid):
    try:
        with open(_key_path(home, kid), encoding="utf-8") as fh:
            rec = json.load(fh)
    except FileNotFoundError:
        raise Bad("no such key: %s" % kid, 1) from None
    except (OSError, ValueError):
        raise Bad("key %s is unreadable" % kid, 1) from None
    if not isinstance(rec, dict) or rec.get("id") != kid:
        raise Bad("key %s is unreadable" % kid, 1)
    return rec


def load_all(home):
    d = os.path.join(home, DIR)
    out = []
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        return out
    for n in names:
        if not n.endswith(".json") or not KID_RE.fullmatch(n[:-5]):
            continue
        with contextlib.suppress(Bad):
            out.append(load(home, n[:-5]))
    return out


def save(home, rec):
    _mkdir_private(os.path.join(home, DIR))
    _write_json(_key_path(home, rec["id"]), rec)


def audit(home, ev):
    ev = dict({"ts": now_ms(), "type": "apikey"}, **ev)
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ status (pure)
def lapsed(rec, now, ttl_ms=None):
    """A request no owner approved within the request TTL: it can never be approved."""
    if rec.get("state") != "pending":
        return False
    ttl_ms = request_ttl_ms() if ttl_ms is None else ttl_ms
    at = rec.get("created_at")
    return not isinstance(at, int) or now - at >= ttl_ms


def status(rec, now, ttl_ms=None):
    """active | pending | revoked | expired, as of now. A rotated key whose grace window is over
    counts as revoked; a request older than the request TTL counts as expired (lapsed)."""
    if rec.get("state") == "revoked":
        return "revoked"
    g = rec.get("grace_until")
    if isinstance(g, int) and g <= now:
        return "revoked"
    if rec.get("state") == "pending":
        return "expired" if lapsed(rec, now, ttl_ms) else "pending"
    if not isinstance(rec.get("expires_at"), int) or rec["expires_at"] <= now:
        return "expired"
    return "active"


def view(rec, now, ttl_ms=None):
    """What ls/show (and later /api/me) may show: never the hash."""
    st = status(rec, now, ttl_ms)
    revoked_at = rec.get("revoked_at")
    if st == "revoked" and revoked_at is None:
        revoked_at = rec.get("grace_until")
    return {"id": rec["id"], "name": rec.get("name"), "owner": rec.get("owner"), "kind": rec.get("kind"),
            "scopes": list(rec.get("scopes") or []), "created_at": rec.get("created_at"),
            "created_by": rec.get("created_by"), "expires_at": rec.get("expires_at"),
            "last_used_at": rec.get("last_used_at"), "status": st, "revoked_at": revoked_at,
            "revoked_by": rec.get("revoked_by"), "approved_by": rec.get("approved_by"),
            "rate_per_min": rec.get("rate_per_min"), "successor": rec.get("successor"),
            "predecessor": rec.get("predecessor"), "grace_until": rec.get("grace_until"),
            "reason": "lapsed" if lapsed(rec, now, ttl_ms) else rec.get("revoke_reason") if st == "revoked" else None}


# ------------------------------------------------------------------ parsing
def parse_scopes(raw):
    if raw is None:
        return list(DEFAULT_SCOPES)
    out = []
    for s in str(raw).split(","):
        s = s.strip()
        if not s:
            continue
        if s not in SCOPES:
            raise Bad("unknown scope %r (one of: %s)" % (s[:40], ", ".join(SCOPES)))
        if s not in out:
            out.append(s)
    if not out:
        raise Bad("--scopes: at least one of: %s" % ", ".join(SCOPES))
    return out


def parse_expires(raw, now):
    """Epoch ms. Nd (days from now) or YYYY-MM-DD (valid through that day, UTC). Never "never"."""
    s = str(DEFAULT_DAYS) + "d" if raw is None else str(raw).strip()
    m = re.fullmatch(r"([0-9]{1,4})d", s)
    if m:
        at = now + int(m.group(1)) * DAY_MS
    elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
        try:
            day = datetime.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            raise Bad("--expires: no such date %s" % s) from None
        at = int((day + datetime.timedelta(days=1)).timestamp() * 1000)
    else:
        raise Bad("--expires: Nd (days) or YYYY-MM-DD; a key always expires")
    if at <= now:
        raise Bad("--expires: must be in the future (use revoke to stop a key now)")
    if at > now + MAX_DAYS * DAY_MS:
        raise Bad("--expires: at most %d days from now" % MAX_DAYS)
    return at


def parse_grace(raw):
    s = "0" if raw is None else str(raw).strip()
    m = re.fullmatch(r"([0-9]{1,5})([mhd]?)", s)
    if not m or (m.group(1) != "0" and not m.group(2)):
        raise Bad("--grace: 0, Nm, Nh or Nd")
    ms = int(m.group(1)) * {"": 0, "m": 60000, "h": 3600000, "d": DAY_MS}[m.group(2)]
    if ms > MAX_GRACE_DAYS * DAY_MS:
        raise Bad("--grace: at most %d days" % MAX_GRACE_DAYS)
    return ms


def parse_name(raw):
    if invisible(str(raw or "")):     # before the whitespace fold: U+2028/2029 count as \s
        raise Bad("--name: no invisible, bidi or line-separator characters")
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not s or len(s) > NAME_MAX or CTL_RE.search(s):
        raise Bad("--name: 1 to %d printable characters (no invisible or bidi characters)" % NAME_MAX)
    import guard  # noqa: WPS433 - only needed here
    if guard.changed(s)[1]:
        raise Bad("--name: looks like it holds a secret; pick a plain label")
    return s


def parse_rate(raw):
    if raw is None:
        return DEFAULT_RATE
    if not re.fullmatch(r"[0-9]{1,4}", str(raw)) or not 1 <= int(raw) <= MAX_RATE:
        raise Bad("--rate: 1 to %d requests per minute" % MAX_RATE)
    return int(raw)


def _opts(argv, values, flags=()):
    out, i = {}, 0
    while i < len(argv):
        k = argv[i]
        name = k[2:] if k.startswith("--") else None
        if name in flags and name not in out:
            out[name] = True
            i += 1
            continue
        if name not in values or i + 1 >= len(argv) or name in out:
            raise Bad("unknown, repeated or incomplete option: %s" % k[:40])
        out[name] = argv[i + 1]
        i += 2
    return out


# ------------------------------------------------------------------ keys
def new_key(home, kind):
    """(kid, key): a kid not in use yet."""
    for _ in range(20):
        kid = "".join(secrets.choice(KID_ALPHABET) for _ in range(KID_LEN))
        if not os.path.exists(_key_path(home, kid)):
            secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
            return kid, PREFIX[kind] + kid + "_" + secret
    raise Bad("could not pick a free key id", 1)  # pragma: no cover


def _record(home, pep, *, name, owner, kind, scopes, by, now, expires_at, rate, state, **extra):
    kid, key = new_key(home, kind)
    rec = {"schema_version": SCHEMA_VERSION, "id": kid, "name": name, "owner": owner, "kind": kind,
           "scopes": scopes, "hash": key_hash(pep, key), "created_by": by, "created_at": now,
           "expires_at": expires_at, "state": state, "revoked_at": None, "revoked_by": None,
           "last_used_at": None, "rate_per_min": rate, "successor": None, "predecessor": None,
           "grace_until": None, "approved_by": None, "approved_at": None}
    rec.update(extra)
    return rec, key


def verify(key, kind="live", home=None, now=None, ttl_ms=None):
    """(record, None) for a usable key, else (None, reason): malformed, wrong_env, unknown,
    mismatch, pending, revoked, expired. Constant-time on the secret part. ttl_ms: the request
    TTL (request_ttl_ms(cfg)), so a caller that has the config loaded does not reload it."""
    home = home or fleet.board_home()
    now = now_ms() if now is None else now
    m = KEY_RE.fullmatch(key or "") if isinstance(key, str) else None
    if not m:
        return None, "malformed"
    if m.group(1) != kind:
        return None, "wrong_env"
    try:
        pep = pepper(home)
        rec = load(home, m.group(2))
    except Bad:
        return None, "unknown"
    if pep is None or not isinstance(rec.get("hash"), str):
        return None, "unknown"
    if not hmac.compare_digest(rec["hash"], key_hash(pep, key)) or rec.get("kind") != kind:
        return None, "mismatch"
    st = status(rec, now, ttl_ms)
    if st != "active":
        return None, st
    return rec, None


def mark_used(kid, home=None, now=None):
    """Record last_used_at, at most once a minute per key; never blocks (a busy store skips it)."""
    home = home or fleet.board_home()
    now = now_ms() if now is None else now
    try:
        with locked(home, wait_s=0):
            rec = load(home, kid)
            last = rec.get("last_used_at")
            if isinstance(last, int) and now - last < USED_FLUSH_MS:
                return False
            rec["last_used_at"] = now
            save(home, rec)
            return True
    except (Bad, OSError):
        return False


# ------------------------------------------------------------------ commands
def _deny(home, action, by, via, msg, kid=None, owner=None):
    audit(home, {"action": action, "by": by, "via": via, "key_id": kid, "owner": owner, "result": "refused",
                 "reason": "not_authorized"})
    raise Bad(msg + "; nothing written", 4)


def _shown_once(rec, key, as_json, what):
    if as_json:
        print(json.dumps({"id": rec["id"], "key": key, "name": rec["name"], "owner": rec["owner"],
                          "scopes": rec["scopes"], "expires_at": rec["expires_at"],
                          "status": rec["state"]}, sort_keys=True))
    else:
        print(key)
    sys.stderr.write("Key %s %s for %s (%s), expires %s. Copy the key now: it is never shown again.\n"
                     % (rec["id"], what, rec["owner"], ",".join(rec["scopes"]), fmt_time(rec["expires_at"])))


def cmd_create(home, argv, pending=False):
    o = _opts(argv, ("name", "owner", "by", "scopes", "expires", "rate") if not pending
              else ("name", "by", "scopes", "expires"), ("json", "test") if not pending else ("json",))
    if not o.get("by") or not o.get("name") or (not pending and not o.get("owner")):
        raise Bad("create: --name N --owner WHO --by WHO" if not pending else "request: --name N --by WHO")
    by, via = identity(o["by"])
    owner = by if pending else identity(o["owner"])[0]
    action = "request" if pending else "create"
    cfg = fleet.load_fleet()
    r = roles(cfg, by)
    if not pending and "owner" not in r:
        if "approver" not in r:
            _deny(home, action, by, via, "--by %s may not issue keys (owners, or approvers for listed people); "
                  "use board apikey request" % by, owner=owner)
        if owner != by and owner not in listed(cfg):
            _deny(home, action, by, via, "--owner %s is not a listed person (owners, approvers, release_managers, "
                  "serve.tunnel.allowed_emails); an approver issues keys to them or to themselves" % owner,
                  owner=owner)
    name = parse_name(o["name"])
    scopes = parse_scopes(o.get("scopes"))
    now = now_ms()
    expires_at = parse_expires(o.get("expires"), now)
    rate = parse_rate(o.get("rate"))
    kind = "test" if o.get("test") else "live"
    with locked(home):
        if pending:
            ttl = request_ttl_ms(cfg)
            n = sum(1 for k in load_all(home) if k.get("owner") == owner and status(k, now, ttl) == "pending")
            if n >= MAX_PENDING_PER_OWNER:
                raise Bad("%s has %d pending requests already; wait for an owner" % (owner, n), 1)
        pep = pepper(home, create=True)
        extra = {"requested_ttl_ms": expires_at - now} if pending else {}
        rec, key = _record(home, pep, name=name, owner=owner, kind=kind, scopes=scopes, by=by, now=now,
                           expires_at=expires_at, rate=rate, state="pending" if pending else "active", **extra)
        save(home, rec)
    audit(home, {"action": action, "by": by, "via": via, "key_id": rec["id"], "name": name, "owner": owner,
                 "scopes": scopes, "kind": kind, "expires_at": expires_at, "result": "ok"})
    _shown_once(rec, key, o.get("json"), "requested (pending until an owner approves it)" if pending else "created")
    return 0


def _owner_only(home, action, by, via, rec):
    if "owner" not in roles(fleet.load_fleet(), by):
        _deny(home, action, by, via, "--by %s is not an owner (fleet.yaml owners)" % by, rec["id"], rec.get("owner"))


def _one(home, argv, action, values, flags=()):
    if not argv or argv[0].startswith("--"):
        raise Bad("%s: the key id first" % action)
    kid, o = argv[0], _opts(argv[1:], values, flags)
    if not o.get("by"):
        raise Bad("%s: --by WHO" % action)
    by, via = identity(o["by"])
    return kid, o, by, via


def _age(ms):
    d, h = divmod(max(0, ms) // 3600000, 24)
    return "%dd %dh" % (d, h) if d else "%dh %dm" % (h, max(0, ms) // 60000 % 60)


def cmd_approve(home, argv):
    kid, o, by, via = _one(home, argv, "approve", ("by", "expires", "scopes"))
    now = now_ms()
    ttl = request_ttl_ms()
    with locked(home):
        rec = load(home, kid)
        _owner_only(home, "approve", by, via, rec)
        if lapsed(rec, now, ttl):
            audit(home, {"action": "approve", "by": by, "via": via, "key_id": kid, "owner": rec.get("owner"),
                         "result": "refused", "reason": "lapsed", "request_age_ms": now - (rec.get("created_at") or 0)})
            raise Bad("request expired; ask again (requests lapse after %d days)" % (ttl // DAY_MS), 1)
        st = status(rec, now, ttl)
        if st != "pending":
            raise Bad("key %s is %s, not pending" % (kid, st), 1)
        requested = list(rec.get("scopes") or [])
        scopes = requested
        if o.get("scopes"):
            scopes = parse_scopes(o["scopes"])
            wider = [x for x in scopes if x not in requested]
            if wider:
                audit(home, {"action": "approve", "by": by, "via": via, "key_id": kid, "owner": rec.get("owner"),
                             "result": "refused", "reason": "wider_scopes", "scopes": scopes,
                             "requested_scopes": requested})
                raise Bad("--scopes may only narrow the request (%s); not requested: %s"
                          % (",".join(requested), ",".join(wider)))
        ttl_key = rec.get("requested_ttl_ms") if isinstance(rec.get("requested_ttl_ms"), int) else DEFAULT_DAYS * DAY_MS
        rec["expires_at"] = parse_expires(o.get("expires"), now) if o.get("expires") else \
            min(now + ttl_key, now + MAX_DAYS * DAY_MS)
        age = now - (rec.get("created_at") or now)
        rec.update({"state": "active", "approved_by": by, "approved_at": now, "scopes": scopes})
        save(home, rec)
    ev = {"action": "approve", "by": by, "via": via, "key_id": kid, "name": rec.get("name"),
          "owner": rec.get("owner"), "scopes": scopes, "request_age_ms": age, "expires_at": rec["expires_at"],
          "result": "ok"}
    if scopes != requested:
        ev["requested_scopes"] = requested
    audit(home, ev)
    print("%s active: approved for %s, scopes %s%s, requested %s ago, expires %s" % (
        kid, rec.get("owner"), ",".join(scopes), " (requested %s)" % ",".join(requested) if scopes != requested else "",
        _age(age), fmt_time(rec["expires_at"])))
    return 0


def cmd_revoke(home, argv):
    kid, o, by, via = _one(home, argv, "revoke", ("by",))
    now = now_ms()
    with locked(home):
        rec = load(home, kid)
        r = roles(fleet.load_fleet(), by)
        mine = rec.get("owner") == by or ("approver" in r and rec.get("created_by") == by)
        if "owner" not in r and not mine:
            # someone else's key does not exist for this caller: the same answer as an unknown id
            audit(home, {"action": "revoke", "by": by, "via": via, "key_id": kid, "owner": rec.get("owner"),
                         "result": "refused", "reason": "not_authorized"})
            raise Bad("no such key: %s" % kid, 1)
        before = status(rec, now)
        if before == "revoked":
            raise Bad("key %s is already revoked" % kid, 1)
        reason = "revoked"
        if rec.get("state") == "pending":
            reason = "withdrawn" if rec.get("owner") == by else "denied"
        rec.update({"state": "revoked", "revoked_at": now, "revoked_by": by, "revoke_reason": reason})
        save(home, rec)
    audit(home, {"action": "revoke", "by": by, "via": via, "key_id": kid, "name": rec.get("name"),
                 "owner": rec.get("owner"), "was": before, "reason": reason, "result": "ok"})
    print("%s %s" % (kid, reason))
    return 0


def _reissue(home, argv, action):
    """rotate (same scopes, optional grace) and scopes (new scopes, the old key revoked now)."""
    vals = ("by", "grace", "expires") if action == "rotate" else ("by", "scopes")
    kid, o, by, via = _one(home, argv, action, vals, ("json",))
    if action == "scopes" and not o.get("scopes"):
        raise Bad("scopes: --scopes a,b")
    now = now_ms()
    grace = parse_grace(o.get("grace")) if action == "rotate" else 0
    with locked(home):
        old = load(home, kid)
        _owner_only(home, action, by, via, old)
        st = status(old, now)
        if st != "active":
            raise Bad("key %s is %s; only an active key can be %s" % (
                kid, st, "rotated" if action == "rotate" else "rescoped"), 1)
        if old.get("successor"):
            raise Bad("key %s was already replaced by %s" % (kid, old["successor"]), 1)
        scopes = parse_scopes(o["scopes"]) if action == "scopes" else list(old.get("scopes") or [])
        expires_at = parse_expires(o.get("expires"), now) if action == "rotate" else old["expires_at"]
        pep = pepper(home, create=True)
        new, key = _record(home, pep, name=old["name"], owner=old["owner"], kind=old.get("kind", "live"),
                           scopes=scopes, by=by, now=now, expires_at=expires_at,
                           rate=old.get("rate_per_min", DEFAULT_RATE), state="active", predecessor=kid,
                           approved_by=old.get("approved_by"), approved_at=old.get("approved_at"))
        old["successor"] = new["id"]
        if grace:
            old["grace_until"] = now + grace
        else:
            old.update({"state": "revoked", "revoked_at": now, "revoked_by": by})
        old["revoke_reason"] = "rotated" if action == "rotate" else "rescoped"
        save(home, new)
        save(home, old)
    audit(home, {"action": action, "by": by, "via": via, "key_id": kid, "successor": new["id"],
                 "name": new["name"], "owner": new["owner"], "scopes": scopes, "grace_ms": grace, "result": "ok"})
    _shown_once(new, key, o.get("json"), "(replaces %s, %s)" % (kid, "rotated" if action == "rotate" else "new scopes"))
    if grace:
        sys.stderr.write("%s keeps working until %s.\n" % (kid, fmt_time(old["grace_until"])))
    return 0


def cmd_expire(home, argv):
    kid, o, by, via = _one(home, argv, "expire", ("by", "expires"))
    if not o.get("expires"):
        raise Bad("expire: --expires Nd|YYYY-MM-DD")
    now = now_ms()
    at = parse_expires(o["expires"], now)
    with locked(home):
        rec = load(home, kid)
        _owner_only(home, "expire", by, via, rec)
        st = status(rec, now)
        if st == "revoked":
            raise Bad("key %s is revoked" % kid, 1)
        if lapsed(rec, now):
            audit(home, {"action": "expire", "by": by, "via": via, "key_id": kid, "owner": rec.get("owner"),
                         "result": "refused", "reason": "lapsed"})
            raise Bad("request expired; ask again", 1)
        before = rec.get("expires_at")
        if st == "pending":
            rec["requested_ttl_ms"] = at - now
        rec["expires_at"] = at
        save(home, rec)
    audit(home, {"action": "expire", "by": by, "via": via, "key_id": kid, "name": rec.get("name"),
                 "owner": rec.get("owner"), "from": before, "expires_at": at, "result": "ok"})
    print("%s expires %s" % (kid, fmt_time(at)))
    return 0


def fmt_time(ms):
    if not isinstance(ms, int):
        return "-"
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _visible(cfg, by, rec):
    return "owner" in roles(cfg, by) or rec.get("owner") == by


def cmd_ls(home, argv):
    o = _opts(argv, ("by", "owner"), ("json", "all"))
    if not o.get("by"):
        raise Bad("ls: --by WHO")
    by, _ = identity(o["by"])
    cfg = fleet.load_fleet()
    want = identity(o["owner"])[0] if o.get("owner") else None
    now, ttl = now_ms(), request_ttl_ms(cfg)
    rows = [view(r, now, ttl) for r in load_all(home) if _visible(cfg, by, r) and (want is None or r.get("owner") == want)]
    if not o.get("all"):
        rows = [r for r in rows if r["status"] in ("active", "pending")]
    rows.sort(key=lambda r: (r.get("created_at") or 0, r["id"]))
    if o.get("json"):
        print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
        return 0
    if not rows:
        print("no keys")
        return 0
    head = ("ID", "NAME", "OWNER", "SCOPES", "CREATED", "EXPIRES", "LAST USED", "STATUS")
    table = [head] + [(r["id"], r["name"] or "", r["owner"] or "", ",".join(r["scopes"]), fmt_time(r["created_at"]),
                       fmt_time(r["expires_at"]), fmt_time(r["last_used_at"]),
                       r["status"] + (" (grace)" if r["status"] == "active" and r["grace_until"] else "")
                       + (" (lapsed)" if r["reason"] == "lapsed" else ""))
                      for r in rows]
    w = [max(len(str(row[i])) for row in table) for i in range(len(head))]
    for row in table:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(row)).rstrip())
    return 0


def cmd_show(home, argv):
    kid, o, by, via = _one(home, argv, "show", ("by",), ("json",))
    rec = load(home, kid)
    if not _visible(fleet.load_fleet(), by, rec):
        audit(home, {"action": "show", "by": by, "via": via, "key_id": kid, "owner": rec.get("owner"),
                     "result": "refused", "reason": "not_authorized"})
        raise Bad("no such key: %s" % kid, 1)   # someone else's key: not even its existence
    v = view(rec, now_ms())
    if o.get("json"):
        print(json.dumps(v, ensure_ascii=False, sort_keys=True))
        return 0
    for k in ("id", "name", "owner", "kind", "scopes", "status", "created_at", "created_by", "approved_by",
              "expires_at", "last_used_at", "rate_per_min", "revoked_at", "revoked_by", "reason",
              "grace_until", "predecessor", "successor"):
        val = v[k]
        if k.endswith("_at") or k == "grace_until":
            val = fmt_time(val)
        elif isinstance(val, list):
            val = ",".join(val)
        if val not in (None, "", "-") or k in ("expires_at", "last_used_at"):
            print("%-13s %s" % (k, val if val is not None else "-"))
    return 0


COMMANDS = {
    "create": lambda h, a: cmd_create(h, a),
    "request": lambda h, a: cmd_create(h, a, pending=True),
    "approve": cmd_approve,
    "ls": cmd_ls,
    "show": cmd_show,
    "revoke": cmd_revoke,
    "rotate": lambda h, a: _reissue(h, a, "rotate"),
    "scopes": lambda h, a: _reissue(h, a, "scopes"),
    "expire": cmd_expire,
}


def usage():
    return (__doc__ % {"scopes": ", ".join(SCOPES), "default": DEFAULT_DAYS, "max": MAX_DAYS,
                       "grace": MAX_GRACE_DAYS, "rate": DEFAULT_RATE, "maxrate": MAX_RATE}).strip()


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(usage())
        return 0 if len(argv) >= 2 else 2
    cmd = argv[1]
    if cmd not in COMMANDS:
        sys.stderr.write("board apikey: unknown command %r (create request approve ls show revoke rotate expire "
                         "scopes)\n" % cmd[:40])
        return 2
    if any(a in ("-h", "--help") for a in argv[2:]):
        print(usage())
        return 0
    try:
        return COMMANDS[cmd](home_dir(), argv[2:])
    except Bad as e:
        sys.stderr.write("board apikey %s: %s\n" % (cmd, e))
        return e.code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
