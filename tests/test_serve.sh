#!/usr/bin/env bash
# shellcheck disable=SC2015  # ok/bad always return 0
# tests/test_serve.sh — board-serve (lib/serve.py): state, SSE, identity, CSRF, approvers,
# permission presses (fake tmux), path allowlist, Cloudflare Access JWT (if cryptography exists).
#
# Usage: tests/test_serve.sh [-v]
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1

T="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-serve.XXXXXX")"
SRV_PID=""; LIVE_PID=""
cleanup() {
  [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
  [ -n "$LIVE_PID" ] && kill "$LIVE_PID" 2>/dev/null
  [ -n "${KEEP_T:-}" ] && echo "kept $T" || rm -rf "$T"
}
trap cleanup EXIT

export BOARD_HOME="$T/home"
export BOARD_CONFIG_DIR="$T/config"
export CLAUDE_SESSIONS_DIR="$T/sessions"
export CLAUDE_SETTINGS="$T/settings.json"
unset TMUX_PANE
while IFS= read -r v; do unset "$v"; done < <(compgen -e | grep -E '^(SLACK_|SMTP_|ALERT_|LINE_|AWS_)')
export ALERT_DESKTOP=0
mkdir -p "$BOARD_HOME"/{state,decisions,tasks,messages/m-1/replies} "$BOARD_CONFIG_DIR" "$CLAUDE_SESSIONS_DIR" "$T/bin"

pass=0; fail=0
ok() { pass=$((pass + 1)); [ "$VERBOSE" = 1 ] && echo "  ok   $1"; return 0; }
bad() { fail=$((fail + 1)); echo "  FAIL $1"; [ -n "${2:-}" ] && echo "       $2"; return 0; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got '$2', want '$3'"; fi; }

# ---------------------------------------------------------------- fixtures
SID="aaaaaaaa-1111-2222-3333-444444444444"
NOW="$(date +%s)000"
sleep 600 & LIVE_PID=$!
cat > "$CLAUDE_SESSIONS_DIR/$LIVE_PID.json" <<EOF
{"pid":$LIVE_PID,"sessionId":"$SID","cwd":"/secret/path","tmux":"CP:@3.%42","name":"CP","status":"waiting","startedAt":1}
EOF
cat > "$BOARD_HOME/state/$SID.json" <<EOF
{"session_id":"$SID","state":"needs-you","tmux_pane":"%42","last_event":"PermissionRequest",
 "pending":{"tool_use_id":"tu_1","tool_name":"Bash","summary":"rm -rf build"},"updated_at":$NOW}
EOF
dec() {  # dec ID KIND EXTRA_JSON
  jq -n --arg id "$1" --arg kind "$2" --argjson now "$NOW" --argjson x "${3:-{\}}" \
    '{id: $id, kind: $kind, from: "CP", question: "Q?", options: ["A","B"], status: "pending",
      answer: null, answered_by: null, answered_at: null, relayed_at: null, created_at: $now} + $x' \
    > "$BOARD_HOME/decisions/$1.json"
}
dec d-q1 question
dec d-q2 question
dec d-q3 question
dec d-gate question '{"task_id":"t-1","stage":"build","gate":"owner","options":["approve","request changes"]}'
dec d-prod question '{"task_id":"t-1","stage":"production","gate":"production","options":["approve","request changes"]}'
dec d-perm permission "{\"from\":null,\"session_id\":\"$SID\",\"tool_use_id\":\"tu_1\",\"tool_name\":\"Bash\"}"
cat > "$BOARD_HOME/tasks/t-1.json" <<EOF
{"id":"t-1","title":"Ship it","owner":"CP","stage":"build","artifacts":{},"gates":[{"stage":"build","gate":"owner","status":"pending","decision_id":"d-gate"}],"updated_at":$NOW}
EOF
printf '%s\n' '{"ts":1,"type":"hook","event":"Stop"}' \
  '{"ts":2,"type":"approval","decision_id":"d-old","task_id":"t-1","stage":"plan","gate":"owner","answer":"approve","result":"approved","answered_by":"local","answered_at":2,"relayed_at":null}' \
  > "$BOARD_HOME/events.jsonl"
echo '{"id":"m-1","to":"CP","body":"hello","by":"local","created_at":1,"status":"replied"}' > "$BOARD_HOME/messages/m-1.json"
echo '{"from":"CP","body":"hi back","created_at":2}' > "$BOARD_HOME/messages/m-1/replies/r-1.json"
echo '{"five_hour":{"pct":42,"resets_at":1},"seven_day":{"pct":7,"resets_at":2},"updated_at":3,"session_id":"x"}' > "$BOARD_HOME/usage.json"
echo "{\"heartbeat_at\":$NOW,\"operator_session\":\"BOARD\",\"version\":\"t\",\"secret_field\":\"no\"}" > "$BOARD_HOME/operator.json"

cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  display-message) echo CP ;;
  capture-pane) cat "$FAKE_TMUX_SCREEN" ;;
  send-keys) exit 0 ;;
