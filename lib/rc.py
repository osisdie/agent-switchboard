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
                         apply the stored choice now (--auto: the collector's retry rules)
  rc.py want CODE        print on|off: should fleet-up pass --remote-control

State:
  $BOARD_HOME/rc-prefs.json  {version, all: {state, by, at}?, sessions: {CODE: {state, by, at}},
                              updated_at, updated_by}            (0600, flock rc-prefs.lock)
  $BOARD_HOME/rc-apply.json  {CODE: {sid, want, result, reason, at, tries}}  (last attempt)
  events.jsonl               {"type": "rc", ...} for every change and every attempt

What a session wants: its own choice, else the "all" choice, else fleet.yaml (the session's
remote_control, then the top-level remote_control), else on. Only a choice made with `board rc`
(its own or "all") is applied to a running session; fleet.yaml alone only decides fleet-up.

Applying (Claude Code 2.1.x has no flag for a running session; `/remote-control` toggles it):
  1. the session must be idle (registry status idle, hook state done/rest), else "deferred";
  2. its pane must belong to its tmux session and show Claude's empty prompt box (the prompt
     line between two rules, nothing typed, no dialog, nothing running), else "refused";
  3. the registry and hook state are read again, then `send-keys -l -- /remote-control`;
  4. the prompt line must now show exactly that text and the session still be idle, else
     "partial" (typed, Enter not pressed: never Enter on a guess);
  5. Enter. On: bridgeSessionId must appear in the registry. Off: Claude opens a dialog
     (Disconnect this session / Show QR code / Continue); Up moves the focus row by row until
     "Disconnect this session" is focused (checked on screen every time), then Enter, and
     bridgeSessionId must disappear. Anything unexpected in the dialog: Escape (= Continue,
     Remote Control stays on).
  Results: confirmed, already, deferred, refused, partial, unconfirmed, unavailable (Claude
  answered "Unknown command": Remote Control is not available in that session, e.g.
  DO_NOT_TRACK is set), consent (Claude asks its one-time confirmation: dismissed with
  Escape; run /remote-control in the session once), offline, failed.

Exit codes: 0 ok (also "deferred"/"already"), 1 error, 2 usage, 3 refused (nothing sent),
            5 partial (keys sent, not finished), 6 sent but not confirmed / unavailable.
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

PREFS_FILE = "rc-prefs.json"
APPLY_FILE = "rc-apply.json"
RUN_LOCK = "rc-run.lock"
STATES = ("on", "off")
CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
IDLE_STATES = ("done", "rest")
# --auto never retries these for the same session process and wanted state.
FINAL_AUTO = ("unavailable", "consent", "partial")
RETRY_AUTO = ("refused", "unconfirmed", "failed")


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


# ------------------------------------------------------------------ attempts

def load_attempts(home):
    data = prefs._read(os.path.join(home, APPLY_FILE))  # noqa: SLF001
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def record_attempt(home, code, sid, want, result, reason, now):
    with prefs.Locked(home, APPLY_FILE) as st:
        prev = st.data.get(code) if isinstance(st.data.get(code), dict) else {}
        same = prev.get("sid") == sid and prev.get("want") == want
        tries = (int(prev.get("tries") or 0) if same else 0) + (0 if result in ("deferred", "already") else 1)
        st.data[code] = {"sid": sid, "want": want, "result": result, "reason": reason, "at": now, "tries": tries}


def auto_allowed(cfg, att, sid, want, now):
    """--auto: may the collector try this session (process sid) for this wanted state now?"""
    if not att or att.get("sid") != sid or att.get("want") != want:
        return True
    res = att.get("result")
    if res in FINAL_AUTO:
        return False
    if res in RETRY_AUTO:
        rcfg = cfg.get("rc") or {}
        tries = int(att.get("tries") or 0)
        if tries >= int(rcfg.get("max_tries") or 3):
            return False
        return now - int(att.get("at") or 0) >= float(rcfg.get("retry_minutes") or 10) * 60000
    return True


