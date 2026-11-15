#!/usr/bin/env python3
"""System resources sampler: $BOARD_HOME/resources.json (see docs/contract.md, "Resources" and
docs/design/resources-and-scheduling.md). Python stdlib only (+ pyyaml through lib/fleet.py).

Usage:
  resources.py show [--json|--mirror] [--refresh]   print the last sample (--mirror: the bucketed
                                                    meta/resources document for the Artifact board;
                                                    --refresh: sample now, write nothing)
  resources.py doctor                               which metrics this platform provides

The collector calls step() on every run; it samples at most every `resources.interval` seconds
(default 15) and returns the snapshot digest {level, worst}, which only changes when a level
changes, so the operator's Monitor on snapshot.json never wakes for raw numbers.

Sources: Linux and WSL read <proc>/{stat,loadavg,meminfo,vmstat,pressure/memory,uptime} and
<proc>/<pid>/{stat,cmdline}; elsewhere os.getloadavg(), shutil-style statvfs and
`ps -axo pid=,ppid=,rss=,%cpu=,time=,etime=,args=`. A metric that cannot be read goes into
`unavailable`, never 0. Command lines are only matched, never stored: only `comm` and the class
leave this module (a teammate's argv carries keys).

The board never stops a process. It only shows what is running and alerts (lib/notify.py).

Test hooks: BOARD_PROC_ROOT (a fixture /proc tree; a missing directory selects the `ps` path),
BOARD_NOW_MS (fake clock, epoch ms).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402

LEVELS = ["ok", "warn", "high", "critical"]
DEFAULTS = {
    "enabled": True,
    "interval": 15,
    "disks": "auto",               # auto = BOARD_HOME + each live session's cwd; or a list of paths
    "thresholds": {                # [warn, high]; critical = high for 2 samples, or an OOM kill
        "mem_available_pct": [15, 8],
        "mem_available_mb": [2048, 1024],
        "swap_used_pct": [50, 80],
        "psi_mem_some_avg60": [10, 25],
        "load_per_cpu": [1.5, 3.0],
        "disk_free_pct": [10, 3],
        "disk_free_mb": [5120, 1024],
    },
    "teammate_idle_minutes": 30,
    "process_classes": {},         # {class: regex} overrides / additions
}
OPERATOR_STALE_MINUTES = 10        # schedule.operator_stale_minutes
DISK_TIMEOUT_S = 2.0               # all disk probes of one sample together
DISK_HUNG_MS = 10 * 60 * 1000      # a path that timed out is not probed again for this long
NETWORK_FS = re.compile(r"^(nfs\d?|cifs|smb\d?|smbfs|9p|fuse(\..*)?|fuseblk|sshfs|afs|ceph|glusterfs|lustre)$")
# Lower-is-worse metrics leave a level at threshold * 4/3 (enter at 15% free, leave at 20%);
# higher-is-worse ones at threshold * 3/4. A level changes after DWELL equal samples in a row.
LEAVE_LOW = 4.0 / 3.0
LEAVE_HIGH = 3.0 / 4.0
DWELL = 2
CRITICAL_AFTER = 2                 # samples at high
OOM_HOLD_MS = 10 * 60 * 1000       # an OOM kill keeps the level critical this long
IDLE_CPU_PCT = 1.0                 # a teammate tree under this CPU% (and no new process) is idle
HEAVY_RSS_MB = 1024
HEAVY_CLASSES = ("test", "build", "browser")
CLASS_ORDER = ("test", "build", "browser", "container")
CLASS_RE = {
    "test": r"\b(pytest|py\.test|jest|vitest|playwright test|go test|cargo test|mvn|gradlew?)\b",
    "build": r"\b(tsc|webpack|vite build|cargo build|docker build)\b",
    "browser": r"(chrome|chromium|firefox|headless_shell)",
    "container": r"\b(docker|containerd)\b",
}
# Matched on `comm` alone: an idle Playwright MCP server mentions "playwright"/"chrome" in its
# argv but is not a browser; the browser processes it starts are.
COMM_ONLY = ("browser",)
ARGV_COMM_RE = re.compile(r"^(claude|node|bun|python)", re.I)   # whose argv we read outside session trees
SHELLS = ("bash", "sh", "zsh", "dash", "fish")                     # a shell running `pytest` is not the test
NAME_RE = re.compile(r"[^A-Za-z0-9._@ -]")
MIB = 1024 * 1024
try:
    HZ = os.sysconf("SC_CLK_TCK")
    PAGE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError, OSError):  # pragma: no cover
    HZ, PAGE = 100, 4096


# ------------------------------------------------------------------ basics

def now_ms():
    fake = os.environ.get("BOARD_NOW_MS")
    if fake:
        try:
            return int(fake)
        except ValueError:
            pass
    return int(time.time() * 1000)


def proc_root():
    return os.environ.get("BOARD_PROC_ROOT") or "/proc"


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, data, indent=None):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=indent)
        fh.write("\n")
    os.replace(tmp, path)


def settings(cfg=None):
    """The `resources:` block of fleet.yaml over DEFAULTS, plus the operator-stale minutes."""
    cfg = cfg if cfg is not None else fleet.load_fleet()
    block = cfg.get("resources") if isinstance(cfg.get("resources"), dict) else {}
    out = fleet._merge(DEFAULTS, block)  # noqa: SLF001
    sched = cfg.get("schedule") if isinstance(cfg.get("schedule"), dict) else {}
    out["operator_stale_minutes"] = num(sched.get("operator_stale_minutes"), OPERATOR_STALE_MINUTES)
    out["interval"] = max(1.0, num(out.get("interval"), 15))
    out["teammate_idle_minutes"] = max(0.0, num(out.get("teammate_idle_minutes"), 30))
    serve = cfg.get("serve") if isinstance(cfg.get("serve"), dict) else {}
    out["serve_enabled"] = bool(serve.get("enabled"))
    out["thresholds"], out["config_errors"] = clean_thresholds(out.get("thresholds"))
    out["process_classes"], errs = clean_classes(out.get("process_classes"))
    out["config_errors"] += errs
    return out


def clean_classes(pc):
    """resources.process_classes as {class: regex}. Anything else (a list, a string) or a pattern
    that does not compile is dropped and reported, never raised: a bad override must not stop the
    sampler (and with it res:* and the operator-watch input)."""
    if pc in (None, {}):
        return {}, []
    if not isinstance(pc, dict):
        return {}, ["resources.process_classes: %r is not a map of class: regex; ignored" % (pc,)]
    out, errors = {}, []
    for k, v in pc.items():
        try:
            if not isinstance(v, str) or not v:
                raise re.error("not a string")
            re.compile(v)
            out[str(k)] = v
        except re.error as exc:
            errors.append("resources.process_classes.%s: %r is not a valid regex (%s); ignored" % (k, v, exc))
    return out, errors

def clean_thresholds(thr):
    """Every threshold as [warn, high] floats. A bad entry falls back to its default and is
    reported (resources.json `config_errors`, `board doctor`, stderr) instead of stopping the
    sampler."""
    out, errors = {}, []
    thr = thr if isinstance(thr, dict) else {}
    for key, default in DEFAULTS["thresholds"].items():
        v = thr.get(key, default)
        try:
            if not isinstance(v, (list, tuple)) or len(v) != 2:
                raise ValueError
            out[key] = [float(v[0]), float(v[1])]
        except (TypeError, ValueError):
            errors.append("resources.thresholds.%s: %r is not [warn, high] numbers; using %s" % (key, v, default))
            out[key] = list(default)
    return out, errors


def num(v, default):
    try:
        return float(v) if v is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def mb(nbytes):
    return int(round(nbytes / MIB))


def safe_name(s, n=64):
    return NAME_RE.sub("?", str(s or ""))[:n]


def home_rel(path):
    """~ for the home directory, so a path label never names the user."""
    home = os.path.expanduser("~")
    p = os.path.abspath(path)
    return "~" + p[len(home):] if p == home or p.startswith(home + os.sep) else p


# ------------------------------------------------------------------ system metrics (Linux / WSL)

def have_proc(root=None):
    return os.path.isfile(os.path.join(root or proc_root(), "meminfo"))


def detect_platform(root=None):
    root = root or proc_root()
    if have_proc(root):
        ver = (read_text(os.path.join(root, "version")) or "") + (read_text(os.path.join(root, "sys", "kernel", "osrelease")) or "")
        return "wsl" if "microsoft" in ver.lower() else "linux"
    return "darwin" if sys.platform == "darwin" else "other"


def read_meminfo(root):
    out = {}
    for line in (read_text(os.path.join(root, "meminfo")) or "").splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = int(parts[1]) * 1024  # kB -> bytes
            except ValueError:
                pass
    return out


def read_cpu_times(root):
    """(busy, total) jiffies of the "cpu" line, and the number of cpuN lines."""
    busy = total = None
    ncpu = 0
    for line in (read_text(os.path.join(root, "stat")) or "").splitlines():
        if line.startswith("cpu "):
            vals = [int(x) for x in line.split()[1:] if x.isdigit()]
            if len(vals) >= 4:
                total = sum(vals[:8])
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                busy = total - idle
        elif re.match(r"cpu\d+ ", line):
            ncpu += 1
    return busy, total, ncpu


def read_loadavg(root):
    txt = read_text(os.path.join(root, "loadavg"))
    try:
        return [float(x) for x in txt.split()[:3]]
    except (AttributeError, ValueError):
        return None


def read_oom_kills(root):
    for line in (read_text(os.path.join(root, "vmstat")) or "").splitlines():
        if line.startswith("oom_kill "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def read_psi(root):
    for line in (read_text(os.path.join(root, "pressure", "memory")) or "").splitlines():
        if line.startswith("some "):
            m = re.search(r"avg60=([0-9.]+)", line)
            if m:
                return float(m.group(1))
    return None


def read_uptime(root):
    txt = read_text(os.path.join(root, "uptime"))
    try:
        return float(txt.split()[0])
    except (AttributeError, IndexError, ValueError):
        return None


# ------------------------------------------------------------------ processes

def scan_proc(root, now_s):
    """{pid: {pid, ppid, comm, cpu_s, start, age_s, rss_kb}} from <root>/<pid>/stat."""
    up = read_uptime(root)
    out = {}
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        txt = read_text(os.path.join(root, name, "stat"))
        if not txt or ")" not in txt:
            continue
        comm = txt[txt.find("(") + 1:txt.rfind(")")]
        f = txt[txt.rfind(")") + 2:].split()
        try:
            start = int(f[19]) / float(HZ)
            out[int(name)] = {
                "pid": int(name), "ppid": int(f[1]), "comm": comm,
                "cpu_s": (int(f[11]) + int(f[12])) / float(HZ),
                "start": round(start, 2), "age_s": max(0.0, up - start) if up is not None else None,
                "rss_kb": int(f[21]) * PAGE // 1024, "pcpu": None,
            }
        except (IndexError, ValueError):
            continue
    return out


def proc_argv(root, pid):
    raw = None
    try:
        with open(os.path.join(root, str(pid), "cmdline"), "rb") as fh:
            raw = fh.read(4096)
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def parse_clock(s):
    """ps time/etime "[[dd-]hh:]mm:ss[.cc]" -> seconds."""
    s = str(s or "").strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d) if d.isdigit() else 0
    parts = s.split(":")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return None
    sec = 0.0
    for v in vals:
        sec = sec * 60 + v
    return days * 86400 + sec


def scan_ps(now_s):
    """{pid: {...}} from `ps`; each entry also carries its argv (ps prints it anyway)."""
    try:
        txt = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,%cpu=,time=,etime=,args="],
                             capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    out = {}
    for line in txt.splitlines():
        f = line.split(None, 6)
        if len(f) < 7 or not f[0].isdigit():
            continue
        try:
            age = parse_clock(f[5])
            argv = f[6].split()
            # comm from the executable path, which may hold spaces ("/Applications/Google
            # Chrome.app/.../Google Chrome --type=renderer"): everything before the first " -".
            exe = f[6].split(" -", 1)[0].strip()
            out[int(f[0])] = {
                "pid": int(f[0]), "ppid": int(f[1]), "rss_kb": int(f[2]), "pcpu": float(f[3]),
                "cpu_s": parse_clock(f[4]) or 0.0, "age_s": age,
                "start": round(now_s - age) if age is not None else None,
                "comm": os.path.basename(exe)[:15] if exe else "?", "argv": argv,
            }
        except ValueError:
            continue
    return out


def flag_value(argv, name):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a[len(name) + 1:]
    return None


def is_claude(p):
    argv = p.get("argv") or []
    a0 = os.path.basename(argv[0]) if argv else ""
    if p["comm"].lower().startswith("claude") or a0.lower().startswith("claude"):
        return True
    return any("claude-code" in a for a in argv[:2])


def teammate_info(p):
    """{name, agent_id, parent} for a teammate claude process (--agent-id / --agent-name), else None."""
    argv = p.get("argv") or []
    aid = flag_value(argv, "--agent-id")
    name = flag_value(argv, "--agent-name")
    if not (aid or name) or not is_claude(p):
        return None
    return {"name": safe_name(name or (aid or "").split("@")[0] or "?"),
            "team": (aid or "").split("@", 1)[1] if aid and "@" in aid else None,
            "parent": flag_value(argv, "--parent-session-id")}


def compile_classes(overrides):
    pats = dict(CLASS_RE)
    for k, v in (overrides if isinstance(overrides, dict) else {}).items():
        if isinstance(v, str) and v:
            pats[str(k)] = v
    order = list(CLASS_ORDER) + [k for k in pats if k not in CLASS_ORDER]
    out = []
    for k in order:
        try:
            out.append((k, re.compile(pats[k], re.I)))
        except re.error:
            continue
    return out


def classify(p, classes):
    if p.get("teammate"):
        return "teammate"
    if is_claude(p):
        return "claude"
    if p["comm"] in SHELLS:
        return "other"
    argv = p.get("argv") or []
    target = " ".join([p["comm"]] + [os.path.basename(a) for a in argv[:4]])
    for name, rx in classes:
        if rx.search(p["comm"] if name in COMM_ONLY else target):
            return name
    return "other"


def is_watch(p):
    argv = p.get("argv") or []
    return "--watch" in argv and any(os.path.basename(a) in ("serve.py", "board-serve") for a in argv[:3])


# ------------------------------------------------------------------ levels

def metric_candidate(value, thr, lower_is_worse, current):
    """The level a value implies, with hysteresis around the current committed level."""
    if value is None or not isinstance(thr, (list, tuple)) or len(thr) < 2:
        return "ok"
    for lvl, t in (("high", thr[1]), ("warn", thr[0])):
        if t is None:
            continue
        try:
            t = float(t)
        except (TypeError, ValueError):  # settings() already replaced bad values; belt and braces
            continue
        held = LEVELS.index(current) >= LEVELS.index(lvl)
        if lower_is_worse:
            if value <= (t * LEAVE_LOW if held else t):
                return lvl
        elif value >= (t * LEAVE_HIGH if held else t):
            return lvl
    return "ok"


def step_level(st, cand):
    """Dwell, per direction: DWELL samples in a row worse than the committed level move it up,
    DWELL in a row better move it down, to the step of that streak closest to the committed level
    (from ok, 7% then 9% available is warn; from high, 19% then 21% is warn). A sample at the
    committed level ends both streaks. `st` = {level, up_n, up_to, down_n, down_to, high_n};
    returns the level shown (critical after CRITICAL_AFTER samples at high)."""
    cur = st.get("level") or "ok"
    ci, ki = LEVELS.index(cand), LEVELS.index(cur)
    if ci > ki:
        st["up_to"] = min(ci, st["up_to"]) if st.get("up_n") else ci
        st["up_n"], st["down_n"] = st.get("up_n", 0) + 1, 0
    elif ci < ki:
        st["down_to"] = max(ci, st["down_to"]) if st.get("down_n") else ci
        st["down_n"], st["up_n"] = st.get("down_n", 0) + 1, 0
    else:
        st["up_n"] = st["down_n"] = 0
    if st.get("up_n", 0) >= DWELL:
        cur, st["up_n"] = LEVELS[st["up_to"]], 0
    elif st.get("down_n", 0) >= DWELL:
        cur, st["down_n"] = LEVELS[st["down_to"]], 0
    st.pop("cand", None)
    st.pop("n", None)
    st["level"] = cur
    st["high_n"] = st.get("high_n", 0) + 1 if cur == "high" else 0
    return "critical" if cur == "high" and st["high_n"] >= CRITICAL_AFTER else cur


def evaluate(values, thresholds, mstate, prune=True):
    """values: {metric: [(value, threshold_key, lower_is_worse), ...]}. Mutates mstate
    ({metric: dwell state}); returns {metric: level}. With prune, the state of metrics missing
    from `values` is dropped."""
    out = {}
    for metric, subs in values.items():
        st = mstate.setdefault(metric, {"level": "ok"})
        cur = st.get("level") or "ok"
        cand = "ok"
        for value, key, low in subs:
            c = metric_candidate(value, thresholds.get(key), low, cur)
            if LEVELS.index(c) > LEVELS.index(cand):
                cand = c
        out[metric] = step_level(st, cand)
    if prune:
        for gone in [m for m in mstate if m not in values]:
            mstate.pop(gone, None)
    return out


WORST_ORDER = ("oom", "mem", "swap", "disk", "load")


def overall(levels):
    """(level, worst metric | None)."""
    best, worst = "ok", None
    for m in sorted(levels, key=lambda k: (WORST_ORDER.index(k.split(":")[0]) if k.split(":")[0] in WORST_ORDER else 9, k)):
        if LEVELS.index(levels[m]) > LEVELS.index(best):
            best, worst = levels[m], m
    return best, worst


# ------------------------------------------------------------------ disks

def statvfs(path):
    return os.statvfs(path)


def mount_of(path):
    p = os.path.realpath(path)
    while not os.path.ismount(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def read_mounts(root=None):
    """[(mount point, fstype)] from <proc>/self/mountinfo, longest mount point first."""
    out = []
    for line in (read_text(os.path.join(root or proc_root(), "self", "mountinfo")) or "").splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        f, r = left.split(), right.split()
        if len(f) < 5 or not r:
            continue
        out.append((re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), f[4]), r[0]))
    out.sort(key=lambda x: -len(x[0]))
    return out


def fs_type(path, mounts):
    """The filesystem type of PATH by mount-point prefix only: no syscall on PATH itself."""
    p = os.path.abspath(path)
    for mp, fst in mounts:
        if p == mp or p.startswith(mp.rstrip("/") + "/"):
            return fst
    return None


def _probe(path):
    """(mount point, statvfs) of PATH, or None when PATH is not a directory."""
    if not os.path.isdir(path):
        return None
    mnt = mount_of(path)
    return mnt, statvfs(mnt)


def probe_all(paths, timeout=None):
    """{path: (mount, statvfs) | None | "timeout"}. Each path is probed in a daemon thread and the
    whole batch waits at most `timeout`, so a hung network mount cannot stall the collector (or
    operator:stale with it). NFS and FUSE waits are killable, so the process still exits."""
    box, threads = {}, []
    for path in paths:
        def run(p=path):
            try:
                box[p] = _probe(p)
            except OSError:
                box[p] = None
        t = threading.Thread(target=run, daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + (DISK_TIMEOUT_S if timeout is None else timeout)
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return {p: box[p] if p in box else "timeout" for p in paths}


def disk_targets(rcfg, home, sessions):
    """[(path, label, explicit)]: BOARD_HOME and each live session's cwd (auto), or the
    configured paths (labelled disks[0], disks[1], ... so no path reaches the board)."""
    spec = rcfg.get("disks", "auto")
    if isinstance(spec, list):
        return [(os.path.expanduser(str(p)), "disks[%d]" % i, True) for i, p in enumerate(spec) if p]
    out = [(home, "$BOARD_HOME", False)]
    for s in sessions:
        if s.get("cwd") and s.get("pid"):
            out.append((os.path.expanduser(str(s["cwd"])), str(s.get("code")), False))
    return out


def _disk_id(state, key):
    ids = state.setdefault("disk_ids", {})
    if key not in ids:
        used = {v for v in ids.values()}
        n = 1
        while "disk%d" % n in used:
            n += 1
        ids[key] = "disk%d" % n
    return ids[key]


def sample_disks(rcfg, home, sessions, state, now):
    """Disks by mount, each with an opaque id (disk1, disk2, ... stable across samples) and the
    labels of what lives there. Network and FUSE filesystems (nfs, cifs, 9p, fuse, ...) are
    skipped unless configured explicitly; a path that does not answer within DISK_TIMEOUT_S is
    reported `unresponsive` and left alone for DISK_HUNG_MS."""
    mounts = read_mounts()
    hung = state.setdefault("disk_hung", {})
    for p in [p for p, t in hung.items() if now - int(t) >= DISK_HUNG_MS]:
        hung.pop(p, None)
    targets = []
    for path, label, explicit in disk_targets(rcfg, home, sessions):
        if not explicit and NETWORK_FS.match(fs_type(path, mounts) or ""):
            continue
        targets.append((path, label))
    paths = sorted({p for p, _ in targets if p not in hung})
    res = probe_all(paths) if paths else {}
    by_mount, stuck = {}, {}
    for path, label in targets:
        r = "timeout" if path in hung else res.get(path)
        if r == "timeout":
            hung.setdefault(path, now)
            d = stuck.setdefault(path, {"id": _disk_id(state, "path:" + path), "mount": None, "total_mb": None,
                                        "free_mb": None, "free_pct": None, "labels": [], "unresponsive": True})
        elif r is None:
            continue
        else:
            mnt, st = r
            d = by_mount.get(mnt)
            if d is None:
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                d = by_mount[mnt] = {"id": _disk_id(state, mnt), "mount": home_rel(mnt) if mnt != "/" else "/",
                                     "total_mb": mb(total), "free_mb": mb(free),
                                     "free_pct": int(round(100.0 * free / total)) if total else None, "labels": []}
        if label not in d["labels"]:
            d["labels"].append(label)
    out = list(by_mount.values()) + list(stuck.values())
    return sorted(out, key=lambda d: (len(d["id"]), d["id"]))


# ------------------------------------------------------------------ the sample

def _tree(procs):
    kids = {}
    for p in procs.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])
    return kids


def _descendants(kids, pid, stop=()):
    out, todo, seen = [], list(kids.get(pid, [])), {pid}
    while todo:
        c = todo.pop()
        if c in stop or c in seen:  # seen: a ppid loop (pid reuse between reads) cannot spin
            continue
        seen.add(c)
        out.append(c)
        todo.extend(kids.get(c, []))
    return out


def _cpu_pct(p, prev, elapsed_s):
    """CPU% over the interval from the cumulative CPU time (/proc stat ticks, or ps `time`).
    Without a previous sample of this process: ps's own %cpu where there is one, else None."""
    old = prev.get(str(p["pid"]))
    if not old or elapsed_s <= 0 or abs(float(old.get("start") or 0) - float(p.get("start") or 0)) > 2:
        return p.get("pcpu")
    return max(0.0, (p["cpu_s"] - float(old.get("cpu_s") or 0)) / elapsed_s * 100.0)


