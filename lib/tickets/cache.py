"""The ticket cache: $BOARD_HOME/tickets/cache/<provider>__<key>.json (0600, atomic writes).

A fetched ticket is served for `tickets.cache_ttl_seconds` (60); a 404 is remembered for
`tickets.not_found_ttl_seconds` (30). Only finalized tickets are stored (every string already
redacted). Files older than 7 days are removed on write, unless tickets/links/ names the same file
(links come with ticket take)."""
import glob
import json
import os
import re
import threading
import time

from . import PURGE_AFTER

NOT_FOUND = object()


def safe_part(s):
    return re.sub(r"[^A-Za-z0-9._-]", lambda m: "%%%02X" % ord(m.group(0)), str(s))[:160]


class Cache:
    def __init__(self, home, now=time.time):
        self.home = home
        self.dir = os.path.join(home, "tickets", "cache")
        self.links = os.path.join(home, "tickets", "links")
        self.now = now

    def path(self, prov, key):
        return os.path.join(self.dir, "%s__%s.json" % (safe_part(prov), safe_part(key)))

    def get(self, prov, key, ttl, not_found_ttl):
        try:
            with open(self.path(prov, key), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        if doc.get("provider") != prov or doc.get("key") != key:
            return None     # another (provider, key) that maps to the same file name
        age = self.now() * 1000 - (doc.get("fetched_at") or 0)
        if doc.get("not_found"):
            return NOT_FOUND if 0 <= age < not_found_ttl * 1000 else None
        if 0 <= age < ttl * 1000:
            return doc
        return None

    def _write(self, path, doc):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
        self.purge()

    def put(self, prov, key, ticket):
        self._write(self.path(prov, key), ticket)

    def put_not_found(self, prov, key):
        self._write(self.path(prov, key), {"provider": prov, "key": key, "not_found": True,
                                           "fetched_at": int(self.now() * 1000)})

    def invalidate(self, prov, key):
        try:
            os.remove(self.path(prov, key))
        except OSError:
            pass

    def purge(self):
        cutoff = self.now() - PURGE_AFTER
        for p in glob.glob(os.path.join(self.dir, "*.json")):
            try:
                if os.stat(p).st_mtime < cutoff and not os.path.exists(os.path.join(self.links, os.path.basename(p))):
                    os.remove(p)
            except OSError:
                pass