# ------------------------------------------------------------------ the board's view

def annotate(sessions, cfg, home, now=None):
    """Adds `rc` to every snapshot row: {on: bool|None (None: not running), want, source,
    pending (an explicit choice the running session does not follow yet), last: the last
    attempt for this session process, or None}."""
    data = load(home)
    atts = load_attempts(home)
    for r in sessions:
        code = str(r.get("code") or "")
        want, source = wanted(code, cfg, data)
        running = bool(r.get("session_id"))
        on = bool(r.get("remote_url")) if running else None
        att = atts.get(code)
        last = None
        if att and running and att.get("sid") == r.get("session_id"):
            last = {k: att.get(k) for k in ("want", "result", "reason", "at")}
        r["rc"] = {"on": on, "want": want, "source": source,
                   "pending": bool(running and explicit(source) and "/" not in code and on != (want == "on")),
                   "last": last}
    return sessions


def page_state(home, cfg=None):
    """The board page's view of the choices (rc-prefs.json): {all, sessions: {CODE: on|off}}."""
    data = load(home)
    return {"all": data["all"]["state"] if data["all"] else None,
            "sessions": {c: e["state"] for c, e in sorted(data["sessions"].items())},
            "updated_at": data.get("updated_at")}


def auto_candidates(sessions, cfg, home, now):
    """Codes the collector should apply now: pending, idle, with a pane, retry rules allow."""
    if not (cfg.get("rc") or {}).get("auto_apply", True):
        return []
    if os.environ.get("BOARD_RC_AUTO", "").strip().lower() in ("0", "false", "no", "off"):
        return []
    atts = load_attempts(home)
    out = []
    for r in sessions:
        rc = r.get("rc") or {}
        if not rc.get("pending") or r.get("state") not in IDLE_STATES or not r.get("tmux_pane"):
            continue
        if r.get("status") != "idle":
            continue
        if auto_allowed(cfg, atts.get(r["code"]), r.get("session_id"), rc.get("want"), now):
            out.append(r["code"])
    return out


