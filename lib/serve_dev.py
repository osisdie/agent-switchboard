#!/usr/bin/env python3
"""serve_dev: board-serve's developer portal (docs/contract.md "Developer portal (/dev)").

  GET  /dev, /dev/keys           artifact/dev.html, served like the board page: the same identity
                                 gate (local listener, or a verified Access email on the tunnel), a
                                 nonce CSP, no-store
  GET  /api/dev                  what the portal renders: the live OpenAPI document, the MCP tool
                                 table, this listener's base URL and whether it is the tunnel, and
                                 the caller's key roles (X-Board: 1, no cross-site Sec-Fetch-Site)
  GET  /api/apikeys              the keys this person may see (`board apikey ls --all`): owners all,
                                 everyone else their own (same X-Board / Sec-Fetch-Site checks)
  POST /api/apikeys              {"action": "create"|"request", "name", "owner"?, "scopes"?, "expires"?}
  POST /api/apikeys/<kid>        {"action": "approve"|"revoke", "scopes"?, "expires"?}
                                 (X-Board: 1 and this board's own Origin, as every board POST)

Every key change runs `board apikey ... --by board:<identity>`, so lib/apikeys.py decides who may do
what and audits every change and every refusal exactly as on the command line. Nothing here
re-implements those rules. A new key (create, request) is returned in that one response and never
again; it is never logged, stored or audited here (the CLI audits the action without it).

The key routes take the board's session identity only. A request that carries an Authorization
header is refused (403 api_key_refused, audited): an API key can never manage keys, whatever it is
sent with. Non-owners get other people's emails as role labels (lib/pii.py), as on the board.
"""
import json
import os
import re

import api_spec
import apikeys
import fleet

PAGE = os.path.join(fleet.REPO, "artifact", "dev.html")
PAGES = ("/dev", "/dev/", "/dev/keys")
INFO = "/api/dev"
KEYS = "/api/apikeys"
KEY_RE = re.compile(r"/api/apikeys/([0-9a-z]{12})")
OWN_CHARSET_RE = re.compile(r"\A<meta charset=\"utf-8\">[^\n]*\n")
EXPIRES_RE = re.compile(r"[0-9]{1,4}d|[0-9]{4}-[0-9]{2}-[0-9]{2}")
EXIT_STATUS = {1: (409, "refused"), 2: (400, "bad_request"), 4: (403, "not_authorized"), 8: (409, "busy")}


def fetch_checked(path):
    """GET routes that only the portal's own fetch may call (Handler.check_fetch, before identity)."""
    return path in (INFO, KEYS)


def is_get(path):
    return path in PAGES or path in (INFO, KEYS)


def is_post(path):
    return path == KEYS or KEY_RE.fullmatch(path) is not None


def get(h, S, path, ident, via):
    if path in PAGES:
        return page(h, S)
    if path == INFO:
        return h.send_json(200, info(h, S, ident, via))
    return keys_list(h, S, ident, via)


def post(h, S, path, ident, via):
    m = KEY_RE.fullmatch(path)
    try:
        # before the body: a request that carries a key is refused whatever else is wrong with it
        refuse_bearer(h, S, ident, via, "manage", m.group(1) if m else None)
        body = h.read_body()
        if m:
            return key_act(h, S, m.group(1), body, ident, via)
        return key_new(h, S, body, ident, via)
    except Bad as exc:
        return h.refuse(S.Refused(400, "bad_body", str(exc)))
    except S.Refused as exc:
        return h.refuse(exc)


class Bad(Exception):
    """A body the key routes do not accept (400 bad_body); the CLI's own usage errors are exit 2."""


# ------------------------------------------------------------------ the page
def render(S, nonce):
    with open(PAGE, "rb") as fh:
        raw = fh.read()
    body = OWN_CHARSET_RE.sub("", raw.decode("utf-8"), count=1)
    body = body.replace("<script>", '<script nonce="%s">' % nonce)
    head = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<meta name="board-mode" content="local">'
            '<meta name="referrer" content="no-referrer">'
            '<link rel="icon" href="/favicon.png" sizes="32x32" type="image/png">'
            '<style nonce="%s">%s</style>\n' % (nonce, S.RESET_CSS))
    return (head + body + "\n</html>\n").encode("utf-8")


