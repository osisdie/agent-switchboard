#!/usr/bin/env python3
"""Owner-away mode and consensus items, read-only (bin/board writes them; docs/contract.md,
"Owner away & consensus").

- away_record(home): None when $BOARD_HOME/owner-away does not exist; else {since, by, said}.
  Presence of the file is what counts: a free-text (legacy) file is still "away" and comes back
  as {since: <mtime ms>, by: None, said: <first 300 chars>, legacy: True}.
- consensus_items(home, now): the compact list for the snapshot and the board page: every
  `pending` item, plus final ones (agreed, objected, owner_back) for 24 h after they were decided.
  When the owner is not away, a still-pending item is shown as `owner_back` (display only; the
  next `board consensus` command writes it).
- Free text that BOARD may quote never looks like a relay: "[BOARD" becomes "(BOARD" (as in
  bin/board).
"""
import glob
import json
import os
import re

import fleet

KEEP_MS = 24 * 3600 * 1000
FINAL = ("agreed", "objected", "owner_back")
MAX_TEXT = 300
MAX_NOTE = 200
MAX_READ = 16 * 1024   # bin/board away_record reads the same amount; --said is <= 2000 characters
DEFANG_RE = re.compile(r"\[(board)", re.IGNORECASE)


def _clip(s, n):
    s = s if isinstance(s, str) else ("" if s is None else str(s))
    s = fleet.safe_text(re.sub(r"[\r\n\t]+", " ", s))
    return s if len(s) <= n else s[: n - 1] + "…"


def _text(s, n):
    """Free text from sessions or the owner: clipped, one line, and never relay-shaped."""
    return DEFANG_RE.sub(r"(\1", _clip(s, n))


def _ms(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def away_record(home):
    path = os.path.join(home, "owner-away")
    if not os.path.lexists(path):
        return None
    raw = b""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_READ)
    except OSError:
        pass
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        rec = None
    if isinstance(rec, dict):
        return {"since": _ms(rec.get("since")) or None,
                "by": _clip(rec.get("by"), 200) if rec.get("by") else None,
                "said": _text(rec.get("said"), MAX_TEXT) if rec.get("said") else None}
    try:
        mtime = int(os.lstat(path).st_mtime * 1000)
    except OSError:
        mtime = None
    text = raw.decode("utf-8", "replace").strip()
    return {"since": mtime, "by": None, "said": _text(text, MAX_TEXT) if text else None, "legacy": True}


def compact(c):
    """The fields the page shows; votes keep their order (a change agree -> object stays visible)."""
    votes = []
    for v in c.get("votes") or []:
        if isinstance(v, dict):
            votes.append({"by": _clip(v.get("by"), 64), "vote": _clip(v.get("vote"), 16),
                          "at": _ms(v.get("at")) or None,
                          "note": _text(v.get("note"), MAX_NOTE) if v.get("note") else None})
    parts = [_clip(p, 64) for p in (c.get("participants") or []) if isinstance(p, str)]
    return {"id": c.get("id"), "from": _clip(c.get("from"), 64), "participants": parts,
            "ref": _clip(c.get("ref"), MAX_TEXT) if c.get("ref") else None,
            "proposal": _text(c.get("proposal"), MAX_TEXT), "status": _clip(c.get("status"), 16),
            "votes": votes, "created_at": _ms(c.get("created_at")) or None,
            "updated_at": _ms(c.get("updated_at")) or None, "decided_at": _ms(c.get("decided_at")) or None}


def consensus_items(home, now):
    away = away_record(home) is not None
    out = []
    for path in glob.glob(os.path.join(home, "consensus", "c-*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                c = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(c, dict) or not isinstance(c.get("id"), str):
            continue
        if c.get("status") in FINAL and now - _ms(c.get("decided_at") or c.get("updated_at")) >= KEEP_MS:
            continue
        item = compact(c)
        if not away and item["status"] == "pending":
            item["status"] = "owner_back"  # display only: the owner is back, so it waits for them
        out.append(item)
    out.sort(key=lambda c: (c.get("created_at") or 0, c["id"]))
    return out
