#!/usr/bin/env python3
"""Build $BOARD_HOME/snapshot.json from the Claude Code session registry, the hook state
files and the decision files (see docs/contract.md).

Usage: collect.py [--no-notify] [--baseline] [--verbose]

- Writes snapshot.json atomically, and only when its content (excluding generated_at) changed.
- Marks pending decisions `timeout` after decision_timeout_hours, and permission decisions
  `stale` once the session no longer shows that tool_use_id as pending.
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
- Card details per session: `title` (the pane title Claude Code sets, from one batched
  `tmux list-panes -a`), `repo` / `branch` (basename of the git toplevel of cwd and the current
  branch, cached 30 s in $BOARD_HOME/gitinfo.json), `last_event`, `notification_type` and `recent`
  (the tool calls in flight: tool name + the hook's redacted summary). All of it is untrusted text:
  control characters removed, paths cut to their last component, clipped. Never a full path.
"""
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import away  # noqa: E402
import fleet  # noqa: E402
import limit  # noqa: E402
import notify  # noqa: E402
import resources  # noqa: E402

FINAL = ("relayed", "stale", "timeout", "unauthorized")
TITLE_MAX = 120
SUMMARY_MAX = 160
NAME_MAX = 80
RECENT_MAX = 3
GIT_TTL_MS = 30 * 1000
# An absolute or home path (Unix or Windows), not part of a URL or a word: replaced by "…/<last part>".
PATH_RE = re.compile(r"(?<![\w.:/~\\-])(?:~(?=/)|/|[A-Za-z]:\\)[^\s'\"`<>|;&()]+")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?)")


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

    def short(m):
        tail = re.split(r"[/\\]", m.group(0).rstrip("/\\"))[-1]
        return "…/" + tail if tail and tail != "~" else "…"
    text = " ".join(PATH_RE.sub(short, text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def hostnames():
    names = set()
    try:
        full = socket.gethostname()
        names.update({full.lower(), full.split(".")[0].lower()})
    except OSError:
        pass
    return {n for n in names if n}


def clean_title(raw, hosts):
    """A pane title as Claude Code sets it ("✳ Fix the login form"): the leading status glyph
    (spinner, ✳, ...) dropped. tmux's default title is the host name: never published."""
    if not isinstance(raw, str):
        return ""
    t = raw.strip()
    while t and unicodedata.category(t[0])[0] not in "LN":
        t = t[1:]
    if not t or t.lower() in hosts or t.lower() == "claude code":
        return ""
    return clean_text(t, TITLE_MAX)


def pane_titles():
    """{pane_id: raw title} for every tmux pane, in one call. {} without tmux."""
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_title}"],
                           capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode != 0:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        pane, _, title = line.partition("\t")
        if pane.startswith("%"):
            out[pane] = title
    return out


def git_where(cwd):
    """(repo, branch) for a working directory: the basename of its git toplevel and the current
    branch ("detached" for a detached HEAD). (None, None) outside a repo."""
    if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
        return None, None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")

    def git(*args):
        try:
            r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True,
                               timeout=3, check=False, env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None
    top = git("rev-parse", "--show-toplevel")
    if not top:
        return None, None
    branch = git("symbolic-ref", "--short", "-q", "HEAD") or "detached"
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
        r["title"] = clean_title(titles.get(r.get("tmux_pane") or ""), hosts) or None if r.get("pid") else None
        g = info.get(r.get("cwd")) if r.get("pid") else None
        r["repo"] = (g or {}).get("repo")
        r["branch"] = (g or {}).get("branch")


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


def process_decisions(cfg, home, sessions, now):
    by_sid = {s["session_id"]: s for s in sessions if s.get("session_id")}
    timeout_ms = float(cfg.get("decision_timeout_hours") or 24) * 3600 * 1000
    out = []
    for path in sorted(glob.glob(os.path.join(home, "decisions", "*.json"))):
        d = read_json(path)
        if not isinstance(d, dict) or not d.get("id"):
            continue
        changed = False
        if d.get("kind") == "permission":
            sess = by_sid.get(d.get("session_id"))
            if not d.get("from") and sess:
                d["from"] = sess["code"]
                changed = True
            if d.get("status") in ("pending", "answered"):
                pend = (sess or {}).get("pending") or {}
                if not sess or sess.get("state") != "needs-you" or pend.get("tool_use_id") != d.get("tool_use_id"):
                    d["status"] = "stale"
                    changed = True
        if d.get("status") == "pending" and now - int(d.get("created_at") or 0) > timeout_ms:
            d["status"] = "timeout"
            changed = True
        if changed:
            d["updated_at"] = now
            write_json_atomic(path, d)
            append_event(home, {"ts": now, "type": "decision", "id": d["id"], "status": d["status"],
                                "by": "collector"})
        out.append(d)
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


def resources_digest(cfg, home, sessions, verbose=False):
    """Sample resources when due; never breaks the collector."""
    try:
        return resources.step(cfg, home, sessions)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print("resources: sample failed: %s: %s" % (type(exc).__name__, exc))
        return None


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
        if d.get("kind") == "permission" and not fleet.is_production(d):
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
    card_details(sessions, home, now)
    decisions = process_decisions(cfg, home, sessions, now)
    tasks = load_tasks(home)
    attach_tasks(sessions, tasks)
    snap = {"generated_at": now, "sessions": sessions, "decisions": decisions, "tasks": tasks,
            "limit": limit.active(), "owner_away": away.away_record(home),
            "consensus": away.consensus_items(home, now),
            "resources": resources_digest(cfg, home, sessions, verbose)}

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
    if "--no-notify" not in argv:
        for r in notify.tick():
            if verbose and r.get("status") not in (None, "deduped"):
                print("tick: %s %s %s" % (r.get("key"), r.get("level"), r.get("status") or r.get("error")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
