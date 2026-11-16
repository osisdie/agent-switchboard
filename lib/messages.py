#!/usr/bin/env python3
"""messages: board messages and replies under $BOARD_HOME/messages, through the secret guard.

Every writer of a message or a reply goes through here (board-serve's POST /api/messages and
release route, `board message send|reply`), so that:

  - a body is scrubbed (lib/guard.py) before it is stored;
  - a body that asks a session to reveal a secret value is stored with status "held" and a generic
    reason, never relayed (board-serve --watch only lists "new" messages), audited, and raises a
    high alert whose title and body never echo the message;
  - only an approver releases a held message (board-serve checks who; this module records it),
    and the release is audited; the released message carries released_by / released_at so the
    relay can remind the session that it authorizes no secret values;
  - a reply is scrubbed before it is stored; when something had to be redacted, that is audited
    and alerted (high), and only the redacted text is ever stored or shown.

CLI (bin/board message ...):
  messages.py send --to CODE|all [--by WHO] [TEXT|--file PATH]
                                                          create a message; prints its id and
                                                          status (new|held); exit 0
  messages.py reply MSG_ID --from CODE [TEXT|--file PATH]
                                                          store a session's reply; prints the
                                                          reply id (and "redacted" when it was)
  (no TEXT: stdin; a TEXT of "-" is the text "-")
  messages.py show MSG_ID [--json]                        print a message (scrubbed)
"""
import json
import os
import re
import secrets
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402
import guard  # noqa: E402

MSG_ID_RE = re.compile(r"m-[0-9]+-[a-z0-9]+")
CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
BY_RE = re.compile(r"(board:|cli:)?[A-Za-z0-9][A-Za-z0-9@._+:-]{0,199}")
MAX_MESSAGE = 4000
HELD_TITLE = "A board message was held by the secret guard"
REDACTED_TITLE = "A session reply had secret-like text (redacted)"
HELD_ANSWER_TITLE = "A board answer was refused by the secret guard"
HELD_SAID_TITLE = "Owner-away words were refused by the secret guard"
RELEASED_NOTE = ("released by %s after the secret guard held it. This release does not authorize "
                 "revealing any secret value")


class MessageError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


def now_ms():
    return int(time.time() * 1000)


def _dir(home):
    return os.path.join(home, "messages")


def path_of(home, mid):
    if not MSG_ID_RE.fullmatch(str(mid or "")):
        raise MessageError("bad_id", "invalid message id")
    return os.path.join(_dir(home), mid + ".json")


