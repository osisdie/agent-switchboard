#!/usr/bin/env python3
"""availability: the owner's availability for askers (docs/contract.md, "Owner availability").

Three states, as the board shows them (the profile menu and the chip on the profile button):

  online  the owner takes requests (no file);
  dnd     do not disturb: the owner is present (NOT away: no consensus mode), but askers' messages
          and asker-originated work requests are declined with a canned reply. While
          $BOARD_HOME/owner-dnd exists ({since, by[, said]});
  away    owner-away mode ($BOARD_HOME/owner-away, lib/away.py, `board away`), unchanged.

away wins the display when both files exist (the CLI keeps them independent; the page's menu makes
them exclusive). Declines apply whenever owner-dnd exists.

A second, automatic reason declines askers too: compute is short (`compute_low`), from the board's
own usage data: usage.json at most USAGE_STALE_MS old with the 5-hour window at or above
availability.compute.five_hour_pct (90) or the 7-day one at or above availability.compute.seven_day_pct
(95), or an active usage-limit episode (limit.json, not cleared, not past resets_at by
LIMIT_GRACE_MS). Two small file reads: cheap enough for every request.

A third: the owner is away and chose to decline askers meanwhile (lib/asker_route.py away_askers
"decline": reason owner_away).

A decline is canned: board-serve or the CLI stores the message with status "declined"
(declined_reason owner_dnd|owner_away|compute_low), adds a reply from BOARD with the canned text, audits it,
and never relays it (no `message <id> new` line, no token spent). The texts are fleet.yaml
availability.dnd_reply / availability.compute_reply / availability.away_reply (one line, scrubbed, defanged, at most MAX_REPLY
characters), else the bilingual defaults. They never say where the owner is or show usage numbers.

Only askers are affected (an asker who is not an approver or release manager: the page's asker, a
CLI `--by` asker, an API key owned by one). Owners, approvers, release managers and sessions never
are.

CLI (bin/board dnd ...):
  availability.py on --by WHO [--said TEXT]   do not disturb on (an existing record is kept)
  availability.py off --by WHO                do not disturb off
  availability.py status [--quiet|--json]     exit 0 when do-not-disturb is on, 1 when not
                                              (--json prints {state, dnd, declining})
Exit codes: 0 ok, 1 error / not on (status), 2 usage, 6 --said refused by the secret guard.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402

DND_FILE = "owner-dnd"
REASONS = ("owner_dnd", "owner_away", "compute_low")
STATES = ("online", "dnd", "away")
MAX_REPLY = 300
MAX_SAID = 300
MAX_READ = 16 * 1024
USAGE_STALE_MS = 15 * 60 * 1000     # usage.json older than this says nothing (as lib/scheduler.py)
SKEW_MS = 30 * 1000
LIMIT_GRACE_MS = 10 * 60 * 1000     # a limit episode past resets_at by this much no longer counts
DEFAULT_REPLY = {
    "owner_dnd": "The owner isn't taking requests right now. Please try again later. "
                 "/ 目前不接受詢問或派工，請稍後再試。",
    "compute_low": "The board's compute is busy right now. Please try again later. "
                   "/ 目前運算資源忙碌，請稍後再試。",
    "owner_away": "The owner is away and isn't taking requests right now. Please try again later. "
                  "/ 主人不在，目前不接受詢問或派工，請稍後再試。",
}
DEFAULT_COMPUTE = {"enabled": True, "five_hour_pct": 90, "seven_day_pct": 95, "limit": True}
BY_RE = re.compile(r"(board:|cli:)?[A-Za-z0-9][A-Za-z0-9@._+:-]{0,199}")
WS_RE = re.compile(r"\s+")


def now_ms():
    return int(time.time() * 1000)


def _read_json(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_READ)
        v = json.loads(raw.decode("utf-8"))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _line(text, n):
    """One line of free text: control characters and newlines become spaces, scrubbed by the secret
    guard, "[BOARD" defanged, at most n characters."""
    import guard  # noqa: WPS433 - lazy: `status` needs neither
    import messages  # noqa: WPS433
    s = WS_RE.sub(" ", fleet.CONTROL_RE.sub(" ", str(text or ""))).strip()
    s = messages.defang(guard.scrub(s)).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------ state
def dnd_record(home):
    """None when do-not-disturb is off; else {since, by, said}. The file's presence is what counts."""
    path = os.path.join(home, DND_FILE)
    if not os.path.lexists(path):
        return None
    rec = _read_json(path) or {}
    since = _num(rec.get("since"))
    by = rec.get("by") if isinstance(rec.get("by"), str) and BY_RE.fullmatch(rec["by"]) else None
    said = rec.get("said") if isinstance(rec.get("said"), str) else None
    return {"since": int(since) if since else None, "by": by,
            "said": WS_RE.sub(" ", fleet.CONTROL_RE.sub(" ", said))[:MAX_SAID] if said else None}


