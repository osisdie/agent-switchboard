#!/usr/bin/env python3
"""Notification controls for agent-switchboard: mutes, per-channel triage and presence-aware
reduction (docs/contract.md, "Notification controls"). Python stdlib only.

Usage (bin/board runs these; exit 0 ok, 1 error, 2 usage):
  prefs.py mute CHANNEL|all [--for 30m|1h|4h|2d | --until HH:MM|ISO] [--include-urgent] [--by WHO]
  prefs.py unmute CHANNEL|all [--by WHO]
  prefs.py mute-status [--json]
  prefs.py level CHANNEL|all info|warn|high|urgent|default [--by WHO]
  prefs.py presence on|off [--level warn|high|urgent] [--by WHO]

State:
  $BOARD_HOME/notify-prefs.json  {version, mutes: {CHANNEL|all: {until|null, by, at, include_urgent}},
                                  min_level: {CHANNEL|all: LEVEL}, presence: {reduce, min_level},
                                  updated_at, updated_by}            (0600, flock notify-prefs.lock)
  $BOARD_HOME/presence.json      {IDENTITY: {at, visible, interaction_at, via}}  (board-serve)

Delivery rule (lib/notify.py calls suppression() for every channel of every send):
  1. muted: a mute of the channel or of "all" that has not expired suppresses it ("muted:<target>"),
     except that an urgent alert breaks through unless that mute has include_urgent.
  2. triage: the level must reach the stricter of the "all" floor and the channel's own floor
     ("below_min_level"). Floors come from these prefs, else fleet.yaml alerting.min_level.
  3. presence: while presence reduction is on and an approver is watching the board (a visible
     page, a heartbeat within window_seconds and an interaction within idle_seconds), push
     channels only get what is at or above presence.min_level ("owner_present").
Urgent is the top level, so it always passes 2 and 3: only "mute ... --include-urgent" stops it.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402

LEVELS = ["info", "warn", "high", "urgent"]
CHANNELS = ["slack", "desktop", "email", "line", "sns", "webhook"]
TARGETS = CHANNELS + ["all"]
BREAKTHROUGH = "urgent"
PREFS_FILE = "notify-prefs.json"
PRESENCE_FILE = "presence.json"
LOCK_TIMEOUT_S = 10
MAX_MUTE_MS = 30 * 24 * 3600 * 1000           # --for / --until at most 30 days ahead
PRESENCE_KEEP_MS = 24 * 3600 * 1000           # presence entries older than this are dropped
PRESENCE_MAX = 50
BY_RE = re.compile(r"(board:)?[A-Za-z0-9][A-Za-z0-9@._+:-]{0,199}")
FOR_RE = re.compile(r"([1-9][0-9]{0,4})([mhd])")
HHMM_RE = re.compile(r"([01]?[0-9]|2[0-3]):([0-5][0-9])")
PRESENCE_DEFAULTS = {"reduce": True, "min_level": "high", "window_seconds": 120, "idle_seconds": 900}


class UsageError(ValueError):
    """A bad argument (exit 2)."""


def now_ms():
    fake = os.environ.get("BOARD_NOW_MS")
    if fake:
        try:
            return int(fake)
        except ValueError:
            pass
    return int(time.time() * 1000)


def rank(level):
    return LEVELS.index(level) if level in LEVELS else 0


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


class Locked:
    """Read-modify-write of one JSON file under an exclusive flock (LOCK_TIMEOUT_S), written
    atomically with mode 0600 only when it changed."""

    def __init__(self, home, name):
        self.path = os.path.join(home, name)
        self.lock_path = os.path.join(home, name.rsplit(".", 1)[0] + ".lock")

    def __enter__(self):
        self.lock = open(self.lock_path, "a")  # noqa: SIM115
        deadline = time.time() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    self.lock.close()
                    raise OSError("another writer holds %s; try again" % self.lock_path) from None
                time.sleep(0.05)
        self.data = _read(self.path)
        self.orig = json.dumps(self.data, sort_keys=True)
        return self

    def __exit__(self, exc_type, *rest):
        try:
            if exc_type is None and json.dumps(self.data, sort_keys=True) != self.orig:
                _write(self.path, self.data)
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
        return False


def _log(home, ev):
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ reading

def _valid_until(v):
    """None (until unmuted) or epoch ms as a plain int; anything else is malformed."""
    return v is None or (isinstance(v, int) and not isinstance(v, bool))


def normalize(data):
    """The prefs document with every field validated; unknown or malformed entries are dropped."""
    data = data if isinstance(data, dict) else {}
    out = {"mutes": {}, "min_level": {}, "presence": {}}
    for t, m in (data.get("mutes") or {}).items() if isinstance(data.get("mutes"), dict) else []:
        if t in TARGETS and isinstance(m, dict) and _valid_until(m.get("until")):
            # A malformed until (a string, a float, a bool) drops the whole mute: read as "until
            # unmuted" it would silence alerts for good. Fail open.
            out["mutes"][t] = {"until": m.get("until"),
                               "by": str(m.get("by") or "?")[:200], "at": m.get("at") if isinstance(m.get("at"), int) else 0,
                               "include_urgent": m.get("include_urgent") is True}
    for t, lvl in (data.get("min_level") or {}).items() if isinstance(data.get("min_level"), dict) else []:
        if t in TARGETS and lvl in LEVELS:
            out["min_level"][t] = lvl
    p = data.get("presence") if isinstance(data.get("presence"), dict) else {}
    if isinstance(p.get("reduce"), bool):
        out["presence"]["reduce"] = p["reduce"]
    if p.get("min_level") in LEVELS:
        out["presence"]["min_level"] = p["min_level"]
    for k in ("updated_at", "updated_by"):
        if data.get(k) is not None:
            out[k] = data[k]
    return out


def load(home=None):
    return normalize(_read(os.path.join(home or fleet.board_home(), PREFS_FILE)))


def load_presence(home=None):
    raw = _read(os.path.join(home or fleet.board_home(), PRESENCE_FILE))
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def active_mutes(prefs, now):
    """{target: mute} for the mutes that have not expired (until null = until unmuted)."""
    return {t: m for t, m in prefs["mutes"].items() if m["until"] is None or m["until"] > now}


def config_floors(cfg):
    """alerting.min_level from fleet.yaml: a level (the global floor) or {all|channel: level}."""
    raw = (cfg or {}).get("min_level")
    if isinstance(raw, str):
        raw = {"all": raw}
    return {t: v for t, v in (raw or {}).items() if t in TARGETS and v in LEVELS} if isinstance(raw, dict) else {}


def presence_policy(cfg, prefs):
    """Presence settings: defaults < fleet.yaml alerting.presence < prefs (reduce, min_level)."""
    pol = dict(PRESENCE_DEFAULTS)
    raw = (cfg or {}).get("presence")
    if isinstance(raw, dict):
        if isinstance(raw.get("reduce"), bool):
            pol["reduce"] = raw["reduce"]
        if raw.get("min_level") in LEVELS:
            pol["min_level"] = raw["min_level"]
        for k in ("window_seconds", "idle_seconds"):
            try:
                pol[k] = max(0, int(raw.get(k, pol[k])))
            except (TypeError, ValueError):
                pass
    pol.update(prefs.get("presence") or {})
    return pol


def floor(target, cfg, prefs):
    """(level, source) for a target's own floor."""
    if target in prefs["min_level"]:
        return prefs["min_level"][target], "prefs"
    conf = config_floors(cfg)
    if target in conf:
        return conf[target], "config"
    return "info", "default"