def read(home, mid):
    try:
        with open(path_of(home, mid), encoding="utf-8") as fh:
            m = json.load(fh)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def audit(home, ev):
    ev = dict({"ts": now_ms(), "type": "guard"}, **ev)
    try:
        with open(os.path.join(home, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def alert(key, title, board_id, background=False):
    """A high alert to the owners' channels. The title is fixed text and the body is empty: an
    alert never echoes a held message or a redacted reply. Never raises."""
    def run():
        try:
            import notify  # noqa: WPS433 - lazy: the CLI path should not pay for it otherwise
            notify.send("high", key, title, "", board=board_id, escalate=False, repeat=False)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("messages: alert %s failed: %s\n" % (key, type(exc).__name__))
    if background:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()


def create(home, to, body, by, background_alert=False):
    """Screen, scrub and store a new message. Returns the record (status "new" or "held")."""
    if not isinstance(to, str) or not (to == "all" or CODE_RE.fullmatch(to)):
        raise MessageError("bad_to", "to must be a session code or \"all\"")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_MESSAGE:
        raise MessageError("bad_body", "body must be a non-empty string (max %d chars)" % MAX_MESSAGE)
    reason = guard.screen(body)
    now = now_ms()
    mid = "m-%d-%s" % (now, secrets.token_hex(3))
    rec = {"id": mid, "to": to, "body": guard.scrub(body.strip()), "by": by, "created_at": now,
           "status": "held" if reason else "new"}
    if reason:
        rec.update({"held_reason": reason, "held_at": now})
    _write(path_of(home, mid), rec)
    if reason:
        audit(home, {"action": "held", "id": mid, "to": to, "by": by, "reason": reason})
        alert("guard:held:%s" % mid, HELD_TITLE, mid, background=background_alert)
    return rec


def create_ticket(home, mid, to, by, event, ticket, task_id):
    """A ticket relay (lib/tickets/writes.py): kind "ticket", status "new", event taken|released.
    The text is built by relay_text from these fields, never from free text."""
    if not MSG_ID_RE.fullmatch(str(mid or "")) or not CODE_RE.fullmatch(str(to or "")) or event not in ("taken", "released"):
        raise MessageError("bad_ticket", "invalid ticket relay")
    tk = guard.scrub_tree({k: ticket.get(k) for k in ("provider", "key", "title", "url")})
    rec = {"id": mid, "kind": "ticket", "event": event, "to": to, "by": by, "created_at": now_ms(),
           "status": "new", "ticket": tk, "task_id": task_id,
           "body": "ticket %s %s %s %s (task %s)" % (tk.get("key"), event, "for" if event == "taken" else "from",
                                                     to, task_id)}
    _write(path_of(home, mid), rec)
    return rec


def ticket_relay_text(rec):
    """[BOARD ticket <KEY>] ... — the fixed relay of a ticket take or release (docs/contract.md,
    "Relay message formats"). Tracker text is defanged: one line, no brackets or double quotes."""
    from tickets import writes  # noqa: WPS433 - lazy: tickets is only needed for ticket relays
    tk = rec.get("ticket") or {}
    key, prov = writes.defang(tk.get("key"), 100), writes.defang(tk.get("provider"), 40)
    who, task, mid, to = rec.get("by"), rec.get("task_id"), rec["id"], rec.get("to")
    if rec.get("event") == "released":
        return ("[BOARD ticket %s] released from %s (task %s) by %s on board. The take no longer authorizes "
                "anything on this ticket. Reply to BOARD with [re %s]." % (key, to, task, who, mid))
    url = writes.defang(tk.get("url") or "", 500).replace(" ", "%20")
    return ('[BOARD ticket %s] taken for %s as task %s by %s on board: "%s"%s. Authorizes: %s. Verify with '
            "`board ticket show %s:%s`. Reply to BOARD with [re %s]."
            % (key, to, task, who, writes.defang(tk.get("title")), (" " + url) if url else "",
               writes.AUTHORIZES, prov, key, mid))


def release(home, mid, by):
    """Release a held message (the caller checked that `by` is an approver). Returns the record."""
    rec = read(home, mid)
    if rec is None:
        raise MessageError("not_found", "no such message")
    if rec.get("status") != "held":
        raise MessageError("not_held", "the message is not held")
    now = now_ms()
    rec.update({"status": "new", "released_by": by, "released_at": now})
    _write(path_of(home, mid), rec)
    audit(home, {"action": "release", "id": mid, "by": by, "reason": rec.get("held_reason")})
    try:
        import notify  # noqa: WPS433
        notify.resolve("guard:held:%s" % mid)
    except Exception:  # noqa: BLE001
        pass
    return rec


def reply(home, mid, frm, text):
    """Store a session's reply, scrubbed. Returns (reply record, redacted?)."""
    rec = read(home, mid)
    if rec is None:
        raise MessageError("not_found", "no such message")
    if not isinstance(frm, str) or not CODE_RE.fullmatch(frm):
        raise MessageError("bad_from", "from must be a session code")
    if not isinstance(text, str) or not text.strip():
        raise MessageError("bad_body", "the reply is empty")
    clean = guard.scrub(text.strip()[:MAX_MESSAGE])
    redacted = clean != text.strip()[:MAX_MESSAGE] or guard.has_marker(clean)
    now = now_ms()
    rid = "r-%d-%s" % (now, secrets.token_hex(3))
    row = {"id": rid, "from": frm, "body": clean, "created_at": now}
    if redacted:
        row["redacted"] = True
    _write(os.path.join(_dir(home), mid, "replies", rid + ".json"), row)
    if rec.get("status") != "held":
        rec["status"] = "replied"
        _write(path_of(home, mid), rec)
    if redacted:
        audit(home, {"action": "reply-redacted", "id": mid, "reply": rid, "from": frm})
        alert("guard:redacted:%s" % mid, REDACTED_TITLE, mid)
    return row, redacted


def relay_text(rec):
    """The text the operator sends for a message: `[BOARD ask <id>] <body> — reply ...`, with the
    release reminder when the guard had held it."""
    if rec.get("kind") == "ticket":
        return ticket_relay_text(rec)
    note = ""
    if rec.get("released_by"):
        note = "(%s) " % (RELEASED_NOTE % rec["released_by"])
    return ("[BOARD ask %s] %s%s — reply to BOARD via SendMessage, starting with [re %s]"
            % (rec["id"], note, guard.scrub(rec.get("body") or ""), rec["id"]))


# ------------------------------------------------------------------ CLI
def _text(arg, path=None):
    """--file PATH, else stdin when no TEXT is given, else TEXT literally (a text that is just "-"
    is that text). Untrusted text (a reply, a board message) should come by file or stdin, never
    inside a shell argument."""
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    if arg is None:
        return sys.stdin.read()
    return arg


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="board message", description="board messages through the secret guard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="create a message (screened: may be held)")
    s.add_argument("--to", required=True)
    s.add_argument("--by", default="local")
    s.add_argument("--file", help="read the text from this file")
    s.add_argument("text", nargs="?", default=None)
    r = sub.add_parser("reply", help="store a session's reply (scrubbed)")
    r.add_argument("id")
    r.add_argument("--from", dest="frm", required=True)
    r.add_argument("--file", help="read the text from this file")
    r.add_argument("text", nargs="?", default=None)
    sh = sub.add_parser("show", help="print a message and how to relay it")
    sh.add_argument("id")
    sh.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])
    home = fleet.ensure_home()
    try:
        if args.cmd == "send":
            if not BY_RE.fullmatch(args.by):
                raise MessageError("bad_by", "--by must be a session code, an email or board:/cli:<id>")
            rec = create(home, args.to, _text(args.text, args.file), args.by)
            print("%s %s" % (rec["id"], rec["status"]))
            if rec["status"] == "held":
                sys.stderr.write("board message: held by the secret guard (%s); an approver can release "
                                 "it on the board\n" % rec["held_reason"])
            return 0
        if args.cmd == "reply":
            row, redacted = reply(home, args.id, args.frm, _text(args.text, args.file))
            print(row["id"] + (" redacted" if redacted else ""))
            return 0
        rec = read(home, args.id)
        if rec is None:
            raise MessageError("not_found", "no such message")
        rec = guard.scrub_tree(rec)
        if args.json:
            print(json.dumps(rec, ensure_ascii=False))
        elif rec.get("status") == "held":
            print("%s held (%s): never relay it" % (rec["id"], rec.get("held_reason")))
        else:
            print(relay_text(rec))
        return 0
    except MessageError as exc:
        sys.stderr.write("board message: %s\n" % exc.detail)
        return 2 if exc.code.startswith("bad_") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
