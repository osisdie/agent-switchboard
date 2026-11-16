"""tickets: read issue-tracker tickets for the board (docs/contract.md, "Tickets").

Read-only in this version: `get` and `doctor`. Writes (comment, transition, assign, take) come
later; the provider interface already names them.

  load(cfg=None)            the configured trackers (fleet.yaml `tickets.trackers`) as a Registry
  Registry.route(ref)       "prov:KEY" (explicit) or KEY matched against every tracker's
                            key_pattern: one match -> it; none -> unknown_ticket_ref (400);
                            several -> ticket_ambiguous (409, with the candidates)
  get(ref, ...)             a ticket, from the cache within its TTL, else from the tracker;
                            every string redacted (the tracker tokens' literal values, then the
                            secret guard) before it is cached, printed, logged or served
  doctor(cfg=None)          per tracker: type, host, key pattern, whether its token is set
                            (set/unset only, never a value), and its problems

Every failure is a TicketError with a generic code (ticket_not_found, ticket_auth,
ticket_rate_limited, ticket_upstream, ticket_unconfigured, unknown_ticket_ref, bad_ticket_ref,
ticket_ambiguous) and a short detail. Upstream bodies never reach a caller: they are clipped,
redacted and written to stderr only.
"""
import json
import os
import re
import sys
import time

LIB = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import fleet  # noqa: E402
import guard  # noqa: E402

DEFAULT_TTL = 60            # seconds a fetched ticket is served from the cache
NOT_FOUND_TTL = 30          # seconds a 404 is remembered
PURGE_AFTER = 7 * 86400     # cache files older than this are removed (unless a link names them)
REF_MAX = 128
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._#-]{0,99}")
TITLE_MAX = 300
EXCERPT_MAX = 500
CATEGORIES = ("todo", "doing", "done", "other")
DEFAULT_CATEGORY = {
    "todo": ("backlog", "todo", "to do", "open", "new", "triage", "unstarted", "selected for development"),
    "doing": ("in progress", "in_progress", "doing", "started", "in review", "review", "in_review", "testing"),
    "done": ("done", "closed", "resolved", "cancelled", "canceled", "completed", "won't do", "wontfix", "duplicate"),
}

STATUS = {
    "unknown_ticket_ref": 400,
    "bad_ticket_ref": 400,
    "ticket_ambiguous": 409,
    "ticket_not_found": 404,
    "ticket_auth": 502,          # the tracker refused our token: not the caller's fault
    "ticket_rate_limited": 429,
    "ticket_upstream": 502,
    "ticket_unconfigured": 503,
}


class TicketError(Exception):
    def __init__(self, code, detail="", retry_after=None, candidates=None):
        super().__init__(code)
        self.code = code
        self.detail = redact(str(detail or ""))[:300]
        self.retry_after = retry_after
        self.candidates = candidates

    @property
    def status(self):
        return STATUS.get(self.code, 502)

    def to_json(self):
        out = {"error": self.code, "detail": self.detail}
        if self.retry_after is not None:
            out["retry_after"] = int(self.retry_after)
        if self.candidates:
            out["candidates"] = list(self.candidates)
        return out


# ------------------------------------------------------------------ redaction
# Literal secret values this process has loaded (tracker tokens), longest first when applied.
# They may come from the process environment, which the secret guard does not read, so every
# outbound string passes through here before guard.scrub.
_SECRETS = {}


def add_secret(name, value):
    if not isinstance(value, str) or len(value) < 4:
        return
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:60] or "secret"
    for form in [value] + guard.variants(value):
        _SECRETS.setdefault(form, label)


def _literal(text):
    for v in sorted(_SECRETS, key=len, reverse=True):
        if v in text:
            text = text.replace(v, "[redacted:%s]" % _SECRETS[v])
    return text


def redact(text, structural=False):
    if not isinstance(text, str) or not text:
        return text
    return guard.scrub(_literal(text), structural=structural)