def min_level_for(channel, cfg, prefs):
    a, c = floor("all", cfg, prefs)[0], floor(channel, cfg, prefs)[0]
    return a if rank(a) >= rank(c) else c


def present(cfg, presence, now, approvers=None):
    """Identities watching the board now: a visible page, a heartbeat within window_seconds and an
    interaction within idle_seconds (0 = no idle limit). With `approvers`, only those count."""
    pol = presence_policy(cfg, {"presence": {}})
    win, idle = pol["window_seconds"] * 1000, pol["idle_seconds"] * 1000
    allowed = {str(a).strip().lower() for a in approvers} if approvers is not None else None
    out = []
    for ident, p in sorted(presence.items()):
        if allowed is not None and ident.strip().lower() not in allowed:
            continue
        at, seen = p.get("at"), p.get("interaction_at")
        if p.get("visible") is not True or not isinstance(at, int) or now - at > win or at > now + 60000:
            continue
        if idle and (not isinstance(seen, int) or now - seen > idle):
            continue
        out.append(ident)
    return out


def suppression(channel, level, cfg, prefs, watching, now):
    """None when CHANNEL may deliver an alert at LEVEL, else the reason: "muted:<target>",
    "below_min_level" or "owner_present". `watching` = present() is not empty."""
    mutes = active_mutes(prefs, now)
    for t in ("all", channel):
        m = mutes.get(t)
        if m and (level != BREAKTHROUGH or m["include_urgent"]):
            return "muted:%s" % t
    if rank(level) < rank(min_level_for(channel, cfg, prefs)):
        return "below_min_level"
    pol = presence_policy(cfg, prefs)
    if watching and pol["reduce"] and rank(level) < rank(pol["min_level"]):
        return "owner_present"
    return None