def _round_s(v):
    return None if v is None else int(v // 60 * 60) if v >= 120 else int(v)


def sample(rcfg, home, sessions, state, now):
    """One sample. `sessions`: snapshot rows ({code, pid, session_id, cwd, aliases}). `state` is
    the previous resources-state.json (mutated). Returns the resources.json document."""
    root = proc_root()
    now_s = now / 1000.0
    linux = have_proc(root)
    plat = detect_platform(root)
    unavailable = []
    elapsed = (now - int(state.get("at") or 0)) / 1000.0 if state.get("at") else 0.0
    prev_procs = state.get("procs") or {}
    doc = {"at": now, "platform": plat, "level": "ok", "worst": None}

    # -- system
    cpu = {"pct": None, "ncpu": os.cpu_count() or None, "load": None, "load_per_cpu": None}
    mem = {"total_mb": None, "available_mb": None, "available_pct": None, "psi_some_avg60": None}
    swap = {"total_mb": None, "used_pct": None}
    oom = None
    if linux:
        busy, total, ncpu = read_cpu_times(root)
        if ncpu:
            cpu["ncpu"] = ncpu
        pb, pt = (state.get("cpu_times") or [None, None])[:2] if isinstance(state.get("cpu_times"), list) else (None, None)
        if busy is not None and pb is not None and total > pt:
            cpu["pct"] = int(round(100.0 * (busy - pb) / (total - pt)))
        state["cpu_times"] = [busy, total] if busy is not None else None
        cpu["load"] = read_loadavg(root)
        mi = read_meminfo(root)
        if mi.get("MemTotal") and "MemAvailable" in mi:
            mem["total_mb"] = mb(mi["MemTotal"])
            mem["available_mb"] = mb(mi["MemAvailable"])
            mem["available_pct"] = int(round(100.0 * mi["MemAvailable"] / mi["MemTotal"]))
        else:
            unavailable.append("mem")
        if mi.get("SwapTotal"):
            swap["total_mb"] = mb(mi["SwapTotal"])
            swap["used_pct"] = int(round(100.0 * (mi["SwapTotal"] - mi.get("SwapFree", 0)) / mi["SwapTotal"]))
        else:
            unavailable.append("swap")
        mem["psi_some_avg60"] = read_psi(root)
        if mem["psi_some_avg60"] is None:
            unavailable.append("psi")
        oom = read_oom_kills(root)
        if oom is None:
            unavailable.append("oom")
        procs = scan_proc(root, now_s)
    else:
        try:
            cpu["load"] = [round(x, 2) for x in os.getloadavg()]
        except (AttributeError, OSError):
            cpu["load"] = None
        unavailable += ["mem", "swap", "psi", "oom"]
        procs = scan_ps(now_s)
        if procs is None:
            procs = {}
            unavailable.append("processes")
    if cpu["load"] is not None:
        cpu["load"] = [round(x, 2) for x in cpu["load"]]
        if cpu["ncpu"]:
            cpu["load_per_cpu"] = round(cpu["load"][0] / cpu["ncpu"], 2)
    else:
        unavailable.append("load")

    # -- processes: session trees and teammates
    for p in procs.values():
        if "argv" not in p and ARGV_COMM_RE.match(p["comm"]):
            p["argv"] = proc_argv(root, p["pid"])
    kids = _tree(procs)
    by_sid = {s.get("session_id"): s for s in sessions if s.get("session_id") and s.get("pid")}
    by_code = {}
    for s in sessions:
        if s.get("pid"):
            for a in [s.get("code")] + list(s.get("aliases") or []):
                if a:
                    by_code.setdefault(str(a).lower(), s)
    live = [s for s in sessions if isinstance(s.get("pid"), int) and s["pid"] in procs]
    owner_of = {}
    for s in live:
        for c in _descendants(kids, s["pid"]):
            owner_of.setdefault(c, s["code"])
    teammates = {}
    watch = False
    for p in procs.values():
        if is_watch(p):
            watch = True
        tm = teammate_info(p)
        if tm:
            p["teammate"] = True
            parent = by_sid.get(tm["parent"]) if tm["parent"] else None
            code = parent["code"] if parent else owner_of.get(p["pid"])
            if not code and tm["team"]:
                code = (by_code.get(tm["team"].lower()) or {}).get("code")
            teammates[p["pid"]] = (code, tm)
    classes = compile_classes(rcfg.get("process_classes"))
    tm_pids = set(teammates)
    live = [s for s in live if s["pid"] not in tm_pids]  # a teammate that also registered: count it once

    def child_row(pid, via=None):
        p = procs[pid]
        if "argv" not in p:
            p["argv"] = proc_argv(root, pid) if linux else []
        cls = classify(p, classes)
        rss = mb(p["rss_kb"] * 1024)
        return {"class": cls, "comm": safe_name(p["comm"], 32), "pid": pid, "rss_mb": rss,
                "cpu_pct": _round_pct(_cpu_pct(p, prev_procs, elapsed)), "age_s": _round_s(p.get("age_s")),
                "heavy": cls in HEAVY_CLASSES or rss > HEAVY_RSS_MB, "via": via}

    tracked = {}
    heavy_roots = 0

    def count_heavy(rows):
        n = 0
        heavy = {r["pid"] for r in rows if r["heavy"]}
        for r in rows:
            if not r["heavy"]:
                continue
            pp = procs[r["pid"]]["ppid"]
            if pp in heavy:
                continue
            n += 1
        return n

    tm_rows = {}
    for pid, (code, tm) in teammates.items():
        sub = [c for c in _descendants(kids, pid)]
        rows = [child_row(c, via=tm["name"]) for c in sub]
        allp = [procs[pid]] + [procs[c] for c in sub]
        rss = mb(sum(x["rss_kb"] for x in allp) * 1024)
        pcts = [_cpu_pct(x, prev_procs, elapsed) for x in allp]
        known = [x for x in pcts if x is not None]
        cpu_pct = sum(known) if known else None
        fresh = any(str(x["pid"]) not in prev_procs for x in allp[1:])
        tstate = (state.get("teammates") or {}).get(str(pid)) or {}
        same = tstate and abs(float(tstate.get("start") or 0) - float(procs[pid].get("start") or 0)) <= 2
        if same:  # idle = no CPU above IDLE_CPU_PCT and no new process in its tree since then
            last_active = int(tstate.get("last_active_at") or now)
            if fresh or (cpu_pct is not None and cpu_pct >= IDLE_CPU_PCT):
                last_active = now
            idle_s = max(0.0, (now - last_active) / 1000.0)
        else:     # first sight: nothing to compare yet
            last_active, idle_s = now, None
        tracked.setdefault("teammates", {})[str(pid)] = {"start": procs[pid].get("start"), "last_active_at": last_active}
        tm_rows[pid] = (code, {
            "name": tm["name"], "pid": pid, "rss_mb": rss, "cpu_pct": _round_pct(cpu_pct),
            "age_s": _round_s(procs[pid].get("age_s")), "idle_s": _round_s(idle_s),
            "idle": idle_s is not None and idle_s >= rcfg["teammate_idle_minutes"] * 60,
        }, rows)
        for x in allp:
            tracked.setdefault("procs", {})[str(x["pid"])] = {"start": x.get("start"), "cpu_s": round(x["cpu_s"], 2)}

    srows = []
    for s in live:
        sp = procs[s["pid"]]
        sub = _descendants(kids, s["pid"], stop=tm_pids)
        rows = [child_row(c) for c in sub]
        for x in [sp] + [procs[c] for c in sub]:
            tracked.setdefault("procs", {})[str(x["pid"])] = {"start": x.get("start"), "cpu_s": round(x["cpu_s"], 2)}
        tms = []
        for pid, (code, row, trows) in sorted(tm_rows.items()):
            if code == s["code"]:
                tms.append(row)
                rows.extend(trows)
        heavy_roots += count_heavy(rows)
        srows.append(_session_row(s, sp, rows, tms, prev_procs, elapsed))
    orphans = []
    for pid, (code, row, trows) in sorted(tm_rows.items()):
        if not code or code not in {s["code"] for s in live}:
            orphans.append(row)
            heavy_roots += count_heavy(trows)
    state["procs"] = tracked.get("procs", {})
    state["teammates"] = tracked.get("teammates", {})

    # -- disks
    disks = sample_disks(rcfg, home, sessions, state, now)
    if any(d.get("unresponsive") for d in disks):
        unavailable.append("disk")

    # -- oom
    prev_oom = state.get("oom_kills")
    delta = 0
    if oom is not None and isinstance(prev_oom, int):
        delta = oom - prev_oom if oom >= prev_oom else oom  # went backwards: a reboot, count from 0
    if delta > 0:
        state["last_oom_at"] = now
    state["oom_kills"] = oom
    oom_recent = bool(state.get("last_oom_at")) and now - int(state["last_oom_at"]) < OOM_HOLD_MS

    # -- levels
    thr = rcfg.get("thresholds") or {}
    values = {}
    if mem["available_pct"] is not None:
        values["mem"] = [(mem["available_pct"], "mem_available_pct", True),
                         (mem["available_mb"], "mem_available_mb", True),
                         (mem["psi_some_avg60"], "psi_mem_some_avg60", False)]
    if cpu["load_per_cpu"] is not None:
        values["load"] = [(cpu["load_per_cpu"], "load_per_cpu", False)]
    for d in disks:
        if not d.get("unresponsive"):
            values["disk:" + d["id"]] = [(d["free_pct"], "disk_free_pct", True), (d["free_mb"], "disk_free_mb", True)]
    mstate = state.setdefault("metrics", {})
    levels = evaluate(values, thr, mstate, prune=False)
    if swap["used_pct"] is not None:
        # Swap use alone is normal (idle pages swapped out): it only counts while memory itself,
        # PSI included, is at warn or worse. Until then its candidate is ok.
        pressed = levels.get("mem") in ("warn", "high", "critical")
        levels.update(evaluate({"swap": [(swap["used_pct"] if pressed else None, "swap_used_pct", False)]},
                               thr, mstate, prune=False))
    for gone in [m for m in mstate if m not in levels]:
        mstate.pop(gone, None)
    if oom_recent:
        levels["oom"] = "critical"
    for d in disks:
        d["level"] = levels.get("disk:" + d["id"]) if not d.get("unresponsive") else None
    level, worst = overall(levels)

    doc.update({
        "level": level, "worst": worst, "levels": levels, "cpu": cpu, "mem": mem, "swap": swap,
        "oom_kills": oom, "oom_kills_delta": delta, "oom_recent": oom_recent,
        "last_oom_at": state.get("last_oom_at"), "disks": disks, "sessions": srows,
        "orphan_teammates": orphans, "heavy_running": heavy_roots,
        "teammate_idle_minutes": rcfg["teammate_idle_minutes"],
        "operator": {"watch": watch if procs else None},
        "unavailable": sorted(set(unavailable)), "interval": rcfg["interval"], "thresholds": thr,
        "config_errors": rcfg.get("config_errors") or [],
    })
    state["at"] = now
    return doc


def _round_pct(v):
    return None if v is None else int(round(v))


def _session_row(s, sp, rows, tms, prev, elapsed):
    classes = {}
    for r in rows:
        c = classes.setdefault(r["class"], {"n": 0, "rss_mb": 0, "cpu_pct": 0})
        c["n"] += 1
        c["rss_mb"] += r["rss_mb"]
        c["cpu_pct"] += r["cpu_pct"] or 0
    top = []
    for cls in sorted(classes):
        mine = sorted((r for r in rows if r["class"] == cls), key=lambda r: (-r["rss_mb"], r["pid"]))
        top.extend(mine[:3])
    return {
        "code": s["code"], "pid": s["pid"], "rss_mb": mb(sp["rss_kb"] * 1024),
        "cpu_pct": _round_pct(_cpu_pct(sp, prev, elapsed)),
        "tree_rss_mb": mb((sp["rss_kb"] + sum(r["rss_mb"] * 1024 for r in rows if not r["via"])) * 1024),
        "classes": classes, "children": top, "teammates": tms,
        "teammates_n": len(tms), "teammates_rss_mb": sum(t["rss_mb"] for t in tms),
        "idle_teammates_n": sum(1 for t in tms if t["idle"]),
        "heavy_n": sum(1 for r in rows if r["heavy"]),
    }


def disabled_doc(now):
    """resources.enabled: false — no metrics, but the operator watch is still checked (J6)."""
    root = proc_root()
    if have_proc(root):
        procs = scan_proc(root, now / 1000.0)
        for p in procs.values():
            if ARGV_COMM_RE.match(p["comm"]):
                p["argv"] = proc_argv(root, p["pid"])
    else:
        procs = scan_ps(now / 1000.0) or {}
    return {"at": now, "enabled": False, "platform": detect_platform(root), "level": None, "worst": None,
            "operator": {"watch": any(is_watch(p) for p in procs.values()) if procs else None}}


# ------------------------------------------------------------------ collector entry point

def paths(home):
    return os.path.join(home, "resources.json"), os.path.join(home, "resources-state.json")


def digest(doc):
    """The snapshot's `resources` field: changes only when a level changes."""
    if not isinstance(doc, dict) or doc.get("enabled") is False or not doc.get("level"):
        return None
    return {"level": doc.get("level"), "worst": doc.get("worst")}


REWRITE_MS = 60 * 1000              # resources.json is rewritten at least this often (freshness)


def _load_state(home):
    state = read_json(paths(home)[1])
    return state if isinstance(state, dict) else {}


def _sample_doc(rcfg, home, sessions, state, now):
    if rcfg.get("enabled") is False:
        state.clear()
        state["at"] = now
        return disabled_doc(now)
    for e in rcfg.get("config_errors") or []:
        sys.stderr.write("resources: %s\n" % e)
    return sample(rcfg, home, sessions, state, now)


def step(cfg, home, sessions, now=None):
    """Called on every collector run. Samples when `interval` has passed and returns the snapshot
    digest. resources.json is rewritten when it changed (`at` aside), and at least every
    REWRITE_MS, so its `at` always says whether the sampler (and the operator-watch check) is
    alive, also with resources.enabled: false."""
    now = now if now is not None else now_ms()
    rcfg = settings(cfg)
    rpath, spath = paths(home)
    state = _load_state(home)
    cur = read_json(rpath)
    last = int(state.get("at") or 0)
    if last and last <= now < last + rcfg["interval"] * 1000 - 500:
        return digest(cur)
    doc = _sample_doc(rcfg, home, sessions, state, now)
    write_json_atomic(spath, state)
    old_at = int(cur.get("at") or 0) if isinstance(cur, dict) else 0
    if _meaningful(cur) != _meaningful(doc) or not old_at or now - old_at >= REWRITE_MS or now < old_at:
        write_json_atomic(rpath, doc, indent=1)
    return digest(doc)


def _meaningful(doc):
    """What decides a rewrite: the bucketed board view (percentages to 5%, sizes to 100 MB, ages
    to the minute; levels, worst, OOM, teammates and the operator watch exactly), without `at`.
    So resources.json (and the board's SSE) change on real changes, plus the REWRITE_MS refresh."""
    if not isinstance(doc, dict):
        return None
    v = mirror_view(doc) or {}
    v.pop("at", None)

    def minutes(x):
        if isinstance(x, dict):
            return {k: (int(x[k]) // 60 if k in ("age_s", "idle_s") and x[k] is not None else minutes(x[k])) for k in x}
        return [minutes(i) for i in x] if isinstance(x, list) else x
    return json.dumps([minutes(v), doc.get("config_errors") or []], sort_keys=True)


def preview(cfg, home, sessions, now=None):
    """A fresh sample for `board resources --refresh`: CPU deltas against the collector's last
    sample, but nothing is written, so the collector's dwell state does not advance."""
    now = now if now is not None else now_ms()
    return _sample_doc(settings(cfg), home, sessions, json.loads(json.dumps(_load_state(home))), now)


# ------------------------------------------------------------------ operator health (J6)

def operator_health(op, res, rcfg, pending_n, now):
    """Is the local-mode operator gone while something waits? -> {stale, reasons, heartbeat_at, watch}.
    Only with serve.enabled (the self-hosted board, whose operator writes operator.json and runs
    `board-serve --watch`): stale when the heartbeat is older than operator_stale_minutes or
    missing, or no --watch process exists. In Artifact mode the heartbeat lives in meta/operator,
    which the collector cannot read, so nothing is judged (the page's banner covers it). Only
    counts when pending_n > 0 (what --watch would hand the operator: answered questions, new
    messages)."""
    hb = None
    if isinstance(op, dict):
        try:
            hb = int(float(op.get("heartbeat_at"))) if op.get("heartbeat_at") is not None else None
        except (TypeError, ValueError):
            hb = None
        if hb is not None and hb < 1e12:
            hb *= 1000
    watch = None
    if isinstance(res, dict) and isinstance(res.get("operator"), dict):
        fresh = now - int(res.get("at") or 0) <= max(120, 4 * float(rcfg.get("interval") or 15)) * 1000
        watch = res["operator"].get("watch") if fresh else None
    stale_ms = float(rcfg.get("operator_stale_minutes") or OPERATOR_STALE_MINUTES) * 60000
    reasons = []
    if rcfg.get("serve_enabled"):
        if hb is not None and now - hb > stale_ms:
            reasons.append("heartbeat %d min old" % ((now - hb) // 60000))
        elif hb is None:
            reasons.append("no operator heartbeat (operator.json)")
        if watch is False:
            reasons.append("board-serve --watch is not running")
    return {"stale": bool(reasons) and pending_n > 0, "reasons": reasons, "heartbeat_at": hb, "watch": watch,
            "pending": pending_n}


# ------------------------------------------------------------------ board views

BOARD_SESSION_KEYS = ("code", "rss_mb", "cpu_pct", "tree_rss_mb", "classes", "teammates_n", "teammates_rss_mb",
                      "idle_teammates_n", "heavy_n")
BOARD_CHILD_KEYS = ("class", "comm", "rss_mb", "cpu_pct", "age_s", "heavy", "via")
BOARD_TM_KEYS = ("name", "rss_mb", "cpu_pct", "age_s", "idle_s", "idle")


def board_view(doc):
    """The `resources` block of /api/state: no pids and no paths. Disks are opaque ids (disk1, ...)
    with the labels of what lives there ($BOARD_HOME, session codes)."""
    if not isinstance(doc, dict):
        return None
    if doc.get("enabled") is False:
        return {"enabled": False, "at": doc.get("at"), "operator": doc.get("operator")}
    sessions = []
    for s in doc.get("sessions") or []:
        if not isinstance(s, dict):
            continue
        row = {k: s.get(k) for k in BOARD_SESSION_KEYS}
        row["children"] = [{k: c.get(k) for k in BOARD_CHILD_KEYS} for c in s.get("children") or [] if isinstance(c, dict)]
        row["teammates"] = [{k: t.get(k) for k in BOARD_TM_KEYS} for t in s.get("teammates") or [] if isinstance(t, dict)]
        sessions.append(row)
    disks = [{"id": d.get("id"), "total_mb": d.get("total_mb"), "free_mb": d.get("free_mb"),
              "free_pct": d.get("free_pct"), "level": d.get("level"), "unresponsive": bool(d.get("unresponsive")),
              "labels": [x for x in d.get("labels") or [] if isinstance(x, str)]}
             for d in doc.get("disks") or [] if isinstance(d, dict)]
    return {
        "enabled": True, "at": doc.get("at"), "platform": doc.get("platform"), "level": doc.get("level"),
        "worst": doc.get("worst"), "levels": doc.get("levels") or {}, "cpu": doc.get("cpu"), "mem": doc.get("mem"),
        "swap": doc.get("swap"), "oom_kills": doc.get("oom_kills"), "oom_recent": doc.get("oom_recent"),
        "last_oom_at": doc.get("last_oom_at"), "disks": disks, "sessions": sessions,
        "orphan_teammates": [{k: t.get(k) for k in BOARD_TM_KEYS} for t in doc.get("orphan_teammates") or [] if isinstance(t, dict)],
        "heavy_running": doc.get("heavy_running"), "unavailable": doc.get("unavailable") or [],
        "teammate_idle_minutes": doc.get("teammate_idle_minutes"), "operator": doc.get("operator"),
        "thresholds": doc.get("thresholds") if isinstance(doc.get("thresholds"), dict) else None,
    }


def _b5(v):
    return None if v is None else int(5 * round(float(v) / 5))


def _b100(v):
    return None if v is None else int(100 * round(float(v) / 100))


def mirror_view(doc):
    """meta/resources for the Artifact board: board_view() with percentages bucketed to 5% and
    sizes to 100 MB, so a mirror only differs when something meaningful moved."""
    v = board_view(doc)
    if not v or v.get("enabled") is False:
        return v
    v = json.loads(json.dumps(v))
    if v.get("cpu"):
        v["cpu"]["pct"] = _b5(v["cpu"].get("pct"))
    for k in ("available_pct",):
        if v.get("mem"):
            v["mem"][k] = _b5(v["mem"].get(k))
    if v.get("mem"):
        v["mem"]["available_mb"] = _b100(v["mem"].get("available_mb"))
    if v.get("swap"):
        v["swap"]["used_pct"] = _b5(v["swap"].get("used_pct"))
    for d in v.get("disks") or []:
        d["free_pct"], d["free_mb"] = _b5(d.get("free_pct")), _b100(d.get("free_mb"))
    for s in v.get("sessions") or []:
        for k in ("rss_mb", "tree_rss_mb", "teammates_rss_mb"):
            s[k] = _b100(s.get(k))
        s["cpu_pct"] = _b5(s.get("cpu_pct"))
        for c in (s.get("children") or []) + (s.get("teammates") or []):
            c["rss_mb"], c["cpu_pct"] = _b100(c.get("rss_mb")), _b5(c.get("cpu_pct"))
        for c in (s.get("classes") or {}).values():
            c["rss_mb"], c["cpu_pct"] = _b100(c.get("rss_mb")), _b5(c.get("cpu_pct"))
    for t in v.get("orphan_teammates") or []:
        t["rss_mb"], t["cpu_pct"] = _b100(t.get("rss_mb")), _b5(t.get("cpu_pct"))
    v["at"] = None if v.get("at") is None else int(v["at"]) // 60000 * 60000
    return v


def idle_teammates(doc):
    """[(code | None, [teammate rows idle past the threshold])], code None = orphaned."""
    out = []
    if not isinstance(doc, dict):
        return out
    for s in doc.get("sessions") or []:
        idle = [t for t in s.get("teammates") or [] if t.get("idle")]
        if idle:
            out.append((s.get("code"), idle))
    orphans = [t for t in doc.get("orphan_teammates") or [] if t.get("idle")]
    if orphans:
        out.append((None, orphans))
    return out


# ------------------------------------------------------------------ CLI

def fmt_mb(v):
    if v is None:
        return "?"
    return "%.1f GB" % (v / 1024.0) if v >= 1024 else "%d MB" % v


def fmt_age(s):
    if s is None:
        return "-"
    s = int(s)
    return "%ds" % s if s < 60 else "%dm" % (s // 60) if s < 3600 else "%dh%02dm" % (s // 3600, s % 3600 // 60)


def show_text(doc, now):
    S = fleet.safe_text
    if not isinstance(doc, dict):
        return "no resources.json yet (the collector samples every resources.interval seconds)"
    if doc.get("enabled") is False:
        w = (doc.get("operator") or {}).get("watch")
        return "resources: disabled (fleet.yaml resources.enabled: false)\noperator watch: %s" % (
            {True: "running", False: "not running"}.get(w, "unknown"))
    lines = ["level: %s%s   platform: %s   sampled %s ago" % (
        doc.get("level"), " (worst: %s)" % S(doc["worst"]) if doc.get("worst") else "", doc.get("platform"),
        fmt_age((now - int(doc.get("at") or now)) / 1000.0))]
    c = doc.get("cpu") or {}
    if c.get("load"):
        lines.append("cpu %s of %s · load %s (%s/cpu)" % ("%d%%" % c["pct"] if c.get("pct") is not None else "?",
                                                         c.get("ncpu"), " ".join("%.2f" % x for x in c["load"]),
                                                         c.get("load_per_cpu")))
    m, sw = doc.get("mem") or {}, doc.get("swap") or {}
    if m.get("total_mb"):
        lines.append("mem %s available of %s (%s%%)%s%s · oom kills %s" % (
            fmt_mb(m.get("available_mb")), fmt_mb(m.get("total_mb")), m.get("available_pct"),
            " · psi %.1f" % m["psi_some_avg60"] if m.get("psi_some_avg60") is not None else "",
            " · swap %s%% of %s" % (sw.get("used_pct"), fmt_mb(sw.get("total_mb"))) if sw.get("total_mb") else "",
            doc.get("oom_kills") if doc.get("oom_kills") is not None else "?"))
    for d in doc.get("disks") or []:
        used = ", ".join(S(x) for x in d.get("labels") or [])
        if d.get("unresponsive"):
            lines.append("%s  not responding (skipped for 10 min)  %s" % (S(d.get("id")), used))
            continue
        lines.append("%s %s  %s%% free (%s of %s) [%s]  %s" % (S(d.get("id")), S(d.get("mount")), d.get("free_pct"),
                                                          fmt_mb(d.get("free_mb")), fmt_mb(d.get("total_mb")),
                                                          d.get("level"), used))
    for e in doc.get("config_errors") or []:
        lines.append("config: %s" % S(e))
    if doc.get("unavailable"):
        lines.append("unavailable on this platform: %s" % ", ".join(doc["unavailable"]))
    w = (doc.get("operator") or {}).get("watch")
    lines.append("heavy jobs running: %s · operator watch: %s" % (doc.get("heavy_running"),
                                                                {True: "running", False: "not running"}.get(w, "unknown")))
    rows = [("SESSION", "RSS", "CPU", "CHILDREN", "TEAMMATES")]
    for s in doc.get("sessions") or []:
        kids = ", ".join("%s %s (%s)" % (S(c["class"]), S(c["comm"]), fmt_mb(c["rss_mb"]))
                         for c in s.get("children") or [] if c.get("class") not in ("other",))[:80] or "-"
        tm = "-"
        if s.get("teammates_n"):
            tm = "%d (%s%s)" % (s["teammates_n"], fmt_mb(s.get("teammates_rss_mb")),
                                ", %d idle" % s["idle_teammates_n"] if s.get("idle_teammates_n") else "")
        rows.append((S(s.get("code")), fmt_mb(s.get("rss_mb")), "%s%%" % s["cpu_pct"] if s.get("cpu_pct") is not None else "-", kids, tm))
    if len(rows) > 1:
        widths = [max(len(r[i]) for r in rows) for i in range(4)]
        lines.append("")
        lines.extend("  ".join(r[i].ljust(widths[i]) for i in range(4)) + "  " + r[4] for r in rows)
    for t in doc.get("orphan_teammates") or []:
        lines.append("orphaned teammate %s: %s, idle %s" % (S(t.get("name")), fmt_mb(t.get("rss_mb")), fmt_age(t.get("idle_s"))))
    idle = idle_teammates(doc)
    if idle:
        lines.append("")
        lines.append("idle > %d min (the board never stops them; ask the session to stop finished teammates):" % (
            int(doc.get("teammate_idle_minutes") or 30)))
        for code, ts in idle:
            lines.append("  %s: %s" % (S(code or "orphaned"), ", ".join("%s (%s, idle %s)" % (
                S(t["name"]), fmt_mb(t.get("rss_mb")), fmt_age(t.get("idle_s"))) for t in ts)))
    return "\n".join(lines)


def doctor(home, now):
    rcfg = settings()
    root = proc_root()
    out = []
    plat = detect_platform(root)
    if rcfg.get("enabled") is False:
        out.append("warn  resources disabled (fleet.yaml resources.enabled: false); the operator watch is still checked")
    if have_proc(root):
        out.append("ok    platform %s (reads %s)" % (plat, root))
    else:
        ok = scan_ps(now / 1000.0) is not None
        out.append("%s platform %s (ps%s)" % ("ok   " if ok else "FAIL ", plat, "" if ok else " not found"))
    for e in rcfg.get("config_errors") or []:
        out.append("FAIL  %s" % e)
    doc = read_json(paths(home)[0])
    if isinstance(doc, dict) and doc.get("enabled") is not False:
        miss = doc.get("unavailable") or []
        out.append(("warn  unavailable here: %s" % ", ".join(miss)) if miss else "ok    all metrics available")
        age = (now - int(doc.get("at") or 0)) / 1000.0
        out.append(("ok    resources.json sampled %s ago" if age <= 4 * rcfg["interval"] + 60
                    else "warn  resources.json is %s old (is the collector running?)") % fmt_age(age))
    else:
        out.append("warn  no resources.json yet")
    w = ((doc or {}).get("operator") or {}).get("watch") if isinstance(doc, dict) else None
    if rcfg.get("serve_enabled"):
        out.append({True: "ok    operator watch (board-serve --watch) running",
                    False: "warn  operator watch (board-serve --watch) not running"}.get(w, "warn  operator watch unknown"))
    return "\n".join(out)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    home = fleet.ensure_home()
    now = now_ms()
    if cmd == "show":
        doc = read_json(paths(home)[0])
        if "--refresh" in rest:
            snap = read_json(os.path.join(home, "snapshot.json")) or {}
            doc = preview(fleet.load_fleet(), home, [s for s in snap.get("sessions") or [] if isinstance(s, dict)], now)
        if "--mirror" in rest:
            print(json.dumps(mirror_view(doc), ensure_ascii=False, sort_keys=True))
        elif "--json" in rest:
            print(json.dumps(doc, ensure_ascii=False, indent=1))
        else:
            print(show_text(doc, now))
        return 0
    if cmd == "doctor":
        print(doctor(home, now))
        return 0
    sys.stderr.write("unknown command: %s\n" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
