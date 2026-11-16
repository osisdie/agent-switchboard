#!/usr/bin/env python3
"""Resume sessions stuck on a usage limit (docs/contract.md, "Resume after a usage limit").

After the account has quota again (another account, or the limit reset), a Claude Code session
that hit the limit sits in one of a few screens. `board resume` reads the pane, recognizes the
screen with patterns (fleet.yaml limit_resume, defaults for Claude Code 2.1.x) and sends the
fewest keys that get it going again:

  dialog  the usage-limit options dialog ("What do you want to do?": Stop and wait / Upgrade /
          extra usage ...): Escape, the dialog's own cancel, which is its "Stop" row. Never a row
          number: the rows move, and some of them spend money or change the account.
  wait    Claude's automatic continue is armed (below the input box: "Continuing automatically
          at 3pm · Esc to cancel"): Escape cancels the wait.
  ready   the box holds Claude's own prefilled "continue" and the limit has reset ("Press enter
          to continue"), or the last reply above it is a limit message: Enter.
  idle    an empty input box and, in the last reply above it, a usage-limit message: the resume
          prompt (limit_resume.prompt) typed literally, read back from the box, then Enter.

Between two keys the pane is read again and the next screen must be one the key leads to
(dialog -> wait|ready|idle, wait -> ready|idle, an empty box after one of our keys counts as idle);
two Escapes are at least esc_gap_seconds apart (a quick double Escape opens Claude's rewind menu).
Any other screen: nothing sent ("refused", exit 3), or, after a first key, nothing more sent
("partial", exit 5). After Enter the session must turn busy (registry status, or "esc to interrupt"
on screen) or echo the prompt; else "unconfirmed" (6), or "limited" (6) when Claude shows the limit
again (no quota yet).

Usage (bin/board runs these):
  resume.py run CODE [--by WHO] [--dry-run]     one session
  resume.py run --all [--by WHO] [--dry-run]    every running session the screen shows at a limit;
                                                the others are reported as skipped, nothing sent
  resume.py scan [--json]                       classify every running session's pane (read-only)

Exit codes: 0 resumed (also --dry-run of a limit screen, and --all with nothing to do), 1 error,
            2 usage, 3 refused: nothing sent (reason_code not_limited: the screen is no known
            limit state; offline, pane, locked), 5 partial: keys sent, prompt not submitted,
            6 submitted but not confirmed, or Claude is still at the limit.

State:
  $BOARD_HOME/resume-scan.json  {at, sessions: {CODE: {sid, state, since, at}}}: the collector's
                                throttled look at the panes (board-collect --scan-limits), what
                                the board's "usage limit" chip and Resume button follow (0600)
  events.jsonl                  {"type": "resume", code, result, reason, reason_code, state,
                                 keys, by, dry_run, pane}: every attempt and refusal
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import actions  # noqa: E402
import fleet  # noqa: E402
import prefs  # noqa: E402

LIMIT_STATES = ("dialog", "wait", "ready", "idle")
SCAN_FILE = "resume-scan.json"
EXIT = {"resumed": 0, "dry-run": 0, "skipped": 0, "refused": 3, "partial": 5, "unconfirmed": 6,
        "limited": 6, "failed": 1}
MARK = "❯"
# What each key may lead to: the next screen must be one of these (an empty box counts as idle).
NEXT = {"dialog": ("wait", "ready", "idle"), "wait": ("ready", "idle")}


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


def limit_dialog(lines, c):
    """The usage-limit options dialog: no input box, nothing running, the dialog footer
    (dialog_footer_regex) and above it, within 20 lines and below the last echoed prompt, its
    title (dialog_title_regex) and one of its own rows (dialog_row_regex). Returns the matched row."""
    foot = [k for k, ln in enumerate(lines) if _rx(c, "dialog_footer_regex").search(ln)]
    if not foot:
        return None
    f = foot[-1]
    top = max(0, f - 20)
    for k in range(f - 1, top - 1, -1):
        if lines[k].startswith(MARK):
            top = k + 1
            break
    body = [ln.replace(MARK, " ") for ln in lines[top:f]]
    if not _any(_rx(c, "dialog_title_regex"), body):
        return None
    return _any(_rx(c, "dialog_row_regex", re.M), [ln.strip() for ln in body])


def classify(lines, c):
    """(state, detail): state is one of LIMIT_STATES, or "none" (an idle session with no limit
    message), "busy", "unknown". Only the pane's visible screen counts, never the scrollback."""
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
            return "dialog", "usage-limit dialog (%s)" % _norm(row)[:80]
        return "unknown", "no input box and no usage-limit dialog on screen"
    i, j, text = box
    below = lines[j + 1:]
    hit = _any(_rx(c, "wait_regex", re.I), below)
    if hit:
        return "wait", "automatic continue is waiting (%s)" % _norm(hit)[:80]
    reply = last_reply(lines, i - 1)
    said = _any(_rx(c, "limit_regex", re.I), reply)
    if text == "":
        why = actions.idle_screen(lines, c)
        if why:
            return "unknown", why
        if said:
            return "idle", "idle after a usage-limit message (%s)" % _norm(said)[:80]
        return "none", "idle, no usage-limit message above the prompt"
    if text == _norm(str(c.get("prefill") or "")) and (said or _any(_rx(c, "ready_regex", re.I), below)):
        return "ready", "Claude's prefilled \"%s\" waits for Enter" % text
    return "unknown", "the prompt box is not empty"


