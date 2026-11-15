#!/usr/bin/env python3
"""Scheduler core: which tasks may start now, and the advisories ($BOARD_HOME/scheduler.json,
docs/contract.md "Scheduler"; design in docs/design/resources-and-scheduling.md).

decide() is a pure function of the tasks, the sessions' states, resources.json, the settings and
the time: no I/O, the same inputs give the same output (and digest). The collector calls step()
on every run; it writes scheduler.json (0600) only when the decision changed, so the file has no
heartbeat. Nothing here sends anything, and nothing ever stops a process: `stop-own-heavy` only
asks a session to stop a heavy process it started itself.

Usage:
  scheduler.py show [--json]        the last decision (--json: scheduler.json as written)
"""
import hashlib
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import tasks as taskstore  # noqa: E402

FILE = "scheduler.json"
SCHEMA_VERSION = 1
LEVELS = ("ok", "warn", "high", "critical")
DEFAULTS = {
    "enabled": True,
    "stop_own_heavy": True,          # at critical: ask a session to stop its own largest heavy process
    "stop_max": 2,                   # ... at most this many sessions at a time (largest first)
    "max_heavy_concurrent": "auto",  # auto: 1 if the machine has < 16 GiB, else 2
    "one_pr_per_repo": True,
    "stall_minutes": [45, 90],       # a running task with no progress this long: stuck warn / high
    "idle_with_work_minutes": 30,    # a session idle this long with a ready task: `resume` (info)
}
STAGE_W = {"plan": .15, "design": .25, "build": .45, "test": .70, "deploy": .85, "production": .85, "maintain": .85}
SIZE_W = {"S": 1.0, "M": .8, "L": .6}
BUSY = ("working", "needs-you")
FREE = ("done", "rest")
STATE_ORDER = ("run", "ready", "queued", "deferred", "blocked", "paused")
SEVERITY_ORDER = ("high", "warn", "info")
RES_STALE_MS = 5 * 60 * 1000         # resources.json older than this: no resource gates
RES_SKEW_MS = 30 * 1000              # ... or ahead of the caller's clock by more than this
RSS_BUCKET_MB = 256                  # process sizes in advisories, so small growth is not a change
HOUR_MS = 3600 * 1000
# Advisory text never reads like an approval, a deploy order or a board decision.
BANNED_RE = re.compile(r"approv|deploy|\[BOARD|decision", re.I)
SAFE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")


