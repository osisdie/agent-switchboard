#!/usr/bin/env python3
"""Usage-limit episodes: $BOARD_HOME/limit.json (see docs/contract.md, "Degraded mode").

Usage:
  limit.py record --event EVENT --text JSON_STRING   record/extend an episode (called by the hook)
  limit.py clear [--after-ms MS]                     end the active episode (only if hit_at < MS)
  limit.py show [--json]                             print the current state

limit.json: {hit_at, kind: "5h"|"weekly"|"unknown", resets_at|null, note, event,
             updated_at, cleared_at?}. An episode is active while cleared_at is absent.
"""
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402

LIMIT_RE = re.compile(
    r"usage[ _]limit|rate[ _]limit|limit reached|hit your (usage )?limit|resets? at|resets \d|"
    r"weekly limit|5-hour limit|out of (extra )?usage", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

RESET_RE = re.compile(
    r"resets?\s+(?:at\s+|on\s+)?"
    r"(?:(?P<wd>mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+(?:at\s+)?)?"
    r"(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+-]+)*)\))?", re.I)
RESET_IN_RE = re.compile(r"resets?\s+in\s+(?:(?P<h>\d+)\s*h(?:ours?|rs?)?)?\s*(?:(?P<m>\d+)\s*m(?:in(?:utes?)?)?)?", re.I)
EPOCH_RE = re.compile(r"\|\s*(?P<ts>\d{10})(?!\d)")


def now_ms():
    return int(time.time() * 1000)


def path():
    return os.path.join(fleet.board_home(), "limit.json")


def read():
    try:
        with open(path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def active():
    data = read()
    if data and not data.get("cleared_at"):
        return data
    return None


def write(data):
    fleet.ensure_home()
    tmp = "%s.tmp.%d" % (path(), os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path())


def _tz(name):
    if not name:
        return None
    if name.upper() == "UTC":
        return dt.timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # unknown zone name → local time
        return None


def parse_resets(text, now=None):
    """Best-effort reset time (epoch ms) from a limit message, or None."""
    now = now if now is not None else now_ms()
    m = EPOCH_RE.search(text)
    if m:
        return int(m.group("ts")) * 1000
    m = RESET_IN_RE.search(text)
    if m and (m.group("h") or m.group("m")):
        return now + (int(m.group("h") or 0) * 60 + int(m.group("m") or 0)) * 60000
    for m in RESET_RE.finditer(text):
        if not (m.group("ap") or m.group("m") or m.group("tz") or m.group("mon")):
            continue  # a bare number is too ambiguous
        hour, minute = int(m.group("h")), int(m.group("m") or 0)
        ap = (m.group("ap") or "").lower().replace(".", "")
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            continue
        tz = _tz(m.group("tz"))
        base = dt.datetime.fromtimestamp(now / 1000, tz) if tz else dt.datetime.fromtimestamp(now / 1000)
        if m.group("wd") and not m.group("mon"):
            want = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(m.group("wd").lower())
            cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            cand += dt.timedelta(days=(want - cand.weekday()) % 7)
            if cand.timestamp() * 1000 <= now:
                cand += dt.timedelta(days=7)
        elif m.group("mon"):
            mon = MONTHS.get(m.group("mon")[:3].lower())
            if not mon:
                continue
            try:
                cand = base.replace(month=mon, day=int(m.group("day")), hour=hour, minute=minute,
                                    second=0, microsecond=0)
            except ValueError:
                continue
            if cand.timestamp() * 1000 < now - 86400000:
                cand = cand.replace(year=cand.year + 1)
        else:
            cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if cand.timestamp() * 1000 <= now:
                cand += dt.timedelta(days=1)
        return int(cand.timestamp() * 1000)
    return None


def detect_kind(text, resets_at, now):
    t = text.lower()
    if re.search(r"weekly|per week|7-day|seven-day", t):
        return "weekly"
    if re.search(r"5-hour|five-hour|5h\b|session limit", t):
        return "5h"
    if resets_at and resets_at - now <= 5 * 3600 * 1000 + 60000:
        return "5h"
    return "unknown"


def fmt_time(ms, now=None):
    if not ms:
        return "unknown"
    now = now if now is not None else now_ms()
    t = dt.datetime.fromtimestamp(ms / 1000)
    return t.strftime("%H:%M") if 0 <= ms - now < 20 * 3600 * 1000 else t.strftime("%a %d %b %H:%M")


def record(event, text):
    now = now_ms()
    text = fleet.safe_text(text or "")[:300]
    resets = parse_resets(text, now)
    cur = active()
    if cur:
        if resets and not cur.get("resets_at"):
            cur["resets_at"] = resets
        if cur.get("kind") == "unknown":
            cur["kind"] = detect_kind(text, cur.get("resets_at"), now)
        cur["updated_at"] = now
        write(cur)
        return cur
    data = {"hit_at": now, "kind": detect_kind(text, resets, now), "resets_at": resets,
            "note": text, "event": event, "updated_at": now}
    write(data)
    return data


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    opts = {}
    i = 0
    while i < len(rest):
        if rest[i].startswith("--") and i + 1 < len(rest) and not rest[i + 1].startswith("--"):
            opts[rest[i][2:]] = rest[i + 1]
            i += 2
        else:
            opts[rest[i].lstrip("-")] = True
            i += 1
    if cmd == "record":
        text = opts.get("text") or ""
        try:
            text = json.loads(text) if isinstance(text, str) and text.startswith('"') else text
        except ValueError:
            pass
        text = str(text)
        m = LIMIT_RE.search(text)
        if not m:
            return 0
        # Keep only the text around the match (and the reset time, which follows it).
        record(str(opts.get("event") or ""), text[max(0, m.start() - 80):m.end() + 160].strip())
    elif cmd == "clear":
        cur = active()
        after = int(opts.get("after-ms") or 0)
        if cur and (not after or int(cur.get("hit_at") or 0) < after):
            cur["cleared_at"] = now_ms()
            write(cur)
            print("cleared")
    elif cmd == "show":
        cur = active()
        if opts.get("json"):
            print(json.dumps(cur))
        elif not cur:
            print("no active usage limit")
        else:
            print("usage limit hit %s (%s), resets ~%s" % (
                dt.datetime.fromtimestamp(cur["hit_at"] / 1000).strftime("%Y-%m-%d %H:%M"),
                fleet.safe_text(cur.get("kind")), fmt_time(cur.get("resets_at"))))
            if cur.get("note"):
                print("note: %s" % fleet.safe_text(cur["note"]))
    else:
        sys.stderr.write("unknown command: %s\n" % cmd)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
