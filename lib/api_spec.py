#!/usr/bin/env python3
"""api_spec: the single source of /api/v1 (docs/contract.md "Public API (/api/v1)").

ROUTES is the route table. lib/serve_api.py dispatches from it, and GET /api/v1/openapi.json is
generated from it (`openapi()`), so the router and the published spec cannot drift apart
(tests/test_api_v1.py checks that they are equal).

Each route: method, path template ({name} segments, each checked against PARAMS), the scope the
key needs (None: any valid key; "public": no key, only the listener's own checks), the handler
name on serve_api.Api, the query parameters it accepts (anything else is a 400), a summary, the
JSON Schema of its response (status `ok`, default 200) and, for a POST, of its JSON body (`body`;
unknown fields are refused). Writes never carry authority: anything that needs an approver (a
ticket take) only creates a pending board decision.

CLI: api_spec.py openapi    print the OpenAPI document
"""
import json
import re
import sys

VERSION = "1.0.0"
PREFIX = "/api/v1"

# Path parameters: the only values a {name} segment may take (fullmatch).
PARAMS = {
    "code": (r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", "A session code (fleet.yaml)."),
    "decision_id": (r"d-[0-9]+-[a-z0-9]+", "A decision id."),
    "task_id": (r"[A-Za-z0-9._-]{1,80}", "A task id."),
    "message_id": (r"m-[0-9]+-[a-z0-9]+", "A board message id."),
    "ref": (r"(?:[a-z][a-z0-9-]{0,31}:)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
            "A ticket reference: KEY, or provider:KEY."),
}
# Stages a key may move a task to (or create it in). deploy, production and maintain need a person.
API_STAGES = ("plan", "design", "build", "test")
STAGES = API_STAGES + ("deploy", "production", "maintain")
ARTIFACT_KEYS = ("intent", "spec", "plan", "evals", "review")
_CODE = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"}
_REF = {"type": "string", "minLength": 1, "maxLength": 200}
_GITREF = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,199}$"}   # tasks.REF_RE
QUERY = {
    "status": (r"pending|answered|relayed|stale|timeout|unauthorized|answered_in_pane",
               "Only decisions with this status."),
    "d": (r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "A day (YYYY-MM-DD) within the 14 kept days."),
    "before": (r"[0-9]{1,16}\.m-[0-9]+-[a-z0-9]+", "A cursor: `next` from the previous page."),
    "limit": (r"[1-9]|[1-4][0-9]|50", "Threads per page, 1..50 (default 20)."),
    "to": (r"all|[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", "Only messages to this session code (or to all)."),
    "q": (r"[^\x00-\x1f\x7f]{1,200}", "Text to look for (case-insensitive) in the messages and their replies."),
}

_OBJ = {"type": "object"}
_LIST = {"type": "array", "items": _OBJ}
ERROR = {"type": "object", "required": ["error"],
         "properties": {"error": {"type": "string"}, "detail": {"type": "string"}}}

