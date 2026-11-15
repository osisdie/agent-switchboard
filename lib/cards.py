#!/usr/bin/env python3
"""Card extras: $BOARD_HOME/cards.json, what a session card shows beyond the snapshot
(docs/contract.md, "Card extras"). Written by the collector on every run (lib/collect.py), read
only by board-serve. Never mirrored to the Artifact, never in alerts, never in snapshot.json, so
raw numbers (RSS, cost, context %) do not wake the operator's Monitor.

Per session code:
  teammates    [{name, title, busy, idle, idle_s, rss_mb}]: the teammate processes resources.json
               maps to this session, each with its tmux pane title (the task summary Claude Code
               sets, cleaned like a card title) and whether the title glyph says it is working.
  meter        {ctx_pct, cost_usd, model, at} from $BOARD_HOME/statusline/<sessionId>.json (the
               statusline tap writes only those fields).
  done         the hook's ring of the last 8 completed tool calls [{tool, summary, ms, ok, at}].
  doing        the last assistant text in the session's transcript (one line, <= 280 chars).
  prompt       the first line of the last user prompt (<= 120 chars); only with
               fleet.yaml cards.show_prompt (default true).
  branch       {base, ahead, behind, subject}: against origin/<default> as the local refs know
               it (no fetch), and the last commit subject.
  state_since  epoch ms when the card entered its current state (as the collector saw it).

`doing` and `prompt` are approver-only: board-serve drops them for everyone else. Every string
goes through guard.scrub and the collector's clean_text (no paths, no control characters).

Cost per collector run: one stat per live session for its statusline file and its transcript
(the path is cached; a lookup is a glob, at most LOOKUPS_PER_RUN), a 64 KB tail read only for a
transcript whose mtime or size changed (at most TRANSCRIPTS_PER_RUN of them), a few /proc stat reads per teammate, and git only when a
cwd's cached branch info is older than GIT_TTL_MS. The cache lives in cards-state.json.

Test hooks: CLAUDE_PROJECTS_DIR (where transcripts are; default `projects` next to the session
registry, so a test's CLAUDE_SESSIONS_DIR never lets it read ~/.claude/projects), BOARD_PROC_ROOT
(fixture /proc).
"""
import glob
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402
import guard  # noqa: E402

