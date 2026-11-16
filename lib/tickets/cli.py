#!/usr/bin/env python3
"""board ticket — tracker tickets (fleet.yaml `tickets.trackers`).

usage: board ticket get REF [--refresh] [--json]
       board ticket doctor [--json]
       board ticket show REF [--json]
       board ticket take REF --session CODE --by WHO [--comment-file F | --no-comment] [--no-transition] [--no-assign] [--json]
       board ticket release REF --by WHO
       board ticket comment REF --by WHO --file F
       board ticket transition REF STATE --by WHO
       board ticket assign REF NAME --by WHO

  get         REF is KEY (matched against every tracker's key_pattern) or tracker:KEY. Served from
              the cache for tickets.cache_ttl_seconds (60) unless --refresh. Every field is redacted
              (tracker tokens, then the secret guard) before it is printed or cached.
  doctor      each tracker: type, host, key pattern, capabilities, token set/unset (never a value).
  show        the take of a ticket (tickets/links/): status, session, task, who took it and whether
              they are an approver now. Exit 0 only for an active take. Workers check a
              [BOARD ticket] relay with it.
  take        an approver takes the ticket for session CODE: fresh fetch, task (board task new
              --ticket), link, a ticket relay for the operator, then the tracker's `take:` writes
              (comment with the "(via agent-switchboard)" footer, transition, assign). A failed
              tracker write leaves status "partial" (exit 0, failed ops on stderr).
  release     end an active take; the session is told.
  comment     post the file's text (scrubbed, plus the footer); transition / assign: the tracker's
              configured write. All writes: WHO must be in approvers.

Exit codes: 0 ok, 1 error (upstream, auth, unconfigured, already taken, not taken; doctor: a
            tracker has problems; show: no active take), 2 usage, bad or unknown ref, ambiguous
            ref (candidates on stderr), bad value, unknown session, 3 not found, 4 not an approver,
            5 rate limited (retry_after on stderr).
"""
import json
import os
import sys

# Run as a script, this file's directory comes first on sys.path; use lib/ instead, so nothing
# here can shadow a standard module.
LIB = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path[0] = LIB

import fleet  # noqa: E402
import tickets  # noqa: E402
from tickets import TicketError, writes  # noqa: E402

EXIT = {"ticket_not_found": 3, "ticket_rate_limited": 5, "unknown_ticket_ref": 2, "bad_ticket_ref": 2,
        "ticket_ambiguous": 2, "bad_transition": 2, "bad_comment": 2, "bad_body": 2, "unknown_session": 2,
        "ticket_unsupported": 2, "not_approver": 4, "no_approvers": 4}


def usage(out=sys.stdout):
    out.write(__doc__.split("\n\n", 1)[1].split("\n\nExit", 1)[0].strip() + "\n")


class Usage(Exception):
    pass


def parse(args, flags=(), opts=(), npos=1):
    """(positionals, {flag: True}, {opt: value}) or Usage."""
    pos, fl, op = [], {}, {}
    it = iter(args)
    for a in it:
        if a in flags:
            fl[a] = True
        elif a in opts:
            v = next(it, None)
            if v is None:
                raise Usage()
            op[a] = v
        elif a.startswith("-") and a != "-":
            raise Usage()
        else:
            pos.append(a)
    if len(pos) != npos:
        raise Usage()
    return pos, fl, op


def fail(exc):
    sys.stderr.write("board ticket: %s: %s\n" % (exc.code, fleet.safe_text(exc.detail)))
    if exc.candidates:
        sys.stderr.write("  candidates: %s\n" % ", ".join(exc.candidates))
    if exc.retry_after is not None:
        sys.stderr.write("  retry_after: %d s\n" % exc.retry_after)
    for k, v in sorted(exc.extra.items()):
        sys.stderr.write("  %s: %s\n" % (k, fleet.safe_text(v)))
    return EXIT.get(exc.code, 1)


def show(t):
    a = t.get("assignee") or {}
    lines = ["%s:%s  %s (%s)" % (t["provider"], t["key"], t.get("status") or "?", t.get("status_category")),
             "title:    %s" % t.get("title", ""),
             "assignee: %s" % (a.get("name") or a.get("id") or "-"),
             "updated:  %s" % (t.get("updated_at") or "-"),
             "url:      %s" % (t.get("url") or "-")]
    if t.get("excerpt"):
        lines += ["", t["excerpt"]]
    return "\n".join(fleet.safe_text(ln) for ln in lines)


def read_file(path):
    if path == "-":
        return sys.stdin.read()
    if not os.path.exists(path) or os.path.isdir(path):
        raise Usage()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def cmd_get(args):
    (ref,), fl, _ = parse(args, flags=("--json", "--refresh"))
    t = tickets.get(ref, refresh="--refresh" in fl, by="local", via="cli")
    print(json.dumps(t, ensure_ascii=False, indent=1) if "--json" in fl else show(t))
    return 0


