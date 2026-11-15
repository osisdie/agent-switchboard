#!/usr/bin/env python3
"""Task store: the scheduling fields of $BOARD_HOME/tasks/<id>.json (docs/contract.md, "Tasks").

`board task new|stage` (bin/board) create tasks and move them through lifecycle stages; this
module adds the optional fields the scheduler (lib/scheduler.py) reads. Every field is optional,
so task files written before it stay valid. A file is rewritten atomically, mode 0600, with
`schema_version` 1; a file with a newer schema_version is never modified.

Usage (through `board task ...`):
  tasks.py set ID [--repo R] [--branch B] [--base B] [--size S|M|L] [--heavy yes|no]
                  [--priority 0-3]
  tasks.py dep ID --on TASK [--kind merge|deploy|done] | --remove TASK
  tasks.py pr ID --url URL --state open|merged|closed [--head SHA] [--method squash|merge|rebase]
  tasks.py done|pause|resume|drop|block ID
  tasks.py progress ID                    say "still moving" (resets the stall clock)
Prints "ID <what changed>". Exit 1 on a bad value or an unknown task, 2 on a usage error.
"""
import contextlib
import fcntl
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402

SCHEMA_VERSION = 1
STATUSES = ("active", "paused", "blocked", "done", "dropped")
VERBS = {"done": "done", "pause": "paused", "resume": "active", "drop": "dropped", "block": "blocked"}
SIZES = ("S", "M", "L")
DEP_KINDS = ("merge", "deploy", "done")
PR_STATES = ("open", "merged", "closed")
PR_METHODS = ("squash", "merge", "rebase")
ID_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")
REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,199}")      # repo slug, branch, base
SHA_RE = re.compile(r"[0-9a-f]{7,64}")
URL_RE = re.compile(r"https?://[^\s\x00-\x1f\x7f]{1,490}")


class Bad(Exception):
    pass


def now_ms():
    v = os.environ.get("BOARD_NOW_MS")
    return int(v) if v and v.isdigit() else int(time.time() * 1000)


def path_of(home, tid):
    if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
        raise Bad("invalid task id: %s" % str(tid)[:80])
    return os.path.join(home, "tasks", tid + ".json")


def load(home, tid):
    try:
        with open(path_of(home, tid), encoding="utf-8") as fh:
            t = json.load(fh)
    except FileNotFoundError:
        raise Bad("no such task: %s" % tid) from None
    except (OSError, ValueError) as exc:
        raise Bad("task %s unreadable: %s" % (tid, type(exc).__name__)) from None
    if not isinstance(t, dict):
        raise Bad("task %s is not an object" % tid)
    if t.get("id") != tid:
        raise Bad("task file %s.json holds another id; fix or remove it" % tid)
    v = t.get("schema_version", 1)
    if not isinstance(v, int) or v > SCHEMA_VERSION:
        raise Bad("task %s has schema_version %s; this board writes %d (upgrade it)" % (tid, v, SCHEMA_VERSION))
    return t


