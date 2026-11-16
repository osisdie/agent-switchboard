#!/usr/bin/env python3
"""Resume sessions stuck on a usage limit (docs/contract.md, "Resume after a usage limit").

After the account has quota again (another account, or the limit reset), a Claude Code session
that hit the limit sits in one of a few screens. `board resume` reads the pane, recognizes the
screen with patterns (fleet.yaml limit_resume, defaults for Claude Code 2.1.x) and sends the
fewest keys that get it going again:

  dialog  the usage-limit options dialog ("What do you want to do?" with at least two of its own
          rows, one of them limit-specific, and a limit notice): Escape, the dialog's own cancel,
          which is its "Stop" row. Never a row number: the rows move, and some of them spend
          money or change the account.
  wait    Claude's automatic continue is armed (below the input box: "Continuing automatically
          at 3pm · Esc to cancel"): Escape cancels the wait.
  ready   the box holds Claude's own prefilled "continue" and the limit has reset ("Press enter
          to continue"), or the last reply ends in a limit notice: Enter.
  idle    an empty input box under Claude's own limit notice (a "⎿" line, or the last line before
          the box outside an assistant "●" paragraph, that starts with limit_regex): the resume
          prompt (limit_resume.prompt) typed literally, read back from the box, then Enter.

Between two keys the pane is read again and the next screen must be one the key leads to
(dialog -> wait|ready|idle, wait -> ready|idle; an empty box without a limit notice is NOT idle);
two Escapes on a session are at least esc_gap_seconds apart, across runs too (a quick double
Escape opens Claude's rewind menu). Any other screen: nothing sent ("refused", exit 3), or,
after a first key, nothing more sent ("partial", exit 5). After Enter the session must turn busy
(registry status, or "esc to interrupt" on screen) or echo the prompt; else "unconfirmed" (6), or
"limited" (6) when Claude shows the limit again (no quota yet).

Usage (bin/board runs these):
  resume.py CODE [CODE...] [--by WHO] [--dry-run] [--json]   these sessions
  resume.py --all [--by WHO] [--dry-run] [--json]            every running session whose screen
                                                             shows a limit; the others skipped
  resume.py --scan [--json]                                  classify every running pane (read-only)
  resume.py --scan-store                                     the collector's background look

Exit codes: 0 resumed (also --dry-run of a limit screen, and --all with nothing to do), 1 error,
            2 usage, 3 refused: nothing sent (reason_code not_limited, offline, pane, locked,
            config), 5 partial: keys sent, prompt not submitted, 6 submitted but not confirmed,
            or Claude is still at the limit.

State:
  $BOARD_HOME/resume-scan.json  {at, sessions: {CODE: {sid, state, since, at}}}: the collector's
                                throttled look at the panes (in the background), what the board's
                                "usage limit" chip and Resume buttons follow (0600; flock
                                resume-scan.lock)
  $BOARD_HOME/resume-esc-CODE   when the board last sent Escape to CODE (the gap between runs)
  events.jsonl                  {"type": "resume", code, result, reason, reason_code, state,
                                 keys, by, dry_run, pane}: every attempt and refusal; reason is a
                                 fixed text, never pane text
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
import actions  # noqa: E402
import fleet  # noqa: E402
import prefs  # noqa: E402

LIMIT_STATES = ("dialog", "wait", "ready", "idle")
SCAN_FILE = "resume-scan.json"
SCAN_LOCK = "resume-scan.lock"        # read-modify-write of resume-scan.json
SCAN_RUN_LOCK = "resume-scan.run.lock"  # one background look at a time
EXIT = {"resumed": 0, "dry-run": 0, "skipped": 0, "refused": 3, "partial": 5, "unconfirmed": 6,
        "limited": 6, "failed": 1}
MARK = "❯"
BULLET = "●"
TOOL = "⎿"
# What each key may lead to: the next screen must be one of these.
NEXT = {"dialog": ("wait", "ready", "idle"), "wait": ("ready", "idle")}
MAX_CODES = 64


class UsageError(ValueError):
    """A bad argument (exit 2)."""


def now_ms():
    return prefs.now_ms()


def conf(cfg):
    """limit_resume settings over the defaults, plus the session_actions screen patterns
    (rule_regex, prompt_regex, busy_regex, dialog_regex, mode_regex) it shares."""
    out = dict(actions.conf(cfg))
    out.update(fleet.DEFAULTS["limit_resume"])
    out.update(cfg.get("limit_resume") or {})
    return out


def _rx(c, key, flags=0):
    try:
        return re.compile(str(c.get(key) or fleet.DEFAULTS["limit_resume"][key]), flags)
    except re.error:
        return re.compile(str(fleet.DEFAULTS["limit_resume"][key]), flags)


def _any(rx, lines):
    return next((ln for ln in lines or [] if rx.search(ln)), None)


def prompt_text(c):
    """The resume prompt: one line of printable text, at most 500 characters, else None."""
    p = c.get("prompt")
    if not isinstance(p, str):
        return None
    p = p.strip()
    if not p or len(p) > 500 or fleet.CONTROL_RE.search(p):
        return None
    return p


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").replace("\u00a0", " ")).strip()


def same_text(a, b):
    """The box shows B: equal once all whitespace is dropped (the TUI wraps long text, at a space
    or inside a word)."""
    return re.sub(r"\s+", "", (a or "").replace("\u00a0", " ")) == re.sub(r"\s+", "", b or "")


def input_box(lines, c):
    """(prompt line index, bottom rule index, the text in the box) for the last input box on
    screen: a line that starts with the prompt mark, a rule right above it and a rule below it
    (the text may wrap onto a few lines in between). None when there is none."""
    rule = actions._rx(c, "rule_regex")  # noqa: SLF001
    for i in range(len(lines) - 1, 0, -1):
        if not lines[i].startswith(MARK) or not rule.search(lines[i - 1]):
            continue
        for j in range(i + 1, min(len(lines), i + 12)):
            if rule.search(lines[j]):
                body = [lines[i][len(MARK):]] + lines[i + 1:j]
                return i, j, _norm(" ".join(body))
            if lines[j].startswith(MARK):
                break
    return None


def last_reply(lines, top, window=15):
    """The lines between the last echoed prompt ("❯ text" at column 0) above the box and the box;
    with no echo within WINDOW lines, the 8 lines right above the box."""
    for k in range(top - 1, max(-1, top - 1 - window), -1):
        if lines[k].startswith(MARK):
            return lines[k + 1:top]
    return lines[max(0, top - 8):top]


def _in_bullet(lines, k):
    """Is line K part of an assistant or tool paragraph (contiguous non-blank lines that start
    with "●")?"""
    while k >= 0 and lines[k].strip():
        if lines[k].strip().startswith(BULLET):
            return True
        k -= 1
    return False


def limit_notice(lines, c, last_only=True):
    """Claude's own usage-limit notice among LINES, else None. A notice is a tool-result / system
    line ("⎿ Usage limit reached · …") whose text STARTS with limit_regex, or (LAST_ONLY: only
    the last non-blank line; else any line) a line outside an assistant "●" paragraph that starts
    with it. Assistant prose that mentions limits never counts."""
    rx = _rx(c, "limit_regex", re.I)
    nonblank = [k for k, ln in enumerate(lines or []) if ln.strip()]
    for k in nonblank:
        s = lines[k].replace("\u00a0", " ").strip()
        if s.startswith(TOOL):
            if rx.match(s[len(TOOL):].strip()):
                return s
        elif (not last_only or k == nonblank[-1]) and not s.startswith((BULLET, MARK)) and not _in_bullet(lines, k):
            if rx.match(s):
                return s
    return None


def limit_dialog(lines, c):
    """The usage-limit options dialog: the dialog footer (dialog_footer_regex); above it, within 20
    lines and below the last echoed prompt, its title (dialog_title_regex), at least two distinct
    rows of its own (dialog_row_regex, or the bare "Stop" of dialog_stop_regex) of which at least
    one is limit-specific (dialog_row_regex), and a limit notice (limit_notice, any line of that
    block). Returns the first matched row, else None. Another picker with a "Stop" option, or
    "What do you want to do?" without a limit notice, is not it."""
    foot = [k for k, ln in enumerate(lines) if _rx(c, "dialog_footer_regex").search(ln)]
    if not foot:
        return None
    f = foot[-1]
    top = max(0, f - 20)
    for k in range(f - 1, top - 1, -1):
        if lines[k].startswith(MARK):
            top = k + 1
            break
    block = lines[top:f]
    rows = [ln.replace(MARK, " ").strip() for ln in block]
    if not _any(_rx(c, "dialog_title_regex"), rows):
        return None
    specific = {r for r in rows if _rx(c, "dialog_row_regex").search(r)}
    stop = {r for r in rows if _rx(c, "dialog_stop_regex").search(r)}
    if not specific or len(specific | stop) < 2:
        return None
    if not limit_notice(block, c, last_only=False):
        return None
    return sorted(specific)[0]


def classify(lines, c, ev=None):
    """(state, detail): state is one of LIMIT_STATES, or "none" (an idle session with no limit
    notice), "busy", "unknown"; detail is a fixed text. EV (a dict) gets "snippet": the pane text
    that decided it, for the terminal only (never logged). Only the visible screen counts."""
    ev = ev if ev is not None else {}
    if lines is None:
        return "unknown", "capture-pane failed"
    if not lines:
        return "unknown", "the pane is empty"
    if actions._has(c, "busy_regex", lines[-12:]):  # noqa: SLF001
        return "busy", "the session is working (esc to interrupt)"
    box = input_box(lines, c)
    if box is None:
        row = limit_dialog(lines, c)
        if row:
            ev["snippet"] = _norm(row)[:80]
            return "dialog", "the usage-limit dialog is open"
        return "unknown", "no input box and no usage-limit dialog on screen"
    i, j, text = box
    below = lines[j + 1:]
    hit = _any(_rx(c, "wait_regex", re.I), below)
    if hit:
        ev["snippet"] = _norm(hit)[:80]
        return "wait", "automatic continue is waiting"
    said = limit_notice(last_reply(lines, i - 1), c)
    if said:
        ev["snippet"] = _norm(said)[:80]
    if text == "":
        why = actions.idle_screen(lines, c)
        if why:
            return "unknown", why
        if said:
            return "idle", "idle under a usage-limit notice"
        return "none", "idle, no usage-limit notice above the prompt"
    if text == _norm(str(c.get("prefill") or "")) and (said or _any(_rx(c, "ready_regex", re.I), below)):
        return "ready", "Claude's prefilled text waits for Enter"
    return "unknown", "the prompt box is not empty"


def plan(state):
    """The keys a state starts with (dry run; later steps depend on the next screen)."""
    return {"dialog": ["Escape"], "wait": ["Escape"], "ready": ["Enter"],
            "idle": ["<prompt>", "Enter"]}.get(state, [])


# ------------------------------------------------------------------ one session

def esc_path(home, code):
    return os.path.join(home, "resume-esc-%s" % code)


class Run:
    def __init__(self, home, cfg, row):
        self.home = home
        self.c = conf(cfg)
        self.row = row
        self.pane = row.get("tmux_pane")
        self.nlines = int(self.c.get("lines") or 40)
        self.keys = []

    def screen(self):
        return actions._screen(self.pane, self.nlines)  # noqa: SLF001

    def whole(self):
        return actions._screen_all(self.pane) or []  # noqa: SLF001

    def send(self, *keys, literal=False):
        self.keys.append("<prompt>" if literal else " ".join(keys))
        return actions._send(self.pane, *keys, literal=literal)  # noqa: SLF001

    def escape(self):
        """Escape, at least esc_gap_seconds after the last one the board sent this session (in
        this run or an earlier one: resume-esc-CODE). Runs hold the session lock."""
        gap = float(self.c.get("esc_gap_seconds") or 1.5)
        path = esc_path(self.home, self.row["code"])
        try:
            with open(path, encoding="utf-8") as fh:
                last = float(fh.read().strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        wait = last + gap - time.time()
        if wait > 0:
            time.sleep(min(wait, gap))
        ok = self.send("Escape")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("%.3f\n" % time.time())
        except OSError:
            pass
        return ok

    def next_state(self, prev, deadline):
        """Polls until the screen is no longer PREV (or DEADLINE): (state, detail)."""
        while True:
            actions._settle()  # noqa: SLF001
            st, why = classify(self.screen(), self.c)
            if st != prev or time.time() >= deadline:
                return st, why
            time.sleep(0.2)

    def go(self, state, detail):
        """Walks from STATE to a submitted prompt. Returns (result, reason)."""
        secs = float(self.c.get("confirm_seconds") or 10)
        for _ in range(int(self.c.get("max_steps") or 4)):
            if actions.session_now(self.row)[0] is None:
                return "failed", "the session ended"
            if state in ("dialog", "wait"):
                if not self.escape():
                    return "failed", "send-keys failed (Escape)"
                new, why = self.next_state(state, time.time() + secs)
                if new not in NEXT[state]:
                    shown = "the %s is still there" % state if new == state else "the screen shows %s (%s)" % (new, why)
                    return "partial", "after Escape on the %s %s; nothing else sent" % (state, shown)
                state, detail = new, why
                continue
            if state == "ready":
                before = self.whole()
                if not self.send("Enter"):
                    return "partial", "send-keys failed (Enter)"
                return self.confirm(before, None)
            if state == "idle":
                return self.submit()
            break
        return "partial", "too many steps; stopped at %s (%s)" % (state, detail)

    def submit(self):
        prompt = prompt_text(self.c)
        if prompt is None:
            return ("refused" if not self.keys else "partial"), "limit_resume.prompt is empty, too long or has control characters"
        st, why = classify(self.screen(), self.c)
        if st != "idle":
            return ("refused" if not self.keys else "partial"), "not idle under a usage-limit notice before typing (%s)" % why
        # tmux reads a trailing ";" as a command separator.
        text = prompt[:-1] + "\\;" if prompt.endswith(";") else prompt
        if not self.send(text, literal=True):
            return "partial", "send-keys failed (typing the prompt)"
        deadline = time.time() + 3
        while True:
            actions._settle()  # noqa: SLF001
            box = input_box(self.screen() or [], self.c)
            if box and same_text(box[2], prompt):
                break
            if time.time() >= deadline:
                return "partial", "the prompt box does not show the resume prompt after typing; Enter not pressed (clear it in the session)"
            time.sleep(0.2)
        reg, _st = actions.session_now(self.row)
        if reg is None:
            return "partial", "the session ended while typing; Enter not pressed"
        before = self.whole()
        if not self.send("Enter"):
            return "partial", "send-keys failed (Enter)"
        return self.confirm(before, prompt)

    def confirm(self, before, prompt):
        """After Enter: busy (registry or screen), or the prompt echoed above an empty box."""
        secs = float(self.c.get("confirm_seconds") or 10)
        deadline = time.time() + secs
        echo = (MARK + " " + _norm(prompt)[:40]) if prompt else (MARK + " " + _norm(str(self.c.get("prefill") or "")))

        def echoes(ls):
            """Echoed prompts above the input box (the box itself may hold the same text)."""
            ls = ls or []
            b = input_box(ls, self.c)
            return sum(1 for ln in ls[:b[0] - 1 if b else len(ls)] if _norm(ln).startswith(echo))
        while True:
            reg, _st = actions.session_now(self.row)
            if reg is None:
                return "failed", "the session ended"
            whole = self.whole()
            lines = whole[-self.nlines:]
            if reg.get("status") == "busy" or actions._has(self.c, "busy_regex", lines[-12:]):  # noqa: SLF001
                return "resumed", "the session is working again"
            st, _why = classify(lines, self.c)
            if st in ("dialog", "wait"):
                return "limited", "Claude shows the usage limit again (%s): no quota yet? Nothing else sent" % st
            box = input_box(lines, self.c)
            if box and box[2] == "" and echoes(whole) > echoes(before):
                return "resumed", "the prompt was submitted"
            if time.time() >= deadline:
                return "unconfirmed", "the session did not start working within %gs; nothing else sent" % secs
            _sleep()


def _sleep():
    actions._sleep("BOARD_ACTION_POLL", "0.5")  # noqa: SLF001


def resume_one(home, cfg, row, by=None, dry_run=False, lock_wait=None):
    """(result, reason, state, keys, snippet) for one snapshot row, with every guard; audited
    (the fixed reason and an explicit reason_code; the snippet is for the terminal only)."""
    c = conf(cfg)
    state, keys, ev, rcode = None, [], {}, None
    code = str(row.get("code") or "")
    if not actions.CODE_RE.fullmatch(code):
        result, reason, rcode = "refused", "not a session code the board acts on", "not_limited"
    elif not row.get("session_id") or not row.get("pid"):
        result, reason, rcode = "refused", "the session is not running", "offline"
    elif actions.session_now(row)[0] is None:
        result, reason, rcode = "refused", "the session is not running", "offline"
    elif not row.get("tmux_pane"):
        result, reason, rcode = "refused", "the session has no tmux pane", "pane"
    elif actions.check_pane(row, row["tmux_pane"]):
        result, reason, rcode = "refused", actions.check_pane(row, row["tmux_pane"]), "pane"
    else:
        lock = actions.SessionLock(home, code)
        wait = float(c.get("lock_wait_seconds") or 20) if lock_wait is None else lock_wait
        if not lock.acquire(wait):
            result, reason, rcode = "refused", "another action runs on this session; nothing sent", "locked"
        else:
            try:
                r = Run(home, cfg, row)
                state, detail = classify(r.screen(), r.c, ev)
                if state not in LIMIT_STATES:
                    result, reason, rcode = "refused", "not at a usage limit on screen: %s; nothing sent" % detail, "not_limited"
                    if detail != "capture-pane failed":
                        remember(home, row, None)   # the card's chip follows at once
                elif dry_run:
                    keys = plan(state)
                    result, reason = "dry-run", detail
                elif state == "idle" and prompt_text(r.c) is None:
                    result, reason, rcode = "refused", "limit_resume.prompt is empty, too long or has control characters; nothing sent", "config"
                else:
                    try:
                        result, reason = r.go(state, detail)
                    except Exception as exc:  # noqa: BLE001 - one session never stops the rest
                        result, reason = "failed", "error: %s" % exc.__class__.__name__
                    if result == "refused":
                        rcode = "config" if "limit_resume.prompt" in reason else "not_limited"
                    keys = r.keys
                    after, _d = classify(r.screen(), r.c)
                    remember(home, row, after if after in LIMIT_STATES else None)
            finally:
                lock.release()
    log(home, {"code": code, "result": result, "reason": reason, "reason_code": rcode, "state": state,
               "keys": keys, "by": by, "dry_run": bool(dry_run), "pane": row.get("tmux_pane")})
    return result, reason, state, keys, ev.get("snippet")


def log(home, ev):
    prefs._log(home, dict({"ts": now_ms(), "type": "resume"}, **ev))  # noqa: SLF001


# ------------------------------------------------------------------ the collector's look

def scan_path(home):
    return os.path.join(home, SCAN_FILE)


def load_scan(home):
    try:
        with open(scan_path(home), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) and isinstance(d.get("sessions"), dict) else {"at": 0, "sessions": {}}
    except (OSError, ValueError):
        return {"at": 0, "sessions": {}}


def save_scan(home, doc):
    path = scan_path(home)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, sort_keys=True)
    os.replace(tmp, path)


def update_scan(home, fn):
    """Read-modify-write of resume-scan.json under flock resume-scan.lock (the background look
    and `board resume` both write it)."""
    with open(os.path.join(home, SCAN_LOCK), "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            doc = load_scan(home)
            fn(doc)
            save_scan(home, doc)
            return doc
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _entry(prev, sid, state, now):
    since = prev.get("since") if prev and prev.get("sid") == sid and prev.get("state") == state else now
    return {"sid": sid, "state": state, "since": since, "at": now}


def remember(home, row, state, now=None):
    """Records one session's state right after a resume, so the board follows at once."""
    now = now if now is not None else now_ms()
    code = str(row.get("code"))

    def fn(doc):
        doc["sessions"][code] = _entry(doc["sessions"].get(code), row.get("session_id"), state, now)
    try:
        update_scan(home, fn)
    except OSError:
        pass


