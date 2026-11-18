#!/usr/bin/env python3
"""reconcile: settle a pane prompt's decision from what the session itself reported.

A permission prompt or an AskUserQuestion picker (a decision with a tool_use_id) ends in the
session with a PostToolUse (the tool ran: allowed, or the picker was submitted) or a
PostToolUseFailure (it was rejected, declined or failed). The hook records that outcome on its
events.jsonl line; the collector reads it here and closes the decision with what really happened
instead of leaving it `stale` ("not delivered"):

  - the board had recorded an answer and the session's actual answer equals it -> `relayed`,
    `delivered_via: "session"` (e.g. a press that typed every answer but stopped at Submit, then
    someone submitted it in the pane);
  - otherwise -> `answered_in_pane`, `resolved_via: "pane"`.

What the hook stores (docs/contract.md, "Session outcomes"): for a picker, per question the 1-based
number of the option whose label the answer equals, or for free text a salted hash of it (never
the text; nothing at all when the text looks like a secret). For a failure, `denied: true` only
when Claude Code's rejection wording is in the error (the error text is not stored).

CLI (for hooks/board-hook.sh):
  reconcile.py hook-outcome     hook JSON on stdin -> {"picks": [...], "text_sha": [...]} or nothing
"""
import hashlib
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

# How far back the collector looks (decisions and events), and how much of events.jsonl it reads
# on its first run (a bounded backfill) and on any one run.
WINDOW_MS = 7 * 24 * 3600 * 1000
BACKFILL_BYTES = 8 * 1024 * 1024
READ_MAX = 8 * 1024 * 1024
CURSOR_FILE = "reconcile-cursor.json"
# Statuses a session outcome may settle. relayed, timeout, unauthorized and answered_in_pane are
# never touched; stale is final for everything else.
SETTLEABLE = ("pending", "answered", "stale")
OUTCOME_EVENTS = ("PostToolUse", "PostToolUseFailure")
SPACE_RE = re.compile(r"[\s\u0085\u00a0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]+")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
MAX_Q = 6


def norm(s):
    """Whitespace-normalised text: NFC, control characters and runs of (Unicode) spaces become one
    space, trimmed."""
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFC", s)
    return SPACE_RE.sub(" ", CTRL_RE.sub(" ", s)).strip()


def text_sha(tool_use_id, text):
    """A free-text answer's fingerprint: salted with the tool_use_id (one prompt's hashes say
    nothing about another's), 128 bits."""
    h = hashlib.sha256(("%s\n%s" % (tool_use_id, norm(text))).encode("utf-8"))
    return h.hexdigest()[:32]


def _secret_like(text):
    try:
        import guard
        return guard.scrub(text) != text
    except Exception:  # noqa: BLE001 - no guard: be safe, store nothing
        return True


def _answers_of(payload):
    for k in ("tool_response", "tool_input"):
        v = payload.get(k)
        if isinstance(v, dict) and isinstance(v.get("answers"), dict) and isinstance(v.get("questions"), list):
            return v["questions"], v["answers"]
    return None, None


