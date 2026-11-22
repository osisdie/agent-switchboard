#!/usr/bin/env python3
"""Committee decisions on owner-only asks while the owner is away (docs/contract.md, "Committee
decisions"). Builds on committee advice (lib/advice.py, `board advise`).

While the owner is away, a unanimous committee recommendation on a pending board ask becomes the
decision: the board records it as answered by "committee" and BOARD relays it to the asking session
as a [BOARD decision]. The owner's profile setting chooses this (the default) or "owner_final", where
advice stays advice (as before this existed).

  $BOARD_HOME/away-decisions.json   {mode: "committee"|"owner_final", red_lines: "committee"|"owner", by, at}
                                    (the owner's setting; 0600; written under consensus.lock)

Absent file: committee, red lines included (the owner's default). A file that can't be read, or an
invalid value: owner_final / owner (nothing is decided without the owner), with invalid True so the
owner sees it.

When the committee decides (decide(), all of these, checked under consensus.lock):
  - the owner is away, in the same away period the advice was made in, and the advice is open;
  - the setting is committee (and, for a red line or production gate, red_lines is committee);
  - the decision is a board ask (kind "question", not a pane question) still pending/unauthorized;
  - participants (the asker, the recommender, --to, anyone who weighed in) are at least 2 distinct
    sessions and include the asking session;
  - every participant's latest stance is agree (the recommender's recommendation counts as its
    agreement), and nobody's latest stance is object. Silence is never agreement;
  - the answer (the recommended option's label, else the recommendation as one line) is not empty
    and passes the secret guard like an owner's free-text answer.
Then, in one pass: the decision becomes {status: answered, answered_by: "committee", answer,
committee: {...}}; a gate's task entry and the approval event are written as `board answer` writes
them (approved_by "committee"); the advice record freezes with reason "decided"; events.jsonl gets
{type: "committee", action: "decided", ...}; and the owner is notified after the fact
(board-notify, key committee:<id>, info; red lines and production gates high; never escalating).

Who triggers it: the agree that completes the consensus (`board advise ID agree`, which holds
consensus.lock and calls `committee.py _decide ID`), and the owner switching the setting to
committee (or red lines to committee) while away, which sweeps every open advice record. `board
answer` takes the same lock, so an owner's answer and a committee decision never interleave: who
gets the lock first wins, and a committee decision is final (the owner's later answer is refused).

CLI (through `board away decisions ...` and `board advise relay ...`):
  committee.py settings status [--json]
  committee.py settings committee|owner_final [--red-lines committee|owner] --by WHO
  committee.py relay [ID] [--json]     committee decisions waiting to be relayed (the operator's)
  committee.py decided [--json]       committee decisions of the last 7 days
  committee.py _decide ID              (bin/board only, with consensus.lock held)
Exit codes: 0 ok, 1 error / nothing to show, 2 usage, 4 refused (loosening from a fleet session).
"""
import contextlib
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import advice  # noqa: E402
import away  # noqa: E402
import fleet  # noqa: E402
import guard  # noqa: E402

FILE = "away-decisions.json"
LOCK_FILE = "consensus.lock"      # shared with bin/board (away, consensus, advise, answer)
MODES = ("committee", "owner_final")
RED_MODES = ("committee", "owner")
DEFAULTS = {"mode": "committee", "red_lines": "committee"}
BY = "committee"                  # answered_by of a committee decision: never an approver identity
BY_RE = re.compile(r"(board:)?[A-Za-z0-9][A-Za-z0-9@._+:-]{0,199}")
CODE_RE = advice.CODE_RE
APPROVE_WORDS = ("approve", "approved", "yes", "y", "allow", "ok", "lgtm", "go", "ship", "ship it")
MAX_ANSWER = 2000
KEEP_MS = 7 * 24 * 3600 * 1000    # `decided` view: the last week
REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
# The relay a worker trusts (docs/contract.md "Relay message formats"); built here only.
RELAY_RE = re.compile(r"\[BOARD decision (?P<id>[A-Za-z0-9._-]+)\] (?P<answer>.*) \(by committee consensus "
                      r"while owner away: (?P<who>[A-Za-z0-9][A-Za-z0-9_.-]*(?:, [A-Za-z0-9][A-Za-z0-9_.-]*)+)"
                      r"(?P<red>; red line(?:: production gate)?)?\)")