def run_busy(home):
    """True while an applier holds rc-run.lock."""
    try:
        with open(os.path.join(home, RUN_LOCK), "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def spawn_auto(home):
    """Starts `rc.py apply --pending --auto` in the background (never waits for it), unless an
    applier is already running."""
    if run_busy(home):
        return False
    try:
        subprocess.Popen([sys.executable, os.path.realpath(__file__), "apply", "--pending", "--auto"],  # noqa: S603
                         env=dict(os.environ, BOARD_HOME=home), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ applying

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
    args = ["send-keys", "-t", pane] + (["-l", "--"] if literal else []) + list(keys)
    return _tmux(*args)[0] == 0


def _settle():
    try:
        time.sleep(float(os.environ.get("BOARD_PRESS_SETTLE", "0.4")))
    except ValueError:
        time.sleep(0.4)


def _screen(pane, nlines):
    rc, out = _tmux("capture-pane", "-p", "-t", pane)
    if rc != 0:
        return None
    lines = out.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-nlines:]


def _rx(rcfg, key):
    try:
        return re.compile(str(rcfg.get(key) or fleet.DEFAULTS["rc"][key]), re.IGNORECASE if key == "busy_regex" else 0)
    except re.error:
        return re.compile(str(fleet.DEFAULTS["rc"][key]))


def prompt_box(lines, rcfg):
    """(index of the prompt line, its text after the prompt mark) when the last prompt line is
    Claude's input box (a rule right above and right below), else (None, why)."""
    rule, mark = _rx(rcfg, "rule_regex"), "\u276f"
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(mark):
            if i == 0 or i + 1 >= len(lines) or not rule.search(lines[i - 1]) or not rule.search(lines[i + 1]):
                return None, "the prompt line is not inside Claude's input box"
            return i, lines[i][len(mark):].replace("\u00a0", " ").strip()
    return None, "no prompt box on screen (a dialog or picker is open?)"


def idle_screen(lines, rcfg):
    """None when the pane shows an idle Claude with an empty prompt box, else why not."""
    if lines is None:
        return "capture-pane failed"
    i, text = prompt_box(lines, rcfg)
    if i is None:
        return text
    if not _rx(rcfg, "prompt_regex").search(lines[i]):
        return "the prompt box is not empty"
    if any(_rx(rcfg, "busy_regex").search(ln) for ln in lines):
        return "the session is working (esc to interrupt)"
    if any(_rx(rcfg, "dialog_regex").search(ln) for ln in lines[i:]):
        return "a dialog is open"
    return None


def _session_now(row):
    """(registry record, hook state) of this session process, read fresh."""
    reg = _read_json(os.path.join(fleet.sessions_dir(), "%s.json" % row.get("pid")))
    if reg.get("sessionId") != row.get("session_id") or not os.path.exists("/proc/%s" % row.get("pid")):
        return None, None
    st = _read_json(os.path.join(fleet.board_home(), "state", "%s.json" % row.get("session_id")))
    return reg, st


def _idle(reg, st):
    import collect  # noqa: WPS433 - late: collect imports this module
    if not reg or reg.get("status") != "idle":
        return False
    return collect.derive_state(st or {}, reg.get("status"), int(reg.get("statusUpdatedAt") or 0)) in IDLE_STATES


def _bridge(reg):
    return bool((reg or {}).get("bridgeSessionId"))


def _wait_bridge(row, want_on, seconds, pane=None, rcfg=None):
    """Polls the registry until bridgeSessionId matches; returns (ok, screen_reason)."""
    deadline = time.time() + seconds
    while True:
        reg, _st = _session_now(row)
        if reg is None:
            return False, "the session ended"
        if _bridge(reg) == want_on:
            return True, None
        if pane is not None:
            lines = _screen(pane, int(rcfg.get("lines") or 30)) or []
            if any(_rx(rcfg, "unavailable_regex").search(ln) for ln in lines):
                return False, "unavailable"
            if any(_rx(rcfg, "consent_regex").search(ln) for ln in lines):
                return False, "consent"
        if time.time() >= deadline:
            return False, None
        time.sleep(0.5)


def apply_row(home, cfg, row, want):
    """Applies WANT (on|off) to one snapshot row. Returns (result, reason)."""
    rcfg = dict(fleet.DEFAULTS["rc"], **(cfg.get("rc") or {}))
    nlines = int(rcfg.get("lines") or 30)
    want_on = want == "on"
    if not row.get("session_id") or not row.get("pid"):
        return "offline", "the session is not running"
    reg, st = _session_now(row)
    if reg is None:
        return "offline", "the session is not running"
    if _bridge(reg) == want_on:
        return "already", "Remote Control is already %s" % want
    if not _idle(reg, st):
        return "deferred", "the session is not idle; will apply when idle"
    pane = row.get("tmux_pane")
    if not pane:
        return "refused", "the session has no tmux pane"
    if (st or {}).get("tmux_pane") and st["tmux_pane"] != pane:
        return "refused", "pane mismatch: hook saw %s, registry says %s" % (st["tmux_pane"], pane)
    tsess = str(row.get("tmux") or "").split(":", 1)[0]
    if tsess:
        rc, out = _tmux("display-message", "-p", "-t", pane, "#{session_name}")
        if rc != 0:
            return "refused", "tmux pane %s not found" % pane
        if out.strip() != tsess:
            return "refused", "pane %s belongs to tmux session '%s', expected '%s'" % (pane, out.strip(), tsess)
    why = idle_screen(_screen(pane, nlines), rcfg)
    if why:
        return "refused", why
    # Last look right before typing.
    reg, st = _session_now(row)
    if reg is None or not _idle(reg, st) or _bridge(reg) == want_on:
        return "deferred", "the session changed while checking; will apply when idle"
    cmd = str(rcfg.get("command") or "/remote-control")
    if not _send(pane, cmd, literal=True):
        return "failed", "send-keys failed (typing the command)"
    _settle()
    lines = _screen(pane, nlines)
    i, text = prompt_box(lines or [], rcfg)
    if i is None or text != cmd:
        return "partial", "the prompt box does not show %s after typing; Enter not pressed (clear the prompt in the session)" % cmd
    reg, st = _session_now(row)
    if reg is None or not _idle(reg, st):
        return "partial", "the session started working while typing; Enter not pressed (clear the prompt in the session)"
    if not _send(pane, "Enter"):
        return "partial", "send-keys failed (Enter)"
    secs = float(rcfg.get("confirm_seconds") or 10)
    if want_on:
        ok, why = _wait_bridge(row, True, secs, pane, rcfg)
        if ok:
            return "confirmed", "Remote Control is on"
        if why == "consent":
            _send(pane, "Escape")
            return "consent", "Claude asks its one-time Remote Control confirmation; dismissed. Run /remote-control in the session once"
        if why == "unavailable":
            return "unavailable", "Claude does not offer /remote-control in this session (DO_NOT_TRACK set, not signed in to claude.ai, or turned off by policy)"
        return "unconfirmed", why or "bridgeSessionId did not appear within %gs" % secs
    return _disconnect(row, pane, rcfg, nlines, secs)


def _focused(lines):
    """The focused row of a picker ("> label") below the prompt area, or None."""
    for ln in reversed(lines or []):
        m = re.match("^\\s+\u276f\\s+(.*\\S)\\s*$", ln)
        if m:
            return m.group(1)
    return None


def _disconnect(row, pane, rcfg, nlines, secs):
    dialog, label = _rx(rcfg, "dialog_regex"), _rx(rcfg, "disconnect_label")
    deadline = time.time() + min(secs, 6)
    lines = None
    while True:
        lines = _screen(pane, nlines) or []
        if any(dialog.search(ln) for ln in lines) and any(label.search(ln) for ln in lines):
            break
        if any(_rx(rcfg, "unavailable_regex").search(ln) for ln in lines):
            return "unavailable", "Claude does not offer /remote-control in this session"
        if time.time() >= deadline:
            return "unconfirmed", "the Remote Control dialog did not open; nothing else sent"
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
        if not (any(dialog.search(ln) for ln in lines) and any(label.search(ln) for ln in lines)):
            return "partial", "the Remote Control dialog closed unexpectedly"
    else:
        _send(pane, "Escape")
        return "refused", "could not focus \"Disconnect this session\"; closed the dialog with Escape (Remote Control stays on)"
    reg, _st = _session_now(row)
    if reg is None:
        return "failed", "the session ended"
    if not _send(pane, "Enter"):
        return "partial", "send-keys failed (Enter on Disconnect)"
    ok, why = _wait_bridge(row, False, secs)
    if ok:
        return "confirmed", "Remote Control is off"
    return "unconfirmed", why or "bridgeSessionId did not disappear within %gs" % secs


class RunLock:
    """One applier at a time per BOARD_HOME (flock rc-run.lock): --auto skips when busy."""

    def __init__(self, home, wait_s):
        self.path, self.wait_s = os.path.join(home, RUN_LOCK), wait_s

    def __enter__(self):
        self.fh = open(self.path, "a")  # noqa: SIM115
        deadline = time.time() + self.wait_s
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() >= deadline:
                    self.fh.close()
                    raise OSError("another Remote Control change is running; try again") from None
                time.sleep(0.1)

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        return False


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


def apply(home, cfg, codes=None, auto=False, by=None):
    """Applies the stored choice to CODES (None: every pending session). Returns
    [(code, want, result, reason)]."""
    out = []
    with RunLock(home, 0 if auto else 60):
        refresh_snapshot()
        rows = snapshot_rows(home)
        data = load(home)
        atts = load_attempts(home)
        now = now_ms()
        by_code = {r["code"]: r for r in rows}
        if codes is None:
            targets = [r["code"] for r in rows if (r.get("rc") or {}).get("pending")]
        else:
            targets = codes
        for code in targets:
            row = by_code.get(code)
            want, source = wanted(code, cfg, data)
            if row is None:
                res = ("offline", "no such session on the board")
            elif auto and (not explicit(source) or row.get("state") not in IDLE_STATES):
                continue
            elif auto and not auto_allowed(cfg, atts.get(code), row.get("session_id"), want, now):
                continue
            else:
                try:
                    res = apply_row(home, cfg, row, want)
                except Exception as exc:  # noqa: BLE001 - one bad session never stops the rest
                    res = ("failed", "error: %s" % exc.__class__.__name__)
            out.append((code, want) + res)
            if row is not None and row.get("session_id"):
                record_attempt(home, code, row.get("session_id"), want, res[0], res[1], now_ms())
            log(home, {"action": "apply", "code": code, "want": want, "result": res[0], "reason": res[1],
                       "auto": bool(auto), "by": by, "pane": (row or {}).get("tmux_pane")})
    if out:
        refresh_snapshot()
    return out


EXIT = {"confirmed": 0, "already": 0, "deferred": 0, "offline": 0, "refused": 3, "partial": 5,
        "unconfirmed": 6, "unavailable": 6, "consent": 6, "failed": 1}


def _exit_for(results):
    return max([EXIT.get(r[2], 1) for r in results] or [0])


def status(home, cfg):
    rows = snapshot_rows(home)
    annotate(rows, cfg, home)
    data = load(home)
    known = {r["code"] for r in rows}
    out = {"all": data["all"], "sessions": [], "do_not_track": bool(os.environ.get("DO_NOT_TRACK"))}
    for r in rows:
        rc = r["rc"]
        out["sessions"].append({"code": r["code"], "state": r.get("state"), "running": bool(r.get("session_id")),
                                "on": rc["on"], "want": rc["want"], "source": rc["source"],
                                "pending": rc["pending"], "last": rc["last"]})
    for code, e in sorted(data["sessions"].items()):
        if code not in known:
            out["sessions"].append({"code": code, "state": None, "running": False, "on": None, "want": e["state"],
                                    "source": "session", "pending": False, "last": None})
    return out


def status_lines(s):
    a = s["all"]
    lines = ["all: %s" % ("%s (by %s)" % (a["state"], fleet.safe_text(a.get("by"))) if a else "not set (fleet.yaml, else on)")]
    for r in s["sessions"]:
        actual = "offline" if r["on"] is None else ("on" if r["on"] else "off")
        line = "%-16s actual %-7s want %-3s (%s)" % (fleet.safe_text(r["code"]), actual, r["want"], r["source"])
        if r["pending"]:
            line += "  pending: applies when idle"
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
            results = apply(home, cfg, codes, by=args.by)
            for code, want, res, reason in results:
                print("  %s: %s (%s)" % (fleet.safe_text(code), res, fleet.safe_text(reason)))
            return _exit_for(results)
        if args.cmd == "apply":
            for c in args.codes:
                _check(c, "cli")
            if args.codes and args.pending:
                raise UsageError("give CODEs or --pending, not both")
            try:
                results = apply(home, cfg, args.codes or None, auto=args.auto, by=args.by)
            except OSError:
                if args.auto:
                    return 0  # another applier runs; the collector tries again later
                raise
            for code, want, res, reason in results:
                print("%s: %s %s (%s)" % (fleet.safe_text(code), want, res, fleet.safe_text(reason)))
            return 0 if args.auto else _exit_for(results)
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
