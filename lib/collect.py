#!/usr/bin/env python3
"""Build $BOARD_HOME/snapshot.json from the Claude Code session registry, the hook state
files and the decision files (see docs/contract.md).

Usage: collect.py [--no-notify] [--verbose]

- Writes snapshot.json atomically, and only when its content (excluding generated_at) changed.
- Marks pending decisions `timeout` after decision_timeout_hours, and permission decisions
  `stale` once the session no longer shows that tool_use_id as pending.
- Notifies (via `board notify`) on transitions into needs-you and on new pending decisions,
  with a per-session cooldown (notify_cooldown_minutes), and exactly once per usage-limit
  episode ($BOARD_HOME/limit.json, see lib/limit.py).
- Adds the tasks ($BOARD_HOME/tasks/*.json) and the active usage limit to the snapshot.
"""
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402
import limit  # noqa: E402

FINAL = ("relayed", "stale", "timeout", "unauthorized")


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
            "remote_url": (prefix + bridge) if bridge else None,
            "updated_at": max(int(st.get("updated_at") or 0), int(reg.get("updatedAt") or 0)),
            "_started": reg.get("startedAt") or 0,
            "_pref": 0 if (name == code or name in (fleet_by_code.get(code) or {}).get("aliases", [])) else 1,
        })
    # Several live processes may share one code (e.g. teammate panes inside a session's tmux).
    # The one whose registered name is the code (or an alias) and that started first keeps it.
    rows.sort(key=lambda r: (r["code"], r["_pref"], r["_started"]))
    seen = set()
    for r in rows:
        if r["code"] in seen:
            r["code"] = "%s/%s" % (r["code"], r["name"] or r["pid"])
            r["aliases"] = [r["code"]]
        seen.add(r["code"])
        r.pop("_started", None)
        r.pop("_pref", None)
    for s in cfg["sessions"]:
        if s.get("listed", True) and s["code"] not in seen:
            rows.append({
                "code": s["code"], "aliases": [s["code"]] + s["aliases"], "name": None, "pid": None,
                "session_id": None, "cwd": s.get("cwd"), "tmux": None, "tmux_pane": None,
                "status": None, "state": "offline", "pending": None, "remote_url": None, "updated_at": 0,
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


def send(text, verbose):
    board = os.path.join(fleet.REPO, "bin", "board")
    try:
        subprocess.run([board, "notify", text], timeout=15, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
    if verbose:
        print("notify: %s" % text)


def notify(home, cfg, prev, snap, now, verbose):
    """prev may be None (first run): then only the usage-limit alert can fire."""
    cooldown_ms = float(cfg.get("notify_cooldown_minutes") or 10) * 60 * 1000
    cpath = os.path.join(home, "slack-cooldown.json")
    cool = read_json(cpath) or {}
    sent = False
    lim = snap.get("limit")
    if lim and cool.get("_limit_hit_at") != lim.get("hit_at"):
        n = sum(1 for d in snap["decisions"] if d.get("status") == "pending")
        send("[switchboard] usage limit hit (%s), resets ~%s; board frozen; decisions pending: %d" % (
            lim.get("kind") or "unknown", limit.fmt_time(lim.get("resets_at"), now), n), verbose)
        cool["_limit_hit_at"] = lim.get("hit_at")
        sent = True
    if prev is None:
        if sent:
            write_json_atomic(cpath, cool)
        return
    prev_state = {s["code"]: s.get("state") for s in prev.get("sessions", [])}
    prev_pending = {d["id"] for d in prev.get("decisions", []) if d.get("status") == "pending"}
    msgs = []  # (code, text)
    for s in snap["sessions"]:
        if s["state"] == "needs-you" and prev_state.get(s["code"]) != "needs-you":
            p = s.get("pending") or {}
            what = ("%s: %s" % (p.get("tool_name") or "tool", (p.get("summary") or "")[:120])) if p else "waiting"
            msgs.append((s["code"], "[switchboard] %s needs you — %s" % (s["code"], what)))
    for d in snap["decisions"]:
        if d.get("status") == "pending" and d["id"] not in prev_pending:
            code = d.get("from") or "?"
            if d.get("kind") == "permission":
                if any(c == code for c, _ in msgs):
                    continue  # already covered by the needs-you message
                text = "[switchboard] %s asks permission — %s: %s" % (
                    code, d.get("tool_name") or "tool", (d.get("summary") or "")[:120])
            else:
                text = "[switchboard] %s asks: %s" % (code, (d.get("question") or "")[:300])
            msgs.append((code, text))
    for code, text in msgs:
        if now - int(cool.get(code) or 0) < cooldown_ms:
            if verbose:
                print("cooldown: %s" % code)
            continue
        send(text, verbose)
        cool[code] = now
        sent = True
    if sent:
        write_json_atomic(cpath, cool)


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
        return 0
    verbose = "--verbose" in argv or "-v" in argv
    home = fleet.board_home()
    for sub in ("state", "decisions", "outbox", "tasks"):
        os.makedirs(os.path.join(home, sub), exist_ok=True)
    cfg = fleet.load_fleet()
    now = now_ms()
    sessions = build_sessions(cfg, home, str(cfg.get("remote_url_prefix") or ""))
    decisions = process_decisions(cfg, home, sessions, now)
    tasks = load_tasks(home)
    attach_tasks(sessions, tasks)
    snap = {"generated_at": now, "sessions": sessions, "decisions": decisions, "tasks": tasks,
            "limit": limit.active()}

    spath = os.path.join(home, "snapshot.json")
    prev = read_json(spath)
    body = dict(snap)
    body.pop("generated_at")
    prev_body = dict(prev) if isinstance(prev, dict) else None
    if prev_body is not None:
        prev_body.pop("generated_at", None)
    if prev_body != body:
        write_json_atomic(spath, snap)
        if verbose:
            print("snapshot: updated (%d sessions, %d decisions)" % (len(sessions), len(decisions)))
        if "--no-notify" not in argv:
            notify(home, cfg, prev if isinstance(prev, dict) else None, snap, now, verbose)
    elif verbose:
        print("snapshot: unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