def now_ms():
    return advice.now_ms()


def _ms(v):
    return advice._ms(v)  # noqa: SLF001


@contextlib.contextmanager
def locked(home, wait_s=10.0):
    """consensus.lock, the lock bin/board's lock_board takes (flock(2) on both sides)."""
    fd = os.open(os.path.join(home, LOCK_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        end = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError("another away/consensus change holds the lock; try again") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def log_event(home, ev):
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ the owner's setting
def settings(home):
    path = os.path.join(home, FILE)
    present = os.path.lexists(path)
    raw = advice.read_json(path) if present else None
    rec = raw if isinstance(raw, dict) else {}
    bad = present and (not isinstance(raw, dict)
                       or (rec.get("mode") is not None and rec.get("mode") not in MODES)
                       or (rec.get("red_lines") is not None and rec.get("red_lines") not in RED_MODES))
    if bad:
        out = {"mode": "owner_final", "red_lines": "owner", "invalid": True}
    else:
        out = {"mode": rec.get("mode") if rec.get("mode") in MODES else DEFAULTS["mode"],
               "red_lines": rec.get("red_lines") if rec.get("red_lines") in RED_MODES else DEFAULTS["red_lines"],
               "invalid": False}
    by = rec.get("by")
    out["by"] = by if isinstance(by, str) and BY_RE.fullmatch(by) else None
    out["at"] = _ms(rec.get("at")) or None
    return out


def view(home, owner=False):
    """The board's `away_decisions`: the mode for everyone (the page words the advice block by it);
    who set it and when, and an unreadable file, for owners only."""
    s = settings(home)
    out = {"mode": s["mode"], "red_lines": s["red_lines"]}
    if owner:
        out.update({"invalid": s["invalid"], "by": s["by"], "at": s["at"]})
    return out


def loosens(cur, mode, red_lines):
    """True when the change lets the committee decide more than it does now."""
    if cur["invalid"]:
        return mode == "committee" or red_lines == "committee"
    if mode == "committee" and cur["mode"] != "committee":
        return True
    return red_lines == "committee" and cur["red_lines"] != "committee"


def set_settings(home, mode, red_lines, by, now=None):
    """The owner's setting (None keeps a value). Returns (changed, settings, decided ids). Raises
    ValueError on bad input, TimeoutError when the lock is busy. Switching so that the committee
    decides more sweeps the open advice at once (a consensus already complete is decided)."""
    if mode is not None and mode not in MODES:
        raise ValueError("mode must be \"committee\" or \"owner_final\"")
    if red_lines is not None and red_lines not in RED_MODES:
        raise ValueError("red_lines must be \"committee\" or \"owner\"")
    if not isinstance(by, str) or not BY_RE.fullmatch(by):
        raise ValueError("by must be a session code or board:<identity>")
    now = now if now is not None else now_ms()
    decided = []
    with locked(home):
        cur = settings(home)
        new = {"mode": mode or cur["mode"], "red_lines": red_lines or cur["red_lines"]}
        changed = cur["invalid"] or not os.path.exists(os.path.join(home, FILE)) \
            or any(new[k] != cur[k] for k in new)
        if changed:
            advice.write_json(os.path.join(home, FILE), dict(new, by=by, at=now))
            log_event(home, {"ts": now, "type": "away_decisions", "action": "set", "by": by,
                             "mode": new["mode"], "red_lines": new["red_lines"],
                             "previous": {"mode": cur["mode"], "red_lines": cur["red_lines"],
                                          "invalid": cur["invalid"]}})
            if new["mode"] == "committee":
                decided = sweep(home, now)
    for did in decided:
        notify_owner(home, did)
    return changed, settings(home), decided


# ------------------------------------------------------------------ the consensus
def evaluate(home, did, rec=None, dec=None, away_rec=None, s=None):
    """(ok, why, plan). why names the first rule that fails (or "ok"); plan holds what decide()
    writes. Read-only."""
    rec = rec if rec is not None else advice.load_record(home, did)
    dec = dec if dec is not None else advice.load_decision(home, did)
    away_rec = away_rec if away_rec is not None else away.away_record(home)
    s = s if s is not None else settings(home)
    if rec is None:
        return False, "no_advice", None
    if not advice.eligible(dec):
        return False, "not_a_board_ask", None
    if dec.get("status") not in advice.OPEN_STATUSES:
        return False, "not_pending", None
    if away_rec is None:
        return False, "owner_back", None
    if advice.frozen_why(rec, dec, away_rec):
        return False, "frozen", None
    if s["mode"] != "committee":
        return False, "owner_final", None
    production = fleet.is_production(dec)
    red = advice.is_red_line(dec, rec)
    if red and s["red_lines"] != "committee":
        return False, "red_line_owner", None
    parts = []
    for p in rec.get("participants") or []:
        if isinstance(p, str) and CODE_RE.fullmatch(p) and p not in parts:
            parts.append(p)
    asker = dec.get("from")
    if not isinstance(asker, str) or asker not in parts:
        return False, "no_asker", None
    if len(parts) < 2:
        return False, "too_few", None
    last = advice.latest(rec)
    if any(st.get("stance") == "object" for st in last.values()):
        return False, "objected", None
    recommender = rec.get("from")
    if recommender not in parts:
        return False, "no_recommender", None
    for p in parts:
        st = last.get(p)
        if p == recommender:
            if not st or st.get("stance") != "recommend":
                return False, "silent", None
        elif not st or st.get("stance") != "agree":
            return False, "silent", None
    option = rec.get("option") if isinstance(rec.get("option"), str) and rec.get("option") else None
    answer = option if option is not None else rec.get("recommendation")
    answer = re.sub(r"\s+", " ", answer if isinstance(answer, str) else "").strip()[:MAX_ANSWER]
    answer = away.DEFANG_RE.sub(r"(\1", answer)
    if not answer:
        return False, "empty", None
    if guard.scrub(answer) != answer or guard.screen_answer(answer, dec.get("options")):
        return False, "held", None
    stances = [{"by": p, "stance": last[p]["stance"], "at": _ms(last[p].get("at")) or None} for p in parts]
    return True, "ok", {
        "answer": answer, "option": option, "participants": parts, "recommended_by": recommender,
        "asker": asker, "stances": stances, "red_line": bool(red), "production": bool(production),
        "settings": {"mode": s["mode"], "red_lines": s["red_lines"]},
        "away": {"since": _ms((rec.get("away") or {}).get("since")) or None, "by": (rec.get("away") or {}).get("by")}}


def _gate_result(answer):
    return "approved" if answer.strip().lower() in APPROVE_WORDS else "rejected"


def _update_task_gate(home, dec, result, now):
    """The gate entry on the task, as `board answer` writes it (approved_by "committee")."""
    import tasks  # noqa: PLC0415 - only for gates
    tid = dec.get("task_id")
    if not tid or not dec.get("gate"):
        return True
    path = os.path.join(home, "tasks", "%s.json" % tid)
    if not tasks.ID_RE.fullmatch(str(tid)) or not os.path.isfile(path):
        return True
    try:
        with tasks.locked(home, tid):
            t = advice.read_json(path)
            if not isinstance(t, dict):
                return False
            gates = []
            for g in t.get("gates") or []:
                if isinstance(g, dict) and g.get("decision_id") == dec["id"]:
                    g = dict(g, status=result, approved_by=BY if result == "approved" else None,
                             approved_at=now if result == "approved" else None, answered_by=BY)
                gates.append(g)
            t["gates"] = gates
            t["updated_at"] = now
            advice.write_json(path, t)
        return True
    except (tasks.Bad, OSError):
        return False


def decide(home, did, now=None):
    """(lock held by the caller) Record the committee's decision on DID when evaluate() allows it.
    Returns (decided, why). Idempotent: a decided ask is no longer pending, so it never decides twice."""
    now = now if now is not None else now_ms()
    rec = advice.load_record(home, did)
    dec = advice.load_decision(home, did)
    s = settings(home)
    ok, why, plan = evaluate(home, did, rec, dec, None, s)
    if not ok:
        return False, why
    for k in ("press_note", "press_note_at", "press_note_code", "status_reason"):
        dec.pop(k, None)
    dec.update({"status": "answered", "answer": plan["answer"], "answered_by": BY, "answered_at": now,
                "updated_at": now, "committee": dict(plan, decided_at=now)})
    del dec["committee"]["answer"]
    advice.write_json(os.path.join(home, "decisions", did + ".json"), dec)
    rec.update({"status": "frozen", "frozen_reason": "decided", "frozen_at": now, "updated_at": now})
    advice.write_json(os.path.join(home, advice.DIR, did + ".json"), rec)
    log_event(home, {"ts": now, "type": "answer", "id": did, "by": BY})
    log_event(home, {"ts": now, "type": "committee", "action": "decided", "id": did, "answer": plan["answer"],
                     "participants": plan["participants"], "stances": plan["stances"],
                     "recommended_by": plan["recommended_by"], "red_line": plan["red_line"],
                     "production": plan["production"], "settings": plan["settings"], "away": plan["away"]})
    log_event(home, {"ts": now, "type": "advice", "action": "freeze", "id": did, "reason": "decided"})
    if dec.get("gate"):
        result = _gate_result(plan["answer"])
        log_event(home, {"ts": now, "type": "approval", "decision_id": did, "task_id": dec.get("task_id"),
                         "stage": dec.get("stage"), "gate": dec.get("gate"), "answer": plan["answer"],
                         "result": result, "answered_by": BY, "answered_at": now, "relayed_at": None})
        if not _update_task_gate(home, dec, result, now):
            log_event(home, {"ts": now, "type": "committee", "action": "task-not-updated", "id": did,
                             "task_id": dec.get("task_id")})
    return True, "decided"


def sweep(home, now=None):
    """(lock held) decide() every open advice record; the ids decided."""
    out = []
    for path in sorted(glob.glob(os.path.join(home, advice.DIR, "*.json"))):
        did = os.path.basename(path)[:-5]
        if advice.DEC_RE.fullmatch(did) and decide(home, did, now)[0]:
            out.append(did)
    return out


def notify_owner(home, did):
    """After the fact, best effort, never escalating, in the background (the lock may be held)."""
    dec = advice.load_decision(home, did) or {}
    c = dec.get("committee") if isinstance(dec.get("committee"), dict) else {}
    red = c.get("production") or c.get("red_line")
    what = "production gate" if c.get("production") else "red line" if red else "board ask"
    title = "[board] Decided by committee while you were away: %s %s → %s" % (
        what, did, away._text(dec.get("answer"), 120))  # noqa: SLF001
    body = "Asked by %s: \"%s\". Agreed: %s. Review it on the board (decided while you were away)." % (
        away._text(dec.get("from"), 64), away._text(dec.get("question"), 200),  # noqa: SLF001
        ", ".join(away._text(p, 64) for p in c.get("participants") or []))  # noqa: SLF001
    cmd = [os.path.join(REPO, "bin", "board-notify"), "send", "--level", "high" if red else "info",
           "--key", "committee:%s" % did, "--board", did, "--kind", "committee", "--no-escalate",
           "--title", title, "--body", body]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        log_event(home, {"ts": now_ms(), "type": "committee", "action": "notified", "id": did,
                         "level": "high" if red else "info"})
    except OSError:
        pass


# ------------------------------------------------------------------ relay and views
def compact(c):
    """The committee block the board shows (one line, clipped, defanged)."""
    if not isinstance(c, dict):
        return None
    t = away._text  # noqa: SLF001
    return {"participants": [t(p, 64) for p in c.get("participants") or [] if isinstance(p, str)][:32],
            "recommended_by": t(c.get("recommended_by"), 64) if c.get("recommended_by") else None,
            "option": t(c.get("option"), 200) if c.get("option") else None,
            "stances": [{"by": t(x.get("by"), 64), "stance": t(x.get("stance"), 16), "at": _ms(x.get("at")) or None}
                        for x in c.get("stances") or [] if isinstance(x, dict)][:32],
            "red_line": c.get("red_line") is True, "production": c.get("production") is True,
            "settings": {k: t((c.get("settings") or {}).get(k), 16) for k in ("mode", "red_lines")},
            "away_since": _ms((c.get("away") or {}).get("since")) or None,
            "decided_at": _ms(c.get("decided_at")) or None}


def is_committee(dec):
    return isinstance(dec, dict) and dec.get("answered_by") == BY and isinstance(dec.get("committee"), dict)


def relay_text(dec):
    """The exact line BOARD sends to the asking session."""
    c = dec["committee"]
    red = "; red line: production gate" if c.get("production") else "; red line" if c.get("red_line") else ""
    return "[BOARD decision %s] %s (by committee consensus while owner away: %s%s)" % (
        dec["id"], dec.get("answer") or "", ", ".join(c.get("participants") or []), red)


def waiting(home, only=None):
    out = []
    for path in glob.glob(os.path.join(home, "decisions", "*.json")):
        d = advice.read_json(path)
        if not is_committee(d) or d.get("status") != "answered" or not advice.DEC_RE.fullmatch(str(d.get("id"))):
            continue
        if only and d["id"] != only:
            continue
        out.append(d)
    out.sort(key=lambda d: (_ms(d.get("answered_at")), d["id"]))
    return out


def relay(home, only=None, as_json=False):
    rows = [{"id": d["id"], "to": d.get("from"), "red_line": bool(d["committee"].get("red_line")),
             "production": bool(d["committee"].get("production")), "text": relay_text(d)} for d in waiting(home, only)]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    for r in rows:
        print("== %s %s%s" % (r["id"], r["to"], " production-gate" if r["production"] else " red-line" if r["red_line"] else ""))
        print(r["text"])
    return 0 if rows or not only else 1


def decided_view(home, now=None):
    """Committee decisions of the last week, newest first (`board advise decided`)."""
    now = now if now is not None else now_ms()
    out = []
    for path in glob.glob(os.path.join(home, "decisions", "*.json")):
        d = advice.read_json(path)
        if not is_committee(d) or now - _ms(d.get("answered_at")) > KEEP_MS:
            continue
        out.append({"id": d.get("id"), "from": d.get("from"), "question": away._text(d.get("question"), 200),  # noqa: SLF001
                    "answer": away._text(d.get("answer"), 300), "status": d.get("status"),  # noqa: SLF001
                    "answered_at": _ms(d.get("answered_at")) or None, "relayed_at": _ms(d.get("relayed_at")) or None,
                    "gate": d.get("gate"), "committee": compact(d["committee"])})
    out.sort(key=lambda r: (r["answered_at"] or 0, r["id"]), reverse=True)
    return out


# ------------------------------------------------------------------ CLI
SETTINGS_USAGE = ("usage: board away decisions status [--json]\n"
                  "       board away decisions committee|owner_final [--red-lines committee|owner] --by WHO")


def _tmux_session():
    if not os.environ.get("TMUX"):
        return None
    try:
        args = ["tmux", "display-message", "-p"] + (["-t", os.environ["TMUX_PANE"]] if os.environ.get("TMUX_PANE") else []) + ["#S"]
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None


def _fleet_session(home, name):
    """True when NAME (a tmux session) is one of the board's sessions (a code or an alias)."""
    if not name:
        return False
    op = advice.read_json(os.path.join(home, "operator.json"))
    if name == "BOARD" or (isinstance(op, dict) and name == op.get("operator_session")):
        return True     # the operator is a board session too
    try:
        cfg = fleet.load_fleet()
    except Exception:  # noqa: BLE001
        cfg = {}
    if name in fleet.known_codes(home, cfg):
        return True
    return any(name in (s.get("aliases") or []) for s in cfg.get("sessions") or [] if isinstance(s, dict))


def settings_main(home, rest):
    js = "--json" in rest
    rest = [a for a in rest if a != "--json"]
    if not rest or rest[0] in ("-h", "--help", "help"):
        print(SETTINGS_USAGE)
        print("  while the owner is away, a unanimous committee recommendation on a board ask becomes the\n"
              "  decision (committee, the default) or stays advice for the owner (owner_final).\n"
              "  --red-lines owner keeps red lines and production gates for the owner (default: committee).")
        return 0 if rest else 2
    sub = rest[0]
    if sub == "status":
        if rest[1:]:
            sys.stderr.write(SETTINGS_USAGE + "\n")
            return 2
        v = view(home, owner=True)
        v["away"] = away.away_record(home) is not None
        if js:
            print(json.dumps(v, ensure_ascii=False))
        else:
            print("while the owner is away, board asks: %s%s%s" % (
                "the committee decides when every session asked agrees" if v["mode"] == "committee"
                else "advice only; the owner has the final say",
                "" if v["mode"] != "committee" else (
                    "; red lines and production gates included" if v["red_lines"] == "committee"
                    else "; red lines and production gates wait for the owner"),
                " (the saved setting could not be read: owner_final until it is set again)" if v["invalid"] else ""))
        return 0
    if sub not in MODES:
        sys.stderr.write(SETTINGS_USAGE + "\n")
        return 2
    by = red = None
    i, args = 0, rest[1:]
    while i < len(args):
        if args[i] in ("--by", "--red-lines") and i + 1 < len(args):
            if args[i] == "--by":
                by = args[i + 1]
            else:
                red = args[i + 1]
            i += 2
            continue
        sys.stderr.write("board away decisions: unknown option %s\n%s\n" % (args[i], SETTINGS_USAGE))
        return 2
    if not by or (red is not None and red not in RED_MODES):
        sys.stderr.write(SETTINGS_USAGE + "\n")
        return 2
    # The owner's setting: a session never lets the committee decide more. Inside tmux, a change
    # that loosens it from one of the board's own sessions is refused (tightening always works).
    me = _tmux_session()
    if loosens(settings(home), sub, red) and _fleet_session(home, me):
        log_event(home, {"ts": now_ms(), "type": "away_decisions", "action": "set", "by": by, "mode": sub,
                         "red_lines": red, "tmux_session": me, "result": "refused",
                         "reason": "loosening from a board session"})
        sys.stderr.write("board away decisions: refused — this is the board's session '%s'; only the owner lets "
                         "the committee decide more (the profile menu, or a shell that is not a board session)\n" % me)
        return 4
    try:
        changed, s, decided = set_settings(home, sub, red, by)
    except ValueError as exc:
        sys.stderr.write("board away decisions: %s\n" % exc)
        return 2
    except TimeoutError as exc:
        sys.stderr.write("board away decisions: %s\n" % exc)
        return 1
    print("while the owner is away, board asks: %s%s%s" % (
        s["mode"], "" if s["mode"] != "committee" else " (red lines: %s)" % s["red_lines"],
        "" if changed else " (unchanged)"))
    if decided:
        print("decided by the committee now (consensus was already complete): %s" % " ".join(decided))
    return 0


def main(argv):
    home = fleet.board_home()
    args = argv[1:]
    if not args:
        sys.stderr.write(__doc__.split("CLI")[1])
        return 2
    cmd, rest = args[0], args[1:]
    if cmd == "settings":
        return settings_main(fleet.ensure_home(), rest)
    js = "--json" in rest
    rest = [a for a in rest if a != "--json"]
    if cmd == "relay":
        if len(rest) > 1 or (rest and not advice.DEC_RE.fullmatch(rest[0])):
            sys.stderr.write("usage: board advise relay --from BOARD [ID] [--json]\n")
            return 2
        return relay(home, rest[0] if rest else None, js)
    if cmd == "decided":
        rows = decided_view(home)
        if js:
            print(json.dumps(rows, ensure_ascii=False))
            return 0
        if not rows:
            print("no committee decisions in the last 7 days")
        for r in rows:
            c = r["committee"]
            print("%s  %s  %s → %s  [%s]%s" % (r["id"], r["status"], r["from"], r["answer"][:80],
                                              ", ".join(c["participants"]),
                                              "  PRODUCTION GATE" if c["production"] else "  RED LINE" if c["red_line"] else ""))
        return 0
    if cmd == "_decide":
        if len(rest) != 1 or not advice.DEC_RE.fullmatch(rest[0]):
            return 2
        done, why = decide(home, rest[0])
        if done:
            print("decided")
            notify_owner(home, rest[0])
            return 0
        print(why)
        return 0
    sys.stderr.write("committee.py: unknown command %s\n" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
