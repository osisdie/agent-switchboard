#!/usr/bin/env python3
"""Lessons: what the scheduler learned from jams ($BOARD_HOME/lessons.jsonl, 0600, append-only;
docs/contract.md "Lessons"; design: docs/design/resources-and-scheduling.md section 4).

Each line is a full version of one lesson; the last line per id wins, and a tombstone line
({id, deleted: true, by, at}) deletes it. Nothing is ever rewritten.

- Capture (zero tokens): when a jam episode resolves having cost >= lessons.min_lost_minutes
  (minutes x sessions), the collector merges it into the lesson with the same fingerprint (rule +
  signals, never session codes or times), or appends a new `candidate`.
- A candidate becomes `active` once seen lessons.auto_activate_after (2) times. Active lessons whose
  rule matches tighten the schedule (lib/scheduler.py). One unseen and unused for ttl_days (30) is
  retired. At most MAX_ACTIVE are active.
- Rules are declarative, from a fixed vocabulary, and can only tighten:
    {"when": {"all": [{"metric": M, "lt|lte|gt|gte|eq": V}, ...]},
     "then": {"action": "defer_heavy|max_heavy:N|pause_new_starts|serialize_repo|advise:<text>|raise_severity"}}
  Anything else (an unknown key, metric or action) is refused with a message; to undo a lesson,
  retire or delete it.

Usage (through `board lesson ...`; WHO must be an approver for add/edit/activate/retire/delete):
  lessons.py ls [--all] [--status S] [--search TEXT] [--json]
  lessons.py show ID [--json]
  lessons.py add --by WHO --file F        F: JSON {title, rule?, cause?, resolution?}
  lessons.py edit ID --by WHO --file F    F: JSON with any of title, rule, cause, resolution
  lessons.py note ID --by WHO --file F    F: JSON {cause?, resolution?} (the operator's note)
  lessons.py activate|retire|delete ID --by WHO
  lessons.py export [--generic]           active lessons as JSON (--generic: no repo, no sessions)
Exit: 0 ok, 1 unknown lesson, 2 usage or an invalid rule/text (message on stderr), 4 not an
approver, 6 the text asks for a secret (nothing written), 8 the lessons file is busy.
"""
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402

FILE = "lessons.jsonl"
LOCK = "lessons.lock"
SCHEMA_VERSION = 1
MAX_ACTIVE = 100
STATUSES = ("candidate", "active", "retired")
DEFAULTS = {"enabled": True, "min_lost_minutes": 10, "auto_activate_after": 2, "ttl_days": 30}
STAGES = ("plan", "design", "build", "test", "deploy", "production", "maintain")
GLOBAL_METRICS = ("mem_available_mb", "mem_available_pct", "swap_used_pct", "psi_mem", "load_per_cpu",
                  "disk_free_pct", "heavy_running", "five_hour_pct", "five_hour_min_to_reset", "weekly_pct")
TASK_METRICS = ("open_prs_in_repo", "stage")
OPS = ("lt", "lte", "gt", "gte", "eq")
SIMPLE_ACTIONS = ("defer_heavy", "pause_new_starts", "serialize_repo", "raise_severity")
ID_RE = re.compile(r"l-[0-9]+-[0-9a-f]{8}")
CTL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")
# A hint is shown with the schedule: it never reads like an approval, a deploy order or a decision.
BANNED_RE = re.compile(r"approv|deploy|\[BOARD|decision", re.I)
TITLE_MAX, TEXT_MAX, ADVISE_MAX = 120, 400, 160
LOOSEN = ("only tightening actions are allowed (defer_heavy, max_heavy:N, pause_new_starts, serialize_repo, "
          "advise:<text>, raise_severity); a lesson cannot loosen the schedule. To undo one, retire or delete it.")
# Titles of auto-captured lessons, and the default rule some rules get (tighten only).
AUTO = {
    "J1": ("Every session waited on the owner", None),
    "J2": ("A dependency cycle held tasks up", None),
    "J3a": ("Two sessions worked on the same branch", None),
    "J3b": ("Two open PRs in one repo", {"when": {"all": [{"metric": "open_prs_in_repo", "gte": 1}]},
                                         "then": {"action": "serialize_repo"}}),
    "J4": ("Work stalled", None),
    "J5": ("Memory ran out with heavy jobs running", {"when": {"all": [{"metric": "mem_available_pct", "lt": 30}]},
                                                      "then": {"action": "max_heavy:1"}}),
    "J6": ("The operator went silent", None),
    "J7": ("Usage ran low with several sessions working", None),
    "J8": ("A stacked branch was not rebased after a squash merge", None),
}


