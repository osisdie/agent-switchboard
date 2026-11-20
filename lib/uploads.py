#!/usr/bin/env python3
"""uploads: images attached to board messages, under $BOARD_HOME/uploads (docs/contract.md,
"Message attachments").

The board page (POST /api/uploads) and `board message send --attach PATH` store an image here
before the message that carries it is sent; the message then names the upload ids
(`attachments`). When the message is relayed, the relay line lists the images' absolute paths so
the session can open them with its file tool (lib/messages.py relay_text). Nothing serves an image
back to a browser.

  - The type is taken from the file's first bytes (PNG, JPEG, GIF, WebP), never from the client's
    Content-Type or file name; anything else is refused (SVG included: it can carry script).
  - A file is stored as <id>.<ext> with a server-made id (u-<ms>-<16 hex>), mode 0600, in a 0700
    directory; its sidecar <id>.json keeps who uploaded it, the size, the type, the messages that
    carry it and the client's file name, cleaned, as display text only (never part of a path).
  - Caps: MAX_BYTES per image, MAX_PER_MESSAGE per message, MAX_PENDING images an uploader may hold
    that no message carries yet, USER_DAY_BYTES an uploader may store per day, and what one uploader
    may hold in all (USER_HELD_BYTES; an asker ASKER_HELD_BYTES). TOTAL_BYTES on disk, of which
    askers together may fill at most TOTAL_BYTES - DECIDER_RESERVE: approvers can still attach
    images when askers have used their share.
  - Housekeeping (prune: on every upload, when board-serve starts and every hour): an image no
    message carries is deleted after PENDING_MS, every image after MAX_AGE_MS, stray files (no
    sidecar, temp files) after PENDING_MS. A declined message never marks its images as carried, so
    they go with the unsent ones; the ✕ on the page deletes an unsent upload at once (delete_pending).
  - Only the uploader may attach (resolve) or delete an upload; an unknown id and someone else's
    are refused alike.
  - Every change to the directory (store, prune, mark, delete) holds an flock on uploads/.lock, so
    `board message send --attach` and board-serve never race.
"""
import fcntl
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import unicodedata
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

MAX_BYTES = 5 * 1024 * 1024
MAX_PER_MESSAGE = 4
MAX_PENDING = 12
USER_DAY_BYTES = 64 * 1024 * 1024
USER_HELD_BYTES = 128 * 1024 * 1024    # all of one uploader's images, any age
ASKER_HELD_BYTES = 48 * 1024 * 1024     # the same for an asker (fleet.yaml askers, not a decider)
TOTAL_BYTES = 256 * 1024 * 1024
DECIDER_RESERVE = 64 * 1024 * 1024      # never filled by askers' images
PENDING_MS = 60 * 60 * 1000             # uploaded but never sent: gone after an hour
MAX_AGE_MS = 14 * 24 * 3600 * 1000      # every image: gone after two weeks
DAY_MS = 24 * 3600 * 1000
MAX_NAME = 100
ID_RE = re.compile(r"u-[0-9]{1,16}-[0-9a-f]{16}")
# the type an image is stored as (from its bytes) -> its extension
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
TYPES = tuple(EXT)
NAME_STRIP_RE = re.compile("[\x00-\x1f\x7f-\x9f\u00a0\u061c\u180e\u2028\u2029\u200b-\u200f\u202a-\u202e"
                           "\u2060-\u2069\ufeff]+")
LOCK = ".lock"
LOCK_TIMEOUT_S = 5.0        # waiting longer for uploads/.lock: Bad("busy", 503), never a hang


class Bad(Exception):
    """A refused upload or attachment: code is a fixed word, detail fixed text (never file data)."""

    def __init__(self, code, detail, status=400):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


def now_ms():
    return int(time.time() * 1000)


