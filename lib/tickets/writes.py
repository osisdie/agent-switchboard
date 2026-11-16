"""Ticket writes and take (docs/contract.md, "Tickets": writes, take, links, relay).

  comment / transition / assign   approvers only; the identity is checked before anything is sent.
                                  A comment is scrubbed by the secret guard and ends with the footer
                                  "(via agent-switchboard)" (tickets.comment_identity: true adds the
                                  board identity). Each write invalidates the cache and refetches.
  take(ref, session, by)          approver takes a ticket for a session:
                                  1. fresh fetch; 2. an active link -> 409 already_taken;
                                  3. `board task new --owner CODE --ticket prov:key "KEY: title"`;
                                  4. tickets/links/<prov>__<key>.json (status active);
                                  5. messages/<id>.json kind "ticket" (status new: `board-serve
                                     --watch` prints `ticket KEY taken`, the operator relays it);
                                  6. optional upstream writes from the tracker's `take:` (comment,
                                     transition to in-progress, assign), each audited; a failure
                                     gives status "partial" with the failed ops, no rollback.
  release(ref, by)                the link becomes released; the session gets a `released` relay.
  links(home, ref)                the link files for KEY or prov:KEY (`board ticket show`).

Nothing here takes an identity from a request body: callers pass the verified identity, and
`approver()` returns its configured spelling or raises 403.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time

import fleet  # noqa: E402
import guard  # noqa: E402

from . import KEY_RE, NAME_RE, TicketError, audit, cache, get, load, redact, redact_tree, tpl, warn

FOOTER = "(via agent-switchboard)"
COMMENT_MAX = 4000
VALUE_MAX = 80
CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
DONE_STAGES = ("maintain",)
DONE_STATUSES = ("done", "dropped")
# What a take authorizes (owner decision Q4). The relay carries it verbatim; the contract and the
# worker protocol spell out the same list.
AUTHORIZES = ("the delivery harness for this ticket up to and including opening the PR/MR: plan, design, "
              "build, tests, local review, push, open the PR, progress comments and in-progress on this "
              "ticket. NOT merge, production deploy, closing or cancelling the ticket, other tickets or "
              "repos, or any red line")


def now_ms():
    return int(time.time() * 1000)


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ who
def approver(by, cfg=None):
    """The configured spelling of an approver identity, else TicketError 403 (fails closed)."""
    cfg = cfg if cfg is not None else fleet.load_fleet()
    approvers = fleet.id_list(cfg, "approvers")
    if not approvers:
        raise TicketError("no_approvers", "fleet.yaml approvers is empty: nobody may write tickets")
    want = str(by or "").strip().lower()
    hit = next((a for a in approvers if a.strip().lower() == want), None) if want else None
    if hit is None:
        raise TicketError("not_approver", "only approvers write or take tickets")
    return hit


def checked(by, action, ref, via, home=None, cfg=None):
    """approver(), with the refusal audited (nothing else is written or sent)."""
    try:
        return approver(by, cfg)
    except TicketError as exc:
        audit(home or fleet.ensure_home(), {"action": action, "ref": str(ref)[:128], "by": str(by)[:200],
                                            "via": via, "result": "refused", "reason": exc.code})
        raise


def is_approver(ident, cfg=None):
    try:
        approver(ident, cfg)
        return True
    except TicketError:
        return False


def known_session(home, code):
    if not isinstance(code, str) or not CODE_RE.fullmatch(code):
        return False
    known = set()
    try:
        with open(os.path.join(home, "snapshot.json"), encoding="utf-8") as fh:
            snap = json.load(fh)
        known |= {str(s.get("code")) for s in snap.get("sessions") or [] if isinstance(s, dict)}
    except (OSError, ValueError, AttributeError):
        pass
    known |= {s["code"] for s in fleet.load_fleet()["sessions"]}
    return code in known


# ------------------------------------------------------------------ writes
def footer(by, cfg=None):
    cfg = cfg if cfg is not None else fleet.load_fleet()
    t = cfg.get("tickets") if isinstance(cfg.get("tickets"), dict) else {}
    if t.get("comment_identity") is True and by:
        return "(via agent-switchboard, by %s)" % by
    return FOOTER


def compose_comment(text, by, cfg=None):
    if not isinstance(text, str) or not text.strip():
        raise TicketError("bad_comment", "the comment is empty")
    if len(text) > COMMENT_MAX:
        raise TicketError("bad_comment", "the comment is over %d characters" % COMMENT_MAX)
    body = redact(fleet.CONTROL_RE.sub(lambda m: m.group(0) if m.group(0) in "\n\t" else " ", text.strip()))
    return "%s\n\n%s" % (body, footer(by, cfg))


def _value(v, what):
    if not isinstance(v, str) or not v.strip() or len(v) > VALUE_MAX or fleet.CONTROL_RE.search(v):
        raise TicketError("bad_body", "%s must be a short single-line string" % what)
    return v.strip()


def _after_write(home, prov, key, by, via):
    cache.Cache(home).invalidate(prov.name, key)
    try:
        return get("%s:%s" % (prov.name, key), refresh=True, by=by, via=via, home=home)
    except TicketError as exc:
        warn("refetch of %s:%s after a write failed: %s" % (prov.name, key, exc.code))
        return None


def _do(op, ref, value, by, via, home, registry, extra_ev=None):
    home = home or fleet.ensure_home()
    reg = registry or load()
    prov, key = reg.route(ref)
    ev = {"action": op, "provider": prov.name, "key": key, "by": by, "via": via}
    ev.update(extra_ev or {})
    try:
        fn = getattr(prov, op)
        upstream_ref = fn(key, value, home=home)
    except TicketError as exc:
        audit(home, dict(ev, result=exc.code))
        raise
    audit(home, dict(ev, result="ok", ref=upstream_ref))
    return {"status": "ok", "ref": upstream_ref, "ticket": _after_write(home, prov, key, by, via)}


def comment(ref, text, by, via="cli", home=None, registry=None, cfg=None):
    by = checked(by, "comment", ref, via, home, cfg)
    body = compose_comment(text, by, cfg)
    return _do("comment", ref, body, by, via, home, registry, {"body_sha256": sha256(body)})


def transition(ref, to, by, via="cli", home=None, registry=None, cfg=None):
    by = checked(by, "transition", ref, via, home, cfg)
    return _do("transition", ref, _value(to, "to"), by, via, home, registry, {"to": _value(to, "to")})


def assign(ref, who, by, via="cli", home=None, registry=None, cfg=None):
    by = checked(by, "assign", ref, via, home, cfg)
    return _do("assign", ref, _value(who, "who"), by, via, home, registry)


# ------------------------------------------------------------------ links
def links_dir(home):
    return os.path.join(home, "tickets", "links")


def link_path(home, prov, key):
    return os.path.join(links_dir(home), "%s__%s.json" % (cache.safe_part(prov), cache.safe_part(key)))


def _write_json(path, doc):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def links_lock(home, timeout_s=10.0):
    os.makedirs(links_dir(home), mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(links_dir(home), ".lock"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        end = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TicketError("take_failed", "another take is in progress; try again") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def effective(home, link):
    """An active link whose task reached maintain, or is done/dropped, becomes done (written back)."""
    if not isinstance(link, dict) or link.get("status") != "active":
        return link
    tid = str(link.get("task_id") or "")
    task = _read_json(os.path.join(home, "tasks", tid + ".json")) if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", tid) else None
    if isinstance(task, dict) and (task.get("stage") in DONE_STAGES or task.get("status") in DONE_STATUSES):
        link = dict(link, status="done", done_at=now_ms())
        _write_json(link_path(home, link["provider"], link["key"]), link)
    return link


def links(home, ref):
    """The link files for KEY (any tracker) or tracker:KEY, newest first. No tracker config needed."""
    ref = str(ref or "")
    prov, key = ref.split(":", 1) if ":" in ref else (None, ref)
    if not KEY_RE.fullmatch(key) or (prov is not None and not NAME_RE.fullmatch(prov)):
        raise TicketError("bad_ticket_ref", "a ticket ref is KEY or provider:KEY")
    out = []
    try:
        names = os.listdir(links_dir(home))
    except OSError:
        names = []
    suffix = "__%s.json" % cache.safe_part(key)
    for n in names:
        if n.endswith(suffix) and (prov is None or n == "%s%s" % (cache.safe_part(prov), suffix)):
            d = _read_json(os.path.join(links_dir(home), n))
            if d and d.get("key") == key:
                out.append(effective(home, d))
    return sorted(out, key=lambda d: -(d.get("taken_at") or 0))


# ------------------------------------------------------------------ take / release
def _task_new(home, session, prov, key, title, url):
    title = "%s: %s" % (key, title or "(no title)")
    argv = [os.path.join(fleet.REPO, "bin", "board"), "task", "new", "--owner", session,
            "--ticket", "%s:%s" % (prov, key)]
    if url:
        argv += ["--ticket-url", url]
    argv.append(title[:200])
    env = dict(os.environ, BOARD_HOME=home)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TicketError("take_failed", "could not create the task") from exc
    tid = p.stdout.strip()
    if p.returncode != 0 or not re.fullmatch(r"t-[0-9]+-[a-z0-9]+", tid):
        warn("board task new failed (exit %d): %s" % (p.returncode, p.stderr.strip()[:300]))
        raise TicketError("take_failed", "could not create the task")
    return tid


def _fill(template, ctx):
    try:
        return tpl.fill_text(template, ctx)
    except tpl.TemplateError:
        return None


def take(ref, session, by, via="cli", home=None, registry=None, cfg=None,
         comment_text=None, do_comment=True, do_transition=True, do_assign=True):
    """Take a ticket for a session. Returns {status: taken|partial, task_id, msg_id, failed, link, ticket}."""
    import messages  # noqa: WPS433 - lib/messages.py
    cfg = cfg if cfg is not None else fleet.load_fleet()
    home = home or fleet.ensure_home()
    by = checked(by, "take", ref, via, home, cfg)
    if not known_session(home, session):
        raise TicketError("unknown_session", "no such session")
    reg = registry or load(cfg)
    prov, key = reg.route(ref)
    if comment_text is not None:
        comment_text = compose_comment(comment_text, by, cfg)   # checked before anything is written
    t = get("%s:%s" % (prov.name, key), refresh=True, by=by, via=via, registry=reg, home=home)
    ev = {"provider": prov.name, "key": key, "by": by, "via": via, "session": session}
    with links_lock(home):
        cur = _read_json(link_path(home, prov.name, key))
        cur = effective(home, cur) if cur else None
        if cur and cur.get("status") == "active":
            audit(home, dict(ev, action="take", result="already_taken", task_id=cur.get("task_id")))
            raise TicketError("already_taken", "%s is already taken" % key,
                              extra={"task_id": cur.get("task_id"), "session": cur.get("session")})
        tid = _task_new(home, session, prov.name, key, t.get("title"), t.get("url"))
        mid = "m-%d-%s" % (now_ms(), secrets.token_hex(3))
        link = {"provider": prov.name, "key": key, "task_id": tid, "session": session, "taken_by": by,
                "taken_at": now_ms(), "status": "active", "msg_id": mid, "upstream": []}
        _write_json(link_path(home, prov.name, key), link)
    messages.create_ticket(home, mid, session, by, "taken", {
        "provider": prov.name, "key": key, "title": t.get("title"), "url": t.get("url")}, tid)
    # Upstream writes: best effort, each audited; the task and the link are the source of truth.
    spec = prov.take_spec()
    ctx = {"task_id": tid, "session": session, "key": key}
    ops = []
    if do_comment and "comment" in prov.capabilities:
        text = comment_text
        if text is None and spec.get("comment"):
            filled = _fill(spec["comment"], ctx)
            text = compose_comment(filled, by, cfg) if filled else None
        if text:
            ops.append(("comment", text))
    if do_transition and spec.get("transition") and "transition" in prov.capabilities:
        ops.append(("transition", spec["transition"]))
    if do_assign and spec.get("assign") and "assign" in prov.capabilities:
        ops.append(("assign", spec["assign"]))
    failed = []
    for op, value in ops:
        row = {"op": op, "at": now_ms()}
        oev = dict(ev, action=op, task_id=tid)
        if op == "comment":
            oev["body_sha256"] = sha256(value)
        try:
            row["ref"] = getattr(prov, op)(key, value, home=home)
            row["result"] = "ok"
        except TicketError as exc:
            row["result"] = exc.code
            failed.append(op)
        audit(home, dict(oev, result=row["result"]))
        link["upstream"].append(row)
    if ops:
        with links_lock(home):
            _write_json(link_path(home, prov.name, key), link)
        cache.Cache(home).invalidate(prov.name, key)
    status = "partial" if failed else "taken"
    audit(home, dict(ev, action="take", result=status, task_id=tid, msg_id=mid, failed=failed))
    return redact_tree({"status": status, "task_id": tid, "msg_id": mid, "failed": failed, "link": link, "ticket": t})


def release(ref, by, via="cli", home=None, cfg=None):
    """End an active take: the link becomes released and the session is told."""
    import messages  # noqa: WPS433
    cfg = cfg if cfg is not None else fleet.load_fleet()
    home = home or fleet.ensure_home()
    by = checked(by, "release", ref, via, home, cfg)
    rows = [d for d in links(home, ref) if d.get("status") == "active"]
    if not rows:
        raise TicketError("not_taken", "no active take for that ticket")
    if len(rows) > 1:
        raise TicketError("ticket_ambiguous", "several trackers have an active take; name one",
                          candidates=["%s:%s" % (d["provider"], d["key"]) for d in rows])
    with links_lock(home):
        link = _read_json(link_path(home, rows[0]["provider"], rows[0]["key"]))
        if not link or link.get("status") != "active":
            raise TicketError("not_taken", "no active take for that ticket")
        mid = "m-%d-%s" % (now_ms(), secrets.token_hex(3))
        link.update(status="released", released_by=by, released_at=now_ms(), release_msg_id=mid)
        _write_json(link_path(home, link["provider"], link["key"]), link)
    messages.create_ticket(home, mid, link["session"], by, "released",
                           {"provider": link["provider"], "key": link["key"], "title": None, "url": None},
                           link.get("task_id"))
    audit(home, {"action": "release", "provider": link["provider"], "key": link["key"], "by": by, "via": via,
                 "result": "ok", "task_id": link.get("task_id"), "session": link.get("session"), "msg_id": mid})
    return {"status": "released", "msg_id": mid, "link": link}


def defang(text, limit=200):
    """Untrusted tracker text inside a relay line: one line, no brackets (so it can never look
    like a [BOARD ...] header), no double quotes, clipped."""
    t = guard.scrub(fleet.CONTROL_RE.sub(" ", str(text or "")))
    t = t.replace("[", "(").replace("]", ")").replace('"', "'")
    return re.sub(r"\s+", " ", t).strip()[:limit]