def plan(state):
    """The keys a state starts with (dry run; later steps depend on the next screen)."""
    return {"dialog": ["Escape"], "wait": ["Escape"], "ready": ["Enter"],
            "idle": ["<prompt>", "Enter"]}.get(state, [])


# ------------------------------------------------------------------ one session

class Run:
    def __init__(self, cfg, row):
        self.c = conf(cfg)
        self.row = row
        self.pane = row.get("tmux_pane")
        self.nlines = int(self.c.get("lines") or 40)
        self.keys = []
        self.last_esc = 0.0

    def screen(self):
        return actions._screen(self.pane, self.nlines)  # noqa: SLF001

    def whole(self):
        return actions._screen_all(self.pane) or []  # noqa: SLF001

    def send(self, *keys, literal=False):
        self.keys.append("<prompt>" if literal else " ".join(keys))
        return actions._send(self.pane, *keys, literal=literal)  # noqa: SLF001

    def escape(self):
        gap = float(self.c.get("esc_gap_seconds") or 1.5)
        wait = self.last_esc + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        ok = self.send("Escape")
        self.last_esc = time.time()
        return ok

    def next_state(self, prev, deadline):
        """Polls until the screen is no longer PREV (or DEADLINE): (state, detail)."""
        while True:
            actions._settle()  # noqa: SLF001
            st, why = classify(self.screen(), self.c)
            if st == "none":  # an empty box right after one of our keys: the limit screen is gone
                st, why = "idle", "an empty prompt box after %s" % self.keys[-1]
            if st != prev or time.time() >= deadline:
                return st, why
            time.sleep(0.2)

    def go(self, state, detail):
        """Walks from STATE to a submitted prompt. Returns (result, reason)."""
        secs = float(self.c.get("confirm_seconds") or 10)
        for _ in range(int(self.c.get("max_steps") or 4)):
            if actions.session_now(self.row)[0] is None:
                return self._stop("failed", "the session ended")
            if state in ("dialog", "wait"):
                if not self.escape():
                    return self._stop("failed", "send-keys failed (Escape)")
                new, why = self.next_state(state, time.time() + secs)
                if new not in NEXT[state]:
                    shown = "the %s is still there" % state if new == state else "the screen shows %s" % new
                    return self._stop("partial", "after Escape on the %s %s (%s); nothing else sent" % (state, shown, why))
                state, detail = new, why
                continue
            if state == "ready":
                before = self.whole()
                if not self.send("Enter"):
                    return self._stop("partial", "send-keys failed (Enter)")
                return self.confirm(before, None)
            if state == "idle":
                return self.submit()
            break
        return self._stop("partial", "too many steps; stopped at %s (%s)" % (state, detail))

    def submit(self):
        prompt = prompt_text(self.c)
        if prompt is None:
            return self._stop("refused" if not self.keys else "partial", "limit_resume.prompt is empty, too long or has control characters")
        lines = self.screen()
        st, why = classify(lines, self.c)
        if st not in ("idle", "none"):
            return self._stop("refused" if not self.keys else "partial", "the prompt box is not empty and idle before typing (%s)" % why)
        # tmux reads a trailing ";" as a command separator.
        text = prompt[:-1] + "\\;" if prompt.endswith(";") else prompt
        if not self.send(text, literal=True):
            return self._stop("partial", "send-keys failed (typing the prompt)")
        deadline = time.time() + 3
        while True:
            actions._settle()  # noqa: SLF001
            box = input_box(self.screen() or [], self.c)
            if box and same_text(box[2], prompt):
                break
            if time.time() >= deadline:
                return self._stop("partial", "the prompt box does not show the resume prompt after typing; Enter not pressed (clear it in the session)")
            time.sleep(0.2)
        reg, _st = actions.session_now(self.row)
        if reg is None:
            return self._stop("partial", "the session ended while typing; Enter not pressed")
        before = self.whole()
        if not self.send("Enter"):
            return self._stop("partial", "send-keys failed (Enter)")
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
                return self._stop("failed", "the session ended")
            whole = self.whole()
            lines = whole[-self.nlines:]
            if reg.get("status") == "busy" or actions._has(self.c, "busy_regex", lines[-12:]):  # noqa: SLF001
                return "resumed", "the session is working again"
            st, why = classify(lines, self.c)
            if st in ("dialog", "wait"):
                return "limited", "Claude shows the usage limit again (%s): no quota yet? Nothing else sent" % why
            box = input_box(lines, self.c)
            if box and box[2] == "" and echoes(whole) > echoes(before):
                return "resumed", "the prompt was submitted"
            if time.time() >= deadline:
                return "unconfirmed", "the session did not start working within %gs; nothing else sent" % secs
            _sleep()

    def _stop(self, result, reason):
        return result, reason


