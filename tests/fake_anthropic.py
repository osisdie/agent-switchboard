#!/usr/bin/env python3
"""tests/fake_anthropic.py — a scripted stand-in for an Anthropic-compatible model endpoint (the
local asker desk's end-to-end test, tests/test_desk_e2e.py). Never a real model.

  fake_anthropic.py PORT LOG REPO TOKEN

For a request that offers the desk's tools, the turn that starts with `Answer board question
<id>` is played step by step: desk_question -> Read REPO/.env (must be denied) -> Bash (must not
exist) -> Read REPO/README.md (allowed) -> Read REPO/secrets/db.txt (denied) -> Grep for the
secret (finds nothing) -> Edit README.md (must not exist) -> desk_reply with a summary of every
tool result -> "Done.". Any other request gets a short text. GET /v1/models answers 200.
Every request is logged to LOG as one JSON line: whether its auth matched TOKEN, how many
messages it carried, the tools it offered, the step played.
"""
import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG, REPO, TOKEN = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]


def log(obj):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for c in content or []:
        if c.get("type") == "text":
            out.append(c.get("text", ""))
        elif c.get("type") == "tool_result":
            cc = c.get("content")
            out.append("[tool_result%s] %s" % (" ERROR" if c.get("is_error") else "",
                                               text_of(cc) if isinstance(cc, list) else str(cc)))
    return "\n".join(out)


STEPS = [
    lambda mid: ("mcp__board-desk__desk_question", {"message_id": mid}),
    lambda mid: ("Read", {"file_path": REPO + "/.env"}),
    lambda mid: ("Bash", {"command": "cat .env", "description": "x"}),
    lambda mid: ("Read", {"file_path": REPO + "/README.md"}),
    lambda mid: ("Read", {"file_path": REPO + "/secrets/db.txt"}),
    lambda mid: ("Grep", {"pattern": "FAKE-SECRET", "path": REPO, "output_mode": "content"}),
    lambda mid: ("Edit", {"file_path": REPO + "/README.md", "old_string": "Lab", "new_string": "HACKED"}),
]


def plan(body):
    msgs = body.get("messages") or []
    tools = [t.get("name") for t in body.get("tools") or []]
    if "mcp__board-desk__desk_question" not in tools:
        return ("text", "ok")
    start = None
    for i, m in enumerate(msgs):
        if m.get("role") == "user" and "Answer board question m-" in text_of(m.get("content")):
            start = i
    if start is None:
        return ("text", "Waiting for a board question.")
    mid = text_of(msgs[start]["content"]).split("Answer board question ")[1].split()[0].strip()
    results = [text_of(m.get("content")) for m in msgs[start + 1:] if m.get("role") == "user"]
    step = len(results)
    if step < len(STEPS):
        return ("tool",) + STEPS[step](mid)
    if step == len(STEPS):
        summary = " || ".join(r.replace("\n", " ")[:220] for r in results)
        return ("tool", "mcp__board-desk__desk_reply", {"message_id": mid, "answer": "SMOKE ANSWER. " + summary})
    return ("text", "Done.")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def auth_ok(self):
        return self.headers.get("Authorization") == "Bearer " + TOKEN or self.headers.get("x-api-key") == TOKEN

    def do_GET(self):  # noqa: N802
        log({"get": self.path, "auth_ok": self.auth_ok()})
        if self.path.startswith("/v1/models"):
            return self._json(200, {"data": [{"id": "fake-coder", "type": "model"}]})
        return self._json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if "count_tokens" in self.path:
            return self._json(200, {"input_tokens": 100})
        if not self.auth_ok():
            log({"post": self.path, "auth_ok": False})
            return self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad"}})
        p = plan(body)
        log({"post": self.path, "auth_ok": True, "nmsgs": len(body.get("messages") or []),
             "tools": [t.get("name") for t in body.get("tools") or []], "plan": list(p[:2])})
        if p[0] == "text":
            blocks, stop = [{"type": "text", "text": p[1]}], "end_turn"
        else:
            blocks = [{"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": p[1], "input": p[2]}]
            stop = "tool_use"
        msg = {"id": "msg_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant",
               "model": body.get("model") or "fake", "content": blocks, "stop_reason": stop, "stop_sequence": None,
               "usage": {"input_tokens": 10, "output_tokens": 10}}
        if not body.get("stream"):
            return self._json(200, msg)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def ev(name, data):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (name, json.dumps(data))).encode())
            self.wfile.flush()
        ev("message_start", {"type": "message_start", "message": dict(msg, content=[], stop_reason=None)})
        b = blocks[0]
        if b["type"] == "text":
            ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": b["text"]}})
        else:
            ev("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
            ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
        ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                             "usage": {"output_tokens": 10}})
        ev("message_stop", {"type": "message_stop"})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