def cmd_doctor(args):
    _, fl, _ = parse(args, flags=("--json",), npos=0)
    rows = tickets.redact_tree(tickets.doctor())
    if "--json" in fl:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif not rows:
        print("no trackers configured (fleet.yaml tickets.trackers)")
    for r in ([] if "--json" in fl else rows):
        tok = r.get("token")
        print("%s %s (%s)%s%s  key_pattern %s  can %s" % (
            "ok  " if r["ok"] else "FAIL", r["name"], r["type"] or "?", "  host " + r["host"] if r.get("host") else "",
            "  token " + tok + (" (%s)" % r["token_source"] if r.get("token_source") else "") if tok else "",
            r.get("key_pattern"), ",".join(r.get("capabilities") or []) or "-"))
        for p in r["problems"]:
            print("     - %s" % p)
        for w in r.get("warnings") or []:
            print("     ! %s" % w)
    return 0 if all(r["ok"] for r in rows) else 1


def cmd_show(args):
    (ref,), fl, _ = parse(args, flags=("--json",))
    home = fleet.ensure_home()
    rows = writes.links(home, ref)
    cfg = fleet.load_fleet()
    for d in rows:
        d["taken_by_approver"] = writes.is_approver(d.get("taken_by"), cfg)
    rows = tickets.redact_tree(rows)
    if "--json" in fl:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif not rows:
        print("%s: never taken" % fleet.safe_text(ref))
    for d in ([] if "--json" in fl else rows):
        up = ", ".join("%s %s" % (u.get("op"), u.get("result")) for u in d.get("upstream") or []) or "-"
        print("\n".join(fleet.safe_text(x) for x in (
            "%s:%s  %s" % (d.get("provider"), d.get("key"), d.get("status")),
            "session:   %s" % d.get("session"),
            "task:      %s" % d.get("task_id"),
            "taken_by:  %s (approver now: %s)" % (d.get("taken_by"), "yes" if d["taken_by_approver"] else "no"),
            "taken_at:  %s" % d.get("taken_at"),
            "message:   %s" % d.get("msg_id"),
            "upstream:  %s" % up)))
    active = [d for d in rows if d.get("status") == "active"]
    if len(active) > 1:
        sys.stderr.write("board ticket: several trackers have an active take; use tracker:KEY\n")
        return 2
    return 0 if active else 1


def cmd_take(args):
    (ref,), fl, op = parse(args, flags=("--json", "--no-comment", "--no-transition", "--no-assign"),
                           opts=("--session", "--by", "--comment-file"))
    if "--session" not in op or "--by" not in op or ("--comment-file" in op and "--no-comment" in fl):
        raise Usage()
    text = read_file(op["--comment-file"]) if "--comment-file" in op else None
    r = writes.take(ref, op["--session"], op["--by"], via="cli", comment_text=text,
                    do_comment="--no-comment" not in fl, do_transition="--no-transition" not in fl,
                    do_assign="--no-assign" not in fl)
    if "--json" in fl:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("%s %s task %s message %s" % (r["status"], fleet.safe_text(ref), r["task_id"], r["msg_id"]))
    if r["failed"]:
        sys.stderr.write("board ticket: tracker writes failed: %s (the take stands)\n" % ", ".join(r["failed"]))
    return 0


def cmd_release(args):
    (ref,), _, op = parse(args, opts=("--by",))
    if "--by" not in op:
        raise Usage()
    r = writes.release(ref, op["--by"], via="cli")
    print("released %s:%s message %s" % (r["link"]["provider"], r["link"]["key"], r["msg_id"]))
    return 0


def cmd_write(cmd, args):
    if cmd == "comment":
        (ref,), _, op = parse(args, opts=("--by", "--file"))
        if "--by" not in op or "--file" not in op:
            raise Usage()
        r = writes.comment(ref, read_file(op["--file"]), op["--by"], via="cli")
    else:
        (ref, value), _, op = parse(args, opts=("--by",), npos=2)
        if "--by" not in op:
            raise Usage()
        r = getattr(writes, cmd)(ref, value, op["--by"], via="cli")
    t = r.get("ticket")
    print("ok %s%s" % (cmd, " -> %s (%s)" % (t.get("status"), t.get("status_category")) if t else ""))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage()
        return 0 if argv else 2
    if "-h" in argv or "--help" in argv:
        usage()
        return 0
    cmd, rest = argv[0], argv[1:]
    table = {"get": cmd_get, "doctor": cmd_doctor, "show": cmd_show, "take": cmd_take, "release": cmd_release}
    try:
        if cmd in table:
            return table[cmd](rest)
        if cmd in ("comment", "transition", "assign"):
            return cmd_write(cmd, rest)
        raise Usage()
    except Usage:
        usage(sys.stderr)
        return 2
    except TicketError as exc:
        return fail(exc)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
