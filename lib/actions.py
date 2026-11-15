#!/usr/bin/env python3
"""Session actions: a short allowlist of fixed slash commands that the board may type into an
idle Claude Code session (docs/contract.md, "Session actions"). Never free text.

  compact   /compact          summarize the conversation (frees context)
  rc_on     /remote-control   Remote Control on  (the owner's choice lives in lib/rc.py)
  rc_off    /remote-control   Remote Control off, through Claude's "Disconnect" dialog

Usage (bin/board runs these; exit codes below):
  actions.py run CODE compact [--by WHO] [--queue-only]
                  queue a compact and run it now if the session is idle (else it runs once the
                  session is idle; --queue-only leaves that to `apply`)
  actions.py cancel CODE [--by WHO]
                  drop CODE's queued compact
  actions.py apply [CODE...|--pending] [--auto] [--only rc|compact] [--by WHO]
                  run what is pending: a Remote Control choice a running session does not follow
                  yet (lib/rc.py), a queued compact. --auto: the collector's retry rules
  actions.py status [--json]

State:
  $BOARD_HOME/session-actions.json  queued one-shot actions {CODE: {action, by, at, sid}}; an entry
                                    for another session process, or older than queue_hours, is
                                    dropped                                (0600, flock)
  $BOARD_HOME/actions-last.json     the last attempt {CODE: {ACTION: {sid, result, reason, at,
                                    tries, done_at}}}                       (0600, flock)
  events.jsonl                      {"type": "session-action", op: queue|cancel|run|drop, code,
                                    action, result?, reason?, by, auto?, pane?}

Every action, the same guards (in the style of `board press`):
  1. the session must be idle (registry status idle, hook state done/rest), else "deferred";
  2. its pane must belong to its tmux session and show Claude's empty input box (the last line
     that starts with the prompt mark has a rule right above and below and nothing typed; no
     "esc to interrupt", no dialog below it), else "refused" with nothing sent;
  3. registry and hook state read again, then `send-keys -l -- <command>`;
  4. the box must hold exactly the command and the session still be idle, else "partial"
     (typed, Enter not pressed: never Enter on a guess);
  5. Enter, then the action's own confirmation:
     rc_on    bridgeSessionId appears in the registry (Claude's one-time consent dialog gets
              Escape: "consent"; "Unknown command": "unavailable");
     rc_off   the dialog (Disconnect this session / Show QR code / Continue): Up row by row,
              checked on screen, until "Disconnect this session" is focused, Enter, and
              bridgeSessionId disappears; anything unexpected gets Escape (= Continue);
     compact  the session turns busy ("Compacting conversation…"), then idle with
              "⎿ Compacted" right under the echoed "/compact": "confirmed"; "Not enough messages
              to compact": "nothing"; still running after compact_seconds: "started".

Exit codes: 0 ok (also deferred / already / nothing / started), 1 error, 2 usage, 3 refused
            (nothing sent), 5 partial (typed, Enter not pressed), 6 sent but not confirmed,
            or not available in that session.
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402
import prefs  # noqa: E402

ACTIONS = {"compact": "/compact", "rc_on": "/remote-control", "rc_off": "/remote-control"}
QUEUED = ("compact",)
QUEUE_FILE = "session-actions.json"
LAST_FILE = "actions-last.json"
RUN_LOCK_FMT = "actions-%s.lock"  # one per session code: one action at a time on a session
CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
IDLE_STATES = ("done", "rest")
# --auto never retries these for the same session process and action; a queued action is done.
FINAL = ("confirmed", "already", "nothing", "started", "unavailable", "consent", "partial")
RETRY = ("refused", "unconfirmed", "failed")
EXIT = {"confirmed": 0, "already": 0, "deferred": 0, "offline": 0, "nothing": 0, "started": 0, "dropped": 0,
        "busy": 0,
        "refused": 3, "partial": 5, "unconfirmed": 6, "unavailable": 6, "consent": 6, "failed": 1}


class UsageError(ValueError):
    """A bad argument (exit 2)."""


def now_ms():
    return prefs.now_ms()


def log(home, ev):
    prefs._log(home, dict({"ts": now_ms(), "type": "session-action"}, **ev))  # noqa: SLF001


def conf(cfg):
    return dict(fleet.DEFAULTS["session_actions"], **(cfg.get("session_actions") or {}))


def check_code(code):
    if not CODE_RE.fullmatch(code or ""):
        raise UsageError("CODE must be a session code (letters, digits, _ . -)")


def check_by(by):
    if not prefs.BY_RE.fullmatch(by or ""):
        raise UsageError("--by must be a session code or board:<identity> (letters, digits, @ . _ + : -)")


# ------------------------------------------------------------------ queue and attempts

def load_queue(home, cfg=None, now=None):
    """Queued actions that are still valid: {CODE: {action, by, at, sid}}."""
    data = prefs._read(os.path.join(home, QUEUE_FILE))  # noqa: SLF001
    now = now if now is not None else now_ms()
    max_ms = float(conf(cfg or {}).get("queue_hours") or 12) * 3600000
    out = {}
    for code, e in data.items():
        if (CODE_RE.fullmatch(str(code)) and isinstance(e, dict) and e.get("action") in QUEUED
                and isinstance(e.get("at"), int) and now - e["at"] <= max_ms and e.get("sid")):
            out[str(code)] = e
    return out


def queue(home, code, action, by, sid, now=None):
    now = now if now is not None else now_ms()
    with prefs.Locked(home, QUEUE_FILE) as st:
        st.data[code] = {"action": action, "by": by, "at": now, "sid": sid}
    log(home, {"op": "queue", "code": code, "action": action, "by": by})


def unqueue(home, code, by=None, op="cancel", reason=None):
    with prefs.Locked(home, QUEUE_FILE) as st:
        had = st.data.pop(code, None) is not None
    if had:
        log(home, {"op": op, "code": code, "by": by, "reason": reason})
    return had


def load_last(home):
    data = prefs._read(os.path.join(home, LAST_FILE))  # noqa: SLF001
    return {c: {a: r for a, r in v.items() if isinstance(r, dict)} for c, v in data.items() if isinstance(v, dict)}


def record(home, code, action, sid, result, reason, now, choice_at=None):
    """Keeps the last attempt of ACTION on CODE. choice_at: for rc_on/rc_off, when the board
    choice that asked for it was made (tries count per session process and choice)."""
    with prefs.Locked(home, LAST_FILE) as st:
        per = st.data.get(code) if isinstance(st.data.get(code), dict) else {}
        prev = per.get(action) if isinstance(per.get(action), dict) else {}
        same = prev.get("sid") == sid
        same_try = same and prev.get("choice_at") == choice_at
        tries = (int(prev.get("tries") or 0) if same_try else 0) + (0 if result in ("deferred", "already", "busy") else 1)
        rec = {"sid": sid, "result": result, "reason": reason, "at": now, "tries": tries, "choice_at": choice_at,
               "done_at": now if result == "confirmed" else (prev.get("done_at") if same else None)}
        per[action] = rec
        st.data[code] = per
    return rec


def rc_settled(cfg, rec, sid, choice_at):
    """Has the board finished trying this Remote Control choice (made at CHOICE_AT) on this
    session process? Then a change made by hand in the session wins."""
    if not rec or rec.get("sid") != sid or rec.get("choice_at") != choice_at:
        return False
    res = rec.get("result")
    if res in FINAL:
        return True
    return res in RETRY and int(rec.get("tries") or 0) >= int(conf(cfg).get("max_tries") or 3)


def auto_allowed(cfg, rec, sid, now, choice_at=None):
    """--auto: may this action be tried on this session process now? (choice_at: a new board
    choice for rc_on/rc_off starts over.)"""
    if not rec or rec.get("sid") != sid:
        return True
    if choice_at is not None and rec.get("choice_at") != choice_at:
        return True
    res = rec.get("result")
    if res in FINAL:
        return False
    if res in RETRY:
        c = conf(cfg)
        if int(rec.get("tries") or 0) >= int(c.get("max_tries") or 3):
            return False
        return now - int(rec.get("at") or 0) >= float(c.get("retry_minutes") or 10) * 60000
    return True


# ------------------------------------------------------------------ the board's view

def annotate(sessions, cfg, home, now=None):
    """Adds `compact` to every snapshot row: {queued: {at, by}|None, last: {result, at}|None,
    done_at: the last confirmed compact of this session process, or None}."""
    q = load_queue(home, cfg, now)
    last = load_last(home)
    for r in sessions:
        code, sid = str(r.get("code") or ""), r.get("session_id")
        e = q.get(code)
        rec = (last.get(code) or {}).get("compact")
        rec = rec if rec and sid and rec.get("sid") == sid else None
        r["compact"] = {"queued": {"at": e["at"], "by": e.get("by")} if e and e.get("sid") == sid else None,
                        "last": {"result": rec.get("result"), "at": rec.get("at")} if rec else None,
                        "done_at": rec.get("done_at") if rec else None}
    return sessions


def _rc_job(row):
    rc = row.get("rc") or {}
    return ("rc_" + rc["want"]) if rc.get("pending") and rc.get("want") in ("on", "off") else None


def auto_candidates(sessions, cfg, home, now):
    """Codes the collector should run now: something pending, idle, with a pane, rules allow."""
    if not conf(cfg).get("auto_apply", True):
        return []
    if os.environ.get("BOARD_ACTIONS_AUTO", "").strip().lower() in ("0", "false", "no", "off"):
        return []
    q = load_queue(home, cfg, now)
    last = load_last(home)
    out = []
    for r in sessions:
        if r.get("state") not in IDLE_STATES or r.get("status") != "idle" or not r.get("tmux_pane"):
            continue
        code, sid = r["code"], r.get("session_id")
        if run_busy(home, code):
            continue
        at = (r.get("rc") or {}).get("at")
        jobs = [j for j in (_rc_job(r), "compact" if code in q else None) if j]
        if any(auto_allowed(cfg, (last.get(code) or {}).get(j), sid, now, at if j.startswith("rc_") else None) for j in jobs):
            out.append(code)
    return out


def lock_path(home, code):
    return os.path.join(home, RUN_LOCK_FMT % code)


def run_busy(home, code):
    """True while an action runs on CODE (flock actions-<code>.lock)."""
    try:
        with open(lock_path(home, code), "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def spawn(home, args):
    """Starts `actions.py ARGS` in the background (never waits for it)."""
    try:
        subprocess.Popen([sys.executable, os.path.realpath(__file__)] + list(args),  # noqa: S603
                         env=dict(os.environ, BOARD_HOME=home), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def spawn_auto(home, codes):
    """The collector's hand-off: `apply --auto CODE...` in the background."""
    return spawn(home, ["apply", "--auto"] + list(codes)) if codes else False