def redact_tree(obj, key=None):
    if isinstance(obj, str):
        return guard.scrub_tree(_literal(obj), key)
    if isinstance(obj, dict):
        return {k: redact_tree(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_tree(v, key) for v in obj]
    return obj


def warn(msg):
    """stderr only: upstream bodies and reasons that must not reach a response."""
    sys.stderr.write("tickets: %s\n" % redact(fleet.safe_text(msg)))


# ------------------------------------------------------------------ settings
def settings(cfg=None):
    cfg = cfg if cfg is not None else fleet.load_fleet()
    t = cfg.get("tickets") if isinstance(cfg.get("tickets"), dict) else {}

    def num(k, default, lo, hi):
        try:
            return max(lo, min(hi, int(t.get(k, default))))
        except (TypeError, ValueError):
            return default
    trackers = t.get("trackers") if isinstance(t.get("trackers"), list) else []
    return {
        "ttl": num("cache_ttl_seconds", DEFAULT_TTL, 0, 3600),
        "not_found_ttl": num("not_found_ttl_seconds", NOT_FOUND_TTL, 0, 3600),
        "trackers": trackers,
    }


class Registry:
    def __init__(self, providers, cfg_settings):
        self.providers = providers
        self.settings = cfg_settings
        for p in providers:
            for name, value in p.secrets():
                add_secret(name, value)

    def by_name(self, name):
        return next((p for p in self.providers if p.name == name), None)

    def route(self, ref):
        """(provider, key) for a ref. Raises TicketError."""
        ref = str(ref or "")
        if not ref or len(ref) > REF_MAX or fleet.CONTROL_RE.search(ref) or re.search(r"\s", ref):
            raise TicketError("bad_ticket_ref", "a ticket ref is KEY or provider:KEY")
        prov_name, key = (ref.split(":", 1) if ":" in ref else (None, ref))
        if not KEY_RE.fullmatch(key):
            raise TicketError("bad_ticket_ref", "a ticket key is letters, digits and . _ # -")
        if prov_name is not None:
            p = self.by_name(prov_name)
            if p is None:
                raise TicketError("unknown_ticket_ref", "no tracker named %s" % (prov_name if NAME_RE.fullmatch(prov_name) else "that"))
            if not p.matches(key):
                raise TicketError("unknown_ticket_ref", "%s does not match the key_pattern of %s" % (key, p.name))
            return p, key
        hits = [p for p in self.providers if p.matches(key)]
        if not hits:
            raise TicketError("unknown_ticket_ref", "no tracker's key_pattern matches %s" % key)
        if len(hits) > 1:
            raise TicketError("ticket_ambiguous", "%s matches several trackers; name one as provider:KEY" % key,
                              candidates=["%s:%s" % (p.name, key) for p in hits])
        return hits[0], key


def load(cfg=None):
    """The trackers of fleet.yaml `tickets.trackers`. With BOARD_MOCK=1 only `fake` trackers load."""
    from . import providers  # noqa: WPS433 (providers imports this module)
    s = settings(cfg)
    mock = os.environ.get("BOARD_MOCK") == "1"
    out, seen = [], set()
    for i, spec in enumerate(s["trackers"]):
        if not isinstance(spec, dict):
            continue
        p = providers.build(spec, i)
        if mock and p.type != "fake":
            continue
        if p.name in seen:
            p.problems.append("duplicate tracker name %s (only the first is used)" % p.name)
            p.key_re = None
        seen.add(p.name)
        out.append(p)
    return Registry(out, s)


# ------------------------------------------------------------------ normalization
def category(status, mapping):
    st = str(status or "").strip().lower()
    if isinstance(mapping, dict):
        for cat in CATEGORIES:
            vals = mapping.get(cat)
            if isinstance(vals, str):
                vals = [vals]
            if isinstance(vals, list) and st in [str(v).strip().lower() for v in vals]:
                return cat
    for cat, vals in DEFAULT_CATEGORY.items():
        if st in vals:
            return cat
    return "other"


def plain(text, limit):
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    import html  # noqa: WPS433
    text = html.unescape(re.sub(r"<[^>]{0,500}>", " ", text))
    text = fleet.CONTROL_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def assignee(v):
    if v in (None, "", {}, []):
        return None
    if isinstance(v, dict):
        name = v.get("name") or v.get("displayName") or v.get("display_name") or v.get("login") or v.get("email")
        ident = v.get("id") or v.get("accountId") or v.get("account_id")
        if not name and not ident:
            return None
        return {"id": plain(ident, 100) or None, "name": plain(name, 100) or None}
    return {"id": None, "name": plain(v, 100) or None}


def safe_url(u):
    u = str(u or "").strip()
    if not re.match(r"(?i)https?://[^/\s]", u) or fleet.CONTROL_RE.search(u) or any(c in u for c in " <>\"'`"):
        return None
    return u[:500]


def finalize(prov, key, raw, now=None):
    """The ticket as stored and served: fixed fields, plain text, every string redacted."""
    status = plain(raw.get("status"), 80)
    upd = raw.get("updated_at")
    t = {
        "provider": prov.name,
        "key": key,
        "id": plain(raw.get("id") if raw.get("id") is not None else key, 100),
        "title": plain(raw.get("title"), TITLE_MAX),
        "status": status,
        "status_category": category(status, raw.get("status_category_map")),
        "assignee": assignee(raw.get("assignee")),
        "excerpt": plain(raw.get("description"), EXCERPT_MAX),
        "url": safe_url(raw.get("url")),
        "updated_at": upd if isinstance(upd, (int, float)) and not isinstance(upd, bool) else (plain(upd, 64) or None),
        "fetched_at": int((now if now is not None else time.time()) * 1000),
    }
    return redact_tree(t)


# ------------------------------------------------------------------ audit
def audit(home, ev):
    ev = dict(ev)
    ev.setdefault("ts", int(time.time() * 1000))
    ev["type"] = "ticket"
    ev = redact_tree(ev)
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ get
def get(ref, refresh=False, by="local", via="cli", registry=None, home=None):
    """A ticket dict. Raises TicketError. Audited as a `ticket` event (action get)."""
    from . import cache  # noqa: WPS433
    home = home or fleet.ensure_home()
    reg = registry or load()
    ev = {"action": "get", "by": by, "via": via}
    try:
        prov, key = reg.route(ref)
    except TicketError as exc:
        audit(home, dict(ev, ref=str(ref)[:REF_MAX], result=exc.code))
        raise
    ev.update(provider=prov.name, key=key)
    store = cache.Cache(home)
    if not refresh:
        hit = store.get(prov.name, key, reg.settings["ttl"], reg.settings["not_found_ttl"])
        if hit is cache.NOT_FOUND:
            audit(home, dict(ev, result="ticket_not_found", cached=True))
            raise TicketError("ticket_not_found", "%s was not found on %s" % (key, prov.name))
        if hit is not None:
            audit(home, dict(ev, result="cache"))
            return hit
    try:
        raw = prov.get(key, home=home)
    except TicketError as exc:
        if exc.code == "ticket_not_found":
            store.put_not_found(prov.name, key)
        audit(home, dict(ev, result=exc.code))
        raise
    t = finalize(prov, key, raw)
    store.put(prov.name, key, t)
    audit(home, dict(ev, result="ok"))
    return t


def doctor(cfg=None):
    reg = load(cfg)
    return [p.doctor() for p in reg.providers]