esac
EOF
chmod +x "$T/bin/tmux"
export FAKE_TMUX_LOG="$T/tmux.log"; : > "$FAKE_TMUX_LOG"
export FAKE_TMUX_SCREEN="$T/screen"
printf ' Bash command\n   rm -rf build\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n' > "$FAKE_TMUX_SCREEN"
export PATH="$T/bin:$PATH"
"$REPO/bin/board-collect" --once --no-notify >/dev/null 2>&1

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
B="http://127.0.0.1:$PORT"
ORIGIN="Origin: $B"

write_cfg() {  # write_cfg EXTRA_SERVE_YAML
  cat > "$BOARD_CONFIG_DIR/fleet.yaml" <<EOF
approvers: [local, boss@example.com]
release_managers: [rm@example.com]
sessions:
  - code: CP
    cwd: /w
serve:
  port: $PORT
  hostnames: [board.example.com]
$1
EOF
}
start() {
  [ -n "$SRV_PID" ] && { kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null; }
  "$REPO/bin/board-serve" --quiet 2>>"$T/serve.err" & SRV_PID=$!
  local _
  for _ in $(seq 1 50); do
    curl -s -o /dev/null "$B/api/state" && return 0
    sleep 0.1
  done
  echo "board-serve did not start:"; cat "$T/serve.err"; exit 1
}
# req METHOD PATH [curl args...] -> prints the status; body in $T/body
req() {
  local m="$1" p="$2"; shift 2
  curl -s --path-as-is -o "$T/body" -w '%{http_code}' -X "$m" "$@" "$B$p"
}
post() {  # post PATH JSON [curl args...]
  local p="$1" d="$2"; shift 2
  req POST "$p" -H 'Content-Type: application/json' -H 'X-Board: 1' -H "$ORIGIN" --data-binary "$d" "$@"
}
body() { jq -r "$1" "$T/body"; }

write_cfg ""
start

