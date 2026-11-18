#!/usr/bin/env python3
"""Build $BOARD_HOME/snapshot.json from the Claude Code session registry, the hook state
files and the decision files (see docs/contract.md).

Usage: collect.py [--no-notify] [--baseline] [--verbose] [--apply-rc] [--scan-limits]

- Writes snapshot.json atomically, and only when its content (excluding generated_at) changed.
- Marks pending decisions `timeout` after decision_timeout_hours, and permission decisions and
  pane questions (source=pane) `stale` once the session no longer shows that tool_use_id as pending.
- Alerts through lib/notify.py (board-notify): on transitions into needs-you, new pending
  decisions and a new usage-limit episode, then notify.tick() on every run (escalation,
  auto-ack, repeats, usage thresholds). --no-notify skips all of it.
- Alert-storm guard: when $BOARD_HOME/alerts.json does not exist yet (first run), every
  condition already open is acked as a baseline instead of alerted. --baseline does only that.
- Adds the tasks ($BOARD_HOME/tasks/*.json) and the active usage limit to the snapshot.
- Adds owner-away mode (`owner_away`: null or {since, by, said}) and the consensus items that are
  pending or were decided in the last 24 h (`consensus`, lib/away.py).
- Samples system resources every resources.interval seconds into resources.json (lib/resources.py)
  and adds only their digest (`resources`: null or {level, worst}) to the snapshot, so raw numbers
  never change snapshot.json.
- The Claude account the sessions run on: only oauthAccount.emailAddress / organizationName from
  ~/.claude.json (re-read when its mtime changes; never any token) into $BOARD_HOME/account.json
  (0600). Off with BOARD_SHOW_ACCOUNT=0 or fleet.yaml usage.show_account: false (the file is then
  removed). board-serve shows the email to approvers only; the operator never mirrors it.
- Card details per session: `title` (the pane title Claude Code sets, from one batched
  `tmux list-panes -a`), `repo` / `branch` (basename of the git toplevel of cwd and the current
  branch, cached 30 s in $BOARD_HOME/gitinfo.json), `last_event`, `notification_type` and `recent`
  (the tool calls in flight: tool name + the hook's redacted summary). All of it is untrusted text:
  control characters removed, paths cut to their last component, clipped. Never a full path.
- Usage burn rate: feeds the 5-hour reading of usage.json into a small ring buffer
  ($BOARD_HOME/usage-samples.json: one sample per 20 s, an hour kept, emptied when the window
  resets) and stores the burn rate there (a least-squares line through the last 30 min, projected
  to 100%). board-serve shows it as operator.usage.burn on the Deck panel.
- Card extras (teammates and their tasks, the statusline meter, recent tool calls, the last
  assistant text and prompt, ahead/behind, time in state) go to $BOARD_HOME/cards.json through
  lib/cards.py, never into the snapshot.
- Remote Control per session (`rc`: {on, want, source, blocked, pending, last}, lib/rc.py)
  and session actions (`compact`: {queued, last, done_at}, lib/actions.py). With --apply-rc
  (the looping board-collect passes it) what is pending for an idle session (a `board rc`
  choice it does not follow yet, a queued compact) runs then: `actions.py apply --pending
  --auto` in the background, under its own lock and retry rules.
- Sessions stuck on a usage limit (lib/resume.py): `limit` ({state: dialog|wait|ready|idle,
  since}) on the rows whose pane showed one of Claude's limit screens at the last look
  ($BOARD_HOME/resume-scan.json). With --scan-limits (the looping board-collect passes it) the
  running panes are read again (tmux capture-pane, read-only) every limit_resume.scan_seconds.
- Scheduling decision: every run feeds the tasks, the session states and resources.json to
  lib/scheduler.py (a pure function) and writes $BOARD_HOME/scheduler.json only when the decision
  changed; jam episodes and advisory delivery live in schedule-state.json (lib/advisories.py).
  The snapshot only carries `schedule`: {mode, due (sessions with advice to send), jams (open)}.
- Pomodoro tallies: every run's session states advance each session's pomodoro timer
  (lib/pomodoro.py) in $BOARD_HOME/pomodoro.json, never in the snapshot. --no-notify / --baseline
  also hold its milestone notification (off unless fleet.yaml pomodoro.notify).
- Reminders: every run (not with --no-notify or --baseline) fires the owner's due reminders
  (lib/reminders.py, reminders.json): T - lead, T, snoozes; each once, persisted.
"""
import getpass
import glob
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import away  # noqa: E402
import cards  # noqa: E402
import fleet  # noqa: E402
import limit  # noqa: E402
import actions  # noqa: E402
import notify  # noqa: E402
import resume  # noqa: E402
import rc  # noqa: E402
import pomodoro  # noqa: E402
import reconcile  # noqa: E402
import reminders  # noqa: E402
import resources  # noqa: E402
import scheduler  # noqa: E402

