"""Per-tracker rate limits: a token bucket (rate_per_min, default 30), the tracker's own 429
Retry-After, and a circuit breaker (5 failures in a row -> 60 s open).

The bucket lives in this process (board-serve keeps one per tracker across requests). The 429
backoff and the breaker are also kept in $BOARD_HOME/tickets/state/<tracker>.json, so one-shot
`board ticket get` calls respect them too."""
import json
import math
import os
import threading
import time

from . import TicketError

BREAKER_FAILURES = 5
BREAKER_OPEN_S = 60
MAX_BACKOFF_S = 3600

_BUCKETS = {}
_LOCK = threading.Lock()


class Limiter:
    def __init__(self, home, name, rate_per_min, now=time.time, mono=time.monotonic):
        self.home, self.name = home, name
        self.rate = max(1, int(rate_per_min))
        self.now, self.mono = now, mono
        self.path = os.path.join(home, "tickets", "state", "%s.json" % name)
        with _LOCK:
            b = _BUCKETS.get((home, name))
            if b is None or b["rate"] != self.rate:
                b = {"rate": self.rate, "tokens": float(self.rate), "at": mono(), "lock": threading.Lock()}
                _BUCKETS[(home, name)] = b
        self.bucket = b

    # -- persisted state
    def _state(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d):
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            tmp = "%s.tmp.%d.%d" % (self.path, os.getpid(), threading.get_ident())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- checks
    def before(self):
        """Raises TicketError when a request must not go out now."""
        st, now = self._state(), self.now()
        if (st.get("blocked_until") or 0) > now:
            wait = math.ceil(st["blocked_until"] - now)
            raise TicketError("ticket_rate_limited", "%s asked us to wait" % self.name, retry_after=wait)
        if (st.get("open_until") or 0) > now:
            wait = math.ceil(st["open_until"] - now)
            raise TicketError("ticket_upstream", "%s is failing; paused for %d s" % (self.name, wait), retry_after=wait)
        b = self.bucket
        with b["lock"]:
            t = self.mono()
            b["tokens"] = min(float(self.rate), b["tokens"] + (t - b["at"]) * self.rate / 60.0)
            b["at"] = t
            if b["tokens"] < 1.0:
                wait = math.ceil((1.0 - b["tokens"]) * 60.0 / self.rate)
                raise TicketError("ticket_rate_limited", "over rate_per_min for %s" % self.name, retry_after=wait)
            b["tokens"] -= 1.0

    def success(self):
        st = self._state()
        if st.get("failures"):
            st["failures"] = 0
            self._save(st)

    def failure(self):
        st = self._state()
        st["failures"] = int(st.get("failures") or 0) + 1
        if st["failures"] >= BREAKER_FAILURES:
            st["open_until"] = self.now() + BREAKER_OPEN_S
            st["failures"] = 0
        self._save(st)

    def throttled(self, retry_after):
        st = self._state()
        st["blocked_until"] = self.now() + max(1, min(MAX_BACKOFF_S, retry_after))
        self._save(st)


def parse_retry_after(v, now=time.time, default=60):
    """Seconds from a Retry-After header: an integer, or an HTTP date."""
    v = str(v or "").strip()
    if v.isdigit():
        return min(MAX_BACKOFF_S, int(v))
    if v:
        import email.utils  # noqa: WPS433
        try:
            t = email.utils.parsedate_to_datetime(v).timestamp()
            return max(0, min(MAX_BACKOFF_S, math.ceil(t - now())))
        except (TypeError, ValueError, OverflowError):
            pass
    return default