# ---------------------------------------------------------------- state
echo "state:"
eq "GET /api/state 200" "$(req GET /api/state)" "200"
eq "state keys" "$(body 'keys | join(",")')" "approvals,decisions,generated_at,ledger,me,messages,mode,operator,replies,sessions,tasks,writable"
eq "me is local on loopback" "$(body .me)" "local"
eq "mode local" "$(body .mode)" "local"
eq "session CP needs-you" "$(body '.sessions[] | select(.code=="CP") | .state')" "needs-you"
eq "session fields are an allowlist (no cwd/pid)" "$(body '[.sessions[] | has("cwd") or has("pid") or has("session_id")] | any')" "false"
eq "permission decision gets its from" "$(body '.decisions[] | select(.id=="d-perm") | .from')" "CP"
eq "decisions omit session_id" "$(body '[.decisions[] | has("session_id")] | any')" "false"
eq "tasks" "$(body '.tasks[0].title')" "Ship it"
eq "approvals from events.jsonl" "$(body '.approvals | length')" "1"
eq "approval row" "$(body '.approvals[0].decision_id + "/" + .approvals[0].result')" "d-old/approved"
eq "messages" "$(body '.messages[0].body')" "hello"
eq "replies" "$(body '.replies["m-1"][0].body')" "hi back"
eq "operator heartbeat" "$(body '.operator.heartbeat_at')" "$NOW"
eq "operator fields are an allowlist" "$(body '.operator | has("secret_field")')" "false"
eq "usage pct" "$(body '.operator.usage.five_hour.pct')" "42"
eq "usage drops session_id" "$(body '.operator.usage | has("session_id")')" "false"
eq "approver_ids from fleet.yaml" "$(body '.operator.approver_ids | join(",")')" "local,boss@example.com"
eq "limit null when none" "$(body '.operator.limit')" "null"

eq "GET / 200" "$(req GET / -D "$T/hdr")" "200"
grep -q '<meta name="board-mode" content="local">' "$T/body" && ok "page has the local-mode meta" || bad "page has the local-mode meta"
grep -qi '^content-security-policy:.*script-src .nonce-' "$T/hdr" && ok "page sent with a nonce CSP" || bad "page sent with a nonce CSP"
nonce="$(sed -n 's/.*script-src .nonce-\([^'"'"']*\).*/\1/p' "$T/hdr" | tr -d '\r')"
grep -q "<script nonce=\"$nonce\">" "$T/body" && ok "inline script carries the nonce" || bad "inline script carries the nonce"
grep -q 'board-mode' "$REPO/artifact/board.html" && ! grep -q '<meta name="board-mode"' "$REPO/artifact/board.html" \
  && ok "artifact source itself has no local-mode meta" || bad "artifact source itself has no local-mode meta"

# ---------------------------------------------------------------- allowlist / traversal / host
echo "allowlist:"
for p in /../../etc/passwd /%2e%2e/%2e%2e/etc/passwd /artifact/board.html /lib/serve.py /events.jsonl /api/../snapshot.json /api/state/../../x; do
  eq "GET $p refused" "$(req GET "$p")" "404"
done
eq "decision id with traversal refused" "$(post '/api/decisions/..%2F..%2Fevil/answer' '{"answer":"A"}')" "400"
eq "dotted decision id refused" "$(post '/api/decisions/../answer' '{"answer":"A"}')" "400"
eq "unknown decision 404" "$(post '/api/decisions/d-nope/answer' '{"answer":"A"}')" "404"
eq "foreign Host refused (DNS rebinding)" "$(req GET /api/state -H 'Host: evil.example:'"$PORT")" "421"
eq "configured hostname is accepted" "$(req GET /api/state -H 'Host: board.example.com')" "200"

# ---------------------------------------------------------------- CSRF + body checks
echo "csrf:"
eq "POST without X-Board refused" "$(req POST /api/messages -H 'Content-Type: application/json' -H "$ORIGIN" --data-binary '{"to":"CP","body":"x"}')" "403"
eq "  error csrf" "$(body .error)" "csrf"
eq "POST without Origin refused" "$(req POST /api/messages -H 'Content-Type: application/json' -H 'X-Board: 1' --data-binary '{"to":"CP","body":"x"}')" "403"
eq "POST cross-origin refused" "$(req POST /api/messages -H 'Content-Type: application/json' -H 'X-Board: 1' -H 'Origin: https://evil.example' --data-binary '{"to":"CP","body":"x"}')" "403"
eq "POST text/plain refused" "$(req POST /api/messages -H 'Content-Type: text/plain' -H 'X-Board: 1' -H "$ORIGIN" --data-binary '{"to":"CP","body":"x"}')" "415"
big="$(python3 -c 'import json; print(json.dumps({"to":"CP","body":"x"*20000}))')"
eq "POST over the size limit refused" "$(post /api/messages "$big")" "413"
eq "POST non-JSON refused" "$(post /api/messages 'not json')" "400"
eq "bad to refused" "$(post /api/messages '{"to":"../x","body":"x"}')" "400"
eq "no message written by refused POSTs" "$(find "$BOARD_HOME/messages" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')" "1"