ROUTES = [
    {"method": "GET", "path": "/api/v1/openapi.json", "scope": "public", "handler": "openapi", "tag": "meta",
     "summary": "This OpenAPI document (no key needed; the listener's own checks still apply).",
     "schema": _OBJ},
    {"method": "GET", "path": "/api/v1/whoami", "scope": None, "handler": "whoami", "tag": "meta",
     "summary": "The calling key: id, name, owner, scopes, mode.",
     "schema": {"type": "object", "required": ["key_id", "name", "owner", "scopes", "mode"],
                "properties": {"key_id": {"type": "string"}, "name": {"type": "string"},
                               "owner": {"type": "string"}, "scopes": {"type": "array", "items": {"type": "string"}},
                               "expires_at": {"type": "integer"}, "mode": {"enum": ["live", "mock"]}}}},
    {"method": "GET", "path": "/api/v1/state", "scope": "read:state", "handler": "state", "tag": "state",
     "summary": "The board state as a viewer sees it (no private card text, no account).", "schema": _OBJ},
    {"method": "GET", "path": "/api/v1/events", "scope": "read:state", "handler": "events", "tag": "state",
     "summary": "Server-Sent Events: `event: state` with the full state on every change; the key is checked "
                "again before every frame.", "schema": None, "content_type": "text/event-stream"},
    {"method": "GET", "path": "/api/v1/sessions", "scope": "read:state", "handler": "sessions", "tag": "state",
     "summary": "The sessions.", "schema": {"type": "object", "properties": {"sessions": _LIST}}},
    {"method": "GET", "path": "/api/v1/sessions/{code}", "scope": "read:state", "handler": "session",
     "tag": "state", "summary": "One session.", "schema": _OBJ},
    {"method": "GET", "path": "/api/v1/decisions", "scope": "read:decisions", "handler": "decisions",
     "tag": "decisions", "query": ["status"], "summary": "The latest 200 decisions, newest first.",
     "schema": {"type": "object", "properties": {"decisions": _LIST}}},
    {"method": "GET", "path": "/api/v1/decisions/{decision_id}", "scope": "read:decisions",
     "handler": "decision", "tag": "decisions", "summary": "One decision.", "schema": _OBJ},
    {"method": "GET", "path": "/api/v1/tasks", "scope": "read:tasks", "handler": "tasks", "tag": "tasks",
     "summary": "The latest 50 tasks, most recently updated first.",
     "schema": {"type": "object", "properties": {"tasks": _LIST}}},
    {"method": "GET", "path": "/api/v1/tasks/{task_id}", "scope": "read:tasks", "handler": "task",
     "tag": "tasks", "summary": "One task.", "schema": _OBJ},
    {"method": "GET", "path": "/api/v1/schedule", "scope": "read:tasks", "handler": "schedule", "tag": "tasks",
     "summary": "The scheduler's mode and the lessons in force.", "schema": _OBJ},
    {"method": "GET", "path": "/api/v1/pomodoro", "scope": "read:state", "handler": "pomodoro", "tag": "state",
     "summary": "Pomodoro tallies: today and the kept days.", "schema": _OBJ},
    {"method": "GET", "path": "/api/v1/pomodoro/day", "scope": "read:state", "handler": "pomodoro_day",
     "tag": "state", "query": ["d"], "query_required": ["d"], "summary": "One kept day's per-session breakdown.",
     "schema": _OBJ},
    {"method": "GET", "path": "/api/v1/messages", "scope": "read:messages", "handler": "messages",
     "tag": "messages", "query": ["before", "limit", "to", "q"],
     "summary": "Message threads, latest activity first, a page at a time (20 by default); `next` is the "
                "cursor for the following page (null at the end). Scrubbed; a held message stays held.",
     "schema": {"type": "object", "required": ["messages", "replies", "next"],
                "properties": {"messages": _LIST, "replies": _OBJ, "next": {"type": ["string", "null"]}}}},
    {"method": "GET", "path": "/api/v1/messages/{message_id}", "scope": "read:messages", "handler": "message",
     "tag": "messages", "summary": "One board message and its replies (scrubbed; a held message stays held).",
     "schema": _OBJ},
    # ---- writes (POST, JSON body <= 16 KiB, audited with the key's owner and name)
    {"method": "POST", "path": "/api/v1/tasks", "scope": "tasks:write", "handler": "task_new", "tag": "tasks",
     "summary": "Create a task (board task new) in %s (default plan); a later stage needs a person: 403 "
                "needs_approver." % ", ".join(API_STAGES), "ok": 201,
     "body": {"type": "object", "required": ["title", "owner"], "additionalProperties": False,
              "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 200}, "owner": _CODE,
                             "stage": {"enum": list(STAGES)}}},
     "schema": {"type": "object", "properties": {"id": {"type": "string"}}}},
    {"method": "POST", "path": "/api/v1/tasks/{task_id}/stage", "scope": "tasks:write", "handler": "task_stage",
     "tag": "tasks", "summary": "Move a task to a stage (board task stage). deploy, production and maintain "
                                "need a person: 403 needs_approver.",
     "body": {"type": "object", "required": ["stage"], "additionalProperties": False,
              "properties": {"stage": {"enum": list(STAGES)},
                             "artifacts": {"type": "object", "additionalProperties": _REF,
                                           "propertyNames": {"enum": list(ARTIFACT_KEYS)}}}},
     "schema": _OBJ},
    {"method": "POST", "path": "/api/v1/tasks/{task_id}/set", "scope": "tasks:write", "handler": "task_set",
     "tag": "tasks", "summary": "Set a task's scheduling fields (board task set).",
     "body": {"type": "object", "minProperties": 1, "additionalProperties": False,
              "properties": {"repo": _GITREF, "branch": _GITREF, "base": _GITREF, "size": {"enum": ["S", "M", "L"]},
                             "heavy": {"type": "boolean"}, "priority": {"type": "integer", "minimum": 0,
                                                                        "maximum": 3}}},
     "schema": _OBJ},
    {"method": "POST", "path": "/api/v1/tasks/{task_id}/progress", "scope": "tasks:write",
     "handler": "task_progress", "tag": "tasks", "summary": "Say the task is still moving (board task progress).",
     "body": {"type": "object", "maxProperties": 0}, "schema": _OBJ},
    {"method": "POST", "path": "/api/v1/tasks/{task_id}/pr", "scope": "tasks:write", "handler": "task_pr",
     "tag": "tasks", "summary": "Record the task's pull request (board task pr): open or closed. A key never "
                                "records merged: that unblocks dependent tasks and tells stacked sessions to "
                                "rebase, so it comes from the session or the harness.",
     "body": {"type": "object", "required": ["url", "state"], "additionalProperties": False,
              "properties": {"url": {"type": "string", "pattern": "^https?://", "maxLength": 500},
                             "state": {"enum": ["open", "closed"]},
                             "head": {"type": "string", "pattern": "^[0-9a-f]{7,64}$"},
                             "method": {"enum": ["squash", "merge", "rebase"]}}},
     "schema": _OBJ},
    {"method": "POST", "path": "/api/v1/messages", "scope": "write:messages", "handler": "message_new",
     "tag": "messages", "ok": 201,
     "summary": "Post a message to a session (or all). Screened by the secret guard (a secret request is held, "
                "never relayed); relayed as `[BOARD api <id>] via API key \"<name>\" (<owner>): …`, "
                "information only, never a [BOARD ask] or a decision.",
     "body": {"type": "object", "required": ["to", "body"], "additionalProperties": False,
              "properties": {"to": {"anyOf": [_CODE, {"const": "all"}]},
                             "body": {"type": "string", "minLength": 1, "maxLength": 4000}}},
     "schema": {"type": "object", "properties": {"id": {"type": "string"}, "status": {"enum": ["new", "held"]}}}},
    {"method": "POST", "path": "/api/v1/tickets/{ref}/take", "scope": "tickets:take", "handler": "ticket_take",
     "tag": "tickets", "ok": 202,
     "summary": "Ask an approver to start a ticket in a session: creates a PENDING board decision of kind "
                "ticket_take_request from \"api\" (\"Start KEY in CODE?\", approve/decline), never relayed to the "
                "session. On approve the operator runs the take; a key can never answer it. At most 3 pending "
                "per key (429).",
     "body": {"type": "object", "required": ["session"], "additionalProperties": False,
              "properties": {"session": _CODE, "note": {"type": "string", "maxLength": 500}}},
     "schema": {"type": "object", "properties": {"decision_id": {"type": "string"},
                                                 "status": {"const": "pending"}}}},
]