def filter_channels(channels, level, cfg, now, home=None, approvers=None):
    """(allowed, {channel: reason}) for a delivery. Never raises: on any error it fails open
    (everything allowed), so a broken prefs file can't silence alerting."""
    try:
        if not channels:
            return list(channels), {}
        prefs = load(home)
        watching = bool(present(cfg, load_presence(home), now, approvers))
        held = {}
        for c in channels:
            why = suppression(c, level, cfg, prefs, watching, now)
            if why:
                held[c] = why
        return [c for c in channels if c not in held], held
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("notify prefs: %s (delivering anyway)\n" % exc)
        return list(channels), {}


def status(cfg, now, home=None, approvers=None, with_present=True):
    """The effective state: what `board mute status --json`, `board-notify status` and the board
    page show. Never contains channel credentials."""
    prefs = load(home)
    mutes = active_mutes(prefs, now)
    floors = {t: dict(zip(("level", "source"), floor(t, cfg, prefs))) for t in TARGETS}
    eff = {c: min_level_for(c, cfg, prefs) for c in CHANNELS}
    pol = presence_policy(cfg, prefs)
    out = {"mutes": mutes, "min_level": floors, "effective_min_level": eff,
           "presence": {k: pol[k] for k in ("reduce", "min_level", "window_seconds", "idle_seconds")},
           "breakthrough": BREAKTHROUGH, "updated_at": prefs.get("updated_at"),
           "updated_by": prefs.get("updated_by")}
    if with_present:
        out["presence"]["present"] = present(cfg, load_presence(home), now, approvers)
    return out


# ------------------------------------------------------------------ writing

def parse_for(spec):
    m = FOR_RE.fullmatch(str(spec or "").strip())
    if not m:
        raise UsageError("--for takes a number and m, h or d (30m, 1h, 4h, 2d): %r" % spec)
    ms = int(m.group(1)) * {"m": 60000, "h": 3600000, "d": 86400000}[m.group(2)]
    if ms > MAX_MUTE_MS:
        raise UsageError("--for is at most 30 days")
    return ms


def parse_until(spec, now):
    """HH:MM (the next such local time) or an ISO-8601 time (naive = local) -> epoch ms."""
    s = str(spec or "").strip()
    m = HHMM_RE.fullmatch(s)
    if m:
        base = dt.datetime.fromtimestamp(now / 1000)
        t = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t.timestamp() * 1000 <= now:
            t += dt.timedelta(days=1)
        ms = int(t.timestamp() * 1000)
    else:
        try:
            ms = int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            raise UsageError("--until takes HH:MM or an ISO-8601 time: %r" % spec) from None
    if ms <= now:
        raise UsageError("--until is in the past")
    if ms - now > MAX_MUTE_MS:
        raise UsageError("--until is at most 30 days ahead")
    return ms


def _check_by(by):
    if not BY_RE.fullmatch(by or ""):
        raise UsageError("--by must be a session code or board:<identity> (letters, digits, @ . _ + : -)")
    return by


def _prune(data, now):
    mutes = data.get("mutes") if isinstance(data.get("mutes"), dict) else {}
    data["mutes"] = {t: m for t, m in mutes.items()
                     if isinstance(m, dict) and _valid_until(m.get("until")) and (m.get("until") is None or m["until"] > now)}