def sniff(data):
    """The image type of DATA from its first bytes, or None."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff" and len(data) >= 4:
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and data[12:16] in (b"VP8 ", b"VP8L", b"VP8X"):
        return "image/webp"
    return None


def clean_name(raw):
    """The client's file name as display text: percent-decoded, the last path part, no control,
    zero-width or bidi characters, one line, at most MAX_NAME characters; "image" when nothing is
    left. Never used in a path."""
    try:
        s = urllib.parse.unquote(str(raw or ""), errors="replace")
    except Exception:  # noqa: BLE001
        s = ""
    s = re.split(r"[/\\]", unicodedata.normalize("NFC", s))[-1]
    s = re.sub(r"\s+", " ", NAME_STRIP_RE.sub(" ", s)).strip(" .")
    if len(s) > MAX_NAME:
        s = s[:MAX_NAME - 1] + "…"
    try:
        import guard  # noqa: WPS433
        s = guard.scrub(s)
    except Exception:  # noqa: BLE001
        pass
    return s or "image"


def udir(home):
    """$BOARD_HOME/uploads, 0700; refused when it is a symlink or not a directory."""
    d = os.path.join(home, "uploads")
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise Bad("storage", "the uploads directory is not a directory", 500)
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)
    return d


class Locked:
    """flock on uploads/.lock for a change to the directory (board-serve and the CLI alike)."""

    def __init__(self, home):
        self.path = os.path.join(udir(home), LOCK)
        self.fd = None

    def __enter__(self):
        """Wait at most LOCK_TIMEOUT_S: a holder that stopped (a suspended CLI) must never freeze
        board-serve, which waits here holding its write lock. Raises Bad("busy", 503)."""
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise Bad("busy", "the image store is busy (another board command holds its lock); "
                                      "try again in a moment", 503) from None
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def _meta_path(home, uid):
    if not ID_RE.fullmatch(str(uid or "")):
        raise Bad("bad_attachments", "invalid upload id")
    return os.path.join(home, "uploads", uid + ".json")


def path_for(home, att):
    """The absolute path of an attachment's image ({id, type}), built from the id and the type
    only; None when either is not valid."""
    uid, ext = str((att or {}).get("id") or ""), EXT.get((att or {}).get("type"))
    if not ID_RE.fullmatch(uid) or not ext:
        return None
    return os.path.abspath(os.path.join(home, "uploads", "%s.%s" % (uid, ext)))


def read_meta(home, uid):
    try:
        with open(_meta_path(home, uid), encoding="utf-8") as fh:
            m = json.load(fh)
        return m if isinstance(m, dict) and m.get("id") == uid else None
    except (OSError, ValueError):
        return None


def _write_meta(home, meta):
    path = _meta_path(home, meta["id"])
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    os.replace(tmp, path)


def all_meta(home):
    out = []
    try:
        names = os.listdir(os.path.join(home, "uploads"))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and ID_RE.fullmatch(n[:-5]):
            m = read_meta(home, n[:-5])
            if m:
                out.append(m)
    return out


def _remove(home, meta):
    p = path_for(home, meta)
    for f in ([p] if p else []) + [os.path.join(home, "uploads", meta["id"] + ".json")]:
        try:
            os.unlink(f)
        except OSError:
            pass


def prune(home, now=None):
    """Delete images no message carries after PENDING_MS, every image after MAX_AGE_MS, and stray
    files (no sidecar, temp files) after PENDING_MS. Returns how many images were deleted."""
    if not os.path.isdir(os.path.join(home, "uploads")):
        return 0
    with Locked(home):
        return _prune(home, now)


def _prune(home, now=None):
    now = now_ms() if now is None else now
    d = os.path.join(home, "uploads")
    if not os.path.isdir(d):
        return 0
    gone, keep = 0, set()
    for m in all_meta(home):
        age = now - int(m.get("created_at") or 0)
        if age > MAX_AGE_MS or (not m.get("messages") and age > PENDING_MS):
            _remove(home, m)
            gone += 1
        else:
            keep.add(m["id"] + ".json")
            p = path_for(home, m)
            if p:
                keep.add(os.path.basename(p))
    for n in os.listdir(d):
        p = os.path.join(d, n)
        if n in keep or n == LOCK:      # exactly a kept image and its sidecar: a temp file is stray
            continue
        try:
            st = os.lstat(p)
            if now - int(st.st_mtime * 1000) > PENDING_MS and not stat.S_ISDIR(st.st_mode):
                os.unlink(p)
        except OSError:
            pass
    return gone


def check_quota(home, by, size, now=None, asker=False, asker_of=None):
    """Raise Bad when storing SIZE more bytes for BY would pass a cap (429 for the uploader's own
    caps, 507 when the disk budget is spent). asker: BY is an asker (a smaller share, and askers
    together never fill DECIDER_RESERVE). asker_of(by) -> bool: who is an asker NOW (fleet.yaml),
    for the askers' pool; without it, the flag stored at upload time (a decider demoted to asker
    since would escape the pool)."""
    now = now_ms() if now is None else now
    metas = all_meta(home)
    roles = {}

    def in_pool(m):
        if asker_of is None:
            return bool(m.get("asker"))
        b = m.get("by")
        if b not in roles:
            roles[b] = bool(asker_of(b))
        return roles[b]
    mine = [m for m in metas if m.get("by") == by]
    held = ASKER_HELD_BYTES if asker else USER_HELD_BYTES
    if sum(1 for m in mine if not m.get("messages")) >= MAX_PENDING:
        raise Bad("too_many_pending", "at most %d images waiting to be sent; send or remove some first"
                  % MAX_PENDING, 429)
    if sum(int(m.get("size") or 0) for m in mine if now - int(m.get("created_at") or 0) < DAY_MS) + size \
            > USER_DAY_BYTES:
        raise Bad("quota", "at most %d MiB of images a day" % (USER_DAY_BYTES // (1024 * 1024)), 429)
    if sum(int(m.get("size") or 0) for m in mine) + size > held:
        raise Bad("quota", "at most %d MiB of your images are kept at a time; older ones go after %d days"
                  % (held // (1024 * 1024), MAX_AGE_MS // DAY_MS), 429)
    if asker and sum(int(m.get("size") or 0) for m in metas if in_pool(m)) + size > TOTAL_BYTES - DECIDER_RESERVE:
        raise Bad("storage_full", "the board's image storage for askers is full; try again later", 507)
    if sum(int(m.get("size") or 0) for m in metas) + size > TOTAL_BYTES:
        raise Bad("storage_full", "the board's image storage is full; try again later", 507)


def precheck(home, by, size, asker=False, asker_of=None):
    """board-serve, before it reads an upload's body: prune, then the caps (raises Bad)."""
    with Locked(home):
        _prune(home)
        check_quota(home, by, size, asker=asker, asker_of=asker_of)