def state(home):
    """"away" | "dnd" | "online" (away wins when both are set)."""
    if os.path.lexists(os.path.join(home, "owner-away")):
        return "away"
    return "dnd" if os.path.lexists(os.path.join(home, DND_FILE)) else "online"


def _pct(v, default):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 100 else default


def settings(cfg):
    """fleet.yaml `availability`: the canned replies and the compute-low thresholds (bad values fall back
    to the defaults; an empty or unusable reply uses the default text)."""
    p = cfg.get("availability") if isinstance(cfg, dict) and isinstance(cfg.get("availability"), dict) else {}
    c = p.get("compute") if isinstance(p.get("compute"), dict) else {}
    out = {"compute": {"enabled": c.get("enabled") is not False,
                       "five_hour_pct": _pct(c.get("five_hour_pct"), DEFAULT_COMPUTE["five_hour_pct"]),
                       "seven_day_pct": _pct(c.get("seven_day_pct"), DEFAULT_COMPUTE["seven_day_pct"]),
                       "limit": c.get("limit") is not False}}
    for reason, key in (("owner_dnd", "dnd_reply"), ("compute_low", "compute_reply"), ("owner_away", "away_reply")):
        txt = _line(p.get(key), MAX_REPLY) if isinstance(p.get(key), str) else ""
        out[reason] = txt or DEFAULT_REPLY[reason]
    return out


def reply_text(cfg, reason):
    return settings(cfg)[reason]


def limit_active(home, now):
    """An active usage-limit episode: limit.json hit, not cleared, resets_at unknown or not passed by
    more than LIMIT_GRACE_MS."""
    lim = _read_json(os.path.join(home, "limit.json"))
    if lim and not lim.get("cleared_at") and lim.get("hit_at"):
        r = _num(lim.get("resets_at"))
        return r is None or now <= r + LIMIT_GRACE_MS
    return False


def fresh_usage(home, now):
    """usage.json when it was updated within USAGE_STALE_MS (a small clock skew allowed), else None."""
    u = _read_json(os.path.join(home, "usage.json"))
    if not u:
        return None
    at = _num(u.get("updated_at"))
    if at is None or not -SKEW_MS <= now - at <= USAGE_STALE_MS:
        return None
    return u


def compute_low(home, cfg, now=None):
    """None, or why compute is short: "limit" | "5h" | "7d". Never raises."""
    now = now_ms() if now is None else now
    c = settings(cfg)["compute"]
    if not c["enabled"]:
        return None
    if c["limit"] and limit_active(home, now):
        return "limit"
    u = fresh_usage(home, now)
    if not u:
        return None
    for key, name, limit_pct in (("five_hour", "5h", c["five_hour_pct"]), ("seven_day", "7d", c["seven_day_pct"])):
        w = u.get(key) if isinstance(u.get(key), dict) else {}
        pct, resets = _num(w.get("pct")), _num(w.get("resets_at"))
        if pct is not None and pct >= limit_pct and (resets is None or resets > now):
            return name
    return None


def decline_reason(home, cfg, now=None):
    """Why an asker's request is declined now: "owner_dnd", "owner_away" (the owner is away and
    chose to decline askers meanwhile: lib/asker_route.py), "compute_low" or None."""
    if os.path.lexists(os.path.join(home, DND_FILE)):
        return "owner_dnd"
    try:
        import asker_route  # noqa: WPS433 - lazy: asker_route imports this module
        if asker_route.away_mode(home) == "decline":
            return "owner_away"
    except Exception:  # noqa: BLE001
        pass
    return "compute_low" if compute_low(home, cfg, now) else None


