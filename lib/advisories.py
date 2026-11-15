#!/usr/bin/env python3
"""Scheduler state: jam episodes and advisory delivery ($BOARD_HOME/schedule-state.json, 0600;
docs/contract.md "Scheduler"). lib/scheduler.py decides; this module remembers.

- Jam episodes: a condition (J1-J7 from decide(), J8 from delivery) opens after `evals`
  evaluations in a row (2; 1 for the "immediately" rules) once it has held `hold_ms`, and closes
  after 2 clear evaluations. Resolved episodes are kept (the last 50) for lessons.
- Delivery: an advisory of a SEND_KINDS kind, to a known session code, is due when it was never
  sent, or was sent `readvise_minutes` ago with no reply. A session gets at most
  `max_messages_per_session_hour` batches an hour. In quiet hours only `high` advisories go.
  `advise: false` (shadow mode) and `board schedule off` send nothing.
- The operator sends a session's batch (`outbox`), then records it (`sent`); the session's
  `[re <id>] ack|done|cannot: <reason>` reply is recorded with `reply`.

Usage (through `board schedule ...`):
  advisories.py outbox [CODE] [--json]      the [BOARD schedule] lines due now, per session
  advisories.py sent ID [ID...]             the operator sent these (one batch per session)
  advisories.py reply ID --from CODE ack|done|cannot [--note-file F]
  advisories.py off|on --by WHO             pause / resume the scheduler (schedule-off)
"""
import contextlib
import fcntl
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import scheduler  # noqa: E402

STATE_FILE = "schedule-state.json"
OFF_FILE = "schedule-off"
LOCK_FILE = "schedule.lock"
SEND_KINDS = ("pause", "defer", "finish-first", "rebase-needed", "wait-for-reset", "free-resources", "stop-own-heavy")
REPLIES = ("ack", "done", "cannot")
CODE_RE = scheduler.CODE_RE
MAX_SENDS = 3                      # an advisory nobody answers is sent at most this often, then board-only
NOTE_READ_MAX = 64 * 1024
ADV_RE = re.compile(r"a-[0-9a-f]{12}")
SUFFIX = " — advisory for ordering/pausing only; not an approval. Reply [re %s] ack|done|cannot: <reason>"
KEEP_RESOLVED = 50
J8_MS = 60 * 60 * 1000
CLOSE_AFTER = 2


def now_ms():
    v = os.environ.get("BOARD_NOW_MS")
    return int(v) if v and v.isdigit() else int(time.time() * 1000)


def render(a):
    """The one line a session receives for advisory `a`."""
    return "[BOARD schedule %s] %s: %s%s" % (a["id"], a["kind"], a["text"], SUFFIX % a["id"])