class Bad(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


def now_ms():
    v = os.environ.get("BOARD_NOW_MS")
    return int(v) if v and v.isdigit() else int(time.time() * 1000)


def settings(cfg):
    c = cfg.get("lessons") if isinstance(cfg, dict) and isinstance(cfg.get("lessons"), dict) else {}

    def n(k, lo, hi):
        v = c.get(k, DEFAULTS[k])
        return int(max(lo, min(hi, v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else DEFAULTS[k]
    return {"enabled": c.get("enabled", True) is not False, "min_lost_minutes": n("min_lost_minutes", 0, 10080),
            "auto_activate_after": n("auto_activate_after", 1, 100), "ttl_ms": n("ttl_days", 1, 3650) * 86400000}


# ------------------------------------------------------------------ rules (pure)
def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_rule(rule):
    """The rule as stored, or raise Bad with a message a person can act on. None = a text lesson."""
    if rule is None:
        return None
    if not isinstance(rule, dict) or set(rule) != {"when", "then"}:
        raise Bad("rule: an object with exactly \"when\" and \"then\" (or null for a text-only lesson)")
    when, then = rule["when"], rule["then"]
    if not isinstance(when, dict) or set(when) != {"all"} or not isinstance(when["all"], list) \
            or not 1 <= len(when["all"]) <= 6:
        raise Bad("rule.when: {\"all\": [1 to 6 conditions]}")
    conds = []
    for c in when["all"]:
        if not isinstance(c, dict) or "metric" not in c or len(c) != 2:
            raise Bad("rule.when: each condition is {\"metric\": M, \"<op>\": value} with one op of %s" % ", ".join(OPS))
        m = c["metric"]
        op = next(k for k in c if k != "metric")
        if m not in GLOBAL_METRICS + TASK_METRICS:
            raise Bad("rule.when: unknown metric %r (one of: %s)" % (str(m)[:40], ", ".join(GLOBAL_METRICS + TASK_METRICS)))
        if op not in OPS:
            raise Bad("rule.when: unknown operator %r (one of: %s)" % (str(op)[:20], ", ".join(OPS)))
        v = c[op]
        if m == "stage":
            if op != "eq" or v not in STAGES:
                raise Bad("rule.when: stage takes eq and one of: %s" % " ".join(STAGES))
        elif not _finite(v):
            raise Bad("rule.when: %s needs a number" % m)
        conds.append({"metric": m, op: v})
    if not isinstance(then, dict) or not isinstance(then.get("action"), str) or not set(then) <= {"action", "reason"}:
        raise Bad("rule.then: {\"action\": A} (optional \"reason\"); " + LOOSEN)
    a = then["action"]
    if a.startswith("max_heavy:"):
        if not re.fullmatch(r"max_heavy:[0-8]", a):
            raise Bad("rule.then: max_heavy:N takes N from 0 to 8 (the scheduler keeps the lower of N and its own limit)")
    elif a.startswith("advise:"):
        text = _text(a[len("advise:"):], "rule.then advise", ADVISE_MAX) or ""
        if not text:
            raise Bad("rule.then: advise:<text> takes 1 to %d characters" % ADVISE_MAX)
        if BANNED_RE.search(text):
            raise Bad("rule.then: a hint may not mention approvals, deploys, decisions or [BOARD; "
                      "it orders work, it never authorizes anything")
        a = "advise:" + text
    elif a not in SIMPLE_ACTIONS:
        raise Bad("rule.then: unknown action %r: %s" % (a[:40], LOOSEN))
    out = {"when": {"all": conds}, "then": {"action": a}}
    if isinstance(then.get("reason"), str) and then["reason"].strip():
        out["then"]["reason"] = _text(then["reason"], "rule.then reason", TEXT_MAX)
    return out


def holds(rule, metrics):
    """Does the rule's `when` hold for these metrics? A metric that is not known never holds."""
    for c in rule["when"]["all"]:
        v = metrics.get(c["metric"])
        op = next(k for k in c if k != "metric")
        if v is None or (c["metric"] != "stage" and not _finite(v)):
            return False
        want = c[op]
        ok = {"lt": lambda: v < want, "lte": lambda: v <= want, "gt": lambda: v > want,
              "gte": lambda: v >= want, "eq": lambda: v == want}[op]()
        if not ok:
            return False
    return True


def task_only(rule):
    return any(c["metric"] in TASK_METRICS for c in rule["when"]["all"])


def fingerprint(rule_kind, signals):
    return hashlib.sha1(("%s|%s" % (rule_kind, json.dumps(signals or {}, sort_keys=True))).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ the file
def path(home):
    return os.path.join(home, FILE)


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
                    raise Bad("the lessons file is busy; try again", 8) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def load(home, with_deleted=False):
    """{id: the last version}. Unreadable lines are skipped."""
    out = {}
    try:
        with open(path(home), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("id"), str) and ID_RE.fullmatch(rec["id"]):
                    out[rec["id"]] = rec
    except OSError:
        pass
    return out if with_deleted else {k: v for k, v in out.items() if not v.get("deleted")}


def append(home, recs):
    fd = os.open(path(home), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(path(home), 0o600)


def new_id(now):
    return "l-%d-%s" % (now // 1000, os.urandom(4).hex())


def version(rec, now, by, **changes):
    r = dict(rec)
    r.update(changes)
    r.update({"schema_version": SCHEMA_VERSION, "updated_at": now, "updated_by": by})
    return r


def active_n(recs):
    return sum(1 for r in recs.values() if r.get("status") == "active")


# ------------------------------------------------------------------ the collector's side
def capture(home, resolved, cfg, now):
    """Merge resolved jam episodes into lessons. Returns the ids of new candidates."""
    st = settings(cfg)
    if not st["enabled"]:
        return []
    new, lines = [], []
    with locked(home, 2.0):
        everything = load(home, with_deleted=True)
        recs = {k: v for k, v in everything.items() if not v.get("deleted")}
        forgotten = {r.get("fingerprint") for r in everything.values() if r.get("deleted") and r.get("fingerprint")}
        by_fp = {r.get("fingerprint"): r for r in recs.values() if r.get("fingerprint")}
        for ep in resolved:
            if not isinstance(ep, dict) or int(ep.get("lost_min") or 0) < st["min_lost_minutes"] or ep.get("rule") not in AUTO:
                continue
            signals = ep.get("signals") if isinstance(ep.get("signals"), dict) else {}
            fp = fingerprint(ep["rule"], signals)
            if fp in forgotten:
                continue                               # deleted by an approver: never re-learned
            cur = by_fp.get(fp)
            if cur:
                occ = int(cur.get("occurrences") or 0) + 1
                status = cur.get("status")
                if status == "candidate" and occ >= st["auto_activate_after"] and active_n(recs) < MAX_ACTIVE:
                    status = "active"
                rec = version(cur, now, "auto", occurrences=occ, last_seen=now, status=status,
                              lost_min_total=int(cur.get("lost_min_total") or 0) + int(ep["lost_min"]))
            else:
                title, rule = AUTO[ep["rule"]]
                rec = version({"id": new_id(now), "fingerprint": fp, "rule_kind": ep["rule"], "source": "auto",
                               "title": title, "signals": signals, "cause": None, "resolution": None,
                               "occurrences": 1, "lost_min_total": int(ep["lost_min"]), "first_seen": now,
                               "last_seen": now, "rule": rule, "hits": 0, "last_hit_at": None,
                               "status": "active" if st["auto_activate_after"] <= 1 else "candidate"}, now, "auto")
                new.append(rec["id"])
            recs[rec["id"]] = by_fp[fp] = rec
            lines.append(rec)
        if lines:
            append(home, lines)
    return new


def maintain(home, cfg, hits, now):
    """Count the rules that started to match (`hits`: ids) and retire stale active lessons."""
    st = settings(cfg)
    lines = []
    with locked(home, 2.0):
        recs = load(home)
        for i in sorted(hits):
            if i in recs:
                recs[i] = version(recs[i], now, "auto", hits=int(recs[i].get("hits") or 0) + 1, last_hit_at=now)
                lines.append(recs[i])
        for r in recs.values():
            seen = max(int(r.get("last_seen") or 0), int(r.get("last_hit_at") or 0), int(r.get("updated_at") or 0))
            if r.get("status") == "active" and r.get("source") == "auto" and now - seen > st["ttl_ms"]:
                lines.append(version(r, now, "auto", status="retired", retired_by="ttl"))
        if lines:
            append(home, lines)


def active_rules(home):
    """[{id, rule}] of active lessons with a rule, for decide()."""
    return [{"id": r["id"], "rule": r["rule"]} for r in sorted(load(home).values(), key=lambda r: r["id"])
            if r.get("status") == "active" and isinstance(r.get("rule"), dict)]


def board_view(home, n=100):
    recs = sorted(load(home).values(), key=lambda r: (-int(r.get("last_seen") or r.get("updated_at") or 0), r["id"]))
    keep = ("id", "status", "source", "rule_kind", "title", "cause", "resolution", "occurrences", "lost_min_total",
            "first_seen", "last_seen", "rule", "hits", "last_hit_at", "updated_at", "updated_by", "retired_by")
    return [{k: r.get(k) for k in keep} for r in recs[:n]]


# ------------------------------------------------------------------ the CLI
def _text(v, field, cap):
    """Free text from a person: one line, capped, refused (exit 6) when it asks for a secret."""
    if v is None:
        return None
    if not isinstance(v, str):
        raise Bad("%s: a string" % field)
    t = " ".join(CTL_RE.sub(" ", v).split())
    if len(t) > cap:
        raise Bad("%s: at most %d characters" % (field, cap))
    import guard
    if guard.screen(t):
        raise Bad("%s: refused by the secret guard (it asks for a secret value); nothing written" % field, 6)
    return guard.scrub(t) or None


def _read_file(p):
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.loads(fh.read(64 * 1024))
    except (OSError, ValueError):
        raise Bad("--file: a readable JSON object") from None
    if not isinstance(data, dict):
        raise Bad("--file: a JSON object")
    return data


def _approver(by):
    who = by[len("board:"):] if by.startswith("board:") else by
    approvers = fleet.id_list(fleet.load_fleet(), "approvers")
    if not approvers or who not in approvers:
        raise Bad("--by %s is not an approver (fleet.yaml approvers); nothing written" % who[:60], 4)


def _fields(data, allowed, home):
    extra = set(data) - set(allowed)
    if extra:
        raise Bad("unknown field(s): %s (allowed: %s)" % (", ".join(sorted(extra))[:80], ", ".join(allowed)))
    out = {}
    for k in allowed:
        if k not in data:
            continue
        if k == "rule":
            out[k] = validate_rule(data[k])
        else:
            out[k] = _text(data[k], k, TITLE_MAX if k == "title" else TEXT_MAX)
    return out


def _opts(argv, names):
    out, i = {}, 0
    while i < len(argv):
        k = argv[i]
        if not k.startswith("--") or k[2:] not in names or i + 1 >= len(argv) or k[2:] in out:
            raise Bad("unknown, repeated or incomplete option: %s" % k[:40])
        out[k[2:]] = argv[i + 1]
        i += 2
    return out


def _log(home, ev):
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def _one(recs, lid):
    if not isinstance(lid, str) or not ID_RE.fullmatch(lid) or lid not in recs:
        raise Bad("no such lesson: %s" % str(lid)[:40], 1)
    return recs[lid]


def cmd_change(home, verb, argv):
    """add / edit / note / activate / retire / delete: one appended line under the lock."""
    lid = None
    if verb != "add":
        if not argv or argv[0].startswith("--"):
            raise Bad("%s: the lesson id first" % verb)
        lid, argv = argv[0], argv[1:]
    o = _opts(argv, ("by", "file") if verb in ("add", "edit", "note") else ("by",))
    if not o.get("by") or (verb in ("add", "edit", "note") and not o.get("file")):
        raise Bad("%s: --by WHO%s" % (verb, " --file F" if verb in ("add", "edit", "note") else ""))
    by = o["by"]
    if verb != "note":
        _approver(by)
    data = _read_file(o["file"]) if o.get("file") else {}
    now = now_ms()
    with locked(home):
        recs = load(home)
        if verb == "add":
            f = _fields(data, ("title", "rule", "cause", "resolution"), home)
            if not f.get("title"):
                raise Bad("add: a title is required")
            rec = version({"id": new_id(now), "fingerprint": None, "rule_kind": None, "source": "owner",
                           "signals": {}, "occurrences": 0, "lost_min_total": 0, "first_seen": now, "last_seen": now,
                           "hits": 0, "last_hit_at": None, "status": "active", "rule": None, "cause": None,
                           "resolution": None, **f}, now, by)
        else:
            cur = _one(recs, lid)
            if verb == "edit":
                f = _fields(data, ("title", "rule", "cause", "resolution"), home)
                if not f:
                    raise Bad("edit: nothing to change")
                if "title" in f and not f["title"]:
                    raise Bad("edit: the title cannot be empty")
                rec = version(cur, now, by, **f)
            elif verb == "note":
                if cur.get("source") != "auto":
                    raise Bad("note: only for lessons the scheduler learned; approvers edit their own with board lesson edit")
                f = _fields(data, ("cause", "resolution"), home)
                if not f:
                    raise Bad("note: cause and/or resolution")
                rec = version(cur, now, by, **f)
            elif verb == "activate":
                if cur.get("status") != "active" and active_n(recs) >= MAX_ACTIVE:
                    raise Bad("activate: %d lessons are active already; retire one first" % MAX_ACTIVE)
                rec = version(cur, now, by, status="active", retired_by=None)
            elif verb == "retire":
                rec = version(cur, now, by, status="retired", retired_by=by)
            else:
                # delete = forget: the fingerprint stays, so capture never learns this pattern again
                rec = {"id": cur["id"], "deleted": True, "by": by, "at": now, "title": cur.get("title"),
                       "fingerprint": cur.get("fingerprint"), "schema_version": SCHEMA_VERSION}
        append(home, [rec])
    _log(home, {"ts": now, "type": "lesson", "action": verb, "id": rec["id"], "by": by})
    print("%s %s" % (rec["id"], {"add": "added", "edit": "edited", "note": "noted", "activate": "active",
                                 "retire": "retired", "delete": "deleted"}[verb]))
    return 0


def cmd_ls(home, argv):
    o, flags = {}, set()
    i = 0
    while i < len(argv):
        if argv[i] in ("--all", "--json"):
            flags.add(argv[i])
            i += 1
        elif argv[i] in ("--status", "--search") and i + 1 < len(argv):
            o[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            raise Bad("ls: [--all] [--status S] [--search TEXT] [--json]")
    rows = board_view(home, n=10 ** 6)
    if o.get("status"):
        rows = [r for r in rows if r["status"] == o["status"]]
    elif "--all" not in flags:
        rows = [r for r in rows if r["status"] != "retired"]
    q = (o.get("search") or "").lower()
    if q:
        rows = [r for r in rows if q in json.dumps([r.get("title"), r.get("cause"), r.get("resolution"), r.get("rule"),
                                                     r.get("rule_kind")], ensure_ascii=False).lower()]
    if "--json" in flags:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("no lessons")
    for r in rows:
        act = ((r.get("rule") or {}).get("then") or {}).get("action") or "-"
        line = "%-22s %-9s %-6s %3sx %5s min  %-18s %s" % (r["id"], r["status"], r.get("rule_kind") or r.get("source"),
                                                          r.get("occurrences"), r.get("lost_min_total"), act[:18], r.get("title"))
        print(CTL_RE.sub("?", line))                  # the file may have been edited by hand
    return 0


def cmd_export(home, argv):
    generic = "--generic" in argv
    out = []
    for r in board_view(home, n=10 ** 6):
        if r["status"] != "active":
            continue
        e = {k: r.get(k) for k in ("title", "rule_kind", "rule", "cause", "resolution", "occurrences")}
        if not generic:
            e["id"] = r["id"]
        out.append(e)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    home = fleet.ensure_home()
    verb, rest = argv[0], argv[1:]
    try:
        if verb == "ls":
            return cmd_ls(home, rest)
        if verb == "show":
            if not rest:
                raise Bad("show ID [--json]")
            r = _one(load(home), rest[0])
            print(json.dumps(r, ensure_ascii=True, indent=1))      # control characters stay escaped
            return 0
        if verb == "export":
            return cmd_export(home, rest)
        if verb in ("add", "edit", "note", "activate", "retire", "delete"):
            return cmd_change(home, verb, rest)
        raise Bad("unknown command: %s" % verb[:40])
    except Bad as exc:
        sys.stderr.write("board: lesson %s: %s\n" % (verb, exc))
        return exc.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