def save(home, t):
    path = path_of(home, t["id"])
    t["schema_version"] = SCHEMA_VERSION
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(t, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(home, tid, timeout_s=10.0):
    """tasks/<id>.json.lock, shared with bin/board's task writers (`task_write`)."""
    fd = os.open(path_of(home, tid) + ".lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        end = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise Bad("task %s is locked; try again" % tid) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def log_event(home, ev):
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def status_of(t):
    s = t.get("status")
    return s if s in STATUSES else "active"


def _ref(name, v):
    if not REF_RE.fullmatch(v) or ".." in v:
        raise Bad("--%s: letters, digits and ._/@:+- only (max 200), no '..'" % name)
    return v


def _opts(argv, allowed):
    """--key value pairs → dict; anything else is a usage error."""
    out = {}
    i = 0
    while i < len(argv):
        k = argv[i]
        if not k.startswith("--") or k[2:] not in allowed or i + 1 >= len(argv):
            raise SystemExit(_usage("unknown or incomplete option: %s" % k[:40]))
        if k[2:] in out:
            raise SystemExit(_usage("%s given twice" % k))
        out[k[2:]] = argv[i + 1]
        i += 2
    return out


def _usage(msg):
    sys.stderr.write("tasks: %s\n%s\n" % (msg, __doc__.split("Usage (through `board task ...`):\n")[1]))
    return 2


def cmd_set(home, t, o):
    if not o:
        raise SystemExit(_usage("set: nothing to set"))
    ch = {}
    for k in ("repo", "branch", "base"):
        if k in o:
            ch[k] = _ref(k, o[k])
    if "size" in o:
        if o["size"] not in SIZES:
            raise Bad("--size: one of %s" % " ".join(SIZES))
        ch["size"] = o["size"]
    if "heavy" in o:
        if o["heavy"] not in ("yes", "no", "true", "false"):
            raise Bad("--heavy: yes or no")
        ch["heavy"] = o["heavy"] in ("yes", "true")
    if "priority" in o:
        if o["priority"] not in ("0", "1", "2", "3"):
            raise Bad("--priority: 0-3")
        ch["priority"] = int(o["priority"])
    t.update(ch)
    return ch


def _deps_of(home, tid, cache):
    if tid not in cache:
        try:
            cache[tid] = [d.get("task") for d in load(home, tid).get("depends_on") or [] if isinstance(d, dict)]
        except Bad:
            cache[tid] = []
    return cache[tid]


def cmd_dep(home, t, o):
    deps = [d for d in (t.get("depends_on") or []) if isinstance(d, dict) and d.get("task")]
    if "remove" in o:
        if set(o) != {"remove"}:
            raise SystemExit(_usage("dep: --remove goes alone"))
        if not ID_RE.fullmatch(o["remove"]):
            raise Bad("--remove: invalid task id")
        deps = [d for d in deps if d["task"] != o["remove"]]
        t["depends_on"] = deps
        return {"removed": o["remove"]}
    if "on" not in o:
        raise SystemExit(_usage("dep: --on TASK is required"))
    on, kind = o["on"], o.get("kind", "done")
    if kind not in DEP_KINDS:
        raise Bad("--kind: one of %s" % " ".join(DEP_KINDS))
    if on == t["id"]:
        raise Bad("a task cannot depend on itself")
    load(home, on)                                   # must exist
    seen, todo, cache = set(), [on], {}
    while todo:                                      # does `on` (transitively) depend on t?
        cur = todo.pop()
        if cur == t["id"]:
            raise Bad("dependency cycle: %s already depends on %s" % (on, t["id"]))
        if cur not in seen:
            seen.add(cur)
            todo.extend(x for x in _deps_of(home, cur, cache) if isinstance(x, str))
    t["depends_on"] = [d for d in deps if d["task"] != on] + [{"task": on, "kind": kind}]
    return {"on": on, "kind": kind}


def cmd_pr(home, t, o):
    if "url" not in o or "state" not in o:
        raise SystemExit(_usage("pr: --url and --state are required"))
    if not URL_RE.fullmatch(o["url"]):
        raise Bad("--url: an http(s) URL without spaces")
    if o["state"] not in PR_STATES:
        raise Bad("--state: one of %s" % " ".join(PR_STATES))
    pr = {"url": o["url"], "state": o["state"]}
    if "head" in o:
        if not SHA_RE.fullmatch(o["head"]):
            raise Bad("--head: a lowercase hex commit sha")
        pr["head_sha"] = o["head"]
    if "method" in o:
        if o["method"] not in PR_METHODS:
            raise Bad("--method: one of %s" % " ".join(PR_METHODS))
        pr["method"] = o["method"]
    old = t.get("pr") if isinstance(t.get("pr"), dict) else {}
    if old.get("url") == pr["url"]:                  # same PR: keep what this call does not repeat
        pr = dict(old, **pr)
    if pr["state"] == "merged":
        pr["merged_at"] = old.get("merged_at") if old.get("state") == "merged" else now_ms()
    else:
        pr.pop("merged_at", None)
    t["pr"] = pr
    return {"pr": pr["state"]}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    verb, rest = argv[0], argv[1:]
    if verb not in ("set", "dep", "pr", "progress") and verb not in VERBS:
        return _usage("unknown command: %s" % verb[:40])
    if not rest or rest[0].startswith("-"):
        return _usage("%s: task id first" % verb)
    home = fleet.ensure_home()
    try:
        if not os.path.isfile(path_of(home, rest[0])):
            raise Bad("no such task: %s" % rest[0])
        with locked(home, rest[0]):
            t, ch, now = apply(home, verb, rest)
            if t is None:
                return ch
            save(home, t)
    except Bad as exc:
        sys.stderr.write("board: task %s: %s\n" % (verb, exc))
        return 1
    log_event(home, dict({"ts": now, "type": "task", "id": t["id"], "action": verb}, **ch))
    print("%s %s" % (t["id"], " ".join("%s=%s" % (k, v) for k, v in sorted(ch.items()))))
    return 0


def apply(home, verb, rest):
    """Load the task, apply VERB; returns (task, changes, now), or (None, exit code, None)."""
    t = load(home, rest[0])
    now = now_ms()
    if verb == "set":
        ch = cmd_set(home, t, _opts(rest[1:], ("repo", "branch", "base", "size", "heavy", "priority")))
    elif verb == "dep":
        ch = cmd_dep(home, t, _opts(rest[1:], ("on", "kind", "remove")))
    elif verb == "pr":
        ch = cmd_pr(home, t, _opts(rest[1:], ("url", "state", "head", "method")))
    else:
        if len(rest) > 1:
            return None, _usage("%s takes only the task id" % verb), None
        if verb == "progress":
            t["progress_at"] = now
            ch = {"progress_at": now}
        else:
            t["status"] = VERBS[verb]
            if verb == "done":
                t["done_at"] = now
            else:
                t.pop("done_at", None)
            ch = {"status": VERBS[verb]}
    t["updated_at"] = now
    return t, ch, now


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