# ---------------------------------------------------------------- messages
echo "messages:"
eq "POST /api/messages 201" "$(post /api/messages '{"to":"CP","body":" please rebase "}')" "201"
mid="$(body .id)"
mf="$BOARD_HOME/messages/$mid.json"
[ -f "$mf" ] && ok "message file written" || bad "message file written" "$mid"
eq "message fields" "$(jq -r '[.to, .body, .by, .status] | join("|")' "$mf" 2>/dev/null)" "CP|please rebase|local|new"
eq "message write logged with identity" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="message")] | last | .by + "/" + .via' "$BOARD_HOME/events.jsonl")" "local/loopback"

# ---------------------------------------------------------------- SSE
echo "sse:"
python3 - "$B" "$BOARD_HOME" > "$T/sse.out" 2>&1 <<'EOF' &
import http.client, json, os, sys, time, urllib.parse
base, home = sys.argv[1], sys.argv[2]
u = urllib.parse.urlsplit(base)
c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
c.request("GET", "/api/events")
r = c.getresponse()
print("ctype", r.getheader("Content-Type"))
def event():
    data = None
    while True:
        line = r.fp.readline().decode()
        if not line:
            return None
        if line.startswith("data: "):
            data = json.loads(line[6:])
        elif line == "\n" and data is not None:
            return data
first = event()
print("first", len(first["messages"]))
with open(os.path.join(home, "messages", "m-sse.json"), "w") as fh:
    json.dump({"id": "m-sse", "to": "all", "body": "sse!", "by": "local", "created_at": int(time.time() * 1000), "status": "new"}, fh)
t0 = time.time()
second = event()
print("second", [m["id"] for m in second["messages"]][0], "%.1f" % (time.time() - t0))
EOF
sse_pid=$!
for _ in $(seq 1 80); do kill -0 "$sse_pid" 2>/dev/null || break; sleep 0.1; done
kill "$sse_pid" 2>/dev/null
grep -q '^ctype text/event-stream' "$T/sse.out" && ok "SSE content type" || bad "SSE content type" "$(cat "$T/sse.out")"
grep -q '^first 2$' "$T/sse.out" && ok "SSE sends the state at once" || bad "SSE sends the state at once" "$(cat "$T/sse.out")"
grep -q '^second m-sse ' "$T/sse.out" && ok "SSE pushes a change" || bad "SSE pushes a change" "$(cat "$T/sse.out")"

# ---------------------------------------------------------------- answers (loopback)
echo "answers:"
eq "answer from loopback 200" "$(post /api/decisions/d-q1/answer '{"answer":"A"}')" "200"
eq "  status answered" "$(jq -r '.status + "/" + .answered_by + "/" + .answer' "$BOARD_HOME/decisions/d-q1.json")" "answered/local/A"
eq "  answer logged with identity" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="answer")] | last | .by + "/" + .result' "$BOARD_HOME/events.jsonl")" "local/answered"
eq "gate answer 200" "$(post /api/decisions/d-gate/answer '{"answer":"approve"}')" "200"
eq "  gate approved on the task" "$(jq -r '.gates[0].status + "/" + .gates[0].approved_by' "$BOARD_HOME/tasks/t-1.json")" "approved/local"
eq "  approval shows up in state" "$(req GET /api/state >/dev/null; body '[.approvals[].decision_id] | index("d-gate") != null')" "true"
eq "production gate by local refused (release_managers)" "$(post /api/decisions/d-prod/answer '{"answer":"approve"}')" "403"
eq "  error not_approver" "$(body .error)" "not_approver"
eq "  marked unauthorized" "$(jq -r .status "$BOARD_HOME/decisions/d-prod.json")" "unauthorized"
eq "empty answer refused" "$(post /api/decisions/d-q2/answer '{"answer":"  "}')" "400"
eq "permission answer must be allow/deny" "$(post /api/decisions/d-perm/answer '{"answer":"maybe"}')" "400"