def page(h, S):
    nonce = S.secrets.token_urlsafe(16)
    data = render(S, nonce)
    h.send_response(200)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Content-Security-Policy", S.csp(nonce))
    h.common_headers()
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(data)


# ------------------------------------------------------------------ GET /api/dev
def base_url(h):
    if h.server.kind == "tunnel":
        return "https://" + h.server.cfg["hostname"]
    return "http://127.0.0.1:%d" % h.server.server_address[1]


def key_roles(cfg, ident):
    r = apikeys.roles(cfg, ident)
    return {"key_owner": "owner" in r, "can_create": bool(r), "can_approve": "owner" in r}


def snippets(base, tunnel, board_mcp, protocol):
    """Copyable connection snippets for this board. Secrets stay environment placeholders
    ($BOARD_API_KEY, and on the tunnel $CF_ACCESS_CLIENT_ID / $CF_ACCESS_CLIENT_SECRET); the
    Cloudflare Access headers appear only when the board is reached through the tunnel."""
    cf = ['CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID', 'CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET'] \
        if tunnel else []
    hdrs = ['Authorization: Bearer $BOARD_API_KEY'] + cf
    out = []
    out.append({"id": "claude-code-http", "title": "Claude Code (HTTP)", "lang": "bash", "text": "\n".join(
        ["claude mcp add --transport http board %s/mcp \\" % base]
        + ['  --header "%s"%s' % (x, " \\" if i < len(hdrs) - 1 else "") for i, x in enumerate(cf + hdrs[:1])])})
    if board_mcp:
        out.append({"id": "claude-code-stdio", "title": "Claude Code (stdio, on this machine)", "lang": "bash",
                    "text": 'claude mcp add board -e BOARD_API_KEY="$BOARD_API_KEY" -- "%s"' % board_mcp})
    env = {"BOARD_API_URL": base, "BOARD_API_KEY": "<BOARD_API_KEY>"}
    if tunnel:
        env.update({"CF_ACCESS_CLIENT_ID": "<CF_ACCESS_CLIENT_ID>", "CF_ACCESS_CLIENT_SECRET": "<CF_ACCESS_CLIENT_SECRET>"})
    desktop = {"mcpServers": {"board": {"command": board_mcp or "<path to the clone>/bin/board-mcp", "env": env}}}
    out.append({"id": "claude-desktop", "title": "Claude Desktop (stdio, claude_desktop_config.json)", "lang": "json",
                "text": json.dumps(desktop, indent=2),
                "note": "Claude Desktop does not expand variables here: replace each <...> with your own value, "
                        "in your local config only."})
    out.append({"id": "curl", "title": "curl", "lang": "bash", "text": "\n".join(
        ["curl -s \\"] + ['  -H "%s" \\' % x for x in hdrs] + ['  "%s/api/v1/whoami"' % base])})
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": protocol, "capabilities": {}, "clientInfo": {"name": "curl", "version": "1"}}},
        separators=(",", ":"))
    lst = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, separators=(",", ":"))
    call = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "list_sessions", "arguments": {}}}, separators=(",", ":"))
    health = ["# Health check: needs bash, curl and jq. The key (and the Access secrets) come from your",
              "# environment; the output is names and codes only.",
              "B=%s" % base,
              "H=(" + "\n   ".join('-H "%s"' % x for x in hdrs) + ")",
              "J=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')",
              "# 1. whoami: the key's name and scopes",
              "curl -s \"${H[@]}\" \"$B/api/v1/whoami\" | jq -c '{name, scopes, error}'",
              "# 2. MCP initialize",
              "curl -s \"${H[@]}\" \"${J[@]}\" -d '%s' \"$B/mcp\" \\" % init,
              "  | jq -c '{server: .result.serverInfo.name, protocol: .result.protocolVersion, error}'",
              "# 3. tools/list: the tools this key's scopes allow",
              "curl -s \"${H[@]}\" \"${J[@]}\" -d '%s' \"$B/mcp\" | jq -c '[.result.tools[]?.name]'" % lst,
              "# 4. one read tool (needs read:state)",
              "curl -s \"${H[@]}\" \"${J[@]}\" -d '%s' \"$B/mcp\" \\" % call,
              "  | jq -c '{isError: .result.isError, sessions: [.result.structuredContent.sessions[]?.code], error}'"]
    out.append({"id": "health", "title": "Health check", "lang": "bash", "text": "\n".join(health)})
    return out