def view(home, cfg, owner=False, now=None):
    """The board's `availability` field: {state, declining} for everyone; the dnd record for owners."""
    out = {"state": state(home), "declining": decline_reason(home, cfg, now)}
    if owner:
        out["dnd"] = dnd_record(home)
    return out


# ------------------------------------------------------------------ writes
def _audit(home, ev):
    ev = dict({"ts": now_ms()}, **ev)
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def set_dnd(home, on, by, said=None):
    """Turn do-not-disturb on or off. Returns (changed, record or None). Audited (type "dnd")."""
    path = os.path.join(home, DND_FILE)
    cur = dnd_record(home)
    if on:
        if cur is not None:
            return False, cur
        rec = {"since": now_ms(), "by": by}
        if said:
            rec["said"] = said
        tmp = "%s.tmp.%d" % (path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
        _audit(home, dict({"type": "dnd", "action": "on", "by": by}, **({"said": said} if said else {})))
        return True, rec
    if cur is None:
        return False, None
    os.unlink(path)
    _audit(home, {"type": "dnd", "action": "off", "by": by, "previous": cur})
    return True, None


# ------------------------------------------------------------------ CLI
USAGE = ("usage: board dnd on --by WHO [--said TEXT]\n"
         "       board dnd off --by WHO\n"
         "       board dnd status [--quiet|--json]")


def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        print("  do not disturb: the owner is present (not away), but askers' messages and work requests are\n"
              "  declined with a canned reply (fleet.yaml availability.dnd_reply), never relayed. Owners,\n"
              "  approvers, release managers and sessions are unaffected. `board away` is separate.")
        return 0 if args else 2
    sub, rest = args[0], args[1:]
    home = fleet.ensure_home()
    if sub == "status":
        quiet = js = False
        for a in rest:
            if a == "--quiet":
                quiet = True
            elif a == "--json":
                js = True
            else:
                sys.stderr.write("board dnd: status: unknown option %s\n%s\n" % (a, USAGE))
                return 2
        rec = dnd_record(home)
        if js:
            print(json.dumps(view(home, fleet.load_fleet(), owner=True), ensure_ascii=False))
        elif not quiet:
            print("do not disturb since %s (by %s)" % (
                time.strftime("%Y-%m-%d %H:%M", time.localtime((rec.get("since") or 0) / 1000)),
                rec.get("by") or "?") if rec else "do not disturb is off (%s)" % state(home))
        return 0 if rec else 1
    if sub not in ("on", "off"):
        sys.stderr.write(USAGE + "\n")
        return 2
    by, said, i = None, None, 0
    while i < len(rest):
        a = rest[i]
        if a in ("--by", "--said") and i + 1 < len(rest):
            if a == "--by":
                by = rest[i + 1]
            elif sub == "on":
                said = rest[i + 1]
            else:
                sys.stderr.write("board dnd: off: unknown option --said\n")
                return 2
            i += 2
            continue
        sys.stderr.write("board dnd: %s: unknown option %s\n%s\n" % (sub, a, USAGE))
        return 2
    if not by:
        sys.stderr.write(USAGE + "\n")
        return 2
    if not BY_RE.fullmatch(by):
        sys.stderr.write("board dnd: --by must be a session code or board:<identity> (letters, digits, @ . _ + : -)\n")
        return 2
    if said is not None:
        import guard  # noqa: WPS433
        if len(said) > 2000:
            sys.stderr.write("board dnd: --said is longer than 2000 characters\n")
            return 2
        held = guard.screen(said)
        if held:
            _audit(home, {"type": "guard", "action": "held-said", "by": by, "reason": held, "for": "dnd"})
            sys.stderr.write("board dnd: --said refused by the secret guard (%s); nothing changed\n" % held)
            return 6
        said = _line(said, MAX_SAID) or None
    changed, rec = set_dnd(home, sub == "on", by, said)
    if sub == "on":
        print("do not disturb on (by %s)%s" % (rec.get("by") or "?", "" if changed else "; already on, record kept"))
    else:
        print("do not disturb off (by %s)" % by if changed else "do not disturb was not on; nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