TAIL_BYTES = 64 * 1024
TRANSCRIPTS_PER_RUN = 8   # tail reads per collector run
LOOKUPS_PER_RUN = 8       # transcript path lookups (a glob) per collector run
GIT_TTL_MS = 60 * 1000
DOING_MAX = 280
PROMPT_MAX = 120
SUBJECT_MAX = 100
DONE_SUMMARY_MAX = 120
DONE_MAX = 8
METER_MAX_AGE_MS = 24 * 3600 * 1000
SID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")
# A teammate's pane title starts with a status glyph: a braille spinner while it works, ✳ at rest.
SPINNER_RE = re.compile(r"^[\u2800-\u28ff]")
MARKDOWN_RE = re.compile(r"\*\*|__|`+|^#+\s*", re.M)  # one plain line: emphasis, code ticks, headings
IDLE_GLYPH = "\u2733"
MISS_RETRY_MS = 60 * 1000  # a session with no transcript found is looked up again after this
# A wrapper Claude Code puts around a prompt that came from a teammate message.
WRAPPED_RE = re.compile(r"^\s*<(teammate-message|task-notification)\b[^>]*>\s*", re.S)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_private(path, data):
    """Atomic write, 0600."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def settings(cfg):
    c = cfg.get("cards") if isinstance(cfg.get("cards"), dict) else {}
    return {"show_prompt": c.get("show_prompt", True) is not False}


def safe_text(text, limit, clean):
    """One line of untrusted text for a card: scrubbed (guard), then clean_text (paths, control
    characters, the hook's redaction), at most `limit` characters. None when nothing is left."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = " ".join(text[:4000].split())
    return clean(guard.scrub(text), limit) or None


# ------------------------------------------------------------------ teammates

def proc_root():
    return os.environ.get("BOARD_PROC_ROOT") or "/proc"


def ppid_of(root, pid):
    try:
        with open(os.path.join(root, str(pid), "stat"), encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
        return int(txt[txt.rfind(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def pane_of(pid, pane_by_pid, root, depth=6):
    """The tmux pane a process runs in: itself or one of its first `depth` ancestors is a pane's
    process (tmux runs a teammate as `sh -c '... claude ...'` or execs it)."""
    p = pid
    for _ in range(depth):
        if p in pane_by_pid:
            return pane_by_pid[p]
        p = ppid_of(root, p)
        if not p or p <= 1:
            return None
    return None


def teammates(code, res, panes, clean_title, hosts):
    """Teammate rows for one session: resources.json's teammates plus their pane titles."""
    rows = []
    srow = next((s for s in (res or {}).get("sessions") or [] if isinstance(s, dict) and s.get("code") == code), None)
    if not srow:
        return rows
    pane_by_pid = {pid: pane for pane, (_t, pid) in panes.items() if isinstance(pid, int)}
    root = proc_root()
    for t in srow.get("teammates") or []:
        if not isinstance(t, dict):
            continue
        pane = pane_of(t.get("pid"), pane_by_pid, root) if isinstance(t.get("pid"), int) else None
        raw = panes.get(pane, ("", None))[0] if pane else ""
        name = str(t.get("name") or "?")[:64]
        title = clean_title(raw, hosts, names=[name]) or None
        glyph = raw.strip()[:1]
        busy = True if SPINNER_RE.match(glyph) else False if glyph == IDLE_GLYPH else None
        rows.append({"name": name, "title": title, "busy": busy, "idle": bool(t.get("idle")),
                     "idle_s": t.get("idle_s"), "rss_mb": t.get("rss_mb")})
    rows.sort(key=lambda r: r["name"])
    return rows


# ------------------------------------------------------------------ meter

def meter(home, sid, now):
    if not sid or not SID_RE.match(sid):
        return None
    m = read_json(os.path.join(home, "statusline", sid + ".json"))
    if not isinstance(m, dict):
        return None
    at = m.get("at") if isinstance(m.get("at"), int) else None
    if not at or now - at > METER_MAX_AGE_MS:
        return None

    def num(v, lo, hi):
        return round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi else None
    model = m.get("model")
    model = re.sub(r"[^\w .()\[\]+-]", "", model)[:40].strip() or None if isinstance(model, str) else None
    out = {"ctx_pct": num(m.get("ctx_pct"), 0, 100), "cost_usd": num(m.get("cost_usd"), 0, 1e6),
           "model": model, "at": at}
    return out if any(out[k] is not None for k in ("ctx_pct", "cost_usd", "model")) else None


# ------------------------------------------------------------------ recent activity

def done_ring(st, clean):
    out = []
    for r in (st.get("recent_done") or [])[-DONE_MAX:] if isinstance(st, dict) else []:
        if not isinstance(r, dict) or not r.get("tool"):
            continue
        out.append({"tool": clean(str(r["tool"]), 40) or "?",
                    "summary": safe_text(r.get("summary"), DONE_SUMMARY_MAX, clean),
                    "ms": r["ms"] if isinstance(r.get("ms"), int) and r["ms"] >= 0 else None,
                    "ok": r.get("ok") is not False,
                    "at": r["at"] if isinstance(r.get("at"), int) else None})
    return out


# ------------------------------------------------------------------ transcripts

def projects_dir():
    """CLAUDE_PROJECTS_DIR, else `projects` next to the session registry (fleet.sessions_dir():
    ~/.claude/projects, or a test's own directory when CLAUDE_SESSIONS_DIR points there)."""
    if os.environ.get("CLAUDE_PROJECTS_DIR"):
        return os.environ["CLAUDE_PROJECTS_DIR"]
    return os.path.join(os.path.dirname(os.path.normpath(fleet.sessions_dir())), "projects")


def direct_path(sid, cwd):
    """Where Claude Code keeps a session's transcript: projects/<cwd, every character other than a
    letter or digit as "-">/<sessionId>.jsonl. One stat, no glob."""
    if not sid or not SID_RE.match(sid) or not isinstance(cwd, str) or not cwd:
        return None
    return os.path.join(projects_dir(), re.sub(r"[^A-Za-z0-9]", "-", cwd), sid + ".jsonl")


def find_transcript(sid):
    """Any project directory: a session whose cwd changed since it started."""
    if not sid or not SID_RE.match(sid):
        return None
    hits = glob.glob(os.path.join(glob.escape(projects_dir()), "*", glob.escape(sid) + ".jsonl"))
    if not hits:
        return None
    return max(hits, key=lambda p: os.stat(p).st_mtime_ns if os.path.exists(p) else 0)


def read_tail(path, size):
    with open(path, "rb") as fh:
        start = max(0, size - TAIL_BYTES)
        fh.seek(start)
        data = fh.read(TAIL_BYTES)
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line is cut
    return lines


def _texts(content):
    if isinstance(content, str):
        return [content]
    out = []
    for c in content or [] if isinstance(content, list) else []:
        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
            out.append(c["text"])
    return out


def prompt_text(entry):
    """The text a person (or the lead, through a teammate message) typed, else None."""
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return None
    msg = entry.get("message") or {}
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return None
    text = "\n".join(_texts(content)).strip()
    m = WRAPPED_RE.match(text)
    if m:
        text = text[m.end():]
    elif text.startswith("<") or text.startswith("[Request interrupted"):
        return None  # command output, a system reminder, an interruption marker
    return text or None


def scan_tail(lines):
    """(last assistant text, last prompt) in these transcript lines; either may be None."""
    doing = prompt = None
    for raw in reversed(lines):
        if doing is not None and prompt is not None:
            break
        if not raw.strip():
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if doing is None and e.get("type") == "assistant":
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            t = "\n".join(x for x in _texts(msg.get("content")) if x.strip()).strip()
            if t:
                doing = t
        elif prompt is None and e.get("type") == "user":
            prompt = prompt_text(e)
    return doing, prompt


def transcript_text(sid, cwd, cache, budget, cfg_cards, clean, now):
    """{doing, prompt} for a session from its transcript tail, cached by (path, mtime, size).
    `budget`: {"read": tails, "find": path lookups} this run may still do."""
    ent = cache.get(sid) if isinstance(cache.get(sid), dict) else {}
    path = ent.get("path")
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    if st is None:
        path = direct_path(sid, cwd)
        try:
            st = os.stat(path) if path else None
        except OSError:
            st = None
    if st is None:
        miss = ent.get("miss_at")
        if budget["find"] <= 0 or (isinstance(miss, int) and 0 <= now - miss < MISS_RETRY_MS):
            return ent
        budget["find"] -= 1
        path = find_transcript(sid)
        if not path:
            cache[sid] = {"path": None, "miss_at": now, "doing": ent.get("doing"), "prompt": ent.get("prompt")}
            return cache[sid]
        try:
            st = os.stat(path)
        except OSError:
            return {}
    key = [st.st_mtime_ns, st.st_size]
    if path == ent.get("path") and ent.get("key") == key:
        return ent
    if budget["read"] <= 0:
        return ent  # changed, but this run already read enough: next run
    budget["read"] -= 1
    try:
        doing, prompt = scan_tail(read_tail(path, st.st_size))
    except OSError:
        return ent
    new = {"path": path, "key": key,
           "doing": safe_text(MARKDOWN_RE.sub("", doing), DOING_MAX, clean) if doing else ent.get("doing"),
           "prompt": None}
    if cfg_cards["show_prompt"]:
        line = prompt.strip().splitlines()[0] if prompt and prompt.strip() else None
        new["prompt"] = safe_text(line, PROMPT_MAX, clean) if line else ent.get("prompt")
    cache[sid] = new
    return new


# ------------------------------------------------------------------ branch

def _git(cwd, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, timeout=3,
                           check=False, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def branch_info(cwd, clean):
    """{base, ahead, behind, subject} from local refs only (never a fetch); None outside a repo."""
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return None
    subject = _git(cwd, "log", "-1", "--format=%s", "HEAD")
    if subject is None:
        return None
    base = _git(cwd, "rev-parse", "--abbrev-ref", "origin/HEAD")
    bases = [base] if base and base != "origin/HEAD" else ["origin/main", "origin/master"]
    ahead = behind = None
    used = None
    for b in bases:
        out = _git(cwd, "rev-list", "--left-right", "--count", "HEAD..." + b)
        if out:
            parts = out.split()
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                ahead, behind, used = int(parts[0]), int(parts[1]), b
                break
    return {"base": clean(used, 60) if used else None, "ahead": ahead, "behind": behind,
            "subject": safe_text(subject, SUBJECT_MAX, clean)}


def branches(rows, cache, now, clean):
    out = {}
    seen = set()
    for r in rows:
        cwd = r.get("cwd")
        if not r.get("pid") or not isinstance(cwd, str) or cwd in seen:
            continue
        seen.add(cwd)
        hit = cache.get(cwd)
        if not (isinstance(hit, dict) and isinstance(hit.get("at"), int) and 0 <= now - hit["at"] < GIT_TTL_MS):
            hit = {"at": now, "info": branch_info(cwd, clean)}
        out[cwd] = hit
    return out


# ------------------------------------------------------------------ entry point

def step(cfg, home, rows, now, panes, clean, clean_title, hosts):
    """Build and write cards.json for the snapshot's session rows. Returns the document."""
    cfg_cards = settings(cfg)
    spath = os.path.join(home, "cards-state.json")
    state = read_json(spath)
    state = state if isinstance(state, dict) else {}
    tcache = state.get("transcripts") if isinstance(state.get("transcripts"), dict) else {}
    since = state.get("since") if isinstance(state.get("since"), dict) else {}
    gcache = state.get("git") if isinstance(state.get("git"), dict) else {}
    res = read_json(os.path.join(home, "resources.json"))
    res = res if isinstance(res, dict) else {}
    gcache = branches(rows, gcache, now, clean)
    budget = {"read": TRANSCRIPTS_PER_RUN, "find": LOOKUPS_PER_RUN}
    # The busiest cards first, so a cap never starves the one that changed.
    live = sorted((r for r in rows if r.get("pid")), key=lambda r: -int(r.get("updated_at") or 0))
    cards, new_since, live_sids = {}, {}, set()
    for r in rows:
        code = r["code"]
        prev = since.get(code) if isinstance(since.get(code), dict) else {}
        s_at = prev.get("at") if prev.get("state") == r.get("state") and isinstance(prev.get("at"), int) else now
        new_since[code] = {"state": r.get("state"), "at": s_at}
        cards[code] = {"state_since": s_at}
    for r in live:
        code, sid = r["code"], r.get("session_id")
        live_sids.add(sid)
        st = read_json(os.path.join(home, "state", "%s.json" % sid)) if sid and SID_RE.match(sid) else None
        tx = transcript_text(sid, r.get("cwd"), tcache, budget, cfg_cards, clean, now) if sid else {}
        g = gcache.get(r.get("cwd")) or {}
        cards[code].update({
            "teammates": teammates(code, res, panes, clean_title, hosts),
            "meter": meter(home, sid, now),
            "done": done_ring(st or {}, clean),
            "doing": tx.get("doing"),
            "prompt": tx.get("prompt") if cfg_cards["show_prompt"] else None,
            "branch": g.get("info"),
        })
    if not cfg_cards["show_prompt"]:  # switched off: no prompt stays in the cache either
        for v in tcache.values():
            if isinstance(v, dict):
                v.pop("prompt", None)
    state = {"transcripts": {k: v for k, v in tcache.items() if k in live_sids},
             "since": new_since, "git": gcache}
    if state != read_json(spath):  # the same document most runs: no write
        write_private(spath, state)
    doc = {"sessions": cards}
    path = os.path.join(home, "cards.json")
    old = read_json(path)
    if old != doc:
        write_private(path, doc)
    return doc
