#!/usr/bin/env python3
"""asker_route: who answers askers (docs/contract.md, "Asker routing"): the local desk or the Claude
sessions depending on hosted-model usage, and what happens to askers' requests while the owner is away.

By default askers are answered by the local asker desk (lib/desk.py) whenever it is available. The
owner may choose instead that while hosted-model usage is low, askers are answered the usual way:
the operator triages and routes their messages to the Claude sessions, as before the desk existed.

  $BOARD_HOME/asker-route.json        {mode: "local"|"claude_when_ample", five_hour_below, seven_day_below,
                                       away_askers: "committee"|"decline"|"wait", by, at}
                                      (the owner's settings; 0600; flock asker-route.lock)
  $BOARD_HOME/asker-route-state.json  {effective: "local"|"claude", since, why}   (the hysteresis memory)

The effective route is "claude" only when ALL of these hold, else "local":
  - mode is claude_when_ample;
  - do-not-disturb is off (askers are declined then anyway; the desk keeps answering them);
  - no active usage-limit episode (lib/availability.py limit_active);
  - usage.json is fresh (the 15-minute rule of compute_low) and has both windows' pct (a window
    past its resets_at counts as 0): stale or missing usage stays local (save quota);
  - hysteresis: from local, both 5h <= five_hour_below - 5 and 7d <= seven_day_below - 5; once on
    claude, it stays while 5h < five_hour_below and 7d < seven_day_below.

lib/messages.py `_to_desk` asks `claude_now()`: when it says yes the desk is skipped and the message
takes the pre-desk way (status "new" for the operator, or declined on compute-low), with no
desk_forward card. Each flip of the effective route is audited ({type: "asker_route", action: "switch"}).

While the owner is away ($BOARD_HOME/owner-away), away_askers says what happens to askers:
  committee (default)  no owner card: an asker's work request goes the usual way (status "new", the
                       operator routes it to sessions, which handle it under the away/consensus
                       rules); open desk_forward cards are settled the same way (lib/desk.py
                       settle_away). Plain questions are answered as usual.
  decline              askers' messages and work requests get a canned reply (reason owner_away,
                       lib/availability.py), never relayed, not even to the desk.
  wait                 the cards wait for the owner (as before this setting).

CLI (bin/board askers route|away ...):
  asker_route.py local --by WHO                       askers stay on the local model (default)
  asker_route.py ample [--5h N] [--7d M] --by WHO     Claude sessions while usage is below N% / M%
  asker_route.py status [--json]                      the setting and the effective route now
  asker_route.py away committee|decline|wait --by WHO  askers' requests while the owner is away
Exit codes: 0 ok, 1 error, 2 usage.
"""
import fcntl
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import availability  # noqa: E402
import fleet  # noqa: E402

FILE = "asker-route.json"
STATE_FILE = "asker-route-state.json"
LOCK_FILE = "asker-route.lock"
MODES = ("local", "claude_when_ample")
AWAY_MODES = ("committee", "decline", "wait")
AWAY_DEFAULT = "committee"
EFFECTIVE = ("local", "claude")
DEFAULTS = {"five_hour_below": 50, "seven_day_below": 70}
HYSTERESIS = 5
WHY = ("off", "dnd", "limit", "compute_low", "no_usage", "stale", "above", "ample")
BY_RE = availability.BY_RE
LOCK_TIMEOUT_S = 5