def look(cfg, row):
    """The state of one running session's pane, read-only; None when it cannot be read."""
    pane = row.get("tmux_pane")
    if (not pane or not row.get("session_id") or not row.get("pid") or row.get("state") == "offline"
            or not actions.CODE_RE.fullmatch(str(row.get("code") or ""))):
        return None
    if actions.check_pane(row, pane):
        return None
    c = conf(cfg)
    st, _why = classify(actions._screen(pane, int(c.get("lines") or 40)), c)  # noqa: SLF001
    return st


def scan(home, cfg, sessions, now):
    """Classifies every running session's pane (keeping the last look of one an action is running
    on) and merges it into resume-scan.json: an entry written after this scan started (by `board
    resume`) wins over this look."""
    looked, keep = {}, set()
    for r in sessions:
        code = str(r.get("code") or "")
        if not actions.CODE_RE.fullmatch(code):
            continue
        if actions.run_busy(home, code):
            keep.add(code)
            continue
        st = look(cfg, r)
        if st is not None:
            looked[code] = (r.get("session_id"), st if st in LIMIT_STATES else None)

    def fn(doc):
        cur = doc.get("sessions") or {}
        out = {}
        for code, (sid, st) in looked.items():
            out[code] = _entry(cur.get(code), sid, st, now)
        for code, e in cur.items():
            if isinstance(e, dict) and (code in keep or int(e.get("at") or 0) > now):
                out[code] = e
        doc["sessions"], doc["at"] = out, now
    return update_scan(home, fn)