# ---------------------------------------------------------------- permission -> board press
echo "permission:"
: > "$FAKE_TMUX_LOG"
eq "permission allow 200" "$(post /api/decisions/d-perm/answer '{"answer":"allow"}')" "200"
eq "  status relayed" "$(body .status)" "relayed"
grep -q '^send-keys -t %42 1$' "$FAKE_TMUX_LOG" && ok "  board press sent the allow key" || bad "  board press sent the allow key" "$(cat "$FAKE_TMUX_LOG")"
eq "  decision file relayed" "$(jq -r .status "$BOARD_HOME/decisions/d-perm.json")" "relayed"
eq "  press logged with identity" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="press")] | last | .by + "/" + .result' "$BOARD_HOME/events.jsonl")" "local/sent"
eq "answering a relayed decision again 409" "$(post /api/decisions/d-perm/answer '{"answer":"deny"}')" "409"
: > "$FAKE_TMUX_LOG"
# A new prompt (tu_2) that is no longer on screen when the answer arrives.
jq '.pending.tool_use_id = "tu_2"' "$BOARD_HOME/state/$SID.json" > "$T/st" && mv "$T/st" "$BOARD_HOME/state/$SID.json"
dec d-perm2 permission "{\"from\":null,\"session_id\":\"$SID\",\"tool_use_id\":\"tu_2\",\"tool_name\":\"Bash\"}"
echo '$ just a shell' > "$FAKE_TMUX_SCREEN"
eq "stale permission (prompt gone from the pane) 409" "$(post /api/decisions/d-perm2/answer '{"answer":"allow"}')" "409"
eq "  error stale" "$(body .error)" "stale"
grep -q 'send-keys' "$FAKE_TMUX_LOG" && bad "  no keys sent on stale" "$(cat "$FAKE_TMUX_LOG")" || ok "  no keys sent on stale"
eq "  decision file stale" "$(jq -r .status "$BOARD_HOME/decisions/d-perm2.json")" "stale"

# ---------------------------------------------------------------- non-loopback (forwarded)
echo "remote:"
CF_EMAIL='Cf-Access-Authenticated-User-Email: boss@example.com'
eq "forwarded GET refused without CF config" "$(req GET /api/state -H 'X-Forwarded-For: 203.0.113.9')" "403"
eq "  error remote_refused" "$(body .error)" "remote_refused"
eq "forwarded GET / refused too" "$(req GET / -H 'Cf-Connecting-Ip: 203.0.113.9')" "403"
eq "CF email header alone is not trusted by default" "$(post /api/decisions/d-q2/answer '{"answer":"A"}' -H "$CF_EMAIL")" "403"
eq "  decision untouched" "$(jq -r .status "$BOARD_HOME/decisions/d-q2.json")" "pending"