def _sleep():
    actions._sleep("BOARD_ACTION_POLL", "0.5")  # noqa: SLF001


def reason_code(result, reason):
    if result != "refused":
        return None
    for code, needle in (("offline", "not running"), ("pane", "pane"), ("locked", "another action"),
                         ("config", "limit_resume.prompt")):
        if needle in reason:
            return code
    return "not_limited"


def resume_one(home, cfg, row, by=None, dry_run=False, lock_wait=None):
    """(result, reason, state, keys) for one snapshot row, with every guard; audited."""
    c = conf(cfg)
    state, keys = None, []
    if not row.get("session_id") or not row.get("pid"):
        res = ("refused", "the session is not running")
    else:
        reg, _st = actions.session_now(row)
        pane = row.get("tmux_pane")
        if reg is None:
            res = ("refused", "the session is not running")
        elif not pane:
            res = ("refused", "the session has no tmux pane")
        else:
            why = actions.check_pane(row, pane)
            if why:
                res = ("refused", why)
            else:
                lock = actions.SessionLock(home, row["code"])
                wait = float(c.get("lock_wait_seconds") or 20) if lock_wait is None else lock_wait
                if not lock.acquire(wait):
                    res = ("refused", "another action runs on this session; nothing sent")
                else:
                    try:
                        r = Run(cfg, row)
                        state, detail = classify(r.screen(), r.c)
                        if state not in LIMIT_STATES:
                            res = ("refused", "not at a usage limit on screen: %s; nothing sent" % detail)
                            if detail != "capture-pane failed":
                                remember(home, row, None)   # the card's chip follows at once
                        elif dry_run:
                            keys = plan(state)
                            res = ("dry-run", detail)
                        elif state == "idle" and prompt_text(r.c) is None:
                            res = ("refused", "limit_resume.prompt is empty, too long or has control characters; nothing sent")
                        else:
                            try:
                                res = r.go(state, detail)
                            except Exception as exc:  # noqa: BLE001 - one session never stops the rest
                                res = ("failed", "error: %s" % exc.__class__.__name__)
                            keys = r.keys
                            after, _d = classify(r.screen(), r.c)
                            remember(home, row, after if after in LIMIT_STATES else None)
                    finally:
                        lock.release()
    result, reason = res
    log(home, {"code": row.get("code"), "result": result, "reason": reason,
               "reason_code": reason_code(result, reason), "state": state, "keys": keys,
               "by": by, "dry_run": bool(dry_run), "pane": row.get("tmux_pane")})
    return result, reason, state, keys


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


def _entry(prev, sid, state, now):
    since = prev.get("since") if prev and prev.get("sid") == sid and prev.get("state") == state else now
    return {"sid": sid, "state": state, "since": since, "at": now}


def remember(home, row, state, now=None):
    """Records one session's state right after a resume, so the board follows at once."""
    now = now if now is not None else now_ms()
    try:
        doc = load_scan(home)
        code = str(row.get("code"))
        doc["sessions"][code] = _entry(doc["sessions"].get(code), row.get("session_id"), state, now)
        save_scan(home, doc)
    except OSError:
        pass


