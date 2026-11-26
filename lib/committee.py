#!/usr/bin/env python3
"""Committee decisions on owner-only asks while the owner is away (docs/contract.md, "Committee
decisions"). Builds on committee advice (lib/advice.py, `board advise`).

While the owner is away and has opted in, a unanimous committee recommendation on a pending board
ask becomes the decision: the board records it as answered by "committee" and BOARD relays it to
the asking session as a [BOARD decision]. Otherwise ("owner_final", the default) advice stays advice.

  $BOARD_HOME/away-decisions.json   {mode: "committee"|"owner_final", red_lines: "committee"|"owner", by, at}
                                    (the owner's setting; 0600; written under consensus.lock)

Absent file, unreadable file or invalid value: owner_final / owner (fails closed: nothing is decided
without the owner until the owner opts in; invalid True so the owner sees a broken file).

When the committee decides (decide(), all of these, checked under consensus.lock):
  - the owner is away, in the same away period the advice was made in, and the advice is open;
  - the setting is committee (and, for a red line or production gate, red_lines is committee);
  - the decision is a board ask (kind "question", not a pane question) still pending/unauthorized;
  - participants (the asker, the recommender, --to, anyone who weighed in), each mapped to its fleet
    code (aliases count once), are at least 2 distinct fleet.yaml sessions and include the asker;
  - for a red line or production gate, a strong record: the recommendation carries a rationale
    beyond the option label (RATIONALE_MIN characters), and every agree carries its session's own
    reason as --note (NOTE_MIN characters; the recommender's reaffirm too). `board advise` refuses
    either without it (exit 2) while the owner lets the committee decide;
  - every participant's latest stance is agree (the recommender's recommendation counts as its
    agreement), and nobody's latest stance is object. Silence is never agreement;
  - each of those counting stances is binding: it was written while the setting was committee
    (red_lines committee for a red line) and its --from was verified (verify(): the command ran
    inside that session, the `board resolve --from` ancestry check). An advice-only agree from
    before the owner opted in never counts;
  - the answer (the recommended option's label, else the recommendation as one line, the relay
    phrases stripped) is not empty and passes the secret guard like an owner's free-text answer.
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

Test mode (BOARD_COMMITTEE_TEST=1) lets tests use $CLAUDE_SESSIONS_DIR for the ancestry checks and
skips the command-line check. It applies only when $BOARD_HOME resolves under /tmp/, /private/tmp/ or
/var/folders/, is not ~/.agent-switchboard, and its advice/, decisions/, away-decisions.json and
owner-away (when present) resolve inside it (no symlink out to a real board), so it is meant for
boards that BOARD does not operate. Otherwise the registry is always <passwd home>/.claude/sessions
(neither $HOME nor $CLAUDE_SESSIONS_DIR is trusted) and /proc is the real one. All of this guards
against honest mistakes and casual misuse within one user account, not against a hostile process of
that user: it is not a security boundary.

CLI (through `board away decisions ...` and `board advise relay ...`):
  committee.py settings status [--json]
  committee.py settings committee|owner_final [--red-lines committee|owner] --by WHO
                                       (a change that lets the committee decide more is refused,
                                       exit 4, when any ancestor process is a Claude Code session)
  committee.py verify CODE             exit 0 when this command runs inside session CODE, else 4
  committee.py canon NAME...           each name's fleet code, one per line (an alias is its code);
                                       exit 3 when one is not a fleet.yaml session (named on stderr)
  committee.py relay [ID] [--json]     committee decisions waiting to be relayed (the operator's)
  committee.py decided [--json]       committee decisions of the last 7 days
  committee.py _decide ID              (bin/board only, with consensus.lock held)
Exit codes: 0 ok, 1 error / nothing to show, 2 usage, 4 refused (loosening from a session; verify).
"""
import contextlib
import fcntl
import glob
import json
import os
import pwd
import re
import shlex
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
DEFAULTS = {"mode": "owner_final", "red_lines": "owner"}   # fails closed: the owner opts in
BY = "committee"                  # answered_by of a committee decision: never an approver identity
BY_RE = re.compile(r"(board:)?[A-Za-z0-9][A-Za-z0-9@._+:-]{0,199}")
CODE_RE = advice.CODE_RE
TEST_ENV = "BOARD_COMMITTEE_TEST"
TEST_ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/")
# the relay's own phrases never appear inside an answer (a recommendation can't fake the suffix)
STRIP_RE = re.compile(r"\(\s*by\s+(?:committee\s+consensus\s+while\s+owner\s+away\s*:?|owner\s+on\s+board\s*\)?)", re.I)
MAX_ANSWER = 2000
RATIONALE_MIN = 20                # a red line's recommendation: its reason, beyond the option label
NOTE_MIN = 10                     # a red line's agree: the session's own reason
MAX_RATIONALE = 400
MAX_NOTE = 200
KEEP_MS = 7 * 24 * 3600 * 1000    # `decided` view: the last week
REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
# The relay a worker trusts (docs/contract.md "Relay message formats"); built here only.
RELAY_RE = re.compile(r"\[BOARD decision (?P<id>[A-Za-z0-9._-]+)\] (?P<answer>.*) \(by committee consensus "
                      r"while owner away: (?P<who>[A-Za-z0-9][A-Za-z0-9_.-]*(?:, [A-Za-z0-9][A-Za-z0-9_.-]*)+)"
                      r"(?P<red>; red line(?:: production gate)?)?\)")