def _change(home, by, now, fn):
    with Locked(home, PREFS_FILE) as st:
        data = normalize(st.data)
        _prune(data, now)
        result = fn(data)
        data["version"] = 1
        data["updated_at"], data["updated_by"] = now, by
        st.data = data
    return result


def mute(home, target, until, by, include_urgent=False, now=None):
    if target not in TARGETS:
        raise UsageError("unknown channel %r (%s)" % (target, ", ".join(TARGETS)))
    _check_by(by)
    now = now if now is not None else now_ms()
    rec = {"until": until, "by": by, "at": now, "include_urgent": bool(include_urgent)}
    _change(home, by, now, lambda d: d["mutes"].__setitem__(target, rec))
    _log(home, {"ts": now, "type": "notify-prefs", "action": "mute", "target": target, "until": until,
                "include_urgent": bool(include_urgent), "by": by})
    return rec


def unmute(home, target, by, now=None):
    """Removes TARGET's mute ("all" removes every mute). Returns the targets unmuted."""
    if target not in TARGETS:
        raise UsageError("unknown channel %r (%s)" % (target, ", ".join(TARGETS)))
    _check_by(by)
    now = now if now is not None else now_ms()

    def fn(d):
        gone = sorted(d["mutes"]) if target == "all" else ([target] if target in d["mutes"] else [])
        for t in gone:
            d["mutes"].pop(t, None)
        return gone
    gone = _change(home, by, now, fn)
    _log(home, {"ts": now, "type": "notify-prefs", "action": "unmute", "target": target, "removed": gone, "by": by})
    return gone


def set_level(home, target, level, by, now=None):
    """level None (or "default") removes the prefs floor; fleet.yaml / the default applies again."""
    if target not in TARGETS:
        raise UsageError("unknown channel %r (%s)" % (target, ", ".join(TARGETS)))
    if level == "default":
        level = None
    if level is not None and level not in LEVELS:
        raise UsageError("level must be one of %s or default" % ", ".join(LEVELS))
    _check_by(by)
    now = now if now is not None else now_ms()

    def fn(d):
        if level is None:
            d["min_level"].pop(target, None)
        else:
            d["min_level"][target] = level
    _change(home, by, now, fn)
    _log(home, {"ts": now, "type": "notify-prefs", "action": "level", "target": target, "level": level, "by": by})


def set_presence(home, reduce, level, by, now=None):
    if level is not None and level not in LEVELS[1:]:
        raise UsageError("--level must be warn, high or urgent")
    _check_by(by)
    now = now if now is not None else now_ms()

    def fn(d):
        d["presence"]["reduce"] = bool(reduce)
        if level is not None:
            d["presence"]["min_level"] = level
    _change(home, by, now, fn)
    _log(home, {"ts": now, "type": "notify-prefs", "action": "presence", "reduce": bool(reduce),
                "min_level": level, "by": by})


def record_presence(home, ident, visible, interaction_at, via, now=None):
    """A heartbeat from the board page of IDENTITY (board-serve's verified identity). Returns
    True when the visible state changed (worth an audit line), False for a routine heartbeat."""
    now = now if now is not None else now_ms()
    seen = interaction_at if isinstance(interaction_at, int) and not isinstance(interaction_at, bool) else None
    if seen is not None:
        seen = min(seen, now)  # never in the future
    with Locked(home, PRESENCE_FILE) as st:
        data = {k: v for k, v in st.data.items()
                if isinstance(v, dict) and isinstance(v.get("at"), int) and now - v["at"] < PRESENCE_KEEP_MS}
        prev = data.get(ident) or {}
        data[ident] = {"at": now, "visible": bool(visible), "interaction_at": seen, "via": via}
        if len(data) > PRESENCE_MAX:
            for k in sorted(data, key=lambda k: data[k]["at"])[: len(data) - PRESENCE_MAX]:
                data.pop(k, None)
        st.data = data
    return prev.get("visible") is not bool(visible)


# ------------------------------------------------------------------ CLI

def fmt_until(ms, now):
    if ms is None:
        return "until unmuted"
    t = dt.datetime.fromtimestamp(ms / 1000)
    same_day = t.date() == dt.datetime.fromtimestamp(now / 1000).date()
    return "until " + t.strftime("%H:%M" if same_day else "%a %d %b %H:%M")


