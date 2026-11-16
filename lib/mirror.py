#!/usr/bin/env python3
"""mirror: the one source the BOARD operator may build Artifact-db documents from (`board mirror
state`). The Artifact page's viewers are not all owners, approvers or release managers, so this is
the board state as an anonymous viewer sees it, with every viewer rule applied here rather than
left for the operator to remember:

  - no private card text (cards_for: no last prompt, no assistant text);
  - no ticket text (Q3, ticket_text_for: a ticket-backed task shows its key);
  - no account email and no operator account_hint (owners only);
  - no one's email (lib/pii.py: role labels; claude.ai user ids, `local`, session codes stay).

CLI (through `board mirror state`): print that state as JSON. Read-only; it changes nothing.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402
import pii  # noqa: E402
import serve  # noqa: E402


def state(home=None, cfg=None):
    home = home or fleet.board_home()
    cfg = cfg if cfg is not None else fleet.load_fleet()
    st = serve.StateBuilder(home).build(cfg)
    st["sessions"] = serve.cards_for(st.get("sessions"), False)
    st["operator"] = serve.owner_only_operator(st.get("operator"))
    st = serve.ticket_text_for(st, False)
    st = dict(st, account=None, mode="mirror")
    return pii.mask(st, cfg, viewer=None)


def main(argv):
    if argv[1:] != ["state"]:
        sys.stderr.write("usage: mirror.py state   (or: board mirror state)\n")
        return 2
    print(json.dumps(state(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