class RouteError(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


def now_ms():
    return int(time.time() * 1000)


def valid_pct(v):
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 99


def settings(home):
    """The owner's settings; anything missing or invalid falls back to local and the defaults.
    away_askers is committee (the default) only when the file is absent (or written without it): a
    file that can't be read, or an invalid value, is "wait" (nothing goes anywhere without the
    owner), with away_invalid True so the owner sees it."""
    path = os.path.join(home, FILE)
    present = os.path.lexists(path)
    raw = availability._read_json(path)
    rec = raw or {}
    away = rec.get("away_askers")
    invalid = present and (raw is None or (away is not None and away not in AWAY_MODES))
    out = {"mode": rec.get("mode") if rec.get("mode") in MODES else "local",
           "away_askers": "wait" if invalid else away if away in AWAY_MODES else AWAY_DEFAULT,
           "away_invalid": bool(invalid)}
    for k, d in DEFAULTS.items():
        out[k] = rec[k] if valid_pct(rec.get(k)) else d
    by = rec.get("by")
    out["by"] = by if isinstance(by, str) and BY_RE.fullmatch(by) else None
    at = availability._num(rec.get("at"))
    out["at"] = int(at) if at else None
    return out


def _state(home):
    rec = availability._read_json(os.path.join(home, STATE_FILE)) or {}
    eff = rec.get("effective") if rec.get("effective") in EFFECTIVE else "local"
    since = availability._num(rec.get("since"))
    return {"effective": eff, "since": int(since) if since else None}


def _window(u, key, now):
    w = u.get(key) if isinstance(u.get(key), dict) else {}
    pct, resets = availability._num(w.get("pct")), availability._num(w.get("resets_at"))
    if pct is None:
        return None
    return 0 if resets is not None and resets <= now else pct


def evaluate(home, current="local", now=None, cfg=None):
    """(effective, why, detail): what the route is now, given the CURRENT effective route
    (hysteresis). detail (or None) says which window decided: {window, pct, level, rule} with rule
    "at_or_above" (on Claude: at or above the level) or "margin" (from local: not yet level - 5 or
    lower), or {updated_at} for stale usage."""
    now = now_ms() if now is None else now
    s = settings(home)
    if s["mode"] != "claude_when_ample":
        return "local", "off", None
    if os.path.lexists(os.path.join(home, availability.DND_FILE)):
        return "local", "dnd", None
    if availability.limit_active(home, now):
        return "local", "limit", None
    raw = availability._read_json(os.path.join(home, "usage.json"))
    u = availability.fresh_usage(home, now)
    if not u:
        at = availability._num(raw.get("updated_at")) if raw else None
        return ("local", "stale", {"updated_at": int(at)}) if at else ("local", "no_usage", None)
    p5, p7 = _window(u, "five_hour", now), _window(u, "seven_day", now)
    if p5 is None or p7 is None:
        return "local", "no_usage", None
    n5, n7 = s["five_hour_below"], s["seven_day_below"]
    for name, pct, level in (("5h", p5, n5), ("7d", p7, n7)):
        if current == "claude" and pct >= level:
            return "local", "above", {"window": name, "pct": pct, "level": level, "rule": "at_or_above"}
        if current != "claude" and pct > level - HYSTERESIS:
            return "local", "above", {"window": name, "pct": pct, "level": level - HYSTERESIS, "rule": "margin"}
    # below the owner's levels, but compute-low (its own thresholds) declines askers: keep the desk
    cfg = fleet.load_fleet() if cfg is None else cfg
    if availability.compute_low(home, cfg, now):
        return "local", "compute_low", None
    return "claude", "ample", {"five_hour": p5, "seven_day": p7}


def _audit(home, ev):
    availability._audit(home, ev)


def _write(path, rec):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


class _Lock:
    def __init__(self, home):
        self.path = os.path.join(home, LOCK_FILE)
        self.fh = None

    def __enter__(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.fh = os.fdopen(fd, "r+")
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() > deadline:
                    self.fh.close()
                    raise RouteError("busy", "asker-route.lock is held")
                time.sleep(0.02)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


def effective(home, now=None, commit=True):
    """{effective, why, since}: the route now. With commit, a flip is remembered (the hysteresis)
    and audited. Never raises: any error is local (the desk, today's default)."""
    now = now_ms() if now is None else now
    try:
        cur = _state(home)
        eff, why, det = evaluate(home, cur["effective"], now)
        if eff == cur["effective"]:
            return {"effective": eff, "why": why, "detail": det, "since": cur["since"]}
        if not commit:
            return {"effective": eff, "why": why, "detail": det, "since": now}
        with _Lock(home):
            cur = _state(home)      # someone else may have flipped it meanwhile
            eff, why, det = evaluate(home, cur["effective"], now)
            if eff != cur["effective"]:
                _write(os.path.join(home, STATE_FILE), {"effective": eff, "since": now, "why": why})
                _audit(home, {"type": "asker_route", "action": "switch", "effective": eff, "why": why,
                              "previous": cur["effective"]})
                return {"effective": eff, "why": why, "detail": det, "since": now}
            return {"effective": eff, "why": why, "detail": det, "since": cur["since"]}
    except Exception:  # noqa: BLE001 - routing never fails on this: the desk (default) then
        return {"effective": "local", "why": "off", "detail": None, "since": None}


def claude_now(home, now=None):
    """Should an asker's message skip the local desk and go the usual way (Claude sessions)?"""
    return effective(home, now)["effective"] == "claude"


def is_away(home):
    return os.path.lexists(os.path.join(home, "owner-away"))


def away_mode(home):
    """None while the owner is not away; else what happens to askers' requests: committee | decline | wait."""
    if not is_away(home):
        return None
    try:
        return settings(home)["away_askers"]
    except Exception:  # noqa: BLE001 - unreadable: nothing goes anywhere without the owner
        return "wait"


def view(home, owner=False, now=None):
    """The board's `asker_route`: {effective, away} for everyone (the asker composer note: who
    answers, and what the owner's absence means); owners also get the settings, why and detail."""
    e = effective(home, now)
    out = {"effective": e["effective"], "away": away_mode(home)}
    if owner:
        s = settings(home)
        out.update({"mode": s["mode"], "five_hour_below": s["five_hour_below"],
                    "seven_day_below": s["seven_day_below"], "away_askers": s["away_askers"],
                    "away_invalid": s["away_invalid"],
                    "hysteresis": HYSTERESIS, "why": e["why"], "detail": e["detail"], "since": e["since"],
                    "by": s["by"], "at": s["at"]})
    return out


def set_route(home, mode, by, five_hour_below=None, seven_day_below=None, away_askers=None):
    """The owner's settings (None keeps a value). Returns (changed, settings). Raises RouteError on
    bad input. Audited."""
    if mode is not None and mode not in MODES:
        raise RouteError("bad_mode", "mode must be \"local\" or \"claude_when_ample\"")
    if away_askers is not None and away_askers not in AWAY_MODES:
        raise RouteError("bad_away", "away_askers must be \"committee\", \"decline\" or \"wait\"")
    if not isinstance(by, str) or not BY_RE.fullmatch(by):
        raise RouteError("bad_by", "by must be a session code or board:<identity>")
    for name, v in (("five_hour_below", five_hour_below), ("seven_day_below", seven_day_below)):
        if v is not None and not valid_pct(v):
            raise RouteError("bad_pct", "%s must be an integer from 1 to 99" % name)
    given = {"mode": mode, "five_hour_below": five_hour_below, "seven_day_below": seven_day_below,
             "away_askers": away_askers}
    with _Lock(home):
        cur = settings(home)
        new = {k: (given[k] if given[k] is not None else cur[k]) for k in given}
        changed = any(new[k] != cur[k] for k in new) or cur["away_invalid"] or \
            not os.path.exists(os.path.join(home, FILE))
        if changed:
            _write(os.path.join(home, FILE), dict(new, by=by, at=now_ms()))
            _audit(home, dict({"type": "asker_route", "action": "set", "by": by}, **new))
    effective(home)   # a switch to local takes effect (and is audited) right away
    return changed, settings(home)


# ------------------------------------------------------------------ CLI
USAGE = ("usage: board askers route local --by WHO\n"
         "       board askers route ample [--5h N] [--7d M] --by WHO\n"
         "       board askers route status [--json]\n"
         "       board askers away committee|decline|wait --by WHO")
PCT_RE = re.compile(r"[0-9]{1,2}")


def main(argv):
    args = argv[1:]
    if args and args[0] == "away":      # `board askers away committee|decline|wait --by WHO`
        return away_main(args[1:])
    if args and args[0] == "route":     # `board askers route ...`
        args = args[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        print("  who answers askers: the local model (default), or, while hosted-model usage is below\n"
              "  both thresholds (defaults 5h 50%, 7d 70%; switching back needs 5 points of margin), the\n"
              "  Claude sessions through the operator as before the local desk.")
        return 0 if args else 2
    sub, rest = args[0], args[1:]
    home = fleet.ensure_home()
    if sub == "status":
        if rest not in ([], ["--json"]):
            sys.stderr.write(USAGE + "\n")
            return 2
        v = view(home, owner=True)
        if rest:
            print(json.dumps(v, ensure_ascii=False))
        else:
            print("askers answered by: %s (mode %s; 5h below %d%%, 7d below %d%%; why %s); "
                  "while the owner is away: %s%s" % (
                      "Claude sessions" if v["effective"] == "claude" else "local model", v["mode"],
                      v["five_hour_below"], v["seven_day_below"], v["why"], v["away_askers"],
                      " (away now)" if v["away"] else ""))
        return 0
    if sub not in ("local", "ample"):
        sys.stderr.write(USAGE + "\n")
        return 2
    by, n5, n7, i = None, None, None, 0
    while i < len(rest):
        a = rest[i]
        if a in ("--by", "--5h", "--7d") and i + 1 < len(rest):
            v = rest[i + 1]
            if a == "--by":
                by = v
            else:
                if not PCT_RE.fullmatch(v) or not 1 <= int(v) <= 99:
                    sys.stderr.write("board askers route: %s must be an integer from 1 to 99\n" % a)
                    return 2
                if a == "--5h":
                    n5 = int(v)
                else:
                    n7 = int(v)
            i += 2
            continue
        sys.stderr.write("board askers route: %s: unknown option %s\n%s\n" % (sub, a, USAGE))
        return 2
    if not by:
        sys.stderr.write(USAGE + "\n")
        return 2
    try:
        changed, s = set_route(home, "local" if sub == "local" else "claude_when_ample", by, n5, n7)
    except RouteError as exc:
        sys.stderr.write("board askers route: %s\n" % exc.detail)
        return 2 if exc.code.startswith("bad_") else 1
    if s["mode"] == "local":
        print("askers answered by the local model%s" % ("" if changed else " (unchanged)"))
    else:
        print("askers answered by Claude sessions while 5h < %d%% and 7d < %d%%%s" % (
            s["five_hour_below"], s["seven_day_below"], "" if changed else " (unchanged)"))
    return 0


def away_main(rest):
    if not rest or rest[0] in ("-h", "--help", "help"):
        print(USAGE)
        print("  while the owner is away: committee (default; sessions take askers' work requests, no owner\n"
              "  card), decline (a canned reply, nothing relayed), wait (cards wait for the owner)")
        return 0 if rest else 2
    mode, by = rest[0], None
    if mode not in AWAY_MODES:
        sys.stderr.write(USAGE + "\n")
        return 2
    if rest[1:2] == ["--by"] and len(rest) == 3:
        by = rest[2]
    if not by:
        sys.stderr.write(USAGE + "\n")
        return 2
    try:
        changed, _s = set_route(fleet.ensure_home(), None, by, away_askers=mode)
    except RouteError as exc:
        sys.stderr.write("board askers away: %s\n" % exc.detail)
        return 2 if exc.code.startswith("bad_") else 1
    print("while the owner is away, askers' requests: %s%s" % (mode, "" if changed else " (unchanged)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