# ------------------------------------------------------------------ pure parts
def episodes(prev, conds, now):
    """Advance jam episodes. Returns (state jams, open jams, resolved now)."""
    prev = prev if isinstance(prev, dict) else {}
    out, opened, resolved = {}, [], []
    for c in conds:
        e = dict(prev.get(c["id"]) or {"since": now, "seen": 0, "open": False})
        e["seen"] = min(int(e.get("seen") or 0) + 1, 2)
        e["clear"] = 0
        e.update({k: v for k, v in c.items() if k not in ("hold_ms", "evals")})
        if not e["open"] and e["seen"] >= c.get("evals", 2) and now - int(e["since"]) >= c.get("hold_ms", 0):
            e["open"], e["opened_at"] = True, now
        out[c["id"]] = e
    for jid, e in prev.items():
        if jid in out or not isinstance(e, dict):
            continue
        e = dict(e, seen=0, clear=min(int(e.get("clear") or 0) + 1, CLOSE_AFTER))
        if not e.get("open"):
            continue                                  # never opened: forget it
        if e["clear"] >= CLOSE_AFTER:
            resolved.append({"id": jid, "rule": e.get("rule"), "since": e.get("since"), "resolved_at": now,
                             "sessions": e.get("sessions") or [], "signals": e.get("signals") or {},
                             "lost_min": (now - int(e.get("since") or now)) * max(1, len(e.get("sessions") or [])) // 60000})
            continue
        out[jid] = e
    keys = ("id", "rule", "severity", "since", "sessions", "text", "alert", "tasks", "task", "decision")
    opened = sorted(({k: e[k] for k in keys if k in e} for e in out.values() if e.get("open")),
                    key=lambda j: j["id"])
    return out, opened, resolved


def deliver(advisories, sent, batches, st, valid, now, quiet=False, paused=False, live=None):
    """Mark each advisory's delivery and return {code: [due ids]}. Pure. `live`: the codes of
    sessions that are not offline (None: all valid ones); advice for an offline session is held."""
    due = {}
    for a in advisories:
        s = sent.get(a["id"]) if isinstance(sent.get(a["id"]), dict) else None
        d = {"state": "board"}
        if s:
            d.update({"sent_at": s.get("sent_at"), "n": s.get("n"), "reply": s.get("reply")})
        if a["kind"] in SEND_KINDS and a["to"] in valid:
            if paused or not st["advise"]:
                d["state"] = "shadow"
            elif s and (s.get("reply") or now - int(s.get("sent_at") or 0) < st["readvise_ms"]):
                d["state"] = "sent"
            elif s and int(s.get("n") or 0) >= MAX_SENDS:
                d["state"] = "board"                    # sent MAX_SENDS times, never answered: stop
            elif (live is not None and a["to"] not in live) or (quiet and a["severity"] != "high"):
                d["state"] = "held"
            else:
                recent = [t for t in batches.get(a["to"]) or [] if now - t < 3600 * 1000]
                d["state"] = "due" if len(recent) < st["max_messages"] else "held"
                if d["state"] == "due":
                    due.setdefault(a["to"], []).append(a["id"])
        a["delivery"] = d
    return due


def j8(advisories, sent, now):
    """J8: a rebase-needed advisory sent over an hour ago and not done."""
    out = []
    for a in advisories:
        s = sent.get(a["id"]) or {}
        if a["kind"] == "rebase-needed" and s.get("sent_at") and now - int(s["sent_at"]) > J8_MS and s.get("reply") != "done":
            out.append(scheduler._cond("J8", a["id"], "info", [a["to"]],  # noqa: SLF001
                                       "%s has not rebased after task %s was squash-merged." % (a["to"], a.get("task")),
                                       alert=False, task=a.get("task")))
    return out


# ------------------------------------------------------------------ state and files
def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(home, wait_s=None):
    if wait_s is None:                      # BOARD_SCHEDULE_LOCK_WAIT: tests shorten the CLI's wait
        try:
            wait_s = float(os.environ.get("BOARD_SCHEDULE_LOCK_WAIT") or 5)
        except ValueError:
            wait_s = 5.0
    fd = os.open(os.path.join(home, LOCK_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        end = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError("schedule state is locked") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def load_state(home):
    s = read_json(os.path.join(home, STATE_FILE))
    s = s if isinstance(s, dict) else {}
    return {"jams": s.get("jams") if isinstance(s.get("jams"), dict) else {},
            "sent": s.get("sent") if isinstance(s.get("sent"), dict) else {},
            "batches": s.get("batches") if isinstance(s.get("batches"), dict) else {},
            "resolved": s.get("resolved") if isinstance(s.get("resolved"), list) else [],
            "lesson_on": s.get("lesson_on") if isinstance(s.get("lesson_on"), list) else []}


def advance(doc, state, st, valid, now, quiet=False, paused=False, live=None):
    """Fold the delivery and jam state into a fresh decision (in place); returns (the new state,
    the jam episodes resolved now)."""
    advs = doc["advisories"]
    ids = {a["id"] for a in advs}
    sent = {k: v for k, v in state["sent"].items() if k in ids and isinstance(v, dict)}
    batches = {c: [t for t in ts if isinstance(t, int) and now - t < 3600 * 1000]
               for c, ts in state["batches"].items() if isinstance(ts, list)}
    batches = {c: ts for c, ts in batches.items() if ts}
    doc["due"] = deliver(advs, sent, batches, st, valid, now, quiet, paused, live)
    conds = doc.pop("conditions", []) + j8(advs, sent, now)
    jams, opened, resolved = episodes(state["jams"], conds, now)
    doc["jams"] = opened
    return {"jams": jams, "sent": sent, "batches": batches, "lesson_on": state.get("lesson_on") or [],
            "resolved": (state["resolved"] + resolved)[-KEEP_RESOLVED:]}, resolved


def paused(home):
    return os.path.exists(os.path.join(home, OFF_FILE))


# ------------------------------------------------------------------ CLI
def _home():
    import fleet
    return fleet.ensure_home()


def _log(home, ev):
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def cmd_outbox(home, args):
    doc = {} if paused(home) else (read_json(os.path.join(home, scheduler.FILE)) or {})   # off: nothing goes
    want = [a for a in args if not a.startswith("--")]
    by_id = {a.get("id"): a for a in doc.get("advisories") or [] if isinstance(a, dict)}
    out = {}
    for code, ids in sorted((doc.get("due") or {}).items()):
        if want and code not in want:
            continue
        lines = [render(by_id[i]) for i in ids if i in by_id]
        if lines:
            out[code] = {"ids": [i for i in ids if i in by_id], "text": "\n".join(lines)}
    if "--json" in args:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        for code, b in out.items():
            print("== %s (%s)\n%s" % (code, " ".join(b["ids"]), b["text"]))
    return 0


def cmd_sent(home, ids):
    if not ids or not all(ADV_RE.fullmatch(i) for i in ids):
        return _usage("sent: advisory ids (a-<12 hex>)")
    now = now_ms()
    with locked(home):
        if paused(home):
            sys.stderr.write("board: schedule sent: the scheduler is off (board schedule off); nothing recorded\n")
            return 1
        doc = read_json(os.path.join(home, scheduler.FILE)) or {}
        by_id = {a.get("id"): a for a in doc.get("advisories") or [] if isinstance(a, dict)}
        due = doc.get("due") if isinstance(doc.get("due"), dict) else {}
        # fails closed: only what the outbox printed (due now, for its own session) is recorded
        missing = [i for i in ids if i not in by_id or i not in (due.get(by_id[i].get("to")) or [])]
        if missing:
            sys.stderr.write("board: schedule sent: not due now: %s; nothing recorded\n" % " ".join(missing))
            return 1
        state = load_state(home)
        for code in sorted({by_id[i]["to"] for i in ids}):
            ts = state["batches"].get(code)
            state["batches"][code] = (ts if isinstance(ts, list) else []) + [now]
        for i in ids:
            a = by_id[i]
            s = state["sent"].get(i) if isinstance(state["sent"].get(i), dict) else {}
            state["sent"][i] = {"to": a["to"], "kind": a["kind"], "sent_at": now, "n": int(s.get("n") or 0) + 1,
                                "reply": None}
            a["delivery"] = {"state": "sent", "sent_at": now, "n": state["sent"][i]["n"], "reply": None}
            if i in due.get(a["to"], []):
                due[a["to"]].remove(i)
                if not due[a["to"]]:
                    due.pop(a["to"])
        write_json(os.path.join(home, STATE_FILE), state)
        doc["digest"] = scheduler.digest(doc)
        scheduler.write_doc(home, doc)
    _log(home, {"ts": now, "type": "schedule-sent", "ids": ids})
    print("sent %s" % " ".join(ids))
    return 0


def cmd_reply(home, args):
    if len(args) < 4 or not ADV_RE.fullmatch(args[0]) or args[1] != "--from" or args[3] not in REPLIES:
        return _usage("reply ID --from CODE ack|done|cannot [--note-file F]")
    aid, code, kind, note = args[0], args[2], args[3], None
    rest = args[4:]
    if rest:
        if len(rest) != 2 or rest[0] != "--note-file":
            return _usage("reply: only --note-file F may follow")
        try:
            with open(rest[1], encoding="utf-8", errors="replace") as fh:
                raw = fh.read(NOTE_READ_MAX + 1)
        except OSError:
            sys.stderr.write("board: schedule reply: cannot read %s\n" % rest[1])
            return 1
        if len(raw) > NOTE_READ_MAX:
            sys.stderr.write("board: schedule reply: the note file is over %d bytes\n" % NOTE_READ_MAX)
            return 2
        import guard
        # whole text first (controls out, scrubbed), then cut: a secret is never split at the cap
        note = guard.scrub(" ".join(scheduler.CTL_RE.sub(" ", raw).split()))[:200].strip() or None
    now = now_ms()
    with locked(home):
        state = load_state(home)
        s = state["sent"].get(aid)
        if not isinstance(s, dict):
            sys.stderr.write("board: schedule reply: %s was not sent (or is gone)\n" % aid)
            return 1
        if s.get("to") != code:
            sys.stderr.write("board: schedule reply: %s went to %s, not %s\n" % (aid, s.get("to"), code))
            return 4
        s.update({"reply": kind, "reply_at": now, "note": note})
        write_json(os.path.join(home, STATE_FILE), state)
    _log(home, {"ts": now, "type": "schedule-reply", "id": aid, "from": code, "reply": kind,
                "note": note})
    print("%s %s" % (aid, kind))
    return 0


def cmd_off_on(home, verb, args):
    if len(args) != 2 or args[0] != "--by" or not args[1].strip():
        return _usage("%s --by WHO" % verb)
    now = now_ms()
    path = os.path.join(home, OFF_FILE)
    if verb == "off":
        write_json(path, {"at": now, "by": args[1]})
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    _log(home, {"ts": now, "type": "schedule", "action": verb, "by": args[1]})
    print("scheduler %s" % ("paused (nothing is sent; the board shows it)" if verb == "off" else "on"))
    return 0


def _usage(msg):
    sys.stderr.write("board: schedule %s\n" % msg)
    return 2


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    home = _home()
    verb, rest = argv[0], argv[1:]
    try:
        if verb == "outbox":
            return cmd_outbox(home, rest)
        if verb == "sent":
            return cmd_sent(home, rest)
        if verb == "reply":
            return cmd_reply(home, rest)
        if verb in ("off", "on"):
            return cmd_off_on(home, verb, rest)
    except TimeoutError as exc:
        sys.stderr.write("board: schedule %s: %s; try again\n" % (verb, exc))
        return 8
    return _usage("unknown command: %s" % verb[:40])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