def scan_running(home):
    try:
        with open(os.path.join(home, SCAN_RUN_LOCK), "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def scan_store(home, cfg):
    """The background look (resume.py --scan-store): one at a time; the sessions come from the
    last snapshot. Never blocks the collector, whatever tmux does."""
    with open(os.path.join(home, SCAN_RUN_LOCK), "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        scan(home, cfg, actions.snapshot_rows(home), now_ms())
    return 0


def spawn_scan(home):
    try:
        subprocess.Popen([sys.executable, os.path.realpath(__file__), "--scan-store"],  # noqa: S603
                         env=dict(os.environ, BOARD_HOME=home), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def annotate(sessions, cfg, home, now, do_scan=False):
    """Adds `limit` ({state, since}) to the rows the last look found at a usage limit. With
    DO_SCAN (the looping collector) a new look starts in the background every scan_seconds (0:
    never); its result shows from the next collector run on."""
    c = conf(cfg)
    every = float(c.get("scan_seconds") or 0)
    doc = load_scan(home)
    if do_scan and every > 0 and now - int(doc.get("at") or 0) >= every * 1000 and not scan_running(home):
        spawn_scan(home)
    max_age = max(120.0, 4 * every) * 1000
    seen = doc.get("sessions") or {}
    for r in sessions:
        e = seen.get(str(r.get("code") or ""))
        if (isinstance(e, dict) and e.get("state") in LIMIT_STATES and e.get("sid") == r.get("session_id")
                and r.get("state") != "offline" and now - int(e.get("at") or 0) <= max_age):
            r["limit"] = {"state": e["state"], "since": e.get("since")}
    return sessions


# ------------------------------------------------------------------ CLI

def rows(home):
    actions.refresh_snapshot()
    return actions.snapshot_rows(home)


def run(home, cfg, codes=None, all_=False, by=None, dry_run=False):
    """[(code, result, reason, state, keys, snippet)]; --all reports the sessions not at a limit
    as skipped (nothing sent, not audited)."""
    out = []
    snap = rows(home)
    if not all_:
        by_code = {r["code"]: r for r in snap}
        for code in codes:
            out.append((code,) + resume_one(home, cfg, by_code.get(code) or {"code": code}, by, dry_run))
        return out
    for r in snap:
        if not r.get("session_id") or r.get("state") == "offline" or not r.get("tmux_pane"):
            continue
        st = look(cfg, r)
        if st not in LIMIT_STATES:
            out.append((r["code"], "skipped", "not at a usage limit (%s)" % (st or "pane not readable"), st, [], None))
            continue
        out.append((r["code"],) + resume_one(home, cfg, r, by, dry_run))
    return out


def exit_for(results):
    return max([EXIT.get(r[1], 1) for r in results] or [0])


def main(argv):
    ap = argparse.ArgumentParser(prog="board resume", description="resume sessions stuck on a usage limit")
    ap.add_argument("codes", nargs="*", metavar="CODE")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--scan-store", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--by", default="cli")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    home = fleet.ensure_home()
    cfg = fleet.load_fleet()
    try:
        if args.scan_store:
            return scan_store(home, cfg)
        if args.scan:
            if args.codes or args.all or args.dry_run:
                raise UsageError("--scan takes no CODE, --all or --dry-run")
            found = {}
            for r in rows(home):
                st = look(cfg, r)
                if st is not None:
                    found[r["code"]] = st
            if args.json:
                print(json.dumps(found, sort_keys=True))
            else:
                for code, st in sorted(found.items()):
                    print("%-16s %s" % (fleet.safe_text(code), st))
            return 0
        if bool(args.codes) == bool(args.all):
            raise UsageError("give session CODEs or --all")
        if len(args.codes) > MAX_CODES or len(set(args.codes)) != len(args.codes):
            raise UsageError("at most %d distinct session CODEs" % MAX_CODES)
        known = {s["code"] for s in cfg["sessions"]} | {r["code"] for r in actions.snapshot_rows(home)}
        for code in args.codes:
            actions.check_code(code)
            if code not in known:
                raise UsageError("no such session: %s" % code)
        actions.check_by(args.by)
        results = run(home, cfg, args.codes, args.all, args.by, args.dry_run)
        if args.json:
            print(json.dumps([{"code": r[0], "result": r[1], "reason": r[2], "state": r[3], "keys": r[4]}
                              for r in results], ensure_ascii=False))
        else:
            if args.all and not results:
                print("no running session with a tmux pane")
            for code, result, reason, state, keys, snippet in results:
                extra = ""
                if args.dry_run and result == "dry-run":
                    extra = " — would send: %s, then check each next screen" % " ".join(keys)
                elif keys:
                    extra = " — sent: %s" % ", ".join(keys)
                seen = " — screen: \"%s\"" % fleet.safe_text(snippet) if snippet else ""
                print("%s: %s [%s] (%s)%s%s" % (fleet.safe_text(code), result, state or "-", fleet.safe_text(reason), extra, seen))
        if args.dry_run:
            return 0 if all(r[1] in ("dry-run", "skipped") for r in results) else exit_for(results)
        return exit_for(results)
    except (UsageError, actions.UsageError) as exc:
        print("board: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("board: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