def look(cfg, row):
    """The state of one running session's pane, read-only; None when it cannot be read."""
    pane = row.get("tmux_pane")
    if not pane or not row.get("session_id") or not row.get("pid") or row.get("state") == "offline":
        return None
    if actions.check_pane(row, pane):
        return None
    c = conf(cfg)
    st, _why = classify(actions._screen(pane, int(c.get("lines") or 40)), c)  # noqa: SLF001
    return st


def scan(home, cfg, sessions, now):
    """Classifies every running session's pane (skipping one an action is running on)."""
    prev = load_scan(home).get("sessions") or {}
    out = {}
    for r in sessions:
        code = str(r.get("code") or "")
        if not code or actions.run_busy(home, code):
            if code in prev:
                out[code] = prev[code]
            continue
        st = look(cfg, r)
        if st is None:
            continue
        out[code] = _entry(prev.get(code), r.get("session_id"), st if st in LIMIT_STATES else None, now)
    doc = {"at": now, "sessions": out}
    save_scan(home, doc)
    return doc


def annotate(sessions, cfg, home, now, do_scan=False):
    """Adds `limit` ({state, since}) to the rows the last look found at a usage limit. With
    DO_SCAN (the looping collector) the panes are read again every scan_seconds (0: never)."""
    c = conf(cfg)
    every = float(c.get("scan_seconds") or 0)
    doc = load_scan(home)
    if do_scan and every > 0 and now - int(doc.get("at") or 0) >= every * 1000:
        doc = scan(home, cfg, sessions, now)
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


def run(home, cfg, code=None, all_=False, by=None, dry_run=False):
    """[(code, result, reason, state, keys)]; --all reports the sessions not at a limit as skipped."""
    out = []
    snap = rows(home)
    if not all_:
        row = next((r for r in snap if r["code"] == code), None)
        if row is None:
            row = {"code": code}
        out.append((code,) + resume_one(home, cfg, row, by, dry_run))
        return out
    c = conf(cfg)
    for r in snap:
        if not r.get("session_id") or r.get("state") == "offline" or not r.get("tmux_pane"):
            continue
        st = look(cfg, r)
        if st not in LIMIT_STATES:
            out.append((r["code"], "skipped", "not at a usage limit (%s)" % (st or "pane not readable"), st, []))
            continue
        out.append((r["code"],) + resume_one(home, cfg, r, by, dry_run, lock_wait=float(c.get("lock_wait_seconds") or 20)))
    return out


def exit_for(results):
    return max([EXIT.get(r[1], 1) for r in results] or [0])


def main(argv):
    ap = argparse.ArgumentParser(prog="board resume", description="resume sessions stuck on a usage limit")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("code", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--by", default="cli")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("scan")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    home = fleet.ensure_home()
    cfg = fleet.load_fleet()
    try:
        if args.cmd == "run":
            if bool(args.code) == bool(args.all):
                raise UsageError("give a session CODE or --all")
            if args.code:
                actions.check_code(args.code)
                known = {s["code"] for s in cfg["sessions"]} | {r["code"] for r in actions.snapshot_rows(home)}
                if args.code not in known:
                    raise UsageError("no such session: %s" % args.code)
            actions.check_by(args.by)
            results = run(home, cfg, args.code, args.all, args.by, args.dry_run)
            if args.json:
                print(json.dumps([{"code": r[0], "result": r[1], "reason": r[2], "state": r[3], "keys": r[4]}
                                  for r in results], ensure_ascii=False))
            else:
                if args.all and not results:
                    print("no running session with a tmux pane")
                for code, result, reason, state, keys in results:
                    extra = ""
                    if args.dry_run and result == "dry-run":
                        extra = " — would send: %s, then check each next screen" % " ".join(keys)
                    elif keys:
                        extra = " — sent: %s" % ", ".join(keys)
                    print("%s: %s [%s] (%s)%s" % (fleet.safe_text(code), result, state or "-", fleet.safe_text(reason), extra))
            if args.dry_run:
                return 0 if all(r[1] in ("dry-run", "skipped") for r in results) else exit_for(results)
            return exit_for(results)
        if args.cmd == "scan":
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
    except (UsageError, actions.UsageError) as exc:
        print("board: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("board: %s" % exc, file=sys.stderr)
        return 1
    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
