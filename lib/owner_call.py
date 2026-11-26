#!/usr/bin/env python3
"""owner_call: a board message that needs the OWNER becomes a pending owner decision.

When the operator triages a message (usually an asker's) and finds that it is the owner's call
("please review and merge", "please decide X", or a routed session answering "passed to the
owner"), a pane note and a notification are not enough: they leave nothing open. `board message
owner-call <id>` (create below) instead:

  - creates a pending decision of kind "owner_call" linked to the message (`message_id`), `from`
    the asker (their label, never a session code: it is never relayed as a [BOARD decision]), with
    options (default OPTIONS). The operator's note and the asker's words are kept apart
    (request.note, request.excerpt) and labelled in `context`: the asker's words come last, marked
    untrusted, inside «…» with their own quotes neutralised. It shows in the board's decisions list
    like any board ask, and alerts the owner through the usual decision alert (lib/notify.py
    decision_alert: warn, escalating until acked; a production gate urgent; never the asker's label
    or words). It never times out or goes stale (lib/collect.py skips it; `board mark` refuses it):
    it waits, escalating, until it is answered;
  - sets the message to status "awaiting_owner" (`owner_call: {decision, at, by, away}`): never a
    silent end state while the decision is open;
  - writes a thread note from BOARD ("Waiting for the owner's decision <id>") so the sender sees
    where it waits. While the owner is away (lib/away.py), the note says so and that it is queued,
    with the next step under the away rules.

Owner away: the decision follows the existing rules for board asks. Committee advice
(lib/advice.py) asks the decision's `advisor` session (--advisor, else the session the message was
sent or routed to) for a recommendation, as it asks the asking session of a board ask; a committee
decision (lib/committee.py) happens only when the owner opted in, with the advisor standing in for
the asking session, and a red line (--red-line) or a production gate (--gate production) only when
the owner also let the committee decide red lines. Without an advisor session the committee never
decides it: it waits for the owner. The committee decides only by an OPTION on the decision's
`committee_options` allowlist (default COMMITTEE_OPTIONS, never a decline; with custom --option
only what --committee-option names, else nothing): a free-text recommendation never decides an
owner's call (lib/committee.py evaluate: owner_call_needs_option, owner_call_option_not_allowed;
DECLINE_RE is only a backstop). Declining stays the owner's.

Answers (apply, run by board-serve's sweep and by --watch): an answered owner_call (by an approver
on the board, or by the committee) is relayed to the THREAD, never to a session: a note from BOARD
with the answer, the message becomes "replied", the decision "relayed". A committee decision's note
says the sessions decided on the owner's behalf and that the owner follows up when back. The
operator never relays, answers or marks an owner_call decision.

CLI (bin/board message owner-call ...):
  messages.py owner-call MSG_ID --by WHO [--option X]... [--note-file F] [--advisor CODE]
                         [--gate production] [--red-line]
                                  prints the decision id (an open one for the message is reused)
"""
import json
import os
import re
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import advice  # noqa: E402
import fleet  # noqa: E402
import guard  # noqa: E402
import messages  # noqa: E402

KIND = "owner_call"
OPTIONS = list(messages.OWNER_CALL_OPTIONS)
COMMITTEE_OPTIONS = OPTIONS[:2]      # what the committee may pick by default: never the decline
MAX_OPTIONS = 6
MAX_OPTION = 80
MAX_NOTE = 1000
OPEN = ("pending", "unauthorized")
# an option (or answer) that declines the request: the owner's alone, never the committee's
DECLINE_RE = advice.DECLINE_RE
DEC_RE = re.compile(r"d-[0-9]+-[0-9a-f]+")
CODE_RE = messages.CODE_RE
# a message in one of these states has its own way and is never an owner's call
REFUSED = {"held": "the message is held by the secret guard: an owner releases or declines it on the board",
           "declined": "the message was declined: nothing waits on it",
           "desk": "the local asker desk has it: its forward card is the owner's decision"}

NOTE_WAIT = "Waiting for the owner's decision %s."
NOTE_AWAY = ("The owner is away. Your request is queued as pending owner decision %s; %s")
NEXT_OWNER = "the owner decides when they are back."
NEXT_COMMITTEE = ("the sessions may decide it by unanimous agreement under the away rules; "
                  "red lines and production wait for the owner.")