def info(h, S, ident, via):
    import mcp_tools   # only when the portal asks
    import serve_api
    cfg = fleet.load_fleet()
    st = serve_api.api_settings(cfg)
    tunnel = h.server.kind == "tunnel"
    tools = [{"name": t["name"], "title": t["title"], "description": t["description"], "scope": t["scope"],
              "method": t.get("method", "GET"), "path": t["path"], "required": list(t["required"]),
              "read_only": t["annotations"]["readOnlyHint"], "destructive": t["annotations"]["destructiveHint"]}
             for t in mcp_tools.TOOLS]
    resources = [{"uri": r.get("uri") or r.get("uriTemplate"), "title": r["title"], "scope": r["scope"]}
                 for r in mcp_tools.RESOURCES + mcp_tools.TEMPLATES]
    me = {"identity": ident, "via": "access" if via == "cf-jwt" else "local"}
    me.update(key_roles(cfg, ident))
    out = {"base_url": base_url(h), "listener": "tunnel" if tunnel else "local",
           "api": {"enabled": st["enabled"], "available": st["enabled"] and (st["remote"] or not tunnel)},
           # the stdio client's path is this machine's: useful on the local listener only
           "board_mcp": os.path.join(fleet.REPO, "bin", "board-mcp") if not tunnel else None,
           "mock": None,          # the mock board and its try-it console come later (A4)
           "openapi": api_spec.openapi(),
           "snippets": snippets(base_url(h), tunnel, os.path.join(fleet.REPO, "bin", "board-mcp") if not tunnel
                                else None, mcp_tools.PROTOCOL_VERSIONS[0]),
           "mcp": {"path": "/mcp", "protocol_versions": list(mcp_tools.PROTOCOL_VERSIONS),
                   "tools": tools, "resources": resources},
           "scopes": list(apikeys.SCOPES), "default_scopes": list(apikeys.DEFAULT_SCOPES),
           "expiry": {"default_days": apikeys.DEFAULT_DAYS, "max_days": apikeys.MAX_DAYS},
           "me": me}
    return h.viewer_mask(out, ident, via, cfg)


# ------------------------------------------------------------------ the key routes
def audit_refusal(h, S, ident, via, action, reason, kid=None):
    S.append_event(h.server.home, {"ts": S.now_ms(), "type": "apikey", "action": action, "by": ident,
                                   "via": "board", "listener": via, "key_id": kid, "result": "refused",
                                   "reason": reason})


def refuse_bearer(h, S, ident, via, action, kid=None):
    """An API key never manages keys: a key-route request that carries one is refused outright."""
    if h.headers.get_all("Authorization"):
        audit_refusal(h, S, ident, via, action, "api_key_refused", kid)
        raise S.Refused(403, "api_key_refused", "API keys cannot manage keys; use the board while signed in")


def run(h, S, ident, via, args):
    """`board apikey <args> --by board:<ident>`: (stdout) or raise Refused with the CLI's reason."""
    rc, out, err = h.board("apikey", *args, "--by", "board:" + ident)
    if rc != 0:
        line = (err or "failed").strip().splitlines()[-1] if (err or "").strip() else "failed"
        line = re.sub(r"^board apikey [a-z]+: ", "", line)
        cfg = fleet.load_fleet()
        detail = h.viewer_mask(S.guard.scrub(line)[:300], ident, via, cfg)
        status, error = EXIT_STATUS.get(rc, (500, "internal"))
        if rc == 1 and detail.startswith("no such key"):
            status, error = 404, "not_found"
        raise S.Refused(status, error, detail)
    return out