FINAL = ("relayed", "stale", "timeout", "unauthorized", "answered_in_pane")
MAYBE_ANSWERED = "maybe_answered_in_session"
TITLE_MAX = 120
SUMMARY_MAX = 160
NAME_MAX = 80
RECENT_MAX = 3
GIT_TTL_MS = 30 * 1000
# A path, not part of a URL or a word: replaced by "…/<last part>". file:// URLs, UNC
# (\\server\share), Windows (C:\ or C:/), home (~/x, ~user/x) and absolute Unix paths; a quoted
# path may contain spaces.
_PATH_START = r"(?:file://|\\\\|[A-Za-z]:[\\/]|~[\w.-]*/|/)"
QUOTED_PATH_RE = re.compile(r"""(?<![\w.:/~\\…-])(["'])(%s[^"'\n]*)\1""" % _PATH_START)
PATH_RE = re.compile(r"""(?<![\w.:/~\\…-])%s[^\s'"`<>|;&()]+""" % _PATH_START)
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?)")
# scheme://user:password@ or scheme://user@ in a URL: the credentials are dropped.
URL_CREDS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@'\"]+@")
# The hook's redaction (hooks/board-hook.sh, def redact), for text that never went through it.
REDACT = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 [REDACTED]"),
    (re.compile(r"xox[abposr]-[A-Za-z0-9-]+"), "[REDACTED]"),
    (re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED]"),
    (re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{16,}"), "[REDACTED]"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    (re.compile(r"AIza[A-Za-z0-9_-]{35}"), "[REDACTED]"),
    (re.compile(r"npm_[A-Za-z0-9]{36}"), "[REDACTED]"),
    (re.compile(r"A[KS]IA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"""(?i)((?<![\w-])--(?:password|passwd|pass|token|secret|api[_-]?key)(?:\s+|=))("[^"]*"|'[^']*'|[^\s"';|&]+)"""),
     r"\1[REDACTED]"),
    (re.compile(r"""((?<![\w-])-p\s+)([^\s"'/.~0-9;|&-][^\s"';|&]*)"""), r"\1[REDACTED]"),
    (re.compile(r"""((?<![\w-])(?:-u|--user)(?:\s+|=))([^\s:"']+:[^\s"']+)"""), r"\1[REDACTED]"),
    (re.compile(r"""(?i)(password|passwd|token|secret|api[_-]?key|access[_-]?key|client[_-]?secret)(["']?)(\s*[=:]\s*)("[^"]*"|'[^']*'|[^\s&'",}]+)"""),
     r"\1\2\3[REDACTED]"),
]
LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
LONG_B64_RE = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
SHELL_TITLE_RE = re.compile(r"\S+@\S+:")  # an OSC title a shell sets: user@host: ~/dir


def redact(text):
    for rx, sub in REDACT:
        text = rx.sub(sub, text)
    text = LONG_HEX_RE.sub(lambda m: m.group(0) if len(m.group(0)) == 40 and re.fullmatch(r"[0-9a-f]+", m.group(0)) else "[REDACTED]", text)

    def b64(m):
        b = m.group(0)
        mixed = re.search(r"[0-9]", b) and re.search(r"[a-z]", b) and re.search(r"[A-Z]", b)
        return "[REDACTED]" if mixed and max(len(x) for x in b.split("/")) >= 24 else b
    return LONG_B64_RE.sub(b64, text)


def now_ms():
    return int(time.time() * 1000)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, data):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def append_event(home, ev):
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def clean_text(value, limit):
    """Untrusted text for a card: no control characters, no paths (only their last component),
    single spaces, at most `limit` characters. "" when nothing is left."""
    if not isinstance(value, str):
        return ""
    text = "".join(" " if unicodedata.category(c)[0] == "C" else c for c in ANSI_RE.sub("", value))
    text = redact(URL_CREDS_RE.sub(r"\1", text))

    def last(path):
        tail = re.split(r"[/\\]", path.rstrip("/\\"))[-1]
        return "…/" + tail if tail and not tail.startswith("~") and not tail.endswith(":") else "…"
    text = QUOTED_PATH_RE.sub(lambda m: m.group(1) + last(m.group(2)) + m.group(1), text)
    text = " ".join(PATH_RE.sub(lambda m: last(m.group(0)), text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def hostnames():
    """This machine's names, lowercased: the full name, the short name and <short>.local."""
    names = set()
    try:
        full = socket.gethostname().lower()
        short = full.split(".")[0]
        names.update({full, short, short + ".local"})
        names.update(n.lower() for n in [socket.getfqdn()] if n)
    except OSError:
        pass
    return {n for n in names if n}


def local_user():
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name known
        return ""


def clean_title(raw, hosts, user=None, names=()):
    """A pane title as the session sets it ("✳ Fix the login form"): the leading status glyph
    (spinner, ✳, ...) dropped. Never published: tmux's default (the host name), a shell's own
    title (user@host: ~/dir), any title naming this host or the local user as a word, or a title
    that is just the session's own code or alias (a /renamed session: the card shows the task)."""
    if not isinstance(raw, str):
        return ""
    t = raw.strip()
    while t and unicodedata.category(t[0])[0] not in "LN":
        t = t[1:]
    if not t or t.lower() == "claude code" or SHELL_TITLE_RE.search(t):
        return ""
    if t.strip().lower() in {str(n).strip().lower() for n in names if n}:
        return ""
    words = set(re.findall(r"[\w.-]+", t.lower()))
    words |= {w.strip(".-") for w in words}
    user = local_user() if user is None else user
    if words & hosts or (user and re.search(r"(?<![\w.-])%s(?![\w-])" % re.escape(user.lower()), t.lower())):
        return ""
    return clean_text(t, TITLE_MAX)


def pane_list():
    """{pane_id: (raw title, pane pid or None)} for every tmux pane, in one call. {} without tmux."""
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_title}\t#{pane_pid}"],
                           capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode != 0:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        pane, _, rest = line.partition("\t")
        title, tab, pid = rest.rpartition("\t")
        if not tab or not pid.isdigit():  # no pid column (a title with no tab): all of it is the title
            title, pid = rest, None
        if pane.startswith("%"):
            out[pane] = (title, int(pid) if pid else None)
    return out


def pane_titles():
    """{pane_id: raw title} for every tmux pane, in one call. {} without tmux."""
    return {k: v[0] for k, v in pane_list().items()}


def git_where(cwd):
    """(repo, branch) for a working directory: the basename of its git toplevel and the current
    branch ("detached" for a detached HEAD). (None, None) outside a repo."""
    if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
        return None, None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")

    # One call. Its exit code is not 0 on a branch with no commits yet, but the top level still
    # prints first; "HEAD" for the branch then means no commits (exit != 0) or detached (exit 0).
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=3, check=False, env=env,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None, None
    lines = r.stdout.splitlines()
    top = lines[0].strip() if lines and lines[0].startswith("/") else None
    if not top:
        return None, None
    branch = lines[1].strip() if len(lines) > 1 else ""
    if branch in ("", "HEAD"):
        branch = "detached" if r.returncode == 0 else "no commits"
    return clean_text(os.path.basename(top.rstrip("/")), NAME_MAX) or None, clean_text(branch, NAME_MAX) or None


def git_info(home, cwds, now):
    """repo / branch per cwd, cached in $BOARD_HOME/gitinfo.json for GIT_TTL_MS."""
    path = os.path.join(home, "gitinfo.json")
    cache = read_json(path)
    cache = cache if isinstance(cache, dict) else {}
    out, changed = {}, False
    for cwd in sorted({c for c in cwds if isinstance(c, str) and c}):
        hit = cache.get(cwd)
        if not (isinstance(hit, dict) and isinstance(hit.get("at"), int) and 0 <= now - hit["at"] < GIT_TTL_MS):
            repo, branch = git_where(cwd)
            hit = {"at": now, "repo": repo, "branch": branch}
            changed = True
        out[cwd] = hit
    if changed or set(cache) != set(out):
        try:
            write_json_atomic(path, out)
        except OSError:
            pass
    return out


def recent_tools(st):
    """The tool calls in flight (the hook's recent_pre), newest last: tool name + clipped summary."""
    out = []
    for r in (st.get("recent_pre") or [])[-RECENT_MAX:]:
        if isinstance(r, dict) and r.get("tool_name"):
            out.append({"tool": clean_text(r["tool_name"], 40), "summary": clean_text(r.get("summary"), SUMMARY_MAX)})
    return out


def card_details(rows, home, now, titles=None, hosts=None):
    """Adds title, repo, branch to each session row (cwd and tmux_pane stay internal)."""
    titles = pane_titles() if titles is None else titles
    hosts = hostnames() if hosts is None else hosts
    info = git_info(home, [r.get("cwd") for r in rows if r.get("pid")], now)
    for r in rows:
        names = [r.get("code"), r.get("name")] + list(r.get("aliases") or [])
        r["title"] = clean_title(titles.get(r.get("tmux_pane") or ""), hosts, names=names) or None if r.get("pid") else None
        g = info.get(r.get("cwd")) if r.get("pid") else None
        r["repo"] = (g or {}).get("repo")
        r["branch"] = (g or {}).get("branch")


def claude_json_path():
    return os.environ.get("CLAUDE_JSON") or os.path.join(os.path.expanduser("~"), ".claude.json")


def account_info(cfg, home):
    """Write $BOARD_HOME/account.json {email, org, source_mtime, updated_at} (0600) from
    ~/.claude.json oauthAccount.emailAddress / organizationName, nothing else. Re-read only when the
    source changed. Disabled: the file is removed. Returns the record or None."""
    path = os.path.join(home, "account.json")
    if not fleet.account_enabled(cfg):
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    src = claude_json_path()
    try:
        mtime = os.stat(src).st_mtime_ns
    except OSError:
        mtime = None
    cur = read_json(path)
    if isinstance(cur, dict) and cur.get("source_mtime") == mtime:
        return cur
    email = org = None
    if mtime is not None:
        data = read_json(src)
        oa = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(oa, dict):
            email = clean_text(oa.get("emailAddress"), 200) or None
            org = clean_text(oa.get("organizationName"), 200) or None
        del data, oa  # nothing else from the file is kept
    rec = {"email": email, "org": org, "source_mtime": mtime, "updated_at": now_ms()}
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return rec


def live_registry():
    out = []
    for path in glob.glob(os.path.join(fleet.sessions_dir(), "*.json")):
        reg = read_json(path)
        if not isinstance(reg, dict):
            continue
        pid = reg.get("pid")
        if not isinstance(pid, int) or not os.path.exists("/proc/%d" % pid):
            continue
        out.append(reg)
    return out


def split_tmux(tmux):
    if not tmux:
        return None, None
    sess = tmux.split(":", 1)[0] or None
    pane = None
    tail = tmux.rsplit(".", 1)[-1] if "." in tmux else ""
    if tail.startswith("%"):
        pane = tail
    return sess, pane


def derive_state(st, status, status_at):
    """Hook state, corrected by the registry's live status (busy|idle|waiting).

    `waiting` means Claude is blocked on the user (permission prompt or a question dialog).
    A hook `needs-you` that the registry has since moved past (newer busy/idle) is dropped,
    in case the hook missed the event that clears it.
    """
    state = st.get("state")
    hook_at = int(st.get("updated_at") or 0)
    if status == "waiting":
        return "needs-you"
    if not state or state == "offline":
        return "working" if status == "busy" else "rest"
    if state == "needs-you" and status in ("busy", "idle") and status_at > hook_at:
        return "working" if status == "busy" else "done"
    return state


def build_sessions(cfg, home, prefix):
    amap = fleet.alias_map(cfg)
    fleet_by_code = {s["code"]: s for s in cfg["sessions"]}
    rows = []
    for reg in live_registry():
        tsess, pane = split_tmux(reg.get("tmux"))
        name = reg.get("name") or None
        raw = tsess or name or str(reg.get("pid"))
        code = amap.get(raw) or (amap.get(name) if not tsess and name else None) or raw
        # A session started or renamed inside another session's tmux: its own registered name, when
        # it maps to a fleet code, says which card it is, not the tmux session it runs in.
        own = amap.get(name) if name else None
        if own and own != code and own in fleet_by_code:
            code = own
        sid = reg.get("sessionId")
        st = read_json(os.path.join(home, "state", "%s.json" % sid)) if sid else None
        st = st if isinstance(st, dict) else {}
        status = reg.get("status")
        state = derive_state(st, status, int(reg.get("statusUpdatedAt") or 0))
        bridge = reg.get("bridgeSessionId")
        aliases = [code] + list((fleet_by_code.get(code) or {}).get("aliases", []))
        if name and name not in aliases:
            aliases.append(name)
        rows.append({
            "code": code,
            "aliases": aliases,
            "name": name,
            "pid": reg.get("pid"),
            "session_id": sid,
            "cwd": reg.get("cwd") or st.get("cwd"),
            "tmux": reg.get("tmux"),
            "tmux_pane": pane or st.get("tmux_pane"),
            "status": status,
            "state": state,
            "pending": st.get("pending") if state == "needs-you" else None,
            "last_event": clean_text(st.get("last_event"), 40) or None,
            "notification_type": clean_text(st.get("notification_type"), 40) or None,
            "recent": recent_tools(st) if state not in ("offline",) else [],
            "remote_url": (prefix + bridge) if bridge else None,
            "updated_at": max(int(st.get("updated_at") or 0), int(reg.get("updatedAt") or 0)),
            "_started": reg.get("startedAt") or 0,
            "_pref": 0 if (name == code or name in (fleet_by_code.get(code) or {}).get("aliases", [])) else 1,
        })
    # Several live processes may share one code (e.g. teammate panes inside a session's tmux).
    # The one whose registered name is the code (or an alias) and that started first keeps it.
    # A process that loses takes the fleet code its own registered name maps to, when no live
    # process holds that code (a session renamed or started inside another session's tmux), so
    # it does not show twice: once as CODE/name and once as its offline fleet entry.
    rows.sort(key=lambda r: (r["code"], r["_pref"], r["_started"]))
    seen, losers = set(), []
    for r in rows:
        if r["code"] in seen:
            losers.append(r)
        seen.add(r["code"])
    for r in losers:
        own = amap.get(r["name"]) if r["name"] else None
        if own and own not in seen:
            r["code"] = own
            r["aliases"] = [own] + [a for a in (fleet_by_code.get(own) or {}).get("aliases", []) if a != own]
            if r["name"] not in r["aliases"]:
                r["aliases"].append(r["name"])
        else:
            r["code"] = "%s/%s" % (r["code"], r["name"] or r["pid"])
            r["aliases"] = [r["code"]]
        seen.add(r["code"])
    for r in rows:
        r.pop("_started", None)
        r.pop("_pref", None)
    for s in cfg["sessions"]:
        if s.get("listed", True) and s["code"] not in seen:
            rows.append({
                "code": s["code"], "aliases": [s["code"]] + s["aliases"], "name": None, "pid": None,
                "session_id": None, "cwd": s.get("cwd"), "tmux": None, "tmux_pane": None,
                "status": None, "state": "offline", "pending": None, "remote_url": None, "updated_at": 0,
                "last_event": None, "notification_type": None, "recent": [],
            })
    rows.sort(key=lambda r: r["code"])
    return rows


def plain_ask(d):
    """A board ask: a question that is not a pane question and not a gate."""
    return d.get("kind") == "question" and d.get("source") != "pane" and not d.get("gate") and not d.get("task_id")


def maybe_answered(d, by_code, home):
    """The hint for a pending board ask: its asking session took a new prompt after the ask was
    created and finished that turn (UserPromptSubmit after created_at, then Stop). Someone may have
    answered it in the pane; it is never closed for that (board resolve does)."""
    sess = by_code.get(str(d.get("from") or "").lower())
    if not sess or not sess.get("session_id"):
        return None
    st = read_json(os.path.join(home, "state", "%s.json" % sess["session_id"]))
    if not isinstance(st, dict):
        return None
    created = int(d.get("created_at") or 0)
    prompt, stop = st.get("last_prompt_at"), st.get("last_stop_at")
    if isinstance(prompt, int) and isinstance(stop, int) and prompt > created and stop > prompt:
        return stop
    return None


def process_decisions(cfg, home, sessions, now):
    by_sid = {s["session_id"]: s for s in sessions if s.get("session_id")}
    by_code = {}
    for s in sessions:
        if s.get("pid"):
            for a in [s.get("code")] + list(s.get("aliases") or []):
                if a:
                    by_code.setdefault(str(a).lower(), s)
    timeout_ms = float(cfg.get("decision_timeout_hours") or 24) * 3600 * 1000
    # What the sessions reported for their prompts (PostToolUse / PostToolUseFailure on
    # events.jsonl, read incrementally; lib/reconcile.py). The hook appends that line before it
    # writes the state file, so a state without the prompt always comes with its outcome.
    outcomes, save_cursor = reconcile.read_outcomes(home, now)
    retry = False  # a settle lost a race with another writer: read those lines again next run
    out = []
    for path in sorted(glob.glob(os.path.join(home, "decisions", "*.json"))):
        d = read_json(path)
        if not isinstance(d, dict) or not d.get("id"):
            continue
        changed = settled = False
        status_read = d.get("status")
        # A pane question (an AskUserQuestion picker) goes stale like a permission prompt.
        if d.get("kind") == "permission" or d.get("source") == "pane":
            sess = by_sid.get(d.get("session_id"))
            if not d.get("from") and sess:
                d["from"] = sess["code"]
                changed = True
            # The session reported how the prompt ended: settle it with what really happened
            # (delivered, or answered in the pane), even when it had gone stale meanwhile.
            oc = outcomes.get(d.get("tool_use_id")) if d.get("tool_use_id") else None
            if oc and now - int(d.get("created_at") or 0) <= reconcile.WINDOW_MS \
                    and int(oc.get("at") or 0) >= int(d.get("created_at") or 0):
                r = reconcile.settle(d, oc, now, home)
                if r == "settled":
                    changed = settled = True
                elif r == "retry":
                    retry = True  # its answer fields are mid-write: read this outcome again next run
            if d.get("status") in ("pending", "answered"):
                pend = (sess or {}).get("pending") or {}
                if not sess or sess.get("state") != "needs-you" or pend.get("tool_use_id") != d.get("tool_use_id"):
                    d["status"] = "stale"
                    d["status_reason"] = "moved_on"  # an answer it had was never delivered
                    changed = True
        if d.get("status") == "pending" and plain_ask(d) and not d.get("hint"):
            at = maybe_answered(d, by_code, home)
            if at:
                d["hint"], d["hint_at"] = MAYBE_ANSWERED, at
                changed = True
        if d.get("status") == "pending" and now - int(d.get("created_at") or 0) > timeout_ms:
            d["status"] = "timeout"
            d["status_reason"] = "timeout"
            changed = True
        if changed:
            # Not under a lock: `board answer` / `board resolve` may have written the file since it
            # was read. Re-read it right before the write and keep theirs when its status moved.
            fresh = read_json(path)
            if not isinstance(fresh, dict) or fresh.get("status") != status_read:
                retry = retry or (settled and isinstance(fresh, dict) and fresh.get("status") in reconcile.SETTLEABLE)
                out.append(fresh if isinstance(fresh, dict) else d)
                continue
            d["updated_at"] = now
            write_json_atomic(path, d)
            ev = {"ts": now, "type": "decision", "id": d["id"], "status": d["status"], "by": "collector"}
            if d.get("reconciled_at") and d["status"] in ("relayed", "answered_in_pane") and status_read != d["status"]:
                ev["reason"] = "session_outcome"
            append_event(home, ev)
        out.append(d)
    if not retry:
        save_cursor()
    keep_ms = 24 * 3600 * 1000
    out = [d for d in out if d.get("status") not in FINAL
           or now - int(d.get("updated_at") or d.get("created_at") or 0) < keep_ms]
    out.sort(key=lambda d: int(d.get("created_at") or 0))
    return out


def load_tasks(home):
    out = []
    for path in glob.glob(os.path.join(home, "tasks", "*.json")):
        t = read_json(path)
        if isinstance(t, dict) and t.get("id"):
            out.append(t)
    out.sort(key=lambda t: (int(t.get("updated_at") or 0), t["id"]))
    return out


def attach_tasks(sessions, tasks):
    """Each session shows the stage of its most recently updated task."""
    for s in sessions:
        mine = [t for t in tasks if t.get("owner") in s["aliases"]]
        cur = mine[-1] if mine else None
        s["task_id"] = cur["id"] if cur else None
        s["stage"] = cur.get("stage") if cur else None


def resources_digest(cfg, home, sessions, verbose=False, now=None):
    """Sample resources when due; never breaks the collector. `now` is the clock the scheduler
    uses too, so resources.json `at` is never later than what it compares it with."""
    try:
        return resources.step(cfg, home, sessions, now)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print("resources: sample failed: %s: %s" % (type(exc).__name__, exc))
        return None


# ------------------------------------------------------------------ usage burn rate
# A small ring buffer of 5-hour usage samples ($BOARD_HOME/usage-samples.json), fed from
# usage.json (the statusline tap), and the burn rate the Deck panel shows: a least-squares line
# through the last 30 min of samples, projected to 100%.
SAMPLES_FILE = "usage-samples.json"
SAMPLE_MIN_GAP_MS = 20 * 1000        # several sessions report the same number: one sample per 20 s
SAMPLE_KEEP_MS = 60 * 60 * 1000      # keep an hour; the fit uses the last 30 min
SAMPLE_MAX = 180
BURN_WINDOW_MS = 30 * 60 * 1000
BURN_MIN_SAMPLES = 3
BURN_MIN_SPAN_MS = 5 * 60 * 1000
BURN_STALE_MS = 15 * 60 * 1000       # no report for 15 min: the projection is not shown
RESET_SHIFT_MS = 5 * 60 * 1000       # resets_at moved further than this: a new window


def _num(v):
    """A finite int or float (json.load accepts NaN and Infinity), else None."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def burn_rate(samples, now, resets_at=None):
    """Burn rate of the 5-hour window from [{t, pct}] (t in epoch ms, pct 0-100).

    status: "danger" when the line reaches 100% before the window resets, "ok" when it doesn't
    (flat or falling, or the reset comes first), "unknown" with fewer than 3 samples or under
    5 min of data in the last 30 min, "stale" when the newest sample is over 15 min old.
    hit_at is the projected time of 100% (epoch ms), or None."""
    pts = sorted((s for s in samples or [] if _num(s.get("t")) is not None and _num(s.get("pct")) is not None),
                 key=lambda s: s["t"])
    out = {"status": "unknown", "pct_per_hour": None, "hit_at": None, "n": 0}
    if pts and now - pts[-1]["t"] > BURN_STALE_MS:
        out["status"] = "stale"
        return out
    recent = [s for s in pts if now - s["t"] <= BURN_WINDOW_MS]
    out["n"] = len(recent)
    if len(recent) < BURN_MIN_SAMPLES or recent[-1]["t"] - recent[0]["t"] < BURN_MIN_SPAN_MS:
        return out
    n = len(recent)
    mx = sum(s["t"] for s in recent) / n
    my = sum(s["pct"] for s in recent) / n
    num = sum((s["t"] - mx) * (s["pct"] - my) for s in recent)
    den = sum((s["t"] - mx) ** 2 for s in recent)
    per_ms = num / den if den else 0.0
    out["pct_per_hour"] = round(per_ms * 3600 * 1000, 2)
    last = recent[-1]
    if last["pct"] >= 100:
        out.update(status="danger", hit_at=int(last["t"]))
        return out
    if per_ms <= 0:
        out["status"] = "ok"
        return out
    hit_at = int(last["t"] + (100 - last["pct"]) / per_ms)
    out["hit_at"] = hit_at
    out["status"] = "danger" if not resets_at or hit_at < resets_at else "ok"
    return out


def add_usage_sample(buf, usage):
    """Adds the 5-hour reading of usage.json to the buffer {resets_at, samples}; returns the new
    buffer. A reading is taken at its own updated_at; an older or repeated one is ignored, one
    within 20 s of the last sample replaces it, and a moved resets_at starts a new window."""
    buf = buf if isinstance(buf, dict) else {}
    samples = [s for s in buf.get("samples") or [] if isinstance(s, dict)
               and _num(s.get("t")) is not None and _num(s.get("pct")) is not None]
    resets = _num(buf.get("resets_at"))
    w = (usage or {}).get("five_hour") if isinstance(usage, dict) else None
    t = _num((usage or {}).get("updated_at")) if isinstance(usage, dict) else None
    pct = _num((w or {}).get("pct")) if isinstance(w, dict) else None
    if t is None or pct is None:
        return {"resets_at": resets, "samples": samples}
    pct = max(0.0, min(100.0, float(pct)))
    new_resets = _num(w.get("resets_at"))
    if new_resets is not None and resets is not None and abs(new_resets - resets) > RESET_SHIFT_MS:
        samples = []
    if new_resets is not None:
        resets = new_resets
    if samples and t <= samples[-1]["t"]:
        return {"resets_at": resets, "samples": samples}
    if samples and t - samples[-1]["t"] < SAMPLE_MIN_GAP_MS:
        samples.pop()
    samples.append({"t": int(t), "pct": pct})
    samples = [s for s in samples if t - s["t"] <= SAMPLE_KEEP_MS][-SAMPLE_MAX:]
    return {"resets_at": resets, "samples": samples}


def usage_samples(home, now):
    """Feeds usage.json into usage-samples.json and stores the burn rate there. Written only when
    the samples or the burn rate changed. Returns the burn rate (or None)."""
    path = os.path.join(home, SAMPLES_FILE)
    old = read_json(path)
    old = old if isinstance(old, dict) else {}
    buf = add_usage_sample(old, read_json(os.path.join(home, "usage.json")))
    if not buf["samples"] and not old:
        return None
    burn = burn_rate(buf["samples"], now, buf["resets_at"])
    new = {"resets_at": buf["resets_at"], "samples": buf["samples"], "burn": burn}
    if {k: old.get(k) for k in new} != new:
        new["computed_at"] = now
        write_json_atomic(path, new)
    return burn


def alert(prev, snap, verbose):
    """Per-event alerts (board-notify), so the first alert does not wait for the next tick().
    tick() is the safety net: it opens alerts for anything these calls miss."""
    prev_state = {s["code"]: s.get("state") for s in prev.get("sessions", [])}
    prev_pending = {d["id"] for d in prev.get("decisions", []) if d.get("status") == "pending"}
    for s in snap["sessions"]:
        if s["state"] == "needs-you" and prev_state.get(s["code"]) != "needs-you":
            r = notify.on_needs_you(s, snap["decisions"])
            if verbose:
                print("alert: %s %s" % (r.get("key"), r.get("status")))
    for d in snap["decisions"]:
        if d.get("status") != "pending" or d["id"] in prev_pending:
            continue
        if (d.get("kind") == "permission" or d.get("source") == "pane") and not fleet.is_production(d):
            continue  # the session's needs-you alert covers it
        r = notify.on_decision(d)
        if verbose:
            print("alert: %s %s" % (r.get("key"), r.get("status")))
    lim = snap.get("limit")
    if lim and (prev.get("limit") or {}).get("hit_at") != lim.get("hit_at"):
        n = sum(1 for d in snap["decisions"] if d.get("status") == "pending")
        r = notify.on_limit_hit(lim, n)
        if verbose:
            print("alert: %s %s" % (r.get("key"), r.get("status")))


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
        return 0
    verbose = "--verbose" in argv or "-v" in argv
    home = fleet.ensure_home()
    for sub in ("state", "decisions", "outbox", "tasks"):
        os.makedirs(os.path.join(home, sub), exist_ok=True)
    cfg = fleet.load_fleet()
    now = now_ms()
    sessions = build_sessions(cfg, home, str(cfg.get("remote_url_prefix") or ""))
    panes, hosts = pane_list(), hostnames()
    card_details(sessions, home, now, titles={k: v[0] for k, v in panes.items()}, hosts=hosts)
    try:
        account_info(cfg, home)
    except (OSError, ValueError) as exc:  # never breaks the collector
        if verbose:
            print("account: %s" % exc)
    try:
        usage_samples(home, now)
    except (OSError, ValueError, TypeError) as exc:  # never breaks the collector
        if verbose:
            print("usage samples: %s" % exc)
    decisions = process_decisions(cfg, home, sessions, now)
    tasks = load_tasks(home)
    attach_tasks(sessions, tasks)
    try:
        rc.annotate(sessions, cfg, home, now)
        actions.annotate(sessions, cfg, home, now)
    except (OSError, ValueError) as exc:  # never breaks the collector
        if verbose:
            print("rc: %s" % exc)
    try:  # sessions stuck on a usage limit (lib/resume.py): `limit` on their rows
        resume.annotate(sessions, cfg, home, now, do_scan="--scan-limits" in argv)
    except Exception as exc:  # noqa: BLE001 - never breaks the collector
        if verbose:
            print("limit scan: %s: %s" % (type(exc).__name__, exc))
    # Resources and the scheduler share one clock: resources.now_ms() (BOARD_NOW_MS in tests), so
    # resources.json `at` is never later than the `now` the scheduler judges its freshness by.
    res_now = resources.now_ms()
    snap = {"generated_at": now, "sessions": sessions, "decisions": decisions, "tasks": tasks,
            "limit": limit.active(), "owner_away": away.away_record(home),
            "consensus": away.consensus_items(home, now),
            "resources": resources_digest(cfg, home, sessions, verbose, res_now)}
    cdoc = None
    try:  # card extras (lib/cards.py): cards.json, read by board-serve only, never in the snapshot
        cdoc = cards.step(cfg, home, sessions, now, panes, clean_text, clean_title, hosts)
    except Exception as exc:  # noqa: BLE001 - never breaks the collector
        if verbose:
            print("cards: %s: %s" % (type(exc).__name__, exc))
    try:  # pomodoro tallies (lib/pomodoro.py): pomodoro.json, zero tokens
        pomodoro.run(cfg, home, sessions, now, notify_ok=not ({"--no-notify", "--baseline"} & set(argv)),
                     busy=pomodoro.busy_from_cards(cdoc))  # the teammates' pane spinners, already read
    except Exception as exc:  # noqa: BLE001 - never breaks the collector
        if verbose:
            print("pomodoro: %s: %s" % (type(exc).__name__, exc))
    if "--no-notify" not in argv and "--baseline" not in argv:
        try:  # the owner's reminders (lib/reminders.py): each occurrence fires once, persisted
            for r in reminders.tick(home):
                if verbose:
                    print("reminder: %s %s %s" % (r.get("id"), r.get("phase"), r.get("status")))
        except Exception as exc:  # noqa: BLE001 - never breaks the collector
            if verbose:
                print("reminders: %s: %s" % (type(exc).__name__, exc))
    snap["schedule"] = None
    try:  # the scheduling decision (lib/scheduler.py): scheduler.json, written only when it changes
        sdoc, wrote = scheduler.step(cfg, home, sessions, tasks, res_now)
        snap["schedule"] = scheduler.snapshot_digest(sdoc)   # mode, sessions with advice due, open jams
        if verbose and wrote:
            print("scheduler: updated")
    except Exception as exc:  # noqa: BLE001 - never breaks the collector
        if verbose:
            print("scheduler: %s: %s" % (type(exc).__name__, exc))

    spath = os.path.join(home, "snapshot.json")
    prev = read_json(spath)
    body = dict(snap)
    body.pop("generated_at")
    prev_body = dict(prev) if isinstance(prev, dict) else None
    if prev_body is not None:
        prev_body.pop("generated_at", None)
    changed = prev_body != body
    if changed:
        write_json_atomic(spath, snap)
        if verbose:
            print("snapshot: updated (%d sessions, %d decisions)" % (len(sessions), len(decisions)))
    elif verbose:
        print("snapshot: unchanged")

    first = not os.path.exists(os.path.join(home, "alerts.json"))
    if "--baseline" in argv or ("--no-notify" not in argv and first):
        keys = notify.baseline()
        if verbose or "--baseline" in argv:
            print("alerts: baseline — %d open condition(s) acked%s" % (len(keys), (": " + ", ".join(keys)) if keys else ""))
        if "--baseline" in argv:
            return 0
    elif "--no-notify" not in argv and changed and isinstance(prev, dict):
        alert(prev, snap, verbose)
    if "--apply-rc" in argv:
        todo = actions.auto_candidates(sessions, cfg, home, now)
        if todo and actions.spawn_auto(home, todo) and verbose:
            print("session actions: running in the background for %s" % ", ".join(todo))
    if "--no-notify" not in argv:
        for r in notify.tick():
            if verbose and r.get("status") not in (None, "deduped"):
                print("tick: %s %s %s" % (r.get("key"), r.get("level"), r.get("status") or r.get("error")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