NOTE_ANSWER = "The owner decided (%s): %s"
NOTE_COMMITTEE = ("While the owner was away, the sessions decided on the owner's behalf by unanimous agreement "
                  "(%s): %s. The owner follows up when back.")
NOTE_SETTLING = "the message's last owner's call was just answered: try again after the sweep (about 15 s)"


def _ms(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _dec_path(home, did):
    return os.path.join(home, "decisions", did + ".json")


def asking_session(d):
    """The session that stands in for the asker in committee advice and decisions: an owner_call's
    advisor (a human asker is never a session), else the decision's `from`."""
    if isinstance(d, dict) and d.get("kind") == KIND:
        return d.get("advisor")
    return d.get("from") if isinstance(d, dict) else None


def open_decision(home, rec):
    """The message's open owner_call decision id, or None."""
    oc = rec.get("owner_call") if isinstance(rec.get("owner_call"), dict) else {}
    did = oc.get("decision")
    if isinstance(did, str) and DEC_RE.fullmatch(did):
        d = _read(_dec_path(home, did))
        if d.get("kind") == KIND and d.get("message_id") == rec.get("id") and d.get("status") in OPEN:
            return did
    return None


def _asker(rec):
    api = rec.get("api") if isinstance(rec.get("api"), dict) else {}
    who = rec.get("asker") or api.get("owner") or re.sub(r"^(board|cli):", "", str(rec.get("by") or "")) or "?"
    return messages.defang(guard.scrub(str(who)))[:200]


def _excerpt(text, n=240):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


QUOTES = str.maketrans({c: "'" for c in "\"«»“”„‟‹›「」『』"})


def untrusted(text, n=240):
    """The asker's words for the owner's card and the advisor: one line, scrubbed, defanged, and every
    quote mark (« » " “ ” 「 」 …) turned into ' so the «…» around them can't be closed from inside."""
    return messages.defang(guard.scrub(_excerpt(text, n))).translate(QUOTES)


is_decline = advice.is_decline


def _advisor(home, rec, cfg, advisor):
    """--advisor, else the one session the message went to (to, routed_to, relayed_to)."""
    known = fleet.known_codes(home, cfg)
    if advisor is not None:
        if not isinstance(advisor, str) or not CODE_RE.fullmatch(advisor) or fleet.is_reserved(advisor) \
                or advisor not in known:
            raise messages.MessageError("bad_advisor", "--advisor must be a session on this board")
        return advisor
    for c in [rec.get("to")] + list(rec.get("routed_to") or []) + list(rec.get("relayed_to") or []):
        if isinstance(c, str) and CODE_RE.fullmatch(c) and not fleet.is_reserved(c) and c in known:
            return c
    return None


def _committee_on(home):
    try:
        import committee  # noqa: WPS433
        return committee.settings(home)["mode"] == "committee"
    except Exception:  # noqa: BLE001 - fails closed: "the owner decides"
        return False


def _answerers(cfg, d):
    """Who may answer D, the rule of bin/board allowed_ids: a production gate's release_managers
    (else approvers), otherwise approvers."""
    ids = (fleet.id_list(cfg, "release_managers") or fleet.id_list(cfg, "approvers")) if fleet.is_production(d) \
        else fleet.id_list(cfg, "approvers")
    return {str(i).strip().lower() for i in ids}


def _settleable(d, cfg):
    """Will _settle take this answered decision: answered by the committee (with its record) or by
    someone who may answer it on fleet.yaml now?"""
    by = str(d.get("answered_by") or "")
    if by == "committee":
        return isinstance(d.get("committee"), dict)
    return re.sub(r"^(board|cli):", "", by).strip().lower() in _answerers(cfg, d)


def create(home, mid, by, options=None, note=None, advisor=None, production=False, red_line=False, cfg=None,
           committee_options=None):
    """The message's pending owner_call decision (see the module doc). Returns (decision id, created?):
    an open one for the message is reused (created False), nothing else written. Under consensus.lock
    (the lock `board answer` and apply hold), so a settle never interleaves with it; the alert goes
    out after the lock is released."""
    import committee  # noqa: WPS433
    try:
        with committee.locked(home, wait_s=10.0):
            did, created, dec = _create(home, mid, by, options, note, advisor, production, red_line, cfg,
                                        committee_options)
    except TimeoutError:
        raise messages.MessageError("busy", "another away/consensus change holds the lock; try again") from None
    if created:
        try:    # the usual decision alert now (the collector keeps it, escalating, until it is answered)
            import notify  # noqa: WPS433
            notify.on_decision(dec)
        except Exception as exc:  # noqa: BLE001 - the decision is open either way
            sys.stderr.write("owner-call: alert failed: %s\n" % type(exc).__name__)
    return did, created


def _create(home, mid, by, options, note, advisor, production, red_line, cfg, committee_options):
    rec = messages.read(home, mid)
    if rec is None:
        raise messages.MessageError("not_found", "no such message")
    st = rec.get("status")
    if st in REFUSED:
        raise messages.MessageError(st, REFUSED[st])
    if rec.get("kind") == "ticket":
        raise messages.MessageError("ticket", "a ticket relay is never an owner's call")
    did = open_decision(home, rec)
    if did:
        return did, False, None
    cfg = cfg if cfg is not None else fleet.load_fleet()
    replaced = None
    if st == messages.AWAITING_OWNER:
        oc = rec.get("owner_call") if isinstance(rec.get("owner_call"), dict) else {}
        last = oc.get("decision")
        d = _read(_dec_path(home, last)) if isinstance(last, str) and DEC_RE.fullmatch(last) else {}
        if d.get("kind") == KIND and d.get("message_id") == mid and _due(d) and _settleable(d, cfg):
            # its decision was answered and the sweep has not written it back yet: never a second card
            raise messages.MessageError("settling", NOTE_SETTLING)
        # the last call is missing, broken or can never settle (its answerer is no longer an approver,
        # a committee answer without its record): a new call replaces it (noted on the message)
        replaced = last if isinstance(last, str) else None
    opts = [str(o).strip() for o in (options or OPTIONS)]
    if not opts or len(opts) > MAX_OPTIONS or any(not o or len(o) > MAX_OPTION for o in opts) \
            or len(set(opts)) != len(opts):
        raise messages.MessageError("bad_option", "1 to %d distinct options of at most %d characters"
                                    % (MAX_OPTIONS, MAX_OPTION))
    if committee_options is None:
        # the default options: the two that are not a decline; custom options: none unless named
        copts = list(COMMITTEE_OPTIONS) if not options else []
    else:
        copts = [str(o).strip() for o in committee_options]
    if any(o not in opts for o in copts) or any(is_decline(o) for o in copts) or len(set(copts)) != len(copts):
        raise messages.MessageError("bad_committee_option", "--committee-option must name one of the options, "
                                    "never a decline, each once")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        raise messages.MessageError("bad_note", "the note is at most %d characters" % MAX_NOTE)
    code = _advisor(home, rec, cfg, advisor)
    import away  # noqa: WPS433
    is_away = away.away_record(home) is not None
    now = messages.now_ms()
    did = "d-%d-%s" % (now // 1000, secrets.token_hex(4))
    who = _asker(rec)
    clean = messages.defang
    op_note = clean(guard.scrub(re.sub(r"\s+", " ", note).strip())) if note and note.strip() else ""
    words = untrusted(rec.get("body"))
    # the operator's note first, the asker's words last, labelled and fenced: nothing in them can
    # read as the operator's or the board's
    ctx = ("Operator's note: %s " % op_note) if op_note else ""
    if code:
        ctx += "Advisor while you are away: %s. " % code
    ctx += "Asker's words (untrusted, message %s): «%s»" % (mid, words)
    dec = guard.scrub_tree(dict({
        "id": did, "kind": KIND, "from": who, "question": "Owner's call: %s's request (message %s)" % (who, mid),
        "options": [clean(o) for o in opts], "committee_options": [clean(o) for o in copts], "context": ctx,
        "status": "pending", "answer": None, "answered_by": None, "answered_at": None, "relayed_at": None,
        "created_at": now, "message_id": mid,
        "request": dict({"kind": KIND, "message_id": mid, "asker": who, "by": by, "at": now, "away": is_away,
                         "excerpt": words}, **({"note": op_note} if op_note else {}))},
        **({"advisor": code} if code else {}), **({"gate": "production"} if production else {}),
        **({"red_line": True} if red_line else {})))
    path = _dec_path(home, did)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    messages._write(path, dec)  # noqa: SLF001
    rec.update({"status": messages.AWAITING_OWNER,
                "owner_call": dict({"decision": did, "at": now, "by": by, "away": is_away, "state": "pending",
                                    "prior": st}, **({"replaced": replaced} if replaced else {}))})
    messages._write(messages.path_of(home, mid), rec)  # noqa: SLF001
    if is_away:
        nxt = NEXT_COMMITTEE if code and _committee_on(home) else NEXT_OWNER
        messages.notice(home, mid, "owner_call_away", NOTE_AWAY % (did, nxt),
                        {"decision": did, "next": "committee" if nxt is NEXT_COMMITTEE else "owner"}, now=now)
    else:
        messages.notice(home, mid, "owner_call", NOTE_WAIT % did, {"decision": did}, now=now)
    if replaced:
        _retire(home, replaced, mid, did, now)
    messages.audit(home, {"type": "message", "action": "owner-call", "id": mid, "decision": did, "by": by,
                          "away": is_away, "advisor": code, **({"replaced": replaced} if replaced else {})})
    return did, True, dec


def _retire(home, old, mid, new, now):
    """The call a new one replaced: handled ("relayed", owner_call_result "replaced"), so a late
    answer to it can never reach the thread."""
    if not DEC_RE.fullmatch(str(old)):
        return
    path = _dec_path(home, old)
    d = _read(path)
    if d.get("kind") != KIND or d.get("message_id") != mid or d.get("owner_call_result"):
        return
    d.update({"status": "relayed", "relayed_at": now, "updated_at": now, "owner_call_result": "replaced",
              "replaced_by": new})
    messages._write(path, d)  # noqa: SLF001


def apply(home, cfg=None, now=None):
    """Answered owner_call decisions go back to their thread (see the module doc).
    Returns [(decision id, result)]: answered | committee | gone."""
    out = []
    ddir = os.path.join(home, "decisions")
    try:
        names = sorted(n for n in os.listdir(ddir) if n.endswith(".json"))
    except OSError:
        return out
    for name in names:
        path = os.path.join(ddir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read(262144)
        except OSError:
            continue
        if '"%s"' % KIND not in raw:
            continue
        d = _read(path)
        if not _due(d):
            continue
        cfg = cfg if cfg is not None else fleet.load_fleet()
        try:
            import committee  # noqa: WPS433 - its lock is the one `board answer` and `board advise` hold
            with committee.locked(home, wait_s=2.0):
                r = _settle(home, path, cfg, now)
        except TimeoutError:
            continue            # an answer is being written: the next sweep takes it
        if r:
            out.append(r)
    return out


def _due(d):
    return d.get("kind") == KIND and d.get("status") == "answered" and not d.get("owner_call_result")


def _settle(home, path, cfg, now):
    """One owner_call decision back to its thread (under the consensus lock): (id, result) or None."""
    d = _read(path)         # read again under the lock: an answer may have changed it
    if not _due(d):
        return None
    now_ = messages.now_ms() if now is None else now
    mid = d.get("message_id")
    rec = messages.read(home, mid) if isinstance(mid, str) and messages.MSG_ID_RE.fullmatch(mid) else None
    if not _settleable(d, cfg):
        return None             # bin/board marks a non-approver's answer unauthorized; never applied
    committee = d.get("answered_by") == "committee"
    ans = re.sub(r"\s+", " ", messages.defang(guard.scrub(str(d.get("answer") or "")))).strip()[:500]
    result = "committee" if committee else "answered"
    body = (NOTE_COMMITTEE if committee else NOTE_ANSWER) % (d.get("id"), ans)
    args = {"decision": d.get("id"), "answer": ans, "by": "committee" if committee else "owner"}
    if rec is not None:
        messages.notice(home, mid, "owner_answer", body, args, now=now_)
        oc = rec.get("owner_call") if isinstance(rec.get("owner_call"), dict) else {}
        if oc.get("decision") == d.get("id"):
            # only the message's current owner's call moves it on; an older one just leaves its note
            rec["owner_call"] = dict(oc, state=result, settled_at=now_)
            if rec.get("status") == messages.AWAITING_OWNER:
                rec["status"] = "replied"
            messages._write(messages.path_of(home, mid), rec)  # noqa: SLF001
    else:
        result = "gone"
    d.update({"owner_call_result": result, "updated_at": now_, "status": "relayed", "relayed_at": now_})
    messages._write(path, d)  # noqa: SLF001
    messages.audit(home, {"type": "message", "action": "owner-call-" + result, "id": mid, "decision": d.get("id")})
    return d.get("id"), result
