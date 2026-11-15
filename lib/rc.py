#!/usr/bin/env python3
"""Remote Control (RC) on/off for the resident sessions: the owner's choice per session or for
all of them, used by fleet-up at (re)start and applied to running sessions when they are idle
(docs/contract.md, "Remote Control"). Python stdlib only.

Usage (bin/board runs these; exit codes below):
  rc.py set on|off CODE|all [--by WHO] [--no-apply]
                         store the choice ("all" replaces every per-session choice), then apply
                         it to the running sessions it covers (unless --no-apply)
  rc.py clear CODE|all [--by WHO]
                         forget the choice (CODE: back to "all"/fleet.yaml; all: forget all)
  rc.py status [--json]  per session: wanted, actual, pending, last attempt
  rc.py apply [CODE...|--pending] [--auto]
                         apply the stored choice now (lib/actions.py apply --only rc)
  rc.py want CODE        print on|off: should fleet-up pass --remote-control

State:
  $BOARD_HOME/rc-prefs.json  {version, all: {state, by, at}?, sessions: {CODE: {state, by, at}},
                              updated_at, updated_by}            (0600, flock rc-prefs.lock)
  events.jsonl               {"type": "rc", ...} for every change of the choice

What a session wants: its own choice, else the "all" choice, else fleet.yaml (the session's
remote_control, then the top-level remote_control), else on. Only a choice made with `board rc`
(its own or "all") is applied to a running session; fleet.yaml alone only decides fleet-up.

Applying it to a running session is the session action rc_on / rc_off (lib/actions.py: typed
`/remote-control` into an idle session's empty prompt box, with the press-style checks). A
session whose process has DO_NOT_TRACK set cannot get Remote Control at runtime ("blocked").

Exit codes: those of lib/actions.py (0 ok, also deferred / already; 2 usage; 3 refused, nothing
sent; 5 partial; 6 not confirmed / unavailable).
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402
import prefs  # noqa: E402

PREFS_FILE = "rc-prefs.json"
STATES = ("on", "off")
CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class UsageError(ValueError):
    """A bad argument (exit 2)."""


def now_ms():
    return prefs.now_ms()


def log(home, ev):
    ev = dict({"ts": now_ms(), "type": "rc"}, **ev)
    prefs._log(home, ev)  # noqa: SLF001 - the shared events.jsonl writer


# ------------------------------------------------------------------ the owner's choice

def _entry(v):
    return v if isinstance(v, dict) and v.get("state") in STATES else None


def load(home=None):
    """rc-prefs.json, normalized: {all: entry|None, sessions: {CODE: entry}}."""
    data = prefs._read(os.path.join(home or fleet.board_home(), PREFS_FILE))  # noqa: SLF001
    sess = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    return {"all": _entry(data.get("all")),
            "sessions": {str(k): e for k, e in sess.items() if CODE_RE.fullmatch(str(k)) and _entry(e)},
            "updated_at": data.get("updated_at"), "updated_by": data.get("updated_by")}


def _check(target, by):
    if target != "all" and not CODE_RE.fullmatch(target or ""):
        raise UsageError("CODE must be a session code (letters, digits, _ . -) or all")
    if not prefs.BY_RE.fullmatch(by or ""):
        raise UsageError("--by must be a session code or board:<identity> (letters, digits, @ . _ + : -)")


def set_choice(home, target, state, by, now=None):
    """Stores the choice. "all" replaces every per-session choice. Returns the codes whose
    choice was replaced by "all" (empty for a single session)."""
    if state not in STATES:
        raise UsageError("state must be on or off")
    _check(target, by)
    now = now if now is not None else now_ms()
    rec = {"state": state, "by": by, "at": now}
    with prefs.Locked(home, PREFS_FILE) as st:
        d = st.data
        sess = d.get("sessions") if isinstance(d.get("sessions"), dict) else {}
        replaced = []
        if target == "all":
            replaced = sorted(sess)
            d["all"], d["sessions"] = rec, {}
        else:
            sess[target] = rec
            d["sessions"] = sess
        d["version"], d["updated_at"], d["updated_by"] = 1, now, by
    log(home, {"action": "set", "code": target, "want": state, "by": by,
               "replaced": replaced or None})
    return replaced


def clear_choice(home, target, by, now=None):
    _check(target, by)
    now = now if now is not None else now_ms()
    with prefs.Locked(home, PREFS_FILE) as st:
        d = st.data
        sess = d.get("sessions") if isinstance(d.get("sessions"), dict) else {}
        if target == "all":
            had = bool(d.get("all")) or bool(sess)
            d["all"], d["sessions"] = None, {}
        else:
            had = target in sess
            sess.pop(target, None)
            d["sessions"] = sess
        d["version"], d["updated_at"], d["updated_by"] = 1, now, by
    log(home, {"action": "clear", "code": target, "by": by})
    return had


def wanted(code, cfg, data):
    """(on|off, source): source is session, all (board rc), fleet (fleet.yaml) or default."""
    e = data["sessions"].get(code)
    if e:
        return e["state"], "session"
    if data["all"]:
        return data["all"]["state"], "all"
    for s in cfg.get("sessions") or []:
        if s.get("code") == code and isinstance(s.get("remote_control"), bool):
            return ("on" if s["remote_control"] else "off"), "fleet"
    top = cfg.get("remote_control")
    if isinstance(top, bool):
        return ("on" if top else "off"), "fleet"
    return "on", "default"


def explicit(source):
    return source in ("session", "all")


# ------------------------------------------------------------------ the board's view

def annotate(sessions, cfg, home, now=None):
    """Adds `rc` to every snapshot row: {on: bool|None (None: not running), want, source,
    blocked: "do_not_track"|None (the process has DO_NOT_TRACK set: no Remote Control at
    runtime), pending (an explicit choice the running session does not follow yet and can
    get), last: the last rc_on/rc_off attempt for this session process, or None}."""
    import actions  # noqa: WPS433 - actions imports this module lazily
    data = load(home)
    last_all = actions.load_last(home)
    for r in sessions:
        code = str(r.get("code") or "")
        want, source = wanted(code, cfg, data)
        running = bool(r.get("session_id"))
        on = bool(r.get("remote_url")) if running else None
        blocked = "do_not_track" if running and not on and fleet.env_has(r.get("pid"), "DO_NOT_TRACK") else None
        rec = (last_all.get(code) or {}).get("rc_" + want)
        last = None
        if rec and running and rec.get("sid") == r.get("session_id"):
            last = {"want": want, "result": rec.get("result"), "reason": rec.get("reason"), "at": rec.get("at")}
        pending = bool(running and explicit(source) and "/" not in code and on != (want == "on"))
        if blocked and want == "on":
            pending = False
        r["rc"] = {"on": on, "want": want, "source": source, "blocked": blocked, "pending": pending, "last": last}
    return sessions


def page_state(home, cfg=None):
    """The board page's view of the choices (rc-prefs.json): {all, sessions: {CODE: on|off}}."""
    data = load(home)
    return {"all": data["all"]["state"] if data["all"] else None,
            "sessions": {c: e["state"] for c, e in sorted(data["sessions"].items())},
            "updated_at": data.get("updated_at")}


