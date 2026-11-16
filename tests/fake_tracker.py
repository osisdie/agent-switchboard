"""A fake issue tracker for tests/test_tickets.py (stdlib http.server, 127.0.0.1, a thread).

Routes (the shape of the http-json example in fleet.example.yaml):
  GET /api/v1/issues/resolve?displayId=KEY   {"data": {"id", "project"}} | 404
  GET /api/v1/projects/<project>/issues/<id> {"data": {...}} | 404
Behaviour: resolving WEB-99 -> 500; by issue id: "redirect" -> 302 to `redirect_to`; "boom" -> 500; ids listed in
`throttle` -> 429 with the listed Retry-After values first. Without the expected bearer token,
every route answers 401. Writes: POST .../issues/<id>/comments {body} -> {"data": {"id"}};
PATCH .../issues/<id> {status}|{assigneeName} updates the issue; an op in `fail_writes`
("comment", "transition", "assign") answers 500. Bodies are kept in `.writes`. Error bodies and some fields echo the Authorization header back, so a
test can prove the token never leaks. Every request is recorded in `.requests`.
"""
import http.server
import json
import threading
import urllib.parse


class FakeTracker:
    def __init__(self, token, issues=None):
        self.token = token
        self.issues = issues or {}       # display key -> {"id", "project", "data": {...}}
        self.throttle = {}               # internal id -> [retry-after values, ...]
        self.redirect_to = None
        self.fail_writes = set()
        self.writes = []
        self.requests = []
        self.lock = threading.Lock()
        tracker = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def send(self, code, obj, headers=None):
                data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                auth = self.headers.get("Authorization") or ""
                with tracker.lock:
                    tracker.requests.append({"path": u.path, "raw": self.path, "auth": bool(auth),
                                             "host": self.headers.get("Host")})
                if auth != "Bearer " + tracker.token:
                    return self.send(401, {"error": "bad credentials, you sent: %s" % auth})
                parts = u.path.split("/")
                if u.path == "/api/v1/issues/resolve":
                    key = (urllib.parse.parse_qs(u.query).get("displayId") or [""])[0]
                    if key == "WEB-99":
                        return self.send(500, "resolve failed near %s" % auth)
                    it = tracker.issues.get(key)
                    if not it:
                        return self.send(404, {"error": "no issue %s (auth was %s)" % (key, auth)})
                    return self.send(200, {"data": {"id": it["id"], "project": it["project"]}})
                if len(parts) == 7 and parts[1:4] == ["api", "v1", "projects"] and parts[5] == "issues":
                    iid = urllib.parse.unquote(parts[6])
                    if iid == "redirect" and tracker.redirect_to:
                        return self.send(302, {}, {"Location": tracker.redirect_to})
                    if iid == "boom":
                        return self.send(500, "internal error near %s" % auth)
                    with tracker.lock:
                        pending = tracker.throttle.get(iid)
                        ra = pending.pop(0) if pending else None
                    if ra is not None:
                        return self.send(429, {"error": "slow down"}, {"Retry-After": str(ra)})
                    it = next((v for v in tracker.issues.values() if v["id"] == iid), None)
                    if not it:
                        return self.send(404, {"error": "missing"})
                    data = dict(it.get("data") or {})
                    if data.get("echo_auth"):
                        data["description"] = "%s (debug: you sent %s)" % (data.get("description", ""), auth)
                        data["title"] = "%s %s" % (data.get("title", ""), auth.split(" ", 1)[-1])
                    return self.send(200, {"data": data})
                return self.send(404, {"error": "no route"})

            def do_POST(self):
                self.write("POST")

            def do_PATCH(self):
                self.write("PATCH")

            def write(self, method):
                u = urllib.parse.urlsplit(self.path)
                auth = self.headers.get("Authorization") or ""
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"null")
                except ValueError:
                    body = None
                with tracker.lock:
                    tracker.requests.append({"path": u.path, "raw": self.path, "auth": bool(auth), "method": method,
                                             "host": self.headers.get("Host")})
                if auth != "Bearer " + tracker.token:
                    return self.send(401, {"error": "bad credentials"})
                parts = u.path.split("/")
                if len(parts) < 7 or parts[1:4] != ["api", "v1", "projects"] or parts[5] != "issues":
                    return self.send(404, {"error": "no route"})
                iid = urllib.parse.unquote(parts[6])
                it = next((v for v in tracker.issues.values() if v["id"] == iid), None)
                if not it or not isinstance(body, dict):
                    return self.send(404, {"error": "missing"})
                op = "comment" if method == "POST" and parts[7:] == ["comments"] else \
                    "transition" if method == "PATCH" and "status" in body else \
                    "assign" if method == "PATCH" and "assigneeName" in body else None
                if op is None:
                    return self.send(400, {"error": "bad request"})
                if op in tracker.fail_writes:
                    return self.send(500, {"error": "write failed near %s" % auth})
                with tracker.lock:
                    tracker.writes.append({"op": op, "id": iid, "body": body})
                    if op == "transition":
                        it.setdefault("data", {})["status"] = body["status"]
                    elif op == "assign":
                        it.setdefault("data", {})["assigneeName"] = body["assigneeName"]
                    n = len(tracker.writes)
                if op == "comment":
                    return self.send(201, {"data": {"id": "c-%d" % n}})
                return self.send(204, "")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def count(self):
        with self.lock:
            return len(self.requests)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
