#!/usr/bin/env python3
"""board ticket — read tickets from the configured trackers (fleet.yaml `tickets.trackers`).

usage: board ticket get REF [--refresh] [--json]
       board ticket doctor [--json]

  get      REF is KEY (matched against every tracker's key_pattern) or provider:KEY. Served from
           the cache for tickets.cache_ttl_seconds (60) unless --refresh. Every field is redacted
           (tracker tokens, then the secret guard) before it is printed or cached.
  doctor   each tracker: type, host, key pattern, token set/unset (never a value), problems.

Exit codes: 0 ok, 1 error (upstream, auth, unconfigured; doctor: a tracker has problems),
            2 usage, bad or unknown ref, ambiguous ref (the candidates on stderr),
            3 not found, 5 rate limited (retry_after on stderr).
"""
import json
import os
import sys

# Run as a script, this file's directory comes first on sys.path; use lib/ instead, so nothing
# here can shadow a standard module.
LIB = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path[0] = LIB

import tickets  # noqa: E402
from tickets import TicketError  # noqa: E402

EXIT = {"ticket_not_found": 3, "ticket_rate_limited": 5, "unknown_ticket_ref": 2, "bad_ticket_ref": 2,
        "ticket_ambiguous": 2}


def usage(out=sys.stdout):
    out.write(__doc__.split("\n\n", 1)[1].split("\n\nExit", 1)[0].strip() + "\n")


def show(t):
    a = t.get("assignee") or {}
    lines = ["%s:%s  %s (%s)" % (t["provider"], t["key"], t.get("status") or "?", t.get("status_category")),
             "title:    %s" % t.get("title", ""),
             "assignee: %s" % (a.get("name") or a.get("id") or "-"),
             "updated:  %s" % (t.get("updated_at") or "-"),
             "url:      %s" % (t.get("url") or "-")]
    if t.get("excerpt"):
        lines += ["", t["excerpt"]]
    return "\n".join(tickets.fleet.safe_text(ln) for ln in lines)


def cmd_get(args):
    ref, as_json, refresh = None, False, False
    for a in args:
        if a == "--json":
            as_json = True
        elif a == "--refresh":
            refresh = True
        elif a.startswith("-") or ref is not None:
            usage(sys.stderr)
            return 2
        else:
            ref = a
    if ref is None:
        usage(sys.stderr)
        return 2
    try:
        t = tickets.get(ref, refresh=refresh, by="local", via="cli")
    except TicketError as exc:
        sys.stderr.write("board ticket: %s: %s\n" % (exc.code, tickets.fleet.safe_text(exc.detail)))
        if exc.candidates:
            sys.stderr.write("  candidates: %s\n" % ", ".join(exc.candidates))
        if exc.retry_after is not None:
            sys.stderr.write("  retry_after: %d s\n" % exc.retry_after)
        return EXIT.get(exc.code, 1)
    print(json.dumps(t, ensure_ascii=False, indent=1) if as_json else show(t))
    return 0


def cmd_doctor(args):
    as_json = "--json" in args
    if any(a != "--json" for a in args):
        usage(sys.stderr)
        return 2
    rows = tickets.redact_tree(tickets.doctor())
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif not rows:
        print("no trackers configured (fleet.yaml tickets.trackers)")
    for r in ([] if as_json else rows):
        tok = r.get("token")
        print("%s %s (%s)%s%s  key_pattern %s" % (
            "ok  " if r["ok"] else "FAIL", r["name"], r["type"] or "?", "  host " + r["host"] if r.get("host") else "",
            "  token " + tok + (" (%s)" % r["token_source"] if r.get("token_source") else "") if tok else "",
            r.get("key_pattern")))
        for p in r["problems"]:
            print("     - %s" % p)
        for w in r.get("warnings") or []:
            print("     ! %s" % w)
    return 0 if all(r["ok"] for r in rows) else 1


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage()
        return 0 if argv else 2
    if "-h" in argv or "--help" in argv:
        usage()
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "get":
        return cmd_get(rest)
    if cmd == "doctor":
        return cmd_doctor(rest)
    usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