def status(home, cfg):
    import actions  # noqa: WPS433
    rows = actions.snapshot_rows(home)
    annotate(rows, cfg, home)
    data = load(home)
    known = {r["code"] for r in rows}
    out = {"all": data["all"], "sessions": [], "do_not_track": bool(os.environ.get("DO_NOT_TRACK"))}
    for r in rows:
        rc = r["rc"]
        out["sessions"].append({"code": r["code"], "state": r.get("state"), "running": bool(r.get("session_id")),
                                "on": rc["on"], "want": rc["want"], "source": rc["source"],
                                "pending": rc["pending"], "blocked": rc["blocked"], "last": rc["last"]})
    for code, e in sorted(data["sessions"].items()):
        if code not in known:
            out["sessions"].append({"code": code, "state": None, "running": False, "on": None, "want": e["state"],
                                    "source": "session", "pending": False, "blocked": None, "last": None})
    return out


def status_lines(s):
    a = s["all"]
    lines = ["all: %s" % ("%s (by %s)" % (a["state"], fleet.safe_text(a.get("by"))) if a else "not set (fleet.yaml, else on)")]
    for r in s["sessions"]:
        actual = "offline" if r["on"] is None else ("on" if r["on"] else "off")
        line = "%-16s actual %-7s want %-3s (%s)" % (fleet.safe_text(r["code"]), actual, r["want"], r["source"])
        if r["pending"]:
            line += "  pending: applies when idle"
        if r.get("blocked"):
            line += "  unavailable: DO_NOT_TRACK is set in that session"
        if r["last"]:
            line += "  last: %s" % fleet.safe_text(r["last"].get("result"))
        lines.append(line)
    if s.get("do_not_track"):
        lines.append("note: DO_NOT_TRACK is set here; Claude Code ignores --remote-control and hides /remote-control "
                     "in sessions that inherit it")
    return lines