def _int(v, default, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return int(max(lo, min(hi, v)))


def settings(cfg):
    c = cfg.get("schedule") if isinstance(cfg, dict) and isinstance(cfg.get("schedule"), dict) else {}
    stall = c.get("stall_minutes")
    stall = stall if isinstance(stall, list) and len(stall) == 2 else DEFAULTS["stall_minutes"]
    warn_m = _int(stall[0], 45, 1, 10080)
    mh = c.get("max_heavy_concurrent", "auto")
    return {
        "enabled": c.get("enabled", True) is not False,
        "stop_own_heavy": c.get("stop_own_heavy", True) is not False,
        "stop_max": _int(c.get("stop_max"), DEFAULTS["stop_max"], 1, 20),
        "max_heavy": "auto" if mh == "auto" else _int(mh, 2, 0, 64),
        "one_pr_per_repo": c.get("one_pr_per_repo", True) is not False,
        "stall_ms": [warn_m * 60000, max(warn_m, _int(stall[1], 90, 1, 10080)) * 60000],
        "idle_ms": _int(c.get("idle_with_work_minutes"), 30, 1, 10080) * 60000,
    }


def _num(v, default=0):
    """A finite number, else DEFAULT (NaN and Infinity parse from JSON in Python)."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else default


def _s(v):
    return v if isinstance(v, str) else None


def _dicts(v):
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


# Inputs are files anyone (or a bug) may have written: normalise them at the boundary, so the
# decision below only ever sees the types it expects.
def clean_task(t):
    if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not taskstore.ID_RE.fullmatch(t["id"]):
        return None
    pr = t.get("pr") if isinstance(t.get("pr"), dict) else {}
    out = {k: _s(t.get(k)) for k in ("owner", "stage", "size", "repo", "branch", "base")}
    out.update({
        "id": t["id"], "status": taskstore.status_of(t) if isinstance(t.get("status"), (str, type(None))) else "active",
        "heavy": t.get("heavy") is True, "priority": _int(t.get("priority"), 1, 0, 3),
        "gates": [{k: _s(g.get(k)) for k in ("gate", "status", "decision_id")} for g in _dicts(t.get("gates"))],
        "depends_on": [{"task": d["task"], "kind": _s(d.get("kind")) or "done"}
                       for d in _dicts(t.get("depends_on")) if isinstance(d.get("task"), str)],
        "pr": {k: _s(pr.get(k)) for k in ("state", "method", "head_sha")},
    })
    out.update({k: int(_num(t.get(k))) for k in ("updated_at", "progress_at", "created_at")})
    return out


def clean_sessions(sessions):
    return [{"code": s["code"], "aliases": [a for a in s.get("aliases") or [] if isinstance(a, str)]
             if isinstance(s.get("aliases"), list) else [], "state": _s(s.get("state")) or "offline",
             "updated_at": int(_num(s.get("updated_at")))}
            for s in _dicts(sessions) if isinstance(s.get("code"), str)]


def clean_res(res):
    if not isinstance(res, dict):
        return None
    mem = res.get("mem") if isinstance(res.get("mem"), dict) else {}
    return {
        "enabled": res.get("enabled") is not False, "at": _num(res.get("at"), None),
        "level": res.get("level") if _s(res.get("level")) in LEVELS else None,
        "total_mb": _num(mem.get("total_mb")), "heavy_running": int(_num(res.get("heavy_running"))),
        "sessions": [{"code": r["code"], "heavy_n": int(_num(r.get("heavy_n"))), "children": [
            {"class": _s(c.get("class")), "comm": _s(c.get("comm")), "via": _s(c.get("via")),
             "pid": int(_num(c.get("pid"))), "rss_mb": int(_num(c.get("rss_mb"))), "heavy": c.get("heavy") is True}
            for c in _dicts(r.get("children"))]}
            for r in _dicts(res.get("sessions")) if isinstance(r.get("code"), str)],
    }


def touched(t):
    return max(int(_num(t.get("updated_at"))), int(_num(t.get("progress_at"))), int(_num(t.get("created_at"))))


def dep_met(dep, by_id):
    """A dependency is met when the task it names is done, or reached what `kind` asks for."""
    d = by_id.get(dep.get("task"))
    if d is None:
        return False
    if taskstore.status_of(d) == "done":
        return True
    kind = dep.get("kind") or "done"
    pr = d.get("pr") if isinstance(d.get("pr"), dict) else {}
    if kind == "merge":
        return pr.get("state") == "merged"
    if kind == "deploy":
        return d.get("stage") == "maintain" or any(
            isinstance(g, dict) and g.get("gate") == "production" and g.get("status") == "approved"
            for g in d.get("gates") or [])
    return False


def _resources(res, now):
    """(level or None, state of resources.json): resource gates apply only to a fresh sample."""
    if res is None:
        return None, "missing"
    if not res["enabled"]:
        return None, "disabled"
    if res["at"] is None or not -RES_SKEW_MS <= now - res["at"] <= RES_STALE_MS:
        return None, "stale"
    return res["level"], "ok"


def _adv(kind, to, task, ref, severity, text, fallback, **extra):
    if BANNED_RE.search(text):              # a name from a task or process tripped the filter
        text = fallback
    key = "|".join([kind, to or "", task or "", str(ref or "")])
    a = {"id": "a-" + hashlib.sha1(key.encode()).hexdigest()[:12], "to": to, "kind": kind,
         "task": task, "severity": severity, "text": text}
    a.update(extra)
    return a


def _safe(name, fallback):
    return name if isinstance(name, str) and SAFE_NAME_RE.fullmatch(name) and not BANNED_RE.search(name) else fallback


def _ref(v, fallback):
    return v if isinstance(v, str) and taskstore.REF_RE.fullmatch(v) and ".." not in v else fallback


def decide(tasks, sessions, res, st, now):
    """The scheduling decision. Pure: reads only its arguments, which may hold anything."""
    tasks = [t for t in (clean_task(x) for x in (tasks if isinstance(tasks, list) else [])) if t]
    sessions, res = clean_sessions(sessions), clean_res(res)
    level, res_state = _resources(res, now)
    lvl = LEVELS.index(level) if level else 0
    code_of, sstate, since = {}, {}, {}
    for s in sessions:
        for a in [s["code"]] + s["aliases"]:
            code_of.setdefault(a, s["code"])
        sstate[s["code"]] = s["state"]
        since[s["code"]] = s["updated_at"]
    rsess = {r["code"]: r for r in res["sessions"]} if res_state == "ok" else {}
    mem_mb = res["total_mb"] if res else 0
    max_heavy = st["max_heavy"] if st["max_heavy"] != "auto" else (1 if 0 < mem_mb < 16384 else 2)

    by_id = {t["id"]: t for t in tasks}
    live = {i: t for i, t in by_id.items() if taskstore.status_of(t) not in ("done", "dropped")}
    unblocks = {}
    for t in live.values():
        for d in t.get("depends_on") or []:
            if isinstance(d, dict) and d.get("task") in live:
                unblocks[d["task"]] = unblocks.get(d["task"], 0) + 1

    rows = {}
    for i, t in sorted(live.items()):
        heavy = t.get("heavy") is True
        age_h = max(0, (now - touched(t)) // HOUR_MS)
        score = (60 * STAGE_W.get(t.get("stage"), .15) * SIZE_W.get(t.get("size"), .8)
                 + 15 * min(unblocks.get(i, 0), 3) + 10 * _int(t.get("priority"), 1, 0, 3)
                 + 2 * min(age_h, 5) - (15 if heavy and lvl >= 1 else 0))
        blocked = []
        if taskstore.status_of(t) == "blocked":
            blocked.append({"kind": "manual", "ref": None})
        if t.get("stage") == "production":
            blocked.append({"kind": "redline", "ref": "production"})
        for g in t.get("gates") or []:
            if isinstance(g, dict) and g.get("status") == "pending":
                blocked.append({"kind": "human", "ref": g.get("decision_id")})
        for d in t.get("depends_on") or []:
            if isinstance(d, dict) and not dep_met(d, by_id):
                blocked.append({"kind": "task", "ref": d.get("task")})
        state = "paused" if taskstore.status_of(t) == "paused" else ("blocked" if blocked else None)
        why = ["unblocks %d" % unblocks[i]] if unblocks.get(i) else []
        rows[i] = {"task": i, "owner": t.get("owner"), "session": code_of.get(t.get("owner")), "state": state,
                   "score": int(round(score)), "heavy": heavy, "blocked_by": blocked, "why": why,
                   "_touched": touched(t)}

    def rank_key(r):
        return (-r["score"], r["_touched"], r["task"])

    # One session works on one task: its current task is the active one it touched last.
    by_code = {}
    for r in rows.values():
        by_code.setdefault(r["session"], []).append(r)
    stuck, picks, free_owners = [], {}, []
    for code, mine in sorted(by_code.items(), key=lambda kv: kv[0] or ""):
        open_ = sorted((r for r in mine if r["state"] is None), key=rank_key)
        if code is None or sstate.get(code) not in BUSY + FREE:
            for r in open_:
                r["state"], r["blocked_by"] = "deferred", [{"kind": "session", "ref": code or r["owner"]}]
            continue
        cur = max((r for r in mine if r["state"] != "paused"), key=lambda r: (r["_touched"], r["task"]), default=None)
        if sstate[code] in BUSY and cur is not None and cur["state"] is None:
            cur["state"] = "run"
            idle = now - cur["_touched"]
            sev = "high" if idle >= st["stall_ms"][1] else ("warn" if idle >= st["stall_ms"][0] else None)
            if sev and sstate[code] == "working":
                cur["stuck"] = sev
                stuck.append({"task": cur["task"], "session": code, "severity": sev, "since": cur["_touched"]})
            for r in open_:
                if r is not cur:
                    r["state"] = "queued"
                    r["why"].append("after " + cur["task"])
            continue
        picks[code] = open_
        free_owners.append(code)

    # Heavy slots: none at high or worse; else max_heavy less what already runs. Fairness: a
    # session that holds fewer heavy processes gets a slot first, then score, then the longest wait.
    running_heavy = sum(1 for r in rows.values() if r["state"] == "run" and r["heavy"])
    used = max(running_heavy, res["heavy_running"] if res_state == "ok" else 0)
    free_slots = 0 if lvl >= 2 else max(0, max_heavy - used)
    held = {c: (rsess.get(c) or {}).get("heavy_n", 0) for c in free_owners}
    asks = sorted((picks[c][0] for c in free_owners if picks[c] and picks[c][0]["heavy"]),
                  key=lambda r: (held[r["session"]],) + rank_key(r))
    granted = {r["task"] for r in asks[:free_slots]}
    advisories = []
    for code in free_owners:
        for r in picks[code]:
            if r["heavy"] and r["task"] not in granted:
                r["state"] = "deferred"
                reason = ("resources are at %s" % level) if lvl >= 2 else (
                    "%d heavy job(s) already run (max %d)" % (used, max_heavy))
                r["blocked_by"] = [{"kind": "resource", "ref": level if lvl >= 2 else "max_heavy"}]
                if r is not picks[code][0]:      # only the task the session would start next is advised
                    continue
                advisories.append(_adv(
                    "defer", code, r["task"], r["blocked_by"][0]["ref"], "warn",
                    "Task %s is heavy and %s: hold its test runs, builds and browsers; take lighter work first." % (r["task"], reason),
                    "A heavy task of yours has to wait: hold its heavy runs for now; take lighter work first."))
        ready = [r for r in picks[code] if r["state"] is None]
        for n, r in enumerate(ready):
            r["state"] = "ready" if n == 0 else "queued"
            if n:
                r["why"].append("after " + ready[0]["task"])
        if ready and sstate[code] in FREE and now - since.get(code, now) >= st["idle_ms"]:
            advisories.append(_adv("resume", code, ready[0]["task"], None, "info",
                                   "Task %s is ready and nothing blocks it." % ready[0]["task"],
                                   "A task of yours is ready."))

    # One open PR per repo: the best-ranked task with an open PR goes first; the others hold theirs.
    if st["one_pr_per_repo"]:
        repos = {}
        for i, t in live.items():
            if isinstance(t.get("repo"), str):
                repos.setdefault(t["repo"], []).append(rows[i])
        for repo, rs in sorted(repos.items()):
            open_pr = sorted((r for r in rs if (live[r["task"]].get("pr") or {}).get("state") == "open"), key=rank_key)
            if not open_pr:
                continue
            first = open_pr[0]
            for r in rs:
                if r is not first and r["state"] in ("run", "ready") and r["session"]:
                    r["why"].append("finish-first " + first["task"])
                    advisories.append(_adv(
                        "finish-first", r["session"], r["task"], first["task"], "warn",
                        "Task %s has the open PR in this repo: keep committing %s locally; don't push it or open a PR until that one merges." % (first["task"], r["task"]),
                        "Another task has the open PR in this repo: keep committing locally; don't push or open a PR yet."))

    # After a squash merge, a branch stacked on the merged one needs `rebase --onto`.
    for i, x in sorted(by_id.items()):
        pr = x.get("pr") if isinstance(x.get("pr"), dict) else {}
        if pr.get("state") != "merged" or pr.get("method") != "squash" or not x.get("branch"):
            continue
        for j, y in sorted(live.items()):
            r = rows[j]
            if j == i or y.get("base") != x["branch"] or not r["session"] or (y.get("repo") and x.get("repo") and y["repo"] != x["repo"]):
                continue
            onto = _ref(x.get("base"), "<base>")
            advisories.append(_adv(
                "rebase-needed", r["session"], j, i, "warn",
                "Task %s was squash-merged. Rebase your branch: git rebase --onto origin/%s %s %s; force-push only your own "
                "feature branch with --force-with-lease, and point the PR base at %s." % (
                    i, onto, pr.get("head_sha") if taskstore.SHA_RE.fullmatch(str(pr.get("head_sha"))) else "<its old tip>",
                    _ref(y.get("branch"), "<your branch>"), onto),
                "A task your branch is stacked on was squash-merged: rebase with git rebase --onto, force-push only "
                "your own branch with --force-with-lease, and point the PR base at the merged branch's base."))

    # Critical: ask the sessions holding the largest heavy processes to stop their own.
    if lvl == 3 and st["stop_own_heavy"]:
        biggest = []
        def size(c):                     # bucketed, so a growing process does not change the decision
            return c["rss_mb"] // RSS_BUCKET_MB * RSS_BUCKET_MB
        for code, r in sorted(rsess.items()):
            hv = [c for c in r["children"] if c["heavy"]]
            if hv:
                biggest.append((code, max(hv, key=lambda c: (size(c), -c["pid"]))))
        biggest.sort(key=lambda b: (-size(b[1]), b[0]))
        for code, c in biggest[:st["stop_max"]]:
            cls = _safe(c["class"], "heavy")
            via = _safe(c["via"], "")
            who = "you or your teammate %s" % via if via else "you"
            mb = ("about %d MB" % size(c)) if size(c) else ("under %d MB" % RSS_BUCKET_MB)
            advisories.append(_adv(
                "stop-own-heavy", code, None, c["pid"], "high",
                "Memory is critical: if %s started the %s process %s (pid %d, %s), stop it now and re-run it later. "
                "Never stop a process you did not start." % (who, cls, _safe(c["comm"], cls), c["pid"], mb),
                "Memory is critical: stop the largest heavy process you started yourself, and re-run it later.",
                pid=c["pid"], comm=_safe(c["comm"], cls), rss_mb=size(c), **{"class": cls}))

    queue = sorted(rows.values(), key=lambda r: (STATE_ORDER.index(r["state"]),) + rank_key(r))
    for n, r in enumerate(queue, 1):
        r["rank"] = n
        r.pop("_touched")
    advisories.sort(key=lambda a: (SEVERITY_ORDER.index(a["severity"]), a["to"] or "", a["kind"], a["id"]))
    body = {"schema_version": SCHEMA_VERSION, "enabled": True, "mode": "degraded" if lvl == 3 else "normal",
            "level": level, "resources": res_state, "max_heavy": max_heavy, "heavy_used": used,
            "start": [r["task"] for r in queue if r["state"] == "ready"], "queue": queue,
            "stuck": sorted(stuck, key=lambda s: s["task"]), "advisories": advisories}
    body["digest"] = digest(body)
    return body


def off_doc(error=None):
    """schedule.enabled: false (mode off), or a decision that failed (mode error): no advice at all,
    rather than old advice that no longer holds."""
    body = {"schema_version": SCHEMA_VERSION, "enabled": error is not None, "mode": "error" if error else "off",
            "level": None, "resources": None, "max_heavy": None, "heavy_used": None, "start": [], "queue": [],
            "stuck": [], "advisories": []}
    if error:
        body["error"] = error
    body["digest"] = digest(body)
    return body


def digest(body):
    b = {k: v for k, v in body.items() if k not in ("digest", "generated_at")}
    return hashlib.sha1(json.dumps(b, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_doc(home, doc):
    path = os.path.join(home, FILE)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def step(cfg, home, sessions, tasks, now):
    """The collector's step: decide, and write scheduler.json when the decision changed.
    Returns (doc, written)."""
    st = settings(cfg)
    try:
        doc = decide(tasks, sessions, read_json(os.path.join(home, "resources.json")), st, now) if st["enabled"] else off_doc()
    except Exception as exc:  # noqa: BLE001 - a bug must not leave stale advice in place
        doc = off_doc(type(exc).__name__)
    old = read_json(os.path.join(home, FILE))
    if isinstance(old, dict) and old.get("digest") == doc["digest"] and digest(old) == doc["digest"]:
        return old, False
    write_doc(home, dict(doc, generated_at=now))
    return doc, True


def show(argv, home, out=sys.stdout):
    doc = read_json(os.path.join(home, FILE))
    if "--json" in argv:
        out.write(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        return 0
    if not isinstance(doc, dict):
        out.write("no schedule yet (the collector writes %s)\n" % FILE)
        return 0
    if not doc.get("enabled"):
        out.write("scheduler off (fleet.yaml schedule.enabled: false)\n")
        return 0
    out.write("mode %s · resources %s%s · heavy %s/%s · start: %s\n" % (
        doc.get("mode"), doc.get("resources"), (" (%s)" % doc["level"]) if doc.get("level") else "",
        doc.get("heavy_used"), doc.get("max_heavy"), ", ".join(doc.get("start") or []) or "-"))
    for r in doc.get("queue") or []:
        out.write("%3s %-8s %-28s %-10s %4s %s%s\n" % (
            r.get("rank"), r.get("state"), r.get("task"), r.get("session") or r.get("owner") or "-", r.get("score"),
            ",".join("%s:%s" % (b.get("kind"), b.get("ref")) for b in r.get("blocked_by") or []) or "",
            (" stuck:" + r["stuck"]) if r.get("stuck") else ""))
    for a in doc.get("advisories") or []:
        out.write("  [%s] %s → %s: %s\n" % (a.get("severity"), a.get("kind"), a.get("to"), a.get("text")))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help") or argv[0] != "show":
        print(__doc__.strip())
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    import fleet
    return show(argv[1:], fleet.board_home())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
