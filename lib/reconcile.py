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
  - otherwise -> `answered_in_pane`, `resolved_via: "pane"`, with `reconcile_result` saying
    whether the session provably got something else ("differs") or the report cannot tell
    ("unknown"), or the board had no answer ("no_answer").

What the hook stores (docs/contract.md, "Session outcomes"): for a picker, per question the 1-based
number of the option whose label the answer equals, or for free text an HMAC of it under this
install's key ($BOARD_HOME/secrets/reconcile.key, 0600; never the text, and nothing at all for text
that looks like a secret, an email address or a phone number). For a failure, `denied: true` only
when Claude Code's rejection wording is in the error (the error text is not stored).

CLI (for hooks/board-hook.sh):
  reconcile.py hook-outcome     hook JSON on stdin -> {"picks": [...], "text_sha": [...]} or nothing
"""
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
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
KEY_FILE = os.path.join("secrets", "reconcile.key")
KEY_BYTES = 32
# A phone number: 8+ digits, optionally with + ( ) spaces dots or dashes between them. Dates and
# other long numbers match too: such text gets no hash (its slot compares as "unknown").
PHONE_RE = re.compile(r"(?<![\w.])\+?\(?\d(?:[\s().-]*\d){7,}(?![\w.])")
# reconcile_result codes (the page words them)
RESULTS = ("match", "differs", "unknown", "no_answer")
INCONSISTENT = "inconsistent"
# A decision answered this recently whose stored option fields do not fit its answer is left for
# the next run (a writer may be mid-way); after that it settles as "unknown".
INCONSISTENT_GRACE_MS = 60 * 1000


def norm(s):
    """Whitespace-normalised text: NFC, control characters and runs of (Unicode) spaces become one
    space, trimmed."""
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFC", s)
    return SPACE_RE.sub(" ", CTRL_RE.sub(" ", s)).strip()


def board_home():
    return os.environ.get("BOARD_HOME") or os.path.join(os.path.expanduser("~"), ".agent-switchboard")


def hash_key(home, create=True):
    """This install's key for free-text hashes ($BOARD_HOME/secrets/reconcile.key: 32 random bytes,
    0600, made on first use; never served, mirrored or logged). None when it cannot be used safely
    (a symlink, another owner, readable by others, the wrong size): then nothing is hashed. Every
    outcome with a hash carries the id of the key that made it (key_id), so a replaced key (deleted,
    restored, or lost in a creation race) makes older hashes compare as "unknown", never "differs"."""
    path = os.path.join(home, KEY_FILE)
    if create and not os.path.lexists(path):
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            tmp = "%s.tmp.%d.%s" % (path, os.getpid(), secrets.token_hex(4))
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(secrets.token_bytes(KEY_BYTES))
                    fh.flush()
                    os.fsync(fh.fileno())
                try:
                    os.link(tmp, path)  # another writer won: theirs is kept
                except FileExistsError:
                    pass
                except OSError as exc:
                    # a filesystem without hard links only: replace, then use whatever is there now
                    if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
                        raise
                    os.replace(tmp, path)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except OSError:
            return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
            return None
        data = fh.read(KEY_BYTES + 1)
    return data if len(data) == KEY_BYTES else None


def key_id(key):
    """A short id of a hash key (not the key): which key made an outcome's hashes."""
    return hmac.new(key, b"reconcile-key-id", hashlib.sha256).hexdigest()[:8]


def text_sha(key, tool_use_id, text):
    """A free-text answer's fingerprint: HMAC-SHA256 under this install's key, over the
    tool_use_id and the normalised text, first 128 bits. Without the key (it never leaves
    $BOARD_HOME) a short answer cannot be guessed from its hash."""
    msg = ("%s\n%s" % (tool_use_id, norm(text))).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def _unhashable(text):
    """Free text that gets no hash at all: what the secret guard would change, and anything that
    looks like an email address or a phone number."""
    try:
        import guard
        import pii
        return guard.scrub(text) != text or bool(pii.TEXT_EMAIL_RE.search(text)) or bool(PHONE_RE.search(text))
    except Exception:  # noqa: BLE001 - no guard: be safe, store nothing
        return True