def main(argv):
    ap = argparse.ArgumentParser(prog="board rc", description="Remote Control on/off")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("set")
    p.add_argument("state", choices=STATES)
    p.add_argument("target", metavar="CODE|all")
    p.add_argument("--by", default="cli")
    p.add_argument("--no-apply", action="store_true")
    p = sub.add_parser("clear")
    p.add_argument("target", metavar="CODE|all")
    p.add_argument("--by", default="cli")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("apply")
    p.add_argument("codes", nargs="*", metavar="CODE")
    p.add_argument("--pending", action="store_true")
    p.add_argument("--auto", action="store_true")
    p.add_argument("--by", default=None)
    p = sub.add_parser("want")
    p.add_argument("code")
    args = ap.parse_args(argv)
    import actions  # noqa: WPS433
    home = fleet.ensure_home()
    cfg = fleet.load_fleet()
    try:
        if args.cmd == "want":
            print(wanted(args.code, cfg, load(home))[0])
            return 0
        if args.cmd == "status":
            s = status(home, cfg)
            print(json.dumps(s, ensure_ascii=False, sort_keys=True) if args.json else "\n".join(status_lines(s)))
            return 0
        if args.cmd == "clear":
            had = clear_choice(home, args.target, args.by)
            print("cleared %s" % args.target if had else "nothing to clear for %s" % args.target)
            return 0
        if args.cmd == "set":
            replaced = set_choice(home, args.target, args.state, args.by)
            print("Remote Control %s: %s%s" % (args.target, args.state,
                                               " (replaces the choice for %s)" % ", ".join(replaced) if replaced else ""))
            if args.no_apply:
                return 0
            codes = None if args.target == "all" else [args.target]
            results = actions.apply(home, cfg, codes, by=args.by, only="rc")
            for code, action, res, reason in results:
                note = " (will apply when idle)" if res == "deferred" else ""
                print("  %s: %s (%s)%s" % (fleet.safe_text(code), res, fleet.safe_text(reason), note))
            return actions.exit_for(results)
        if args.cmd == "apply":
            for c in args.codes:
                _check(c, "cli")
            if args.codes and args.pending:
                raise UsageError("give CODEs or --pending, not both")
            try:
                results = actions.apply(home, cfg, args.codes or None, auto=args.auto, by=args.by, only="rc")
            except OSError:
                if args.auto:
                    return 0  # another applier runs; the collector tries again later
                raise
            for code, action, res, reason in results:
                print("%s: %s %s (%s)" % (fleet.safe_text(code), action, res, fleet.safe_text(reason)))
            return 0 if args.auto else actions.exit_for(results)
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