write_cfg "  trust_cf_headers: true"
start
eq "trusted CF header: state as that email" "$(req GET /api/state -H "$CF_EMAIL" -H 'X-Forwarded-For: 203.0.113.9' >/dev/null; body .me)" "boss@example.com"
eq "trusted CF header: approver answers" "$(post /api/decisions/d-q2/answer '{"answer":"B"}' -H "$CF_EMAIL")" "200"
eq "  answered_by is the email" "$(jq -r .answered_by "$BOARD_HOME/decisions/d-q2.json")" "boss@example.com"
eq "  logged via cf-header" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="answer")] | last | .via' "$BOARD_HOME/events.jsonl")" "cf-header"
eq "trusted CF header: non-approver refused" "$(post /api/decisions/d-q3/answer '{"answer":"A"}' -H 'Cf-Access-Authenticated-User-Email: intruder@example.com')" "403"
eq "  error not_approver" "$(body .error)" "not_approver"
eq "  marked unauthorized" "$(jq -r '.status + "/" + .answered_by' "$BOARD_HOME/decisions/d-q3.json")" "unauthorized/intruder@example.com"
eq "forwarded without email refused" "$(req GET /api/state -H 'X-Forwarded-For: 203.0.113.9')" "401"
eq "release manager answers the production gate" "$(post /api/decisions/d-prod/answer '{"answer":"approve"}' -H 'Cf-Access-Authenticated-User-Email: rm@example.com')" "200"

# ---------------------------------------------------------------- Cloudflare Access JWT
echo "jwt:"
if python3 -c 'import cryptography' 2>/dev/null; then
  python3 - "$T" <<'EOF'
import base64, json, sys, time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
t = sys.argv[1]
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def num(n): return b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pub = key.public_key().public_numbers()
json.dump({"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "n": num(pub.n), "e": num(pub.e)}]}, open(t + "/certs.json", "w"))
def tok(claims, k=key, kid="k1", alg="RS256"):
    h = b64(json.dumps({"alg": alg, "kid": kid}).encode()); p = b64(json.dumps(claims).encode())
    s = k.sign((h + "." + p).encode(), padding.PKCS1v15(), hashes.SHA256())
    return h + "." + p + "." + b64(s)
now = int(time.time())
base = {"aud": ["AUD123"], "iss": "https://team.example.com", "exp": now + 600, "iat": now, "email": "boss@example.com"}
toks = {"good": tok(base), "badsig": tok(base, k=other), "wrongaud": tok(dict(base, aud=["X"])),
        "expired": tok(dict(base, exp=now - 3600)), "wrongiss": tok(dict(base, iss="https://evil.example")),
        "nobody": tok(dict(base, email="intruder@example.com"))}
for k, v in toks.items():
    open("%s/jwt.%s" % (t, k), "w").write(v)
EOF
  write_cfg "  cf_access:
    team_domain: team.example.com
    aud: AUD123
    certs_url: file://$T/certs.json"
  start
  jwt() { echo "Cf-Access-Jwt-Assertion: $(cat "$T/jwt.$1")"; }
  eq "valid JWT: identity from the token" "$(req GET /api/state -H "$(jwt good)" >/dev/null; body .me)" "boss@example.com"
  eq "bad signature refused" "$(req GET /api/state -H "$(jwt badsig)")" "401"
  eq "wrong audience refused" "$(req GET /api/state -H "$(jwt wrongaud)")" "401"
  eq "expired refused" "$(req GET /api/state -H "$(jwt expired)")" "401"
  eq "wrong issuer refused" "$(req GET /api/state -H "$(jwt wrongiss)")" "401"
  eq "garbage token refused" "$(req GET /api/state -H 'Cf-Access-Jwt-Assertion: a.b.c')" "401"
  eq "email header without JWT refused" "$(req GET /api/state -H "$CF_EMAIL")" "401"
  eq "email header must match the JWT" "$(req GET /api/state -H "$(jwt good)" -H 'Cf-Access-Authenticated-User-Email: rm@example.com')" "401"
  dec d-q4 question
  eq "valid JWT answers" "$(post /api/decisions/d-q4/answer '{"answer":"A"}' -H "$(jwt good)")" "200"
  eq "  logged via cf-jwt" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="answer")] | last | .by + "/" + .via' "$BOARD_HOME/events.jsonl")" "boss@example.com/cf-jwt"
  dec d-q5 question
  eq "valid JWT of a non-approver refused" "$(post /api/decisions/d-q5/answer '{"answer":"A"}' -H "$(jwt nobody)")" "403"
  eq "loopback still local with JWT configured" "$(req GET /api/state >/dev/null; body .me)" "local"