# ------------------------------------------------------------------ the pane

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _tmux(*args):
    try:
        r = subprocess.run(["tmux"] + list(args), capture_output=True, text=True, timeout=10,  # noqa: S603,S607
                           stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return r.returncode, r.stdout


def _send(pane, *keys, literal=False):
    return _tmux(*(["send-keys", "-t", pane] + (["-l", "--"] if literal else []) + list(keys)))[0] == 0


def _sleep(name, default):
    try:
        time.sleep(float(os.environ.get(name, default)))
    except ValueError:
        time.sleep(float(default))


def _settle():
    _sleep("BOARD_PRESS_SETTLE", "0.4")


def _screen(pane, nlines):
    rc, out = _tmux("capture-pane", "-p", "-t", pane)
    if rc != 0:
        return None
    lines = out.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-nlines:]


def _rx(c, key):
    try:
        return re.compile(str(c.get(key) or fleet.DEFAULTS["session_actions"][key]), re.IGNORECASE if key == "busy_regex" else 0)
    except re.error:
        return re.compile(str(fleet.DEFAULTS["session_actions"][key]))


def _has(c, key, lines):
    rx = _rx(c, key)
    return any(rx.search(ln) for ln in lines or [])


def prompt_box(lines, c):
    """(index of the prompt line, its text after the prompt mark) when the last prompt line is
    Claude's input box (a rule right above and right below), else (None, why)."""
    rule, mark = _rx(c, "rule_regex"), "\u276f"
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(mark):
            if i == 0 or i + 1 >= len(lines) or not rule.search(lines[i - 1]) or not rule.search(lines[i + 1]):
                return None, "the prompt line is not inside Claude's input box"
            return i, lines[i][len(mark):].replace("\u00a0", " ").strip()
    return None, "no prompt box on screen (a dialog or picker is open?)"


