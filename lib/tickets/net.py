"""The trackers' HTTP client (stdlib urllib), built against SSRF and token leaks.

  - https only; plain http only for 127.0.0.1 / localhost (a local tracker or a test server);
  - the host is pinned to the tracker's base_url: a request URL is base_url + a path template
    whose values are single percent-encoded segments, and the result is checked again (scheme,
    host and port) before anything is sent;
  - redirects are never followed (a 3xx is an upstream error), so the token never travels to
    another host;
  - no proxy for 127.0.0.1 / localhost; 10 s timeout; a response body over 1 MiB is refused;
  - the token goes in one header only, and a value with CR/LF is refused.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import fleet  # noqa: E402  (lib/ is on sys.path: see tickets/__init__.py)

from . import TicketError, tpl, warn

TIMEOUT_S = 10
MAX_BODY = 1024 * 1024
ERR_CLIP = 300


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TicketError("ticket_upstream", "the tracker's answer is not JSON") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # urllib then raises HTTPError(3xx): never followed


def check_base(raw):
    """(base, None) or (None, reason): https, or http on 127.0.0.1/localhost; no user, query or fragment."""
    return fleet.check_board_url(raw)


def build_url(base, path_tpl, ctx, query=None):
    try:
        path = tpl.fill_path(path_tpl, ctx)
        q = [(str(k), tpl.fill_text(v, ctx)) for k, v in (query or {}).items()]
    except tpl.TemplateError as exc:
        raise TicketError("ticket_upstream", "cannot build the request: %s" % exc) from exc
    url = base + path + ("?" + urllib.parse.urlencode(q) if q else "")
    b, u = urllib.parse.urlsplit(base), urllib.parse.urlsplit(url)
    if (u.scheme, u.hostname, u.port, u.username) != (b.scheme, b.hostname, b.port, None) \
            or not u.path.startswith(b.path) or u.fragment:
        raise TicketError("ticket_upstream", "the request would leave the tracker's host")
    return url


def request(base, method, url, headers, body=None, timeout=TIMEOUT_S, opener=None):
    """One HTTP exchange. Returns a Response for 2xx and 4xx/5xx statuses the caller maps; raises
    TicketError for a redirect, a network failure or an oversized body."""
    host = urllib.parse.urlsplit(base).hostname
    for k, v in headers.items():
        if any(c in str(k) + str(v) for c in "\r\n\0"):
            raise TicketError("ticket_unconfigured", "a request header contains a line break")
    if opener is None:
        handlers = [_NoRedirect()]
        if host in fleet.LOCAL_HOSTS:
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "agent-switchboard-tickets"}
    hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        resp = opener.open(req, timeout=timeout)
        status = resp.status
    except urllib.error.HTTPError as exc:
        resp, status = exc, exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        warn("%s %s: %s" % (method, url, getattr(exc, "reason", exc)))
        raise TicketError("ticket_upstream", "the tracker is unreachable") from exc
    try:
        if 300 <= status < 400:
            warn("%s %s: redirect to another location refused (HTTP %d)" % (method, url, status))
            raise TicketError("ticket_upstream", "the tracker answered with a redirect (not followed)")
        try:
            raw = resp.read(MAX_BODY + 1)
        except (OSError, ValueError) as exc:
            raise TicketError("ticket_upstream", "the tracker's answer was cut off") from exc
        if len(raw) > MAX_BODY:
            raise TicketError("ticket_upstream", "the tracker's answer is over 1 MiB")
        return Response(status, resp.headers, raw)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def log_error_body(name, resp):
    """The upstream error body, clipped and redacted, to stderr only."""
    text = resp.body[:ERR_CLIP].decode("utf-8", "replace")
    warn("%s: HTTP %d: %s" % (name, resp.status, text.replace("\n", " ")))