def hook_outcome(payload):
    """What a PostToolUse of an AskUserQuestion says about its answers, without the answer text."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PostToolUse" \
            or payload.get("tool_name") != "AskUserQuestion" or not payload.get("tool_use_id"):
        return None
    tu = str(payload["tool_use_id"])
    qs, answers = _answers_of(payload)
    if qs is None:
        return None
    by_norm = {norm(k): v for k, v in answers.items() if isinstance(k, str)}
    picks, shas = [], []
    for q in qs[:MAX_Q]:
        q = q if isinstance(q, dict) else {}
        a = answers.get(q.get("question"))
        if a is None:
            a = by_norm.get(norm(q.get("question")))
        a = norm(a)
        if not a:
            picks.append(None)
            shas.append(None)
            continue
        labels = [norm(o.get("label") if isinstance(o, dict) else o).casefold() for o in (q.get("options") or [])]
        if a.casefold() in labels:
            picks.append(labels.index(a.casefold()) + 1)
            shas.append(None)
        else:
            picks.append(None)
            shas.append(None if _secret_like(a) else text_sha(tu, a))
    return {"picks": picks, "text_sha": shas}


# ------------------------------------------------------------------ collector side
def read_outcomes(home, now):
    """Session outcomes (tool_use_id -> outcome) from events.jsonl lines not read before; the first
    run reads at most the last BACKFILL_BYTES. Returns (outcomes, save) where save() records how far
    it read (call it once the decisions are written)."""
    path = os.path.join(home, "events.jsonl")
    cpath = os.path.join(home, CURSOR_FILE)
    try:
        st = os.stat(path)
    except OSError:
        return {}, lambda: None
    try:
        with open(cpath, encoding="utf-8") as fh:
            cur = json.load(fh)
    except (OSError, ValueError):
        cur = None
    if isinstance(cur, dict) and cur.get("ino") == st.st_ino and isinstance(cur.get("off"), int) \
            and 0 <= cur["off"] <= st.st_size:
        off, backfill = cur["off"], False
    else:
        off, backfill = max(0, st.st_size - BACKFILL_BYTES), True
    out = {}
    end = off
    if st.st_size > off:
        with open(path, "rb") as fh:
            fh.seek(off)
            buf = fh.read(min(READ_MAX, st.st_size - off))
        cut = buf.rfind(b"\n")
        if cut >= 0:
            end = off + cut + 1
            lines = buf[:cut].split(b"\n")
            if backfill and off > 0:
                lines = lines[1:]  # a backfill starts mid-line
            for raw in lines:
                if b'"PostToolUse' not in raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or ev.get("type") != "hook" or ev.get("event") not in OUTCOME_EVENTS:
                    continue
                tu, ts = ev.get("tool_use_id"), ev.get("ts")
                if not isinstance(tu, str) or not tu or not isinstance(ts, int) or now - ts > WINDOW_MS:
                    continue
                o = {"ok": ev["event"] == "PostToolUse", "at": ts, "denied": ev.get("denied") is True}
                oc = ev.get("outcome")
                if isinstance(oc, dict) and isinstance(oc.get("picks"), list) and isinstance(oc.get("text_sha"), list):
                    o["picks"], o["text_sha"] = oc["picks"], oc["text_sha"]
                out[tu] = o

    def save():
        if end == (cur or {}).get("off") and st.st_ino == (cur or {}).get("ino"):
            return
        tmp = cpath + ".tmp.%d" % os.getpid()
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"ino": st.st_ino, "off": end}, fh)
            os.replace(tmp, cpath)
        except OSError:
            pass
    return out, save


def board_answers(d):
    """The board's recorded answer as a comparable value: "allow" / "deny" for a permission, for a
    question a list per question of ("pick", n) or ("text", text); None when nothing was recorded."""
    ans = d.get("answer")
    if ans is None or ans == "":
        return None
    if d.get("kind") == "permission":
        return ans if ans in ("allow", "deny") else None
    opts, texts = d.get("answer_options"), d.get("answer_texts")
    if isinstance(opts, list) and opts:
        texts = texts if isinstance(texts, list) and len(texts) == len(opts) else [None] * len(opts)
        out = []
        for o, t in zip(opts, texts):
            if isinstance(t, str) and t:
                out.append(("text", t))
            elif isinstance(o, int) and not isinstance(o, bool) and o >= 1:
                out.append(("pick", o))
            else:
                return None
        return out
    idx = d.get("answer_index")
    if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 1:
        return [("pick", idx)]
    return [("text", str(ans))]


def matches(d, outcome):
    """Did the session end with exactly the board's recorded answer?"""
    want = board_answers(d)
    if want is None:
        return False
    if d.get("kind") == "permission":
        got = "allow" if outcome.get("ok") else ("deny" if outcome.get("denied") else None)
        return got == want
    if not outcome.get("ok"):
        return False
    picks, shas = outcome.get("picks"), outcome.get("text_sha")
    if not isinstance(picks, list) or not isinstance(shas, list) or len(picks) != len(want) \
            or len(shas) != len(want):
        return False
    tu = str(d.get("tool_use_id") or "")
    for (kind, v), p, s in zip(want, picks, shas):
        if kind == "pick":
            if p != v:
                return False
        elif not isinstance(s, str) or s != text_sha(tu, v):
            return False
    return True


def settle(d, outcome):
    """Apply a session outcome to a settleable decision (in place). Returns True when changed."""
    if d.get("status") not in SETTLEABLE or not d.get("tool_use_id"):
        return False
    if not (d.get("kind") == "permission" or d.get("source") == "pane"):
        return False
    # A stale decision keeps why it had gone stale (its status_reason code, e.g. partial).
    if d.get("status") == "stale":
        d["reconciled_from"] = str(d.get("status_reason") or "stale")
    at = outcome.get("at")
    if matches(d, outcome):
        d["status"] = "relayed"
        d["relayed_at"] = d.get("relayed_at") or at
        d["delivered_via"] = "session"
    else:
        d["status"] = "answered_in_pane"
        d["resolved_via"] = "pane"
        d["resolved_by"] = d.get("from") or None
        d["resolved_at"] = at
    d.pop("status_reason", None)
    d["reconciled_at"] = at
    return True


def main(argv):
    if len(argv) >= 2 and argv[1] == "hook-outcome":
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            return 0
        o = hook_outcome(payload)
        if o is not None:
            sys.stdout.write(json.dumps(o, separators=(",", ":")))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