def compile_path(template):
    """The regex for a path template; each {name} must be in PARAMS."""
    out, pos = "", 0
    for m in re.finditer(r"\{([a-z_]+)\}", template):
        out += re.escape(template[pos:m.start()]) + "(?P<%s>%s)" % (m.group(1), PARAMS[m.group(1)][0])
        pos = m.end()
    return re.compile(out + re.escape(template[pos:]))


def openapi():
    paths = {}
    for r in ROUTES:
        op = {"operationId": r["handler"], "summary": r["summary"], "tags": [r["tag"]],
              "responses": {str(r.get("ok", 200)): {"description": "OK", "content": {
                  r.get("content_type", "application/json"): {
                      "schema": r["schema"] if r.get("schema") is not None else {"type": "string"}}}}}}
        if r.get("body") is not None:
            op["requestBody"] = {"required": True, "content": {"application/json": {"schema": r["body"]}}}
            for code, what in (("400", "A bad body"), ("409", "Busy or already requested; try again"),
                               ("413", "Body over 16 KiB"), ("415", "Content-Type must be application/json")):
                op["responses"][code] = {"description": what, "content": {"application/json": {"schema": ERROR}}}
        params = [{"name": n, "in": "path", "required": True, "description": PARAMS[n][1],
                   "schema": {"type": "string", "pattern": "^(?:%s)$" % PARAMS[n][0]}}
                  for n in re.findall(r"\{([a-z_]+)\}", r["path"])]
        params += [{"name": q, "in": "query", "required": q in r.get("query_required", []),
                    "description": QUERY[q][1], "schema": {"type": "string", "pattern": "^(?:%s)$" % QUERY[q][0]}}
                   for q in r.get("query", [])]
        if params:
            op["parameters"] = params
        if r["scope"] == "public":
            op["security"] = []
        else:
            op["x-scope"] = r["scope"]
            op["responses"].update({
                "401": {"description": "No valid key", "content": {"application/json": {"schema": ERROR}}},
                "403": {"description": "The key lacks the scope (insufficient_scope), or a test key on the live "
                                       "board (key_wrong_env)", "content": {"application/json": {"schema": ERROR}}},
                "429": {"description": "Rate limited; see Retry-After",
                        "content": {"application/json": {"schema": ERROR}}}})
        if params:
            op["responses"]["400"] = {"description": "A bad parameter",
                                      "content": {"application/json": {"schema": ERROR}}}
        if "{" in r["path"] or r["handler"] == "pomodoro_day":
            op["responses"]["404"] = {"description": "Not found", "content": {"application/json": {"schema": ERROR}}}
        paths.setdefault(r["path"], {})[r["method"].lower()] = op
    return {
        "openapi": "3.1.0",
        "info": {"title": "agent-switchboard API", "version": VERSION,
                 "description": "The board API. Authenticate with `Authorization: Bearer asb_live_…` "
                                "(a board API key, `board apikey`). A key never answers decisions, presses "
                                "prompts or manages keys: what needs a person becomes a pending board "
                                "decision. No CORS: call it from a server or script."},
        "servers": [{"url": "/"}],
        "components": {"securitySchemes": {"boardKey": {"type": "http", "scheme": "bearer",
                                                        "bearerFormat": "asb_live_<kid>_<secret>"}},
                       "schemas": {"Error": ERROR}},
        "security": [{"boardKey": []}],
        "paths": paths,
    }


if __name__ == "__main__":
    if sys.argv[1:] == ["openapi"]:
        print(json.dumps(openapi(), indent=2, sort_keys=True))
        sys.exit(0)
    print(__doc__.strip())
    sys.exit(2)