def store(home, data, name, by, now=None, asker=False, asker_of=None):
    """Check and store one image from BY (asker: BY is an asker, for the caps). Returns its public
    record {id, name, size, type}."""
    now = now_ms() if now is None else now
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise Bad("empty", "the image is empty")
    if len(data) > MAX_BYTES:
        raise Bad("too_large", "an image is at most %d MiB" % (MAX_BYTES // (1024 * 1024)), 413)
    ctype = sniff(data)
    if not ctype:
        raise Bad("not_image", "only PNG, JPEG, GIF and WebP images can be attached", 415)
    d = udir(home)
    with Locked(home):
        _prune(home, now)
        check_quota(home, by, len(data), now, asker=asker, asker_of=asker_of)
        return _store(home, d, data, ctype, name, by, now, asker)


def _store(home, d, data, ctype, name, by, now, asker):
    uid = "u-%d-%s" % (now, secrets.token_hex(8))
    path = os.path.join(d, "%s.%s" % (uid, EXT[ctype]))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(bytes(data))
        meta = {"id": uid, "by": by, "name": clean_name(name), "size": len(data), "type": ctype,
                "created_at": now, "messages": []}
        if asker:
            meta["asker"] = True
        _write_meta(home, meta)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return public(meta)


def public(meta):
    return {"id": meta["id"], "name": str(meta.get("name") or "image"), "size": int(meta.get("size") or 0),
            "type": meta.get("type")}


def resolve(home, ids, by):
    """The attachments of a new message: IDS (a list of 1..MAX_PER_MESSAGE distinct upload ids)
    must each be an image BY uploaded and that is still on disk. Returns their public records, in
    order; raises Bad("bad_attachments") otherwise (an unknown id and someone else's look alike)."""
    if ids is None:
        return []
    if not isinstance(ids, list) or len(ids) > MAX_PER_MESSAGE:
        raise Bad("bad_attachments", "attachments must be a list of at most %d upload ids" % MAX_PER_MESSAGE)
    if len(set(map(str, ids))) != len(ids):
        raise Bad("bad_attachments", "an upload id is listed twice")
    out = []
    for uid in ids:
        if not isinstance(uid, str) or not ID_RE.fullmatch(uid):
            raise Bad("bad_attachments", "invalid upload id")
        m = read_meta(home, uid)
        p = path_for(home, m) if m else None
        if not m or m.get("by") != by or not p or not os.path.isfile(p):
            raise Bad("bad_attachments", "no such upload of yours: upload the image again")
        out.append(public(m))
    return out


def mark_attached(home, ids, mid, now=None):
    """Record that message MID carries these uploads (so prune keeps them for MAX_AGE_MS)."""
    if not ids:
        return
    with Locked(home):
        for uid in ids:
            m = read_meta(home, uid)
            if not m:
                continue
            msgs = [x for x in m.get("messages") or [] if isinstance(x, str)]
            if mid not in msgs:
                msgs.append(mid)
            m.update({"messages": msgs, "attached_at": now_ms() if now is None else now})
            _write_meta(home, m)


def unmark(home, ids, mid):
    """Message MID no longer carries these uploads (a held message released into "declined"): one
    no other message carries is unsent again, and goes with the next prune (its created_at is old
    enough by then, or it waits out PENDING_MS)."""
    if not ids:
        return
    with Locked(home):
        for uid in ids:
            m = read_meta(home, uid)
            if not m or mid not in (m.get("messages") or []):
                continue
            m["messages"] = [x for x in m["messages"] if x != mid]
            _write_meta(home, m)


def delete_pending(home, uid, by):
    """The page's ✕: delete BY's own upload that no message carries yet. An invalid id → Bad 400
    bad_id; an unknown id and someone else's alike → 404 not_found; one a message carries → 409
    in_use (it stays until it ages out)."""
    if not isinstance(uid, str) or not ID_RE.fullmatch(uid):
        raise Bad("bad_id", "invalid upload id")
    if not os.path.isdir(os.path.join(home, "uploads")):
        raise Bad("not_found", "no such upload of yours", 404)
    with Locked(home):
        m = read_meta(home, uid)
        if not m or m.get("by") != by:
            raise Bad("not_found", "no such upload of yours", 404)
        if m.get("messages"):
            raise Bad("in_use", "a message carries this image; it is deleted when it ages out", 409)
        _remove(home, m)


def discard(home, ids):
    """Delete these uploads outright (the CLI, when a later --attach of the same command failed)."""
    if not ids:
        return
    with Locked(home):
        for uid in ids:
            m = read_meta(home, uid)
            if m and not m.get("messages"):
                _remove(home, m)


def human_size(n):
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.0f KB" % (n / 1024)
    return "%.1f MB" % (n / (1024 * 1024))
