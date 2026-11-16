#!/usr/bin/env python3
"""reminders: the owner's own alarms on the board ("3 PM meeting, remind me 5 minutes early").

State: $BOARD_HOME/reminders.json (0600, atomic writes, flock reminders.lock)
  {"version": 1, "reminders": [{
     id, title (secret guard scrubbed, <= 200 chars), at "HH:MM" (local wall time), date
     "YYYY-MM-DD" (the first occurrence), tz (IANA name, "" = this machine's local time),
     lead_min (5), level (high), breakthrough (true), repeat once|daily|weekdays, created_by,
     created_at, state active|done|missed,
     occ {at: ms of T, lead_at, lead_sent, due_sent, late, snooze_at, snoozes, last_sent, acked_at,
          acked_by, missed}   the current occurrence; its fired times are persisted before each
                    send, so an occurrence fires once, across collector restarts too. It stays
                    open (Ack, Snooze) until LATE_MIN after its last nudge
     history [{at, result: done|acked|missed}]  the last 10 occurrences}]}

Firing (tick(), called by the collector on every run; zero tokens):
  T - lead   "⏰ in 5 min: Meeting (15:00)" at the reminder's level, unless acked;
  T          "⏰ now: Meeting (15:00)", unless acked; "(late)" when the collector was down and it is
             up to LATE_MIN (15) minutes after T; later than that the occurrence is marked missed
             and nothing is sent (never a backlog);
  snooze     "⏰ snoozed: Meeting (15:00)" at the snooze time, unless acked;
  then a repeating reminder moves to its next occurrence (daily, or weekdays: Mon-Fri), skipping
  any that are already over (marked missed, never sent).
breakthrough: true sends through notify's own urgent rule for mutes (a mute without
include_urgent does not hold it); false obeys mutes like any alert. Min levels and presence apply
as usual. Desktop toasts follow ALERT_DESKTOP like every alert.

CLI (bin/board remind ...):
  reminders.py add TITLE --at 15:00|3pm [--date YYYY-MM-DD] [--lead 5] [--level info|warn|high|urgent]
                         [--repeat once|daily|weekdays] [--no-breakthrough] [--by WHO] [--json]
  reminders.py ls [--json] | rm ID | snooze ID 5m|1h | ack ID   [--by WHO]
  reminders.py tick [--json]                   one firing pass (what the collector runs)
--at is 24-hour unless it ends in am/pm: 15:00, 9:30, 1530, 3pm, 3:30 pm (so 123 is 01:23).
Exit codes: 0 ok, 1 error, 2 usage or a bad value, 3 no such reminder, 8 busy (try again).
Test hooks: BOARD_NOW_MS (fake clock, epoch ms).
"""
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402
import guard  # noqa: E402