def idle_screen(lines, c):
    """None when the pane shows an idle Claude with an empty prompt box, else why not."""
    if lines is None:
        return "capture-pane failed"
    i, text = prompt_box(lines, c)
    if i is None:
        return text
    if not _rx(c, "prompt_regex").search(lines[i]):
        return "the prompt box is not empty"
    if _has(c, "busy_regex", lines):
        return "the session is working (esc to interrupt)"
    if _has(c, "dialog_regex", lines[i:]):
        return "a dialog is open"
    if _has(c, "mode_regex", lines[i:]):
        return "the prompt is in vim mode (-- NORMAL --): Enter would not submit"
    return None


def _proc_ppid(pid):
    try:
        with open("/proc/%d/stat" % int(pid), encoding="utf-8") as fh:
            data = fh.read()
        return int(data[data.rindex(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def pane_owns(pane_pid, pid, depth=20):
    """Is PANE_PID (the pane's shell) the session process PID or one of its ancestors?"""
    try:
        pane_pid, cur = int(pane_pid), int(pid)
    except (TypeError, ValueError):
        return False
    for _ in range(depth):
        if cur == pane_pid:
            return True
        cur = _proc_ppid(cur)
        if not cur or cur <= 1:
            return False
    return False


def check_pane(row, pane):
    """None when PANE is this session's pane: it exists, belongs to the registry's tmux session
    (when the registry names one) and its process (#{pane_pid}) is an ancestor of the session
    process. Else why not."""
    rcode, out = _tmux("display-message", "-p", "-t", pane, "#{session_name}\t#{pane_pid}")
    parts = out.strip().split("\t")
    if rcode != 0 or len(parts) != 2:
        return "tmux pane %s not found" % pane
    name, pane_pid = parts
    tsess = str(row.get("tmux") or "").split(":", 1)[0]
    if tsess and name != tsess:
        return "pane %s belongs to tmux session '%s', expected '%s'" % (pane, name, tsess)
    if not pane_owns(pane_pid, row.get("pid")):
        return "pane %s does not run this session's process (pid %s)" % (pane, row.get("pid"))
    return None


def above_box(lines, c, cmd, window=8):
    """The lines between the last echo of CMD ("❯ /compact") and the input box: what Claude
    printed for this command. None when the box or the echo is not on screen."""
    i, _ = prompt_box(lines or [], c)
    if i is None:
        return None
    top = i - 1
    for k in range(top - 1, max(-1, top - 1 - window), -1):
        if lines[k].startswith("\u276f") and lines[k][1:].replace("\u00a0", " ").strip() == cmd:
            return lines[k + 1:top]
    return None


def _focused(lines):
    """The focused row of a picker ("> label") below the prompt area, or None."""
    for ln in reversed(lines or []):
        m = re.match("^\\s+\u276f\\s+(.*\\S)\\s*$", ln)
        if m:
            return m.group(1)
    return None


def session_now(row):
    """(registry record, hook state) of this session process, read fresh; (None, None) when it
    is gone or the registry now names another session."""
    reg = _read_json(os.path.join(fleet.sessions_dir(), "%s.json" % row.get("pid")))
    if reg.get("sessionId") != row.get("session_id") or not os.path.exists("/proc/%s" % row.get("pid")):
        return None, None
    st = _read_json(os.path.join(fleet.board_home(), "state", "%s.json" % row.get("session_id")))
    return reg, st


def is_idle(reg, st):
    import collect  # noqa: WPS433 - late: collect imports this module
    if not reg or reg.get("status") != "idle":
        return False
    return collect.derive_state(st or {}, reg.get("status"), int(reg.get("statusUpdatedAt") or 0)) in IDLE_STATES


def _bridge(reg):
    return bool((reg or {}).get("bridgeSessionId"))


# ------------------------------------------------------------------ running one action

def run_action(home, cfg, row, action):
    """Runs ACTION on one snapshot row with every guard. Returns (result, reason)."""
    c = conf(cfg)
    nlines = int(c.get("lines") or 30)
    cmd = ACTIONS[action]
    rc_on = {"rc_on": True, "rc_off": False}.get(action)
    if not row.get("session_id") or not row.get("pid"):
        return "offline", "the session is not running"
    reg, st = session_now(row)
    if reg is None:
        return "offline", "the session is not running"
    if rc_on is not None and _bridge(reg) == rc_on:
        return "already", "Remote Control is already %s" % ("on" if rc_on else "off")
    if rc_on and fleet.env_has(row["pid"], "DO_NOT_TRACK"):
        return "unavailable", "DO_NOT_TRACK is set in this session: Claude Code has no Remote Control here (restart it without DO_NOT_TRACK)"
    if not is_idle(reg, st):
        return "deferred", "the session is not idle; will run when idle"
    pane = row.get("tmux_pane")
    if not pane:
        return "refused", "the session has no tmux pane"
    if (st or {}).get("tmux_pane") and st["tmux_pane"] != pane:
        return "refused", "pane mismatch: hook saw %s, registry says %s" % (st["tmux_pane"], pane)
    why = check_pane(row, pane) or idle_screen(_screen(pane, nlines), c)
    if why:
        return "refused", why
    # Last look right before typing.
    reg, st = session_now(row)
    if reg is None or not is_idle(reg, st) or (rc_on is not None and _bridge(reg) == rc_on):
        return "deferred", "the session changed while checking; will run when idle"
    if not _send(pane, cmd, literal=True):
        return "failed", "send-keys failed (typing the command)"
    _settle()
    i, text = prompt_box(_screen(pane, nlines) or [], c)
    if i is None or text != cmd:
        return "partial", "the prompt box does not show %s after typing; Enter not pressed (clear the prompt in the session)" % cmd
    reg, st = session_now(row)
    if reg is None or not is_idle(reg, st):
        return "partial", "the session started working while typing; Enter not pressed (clear the prompt in the session)"
    if not _send(pane, "Enter"):
        return "partial", "send-keys failed (Enter)"
    if action == "compact":
        return _confirm_compact(row, pane, c, nlines)
    if rc_on:
        return _confirm_rc_on(row, pane, c, nlines)
    return _disconnect(row, pane, c, nlines)


def _escape_if_no_box(pane, c, nlines):
    """After an unconfirmed Remote Control step: when Claude's empty input box is not on screen,
    a dialog is probably open (the registry was wrong, or a dialog we did not expect). Escape
    closes it without changing anything (it is "Continue" / "Never mind" in Claude's dialogs).
    Returns True when Escape was sent."""
    if idle_screen(_screen(pane, nlines), c) is None:
        return False
    _send(pane, "Escape")
    return True


def _rc_dialog(lines, c):
    """Is Claude's Remote Control dialog (Disconnect / Show QR code / Continue) on screen?"""
    label = _rx(c, "rc_disconnect_label")
    return _has(c, "dialog_regex", lines) and any(label.search(ln.replace("\u276f", " ")) for ln in lines)


def _confirm_rc_on(row, pane, c, nlines):
    secs = float(c.get("rc_confirm_seconds") or 10)
    deadline = time.time() + secs
    while True:
        reg, _st = session_now(row)
        if reg is None:
            return "failed", "the session ended"
        if _bridge(reg):
            return "confirmed", "Remote Control is on"
        lines = _screen(pane, nlines) or []
        if _has(c, "rc_unavailable_regex", lines):
            _escape_if_no_box(pane, c, nlines)
            return "unavailable", "Claude does not offer /remote-control in this session (DO_NOT_TRACK set, not signed in to claude.ai, or turned off by policy)"
        if _has(c, "rc_consent_regex", lines):
            _send(pane, "Escape")
            return "consent", "Claude asks its one-time Remote Control confirmation; dismissed. Run /remote-control in the session once"
        if _rc_dialog(lines, c):
            _send(pane, "Escape")
            return "refused", "Claude opened the Disconnect dialog: Remote Control was already on although the registry said off; closed it with Escape"
        if time.time() >= deadline:
            esc = _escape_if_no_box(pane, c, nlines)
            return "unconfirmed", "bridgeSessionId did not appear within %gs%s" % (secs, "; closed an open dialog with Escape" if esc else "")
        time.sleep(0.5)


def _disconnect(row, pane, c, nlines):
    secs = float(c.get("rc_confirm_seconds") or 10)
    label = _rx(c, "rc_disconnect_label")
    deadline = time.time() + min(secs, 6)
    while True:
        lines = _screen(pane, nlines) or []
        if _rc_dialog(lines, c):
            break
        if _has(c, "rc_consent_regex", lines):
            _send(pane, "Escape")
            return "consent", "Claude asked its one-time Remote Control confirmation (Remote Control was off although the registry said on); dismissed with Escape"
        if _has(c, "rc_unavailable_regex", lines):
            _escape_if_no_box(pane, c, nlines)
            return "unavailable", "Claude does not offer /remote-control in this session"
        if time.time() >= deadline:
            esc = _escape_if_no_box(pane, c, nlines)
            return "unconfirmed", "the Remote Control dialog did not open%s" % ("; closed an unexpected screen with Escape" if esc else "; nothing else sent")
        time.sleep(0.3)
    for _ in range(4):
        cur = _focused(lines)
        if cur is not None and label.search(cur):
            break
        if cur is None:
            _send(pane, "Escape")
            return "refused", "no focused row in the Remote Control dialog; closed it with Escape (Remote Control stays on)"
        if not _send(pane, "Up"):
            _send(pane, "Escape")
            return "failed", "send-keys failed (Up); closed the dialog with Escape"
        _settle()
        lines = _screen(pane, nlines) or []
        if not _rc_dialog(lines, c):
            esc = _escape_if_no_box(pane, c, nlines)
            return "partial", "the Remote Control dialog closed unexpectedly%s" % ("; sent Escape" if esc else "")
    else:
        _send(pane, "Escape")
        return "refused", "could not focus \"Disconnect this session\"; closed the dialog with Escape (Remote Control stays on)"
    if session_now(row)[0] is None:
        return "failed", "the session ended"
    if not _send(pane, "Enter"):
        return "partial", "send-keys failed (Enter on Disconnect)"
    deadline = time.time() + secs
    while True:
        reg, _st = session_now(row)
        if reg is None:
            return "failed", "the session ended"
        if not _bridge(reg):
            return "confirmed", "Remote Control is off"
        if time.time() >= deadline:
            esc = _escape_if_no_box(pane, c, nlines)
            return "unconfirmed", "bridgeSessionId did not disappear within %gs%s" % (secs, "; sent Escape" if esc else "")
        time.sleep(0.5)


def _confirm_compact(row, pane, c, nlines):
    start_by = time.time() + float(c.get("compact_start_seconds") or 15)
    end_by = time.time() + float(c.get("compact_seconds") or 600)
    idle_for = float(c.get("compact_idle_seconds") or 5)
    started, quiet_since = False, None
    while True:
        lines = _screen(pane, nlines) or []
        out = above_box(lines, c, "/compact")
        if out is not None and _has(c, "compact_empty_regex", out):
            return "nothing", "Claude says there is not enough to compact"
        reg, _st = session_now(row)
        if reg is None:
            return "failed", "the session ended"
        running = _has(c, "compact_running_regex", lines)
        if running or reg.get("status") == "busy":
            started = True
        if (reg.get("status") == "idle" and not running and out is not None
                and _has(c, "compact_done_regex", out)):
            return "confirmed", "compacted"
        now = time.time()
        if started and reg.get("status") == "idle" and not running:
            quiet_since = quiet_since or now
            if now - quiet_since >= idle_for:
                return "unconfirmed", "the session is idle again without \"Compacted\" (interrupted?)"
        else:
            quiet_since = None
        if not started and now >= start_by:
            return "unconfirmed", "the compact did not start; nothing else sent"
        if now >= end_by:
            return "started", "still compacting after %gs; it finishes in the session" % float(c.get("compact_seconds") or 600)
        _sleep("BOARD_ACTION_POLL", "1")


# ------------------------------------------------------------------ applying what is pending

class SessionLock:
    """One action at a time on a session (flock actions-<code>.lock). acquire(wait_s) returns
    False when another action still holds it after WAIT_S seconds."""

    def __init__(self, home, code):
        self.path, self.fh = lock_path(home, code), None

    def acquire(self, wait_s):
        self.fh = open(self.path, "a")  # noqa: SIM115
        deadline = time.time() + wait_s
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.time() >= deadline:
                    self.fh.close()
                    self.fh = None
                    return False
                time.sleep(0.1)

    def release(self):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


def refresh_snapshot():
    try:
        subprocess.run([os.path.join(fleet.REPO, "bin", "board-collect"), "--once", "--no-notify"],  # noqa: S603
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def snapshot_rows(home):
    snap = _read_json(os.path.join(home, "snapshot.json"))
    return [r for r in snap.get("sessions") or [] if isinstance(r, dict) and r.get("code")]


def apply(home, cfg, codes=None, auto=False, by=None, only=None):
    """Runs what is pending for CODES (None: every session with something pending). ONLY:
    "rc" or "compact". With explicit CODES and rc, the stored Remote Control choice is applied
    even when the snapshot does not show it pending ("already" when it matches): an explicit
    action forces it. One action at a time per session (SessionLock): --auto skips a busy
    session, a CLI call waits lock_wait_seconds and then only reports "busy" (the choice or the
    queued compact is stored; the collector applies it when the session is free).
    Returns [(code, action, result, reason)]."""
    import rc  # noqa: WPS433 - rc imports this module lazily too
    out = []
    refresh_snapshot()
    rows = snapshot_rows(home)
    by_code = {r["code"]: r for r in rows}
    now = now_ms()
    q = load_queue(home, cfg, now)
    last = load_last(home)
    rcdata = rc.load(home)
    c = conf(cfg)
    targets = codes if codes is not None else [
        r["code"] for r in rows if (only in (None, "rc") and _rc_job(r)) or (only in (None, "compact") and r["code"] in q)]
    for code in targets:
        row = by_code.get(code)
        if row is None:
            out.append((code, "-", "offline", "no such session on the board"))
            continue
        want, source, choice_at = rc.wanted_at(code, cfg, rcdata)
        jobs = []
        if only in (None, "rc"):
            job = _rc_job(row)
            if job is None and codes is not None and only == "rc" and not auto:
                job = "rc_" + want
            if job and not (auto and not rc.explicit(source)):
                jobs.append(job)
        if only in (None, "compact") and code in q:
            jobs.append("compact")
        if not jobs:
            continue
        lock = SessionLock(home, code)
        if not lock.acquire(0 if auto else float(c.get("lock_wait_seconds") or 20)):
            if not auto:
                out.append((code, jobs[0], "busy", "another action runs on this session; stored, will apply when it is free"))
            continue
        try:
            sid = row.get("session_id")
            for job in jobs:
                is_rc = job.startswith("rc_")
                if auto and (row.get("state") not in IDLE_STATES
                             or not auto_allowed(cfg, (last.get(code) or {}).get(job), sid, now, choice_at if is_rc else None)):
                    continue
                if job in QUEUED and q[code].get("sid") != sid:
                    unqueue(home, code, by, op="drop", reason="the session restarted")
                    out.append((code, job, "dropped", "queued for an earlier session process"))
                    continue
                try:
                    res = run_action(home, cfg, row, job)
                except Exception as exc:  # noqa: BLE001 - one bad session never stops the rest
                    res = ("failed", "error: %s" % exc.__class__.__name__)
                out.append((code, job) + res)
                rec = record(home, code, job, sid, res[0], res[1], now_ms(), choice_at if is_rc else None) if sid else None
                log(home, {"op": "run", "code": code, "action": job, "result": res[0], "reason": res[1],
                           "auto": bool(auto), "by": by, "pane": row.get("tmux_pane")})
                if job in QUEUED and res[0] != "deferred":
                    gave_up = res[0] in RETRY and rec and int(rec.get("tries") or 0) >= int(c.get("max_tries") or 3)
                    if res[0] not in RETRY or gave_up:
                        unqueue(home, code, by, op="done", reason=res[0])
                if res[0] not in ("confirmed", "already", "nothing"):
                    break  # the next action waits for the next idle moment
        finally:
            lock.release()
    if out:
        refresh_snapshot()
    return out


def exit_for(results):
    return max([EXIT.get(r[2], 1) for r in results] or [0])


def status(home, cfg):
    rows = snapshot_rows(home)
    q = load_queue(home, cfg)
    last = load_last(home)
    return {"queued": q, "sessions": [{"code": r["code"], "state": r.get("state"), "rc": r.get("rc"),
                                       "compact": r.get("compact"), "last": last.get(r["code"]) or {}} for r in rows]}


def main(argv):
    ap = argparse.ArgumentParser(prog="board session", description="session actions")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("code")
    p.add_argument("action", choices=sorted(QUEUED))
    p.add_argument("--by", default="cli")
    p.add_argument("--queue-only", action="store_true")
    p = sub.add_parser("cancel")
    p.add_argument("code")
    p.add_argument("--by", default="cli")
    p = sub.add_parser("apply")
    p.add_argument("codes", nargs="*", metavar="CODE")
    p.add_argument("--pending", action="store_true")
    p.add_argument("--auto", action="store_true")
    p.add_argument("--only", choices=["rc", "compact"])
    p.add_argument("--by", default=None)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    home = fleet.ensure_home()
    cfg = fleet.load_fleet()
    try:
        if args.cmd == "run":
            check_code(args.code)
            check_by(args.by)
            refresh_snapshot()
            rows = {r["code"]: r for r in snapshot_rows(home)}
            if args.code not in set(rows) | {s["code"] for s in cfg["sessions"]}:
                raise UsageError("no such session: %s" % args.code)
            sid = (rows.get(args.code) or {}).get("session_id")
            if not sid:
                print("%s %s: offline (the session is not running; nothing queued)" % (args.code, args.action))
                return 0
            queue(home, args.code, args.action, args.by, sid)
            if args.queue_only:
                print("%s %s: queued (runs when the session is idle)" % (args.code, args.action))
                return 0
            results = apply(home, cfg, [args.code], by=args.by, only=args.action)
            for code, action, res, reason in results:
                note = {"deferred": " (queued: runs when the session is idle)",
                        "busy": " (queued: runs when the session is free)"}.get(res, "")
                print("%s %s: %s (%s)%s" % (fleet.safe_text(code), action, res, fleet.safe_text(reason), note))
            return exit_for(results)
        if args.cmd == "cancel":
            check_code(args.code)
            check_by(args.by)
            print("cancelled the queued action for %s" % args.code if unqueue(home, args.code, args.by)
                  else "nothing queued for %s" % args.code)
            return 0
        if args.cmd == "apply":
            for c in args.codes:
                check_code(c)
            if args.codes and args.pending:
                raise UsageError("give CODEs or --pending, not both")
            results = apply(home, cfg, args.codes or None, auto=args.auto, by=args.by, only=args.only)
            for code, action, res, reason in results:
                print("%s %s: %s (%s)" % (fleet.safe_text(code), action, res, fleet.safe_text(reason)))
            return 0 if args.auto else exit_for(results)
        if args.cmd == "status":
            s = status(home, cfg)
            if args.json:
                print(json.dumps(s, ensure_ascii=False, sort_keys=True))
            else:
                for r in s["sessions"]:
                    cp = r.get("compact") or {}
                    line = "%-16s %-9s" % (fleet.safe_text(r["code"]), r.get("state"))
                    if cp.get("queued"):
                        line += "  compact queued"
                    if cp.get("done_at"):
                        line += "  last compacted %s" % time.strftime("%F %T", time.localtime(cp["done_at"] / 1000))
                    print(line)
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