def now_ms():
    return advice.now_ms()


def approve_words():
    """The approve-style gate answers: bin/board's APPROVE_WORDS line is the one source. Unreadable:
    none (every committee gate answer is then "rejected", never approved by accident)."""
    try:
        with open(os.path.join(REPO, "bin", "board"), encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"APPROVE_WORDS=\((.*)\)\s*$", line)
                if m:
                    return tuple(w.lower() for w in shlex.split(m.group(1)))
    except (OSError, ValueError):
        pass
    return ()


def canon_map(cfg=None):
    """alias -> fleet code (codes map to themselves), and the set of fleet.yaml codes."""
    try:
        cfg = fleet.load_fleet() if cfg is None else cfg
        return fleet.alias_map(cfg), {sess["code"] for sess in cfg.get("sessions") or []}
    except Exception:  # noqa: BLE001 - no config: nobody is a known session (fails closed)
        return {}, set()


def canon(m, name):
    return m.get(name, name) if isinstance(name, str) else None


# ------------------------------------------------------------------ who runs this command
def _real_home():
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return os.path.expanduser("~")


def test_mode(home):
    if os.environ.get(TEST_ENV) != "1":
        return False
    real = os.path.realpath(home) + "/"
    default = os.path.realpath(os.path.join(_real_home(), ".agent-switchboard")) + "/"
    if real == default or not real.startswith(TEST_ROOTS):
        return False
    # the board's own files stay inside it: a symlink out to a real board is not a test board
    for name in (advice.DIR, "decisions", FILE, "owner-away"):
        path = os.path.join(home, name)
        if os.path.lexists(path) and not (os.path.realpath(path) + "/").startswith(real):
            return False
    return True


def _sessions_dir(home):
    if test_mode(home):
        return fleet.sessions_dir()
    return os.path.join(_real_home(), ".claude", "sessions")