else
  echo "  (skipped: python3 cryptography module not installed)"
fi

# ---------------------------------------------------------------- misc
echo "misc:"
eq "--check prints the listen address" "$("$REPO/bin/board-serve" --check | sed -n 's/^listen *//p')" "http://127.0.0.1:$PORT"
eq "listens on loopback only" "$(python3 - "$PORT" <<'EOF'
import socket, sys
ips = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)} - {"127.0.0.1", "127.0.1.1"}
for ip in ips:
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect((ip, int(sys.argv[1]))); print("open on", ip); sys.exit()
    except OSError:
        pass
print("ok")
EOF
)" "ok"

# ---------------------------------------------------------------- --watch (operator input)
echo "watch:"
w="$("$REPO/bin/board-serve" --watch --once)"
eq "--watch lists answered questions and new messages, oldest first" "$(tr '\n' ',' <<<"$w")" \
  "$(python3 - "$BOARD_HOME" <<'EOF2'
import glob, json, os, sys
h = sys.argv[1]; out = []
for f in glob.glob(os.path.join(h, "decisions", "*.json")):
    d = json.load(open(f))
    if d["status"] == "answered" and d["kind"] != "permission":
        out.append((d["answered_at"] or 0, "decision %s answered" % d["id"]))
for f in glob.glob(os.path.join(h, "messages", "*.json")):
    m = json.load(open(f))
    if m["status"] == "new":
        out.append((m["created_at"] or 0, "message %s new" % m["id"]))
print("".join(l + "," for _, l in sorted(out)), end="")
EOF2
)"
grep -q '^decision d-perm' <<<"$w" && bad "--watch skips permission decisions" "$w" || ok "--watch skips permission decisions"
grep -q '^message m-1 ' <<<"$w" && bad "--watch skips replied messages" "$w" || ok "--watch skips replied messages"
"$REPO/bin/board-serve" --watch --tick 1 > "$T/watch.out" & wpid=$!
sleep 0.5
echo '{"id":"m-late","to":"CP","body":"x","by":"local","created_at":9,"status":"new"}' > "$BOARD_HOME/messages/m-late.json"
sleep 2.2
kill "$wpid" 2>/dev/null; wait "$wpid" 2>/dev/null
eq "--watch prints a new message once" "$(grep -c '^message m-late new$' "$T/watch.out")" "1"
grep -q '^tick$' "$T/watch.out" && ok "--watch ticks" || bad "--watch ticks" "$(cat "$T/watch.out")"

# ---------------------------------------------------------------- fleet-up
echo "fleet-up:"
kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null; SRV_PID=""
mkdir -p "$T/fbin"
cat > "$T/fbin/tmux" <<'EOF2'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  has-session) [ -n "${FAKE_SB:-}" ] ;;
  list-windows) [ -n "${FAKE_SB:-}" ] && printf '%s\n' collector ${FAKE_SERVE_WIN:+serve} ;;
esac
EOF2
chmod +x "$T/fbin/tmux"
printf 'collector: false\nserve:\n  enabled: true\n  port: %s\nsessions: []\n' "$PORT" > "$BOARD_CONFIG_DIR/fleet.yaml"
fu() { BOARD_HOME="$T/fu-home" PATH="$T/fbin:$PATH" "$REPO/bin/fleet-up" "$@" 2>&1; }
out="$(fu --dry-run)"
grep -q "would run: tmux new-session -d -s switchboard -n serve .*bin/board-serve" <<<"$out" \
  && ok "fleet-up starts board-serve in a new switchboard session" || bad "fleet-up starts board-serve in a new switchboard session" "$out"
out="$(FAKE_SB=1 fu --dry-run)"
grep -q "would run: tmux new-window -d -t =switchboard: -n serve " <<<"$out" \
  && ok "fleet-up adds a serve window to an existing switchboard session" || bad "fleet-up adds a serve window" "$out"