def label_pick(answer, options):
    """The 1-based option an answer names: its exact (whitespace-normalised) label first, else a
    case-insensitive match when exactly one label fits; None for free text."""
    labels = [norm(o.get("label") if isinstance(o, dict) else o) for o in (options or [])]
    if answer in labels:
        return labels.index(answer) + 1
    ci = [i for i, lb in enumerate(labels) if lb.casefold() == answer.casefold()]
    return ci[0] + 1 if len(ci) == 1 else None


def _answers_of(payload):
    for k in ("tool_response", "tool_input"):
        v = payload.get(k)
        if isinstance(v, dict) and isinstance(v.get("answers"), dict) and isinstance(v.get("questions"), list):
            return v["questions"], v["answers"]
    return None, None


def hook_outcome(payload, home=None):
    """What a PostToolUse of an AskUserQuestion says about its answers, without the answer text."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PostToolUse" \
            or payload.get("tool_name") != "AskUserQuestion" or not payload.get("tool_use_id"):
        return None
    tu = str(payload["tool_use_id"])
    qs, answers = _answers_of(payload)
    if qs is None:
        return None
    by_norm = {norm(k): v for k, v in answers.items() if isinstance(k, str)}
    key = None
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
        n = label_pick(a, q.get("options"))
        if n is not None:
            picks.append(n)
            shas.append(None)
            continue
        picks.append(None)
        if _unhashable(a):
            shas.append(None)
            continue
        key = key or hash_key(home or board_home())
        shas.append(text_sha(key, tu, a) if key else None)
    out = {"picks": picks, "text_sha": shas}
    if key and any(shas):
        out["kid"] = key_id(key)
    return out


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
                    if isinstance(oc.get("kid"), str):
                        o["kid"] = oc["kid"]
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


def _form_answer_text(qs, opts, texts):
    """The answer text `board answer --options` / `--form-file` composes for a form: "Header: label"
    per question (the text for a free-text row), joined by " · "."""
    parts = []
    for i, q in enumerate(qs):
        h = str(q.get("header") or "").strip() or "Q%d" % (i + 1)
        if texts[i] is not None:
            v = texts[i]
        else:
            v = str(q["options"][opts[i] - 1].get("label") if isinstance(q["options"][opts[i] - 1], dict)
                    else q["options"][opts[i] - 1])
        parts.append("%s: %s" % (h, v))
    return " · ".join(parts)


def board_answers(d):
    """The board's recorded answer as a comparable value: "allow" / "deny" for a permission, for a
    question a list per question of ("pick", n) or ("text", text); None when nothing was recorded;
    INCONSISTENT when the stored option fields do not fit the answer (another write in progress).

    A form of several questions is told by its stored fields, as bin/board writes them:
    `answer_options` one number per question (a free-text row is that question's option count + 1),
    `answer_texts` null or one entry per question (the text, or null for an option), and `answer`
    the composed "Header: label · Header: text" (checked when it can be recomposed)."""
    ans = d.get("answer")
    if ans is None or ans == "":
        return None
    if d.get("kind") == "permission":
        return ans if ans in ("allow", "deny") else INCONSISTENT
    qs = d.get("questions") if isinstance(d.get("questions"), list) else []
    qs = [q if isinstance(q, dict) else {} for q in qs]
    opts, texts = d.get("answer_options"), d.get("answer_texts")
    if opts is not None:
        if not isinstance(opts, list) or len(opts) < 2 or len(opts) != len(qs):
            return INCONSISTENT
        if texts is None:
            texts = [None] * len(opts)
        if not isinstance(texts, list) or len(texts) != len(opts):
            return INCONSISTENT
        out = []
        for q, o, t in zip(qs, opts, texts):
            n = len(q.get("options") or [])
            if not isinstance(o, int) or isinstance(o, bool):
                return INCONSISTENT
            if t is not None:
                if not isinstance(t, str) or not t or o != n + 1:
                    return INCONSISTENT
                out.append(("text", t))
            elif 1 <= o <= n:
                out.append(("pick", o))
            else:
                return INCONSISTENT
        composed = _form_answer_text(qs, opts, texts)
        if norm(str(ans)) not in (norm(composed), norm(_scrub(composed))):
            return INCONSISTENT  # the options of another answer
        return out
    idx = d.get("answer_index")
    q0 = qs[0].get("options") if qs else None
    want = label_pick(norm(str(ans)), q0) if q0 is not None else None
    if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 1:
        return [("pick", idx)] if q0 is None or want == idx else INCONSISTENT
    if want is not None and len(qs) <= 1:
        return INCONSISTENT  # the answer is a label but no option number was stored with it
    return [("text", str(ans))]


def _scrub(text):
    try:
        import guard
        return guard.scrub(text)
    except Exception:  # noqa: BLE001
        return text


def compare(d, outcome, key):
    """"match" (the session got exactly the board's answer), "differs" (it provably got something
    else), "unknown" (the report cannot tell), "no_answer" (the board recorded none), or
    INCONSISTENT."""
    want = board_answers(d)
    if want is None:
        return "no_answer"
    if want == INCONSISTENT:
        return INCONSISTENT
    if d.get("kind") == "permission":
        got = "allow" if outcome.get("ok") else ("deny" if outcome.get("denied") else None)
        if got is None:
            return "unknown"  # a failure that is not a rejection: allowed and failed, or not
        return "match" if got == want else "differs"
    if not outcome.get("ok"):
        return "unknown"
    picks, shas = outcome.get("picks"), outcome.get("text_sha")
    if not isinstance(picks, list) or not isinstance(shas, list) or len(picks) != len(want) \
            or len(shas) != len(want):
        return "unknown"
    tu = str(d.get("tool_use_id") or "")
    result = "match"
    for (kind, v), p, sh in zip(want, picks, shas):
        p = p if isinstance(p, int) and not isinstance(p, bool) else None
        sh = sh if isinstance(sh, str) and sh else None
        if kind == "pick":
            if p is not None:
                if p != v:
                    return "differs"
            elif sh is not None:
                return "differs"  # free text where the board picked an option
            else:
                result = "unknown"
        else:
            if p is not None:
                return "differs"  # an option where the board typed text
            if sh is None or key is None or outcome.get("kid") != key_id(key):
                # no hash, no key, or hashed under another key (replaced since): cannot tell
                result = "unknown"
            elif sh != text_sha(key, tu, v):
                return "differs"
    return result


def settle(d, outcome, now=None, home=None):
    """Apply a session outcome to a settleable decision (in place). Returns "settled", "retry"
    (not now: its stored answer fields are mid-write) or None (nothing to do)."""
    if d.get("status") not in SETTLEABLE or not d.get("tool_use_id"):
        return None
    if not (d.get("kind") == "permission" or d.get("source") == "pane"):
        return None
    key = hash_key(home or board_home(), create=False) if d.get("kind") != "permission" else None
    result = compare(d, outcome, key)
    if result == INCONSISTENT:
        # A write time ahead of our clock (skew, a hand-edited file) never keeps it waiting: it
        # counts as old, so the decision settles as unknown instead of retrying forever.
        written = int(d.get("updated_at") or d.get("answered_at") or 0)
        if now is not None and written <= now and now - written < INCONSISTENT_GRACE_MS:
            return "retry"
        result = "unknown"
    # A stale decision keeps why it had gone stale (its status_reason code, e.g. partial).
    if d.get("status") == "stale":
        d["reconciled_from"] = str(d.get("status_reason") or "stale")
    at = outcome.get("at")
    d["reconcile_result"] = result
    if result == "match":
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
    return "settled"


def main(argv):
    if len(argv) >= 2 and argv[1] == "hook-outcome":
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            return 0
        o = hook_outcome(payload, board_home())
        if o is not None:
            sys.stdout.write(json.dumps(o, separators=(",", ":")))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