def _ppid(pid):
    try:
        with open("/proc/%d/status" % pid, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _argv(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        pass
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return r.stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []


def _is_claude(pid):
    """The process is Claude Code: its program or script (argv[0] or argv[1]) names claude."""
    return any("claude" in a.lower() for a in _argv(pid)[:2])


def _ancestors(limit=32, start=None):
    p = os.getpid() if start is None else start
    for _ in range(limit):
        if p <= 1:
            return
        yield p
        p = _ppid(p)


def _registered(home, pid):
    """The registry entry of PID when it describes that very process (lib/actions.py same_process)
    and, outside test mode, that process is Claude Code; else None."""
    import actions  # noqa: PLC0415
    reg = advice.read_json(os.path.join(_sessions_dir(home), "%d.json" % pid))
    if not isinstance(reg, dict) or not actions.same_process(reg, pid):
        return None
    if not test_mode(home) and not _is_claude(pid):
        return None
    return reg


def caller_session(home):
    """The session this command runs in: the first ancestor with a registry entry (as `board resolve
    --from` finds it), by its tmux session name, else its registered name; None when there is none."""
    for pid in _ancestors():
        reg = _registered(home, pid)
        if reg is not None:
            t = str(reg.get("tmux") or "").split(":")[0]
            n = reg.get("name") if isinstance(reg.get("name"), str) else ""
            return t or n or None
    return None


def verify(home, code):
    """True when this command runs inside session CODE (aliases count as their code). Fails closed."""
    found = caller_session(home)
    m, _codes = canon_map()
    return bool(found) and canon(m, found) == canon(m, code), found


def agent_ancestor(home, start=None):
    """True when the process START (default: this one) or any of its ancestors is a Claude Code
    session (a registered one, or outside test mode any process whose program names claude): an
    agent must not loosen the owner's setting. A process re-parented to init (setsid, nohup) has
    no such ancestor any more: best effort, not a security boundary."""
    for pid in _ancestors(start=start):
        if _registered(home, pid) is not None or (not test_mode(home) and _is_claude(pid)):
            return True
    return False


def _loopback_hex(addr):
    """/proc/net/tcp{,6} address (hex, as the kernel prints it) is a loopback one: 127.0.0.0/8, ::1
    or ::ffff:127.x.y.z."""
    a = addr.upper()
    if len(a) == 8:
        return a.endswith("7F")                      # IPv4, little-endian: first octet last
    return a == "00000000000000000000000001000000" or (
        a.startswith("0000000000000000FFFF0000") and a.endswith("7F"))


def _tcp_inodes(local_port, remote_port):
    """Socket inodes of the loopback TCP connections from local_port to remote_port (/proc/net/tcp,
    tcp6): both ends on a loopback address."""
    out = set()
    for name in ("tcp", "tcp6"):
        try:
            with open("/proc/net/" + name, encoding="ascii") as fh:
                next(fh, None)
                for line in fh:
                    f = line.split()
                    if len(f) < 10:
                        continue
                    (la, lp), (ra, rp) = f[1].rsplit(":", 1), f[2].rsplit(":", 1)
                    if int(lp, 16) == local_port and int(rp, 16) == remote_port and f[9] != "0" \
                            and _loopback_hex(la) and _loopback_hex(ra):
                        out.add(f[9])
        except (OSError, ValueError, IndexError):
            continue
    return out


def peer_pids(client_port, server_port):
    """The processes holding the client end of a loopback connection to server_port (best effort:
    only processes this user may inspect; an empty list when it can't be told)."""
    inodes = _tcp_inodes(client_port, server_port)
    if not inodes:
        return []
    want = {"socket:[%s]" % i for i in inodes}
    out = []
    for d in glob.glob("/proc/[0-9]*/fd"):
        try:
            if any(os.readlink(os.path.join(d, fd)) in want for fd in os.listdir(d)):
                out.append(int(d.split("/")[2]))
        except (OSError, ValueError):
            continue
    return out


def agent_peer(home, client_port, server_port):
    """For a loosening request on the loopback API: True when the client process (or an ancestor)
    is a Claude Code session, False when it is not, None when the peer can't be told."""
    pids = peer_pids(client_port, server_port)
    if not pids:
        return None
    return any(agent_ancestor(home, start=p) for p in pids)


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
    # every name is its fleet code: an alias is the same session, counted once
    m, codes = canon_map()
    parts = []
    for p in rec.get("participants") or []:
        c = canon(m, p)
        if isinstance(c, str) and CODE_RE.fullmatch(c) and c not in parts:
            parts.append(c)
    asker = canon(m, advice.asking_session(dec))
    if not isinstance(asker, str) or asker not in parts:
        return False, "no_asker", None
    if len(parts) < 2:
        return False, "too_few", None
    if any(p not in codes for p in parts):
        return False, "unknown_session", None
    recommender = canon(m, rec.get("from"))
    if recommender not in parts:
        return False, "no_recommender", None
    last = {}
    for st in rec.get("stances") or []:
        if isinstance(st, dict) and isinstance(st.get("by"), str) and st.get("stance") in advice.STANCES:
            last[canon(m, st["by"])] = st
    if any(st.get("stance") == "object" for st in last.values()):
        return False, "objected", None
    for p in parts:
        st = last.get(p)
        if not st or st.get("stance") != ("recommend" if p == recommender else "agree"):
            return False, "silent", None
        if not binding(st, red):
            return False, "not_binding", None
    option = rec.get("option") if isinstance(rec.get("option"), str) and rec.get("option") else None
    # a red line is decided only with a strong record: the reason for it, and each session's own
    if red and len(rationale_text(rec.get("recommendation"), option)) < RATIONALE_MIN:
        return False, "no_rationale", None
    if red and any(p != recommender and not note_ok(last[p].get("note")) for p in parts):
        return False, "no_note", None
    answer = option if option is not None else rec.get("recommendation")
    answer = clean_answer(answer)
    if not answer:
        return False, "empty", None
    if dec.get("kind") == "owner_call" and advice.is_decline(answer):
        return False, "owner_only_decline", None     # declining an asker's request stays the owner's
    if guard.scrub(answer) != answer or guard.screen_answer(answer, dec.get("options")):
        return False, "held", None
    stances = [{"by": p, "stance": last[p]["stance"], "at": _ms(last[p].get("at")) or None,
                "note": short(last[p].get("note"), MAX_NOTE), "binding": last[p].get("binding")} for p in parts]
    return True, "ok", {
        "answer": answer, "option": option, "participants": parts, "recommended_by": recommender,
        "rationale": short(rec.get("recommendation"), MAX_RATIONALE),
        "asker": asker, "stances": stances, "red_line": bool(red), "production": bool(production),
        "settings": {"mode": s["mode"], "red_lines": s["red_lines"]},
        "away": {"since": _ms((rec.get("away") or {}).get("since")) or None, "by": (rec.get("away") or {}).get("by")}}


def binding(st, red):
    """A stance counts toward a decision only when it was written while the owner let the committee
    decide that class of ask (red lines included or not) and its --from was verified."""
    b = st.get("binding") if isinstance(st, dict) else None
    return isinstance(b, dict) and b.get("verified") is True and b.get("mode") == "committee" \
        and (not red or b.get("red_lines") == "committee")


def rationale_text(text, label=None):
    """The recommendation's reason: one line, without the option label, relay phrases stripped."""
    t = clean_answer(text)
    if isinstance(label, str) and label.strip():
        t = re.sub(re.escape(label.strip()), " ", t, flags=re.I)
    return re.sub(r"[\s.,;:!?\-]+", " ", t).strip()


def note_ok(note):
    return isinstance(note, str) and len(re.sub(r"\s+", " ", note).strip()) >= NOTE_MIN


def short(text, n):
    """Free text for the record, an event or a notify: scrubbed, one line, clipped, defanged."""
    return away._text(guard.scrub(text if isinstance(text, str) else ""), n) or None  # noqa: SLF001


def clean_answer(answer):
    """One line, at most MAX_ANSWER characters, no relay phrases (stripped until none is left, so
    nesting can't rebuild one), defanged. No pass cap: a pass that changes the text removes at least
    one phrase, so the text gets strictly shorter and the loop ends."""
    a = re.sub(r"\s+", " ", answer if isinstance(answer, str) else "")
    while True:
        b = re.sub(r"\s+", " ", STRIP_RE.sub("", a))
        if b == a:
            break
        a = b
    return away.DEFANG_RE.sub(r"(\1", a.strip()[:MAX_ANSWER])


def _gate_result(answer):
    return "approved" if answer.strip().lower() in approve_words() else "rejected"


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
                     "recommended_by": plan["recommended_by"], "rationale": plan["rationale"], "red_line": plan["red_line"],
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
    notes = "; ".join("%s: %s" % (away._text(x.get("by"), 64), short(x.get("note"), 120))  # noqa: SLF001
                      for x in c.get("stances") or [] if isinstance(x, dict) and x.get("note"))
    body = "Asked by %s: \"%s\". Agreed: %s. Why: %s.%s Review it on the board (decided while you were away)." % (
        away._text(dec.get("from"), 64), away._text(dec.get("question"), 200),  # noqa: SLF001
        ", ".join(away._text(p, 64) for p in c.get("participants") or []),  # noqa: SLF001
        short(c.get("rationale"), 240) or "-", (" Notes: %s." % notes) if notes else "")
    log_event(home, {"ts": now_ms(), "type": "committee", "action": "notify-body", "id": did, "body": body})
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
            "rationale": short(c.get("rationale"), MAX_RATIONALE),
            "stances": [{"by": t(x.get("by"), 64), "stance": t(x.get("stance"), 16), "at": _ms(x.get("at")) or None,
                         "note": short(x.get("note"), MAX_NOTE)}
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
        dec["id"], clean_answer(dec.get("answer")), ", ".join(c.get("participants") or []), red)


def waiting(home, only=None):
    out = []
    for path in glob.glob(os.path.join(home, "decisions", "*.json")):
        d = advice.read_json(path)
        if not is_committee(d) or d.get("status") != "answered" or not advice.DEC_RE.fullmatch(str(d.get("id"))):
            continue
        if d.get("kind") == "owner_call":
            continue    # an owner's call goes back to its thread (lib/owner_call.py), never to a session
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


def settings_main(home, rest):
    js = "--json" in rest
    rest = [a for a in rest if a != "--json"]
    if not rest or rest[0] in ("-h", "--help", "help"):
        print(SETTINGS_USAGE)
        print("  while the owner is away, a unanimous committee recommendation on a board ask becomes the\n"
              "  decision (committee) or stays advice for the owner (owner_final, the default until the\n"
              "  owner opts in). --red-lines owner keeps red lines and production gates for the owner.\n"
              "  Only the owner lets the committee decide more: refused (exit 4) under a Claude Code session.")
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
                else "advice only; the owner has the final say"
                + ("" if os.path.exists(os.path.join(home, FILE)) else " (the default: the owner has not opted in)"),
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
    # The owner's setting: an agent never lets the committee decide more. A change that loosens it
    # is refused when any ancestor process is a Claude Code session (tightening always works).
    if loosens(settings(home), sub, red) and agent_ancestor(home):
        log_event(home, {"ts": now_ms(), "type": "away_decisions", "action": "set", "by": by, "mode": sub,
                         "red_lines": red, "result": "refused", "reason": "loosening under a Claude Code session"})
        sys.stderr.write("board away decisions: refused — this runs under a Claude Code session; only the owner "
                         "lets the committee decide more (the profile menu, or the owner's own terminal)\n")
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
    if cmd == "canon":
        m, codes = canon_map()
        bad = []
        for n in rest:
            c = canon(m, n)
            print(c)
            if c not in codes:
                bad.append(n)
        if bad:
            sys.stderr.write("not a fleet.yaml session: %s\n" % ", ".join(bad))
            return 3
        return 0
    if cmd == "rationale-ok":
        # committee.py rationale-ok [LABEL] < recommendation: exit 0 when it gives a reason beyond the label
        return 0 if len(rationale_text(sys.stdin.read(), rest[0] if rest else None)) >= RATIONALE_MIN else 1
    if cmd == "verify":
        if len(rest) != 1 or not CODE_RE.fullmatch(rest[0]):
            sys.stderr.write("usage: committee.py verify CODE\n")
            return 2
        ok, found = verify(home, rest[0])
        print(found or "none")
        return 0 if ok else 4
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