out="$(FAKE_SB=1 FAKE_SERVE_WIN=1 fu --dry-run)"
grep -q "window switchboard:serve exists — skip" <<<"$out" && ! grep -q "would run" <<<"$out" \
  && ok "fleet-up leaves an existing serve window alone" || bad "fleet-up leaves an existing serve window alone" "$out"
python3 -c 'import socket,sys,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(); time.sleep(5)' "$PORT" &
lpid=$!; sleep 0.3
out="$(fu --dry-run)"
kill "$lpid" 2>/dev/null; wait "$lpid" 2>/dev/null
grep -q "already answers — skip" <<<"$out" && ok "fleet-up skips when the port already answers" || bad "fleet-up skips when the port answers" "$out"
printf 'collector: false\nsessions: []\n' > "$BOARD_CONFIG_DIR/fleet.yaml"
out="$(fu --dry-run)"
grep -q " serve: \|-n serve" <<<"$out" && bad "serve off by default" "$out" || ok "serve off by default"

# ---------------------------------------------------------------- expose-cloudflare.sh
echo "expose-cloudflare:"
mkdir -p "$T/cfbin"
printf '#!/usr/bin/env bash\necho "$*" >> "%s/cloudflared.log"\n' "$T" > "$T/cfbin/cloudflared"
chmod +x "$T/cfbin/cloudflared"
cat > "$T/cf.yml" <<'EOF2'
tunnel: t1
credentials-file: /dev/null
ingress:
  - hostname: other.example.com
    service: http://127.0.0.1:8080
  - service: http_status:404
EOF2
sum0="$(cksum < "$T/cf.yml")"
xc() { PATH="$T/cfbin:$PATH" "$REPO/scripts/expose-cloudflare.sh" --config "$T/cf.yml" --port 39410 "$@" 2>&1; }
out="$(xc --hostname board.example.com --tunnel t1)"
eq "dry run by default leaves the config alone" "$(cksum < "$T/cf.yml")" "$sum0"
[ ! -e "$T/cloudflared.log" ] && ok "dry run runs no cloudflared command" || bad "dry run runs no cloudflared command" "$(cat "$T/cloudflared.log")"
grep -q "service: http://127.0.0.1:39410" <<<"$out" && grep -q "cloudflared tunnel route dns t1 board.example.com" <<<"$out" \
  && grep -q "Self-hosted" <<<"$out" && grep -q "aud: <Application Audience" <<<"$out" \
  && ok "dry run prints ingress, DNS and Access steps" || bad "dry run prints ingress, DNS and Access steps" "$out"
out="$(xc --hostname board.example.com --tunnel t1 --dry-run)"
eq "--dry-run leaves the config alone" "$(cksum < "$T/cf.yml")" "$sum0"
xc --hostname 'bad host;rm' --tunnel t1 >/dev/null; eq "bad host name refused" "$?" "1"
out="$(xc --hostname board.example.com --tunnel t1 --apply)"
eq "--apply inserts before the catch-all" "$(python3 -c 'import sys,yaml; print([r.get("hostname","*") for r in yaml.safe_load(open(sys.argv[1]))["ingress"]])' "$T/cf.yml")" \
  "['other.example.com', 'board.example.com', '*']"
ls "$T"/cf.yml.bak.* >/dev/null 2>&1 && ok "--apply keeps a backup" || bad "--apply keeps a backup"
grep -q '^tunnel route dns t1 board.example.com$' "$T/cloudflared.log" && ok "--apply routes DNS" || bad "--apply routes DNS" "$(cat "$T/cloudflared.log" 2>/dev/null)"
sum1="$(cksum < "$T/cf.yml")"
out="$(xc --hostname board.example.com --tunnel t1 --apply)"
eq "--apply twice adds nothing" "$(cksum < "$T/cf.yml")" "$sum1"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