def keys_list(h, S, ident, via):
    try:
        refuse_bearer(h, S, ident, via, "ls")
        out = run(h, S, ident, via, ["ls", "--all", "--json"])
    except S.Refused as exc:
        return h.refuse(exc)
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        rows = []
    rows = [r for r in rows if isinstance(r, dict)]
    rows.sort(key=lambda r: (-(r.get("created_at") or 0), r.get("id") or ""))
    cfg = fleet.load_fleet()
    me = key_roles(cfg, ident)
    resp = {"keys": rows, "me": dict(me, identity=ident), "scopes": list(apikeys.SCOPES),
            "default_scopes": list(apikeys.DEFAULT_SCOPES)}
    if me["key_owner"]:
        resp["listed"] = sorted(apikeys.listed(cfg))      # owners only: who a key may be issued to
    return h.send_json(200, h.viewer_mask(resp, ident, via, cfg))


def _str(body, name, required=False, limit=200):
    v = body.get(name)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise Bad("%s must be a string of 1 to %d characters" % (name, limit))
    return v.strip()


def _scopes(body):
    v = body.get("scopes")
    if v is None:
        return None
    if not isinstance(v, list) or not 1 <= len(v) <= len(apikeys.SCOPES) or \
            not all(isinstance(s, str) and s in apikeys.SCOPES for s in v):
        raise Bad("scopes: a list of: %s" % ", ".join(apikeys.SCOPES))
    return ",".join(dict.fromkeys(v))


def _expires(body):
    v = body.get("expires")
    if v is None:
        return None
    if not isinstance(v, str) or not EXPIRES_RE.fullmatch(v):
        raise Bad("expires: Nd (days) or YYYY-MM-DD")
    return v


def _fields(body, allowed):
    extra = sorted(set(body) - set(allowed))
    if extra:
        raise Bad("unknown field %s" % extra[0][:40])


def key_new(h, S, body, ident, via):
    action = body.get("action")
    if action not in ("create", "request"):
        raise Bad("action: create or request")
    _fields(body, ("action", "name", "owner", "scopes", "expires") if action == "create"
            else ("action", "name", "scopes", "expires"))
    args = [action, "--name", _str(body, "name", True, 120)]
    if action == "create":
        args += ["--owner", _str(body, "owner", True, 254)]
    scopes, expires = _scopes(body), _expires(body)
    if scopes:
        args += ["--scopes", scopes]
    if expires:
        args += ["--expires", expires]
    with h.server.write_lock:
        out = run(h, S, ident, via, args + ["--json"])
    try:
        rec = json.loads(out)
    except ValueError:
        rec = None
    if not isinstance(rec, dict) or not isinstance(rec.get("key"), str):
        raise S.Refused(500, "internal", "the key store gave no key")
    cfg = fleet.load_fleet()
    shown = {k: rec.get(k) for k in ("id", "name", "owner", "scopes", "expires_at", "status")}
    shown = h.viewer_mask(shown, ident, via, cfg)
    # The key itself: in this one response only. Never masked (it holds no email), logged or kept.
    shown["key"] = rec["key"]
    shown["shown_once"] = True
    return h.send_json(201, shown)


def key_act(h, S, kid, body, ident, via):
    action = body.get("action")
    if action not in ("approve", "revoke"):
        raise Bad("action: approve or revoke")
    _fields(body, ("action", "scopes", "expires") if action == "approve" else ("action",))
    args = [action, kid]
    if action == "approve":
        scopes, expires = _scopes(body), _expires(body)
        if scopes:
            args += ["--scopes", scopes]
        if expires:
            args += ["--expires", expires]
    with h.server.write_lock:
        out = run(h, S, ident, via, args)
    cfg = fleet.load_fleet()
    return h.send_json(200, h.viewer_mask({"id": kid, "action": action, "result": "ok",
                                           "detail": S.guard.scrub(out.strip().splitlines()[-1] if out.strip() else "")[:300]},
                                          ident, via, cfg))
