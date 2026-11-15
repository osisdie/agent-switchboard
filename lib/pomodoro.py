#!/usr/bin/env python3
"""Pomodoro tallies for the fleet: $BOARD_HOME/pomodoro.json (docs/contract.md, "Pomodoro").

Every focused stream runs its own pomodoro. A stream is a top-level session (key = its code,
focused while its state is `working`) or one of its teammates (key "CODE/teammate-name", focused
while either signal says it works: resources.json reports it active (not idle, idle_s below
pomodoro.teammate_idle_s, default 60), or its pane title shows the working spinner (cards.json
`busy`, which stays true through a long model turn at ~0% CPU)). A lead's stream counts only its own state, never its teammates' work. The collector feeds
every run's states into step(); nothing here reads a transcript or calls a model (zero tokens).

Timer rules (per stream; FOCUS = pomodoro.focus_minutes, default 25; GRACE =
pomodoro.grace_minutes, default 2; MODE = pomodoro.mode, "accumulate" (default) or "strict"):
  working    the pomodoro runs. At FOCUS minutes of focus it completes (the stream's tally for the
             local day it completed in goes up by one) and the next one starts at once with the
             remainder, so 50 minutes of work is 2.
  done/rest  the clock stops (pause). accumulate: the progress is kept, however long the break
             (agents pause between prompts all the time). strict (human pomodoro): a done/rest run
             longer than GRACE resets the current pomodoro to 0:00; a shorter one keeps it.
  needs-you  the clock stops and the progress is kept, however long the human takes: the session
             is in the middle of its work, waiting on the owner.
  gone       a teammate stream with no data (the teammate ended, or resources.json is missing or
             stale): the clock stops and the progress is kept. A gone stream with no progress is
             dropped; one with progress is dropped after HISTORY_DAYS.
  offline    resets the current pomodoro (the session ended).
  no data    when no collector run was recorded for longer than nodata_ms() (GRACE, at least 60 s
             and at least HEARTBEAT_MS + 2 poll intervals: the collector was stopped, the machine
             slept), that gap counts as a break: strict resets every current pomodoro, accumulate
             keeps them (the gap itself is never credited). A collector restarted within that time
             (less the HEARTBEAT_MS a write may lag) carries on.
Time between two collector runs counts for the state seen at the first of them. Focused minutes
are split at local midnight (pomodoro.timezone, an IANA name; empty = the system's), and a
pomodoro is counted on the day it completes. Day tallies are per stream; the fleet's day is the
sum over all streams, teammates included. 14 days of tallies are kept (a day later than today,
after the clock moved back, is kept too).

pomodoro.json (0600) is rewritten when something a viewer sees changed (a state, a tally, a reset)
and at least every HEARTBEAT_MS while a pomodoro runs or is paused, so a restarted collector
carries on from it.
Skipping a write loses nothing: the next run integrates again from the stored point.

Notify (off by default): with pomodoro.notify true, every pomodoro.notify_every-th (default 10)
fleet pomodoro of the day sends one info notification ("AI fleet finished its 10th 🍅 today").

Usage:
  pomodoro.py show [today|week] [--json|--mirror]
      today      the fleet total, each stream's count, focus and the current pomodoro
      week       the last 7 days
      --json     today's tallies as of the last collector run (week: just those days)
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
VERSION = 2  # 2: teammate streams, accumulate mode, seen_at (a v1 document migrates as is)
HISTORY_DAYS = 14
HEARTBEAT_MS = 20 * 1000
NODATA_MIN_MS = 60 * 1000  # a gap shorter than this is never "no data" (> HEARTBEAT_MS + a run)
SEEN_STEP_MS = 5 * 60 * 1000  # the board view's freshness bucket (see board_view)
FOCUS = "working"
BREAK = ("done", "rest")
GONE = "gone"
PAUSE = ("needs-you", GONE)
MODES = ("accumulate", "strict")
DEFAULTS = {"enabled": True, "focus_minutes": 25, "grace_minutes": 2, "timezone": "",
            "notify": False, "notify_every": 10, "mode": "accumulate", "teammate_idle_s": 60}
SEG = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
NAME_RE = re.compile(r"^%s$" % SEG)
CODE_RE = re.compile(r"^%s(/%s)?$" % (SEG, SEG))   # a stream: "CODE" or "CODE/teammate-name"
RES_STALE_MS = 2 * 60 * 1000  # resources.json older than this (plus 2 sample intervals): no teammate data
DAY_MS = 24 * 3600 * 1000
TEAM_GONE_MS = DAY_MS   # a gone teammate keeps its unfinished pomodoro this long (its day tallies stay)
VIEW_TEAM_CAP = 50      # teammate rows per session in the board view; the rest fold into one
OTHERS = "+others"      # "CODE/+others": the folded row ("+" never starts a real teammate name)


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
        "mode": c.get("mode") if c.get("mode") in MODES else DEFAULTS["mode"],
        "teammate_idle_ms": int(_num(c.get("teammate_idle_s"), DEFAULTS["teammate_idle_s"], 5, 3600) * 1000),
        # fleet.yaml poll_interval (s): a gap is "no data" only beyond a heartbeat plus two runs
        "poll_ms": int(_num(cfg.get("poll_interval") if isinstance(cfg, dict) else None, 3, 1, 3600) * 1000),
        # resources.interval (s): how often resources.json can say a teammate is active
        "res_ms": int(_num((cfg.get("resources") or {}).get("interval") if isinstance(cfg, dict)
                           and isinstance(cfg.get("resources"), dict) else None, 15, 1, 3600) * 1000),
    }


def strict(st):
    return st.get("mode") == "strict"


def is_teammate(key):
    return "/" in key


def parent_of(key):
    return key.split("/", 1)[0]


def safe_name(name):
    """A teammate name as a stream key segment ("" when nothing usable is left)."""
    n = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name or "")).strip("-._")[:64]
    return n if NAME_RE.match(n) else ""


def busy_from_cards(doc):
    """{(code, safe name)} of the teammates whose pane shows the working spinner, from the document
    cards.step just built (lib/cards.py teammates(): `busy` true). No extra process or read."""
    out = set()
    for code, c in ((doc or {}).get("sessions") or {}).items() if isinstance(doc, dict) else ():
        for t in (c.get("teammates") or []) if isinstance(c, dict) else ():
            if isinstance(t, dict) and t.get("busy") is True and safe_name(t.get("name")):
                out.add((code, safe_name(t.get("name"))))
    return out


def teammate_states(res, now, st, spinning=None):
    """{"CODE/name": "working" | "rest"} from resources.json, or None when it has no usable teammate
    data (missing, disabled, or older than RES_STALE_MS plus two sample intervals): every teammate
    stream then pauses. Focused = either signal: not idle with idle_s known and below
    teammate_idle_ms (CPU), or (code, name) in `spinning` (the pane's working spinner, see
    busy_from_cards: a long model turn idles the CPU). Only codes, names and states leave here."""
    spinning = spinning or set()
    if not isinstance(res, dict) or res.get("enabled") is False or not isinstance(res.get("sessions"), list):
        return None
    at = res.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool) or now - at > RES_STALE_MS + 2 * st.get("res_ms", 15000):
        return None
    out = {}
    for s in res["sessions"]:
        code = s.get("code") if isinstance(s, dict) else None
        if not isinstance(code, str) or not NAME_RE.match(code):
            continue
        for t in s.get("teammates") or []:
            name = safe_name(t.get("name")) if isinstance(t, dict) else ""
            if not name:
                continue
            idle_s = t.get("idle_s")
            busy = (t.get("idle") is not True and isinstance(idle_s, (int, float)) and not isinstance(idle_s, bool)
                    and idle_s * 1000 < st.get("teammate_idle_ms", 60000)) or (code, name) in spinning
            key = code + "/" + name
            if busy or key not in out:    # two teammates with one name: one stream, busy if either is
                out[key] = FOCUS if busy else "rest"
    return out


def nodata_ms(st):
    """How long without a collector run counts as a break: GRACE, at least NODATA_MIN_MS, and at
    least one heartbeat plus two poll intervals (a slow poll_interval never looks like a gap)."""
    return max(st["grace_ms"], NODATA_MIN_MS, HEARTBEAT_MS + 2 * st.get("poll_ms", 3000))


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
    # v1 -> v2: the same sessions and days (a v1 code is a top-level stream); seen_at starts at `at`
    for code, r in (doc.get("sessions") or {}).items() if isinstance(doc.get("sessions"), dict) else ():
        if not isinstance(r, dict) or not CODE_RE.match(str(code)):
            continue
        ok = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
        out["sessions"][code] = {
            "state": r.get("state") if isinstance(r.get("state"), str) else "offline",
            "acc": r["acc"] if ok(r.get("acc")) and r["acc"] >= 0 else 0,
            "away_since": r["away_since"] if ok(r.get("away_since")) else None,
            "done_at": r["done_at"] if ok(r.get("done_at")) else None,
            "seen_at": r["seen_at"] if ok(r.get("seen_at")) else out["at"],
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
    """Advance the timers to `now`, given each stream's state seen now ({key: state}; a missing
    session code is offline, a missing teammate key is gone). Returns (new document, events).
    Pure: no I/O, `doc` is not changed."""
    doc = normalize(doc)
    tz = tzinfo(st.get("tz") or "")
    grace = st["grace_ms"]
    hard = strict(st)
    seen = doc["at"]
    gap = now - seen if seen is not None and now >= seen else None  # None: first run / clock moved back
    events = []
    for code in sorted(set(doc["sessions"]) | {c for c in states if CODE_RE.match(str(c))}):
        rec = doc["sessions"].get(code) or {"state": "offline", "acc": 0, "away_since": None, "done_at": None,
                                            "seen_at": now}
        prev, new = rec["state"], states.get(code) or (GONE if is_teammate(code) else "offline")
        if gap is not None and gap > nodata_ms(st):
            if hard:
                _reset(rec, code, seen, events)         # nothing was seen for longer than the grace
            rec["away_since"] = None                    # accumulate: a break; the gap is not credited
        elif gap is not None:
            if prev == FOCUS:
                _focus(doc, code, rec, seen, now, st, tz, events)
            elif prev in BREAK:
                since = rec["away_since"] if rec["away_since"] is not None else seen
                if hard and now - since > grace:
                    _reset(rec, code, since + grace, events)
            elif prev not in PAUSE:
                _reset(rec, code, seen, events)         # offline (or an unknown state)
        if new in BREAK:
            if not (prev in BREAK and rec["away_since"] is not None):
                rec["away_since"] = now
            rec["away_since"] = min(rec["away_since"], now)  # the clock may have moved back
        else:
            rec["away_since"] = None
        if new not in (FOCUS,) + BREAK + PAUSE:
            _reset(rec, code, now, events)
        rec["state"] = new
        if new not in ("offline", GONE):
            rec["seen_at"] = now
        elif rec.get("seen_at") is None or rec["seen_at"] > now:
            rec["seen_at"] = now                        # the clock moved back: start the wait again
        keep_ms = TEAM_GONE_MS if new == GONE else HISTORY_DAYS * DAY_MS
        if new in ("offline", GONE) and (not rec["acc"] or now - rec["seen_at"] > keep_ms):
            doc["sessions"].pop(code, None)              # nothing to keep (or gone too long)
        else:
            doc["sessions"][code] = rec
    doc["at"] = now
    today = day_of(now, tz)
    # Only days older than the window go: a later day stays (the clock moved back across midnight,
    # or pomodoro.timezone moved west) and is dropped once it, too, is out of the window.
    oldest = days_back(today, HISTORY_DAYS)[0]
    doc["days"] = {d: v for d, v in doc["days"].items() if d >= oldest}
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


def run(cfg, home, rows, now, notify_ok=True, send=None, res=False, busy=None):
    """The collector's step: read pomodoro.json, advance it with this run's session states and the
    teammates' activity in resources.json (`res`; False = read it from `home`) and their pane
    spinners (`busy`, see busy_from_cards), write it when needed, notify a milestone.
    Returns (doc, events)."""
    st = settings(cfg)
    old = read_doc(home)
    if not st["enabled"]:
        if old is not None:
            try:
                os.remove(os.path.join(home, FILE))
            except OSError:
                pass
        return None, []
    states = {r["code"]: r.get("state") for r in rows
              if isinstance(r, dict) and isinstance(r.get("code"), str) and NAME_RE.match(r["code"])}
    if res is False:
        try:
            with open(os.path.join(home, "resources.json"), encoding="utf-8") as fh:
                res = json.load(fh)
        except (OSError, ValueError):
            res = None
    states.update(teammate_states(res, now, st, busy) or {})   # no data: every teammate stream is gone (paused)
    doc, events = step(old, states, now, st)
    total, reached = milestone(doc, now, st)
    doc["settings"] = {"focus_ms": st["focus_ms"], "grace_ms": st["grace_ms"], "tz": st["tz"], "mode": st["mode"]}
    old_at = old.get("at") if isinstance(old, dict) else None
    # A running pomodoro needs a fresh `at`, or the next run would take the time since the last
    # write for a gap with no data; in strict mode a paused one does too (a gap would reset it).
    # Accumulate keeps paused progress across any gap, so only a running clock needs the heartbeat.
    live = any(r["state"] == FOCUS or (strict(st) and r["acc"]) for r in doc["sessions"].values())
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

def day_start(ms, tz):
    """Epoch ms of the local midnight that starts the day of `ms`."""
    d = dt.datetime.fromtimestamp(ms / 1000.0, tz).date()
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000)


def raw_view(doc, now):
    """Today's tallies and each session's current pomodoro, as of the document's `at` (for the CLI
    and the Artifact mirror)."""
    if not isinstance(doc, dict):
        return None
    s = doc.get("settings") if isinstance(doc.get("settings"), dict) else {}
    st = {"focus_ms": int(_num(s.get("focus_ms"), 25 * 60000, 60000, 180 * 60000)),
          "grace_ms": int(_num(s.get("grace_ms"), 2 * 60000, 0, 3600000)),
          "tz": s.get("tz") if isinstance(s.get("tz"), str) else "",
          "mode": s.get("mode") if s.get("mode") in MODES else "strict"}  # a v1 file had no mode: strict
    raw = doc
    doc = normalize(doc)
    tz = tzinfo(st["tz"])
    today = day_of(now, tz)
    per = doc["days"].get(today, {})
    sessions = {}
    for code in sorted(set(per) | set(doc["sessions"]), key=lambda c: (parent_of(c), c)):  # teammates after their lead
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
    tm = [v for c, v in per.items() if is_teammate(c)]
    return {"enabled": True, "focus_ms": st["focus_ms"], "grace_ms": st["grace_ms"], "tz": st["tz"],
            "mode": st["mode"], "at": raw.get("at") if isinstance(raw.get("at"), int) else None, "today": today,
            "fleet": {"done": days[-1]["done"], "focus_ms": days[-1]["focus_ms"],
                      "teammates_done": sum(v["done"] for v in tm)},
            "sessions": sessions, "days": days}


def bound_streams(sessions, cap=VIEW_TEAM_CAP):
    """The streams worth a row on the board: a 🍅 or focus today, or a current pomodoro (running, or
    paused with progress). At most `cap` teammates per session: the running ones always, then the
    most 🍅 and focus; the rest fold into one "CODE/+others" row with their summed counts and
    `streams`, so every total stays exact. Folded streams never run, so the row needs no clock."""
    keep = {c: v for c, v in sessions.items() if v["done"] or v["focus_ms"] or v["running"] or v["progress_ms"]}
    by_lead = {}
    for c, v in keep.items():
        if is_teammate(c):
            by_lead.setdefault(parent_of(c), []).append((c, v))
    folded, extra = set(), {}
    for lead, rows in by_lead.items():
        if len(rows) <= cap:
            continue
        rows.sort(key=lambda cv: (not cv[1]["running"], -cv[1]["done"], -cv[1]["focus_ms"], cv[0]))
        for c, v in rows[max(cap, sum(1 for _, r in rows if r["running"])):]:
            folded.add(c)
            e = extra.setdefault(lead + "/" + OTHERS, {"done": 0, "focus_ms": 0, "progress_ms": 0, "running": False,
                                                       "state": "folded", "done_at": None, "streams": 0})
            e["done"] += v["done"]
            e["focus_ms"] += v["focus_ms"]
            e["streams"] += 1
    out = {c: v for c, v in keep.items() if c not in folded}
    out.update(extra)
    return {c: out[c] for c in sorted(out, key=lambda c: (parent_of(c), c))}


def board_view(doc, now):
    """What board-serve gives the page (state.pomodoro): numbers and session codes only. It changes
    only when something a viewer sees changes, never with the collector's heartbeat, so it costs no
    SSE frame while sessions simply keep working or keep waiting:

      - a running pomodoro carries `started_at` (= at - progress: fixed while the session works)
        and `progress_ms: null`; a paused one carries `progress_ms` and `started_at: null`;
      - every focus_ms of today leaves out the running stretches; the page adds
        now - max(started_at, day_start) for each running session;
      - `seen` is the last collector run floored to SEEN_STEP_MS (null when nothing runs): the page
        stops the clocks once it is older than seen_step_ms plus a few minutes.
    """
    v = raw_view(doc, now)
    if not v:
        return None
    # Bounded however many teammates came and went today: the rows worth showing (bound_streams);
    # the fleet and day totals come from every stream. The page has no use for the per-day maps.
    v["sessions"] = bound_streams(v["sessions"])
    for d in v["days"]:
        d.pop("sessions", None)
    at = v.pop("at")
    ds = day_start(now, tzinfo(v["tz"]))
    shift, running = 0, False
    for r in v["sessions"].values():
        if r["running"] and at is not None:
            running = True
            r["started_at"] = at - r["progress_ms"]
            r["progress_ms"] = None
            live = max(0, at - max(r["started_at"], ds))  # the running stretch that counts for today
            # May go negative when the pomodoro paused across midnight (part of its progress counted
            # for yesterday). Never clamp it here: base + live must stay the collector's focus.
            r["focus_ms"] -= live
            shift += live
        else:
            r["running"] = False if at is None else r["running"]
            r["started_at"] = None
    v["fleet"]["focus_ms"] -= shift
    v["days"][-1]["focus_ms"] -= shift
    v.update({"day_start": ds, "seen": (at // SEEN_STEP_MS) * SEEN_STEP_MS if running else None,
              "seen_step_ms": SEEN_STEP_MS})
    return v


def mirror_view(view):
    """meta/pomodoro for the Artifact: the daily totals (from raw_view), no running clock."""
    if not view:
        return None
    return {"today": view["today"], "tz": view["tz"], "focus_ms": view["focus_ms"], "mode": view["mode"], "fleet": view["fleet"],
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
    view = raw_view(read_doc(home), now) if st["enabled"] else None
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
    tm_done = view["fleet"].get("teammates_done", 0)
    out.write("AI fleet: %d 🍅 · %s focus%s\n" % (view["fleet"]["done"], fmt_focus(view["fleet"]["focus_ms"]),
                                                  " (teammates %d 🍅)" % tm_done if tm_done else ""))
    for code, v in view["sessions"].items():
        cur = ""
        if v["progress_ms"] or v["running"]:
            cur = "  now %s / %s%s" % (_mmss(v["progress_ms"]), _mmss(view["focus_ms"]),
                                       "" if v["running"] else " (paused)")
        label = "  + " + code.split("/", 1)[1] if is_teammate(code) else code
        out.write("  %-16s %3d 🍅  %8s%s\n" % (fleet.safe_text(label)[:16], v["done"], fmt_focus(v["focus_ms"]), cur))
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
