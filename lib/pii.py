#!/usr/bin/env python3
"""pii: other people's emails, as board-serve, /api/v1 and the Artifact mirror serve them
(docs/contract.md "Who sees whose identity").

Only an owner (fleet.yaml `owners`, else `approvers`) sees other people's emails. Everyone else gets
a role label in their place: "owner", "approver", "release manager", "asker", "viewer" (a listed
tunnel viewer), "you" for their own email (only where it is verified: the tunnel), or "someone".
The raw values stay on disk and in the CLI; this module only changes what is served.

Only email-shaped identities are masked (with or without a "board:" / "cli:" prefix): `local`,
session codes, key ids ("apikey:<kid>") and the Artifact board's opaque claude.ai user ids stay as
they are (the Artifact page needs those to check its approvers).

  mask(obj, cfg, viewer=None)   a masked deep copy: identity fields (answered_by, by, released_by,
                                updated_by, approved_by, ...), any string that is exactly an email,
                                and every email inside other strings (a note, a message body)

CLI: pii.py mask   JSON on stdin -> the same JSON masked for an anonymous viewer (no "you"), for
                   the operator's Artifact mirror (`board mask`)
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet  # noqa: E402

EMAIL_RE = re.compile(r"[^@\s:]{1,64}@[^@\s]{1,189}\.[^@\s.]{1,63}")
# an email inside free text (a note, a message body): every one is masked, known or not
TEXT_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,63}")
PREFIX_RE = re.compile(r"^(board:|cli:)", re.I)
# Fields that hold who did something. `owner` is not here: a task's owner is a session code.
IDENT_KEYS = frozenset(("by", "answered_by", "resolved_by", "released_by", "updated_by", "created_by",
                        "approved_by", "retired_by", "returned_by", "taken_by", "acked_by", "requested_by",
                        "asker", "set_by", "said_by"))
LABELS = {"owner": "owner", "approver": "approver", "release_manager": "release manager", "asker": "asker",
          "viewer": "viewer"}


def _ids(cfg, key):
    return [str(i).strip().lower() for i in fleet.id_list(cfg, key)]


def roster(cfg):
    """lowercased email -> role label, the strongest role first."""
    out = {}
    owners = _ids(cfg, "owners") or _ids(cfg, "approvers")
    for role, ids in (("owner", owners), ("approver", _ids(cfg, "approvers")),
                      ("release_manager", _ids(cfg, "release_managers")), ("asker", _ids(cfg, "askers"))):
        for i in ids:
            if "@" in i:
                out.setdefault(i, LABELS[role])
    serve = cfg.get("serve") if isinstance(cfg.get("serve"), dict) else {}
    tun = serve.get("tunnel") if isinstance(serve.get("tunnel"), dict) else {}
    allowed = tun.get("allowed_emails")
    for i in ([allowed] if isinstance(allowed, str) else allowed if isinstance(allowed, list) else []):
        i = str(i).strip().lower()
        if "@" in i:
            out.setdefault(i, LABELS["viewer"])
    return out


class Masker:
    def __init__(self, cfg, viewer=None):
        self.roster = roster(cfg)
        self.viewer = str(viewer or "").strip().lower() or None


    def label(self, value):
        """The label for one identity value, or the value itself when it is not an email."""
        s = str(value)
        bare = PREFIX_RE.sub("", s.strip())
        if not EMAIL_RE.fullmatch(bare):
            return value
        low = bare.lower()
        if self.viewer and low == self.viewer:
            return "you"
        return self.roster.get(low, "someone")

    def text(self, s):
        """A free-text string: every email inside it becomes a label (known or not: "someone")."""
        if "@" not in s:
            return s
        return TEXT_EMAIL_RE.sub(lambda m: self.label(m.group(0)), s)

    def walk(self, obj, key=None):
        if isinstance(obj, dict):
            return {k: (v if k == "me" else self.walk(v, k)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            if key in IDENT_KEYS or EMAIL_RE.fullmatch(PREFIX_RE.sub("", obj.strip())):
                lab = self.label(obj)
                if lab is not obj:
                    return lab
            return self.text(obj)
        return obj


def mask(obj, cfg=None, viewer=None):
    return Masker(cfg if cfg is not None else fleet.load_fleet(), viewer).walk(obj)


def main(argv):
    if argv[1:] != ["mask"]:
        sys.stderr.write("usage: pii.py mask < doc.json   (or: board mask)\n")
        return 2
    try:
        doc = json.load(sys.stdin)
    except ValueError:
        sys.stderr.write("pii.py: stdin is not JSON\n")
        return 1
    print(json.dumps(mask(doc), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
