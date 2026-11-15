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
import lessons as lessonstore  # noqa: E402
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
    "advise": True,                  # deliver advisories to sessions; false = shadow mode (board only)
    "readvise_minutes": 60,          # re-send an advisory nobody answered after this long
    "max_messages_per_session_hour": 4,
    "human_wait_minutes": 10,        # J1: every session with work waits on the owner this long
    "conserve": {"five_hour_pct": 85, "margin_minutes": 20, "weekly_pace_slack_pct": 15},
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
WEEK_MS = 7 * 24 * HOUR_MS
USAGE_STALE_MS = 15 * 60 * 1000      # usage.json older than this: no usage mode (a limit episode still counts)
WAIT_RESET_PCT = 95
# Advisory text never reads like an approval, a deploy order or a board decision.
BANNED_RE = re.compile(r"approv|deploy|\[BOARD|decision", re.I)
SAFE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")          # a session code that may receive advice
OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+-]{0,79}")      # session codes and aliases as stored
DEC_RE = re.compile(r"d-[0-9]+-[a-z0-9]+")
CTL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
LIMIT_GRACE_MS = 10 * 60 * 1000      # a limit episode past resets_at by this much no longer holds starts
DEFAULT_BRANCHES = ("main", "master", "trunk", "develop", "detached", "HEAD")


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
    cons = c.get("conserve") if isinstance(c.get("conserve"), dict) else {}
    return {
        "enabled": c.get("enabled", True) is not False,
        "stop_own_heavy": c.get("stop_own_heavy", True) is not False,
        "stop_max": _int(c.get("stop_max"), DEFAULTS["stop_max"], 1, 20),
        "max_heavy": "auto" if mh == "auto" else _int(mh, 2, 0, 64),
        "one_pr_per_repo": c.get("one_pr_per_repo", True) is not False,
        "stall_ms": [warn_m * 60000, max(warn_m, _int(stall[1], 90, 1, 10080)) * 60000],
        "idle_ms": _int(c.get("idle_with_work_minutes"), 30, 1, 10080) * 60000,
        "advise": c.get("advise", True) is not False,
        "readvise_ms": _int(c.get("readvise_minutes"), 60, 1, 10080) * 60000,
        "max_messages": _int(c.get("max_messages_per_session_hour"), 4, 1, 60),
        "human_wait_ms": _int(c.get("human_wait_minutes"), 10, 0, 10080) * 60000,
        "conserve": {"five_hour_pct": _int(cons.get("five_hour_pct"), 85, 1, 100),
                     "margin_ms": _int(cons.get("margin_minutes"), 20, 0, 600) * 60000,
                     "weekly_slack": _int(cons.get("weekly_pace_slack_pct"), 15, 0, 100)},
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
    out = {k: _s(t.get(k)) for k in ("stage", "size")}
    out.update({k: _ref(t.get(k), None) for k in ("repo", "branch", "base")})
    out["owner"] = t["owner"] if isinstance(t.get("owner"), str) and OWNER_RE.fullmatch(t["owner"]) else None
    out.update({
        "id": t["id"], "status": taskstore.status_of(t) if isinstance(t.get("status"), (str, type(None))) else "active",
        "heavy": t.get("heavy") is True, "priority": _int(t.get("priority"), 1, 0, 3),
        "gates": [{"gate": _s(g.get("gate")), "status": _s(g.get("status")),
                   "decision_id": g["decision_id"] if isinstance(g.get("decision_id"), str)
                   and DEC_RE.fullmatch(g["decision_id"]) else None} for g in _dicts(t.get("gates"))],
        "depends_on": [{"task": d["task"], "kind": _s(d.get("kind")) or "done"}
                       for d in _dicts(t.get("depends_on")) if isinstance(d.get("task"), str)],
        "pr": {k: _s(pr.get(k)) for k in ("state", "method", "head_sha")},
    })
    out.update({k: int(_num(t.get(k))) for k in ("updated_at", "progress_at", "created_at")})
    return out


def clean_sessions(sessions):
    return [{"code": s["code"], "aliases": [a for a in s.get("aliases") or [] if isinstance(a, str) and OWNER_RE.fullmatch(a)]
             if isinstance(s.get("aliases"), list) else [], "state": _s(s.get("state")) or "offline",
             "updated_at": int(_num(s.get("updated_at"))), "repo": _ref(s.get("repo"), None),
             "branch": _ref(s.get("branch"), None)}
            for s in _dicts(sessions) if isinstance(s.get("code"), str) and OWNER_RE.fullmatch(s["code"])]


def clean_res(res):
    if not isinstance(res, dict):
        return None
    mem = res.get("mem") if isinstance(res.get("mem"), dict) else {}
    swap = res.get("swap") if isinstance(res.get("swap"), dict) else {}
    cpu = res.get("cpu") if isinstance(res.get("cpu"), dict) else {}
    disks = [_num(d.get("free_pct"), None) for d in _dicts(res.get("disks"))]
    return {
        "metrics": {"mem_available_mb": _num(mem.get("available_mb"), None),
                    "mem_available_pct": _num(mem.get("available_pct"), None),
                    "psi_mem": _num(mem.get("psi_some_avg60"), None), "swap_used_pct": _num(swap.get("used_pct"), None),
                    "load_per_cpu": _num(cpu.get("load_per_cpu"), None),
                    "disk_free_pct": min((d for d in disks if d is not None), default=None),
                    "heavy_running": _num(res.get("heavy_running"), None)},
        "enabled": res.get("enabled") is not False, "at": _num(res.get("at"), None),
        "level": res.get("level") if _s(res.get("level")) in LEVELS else None,
        "total_mb": _num(mem.get("total_mb")), "heavy_running": int(_num(res.get("heavy_running"))),
        "oom": res.get("oom_recent") is True or _num(res.get("oom_kills_delta")) > 0,
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
        # Owner decision: deployed means the task reached `maintain` (or is done, above). An approved
        # production gate only allows the deploy; it never satisfies a deploy dependency.
        return d.get("stage") == "maintain"
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


def usage_mode(ctx, st, now):
    """("wait-reset"|"conserve"|None, resets_at, why) from limit.json, usage.json and its burn rate."""
    ctx = ctx if isinstance(ctx, dict) else {}
    lim = ctx.get("limit") if isinstance(ctx.get("limit"), dict) else None
    u = ctx.get("usage") if isinstance(ctx.get("usage"), dict) else {}
    burn = ctx.get("burn") if isinstance(ctx.get("burn"), dict) else {}
    win = {k: (u.get(k) if isinstance(u.get(k), dict) else {}) for k in ("five_hour", "seven_day")}
    fresh = -RES_SKEW_MS <= now - _num(u.get("updated_at"), -10 ** 15) <= USAGE_STALE_MS
    p5, r5 = _num(win["five_hour"].get("pct"), None), _num(win["five_hour"].get("resets_at"), None)
    pw, rw = _num(win["seven_day"].get("pct"), None), _num(win["seven_day"].get("resets_at"), None)
    lim_resets = _num((lim or {}).get("resets_at"), None)
    if lim is not None and (lim_resets is None or now <= lim_resets + LIMIT_GRACE_MS):
        return "wait-reset", lim_resets, "limit"
    if not fresh:
        return None, None, None
    c = st["conserve"]
    if p5 is not None and p5 >= WAIT_RESET_PCT:
        return "wait-reset", r5, "5h"
    if p5 is not None and p5 >= c["five_hour_pct"]:
        return "conserve", r5, "5h"
    hit = _num(burn.get("hit_at"), None)
    if burn.get("status") == "danger" and hit is not None and r5 is not None and hit < r5 - c["margin_ms"]:
        return "conserve", r5, "burn"
    if pw is not None and rw is not None:
        elapsed = min(1.0, max(0.0, 1 - (rw - now) / WEEK_MS))
        if pw > elapsed * 100 + c["weekly_slack"]:
            return "conserve", rw, "weekly"
    return None, None, None


def _when(ms):
    import datetime as dt
    try:
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%H:%M UTC") if ms else "the reset"
    except (OverflowError, OSError, ValueError, TypeError):
        return "the reset"


def _who(code):
    """A session name fit for a jam text, else "a session"."""
    return _safe(code, "a session")


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


def decide(tasks, sessions, res, st, now, ctx=None):
    """The scheduling decision. Pure: reads only its arguments, which may hold anything. `ctx`:
    {usage: usage.json, burn: usage-samples.json burn, limit: the active limit episode or None,
    away: owner away, operator_stale: the operator:stale alert is open}."""
    tasks = [t for t in (clean_task(x) for x in (tasks if isinstance(tasks, list) else [])) if t]
    sessions, res = clean_sessions(sessions), clean_res(res)
    ctx = ctx if isinstance(ctx, dict) else {}
    level, res_state = _resources(res, now)
    lvl = LEVELS.index(level) if level else 0
    umode, resets_at, uwhy = usage_mode(ctx, st, now)
    conserve = umode == "conserve"
    code_of, sstate, since = {}, {}, {}
    for s in sessions:
        for a in [s["code"]] + s["aliases"]:
            code_of.setdefault(a, s["code"])
        sstate[s["code"]] = s["state"]
        since[s["code"]] = s["updated_at"]
    rsess = {r["code"]: r for r in res["sessions"]} if res_state == "ok" else {}
    mem_mb = res["total_mb"] if res else 0
    max_heavy = st["max_heavy"] if st["max_heavy"] != "auto" else (1 if 0 < mem_mb < 16384 else 2)
    # Active lessons (lib/lessons.py): declarative rules that can only tighten the schedule.
    metrics = dict(res["metrics"]) if res_state == "ok" else {}
    u = ctx.get("usage") if isinstance(ctx.get("usage"), dict) else {}
    if -RES_SKEW_MS <= now - _num(u.get("updated_at"), -10 ** 15) <= USAGE_STALE_MS:
        five = u.get("five_hour") if isinstance(u.get("five_hour"), dict) else {}
        week = u.get("seven_day") if isinstance(u.get("seven_day"), dict) else {}
        r5 = _num(five.get("resets_at"), None)
        metrics.update(five_hour_pct=_num(five.get("pct"), None), weekly_pct=_num(week.get("pct"), None),
                       five_hour_min_to_reset=(r5 - now) // 60000 if r5 is not None else None)
    rules = []
    for x in ctx.get("lessons") or []:
        try:
            rules.append({"id": x["id"], "rule": lessonstore.validate_rule(x["rule"])})
        except (lessonstore.Bad, KeyError, TypeError, AttributeError):
            continue                          # a broken line never loosens anything: it is skipped
    applied = set()

    def acting(action, extra=None):
        """The lessons with this action whose rule holds (for a task when `extra` has its metrics)."""
        out = []
        for L in rules:
            a = L["rule"]["then"]["action"]
            if (a == action or a.startswith(action + ":")) and (extra is not None or not lessonstore.task_only(L["rule"])) \
                    and lessonstore.holds(L["rule"], dict(metrics, **(extra or {}))):
                applied.add(L["id"])
                out.append(L)
        return out
    for L in acting("max_heavy"):
        max_heavy = min(max_heavy, int(L["rule"]["then"]["action"].split(":")[1]))

    by_id = {t["id"]: t for t in tasks}
    live = {i: t for i, t in by_id.items() if taskstore.status_of(t) not in ("done", "dropped")}
    unblocks = {}
    for t in live.values():
        for d in t.get("depends_on") or []:
            if isinstance(d, dict) and d.get("task") in live:
                unblocks[d["task"]] = unblocks.get(d["task"], 0) + 1

    open_prs = {}
    for t in live.values():
        if t["repo"] and t["pr"].get("state") == "open":
            open_prs[t["repo"]] = open_prs.get(t["repo"], 0) + 1

    def tmetrics(i):
        t = live[i]
        return {"stage": t.get("stage"), "open_prs_in_repo": open_prs.get(t["repo"], 0) if t["repo"] else None}
    rows = {}
    for i, t in sorted(live.items()):
        heavy = t.get("heavy") is True
        age_h = max(0, (now - touched(t)) // HOUR_MS)
        # conserve: nearly finished work wins (p_finish weight 90, plan/design -20)
        score = ((90 if conserve else 60) * STAGE_W.get(t.get("stage"), .15) * SIZE_W.get(t.get("size"), .8)
                 + 15 * min(unblocks.get(i, 0), 3) + 10 * _int(t.get("priority"), 1, 0, 3)
                 + 2 * min(age_h, 5) - (15 if heavy and lvl >= 1 else 0)
                 - (20 if conserve and t.get("stage") in ("plan", "design") else 0))
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
    stuck, picks, free_owners, cur_of = [], {}, [], {}
    for code, mine in sorted(by_code.items(), key=lambda kv: kv[0] or ""):
        open_ = sorted((r for r in mine if r["state"] is None), key=rank_key)
        if code is None or sstate.get(code) not in BUSY + FREE:
            for r in open_:
                r["state"], r["blocked_by"] = "deferred", [{"kind": "session", "ref": code or r["owner"]}]
            continue
        cur = max((r for r in mine if r["state"] != "paused"), key=lambda r: (r["_touched"], r["task"]), default=None)
        cur_of[code] = cur
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
    for r in asks:                                   # a lesson may hold a heavy start back
        held_by = acting("defer_heavy", tmetrics(r["task"]))
        if held_by and r["task"] in granted:
            granted.discard(r["task"])
            r["lesson"] = held_by[0]["id"]
    advisories = []
    for code in free_owners:
        for r in picks[code]:
            if r["heavy"] and r["task"] not in granted:
                r["state"] = "deferred"
                reason = ("a lesson %s holds heavy starts back now" % r["lesson"]) if r.get("lesson") else (
                    ("resources are at %s" % level) if lvl >= 2 else
                    ("%d heavy job(s) already run (max %d)" % (used, max_heavy)))
                r["blocked_by"] = [{"kind": "lesson", "ref": r.pop("lesson")} if r.get("lesson") else
                                   {"kind": "resource", "ref": level if lvl >= 2 else "max_heavy"}]
                if r is not picks[code][0]:      # only the task the session would start next is advised
                    continue
                advisories.append(_adv(
                    "defer", code, r["task"], r["blocked_by"][0]["ref"], "warn",
                    "Task %s is heavy and %s: hold its test runs, builds and browsers; take lighter work first." % (r["task"], reason),
                    "A heavy task of yours has to wait: hold its heavy runs for now; take lighter work first."))
        ready = [r for r in picks[code] if r["state"] is None]
        for r in list(ready):                        # lessons: pause new starts, one repo at a time
            busy_repo = live[r["task"]]["repo"] and any(
                x["state"] == "run" and live[x["task"]]["repo"] == live[r["task"]]["repo"] for x in rows.values())
            hold = acting("pause_new_starts", tmetrics(r["task"])) or (
                acting("serialize_repo", tmetrics(r["task"])) if busy_repo else [])
            if hold:
                r["state"], r["blocked_by"] = "deferred", [{"kind": "lesson", "ref": hold[0]["id"]}]
                ready.remove(r)
        if umode == "wait-reset" and ready:        # no new starts until the usage window resets
            for r in ready:
                r["state"], r["blocked_by"] = "deferred", [{"kind": "reset", "ref": resets_at}]
            advisories.append(_adv(
                "wait-for-reset", code, ready[0]["task"], resets_at, "warn",
                "The usage window is nearly used up: don't start task %s before %s; finish or pause what runs."
                % (ready[0]["task"], _when(resets_at)),
                "The usage window is nearly used up: don't start new work before the reset."))
            continue
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

    if acting("raise_severity"):
        for a in advisories:
            if a["severity"] == "warn":                 # ranks higher; quiet hours still judge the original
                a["severity"], a["raised_from"] = "high", "warn"
    hints = [{"lesson": L["id"], "text": L["rule"]["then"]["action"][len("advise:"):]} for L in acting("advise")]
    conds, answer_first = conditions(rows, live, sessions, sstate, cur_of, stuck, picks, free_owners, since,
                                     st, now, lvl, res, umode, resets_at, ctx)
    queue = sorted(rows.values(), key=lambda r: (STATE_ORDER.index(r["state"]),) + rank_key(r))
    for n, r in enumerate(queue, 1):
        r["rank"] = n
        r.pop("_touched")
    advisories.sort(key=lambda a: (SEVERITY_ORDER.index(a["severity"]), a["to"] or "", a["kind"], a["id"]))
    mode = umode or ("degraded" if lvl == 3 else "normal")
    body = {"schema_version": SCHEMA_VERSION, "enabled": True, "mode": mode,
            "usage": {"mode": umode, "why": uwhy, "resets_at": resets_at} if umode else None,
            "level": level, "resources": res_state, "max_heavy": max_heavy, "heavy_used": used,
            "start": [r["task"] for r in queue if r["state"] == "ready"], "queue": queue,
            "stuck": sorted(stuck, key=lambda s: s["task"]), "advisories": advisories,
            "answer_first": answer_first, "conditions": conds,
            "lessons": {"applied": sorted(applied), "hints": hints}}
    body["digest"] = digest(body)
    return body


def _cond(rule, scope, severity, sessions, text, alert=True, hold_ms=0, evals=2, **extra):
    """A jam condition (docs/design J1-J8). Episodes (open after `evals` evaluations and `hold_ms`,
    closed after 2 clear ones) are kept by lib/advisories.py. alert: raise jam:<rule>:<scope>."""
    if BANNED_RE.search(text):
        text = "Jam %s: see the board." % rule
    scope = scope if CODE_RE.fullmatch(str(scope)) else _hash(scope)    # ids and alert keys stay plain
    c = {"id": "j-%s-%s" % (rule, scope), "rule": rule, "severity": severity,
         "sessions": sorted(x for x in sessions if isinstance(x, str) and CODE_RE.fullmatch(x)),
         "text": text, "alert": severity if alert else None, "hold_ms": hold_ms, "evals": evals}
    c.update(extra)
    return c


def _hash(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


def conditions(rows, live, sessions, sstate, cur_of, stuck, picks, free_owners, since, st, now, lvl, res,
               umode, resets_at, ctx):
    """The jam conditions true right now (J1-J8 but J8, which needs delivery state) and the
    decisions to answer first. Pure."""
    out = []
    # answer_first: pending gate decisions, ranked by the sessions and tasks they hold up
    hold = {}
    for r in rows.values():
        for b in r["blocked_by"]:
            if b["kind"] == "human" and b["ref"]:
                h = hold.setdefault(b["ref"], {"sessions": set(), "tasks": set()})
                h["tasks"].add(r["task"])
                if r["session"] and cur_of.get(r["session"]) is r:
                    h["sessions"].add(r["session"])
    for r in rows.values():                      # one level through dependencies
        for b in r["blocked_by"]:
            if b["kind"] == "task" and b["ref"] in rows:
                for h in hold.values():
                    if b["ref"] in h["tasks"]:
                        h["tasks"].add(r["task"])
    answer_first = sorted(({"decision": d, "unblocks_sessions": len(h["sessions"]), "unblocks_tasks": sorted(h["tasks"])}
                           for d, h in hold.items()), key=lambda a: (-a["unblocks_sessions"], -len(a["unblocks_tasks"]), a["decision"]))

    def waits_on_human(r, seen=()):
        for b in r["blocked_by"] if r else []:
            if b["kind"] == "human" or (b["kind"] == "task" and b["ref"] in rows and b["ref"] not in seen
                                         and waits_on_human(rows[b["ref"]], seen + (b["ref"],))):
                return True
        return False
    # J1: every live session with work waits on the owner
    working = [c for c, cur in cur_of.items() if cur is not None and sstate.get(c) != "offline"]
    has_ready = {c for c in free_owners if any(r["state"] == "ready" for r in picks.get(c) or [])}
    if working and all(waits_on_human(cur_of[c]) and c not in has_ready for c in working):
        first = answer_first[0]["decision"] if answer_first else None
        out.append(_cond("J1", "fleet", "info" if ctx.get("away") else "warn", working,
                         "Every session with work is waiting on you%s." % (": answer %s first" % first if first else ""),
                         hold_ms=st["human_wait_ms"], decision=first))
    # J2: a dependency cycle
    graph = {i: [d["task"] for d in t["depends_on"] if d["task"] in live] for i, t in live.items()}
    cycles, color = set(), {}

    def dfs(n, path):
        color[n] = 1
        for m in graph.get(n, []):
            if color.get(m) == 1:
                cycles.add(tuple(sorted(path[path.index(m):])))
            elif not color.get(m):
                dfs(m, path + [m])
        color[n] = 2
    for n in sorted(graph):
        if not color.get(n):
            dfs(n, [n])
    for cyc in sorted(cycles):
        owners = {rows[i]["session"] for i in cyc if rows[i]["session"]}
        out.append(_cond("J2", _hash(*cyc), "high", owners,
                         "Dependency cycle: %s. Ask the owner which dependency to drop." % " → ".join(cyc + cyc[:1]),
                         tasks=list(cyc)))
    # J3a: two live sessions on one repo + branch (same worktree or branch)
    spots = {}
    for s in sessions:
        # The collector's repo/branch come from each session's start cwd, not its worktree, and
        # sessions often sit on the default branch: board-only until git facts (worktree) land.
        if s["state"] != "offline" and s["repo"] and s["branch"] and s["branch"] not in DEFAULT_BRANCHES \
                and CODE_RE.fullmatch(s["code"]):
            spots.setdefault((s["repo"], s["branch"]), []).append(s["code"])
    for (repo, br), codes in sorted(spots.items()):
        if len(codes) >= 2:
            out.append(_cond("J3a", _hash(repo, br), "high", codes,
                             "%s work on the same branch %s of %s: one of them should move to its own worktree and branch."
                             % (" and ".join(sorted(codes)), br, repo), alert=False, evals=1))
    # J3b: two open PRs in one repo
    if st["one_pr_per_repo"]:
        prs = {}
        for i, t in live.items():
            if t["repo"] and t["pr"].get("state") == "open":
                prs.setdefault(t["repo"], []).append(i)
        for repo, ids in sorted(prs.items()):
            if len(ids) >= 2:
                out.append(_cond("J3b", _hash(repo), "warn", {rows[i]["session"] for i in ids if rows[i]["session"]},
                                 "%d open PRs in %s (%s): finish one first." % (len(ids), repo, ", ".join(sorted(ids))),
                                 tasks=sorted(ids)))      # its rule is global: no repo in the fingerprint
    # J4: stalled work, or an idle session with a ready task
    for s in stuck:
        out.append(_cond("J4", s["session"], s["severity"], [s["session"]],
                         "%s has had no progress on %s for a while." % (_who(s["session"]), s["task"]), task=s["task"],
                         signals={"kind": "stuck"}))
    for code in free_owners:
        ready = [r for r in picks.get(code) or [] if r["state"] == "ready"]
        if ready and sstate.get(code) in FREE and now - since.get(code, now) >= st["idle_ms"]:
            out.append(_cond("J4", code + "-idle", "info", [code], "%s is idle with %s ready." % (_who(code), ready[0]["task"]),
                             alert=False, task=ready[0]["task"], signals={"kind": "idle"}))
    # J5: resources critical or an OOM kill (alerted as res:*), J6: operator silent (operator:stale)
    if lvl == 3 or (res and res["oom"]):
        out.append(_cond("J5", "fleet", "high", [r["session"] for r in rows.values() if r["state"] == "run" and r["session"]],
                         "Resources are critical%s." % (" after an OOM kill" if res and res["oom"] else ""),
                         alert=False, evals=1 if res and res["oom"] else 2,   # critical: 2 samples; an OOM at once
                         signals={"oom": bool(res and res["oom"])}))
    if ctx.get("operator_stale"):
        out.append(_cond("J6", "fleet", "high", [], "The operator is silent while something waits to be relayed.",
                         alert=False, evals=1))
    # J7: usage starvation with several sessions working; one alert per reset window
    busy = [c for c, s in sstate.items() if s == "working"]
    if umode and len(busy) >= 2:
        out.append(_cond("J7", str(int(resets_at or 0)), "warn", busy,
                         "Usage is %s and %d sessions are working: let one finish, pause the rest." % (umode, len(busy)),
                         signals={"mode": umode}))
    out.sort(key=lambda c: c["id"])
    return out, answer_first


def off_doc(error=None, paused=False):
    """schedule.enabled: false or `board schedule off` (mode off), or a decision that failed (mode
    error): no advice at all, rather than old advice that no longer holds."""
    body = {"schema_version": SCHEMA_VERSION, "enabled": error is not None, "mode": "error" if error else "off",
            "usage": None, "level": None, "resources": None, "max_heavy": None, "heavy_used": None, "start": [],
            "queue": [], "stuck": [], "advisories": [], "answer_first": [], "due": {}, "jams": [],
            "lessons": {"applied": [], "hints": []}}
    if error:
        body["error"] = error
    if paused:
        body["paused"] = True
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


def context(home, now):
    """decide()'s ctx from $BOARD_HOME: usage.json, the burn rate, the active limit episode, the
    owner-away flag and whether operator:stale is open."""
    lim = read_json(os.path.join(home, "limit.json"))
    alerts = read_json(os.path.join(home, "alerts.json"))
    op = alerts.get("operator:stale") if isinstance(alerts, dict) else None
    samples = read_json(os.path.join(home, "usage-samples.json"))
    return {"usage": read_json(os.path.join(home, "usage.json")),
            "burn": samples.get("burn") if isinstance(samples, dict) else None,
            "limit": lim if isinstance(lim, dict) and lim.get("hit_at") and not lim.get("cleared_at") else None,
            "away": os.path.exists(os.path.join(home, "owner-away")),
            "operator_stale": isinstance(op, dict) and not op.get("resolved")}


def valid_codes(cfg, sessions):
    """Who may receive an advisory: fleet.yaml codes and the live sessions' codes."""
    codes = {s.get("code") for s in (cfg or {}).get("sessions") or [] if isinstance(s, dict)}
    codes |= {s.get("code") for s in sessions or [] if isinstance(s, dict) and s.get("state") != "offline"}
    import advisories
    return {c for c in codes if isinstance(c, str) and advisories.CODE_RE.fullmatch(c)}


def step(cfg, home, sessions, tasks, now):
    """The collector's step: decide, fold in the delivery and jam state (schedule-state.json),
    and write scheduler.json when the decision changed. Returns (doc, written)."""
    import advisories
    st = settings(cfg)
    with advisories.locked(home, wait_s=2.0):
        paused = advisories.paused(home)
        state = advisories.load_state(home)
        try:
            if not st["enabled"] or paused:
                doc = off_doc(paused=paused)
            else:
                ctx = context(home, now)
                ctx["lessons"] = lessonstore.active_rules(home)
                doc = decide(tasks, sessions, read_json(os.path.join(home, "resources.json")), st, now, ctx)
                try:
                    import notify
                    quiet = notify.in_quiet_hours(notify.load_config(), now)
                except Exception:  # noqa: BLE001 - no alerting config: not quiet
                    quiet = False
                live = {x.get("code") for x in sessions or [] if isinstance(x, dict) and x.get("state") != "offline"}
                new_state, resolved = advisories.advance(doc, state, st, valid_codes(cfg, sessions), now, quiet,
                                                         live=live)
                learn(cfg, home, doc, state, new_state, resolved, now)
                if new_state != state:
                    advisories.write_json(os.path.join(home, advisories.STATE_FILE), new_state)
        except Exception as exc:  # noqa: BLE001 - a bug must not leave stale advice in place
            doc = off_doc(type(exc).__name__)
        doc["digest"] = digest(doc)
        old = read_json(os.path.join(home, FILE))
        if isinstance(old, dict) and old.get("digest") == doc["digest"] and digest(old) == doc["digest"]:
            return old, False
        write_doc(home, dict(doc, generated_at=now))
        return doc, True


HIT_OFF_MS = 30 * 60 * 1000      # a rule counts a new hit only after it was off this long (no flapping)


def learn(cfg, home, doc, state, new_state, resolved, now):
    """Lessons: resolved jams become (or merge into) lessons; a rule that starts to match after
    HIT_OFF_MS off counts a hit; stale auto lessons retire. Episodes not yet captured (the lessons
    file was busy) wait in the state for the next pass. A failure here never stops the schedule."""
    applied = set((doc.get("lessons") or {}).get("applied") or [])
    was_on = set(state.get("lesson_on") or [])
    off = {k: v for k, v in (state.get("lesson_off") or {}).items() if isinstance(v, int)} \
        if isinstance(state.get("lesson_off"), dict) else {}
    hits = {i for i in applied - was_on if i not in off or now - off[i] >= HIT_OFF_MS}
    off = {i: t for i, t in off.items() if i not in applied}
    off.update({i: now for i in was_on - applied})
    pending = [e for e in state.get("to_learn") or [] if isinstance(e, dict)] + list(resolved)
    new_state.update(lesson_on=sorted(applied), lesson_off=off, to_learn=[])
    try:
        lessonstore.capture(home, pending, cfg, now)
    except Exception:  # noqa: BLE001 - busy or broken: keep the episodes for the next pass
        new_state["to_learn"] = pending[-advisories_keep():]
    try:
        lessonstore.maintain(home, cfg, hits, now)
    except Exception:  # noqa: BLE001
        new_state["lesson_on"] = sorted(was_on & applied)       # count these hits next pass


def advisories_keep():
    import advisories
    return advisories.KEEP_RESOLVED


def snapshot_digest(doc):
    """The snapshot's `schedule` field: changes only when the mode, the sessions with advice due
    or the open jams change, so the operator's Monitor wakes for those alone."""
    if not isinstance(doc, dict) or not doc.get("enabled"):
        return {"mode": (doc or {}).get("mode") or "off", "due": [], "jams": 0} if isinstance(doc, dict) else None
    return {"mode": doc.get("mode"), "due": sorted(doc.get("due") or {}), "jams": len(doc.get("jams") or [])}


def show(argv, home, out=sys.stdout):
    real = out

    class _Out:                                     # the file may hold anything: no control characters
        def write(self, text):
            real.write("\n".join(CTL_RE.sub("?", x) for x in text.split("\n")))
    out = _Out()
    doc = read_json(os.path.join(home, FILE))
    if "--json" in argv:
        out.write(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        return 0
    if not isinstance(doc, dict):
        out.write("no schedule yet (the collector writes %s)\n" % FILE)
        return 0
    if not doc.get("enabled"):
        out.write("scheduler off (%s)\n" % ("board schedule off" if doc.get("paused") else "fleet.yaml schedule.enabled: false"))
        return 0
    out.write("mode %s · resources %s%s · heavy %s/%s · start: %s · due: %s\n" % (
        doc.get("mode"), doc.get("resources"), (" (%s)" % doc["level"]) if doc.get("level") else "",
        doc.get("heavy_used"), doc.get("max_heavy"), ", ".join(doc.get("start") or []) or "-",
        ", ".join(sorted(doc.get("due") or {})) or "-"))
    for r in doc.get("queue") or []:
        out.write("%3s %-8s %-28s %-10s %4s %s%s\n" % (
            r.get("rank"), r.get("state"), r.get("task"), r.get("session") or r.get("owner") or "-", r.get("score"),
            ",".join("%s:%s" % (b.get("kind"), b.get("ref")) for b in r.get("blocked_by") or []) or "",
            (" stuck:" + r["stuck"]) if r.get("stuck") else ""))
    for a in doc.get("advisories") or []:
        out.write("  [%s] %s → %s (%s): %s\n" % (a.get("severity"), a.get("kind"), a.get("to"),
                                                (a.get("delivery") or {}).get("state", "-"), a.get("text")))
    for j in doc.get("jams") or []:
        out.write("  jam %s %s [%s]: %s\n" % (j.get("rule"), j.get("id"), j.get("severity"), j.get("text")))
    for a in doc.get("answer_first") or []:
        out.write("  answer first: %s (holds %d session(s), %d task(s))\n" % (
            a.get("decision"), a.get("unblocks_sessions") or 0, len(a.get("unblocks_tasks") or [])))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help") or argv[0] != "show":
        print(__doc__.strip())
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    import fleet
    return show(argv[1:], fleet.board_home())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