def status_lines(s, now):
    lines = []
    if s["mutes"]:
        for t, m in sorted(s["mutes"].items()):
            lines.append("muted: %s %s (by %s)%s" % (t, fmt_until(m["until"], now), fleet.safe_text(m["by"]),
                                                     "; urgent too" if m["include_urgent"] else "; urgent breaks through"))
    else:
        lines.append("muted: nothing")
    floors = ["%s=%s (%s)" % (t, v["level"], v["source"]) for t, v in s["min_level"].items() if v["source"] != "default"]
    lines.append("min level: " + (", ".join(floors) if floors else "info everywhere (default)"))
    lines.append("effective: " + " ".join("%s>=%s" % (c, lvl) for c, lvl in s["effective_min_level"].items()))
    p = s["presence"]
    line = "presence: %s" % ("reduce push to %s+ while an approver watches the board" % p["min_level"]
                             if p["reduce"] else "off (push is not reduced while you watch)")
    if "present" in p:
        line += "; watching now: %s" % (", ".join(fleet.safe_text(x) for x in p["present"]) or "nobody")
    lines.append(line)
    return lines


def _approvers():
    try:
        return fleet.id_list(fleet.load_fleet(), "approvers")
    except Exception:  # noqa: BLE001
        return []


def _alert_cfg():
    """The alerting: block (fleet.yaml), for floors and the presence policy."""
    try:
        import notify  # noqa: WPS433 - late import: notify imports this module
        return notify.load_config()
    except Exception:  # noqa: BLE001
        return {}


def main(argv):
    ap = argparse.ArgumentParser(prog="board", description="notification controls")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("mute")
    p.add_argument("target", metavar="CHANNEL|all")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--for", dest="for_", metavar="DURATION")
    g.add_argument("--until", metavar="HH:MM|ISO")
    p.add_argument("--include-urgent", action="store_true")
    p.add_argument("--by", default="cli")
    p = sub.add_parser("unmute")
    p.add_argument("target", metavar="CHANNEL|all")
    p.add_argument("--by", default="cli")
    p = sub.add_parser("mute-status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("level")
    p.add_argument("target", metavar="CHANNEL|all")
    p.add_argument("level", metavar="LEVEL")
    p.add_argument("--by", default="cli")
    p = sub.add_parser("presence")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--level", metavar="LEVEL")
    p.add_argument("--by", default="cli")
    args = ap.parse_args(argv)
    home = fleet.ensure_home()
    now = now_ms()
    try:
        if args.cmd == "mute":
            until = None
            if args.for_ is not None:
                until = now + parse_for(args.for_)
            elif args.until is not None:
                until = parse_until(args.until, now)
            rec = mute(home, args.target, until, args.by, args.include_urgent, now)
            print("muted %s %s%s" % (args.target, fmt_until(rec["until"], now),
                                     " (urgent too)" if rec["include_urgent"] else " (urgent still breaks through)"))
            return 0
        if args.cmd == "unmute":
            gone = unmute(home, args.target, args.by, now)
            print("unmuted %s" % (", ".join(gone) if gone else "nothing (%s was not muted)" % args.target))
            m = active_mutes(load(home), now)
            if args.target != "all" and "all" in m:
                print("note: everything is still muted by 'mute all' (board unmute all)", file=sys.stderr)
            return 0
        if args.cmd == "level":
            set_level(home, args.target, args.level, args.by, now)
            s = status(_alert_cfg(), now, home, with_present=False)
            print("min level %s: %s; effective: %s" % (args.target, args.level, " ".join(
                "%s>=%s" % (c, lvl) for c, lvl in s["effective_min_level"].items())))
            return 0
        if args.cmd == "presence":
            set_presence(home, args.state == "on", args.level, args.by, now)
            pol = presence_policy(_alert_cfg(), load(home))
            print("presence reduction %s%s" % (args.state, " (push below %s is held while an approver watches)"
                                                % pol["min_level"] if args.state == "on" else ""))
            return 0
        if args.cmd == "mute-status":
            s = status(_alert_cfg(), now, home, _approvers())
            if args.json:
                print(json.dumps(s, ensure_ascii=False, sort_keys=True))
            else:
                print("\n".join(status_lines(s, now)))
            return 0
    except UsageError as exc:
        print("board: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("board: %s" % exc, file=sys.stderr)
        return 1
    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