VERSION = 1
LEVELS = ("info", "warn", "high", "urgent")
REPEATS = ("once", "daily", "weekdays")
TITLE_MAX = 200
LEAD_MAX = 24 * 60
LATE_MIN = 15                 # a missed T still fires (late) up to this long after it
SNOOZE_MAX_MS = 12 * 3600 * 1000
MAX_REMINDERS = 100
HISTORY_KEEP = 10
ID_RE = re.compile(r"r-[0-9]+-[a-z0-9]{4,12}")
TIME_RE = re.compile(r"(?i)\s*(\d{1,2})(?::?(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
DUR_RE = re.compile(r"([1-9]\d{0,3})\s*(m|min|h)")
DEFAULTS = {"default_lead_min": 5, "default_level": "high", "timezone": ""}


class Bad(Exception):
    """A usage error or a bad value (exit 2)."""


class NotFound(Exception):
    pass


class Busy(Exception):
    """reminders.lock stayed taken: try again (CLI exit 8, route 409 busy; the collector skips a tick)."""


def now_ms():
    v = os.environ.get("BOARD_NOW_MS")
    return int(v) if v and v.isdigit() else int(time.time() * 1000)


def settings(cfg=None):
    cfg = cfg if cfg is not None else fleet.load_fleet()
    r = cfg.get("reminders") if isinstance(cfg.get("reminders"), dict) else {}
    out = dict(DEFAULTS)
    try:
        out["default_lead_min"] = max(0, min(LEAD_MAX, int(r.get("default_lead_min", DEFAULTS["default_lead_min"]))))
    except (TypeError, ValueError):
        pass
    if r.get("default_level") in LEVELS:
        out["default_level"] = r["default_level"]
    if isinstance(r.get("timezone"), str):
        out["timezone"] = r["timezone"].strip()
    return out


def owners(cfg=None):
    """Who owns reminders on the board: fleet.yaml `owners`, else `approvers` (the owners ->
    approvers fallback). One helper, so the rule moves with the role model."""
    return fleet.owner_ids(cfg if cfg is not None else fleet.load_fleet())


def owner_of(ident, cfg=None):
    """The configured spelling of IDENT when it owns reminders, else None. Empty list: nobody."""
    want = str(ident or "").strip().lower()
    return next((o for o in owners(cfg) if o.strip().lower() == want), None) if want else None


# ------------------------------------------------------------------ time
def tzinfo(name):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown name or no tz database: this machine's time
        return None


def parse_time(s):
    """"15:00", "9:30", "3pm", "3:30 pm", "12am" -> (hour, minute). Raises Bad."""
    m = TIME_RE.fullmatch(str(s or ""))
    if not m:
        raise Bad("--at takes a time like 15:00 or 3pm: %r" % str(s)[:40])
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
    if ap:
        if not 1 <= h <= 12:
            raise Bad("with am/pm the hour is 1-12: %r" % s)
        h = (0 if h == 12 else h) + (12 if ap == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise Bad("not a time of day: %r" % s)
    return h, mi


def local_ms(day, h, mi, tz):
    """Epoch ms of local wall time DAY h:mi in tz (None: this machine's local time)."""
    t = dt.datetime(day.year, day.month, day.day, h, mi, tzinfo=tz) if tz else dt.datetime(day.year, day.month, day.day, h, mi)
    return int(t.timestamp() * 1000)


def local_day(ms, tz):
    return dt.datetime.fromtimestamp(ms / 1000.0, tz).date()


def occurs_on(day, repeat):
    return repeat != "weekdays" or day.weekday() < 5


def first_occurrence(h, mi, tz, now, date=None, repeat="once"):
    """(epoch ms, note). With DATE: that day (must not be over), moved to the next weekday for
    `weekdays`. Without: today when the time is still ahead, else the next day it occurs."""
    if date is not None:
        if not DATE_RE.fullmatch(str(date)):
            raise Bad("--date takes YYYY-MM-DD: %r" % str(date)[:20])
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            raise Bad("not a date: %r" % date) from None
    else:
        day = local_day(now, tz)
    note = None
    for _ in range(8):
        t = local_ms(day, h, mi, tz)
        if occurs_on(day, repeat) and t > now:
            return t, note
        if date is not None and t <= now and occurs_on(day, repeat):
            raise Bad("%s %02d:%02d is already over" % (date, h, mi))
        if t <= now and note is None:
            note = "%02d:%02d has passed today" % (h, mi)
        day += dt.timedelta(days=1)
    raise Bad("no occurrence in the next week")


def next_after(r, t):
    """The occurrence after T for a repeating reminder (None for once)."""
    if r["repeat"] == "once":
        return None
    tz = tzinfo(r.get("tz"))
    h, mi = (int(x) for x in r["at"].split(":"))
    day = local_day(t, tz)
    for _ in range(8):
        day += dt.timedelta(days=1)
        if occurs_on(day, r["repeat"]):
            return local_ms(day, h, mi, tz)
    return None


def hhmm(r):
    return r["at"]


# ------------------------------------------------------------------ store
def path(home):
    return os.path.join(home, "reminders.json")


@contextlib.contextmanager
def locked(home, wait_s=5.0):
    """reminders.json under an exclusive flock (the collector, the CLI and board-serve all write).
    Waits at most wait_s, then raises Busy instead of hanging."""
    os.makedirs(home, exist_ok=True)
    fd = os.open(os.path.join(home, "reminders.lock"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        end = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise Busy("the reminders are busy; try again") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def load(home):
    try:
        with open(path(home), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("reminders"), list):
        doc = {"version": VERSION, "reminders": []}
    doc["reminders"] = [r for r in doc["reminders"] if isinstance(r, dict) and ID_RE.fullmatch(str(r.get("id") or ""))]
    return doc


def save(home, doc):
    p = path(home)
    tmp = "%s.tmp.%d.%d" % (p, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, p)


def audit(home, ev):
    ev = dict({"ts": now_ms(), "type": "reminder"}, **ev)   # never the title: it is personal
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def find(doc, rid):
    r = next((x for x in doc["reminders"] if x.get("id") == rid), None)
    if r is None:
        raise NotFound("no such reminder: %s" % str(rid)[:40])
    return r


def new_occ(r, t):
    return {"at": t, "lead_at": t - int(r["lead_min"]) * 60000, "lead_sent": None, "due_sent": None, "late": False,
            "snooze_at": None, "snoozes": 0, "acked_at": None, "acked_by": None, "missed": False}


# ------------------------------------------------------------------ operations
def clean_title(title):
    t = guard.scrub(fleet.CONTROL_RE.sub(" ", str(title or ""))).strip()
    t = re.sub(r"\s+", " ", t)
    if not t:
        raise Bad("the title is empty")
    if len(t) > TITLE_MAX:
        raise Bad("the title is over %d characters" % TITLE_MAX)
    return t


def add(home, title, at, date=None, lead=None, level=None, repeat="once", breakthrough=True, by="cli", cfg=None, now=None):
    """Create a reminder. Returns (record, note) where note says when "today" became tomorrow."""
    s = settings(cfg)
    now = now if now is not None else now_ms()
    title = clean_title(title)
    h, mi = parse_time(at)
    lead = s["default_lead_min"] if lead in (None, "") else lead
    try:
        lead = int(lead)
    except (TypeError, ValueError):
        raise Bad("--lead takes minutes") from None
    if not 0 <= lead <= LEAD_MAX:
        raise Bad("--lead is 0-%d minutes" % LEAD_MAX)
    level = level or s["default_level"]
    if level not in LEVELS:
        raise Bad("--level is one of %s" % ", ".join(LEVELS))
    if repeat not in REPEATS:
        raise Bad("--repeat is one of %s" % ", ".join(REPEATS))
    tzname = s["timezone"] if tzinfo(s["timezone"]) else ""
    tz = tzinfo(tzname)
    t, note = first_occurrence(h, mi, tz, now, date, repeat)
    r = {"id": "r-%d-%s" % (now, secrets.token_hex(3)), "title": title, "at": "%02d:%02d" % (h, mi),
         "date": local_day(t, tz).isoformat(), "tz": tzname, "lead_min": lead, "level": level,
         "breakthrough": bool(breakthrough), "repeat": repeat, "created_by": str(by)[:200], "created_at": now,
         "state": "active", "history": []}
    r["occ"] = new_occ(r, t)
    with locked(home):
        doc = load(home)
        if len([x for x in doc["reminders"] if x.get("state") == "active"]) >= MAX_REMINDERS:
            raise Bad("at most %d active reminders" % MAX_REMINDERS)
        doc["reminders"].append(r)
        save(home, doc)
    audit(home, {"action": "add", "id": r["id"], "by": r["created_by"], "at": t, "repeat": repeat, "level": level})
    if note:
        note = "%s: set for %s" % (note, r["date"])
    return r, note


def parse_duration(s):
    m = DUR_RE.fullmatch(str(s or "").strip().lower())
    if not m:
        raise Bad("snooze takes minutes or hours: 5m, 30m, 1h")
    ms = int(m.group(1)) * (3600000 if m.group(2) == "h" else 60000)
    if ms > SNOOZE_MAX_MS:
        raise Bad("snooze is at most 12 h")
    return ms


def _finish(r, result, now):
    """Close the current occurrence and move on (a repeating reminder) or end (once)."""
    occ = r["occ"]
    r["history"] = (r.get("history") or [])[-(HISTORY_KEEP - 1):] + [{"at": occ["at"], "result": result, "closed_at": now}]
    nxt = next_after(r, occ["at"])
    skipped = 0
    while nxt is not None and nxt + LATE_MIN * 60000 < now:      # never a backlog
        skipped += 1
        nxt = next_after(r, nxt)
    if nxt is None:
        r["state"] = "missed" if result == "missed" else "done"
        r["occ"] = dict(occ, closed=result)
    else:
        r["occ"] = new_occ(r, nxt)
    return skipped


def act(home, rid, action, by="cli", minutes=None, now=None):
    """ack | snooze | delete on one reminder. Returns the record (None after delete)."""
    now = now if now is not None else now_ms()
    with locked(home):
        doc = load(home)
        r = find(doc, rid)
        if action == "delete":
            doc["reminders"] = [x for x in doc["reminders"] if x.get("id") != rid]
            out = None
        elif r.get("state") != "active":
            raise Bad("the reminder is %s" % r.get("state"))
        elif action == "ack":
            r["occ"].update(acked_at=now, acked_by=str(by)[:200])
            _finish(r, "acked", now)
            out = r
        elif action == "snooze":
            ms = minutes if isinstance(minutes, int) else parse_duration(minutes)
            if now < r["occ"]["lead_at"]:
                raise Bad("nothing to snooze yet: it starts at %s" % hhmm(r))
            r["occ"].update(snooze_at=now + ms, snoozes=int(r["occ"].get("snoozes") or 0) + 1)
            out = r
        else:
            raise Bad("unknown action %r" % action)
        save(home, doc)
    audit(home, {"action": action, "id": rid, "by": str(by)[:200]})
    return out


def phase_due(r, now):
    """(phase, late) the reminder needs now, or (None, False)."""
    occ = r["occ"]
    if r.get("state") != "active" or occ.get("acked_at"):
        return None, False
    t, late_ms = occ["at"], LATE_MIN * 60000
    if occ.get("snooze_at") and now >= occ["snooze_at"]:
        return ("snooze", False) if now - occ["snooze_at"] <= late_ms else ("drop-snooze", False)
    if not occ.get("due_sent"):
        if now >= t:
            return ("due", now - t > 2 * 60000) if now - t <= late_ms else ("missed", False)
        if not occ.get("lead_sent") and now >= occ["lead_at"] and r["lead_min"] > 0:
            return "lead", False
    return None, False


def message(r, phase, late, now):
    t = r["occ"]["at"]
    mins = max(1, int(round((t - now) / 60000.0)))
    head = {"lead": "⏰ in %d min: " % mins, "due": "⏰ now: ", "snooze": "⏰ snoozed: "}[phase]
    title = "%s%s (%s)%s" % (head, r["title"], hhmm(r), " (late)" if late else "")
    body = "" if phase != "lead" else "Ack or snooze it on the board, or: board remind ack %s" % r["id"]
    return title, body


def _plan(home, now):
    """Under the lock: advance every reminder and list the notices to send. (out, todo)"""
    out, todo = [], []
    doc = load(home)
    changed = False
    for r in doc["reminders"]:
        if r.get("state") != "active" or not isinstance(r.get("occ"), dict):
            continue
        occ = r["occ"]
        phase, late = phase_due(r, now)
        # The due nudge sent, no snooze pending and LATE_MIN since the last nudge: it is over
        # (until then the board still offers Ack and Snooze).
        if phase is None and occ.get("due_sent") and not occ.get("snooze_at") \
                and now >= int(occ.get("last_sent") or occ["due_sent"]) + LATE_MIN * 60000:
            _finish(r, "done", now)
            changed = True
            phase, late = phase_due(r, now)
            occ = r["occ"]
        if phase is None:
            continue
        changed = True
        if phase == "missed":
            occ["missed"] = True
            skipped = _finish(r, "missed", now)
            out.append({"id": r["id"], "phase": "missed", "status": "not sent", "skipped": skipped})
            continue
        if phase == "drop-snooze":
            occ["snooze_at"] = None
            continue
        title, body = message(r, phase, late, now)
        key = "reminder:%s:%d:%s" % (r["id"], occ["at"], phase if phase != "snooze" else "snooze%d" % occ.get("snoozes", 1))
        if phase == "lead":
            occ["lead_sent"] = now
        elif phase == "due":
            occ["due_sent"], occ["late"] = now, late
        else:
            occ["snooze_at"] = None
        occ["last_sent"] = now
        todo.append((r, phase, key, title, body))
    if changed:
        save(home, doc)
    return out, todo


def tick(home=None, now=None, send=None, cfg=None):
    """One firing pass. Fired times are written before the send, so an occurrence never fires twice
    (a crash between write and send loses that one notice rather than repeating it). Returns a
    list of {id, phase, status}."""
    home = home or fleet.board_home()
    if not os.path.exists(path(home)):
        return []
    now = now if now is not None else now_ms()
    try:
        with locked(home, wait_s=1.0):
            out, todo = _plan(home, now)
    except Busy:
        return []          # a CLI or board write holds it: the next collector run fires
    for r, phase, key, title, body in todo:
        res = (send or _send)(r["level"], key, title, body, bool(r.get("breakthrough")), now, cfg)
        status = res.get("status") if isinstance(res, dict) else None
        audit(home, {"action": "fire", "id": r["id"], "phase": phase, "result": status})
        out.append({"id": r["id"], "phase": phase, "status": status, "key": key})
    return out


def _send(level, key, title, body, breakthrough, now, cfg):
    import notify  # noqa: WPS433 - lazy: the CLI's ls/add never needs it
    return notify.send(level, key, title, body, kind="reminder", escalate=False, repeat=False,
                       breakthrough=breakthrough, now=now)


def view(r, now):
    """A reminder as the board and `ls --json` show it."""
    occ = r.get("occ") or {}
    phase = "upcoming"
    if r.get("state") != "active":
        phase = r.get("state")
    elif occ.get("acked_at"):
        phase = "acked"
    elif now >= occ.get("at", 0):
        phase = "due"
    elif now >= occ.get("lead_at", 0):
        phase = "soon"
    return {k: r.get(k) for k in ("id", "title", "at", "date", "tz", "lead_min", "level", "breakthrough", "repeat",
                                   "created_by", "created_at", "state")} | {
        "next_at": occ.get("at"), "lead_at": occ.get("lead_at"), "snooze_at": occ.get("snooze_at"),
        "phase": phase, "late": bool(occ.get("late"))}


def board_view(home, now=None):
    now = now if now is not None else now_ms()
    doc = load(home)
    rows = [view(r, now) for r in doc["reminders"]]
    rows.sort(key=lambda v: (v["state"] != "active", v.get("next_at") or 0))
    return guard.scrub_tree({"reminders": rows, "now": now})


# ------------------------------------------------------------------ CLI
def _fmt(v, tz):
    t = v.get("next_at")
    when = dt.datetime.fromtimestamp(t / 1000.0, tzinfo(tz)).strftime("%a %Y-%m-%d %H:%M") if t else "-"
    return "%s  %-8s %s  lead %dm  %s  %s%s  %s" % (
        v["id"], v["phase"], when, v["lead_min"], v["level"], v["repeat"], "" if v["breakthrough"] else " (obeys mutes)",
        fleet.safe_text(v["title"]))


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="board remind", description="the owner's reminders (alarms)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("title")
    a.add_argument("--at", required=True, help="24-hour unless it ends in am/pm: 15:00, 9:30, 1530, 3pm (123 is 01:23)")
    a.add_argument("--date")
    a.add_argument("--lead")
    a.add_argument("--level", choices=LEVELS)
    a.add_argument("--repeat", choices=REPEATS, default="once")
    a.add_argument("--no-breakthrough", action="store_true")
    a.add_argument("--by", default="cli")
    a.add_argument("--json", action="store_true")
    ls = sub.add_parser("ls")
    ls.add_argument("--json", action="store_true")
    for name in ("rm", "ack"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--by", default="cli")
    sn = sub.add_parser("snooze")
    sn.add_argument("id")
    sn.add_argument("duration", nargs="?", default="5m")
    sn.add_argument("--by", default="cli")
    tk = sub.add_parser("tick")
    tk.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])
    home = fleet.ensure_home()
    try:
        if args.cmd == "add":
            r, note = add(home, args.title, args.at, args.date, args.lead, args.level, args.repeat,
                          not args.no_breakthrough, args.by)
            v = view(r, now_ms())
            if note:
                sys.stderr.write("board remind: %s\n" % note)
            print(json.dumps(v, ensure_ascii=False) if args.json else _fmt(v, r["tz"]))
        elif args.cmd == "ls":
            bv = board_view(home)
            if args.json:
                print(json.dumps(bv, ensure_ascii=False, indent=1))
            elif not bv["reminders"]:
                print("no reminders")
            else:
                for v in bv["reminders"]:
                    print(_fmt(v, v.get("tz")))
        elif args.cmd in ("rm", "ack"):
            act(home, args.id, "delete" if args.cmd == "rm" else "ack", args.by)
            print("%s %s" % ("deleted" if args.cmd == "rm" else "acked", args.id))
        elif args.cmd == "snooze":
            r = act(home, args.id, "snooze", args.by, minutes=args.duration)
            print("snoozed %s until %s" % (args.id, dt.datetime.fromtimestamp(
                r["occ"]["snooze_at"] / 1000.0, tzinfo(r.get("tz"))).strftime("%H:%M")))
        elif args.cmd == "tick":
            res = tick(home)
            print(json.dumps(res) if args.json else "\n".join("%s %s %s" % (x["id"], x["phase"], x["status"]) for x in res))
        return 0
    except Bad as exc:
        sys.stderr.write("board remind: %s\n" % exc)
        return 2
    except NotFound as exc:
        sys.stderr.write("board remind: %s\n" % exc)
        return 3
    except Busy as exc:
        sys.stderr.write("board remind: %s\n" % exc)
        return 8


if __name__ == "__main__":
    sys.exit(main(sys.argv))
