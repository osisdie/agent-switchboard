#!/usr/bin/env python3
"""Pomodoro tallies for the fleet: $BOARD_HOME/pomodoro.json (docs/contract.md, "Pomodoro").

A session that is focused (state `working`) runs its own pomodoro. The collector feeds every run's
session states into step(); nothing here reads a transcript or calls a model (zero tokens).

Timer rules (per session code; FOCUS = pomodoro.focus_minutes, default 25; GRACE =
pomodoro.grace_minutes, default 2):
  working    the pomodoro runs. At FOCUS minutes of focus it completes (the code's tally for the
             local day it completed in goes up by one) and the next one starts at once, so 50
             minutes of work is 2.
  done/rest  the clock stops (pause). A done/rest run longer than GRACE resets the current
             pomodoro to 0:00; a shorter one (between tool calls, a quick turn) keeps its progress.
  needs-you  the clock stops and the progress is kept, however long the human takes: the session
             is in the middle of its work, waiting on the owner.
  offline    resets the current pomodoro (the session ended).
  no data    when no collector run was recorded for longer than GRACE (at least NODATA_MIN_MS: the
             collector was stopped, the machine slept), that gap counts as a break: every current
             pomodoro resets. A collector restarted within that time carries on.
Time between two collector runs counts for the state seen at the first of them. Focused minutes
are split at local midnight (pomodoro.timezone, an IANA name; empty = the system's), and a
pomodoro is counted on the day it completes. 14 days of tallies are kept.

pomodoro.json (0600) is rewritten when something a viewer sees changed (a state, a tally, a reset)
and at least every HEARTBEAT_MS while a pomodoro runs or is paused, so a restarted collector
carries on from it.
Skipping a write loses nothing: the next run integrates again from the stored point.

Notify (off by default): with pomodoro.notify true, every pomodoro.notify_every-th (default 10)
fleet pomodoro of the day sends one info notification ("AI fleet finished its 10th 🍅 today").

Usage:
  pomodoro.py show [today|week] [--json|--mirror]
      today      the fleet total, each session's count, focus and the current pomodoro
      week       the last 7 days
      --json     the board view (week: just those days)
      --mirror   what the operator may write to meta/pomodoro (daily totals only)
"""
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402

FILE = "pomodoro.json"
VERSION = 1
HISTORY_DAYS = 14
HEARTBEAT_MS = 20 * 1000
NODATA_MIN_MS = 60 * 1000  # a gap shorter than this is never "no data" (> HEARTBEAT_MS + a run)
FOCUS = "working"
BREAK = ("done", "rest")
PAUSE = ("needs-you",)
DEFAULTS = {"enabled": True, "focus_minutes": 25, "grace_minutes": 2, "timezone": "",
            "notify": False, "notify_every": 10}
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,127}$")


# ------------------------------------------------------------------ settings and time

def _num(v, default, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return max(lo, min(hi, v))


def settings(cfg):
    """The pomodoro block of fleet.yaml, checked and clamped."""
    c = cfg.get("pomodoro") if isinstance(cfg, dict) and isinstance(cfg.get("pomodoro"), dict) else {}
    tzname = c.get("timezone") if isinstance(c.get("timezone"), str) else ""
    return {
        "enabled": c.get("enabled", True) is not False,
        "focus_ms": int(_num(c.get("focus_minutes"), DEFAULTS["focus_minutes"], 1, 180) * 60000),
        "grace_ms": int(_num(c.get("grace_minutes"), DEFAULTS["grace_minutes"], 0, 60) * 60000),
        "tz": tzname.strip() if tzinfo(tzname.strip()) is not None else "",
        "notify": c.get("notify") is True,
        "notify_every": int(_num(c.get("notify_every"), DEFAULTS["notify_every"], 1, 1000)),
    }


def tzinfo(name):
    """A ZoneInfo for an IANA name, None for "" (the system's local time) or an unknown name."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown name, no tz database: the system's time
        return None


def day_of(ms, tz):
    return dt.datetime.fromtimestamp(ms / 1000.0, tz).date().isoformat()


def next_midnight(ms, tz):
    """Epoch ms of the first local midnight after `ms` (DST-aware)."""
    d = dt.datetime.fromtimestamp(ms / 1000.0, tz).date() + dt.timedelta(days=1)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000)


def days_back(today, n):
    d = dt.date.fromisoformat(today)
    return [(d - dt.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


# ------------------------------------------------------------------ the engine

def empty_doc():
    return {"version": VERSION, "at": None, "sessions": {}, "days": {}, "notified": {}}


def normalize(doc):
    """A clean copy of a stored document (anything malformed is dropped)."""
    out = empty_doc()
    if not isinstance(doc, dict):
        return out
    if isinstance(doc.get("at"), int) and not isinstance(doc.get("at"), bool):
        out["at"] = doc["at"]
    for code, r in (doc.get("sessions") or {}).items() if isinstance(doc.get("sessions"), dict) else ():
        if not isinstance(r, dict) or not CODE_RE.match(str(code)):
            continue
        ok = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
        out["sessions"][code] = {
            "state": r.get("state") if isinstance(r.get("state"), str) else "offline",
            "acc": r["acc"] if ok(r.get("acc")) and r["acc"] >= 0 else 0,
            "away_since": r["away_since"] if ok(r.get("away_since")) else None,
            "done_at": r["done_at"] if ok(r.get("done_at")) else None,
        }
    for day, per in (doc.get("days") or {}).items() if isinstance(doc.get("days"), dict) else ():
        if not isinstance(per, dict) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)):
            continue
        row = {}
        for code, v in per.items():
            if isinstance(v, dict) and CODE_RE.match(str(code)):
                row[code] = {"done": max(0, int(_num(v.get("done"), 0, 0, 10 ** 6))),
                             "focus_ms": max(0, int(_num(v.get("focus_ms"), 0, 0, 10 ** 12)))}
        out["days"][day] = row
    if isinstance(doc.get("notified"), dict):
        out["notified"] = {k: int(v) for k, v in doc["notified"].items()
                           if isinstance(v, int) and not isinstance(v, bool)}
    return out


def _tally(doc, day, code):
    return doc["days"].setdefault(day, {}).setdefault(code, {"done": 0, "focus_ms": 0})


def _focus(doc, code, rec, t0, t1, st, tz, events):
    """Credit [t0, t1) as focus: completions at the exact crossing, split at local midnight."""
    t = t0
    while t < t1:
        end = min(t1, next_midnight(t, tz))
        day = day_of(t, tz)
        need = max(0, st["focus_ms"] - rec["acc"])
        row = _tally(doc, day, code)
        if t + need <= end:
            row["focus_ms"] += need
            row["done"] += 1
            rec["acc"], rec["done_at"] = 0, t + need
            t += need
            events.append({"type": "done", "code": code, "at": t, "day": day})
        else:
            row["focus_ms"] += end - t
            rec["acc"] += end - t
            t = end


def _reset(rec, code, at, events):
    if rec["acc"]:
        events.append({"type": "reset", "code": code, "at": at, "lost_ms": rec["acc"]})
    rec["acc"] = 0


def step(doc, states, now, st):
    """Advance the timers to `now`, given each code's state seen now ({code: state}; a code that is
    missing is offline). Returns (new document, events). Pure: no I/O, `doc` is not changed."""
    doc = normalize(doc)
    tz = tzinfo(st.get("tz") or "")
    grace = st["grace_ms"]
    seen = doc["at"]
    gap = now - seen if seen is not None and now >= seen else None  # None: first run / clock moved back
    events = []
    for code in sorted(set(doc["sessions"]) | {c for c in states if CODE_RE.match(str(c))}):
        rec = doc["sessions"].get(code) or {"state": "offline", "acc": 0, "away_since": None, "done_at": None}
        prev, new = rec["state"], states.get(code) or "offline"
        if gap is not None and gap > max(grace, NODATA_MIN_MS):
            _reset(rec, code, seen, events)             # nothing was seen for longer than the grace
            rec["away_since"] = None
        elif gap is not None:
            if prev == FOCUS:
                _focus(doc, code, rec, seen, now, st, tz, events)
            elif prev in BREAK:
                since = rec["away_since"] if rec["away_since"] is not None else seen
                if now - since > grace:
                    _reset(rec, code, since + grace, events)
            elif prev not in PAUSE:
                _reset(rec, code, seen, events)         # offline (or an unknown state)
        if new in BREAK:
            if not (prev in BREAK and rec["away_since"] is not None):
                rec["away_since"] = now
        else:
            rec["away_since"] = None
        if new not in (FOCUS,) + BREAK + PAUSE:
            _reset(rec, code, now, events)
        rec["state"] = new
        if new == "offline" and not rec["acc"]:
            doc["sessions"].pop(code, None)              # nothing to keep for an idle offline code
        else:
            doc["sessions"][code] = rec
    doc["at"] = now
    today = day_of(now, tz)
    keep = set(days_back(today, HISTORY_DAYS))
    doc["days"] = {d: v for d, v in doc["days"].items() if d in keep}
    return doc, events


def milestone(doc, now, st):
    """(today's fleet total, the milestone just reached or None). Records it in doc["notified"]
    whether or not notify is on, so switching it on mid-day does not replay earlier milestones."""
    today = day_of(now, tzinfo(st.get("tz") or ""))
    total = sum(v["done"] for v in doc["days"].get(today, {}).values())
    every = st["notify_every"]
    reached = (total // every) * every
    last = doc["notified"].get(today, 0)
    doc["notified"] = {today: max(last, reached)}
    return total, (reached if reached > last and reached > 0 else None)


def ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suffix)


def fmt_focus(ms):
    m = int(ms // 60000)
    return "%dh %02dm" % (m // 60, m % 60) if m >= 60 else "%dm" % m


# ------------------------------------------------------------------ persistence (collector)

def read_doc(home):
    try:
        with open(os.path.join(home, FILE), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_doc(home, doc):
    path = os.path.join(home, FILE)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _coarse(doc):
    """What a viewer sees change: states, resets, tallies, completions (not the running clock)."""
    if not isinstance(doc, dict):
        return None
    # away_since only matters while there is progress a long break would reset
    return (json.dumps({c: [r.get("state"), r.get("away_since") if r.get("acc") else None, r.get("done_at"),
                            bool(r.get("acc"))]
                        for c, r in (doc.get("sessions") or {}).items()}, sort_keys=True),
            json.dumps({d: {c: v.get("done") for c, v in per.items()}
                        for d, per in (doc.get("days") or {}).items()}, sort_keys=True),
            json.dumps(doc.get("notified"), sort_keys=True), json.dumps(doc.get("settings"), sort_keys=True))


def run(cfg, home, rows, now, notify_ok=True, send=None):
    """The collector's step: read pomodoro.json, advance it with this run's session states, write it
    when needed, notify a milestone. Returns (doc, events)."""
    st = settings(cfg)
    old = read_doc(home)
    if not st["enabled"]:
        if old is not None:
            try:
                os.remove(os.path.join(home, FILE))
            except OSError:
                pass
        return None, []
    states = {r["code"]: r.get("state") for r in rows if isinstance(r, dict) and r.get("code")}
    doc, events = step(old, states, now, st)
    total, reached = milestone(doc, now, st)
    doc["settings"] = {"focus_ms": st["focus_ms"], "grace_ms": st["grace_ms"], "tz": st["tz"]}
    old_at = old.get("at") if isinstance(old, dict) else None
    # A running or paused pomodoro needs a fresh `at`, or the next run would take the time since the
    # last write for a gap with no data. Nothing in progress: nothing a gap could reset.
    live = any(r["state"] == FOCUS or r["acc"] for r in doc["sessions"].values())
    stale = not isinstance(old_at, int) or (live and not 0 <= now - old_at < HEARTBEAT_MS)
    if _coarse(old) != _coarse(doc) or stale:
        write_doc(home, doc)
    if reached and notify_ok and st["notify"]:
        today = day_of(now, tzinfo(st["tz"]))
        focus = sum(v["focus_ms"] for v in doc["days"].get(today, {}).values())
        if send is None:
            import notify
            send = notify.send
        send("info", "pomodoro:%s:%d" % (today, reached), "AI fleet finished its %s 🍅 today" % ordinal(reached),
             body="%d 🍅 today · %s focus" % (total, fmt_focus(focus)), kind="pomodoro", escalate=False, repeat=False)
    return doc, events


# ------------------------------------------------------------------ views (board-serve, CLI)

def board_view(doc, now):
    """What board-serve gives the page (state.pomodoro): numbers and session codes only. The
    current pomodoro is as of `at`; the page runs the clock on for a `running` one."""
    if not isinstance(doc, dict):
        return None
    s = doc.get("settings") if isinstance(doc.get("settings"), dict) else {}
    st = {"focus_ms": int(_num(s.get("focus_ms"), 25 * 60000, 60000, 180 * 60000)),
          "grace_ms": int(_num(s.get("grace_ms"), 2 * 60000, 0, 3600000)),
          "tz": s.get("tz") if isinstance(s.get("tz"), str) else ""}
    raw = doc
    doc = normalize(doc)
    tz = tzinfo(st["tz"])
    today = day_of(now, tz)
    per = doc["days"].get(today, {})
    sessions = {}
    for code in sorted(set(per) | set(doc["sessions"])):
        r = doc["sessions"].get(code) or {}
        t = per.get(code) or {"done": 0, "focus_ms": 0}
        sessions[code] = {"done": t["done"], "focus_ms": t["focus_ms"], "progress_ms": r.get("acc", 0),
                          "running": r.get("state") == FOCUS, "state": r.get("state") or "offline",
                          "done_at": r.get("done_at")}
    days = []
    for d in days_back(today, HISTORY_DAYS):
        row = doc["days"].get(d, {})
        days.append({"date": d, "done": sum(v["done"] for v in row.values()),
                     "focus_ms": sum(v["focus_ms"] for v in row.values()),
                     "sessions": {c: v["done"] for c, v in sorted(row.items()) if v["done"]}})
    return {"enabled": True, "focus_ms": st["focus_ms"], "grace_ms": st["grace_ms"], "tz": st["tz"],
            "at": raw.get("at") if isinstance(raw.get("at"), int) else None, "today": today,
            "fleet": {"done": days[-1]["done"], "focus_ms": days[-1]["focus_ms"]},
            "sessions": sessions, "days": days}


def mirror_view(view):
    """meta/pomodoro for the Artifact: the daily totals, no running clock."""
    if not view:
        return None
    return {"today": view["today"], "tz": view["tz"], "focus_ms": view["focus_ms"], "fleet": view["fleet"],
            "sessions": {c: {"done": v["done"], "focus_ms": v["focus_ms"]} for c, v in view["sessions"].items()
                         if v["done"] or v["focus_ms"]},
            "days": view["days"], "updated_at": view["at"]}


def _mmss(ms):
    s = int(ms // 1000)
    return "%d:%02d" % (s // 60, s % 60)


def show(argv, home=None, now=None, out=sys.stdout):
    home = home or fleet.board_home()
    now = now if now is not None else int(__import__("time").time() * 1000)
    what = "today"
    fmt = "text"
    for a in argv:
        if a in ("today", "week"):
            what = a
        elif a in ("--json", "--mirror"):
            fmt = a[2:]
        else:
            sys.stderr.write("usage: board pomodoro [today|week] [--json|--mirror]\n")
            return 2
    st = settings(fleet.load_fleet())
    view = board_view(read_doc(home), now) if st["enabled"] else None
    if fmt == "mirror":
        out.write(json.dumps(mirror_view(view), ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    if fmt == "json":
        if view and what == "week":
            view = {"tz": view["tz"], "today": view["today"], "days": view["days"][-7:]}
        out.write(json.dumps(view, ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    if not st["enabled"]:
        out.write("pomodoro: off (fleet.yaml pomodoro.enabled: false)\n")
        return 0
    if not view:
        out.write("pomodoro: no data yet (is board-collect running?)\n")
        return 0
    tzlabel = view["tz"] or "system time"
    if what == "week":
        out.write("Pomodoro, last 7 days (%s)\n" % tzlabel)
        days = view["days"][-7:]
        top = max([d["done"] for d in days] + [1])
        for d in days:
            out.write("  %s  %3d 🍅  %8s  %s\n" % (d["date"], d["done"], fmt_focus(d["focus_ms"]),
                                                  "#" * int(round(20.0 * d["done"] / top))))
        out.write("  total       %3d 🍅  %8s\n" % (sum(d["done"] for d in days),
                                                   fmt_focus(sum(d["focus_ms"] for d in days))))
        return 0
    out.write("Pomodoro %s (%s)\n" % (view["today"], tzlabel))
    out.write("AI fleet: %d 🍅 · %s focus\n" % (view["fleet"]["done"], fmt_focus(view["fleet"]["focus_ms"])))
    for code, v in view["sessions"].items():
        cur = ""
        if v["progress_ms"] or v["running"]:
            cur = "  now %s / %s%s" % (_mmss(v["progress_ms"]), _mmss(view["focus_ms"]),
                                       "" if v["running"] else " (paused)")
        out.write("  %-16s %3d 🍅  %8s%s\n" % (fleet.safe_text(code)[:16], v["done"], fmt_focus(v["focus_ms"]), cur))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    if argv[0] == "show":
        return show(argv[1:])
    sys.stderr.write("pomodoro.py: unknown command %s\n" % argv[0])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
