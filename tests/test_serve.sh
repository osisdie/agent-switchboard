#!/usr/bin/env bash
# shellcheck disable=SC2015,SC2016  # ok/bad always return 0; jq programs are single-quoted
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
unset TMUX_PANE TMUX BOARD_PUBLIC_URL
while IFS= read -r v; do unset "$v"; done < <(compgen -e | grep -E '^(SLACK_|SMTP_|ALERT_|LINE_|AWS_)')
export ALERT_DESKTOP=0
mkdir -p "$BOARD_HOME"/{state,decisions,tasks,messages/m-1-a/replies} "$BOARD_CONFIG_DIR" "$CLAUDE_SESSIONS_DIR" "$T/bin"

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
 "pending":{"tool_use_id":"tu_1","tool_name":"Bash","summary":"rm -rf build","summary_sha256":"sha-1"},"updated_at":$NOW}
EOF
dec() {  # dec ID KIND EXTRA_JSON
  jq -n --arg id "$1" --arg kind "$2" --argjson now "$NOW" --argjson x "${3:-{\}}" \
    '{id: $id, kind: $kind, from: "CP", question: "Q?", options: ["A","B"], status: "pending",
      answer: null, answered_by: null, answered_at: null, relayed_at: null, created_at: $now} + $x' \
    > "$BOARD_HOME/decisions/$1.json"
}
dec d-1-q1 question
dec d-1-q2 question
dec d-1-q3 question
dec d-1-gate question '{"task_id":"t-1","stage":"build","gate":"owner","options":["approve","request changes"]}'
dec d-1-prod question '{"task_id":"t-1","stage":"production","gate":"production","options":["approve","request changes"]}'
dec d-1-perm permission "{\"from\":null,\"session_id\":\"$SID\",\"tool_use_id\":\"tu_1\",\"tool_name\":\"Bash\",\"summary\":\"rm -rf build\",\"summary_sha256\":\"sha-1\"}"
cat > "$BOARD_HOME/tasks/t-1.json" <<EOF
{"id":"t-1","title":"Ship it","owner":"CP","stage":"build","artifacts":{},"gates":[{"stage":"build","gate":"owner","status":"pending","decision_id":"d-1-gate"}],"updated_at":$NOW}
EOF
printf '%s\n' '{"ts":1,"type":"hook","event":"Stop"}' \
  '{"ts":2,"type":"approval","decision_id":"d-1-old","task_id":"t-1","stage":"plan","gate":"owner","answer":"approve","result":"approved","answered_by":"local","answered_at":2,"relayed_at":null}' \
  > "$BOARD_HOME/events.jsonl"
echo '{"id":"m-1-a","to":"CP","body":"hello","by":"local","created_at":1,"status":"replied"}' > "$BOARD_HOME/messages/m-1-a.json"
echo '{"from":"CP","body":"hi back","created_at":2}' > "$BOARD_HOME/messages/m-1-a/replies/r-1.json"
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

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
PORT="$(free_port)"; TPORT="$(free_port)"
B="http://127.0.0.1:$PORT"
TB="http://127.0.0.1:$TPORT"
ORIGIN="Origin: $B"
TUNNEL_HDRS=(-H 'Host: board.example.com' -H 'Origin: https://board.example.com')

write_cfg() {  # write_cfg EXTRA_SERVE_YAML [APPROVERS_YAML]
  cat > "$BOARD_CONFIG_DIR/fleet.yaml" <<EOF
approvers: ${2:-[local, boss@example.com]}
release_managers: [rm@example.com]
sessions:
  - code: CP
    cwd: /w
serve:
  port: $PORT
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
body() { jq -r "$@" "$T/body"; }

write_cfg ""
start

# ---------------------------------------------------------------- state
echo "state:"
eq "GET /api/state 200" "$(req GET /api/state)" "200"
eq "state keys" "$(body 'keys | join(",")')" "approvals,can_message,consensus,decisions,generated_at,ledger,me,messages,mode,notify,operator,owner_away,replies,sessions,tasks,writable"
eq "me is local on loopback" "$(body .me)" "local"
eq "mode local" "$(body .mode)" "local"
eq "session CP needs-you" "$(body '.sessions[] | select(.code=="CP") | .state')" "needs-you"
eq "session fields are an allowlist (no cwd/pid)" "$(body '[.sessions[] | has("cwd") or has("pid") or has("session_id")] | any')" "false"
eq "permission decision gets its from" "$(body '.decisions[] | select(.id=="d-1-perm") | .from')" "CP"
eq "decisions omit session_id" "$(body '[.decisions[] | has("session_id")] | any')" "false"
eq "tasks" "$(body '.tasks[0].title')" "Ship it"
eq "approvals from events.jsonl" "$(body '.approvals | length')" "1"
eq "approval row" "$(body '.approvals[0].decision_id + "/" + .approvals[0].result')" "d-1-old/approved"
eq "messages" "$(body '.messages[0].body')" "hello"
eq "replies" "$(body '.replies["m-1-a"][0].body')" "hi back"
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
# Alert links open the board on a card: /?d=<id> (m=, c=) serves the same page; the server ignores
# the query (the page reads it) and never reflects it.
for q in "/?d=d-1-perm" "/?m=m-1-a" "/?c=c-1-x" "/index.html?d=d-1-perm" "/?d=%3Cscript%3Ealert(1)%3C/script%3E"; do
  eq "GET $q 200" "$(req GET "$q")" "200"
  grep -q '<meta name="board-mode" content="local">' "$T/body" && ok "  $q serves the page" || bad "  $q serves the page"
done
grep -qiE '%3Cscript|alert\(1\)' "$T/body" && bad "  the query is not reflected" || ok "  the query is not reflected"
eq "HEAD /?d=... 200" "$(req HEAD "/?d=d-1-perm" -I)" "200"

# ---------------------------------------------------------------- allowlist / traversal / host
echo "allowlist:"
for p in /../../etc/passwd /%2e%2e/%2e%2e/etc/passwd /artifact/board.html /lib/serve.py /events.jsonl /api/../snapshot.json /api/state/../../x; do
  eq "GET $p refused" "$(req GET "$p")" "404"
done
eq "decision id with traversal refused" "$(post '/api/decisions/..%2F..%2Fevil/answer' '{"answer":"A"}')" "400"
eq "dotted decision id refused" "$(post '/api/decisions/../answer' '{"answer":"A"}')" "400"
eq "unknown decision 404" "$(post '/api/decisions/d-1-nope/answer' '{"answer":"A"}')" "404"
eq "foreign Host refused (DNS rebinding)" "$(req GET /api/state -H 'Host: evil.example:'"$PORT")" "421"
eq "localhost:port is accepted" "$(req GET /api/state -H "Host: localhost:$PORT")" "200"
eq "loopback name on another port refused" "$(req GET /api/state -H 'Host: 127.0.0.1:1')" "421"
eq "the tunnel hostname is not served on the local listener" "$(req GET /api/state -H 'Host: board.example.com')" "421"
eq "POST with a foreign Host refused" "$(post /api/messages '{"to":"CP","body":"x"}' -H 'Host: evil.example')" "421"

# ---------------------------------------------------------------- CSRF + body checks
echo "csrf:"
eq "POST without X-Board refused" "$(req POST /api/messages -H 'Content-Type: application/json' -H "$ORIGIN" --data-binary '{"to":"CP","body":"x"}')" "403"
eq "  error csrf" "$(body .error)" "csrf"
eq "POST without Origin refused" "$(req POST /api/messages -H 'Content-Type: application/json' -H 'X-Board: 1' --data-binary '{"to":"CP","body":"x"}')" "403"
eq "POST with Origin null refused" "$(req POST /api/messages -H 'Content-Type: application/json' -H 'X-Board: 1' -H 'Origin: null' --data-binary '{"to":"CP","body":"x"}')" "403"
eq "POST with Origin of the other loopback name accepted" "$(req POST /api/messages -H 'Content-Type: application/json' -H 'X-Board: 1' -H "Origin: http://localhost:$PORT" --data-binary '{"to":"CP","body":"via localhost"}')" "201"
rm -f "$BOARD_HOME/messages/$(body .id).json"
eq "POST with an https origin on the local listener refused" "$(req POST /api/messages -H 'Content-Type: application/json' -H 'X-Board: 1' -H "Origin: https://127.0.0.1:$PORT" --data-binary '{"to":"CP","body":"x"}')" "403"
eq "OPTIONS is 405" "$(req OPTIONS /api/messages -H "$ORIGIN" -H 'Access-Control-Request-Method: POST' -D "$T/hdr")" "405"
grep -qi '^access-control-' "$T/hdr" && bad "no Access-Control-* headers" "$(cat "$T/hdr")" || ok "no Access-Control-* headers"
eq "PUT is 405" "$(req PUT /api/messages)" "405"
req GET /api/state -D "$T/hdr" >/dev/null
grep -qi '^access-control-' "$T/hdr" && bad "no Access-Control-* headers on GET" || ok "no Access-Control-* headers on GET"
eq "POST cross-origin refused" "$(req POST /api/messages -H 'Content-Type: application/json' -H 'X-Board: 1' -H 'Origin: https://evil.example' --data-binary '{"to":"CP","body":"x"}')" "403"
eq "POST text/plain refused" "$(req POST /api/messages -H 'Content-Type: text/plain' -H 'X-Board: 1' -H "$ORIGIN" --data-binary '{"to":"CP","body":"x"}')" "415"
big="$(python3 -c 'import json; print(json.dumps({"to":"CP","body":"x"*20000}))')"
eq "POST over the size limit refused" "$(post /api/messages "$big")" "413"
eq "POST non-JSON refused" "$(post /api/messages 'not json')" "400"
eq "bad to refused" "$(post /api/messages '{"to":"../x","body":"x"}')" "400"
eq "to with a trailing newline refused" "$(post /api/messages '{"to":"CP\n","body":"x"}')" "400"
eq "no message written by refused POSTs" "$(find "$BOARD_HOME/messages" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')" "1"

# ---------------------------------------------------------------- messages
echo "messages:"
eq "POST /api/messages 201" "$(post /api/messages '{"to":"CP","body":" please rebase "}')" "201"
mid="$(body .id)"
mf="$BOARD_HOME/messages/$mid.json"
[ -f "$mf" ] && ok "message file written" || bad "message file written" "$mid"
eq "message fields" "$(jq -r '[.to, .body, .by, .status] | join("|")' "$mf" 2>/dev/null)" "CP|please rebase|local|new"
eq "message write logged with identity" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="message")] | last | .by + "/" + .via' "$BOARD_HOME/events.jsonl")" "local/loopback"

# ---------------------------------------------------------------- owner away (POST /api/away)
echo "away:"
AWF="$BOARD_HOME/owner-away"
eq "state: owner_away null" "$(req GET /api/state >/dev/null; body '.owner_away')" "null"
eq "state: consensus empty" "$(body '.consensus | length')" "0"
eq "bad state refused" "$(post /api/away '{"state":"maybe"}')" "400"
eq "  error bad_state" "$(body .error)" "bad_state"
eq "missing state refused" "$(post /api/away '{"said":"x"}')" "400"
eq "said not a string refused" "$(post /api/away '{"state":"on","said":42}')" "400"
eq "  error bad_said" "$(body .error)" "bad_said"
long_said="$(python3 -c 'import json; print(json.dumps({"state":"on","said":"x"*600}))')"
eq "said over the limit refused" "$(post /api/away "$long_said")" "400"
eq "away without X-Board refused" "$(req POST /api/away -H 'Content-Type: application/json' -H "$ORIGIN" --data-binary '{"state":"on"}')" "403"
eq "away cross-origin refused" "$(req POST /api/away -H 'Content-Type: application/json' -H 'X-Board: 1' -H 'Origin: https://evil.example' --data-binary '{"state":"on"}')" "403"
eq "away text/plain refused" "$(req POST /api/away -H 'Content-Type: text/plain' -H 'X-Board: 1' -H "$ORIGIN" --data-binary '{"state":"on"}')" "415"
big_away="$(python3 -c 'import json; print(json.dumps({"state":"on","said":"x"*20000}))')"
eq "away over the body cap refused" "$(post /api/away "$big_away")" "413"
[ -e "$AWF" ] && bad "refused POSTs write nothing" || ok "refused POSTs write nothing"
eq "approver turns away on" "$(post /api/away '{"state":"on","said":"back tomorrow; agree among yourselves"}')" "200"
eq "  reply" "$(body '"\(.away)/\(.status)"')" "true/on"
eq "  file by board:<identity>" "$(jq -r '.by + "|" + .said' "$AWF")" "board:local|back tomorrow; agree among yourselves"
eq "  file mode 0600" "$(stat -c %a "$AWF")" "600"
eq "  state shows owner_away" "$(req GET /api/state >/dev/null; body '.owner_away.by')" "board:local"
eq "  audited" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="away")] | last | .by + "/" + .state + "/" + .result' "$BOARD_HOME/events.jsonl")" "local/on/on"
eq "on again: unchanged" "$(post /api/away '{"state":"on","said":"other words"}')" "200"
eq "  status unchanged, record kept" "$(body .status)/$(jq -r .said "$AWF")" "unchanged/back tomorrow; agree among yourselves"
cid="$("$REPO/bin/board" consensus propose --from CP --to API --ref PR-1 "bump the shared fixture" 2>/dev/null)"
eq "state lists the consensus item" "$(req GET /api/state >/dev/null; body --arg id "$cid" '.consensus[] | select(.id == $id) | .status + "/" + (.participants | join(","))')" "pending/CP,API"
eq "approver turns away off" "$(post /api/away '{"state":"off","said":"I am back"}')" "200"
eq "  reply" "$(body '"\(.away)/\(.status)"')" "false/off"
[ -e "$AWF" ] && bad "  file removed" || ok "  file removed"
eq "  pending consensus went back to the owner" "$(jq -r .status "$BOARD_HOME/consensus/$cid.json")" "owner_back"
eq "  state: owner_away null again" "$(req GET /api/state >/dev/null; body '.owner_away')" "null"
eq "off again: unchanged" "$(post /api/away '{"state":"off"}')" "200"
eq "  status" "$(body .status)" "unchanged"

# ---------------------------------------------------------------- notification controls
echo "notify prefs:"
NP="$BOARD_HOME/notify-prefs.json"
evlast() { jq -s -r --arg a "$1" '[.[] | select(.type=="serve" and .action==$a)] | last | '"$2" "$BOARD_HOME/events.jsonl"; }
eq "state.notify: nothing muted" "$(req GET /api/state >/dev/null; body '.notify.mutes | length')" "0"
eq "  channels are booleans (none configured here)" "$(body '[.notify.channels[]] | map(type) | unique | join(",")')" "boolean"
eq "  breakthrough level" "$(body .notify.breakthrough)" "urgent"
eq "  no watcher list in the state" "$(body '.notify.presence | has("present")')" "false"
for b in '{"op":"snooze","channel":"slack"}' '{"op":"mute","channel":"pager"}' '{"op":"mute","channel":"slack","for":"99x"}'          '{"op":"mute","channel":"slack","for":"1h","until":"12:00"}' '{"op":"mute","channel":"slack","until":"2001-01-01T00:00:00Z"}'          '{"op":"mute","channel":"slack","for":"31d"}' '{"op":"mute","channel":"slack","for":1}' '{"op":"mute","channel":"slack","include_urgent":"yes"}'          '{"op":"level","channel":"slack","level":"loud"}' '{"op":"presence","reduce":"yes"}' '{"op":"presence","reduce":true,"level":"info"}'          '{"op":"mute","channel":"slack","for":"--help"}' '{"op":"level","channel":"--by","level":"high"}'; do
  eq "bad body refused: $b" "$(post /api/notify-prefs "$b")" "400"
done
[ -e "$NP" ] && bad "  refused POSTs write nothing" || ok "  refused POSTs write nothing"
eq "notify-prefs without X-Board refused" "$(req POST /api/notify-prefs -H 'Content-Type: application/json' -H "$ORIGIN" --data-binary '{"op":"unmute","channel":"all"}')" "403"
eq "notify-prefs cross-origin refused" "$(req POST /api/notify-prefs -H 'Content-Type: application/json' -H 'X-Board: 1' -H 'Origin: https://evil.example' --data-binary '{"op":"unmute","channel":"all"}')" "403"
eq "notify-prefs text/plain refused" "$(req POST /api/notify-prefs -H 'Content-Type: text/plain' -H 'X-Board: 1' -H "$ORIGIN" --data-binary '{"op":"unmute","channel":"all"}')" "415"
big_np="$(python3 -c 'import json; print(json.dumps({"op":"unmute","channel":"all","pad":"x"*20000}))')"
eq "notify-prefs over the body cap refused" "$(post /api/notify-prefs "$big_np")" "413"
eq "notify-prefs non-JSON refused" "$(post /api/notify-prefs 'mute slack')" "400"
eq "notify-prefs with a forwarded header refused" "$(post /api/notify-prefs '{"op":"unmute","channel":"all"}' -H 'X-Forwarded-For: 203.0.113.9')" "403"
eq "approver mutes slack for 1h" "$(post /api/notify-prefs '{"op":"mute","channel":"slack","for":"1h"}')" "200"
eq "  reply carries the new state" "$(body '.notify.mutes.slack.by')" "board:local"
eq "  file written by the CLI, 0600" "$(jq -r '.mutes.slack.by' "$NP")/$(stat -c %a "$NP")" "board:local/600"
eq "  audited" "$(evlast notify-prefs '.by + "/" + .op + "/" + .channel + "/" + .result')" "local/mute/slack/ok"
eq "  state shows it" "$(req GET /api/state >/dev/null; body '.notify.mutes.slack.include_urgent')" "false"
eq "mute all until an ISO time, urgent too" "$(post /api/notify-prefs "{\"op\":\"mute\",\"channel\":\"all\",\"until\":\"$(date -u -d '+3 hours' +%Y-%m-%dT%H:%M:%SZ)\",\"include_urgent\":true}")" "200"
eq "  include_urgent stored" "$(jq -r '.mutes.all.include_urgent' "$NP")" "true"
eq "unmute all" "$(post /api/notify-prefs '{"op":"unmute","channel":"all"}')" "200"
eq "  every mute gone" "$(jq -c .mutes "$NP")" "{}"
eq "slack high and up" "$(post /api/notify-prefs '{"op":"level","channel":"slack","level":"high"}')" "200"
eq "  effective level in the reply" "$(body .notify.effective_min_level.slack)" "high"
eq "level default" "$(post /api/notify-prefs '{"op":"level","channel":"slack","level":"default"}')" "200"
eq "  back to info" "$(body .notify.effective_min_level.slack)" "info"
eq "presence off" "$(post /api/notify-prefs '{"op":"presence","reduce":false}')" "200"
eq "  reply" "$(body .notify.presence.reduce)" "false"
eq "presence on at urgent" "$(post /api/notify-prefs '{"op":"presence","reduce":true,"level":"urgent"}')" "200"
eq "  reply" "$(body '"\(.notify.presence.reduce)/\(.notify.presence.min_level)"')" "true/urgent"
echo "presence:"
PRF="$BOARD_HOME/presence.json"
for b in '{}' '{"visible":"yes"}' '{"visible":true,"interaction_at":"now"}' '{"visible":true,"interaction_at":-5}' '{"visible":true,"interaction_at":true}' \
         '{"visible":true,"idle_ms":"1s"}' '{"visible":true,"idle_ms":1.5}' '{"visible":true,"idle_ms":false}'; do
  eq "bad heartbeat refused: $b" "$(post /api/presence "$b")" "400"
done
eq "presence without X-Board refused" "$(req POST /api/presence -H 'Content-Type: application/json' -H "$ORIGIN" --data-binary '{"visible":true}')" "403"
eq "heartbeat: visible" "$(post /api/presence "{\"visible\":true,\"interaction_at\":$(date +%s)000}")" "200"
eq "  recorded under the server's identity" "$(jq -r '.local.visible|tostring' "$PRF")/$(jq -r '.local.via' "$PRF")" "true/loopback"
eq "  presence.json 0600" "$(stat -c %a "$PRF")" "600"
eq "  watching now (board mute status)" "$("$REPO/bin/board" mute status --json | jq -r '.presence.present | join(",")')" "local"
n_pres() { jq -s '[.[] | select(.type=="serve" and .action=="presence")] | length' "$BOARD_HOME/events.jsonl"; }
before="$(n_pres)"
post /api/presence '{"visible":true,"interaction_at":null}' >/dev/null
eq "  a routine heartbeat is not audited" "$(n_pres)" "$before"
watching() { "$REPO/bin/board" mute status --json | jq -r '.presence.present | join(",")'; }
eq "  a far-future interaction is unknown" "$(post /api/presence '{"visible":true,"interaction_at":99999999999999}'; jq -r '.local.interaction_at' "$PRF")" "200null"
eq "  page clock 1 h ahead (interaction_at): not watching" "$(post /api/presence "{\"visible\":true,\"interaction_at\":$(( $(date +%s) + 3600 ))000}" >/dev/null; watching)" ""
eq "  interaction_at up to 5 s ahead is now" "$(post /api/presence "{\"visible\":true,\"interaction_at\":$(( $(date +%s) + 3 ))000}" >/dev/null; watching)" "local"
eq "  idle_ms 1 s: watching" "$(post /api/presence '{"visible":true,"idle_ms":1000}' >/dev/null; watching)" "local"
t0="$(date +%s%3N)"; post /api/presence '{"visible":true,"idle_ms":60000}' >/dev/null
eq "  idle_ms is applied to the server's clock" "$(jq -r --argjson t "$t0" '(.local.interaction_at - ($t - 60000)) | (. >= 0 and . < 5000)' "$PRF")" "true"
eq "  idle_ms 33 min: not watching (idle limit 15 min)" "$(post /api/presence '{"visible":true,"idle_ms":2000000}' >/dev/null; watching)" ""
eq "  negative idle_ms counts as 0" "$(post /api/presence '{"visible":true,"idle_ms":-99999999}' >/dev/null; watching)" "local"
eq "heartbeat: hidden" "$(post /api/presence '{"visible":false}')" "200"
eq "  visibility change audited" "$(n_pres)" "$((before + 1))"
eq "  nobody watching now" "$("$REPO/bin/board" mute status --json | jq -r '.presence.present | length')" "0"
eq "  the presence file does not change the state stream" "$(req GET /api/state >/dev/null; body 'has("presence")')" "false"
rm -f "$NP" "$PRF"

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
with open(os.path.join(home, "messages", "m-2-sse.json"), "w") as fh:
    json.dump({"id": "m-2-sse", "to": "all", "body": "sse!", "by": "local", "created_at": int(time.time() * 1000), "status": "new"}, fh)
t0 = time.time()
second = event()
print("second", [m["id"] for m in second["messages"]][0], "%.1f" % (time.time() - t0))
EOF
sse_pid=$!
for _ in $(seq 1 80); do kill -0 "$sse_pid" 2>/dev/null || break; sleep 0.1; done
kill "$sse_pid" 2>/dev/null
grep -q '^ctype text/event-stream' "$T/sse.out" && ok "SSE content type" || bad "SSE content type" "$(cat "$T/sse.out")"
grep -q '^first 2$' "$T/sse.out" && ok "SSE sends the state at once" || bad "SSE sends the state at once" "$(cat "$T/sse.out")"
grep -q '^second m-2-sse ' "$T/sse.out" && ok "SSE pushes a change" || bad "SSE pushes a change" "$(cat "$T/sse.out")"

# ---------------------------------------------------------------- answers (loopback)
echo "answers:"
eq "answer from loopback 200" "$(post /api/decisions/d-1-q1/answer '{"answer":"A"}')" "200"
eq "  status answered" "$(jq -r '.status + "/" + .answered_by + "/" + .answer' "$BOARD_HOME/decisions/d-1-q1.json")" "answered/local/A"
eq "  answer logged with identity" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="answer")] | last | .by + "/" + .result' "$BOARD_HOME/events.jsonl")" "local/answered"
eq "gate answer 200" "$(post /api/decisions/d-1-gate/answer '{"answer":"approve"}')" "200"
eq "  gate approved on the task" "$(jq -r '.gates[0].status + "/" + .gates[0].approved_by' "$BOARD_HOME/tasks/t-1.json")" "approved/local"
eq "  approval shows up in state" "$(req GET /api/state >/dev/null; body '[.approvals[].decision_id] | index("d-1-gate") != null')" "true"
eq "production gate by local refused (release_managers)" "$(post /api/decisions/d-1-prod/answer '{"answer":"approve"}')" "403"
eq "  error not_approver" "$(body .error)" "not_approver"
eq "  file untouched (still pending)" "$(jq -r .status "$BOARD_HOME/decisions/d-1-prod.json")" "pending"
eq "  refusal audited" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="answer")] | last | .result + "/" + .reason' "$BOARD_HOME/events.jsonl")" "refused/not an approver"
eq "a free-text answer '--help' is recorded literally" "$(post /api/decisions/d-1-q3/answer '{"answer":"--help"}')" "200"
eq "  answer is --help" "$(jq -r '.status + "/" + .answer' "$BOARD_HOME/decisions/d-1-q3.json")" "answered/--help"
dec d-1-q3 question
eq "can_message for an approver" "$(req GET /api/state >/dev/null; body .can_message)" "true"
eq "empty answer refused" "$(post /api/decisions/d-1-q2/answer '{"answer":"  "}')" "400"
eq "permission answer must be allow/deny" "$(post /api/decisions/d-1-perm/answer '{"answer":"maybe"}')" "400"

# ---------------------------------------------------------------- permission -> board press
echo "permission:"
: > "$FAKE_TMUX_LOG"
eq "permission allow 200" "$(post /api/decisions/d-1-perm/answer '{"answer":"allow"}')" "200"
eq "  status relayed" "$(body .status)" "relayed"
eq "  no CLI output in the response" "$(body 'has("note")')" "false"
grep -q '^send-keys -t %42 1$' "$FAKE_TMUX_LOG" && ok "  board press sent the allow key" || bad "  board press sent the allow key" "$(cat "$FAKE_TMUX_LOG")"
eq "  decision file relayed" "$(jq -r .status "$BOARD_HOME/decisions/d-1-perm.json")" "relayed"
eq "  press logged with identity" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="press")] | last | .by + "/" + .result' "$BOARD_HOME/events.jsonl")" "local/sent"
eq "answering a relayed decision again 409" "$(post /api/decisions/d-1-perm/answer '{"answer":"deny"}')" "409"
: > "$FAKE_TMUX_LOG"
# A new prompt (tu_2) that is no longer on screen when the answer arrives.
jq '.pending.tool_use_id = "tu_2" | .pending.summary_sha256 = "sha-2"' "$BOARD_HOME/state/$SID.json" > "$T/st" && mv "$T/st" "$BOARD_HOME/state/$SID.json"
dec d-1-permb permission "{\"from\":null,\"session_id\":\"$SID\",\"tool_use_id\":\"tu_2\",\"tool_name\":\"Bash\",\"summary\":\"rm -rf build\",\"summary_sha256\":\"sha-2\"}"
echo '$ just a shell' > "$FAKE_TMUX_SCREEN"
eq "stale permission (prompt gone from the pane) 409" "$(post /api/decisions/d-1-permb/answer '{"answer":"allow"}')" "409"
eq "  error stale" "$(body .error)" "stale"
grep -qE 'pane|%4|board press|tmux' "$T/body" && bad "  no board/tmux internals in the error" "$(cat "$T/body")" || ok "  no board/tmux internals in the error"
grep -q 'board press d-1-permb' "$T/serve.err" && ok "  details logged on stderr" || bad "  details logged on stderr"
grep -q 'send-keys' "$FAKE_TMUX_LOG" && bad "  no keys sent on stale" "$(cat "$FAKE_TMUX_LOG")" || ok "  no keys sent on stale"
eq "  decision file stale" "$(jq -r .status "$BOARD_HOME/decisions/d-1-permb.json")" "stale"

# ---------------------------------------------------------------- local listener: forwarded = refused
echo "local listener:"
for hd in 'Cf-Connecting-IP: 203.0.113.9' 'Cf-Ray: 1-x' 'X-Forwarded-For: 203.0.113.9' 'Forwarded: for=203.0.113.9' \
          'Cf-Access-Authenticated-User-Email: boss@example.com' 'Cf-Access-Jwt-Assertion: a.b.c'; do
  eq "GET with '${hd%%:*}' refused" "$(req GET /api/state -H "$hd")" "403"
done
eq "  error forwarded_refused" "$(body .error)" "forwarded_refused"
eq "GET / with X-Forwarded-For refused" "$(req GET / -H 'X-Forwarded-For: 203.0.113.9')" "403"
eq "answer with a Cloudflare email header refused" "$(post /api/decisions/d-1-q2/answer '{"answer":"A"}' -H 'Cf-Access-Authenticated-User-Email: boss@example.com')" "403"
eq "  decision untouched" "$(jq -r .status "$BOARD_HOME/decisions/d-1-q2.json")" "pending"

# ---------------------------------------------------------------- approvers fail closed
echo "approvers:"
write_cfg "" "[]"
start
eq "empty approvers: answer refused" "$(post /api/decisions/d-1-q2/answer '{"answer":"A"}')" "403"
eq "  error no_approvers" "$(body .error)" "no_approvers"
eq "  decision untouched" "$(jq -r .status "$BOARD_HOME/decisions/d-1-q2.json")" "pending"
eq "  state says nobody is an approver" "$(req GET /api/state >/dev/null; body '.operator.approver_ids | length')" "0"
eq "  --check says answers are refused" "$("$REPO/bin/board-serve" --check | grep -c 'answers  REFUSED')" "1"
eq "empty approvers: away refused" "$(post /api/away '{"state":"on"}')" "403"
eq "  error no_approvers" "$(body .error)" "no_approvers"
eq "empty approvers: notify-prefs refused" "$(post /api/notify-prefs '{"op":"mute","channel":"all"}')" "403"
eq "  error no_approvers" "$(body .error)" "no_approvers"
eq "empty approvers: presence refused" "$(post /api/presence '{"visible":true}')" "403"
eq "  error no_approvers" "$(body .error)" "no_approvers"
[ -e "$BOARD_HOME/notify-prefs.json" ] || [ -e "$BOARD_HOME/presence.json" ] && bad "  nothing written" || ok "  nothing written"
write_cfg "" "[boss@example.com]"
start
eq "approvers without local: loopback answer refused" "$(post /api/decisions/d-1-q2/answer '{"answer":"A"}')" "403"
eq "  error not_approver" "$(body .error)" "not_approver"
eq "  file untouched" "$(jq -r '.status + "/" + (.answered_by // "none")' "$BOARD_HOME/decisions/d-1-q2.json")" "pending/none"
eq "  message refused for a non-approver" "$(post /api/messages '{"to":"CP","body":"x"}')" "403"
eq "  can_message false" "$(req GET /api/state >/dev/null; body .can_message)" "false"
eq "  away refused for a non-approver" "$(post /api/away '{"state":"on","said":"x"}')" "403"
eq "  error not_approver" "$(body .error)" "not_approver"
[ -e "$BOARD_HOME/owner-away" ] && bad "  no owner-away file" || ok "  no owner-away file"
eq "  refusal audited" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="away")] | last | .result + "/" + .reason' "$BOARD_HOME/events.jsonl")" "refused/not an approver"
eq "  notify-prefs refused for a non-approver" "$(post /api/notify-prefs '{"op":"mute","channel":"slack","for":"1h"}')" "403"
eq "  error not_approver" "$(body .error)" "not_approver"
eq "  refusal audited" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="notify-prefs")] | last | .result + "/" + .reason + "/" + .channel' "$BOARD_HOME/events.jsonl")" "refused/not an approver/slack"
eq "  presence refused for a non-approver" "$(post /api/presence '{"visible":true}')" "403"
[ -e "$BOARD_HOME/notify-prefs.json" ] || [ -e "$BOARD_HOME/presence.json" ] && bad "  nothing written" || ok "  nothing written"
# MUST-FIX: a non-approver must never cancel an approver's answer that waits for the relay.
dec d-1-q6 question
BOARD_HOME="$BOARD_HOME" "$REPO/bin/board" answer d-1-q6 "ship it" --by boss@example.com >/dev/null
eq "approver's answer recorded" "$(jq -r '.status + "/" + .answered_by' "$BOARD_HOME/decisions/d-1-q6.json")" "answered/boss@example.com"
eq "non-approver (local) posts on it: refused" "$(post /api/decisions/d-1-q6/answer '{"answer":"no"}')" "403"
eq "  still answered by the approver" "$(jq -r '.status + "/" + .answered_by + "/" + .answer' "$BOARD_HOME/decisions/d-1-q6.json")" "answered/boss@example.com/ship it"
BOARD_HOME="$BOARD_HOME" "$REPO/bin/board" answer d-1-q6 "no" --by intruder@example.com >/dev/null 2>&1; rc=$?
eq "board answer by a non-approver on an answered decision exits 4" "$rc" "4"
eq "  no write" "$(jq -r '.status + "/" + .answered_by + "/" + .answer' "$BOARD_HOME/decisions/d-1-q6.json")" "answered/boss@example.com/ship it"
eq "  attempt logged" "$(jq -s -r '[.[] | select(.type=="unauthorized" and .id=="d-1-q6")] | last | .by' "$BOARD_HOME/events.jsonl")" "intruder@example.com"
dec d-1-q7 question
BOARD_HOME="$BOARD_HOME" "$REPO/bin/board" answer d-1-q7 --help --by boss@example.com >/dev/null; rc=$?
eq "board answer ID --help --by WHO records '--help'" "$rc/$(jq -r .answer "$BOARD_HOME/decisions/d-1-q7.json")" "0/--help"
eq "board answer --help still prints help" "$("$REPO/bin/board" answer --help | head -1 | cut -c1-12)" "usage: board"
dec d-1-q2 question
write_cfg ""
start
dec d-1-trunc permission "{\"from\":\"CP\",\"session_id\":\"$SID\",\"tool_use_id\":\"tu_2\",\"tool_name\":\"Bash\",\"summary_truncated\":true}"
: > "$FAKE_TMUX_LOG"
eq "allow on a truncated summary refused" "$(post /api/decisions/d-1-trunc/answer '{"answer":"allow"}')" "409"
eq "  error review_in_session" "$(body .error)" "review_in_session"
grep -q send-keys "$FAKE_TMUX_LOG" && bad "  nothing pressed" || ok "  nothing pressed"
eq "  still pending" "$(jq -r .status "$BOARD_HOME/decisions/d-1-trunc.json")" "pending"
rm -f "$BOARD_HOME/decisions/d-1-trunc.json"

# A long input through the real hook: the decision carries summary_truncated, and Allow is refused.
SID2="bbbbbbbb-1111-2222-3333-444444444444"
long="$(printf 'x%.0s' $(seq 1 300))"
hk() { printf '%s' "$1" | TMUX_PANE=%43 "$REPO/hooks/board-hook.sh"; }
hk "{\"session_id\":\"$SID2\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_use_id\":\"tu_long\",\"tool_input\":{\"command\":\"echo $long\"}}"
hk "{\"session_id\":\"$SID2\",\"hook_event_name\":\"PermissionRequest\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo $long\"}}"
hid="$(jq -r 'select(.tool_use_id == "tu_long") | .id' "$BOARD_HOME"/decisions/*.json 2>/dev/null | head -1)"
[ -n "$hid" ] && ok "the hook wrote a permission decision" || bad "the hook wrote a permission decision"
eq "  state carries the hook's summary_truncated" "$(req GET /api/state >/dev/null; body --arg id "$hid" '.decisions[] | select(.id == $id) | .summary_truncated')" "true"
eq "  Allow refused (review in the session)" "$(post "/api/decisions/$hid/answer" '{"answer":"allow"}')" "409"
eq "  error review_in_session" "$(body .error)" "review_in_session"
rm -f "$BOARD_HOME/decisions/$hid.json" "$BOARD_HOME/state/$SID2.json"

# ---------------------------------------------------------------- tunnel listener (Cloudflare Access)
echo "tunnel listener:"
tunnel_cfg() {  # tunnel_cfg [AUD]
  write_cfg "  tunnel:
    enabled: true
    port: $TPORT
    hostname: board.example.com
    team_domain: team.example.com
    aud: ${1-AUD123}
    certs_url: file://$T/certs.json"
}
tunnel_cfg ""
"$REPO/bin/board-serve" --quiet >"$T/refuse.out" 2>&1; rc=$?
eq "tunnel without aud refuses to start" "$rc" "2"
grep -q 'aud is missing' "$T/refuse.out" && ok "  clear error" || bad "  clear error" "$(cat "$T/refuse.out")"
if python3 -c 'import cryptography' 2>/dev/null; then
  python3 - "$T" <<'EOF'
import base64, json, sys, time
from cryptography.hazmat.primitives import hashes
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
none_alg = b64(json.dumps({"alg": "none", "kid": "k1"}).encode()) + "." + b64(json.dumps(base).encode()) + "."
toks = {"good": tok(base), "badsig": tok(base, k=other), "wrongaud": tok(dict(base, aud=["X"])),
        "expired": tok(dict(base, exp=now - 3600)), "future": tok(dict(base, nbf=now + 3600)),
        "wrongiss": tok(dict(base, iss="https://evil.example")), "unknownkid": tok(base, kid="k9"),
        "nobody": tok(dict(base, email="intruder@example.com")), "noemail": tok({k: v for k, v in base.items() if k != "email"}),
        "rm": tok(dict(base, email="rm@example.com")), "alg_none": none_alg}
for k, v in toks.items():
    open("%s/jwt.%s" % (t, k), "w").write(v)
EOF
  tunnel_cfg
  start
  eq "--check shows the tunnel" "$("$REPO/bin/board-serve" --check | grep -c "^tunnel   http://127.0.0.1:$TPORT")" "1"
  jwt() { echo "Cf-Access-Jwt-Assertion: $(cat "$T/jwt.$1")"; }
  treq() { local m="$1" p="$2"; shift 2; curl -s --path-as-is -o "$T/body" -w '%{http_code}' -X "$m" "$@" "$TB$p"; }
  tpost() { local p="$1" d="$2"; shift 2; treq POST "$p" -H 'Content-Type: application/json' -H 'X-Board: 1' --data-binary "$d" "$@"; }
  eq "no JWT: refused (never local)" "$(treq GET /api/state "${TUNNEL_HDRS[@]}")" "401"
  eq "email header alone: refused" "$(treq GET /api/state "${TUNNEL_HDRS[@]}" -H 'Cf-Access-Authenticated-User-Email: boss@example.com')" "401"
  for k in badsig wrongaud expired future wrongiss unknownkid noemail alg_none; do
    eq "JWT $k refused" "$(treq GET /api/state "${TUNNEL_HDRS[@]}" -H "$(jwt "$k")")" "401"
  done
  eq "garbage JWT refused" "$(treq GET /api/state "${TUNNEL_HDRS[@]}" -H 'Cf-Access-Jwt-Assertion: a.b.c')" "401"
  eq "valid JWT: identity from the token" "$(treq GET /api/state "${TUNNEL_HDRS[@]}" -H "$(jwt good)" >/dev/null; body .me)" "boss@example.com"
  eq "the email header cannot override the token" "$(treq GET /api/state "${TUNNEL_HDRS[@]}" -H "$(jwt good)" -H 'Cf-Access-Authenticated-User-Email: rm@example.com' >/dev/null; body .me)" "boss@example.com"
  eq "tunnel: loopback Host refused" "$(treq GET /api/state -H "Host: 127.0.0.1:$TPORT" -H "$(jwt good)")" "421"
  eq "tunnel: foreign Host refused" "$(treq GET /api/state -H 'Host: evil.example' -H "$(jwt good)")" "421"
  eq "tunnel: page served" "$(treq GET / "${TUNNEL_HDRS[@]}" -H "$(jwt good)")" "200"
  eq "tunnel: alert link /?d= served" "$(treq GET "/?d=d-1-q4" "${TUNNEL_HDRS[@]}" -H "$(jwt good)")" "200"
  eq "tunnel: alert link still needs the token" "$(treq GET "/?d=d-1-q4" "${TUNNEL_HDRS[@]}")" "401"
  dec d-1-q4 question
  eq "tunnel: local Origin refused" "$(tpost /api/decisions/d-1-q4/answer '{"answer":"A"}' -H 'Host: board.example.com' -H "Origin: http://127.0.0.1:$TPORT" -H "$(jwt good)")" "403"
  eq "tunnel: missing Origin refused" "$(tpost /api/decisions/d-1-q4/answer '{"answer":"A"}' -H 'Host: board.example.com' -H "$(jwt good)")" "403"
  eq "tunnel: http:// origin refused" "$(tpost /api/decisions/d-1-q4/answer '{"answer":"A"}' -H 'Host: board.example.com' -H 'Origin: http://board.example.com' -H "$(jwt good)")" "403"
  eq "expired JWT: generic error text" "$(treq GET /api/state "${TUNNEL_HDRS[@]}" -H "$(jwt expired)" >/dev/null; body .detail)" "Cloudflare Access token rejected"
  grep -q 'Access token rejected (expired)' "$T/serve.err" && ok "  reason logged on stderr" || bad "  reason logged on stderr"
  eq "tunnel: approver answers" "$(tpost /api/decisions/d-1-q4/answer '{"answer":"A"}' "${TUNNEL_HDRS[@]}" -H "$(jwt good)")" "200"
  eq "  answered_by is the token's email" "$(jq -r .answered_by "$BOARD_HOME/decisions/d-1-q4.json")" "boss@example.com"
  eq "  logged via cf-jwt" "$(jq -s -r '[.[] | select(.type=="serve" and .action=="answer")] | last | .by + "/" + .via' "$BOARD_HOME/events.jsonl")" "boss@example.com/cf-jwt"
  dec d-1-q5 question
  eq "tunnel: a verified email outside the list gets 403 on state" "$(treq GET /api/state "${TUNNEL_HDRS[@]}" -H "$(jwt nobody)")" "403"
  eq "  error not_allowed" "$(body .error)" "not_allowed"
  eq "  ... and on the page" "$(treq GET / "${TUNNEL_HDRS[@]}" -H "$(jwt nobody)")" "403"
  eq "  ... and on answers" "$(tpost /api/decisions/d-1-q5/answer '{"answer":"A"}' "${TUNNEL_HDRS[@]}" -H "$(jwt nobody)")" "403"
  eq "  file untouched" "$(jq -r .status "$BOARD_HOME/decisions/d-1-q5.json")" "pending"
  dec d-1-prod2 question '{"task_id":"t-1","stage":"production","gate":"production","options":["approve","request changes"]}'
  eq "tunnel: an approver is not a release manager" "$(tpost /api/decisions/d-1-prod2/answer '{"answer":"approve"}' "${TUNNEL_HDRS[@]}" -H "$(jwt good)")" "403"
  dec d-1-prod2 question '{"task_id":"t-1","stage":"production","gate":"production","options":["approve","request changes"]}'
  eq "tunnel: the release manager approves production" "$(tpost /api/decisions/d-1-prod2/answer '{"answer":"approve"}' "${TUNNEL_HDRS[@]}" -H "$(jwt rm)")" "200"
  eq "tunnel: an approver mutes from the phone" "$(tpost /api/notify-prefs '{"op":"mute","channel":"email","for":"4h"}' "${TUNNEL_HDRS[@]}" -H "$(jwt good)")" "200"
  eq "  recorded as board:<email>" "$(jq -r .mutes.email.by "$BOARD_HOME/notify-prefs.json")" "board:boss@example.com"
  eq "  heartbeat from the phone: the token's email" "$(tpost /api/presence '{"visible":true}' "${TUNNEL_HDRS[@]}" -H "$(jwt good)" >/dev/null; jq -r '.["boss@example.com"].via' "$BOARD_HOME/presence.json")" "cf-jwt"
  eq "  a release manager who is not an approver may not" "$(tpost /api/notify-prefs '{"op":"unmute","channel":"all"}' "${TUNNEL_HDRS[@]}" -H "$(jwt rm)")" "403"
  rm -f "$BOARD_HOME/notify-prefs.json" "$BOARD_HOME/presence.json"
  eq "tunnel: messages carry the email" "$(tpost /api/messages '{"to":"CP","body":"from phone"}' "${TUNNEL_HDRS[@]}" -H "$(jwt good)" >/dev/null; jq -r .by "$BOARD_HOME/messages/$(body .id).json")" "boss@example.com"
  # serve.tunnel.allowed_emails lets a viewer in who is not an approver: they read, never decide.
  tunnel_cfg
  printf '    allowed_emails: [boss@example.com, intruder@example.com, rm@example.com]\n' >> "$BOARD_CONFIG_DIR/fleet.yaml"
  start
  eq "allowed_emails: a listed viewer reads the state" "$(treq GET /api/state "${TUNNEL_HDRS[@]}" -H "$(jwt nobody)")" "200"
  eq "  can_message false" "$(body .can_message)" "false"
  dec d-1-q8 question
  BOARD_HOME="$BOARD_HOME" "$REPO/bin/board" answer d-1-q8 "go" --by boss@example.com >/dev/null
  eq "  a viewer's answer on an approver-answered decision: 403" "$(tpost /api/decisions/d-1-q8/answer '{"answer":"stop"}' "${TUNNEL_HDRS[@]}" -H "$(jwt nobody)")" "403"
  eq "  error not_approver" "$(body .error)" "not_approver"
  eq "  still the approver's answer" "$(jq -r '.status + "/" + .answered_by + "/" + .answer' "$BOARD_HOME/decisions/d-1-q8.json")" "answered/boss@example.com/go"
  eq "  a viewer's message: 403" "$(tpost /api/messages '{"to":"CP","body":"x"}' "${TUNNEL_HDRS[@]}" -H "$(jwt nobody)")" "403"
  eq "local listener still local with the tunnel on" "$(req GET /api/state >/dev/null; body .me)" "local"
  eq "local listener still refuses forwarded requests" "$(req GET /api/state -H "$(jwt good)")" "403"
else
  echo "  (skipped: python3 cryptography module not installed)"
fi

# ---------------------------------------------------------------- email case
echo "email case:"
if python3 -c 'import cryptography' 2>/dev/null; then
  tunnel_cfg
  sed -i 's/^approvers: .*/approvers: [LOCAL, Boss@Example.COM]/' "$BOARD_CONFIG_DIR/fleet.yaml"
  start
  dec d-1-c1 question
  eq "mixed-case approver email matches the token's email" "$(tpost /api/decisions/d-1-c1/answer '{"answer":"A"}' "${TUNNEL_HDRS[@]}" -H "$(jwt good)")" "200"
  eq "  answered_by keeps the configured spelling" "$(jq -r .answered_by "$BOARD_HOME/decisions/d-1-c1.json")" "Boss@Example.COM"
  eq "  the tunnel lets the email in" "$(treq GET /api/state "${TUNNEL_HDRS[@]}" -H "$(jwt good)" >/dev/null; body .can_message)" "true"
  dec d-1-c2 question
  eq "'LOCAL' in approvers matches local" "$(post /api/decisions/d-1-c2/answer '{"answer":"A"}')" "200"
else
  echo "  (skipped: python3 cryptography module not installed)"
fi

# ---------------------------------------------------------------- connection limits (in-process)
echo "limits:"
kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null; SRV_PID=""
lim="$(python3 - "$REPO" "$BOARD_HOME" <<'EOF'
import socket, sys, threading, time
sys.path.insert(0, sys.argv[1] + "/lib")
import serve
print("timeout", serve.Handler.timeout)
serve.MAX_CONN, serve.MAX_SSE = 2, 1
home = sys.argv[2]
cfg = serve.settings(); hub = serve.Hub(home); lock = threading.Lock()
a = serve.Server("local", 0, home, cfg, hub, lock, quiet=True)
b = serve.Server("local", 0, home, cfg, hub, lock, quiet=True)
for s in (a, b):
    threading.Thread(target=s.serve_forever, daemon=True).start()
def get(srv, path, read=True):
    c = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
    c.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % (path, srv.server_address[1])).encode())
    return c, (c.recv(64).decode(errors="replace").split("\r\n")[0] if read else "")
# per-listener SSE cap: one stream on a, a second on a is refused, b still serves one
s1, l1 = get(a, "/api/events"); time.sleep(0.2)
s2, l2 = get(a, "/api/events")
s3, l3 = get(b, "/api/events")
print("sse", l1.split(" ")[1], l2.split(" ")[1], l3.split(" ")[1])
for x in (s1, s2, s3): x.close()
time.sleep(0.3)
# connection guard: two idle connections hold both slots; the third gets 503
i1 = socket.create_connection(("127.0.0.1", a.server_address[1])); i2 = socket.create_connection(("127.0.0.1", a.server_address[1]))
time.sleep(0.3)
c3, l4 = get(a, "/api/state")
print("conn", l4.split(" ")[1] if l4 else "closed")
EOF
)"
eq "Handler socket timeout is 60 s" "$(sed -n 's/^timeout //p' <<<"$lim")" "60"
eq "SSE cap is per listener" "$(sed -n 's/^sse //p' <<<"$lim")" "200 503 200"
eq "connections over the cap get 503" "$(sed -n 's/^conn //p' <<<"$lim")" "503"

# ---------------------------------------------------------------- misc
echo "misc:"
eq "--check prints the local address" "$("$REPO/bin/board-serve" --check | sed -n 's/^local *\([^ ]*\).*/\1/p')" "http://127.0.0.1:$PORT"
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
eq "--watch lists answered questions and new messages, oldest first" "$(grep -v '^away \|^consensus ' <<<"$w" | tr '\n' ',')" \
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
grep -q '^decision d-1-perm' <<<"$w" && bad "--watch skips permission decisions" "$w" || ok "--watch skips permission decisions"
grep -q '^message m-1-a ' <<<"$w" && bad "--watch skips replied messages" "$w" || ok "--watch skips replied messages"
"$REPO/bin/board-serve" --watch --tick 1 > "$T/watch.out" & wpid=$!
sleep 0.5
echo '{"id":"m-3-late","to":"CP","body":"x","by":"local","created_at":9,"status":"new"}' > "$BOARD_HOME/messages/m-3-late.json"
"$REPO/bin/board" away on --by CP --said "watch test" >/dev/null
wcid="$("$REPO/bin/board" consensus propose --from CP --to API "watch test" 2>/dev/null)"
sleep 2.2
kill "$wpid" 2>/dev/null; wait "$wpid" 2>/dev/null
eq "--watch prints a new message once" "$(grep -c '^message m-3-late new$' "$T/watch.out")" "1"
eq "--watch reports the owner-away state, then its change" "$(grep -c '^away off$' "$T/watch.out")/$(grep -c '^away on$' "$T/watch.out")" "1/1"
eq "--watch reports a consensus status once" "$(grep -c "^consensus $wcid pending\$" "$T/watch.out")" "1"
"$REPO/bin/board" away off --by CP >/dev/null
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
CFD="$T/cfdir"; mkdir -p "$CFD" "$T/cfbin"
cat > "$T/cfbin/cloudflared" <<EOF2
#!/usr/bin/env bash
echo "\$*" >> "$T/cloudflared.log"
case "\$*" in
  "tunnel list --output json") echo '[{"id":"1111-aaaa","name":"other"},{"id":"2222-bbbb","name":"board"}]' ;;
esac
exit 0
EOF2
cat > "$T/cfbin/curl" <<EOF2
#!/usr/bin/env bash
echo "\$*" >> "$T/curl.log"
cat "$T/access-apps.json"
EOF2
chmod +x "$T/cfbin/cloudflared" "$T/cfbin/curl"
echo '{}' > "$CFD/2222-bbbb.json"; echo '{}' > "$CFD/1111-aaaa.json"
cat > "$CFD/config.yml" <<'EOF2'
tunnel: other
credentials-file: /dev/null
ingress:
  - hostname: other.example.com
    service: http://127.0.0.1:8080
  - service: http_status:404
EOF2
sum0="$(cksum < "$CFD/config.yml")"
xc() { CLOUDFLARED_DIR="$CFD" PATH="$T/cfbin:$PATH" "$REPO/scripts/expose-cloudflare.sh" --port 39411 "$@" 2>&1; }
OUTF="$CFD/agent-switchboard-board.yml"
out="$(xc --hostname board.example.com --tunnel board)"
[ ! -e "$OUTF" ] && [ ! -e "$T/cloudflared.log" ] && ok "dry run by default writes and runs nothing" || bad "dry run writes and runs nothing"
grep -q "service: http://127.0.0.1:39411" <<<"$out" && grep -q "cloudflared tunnel route dns board board.example.com" <<<"$out" \
  && grep -q "Self-hosted" <<<"$out" && grep -q "aud: <Application Audience" <<<"$out" \
  && ok "dry run prints config, commands, Access steps and fleet.yaml" || bad "dry run prints everything" "$out"
xc --hostname 'bad host;rm' --tunnel board >/dev/null; eq "bad host name refused" "$?" "1"
out="$(xc --hostname board.example.com --tunnel board --apply)"; rc=$?
eq "--apply without an Access check refused" "$rc" "1"
grep -q "Access application covers" <<<"$out" && ok "  says to verify Access" || bad "  says to verify Access" "$out"
[ ! -e "$OUTF" ] && ok "  nothing written" || bad "  nothing written"
echo '{"success":true,"result":[{"domain":"other.example.com","aud":"zzz"}]}' > "$T/access-apps.json"
out="$(CLOUDFLARE_API_TOKEN=t CLOUDFLARE_ACCOUNT_ID=acc1 xc --hostname board.example.com --tunnel board --apply)"; rc=$?
eq "--apply refused when the API shows no Access app for the host" "$rc" "1"
[ ! -e "$OUTF" ] && ok "  nothing written" || bad "  nothing written"
out="$(xc --hostname board.example.com --tunnel other --apply --access-checked)"; rc=$?
eq "--apply refuses a tunnel another config uses" "$rc" "1"
grep -q "already used by" <<<"$out" && ok "  says why" || bad "  says why" "$out"
echo "keep me" > "$T/foreign.yml"
out="$(xc --hostname board.example.com --tunnel board --apply --access-checked --out "$T/foreign.yml")"; rc=$?
eq "--apply refuses an --out file it did not write" "$rc$(cat "$T/foreign.yml")" "1keep me"
echo '{"success":true,"result":[{"domain":"board.example.com/*","aud":"AUDX"}]}' > "$T/access-apps.json"
out="$(CLOUDFLARE_API_TOKEN=t CLOUDFLARE_ACCOUNT_ID=acc1 xc --hostname board.example.com --tunnel board --apply)"; rc=$?
eq "--apply with an Access app found through the API" "$rc" "0"
grep -q "AUD AUDX" <<<"$out" && ok "  shows the app's AUD" || bad "  shows the app's AUD" "$out"
grep -q "set fleet.yaml serve.tunnel.aud" <<<"$out" && ok "  says to set the AUD when it is not configured" || bad "  says to set the AUD" "$out"
printf 'serve:\n  tunnel:\n    aud: OTHER\n' > "$BOARD_CONFIG_DIR/fleet.yaml"
out="$(CLOUDFLARE_API_TOKEN=t CLOUDFLARE_ACCOUNT_ID=acc1 xc --hostname board.example.com --tunnel board --apply)"
grep -q "WARNING: fleet.yaml serve.tunnel.aud (OTHER) does not match" <<<"$out" && ok "  warns when the AUD does not match" || bad "  warns when the AUD does not match" "$out"
printf 'serve:\n  tunnel:\n    aud: AUDX\n' > "$BOARD_CONFIG_DIR/fleet.yaml"
out="$(CLOUDFLARE_API_TOKEN=t CLOUDFLARE_ACCOUNT_ID=acc1 xc --hostname board.example.com --tunnel board --apply)"
grep -q "WARNING\|NOTE: set" <<<"$out" && bad "  quiet when the AUD matches" "$out" || ok "  quiet when the AUD matches"
eq "  writes its own config file" "$(python3 -c 'import sys,yaml; c=yaml.safe_load(open(sys.argv[1])); print(c["tunnel"], [r.get("hostname","*")+">"+r["service"] for r in c["ingress"]])' "$OUTF")" \
  "2222-bbbb ['board.example.com>http://127.0.0.1:39411', '*>http_status:404']"
eq "  config.yml untouched" "$(cksum < "$CFD/config.yml")" "$sum0"
grep -q '^tunnel route dns board board.example.com$' "$T/cloudflared.log" && ok "  routes DNS" || bad "  routes DNS" "$(cat "$T/cloudflared.log")"
grep -q -- '--config '"$OUTF"' ingress validate' "$T/cloudflared.log" && ok "  validates its own file" || bad "  validates its own file"
out="$(xc --hostname board.example.com --tunnel board --apply --access-checked)"; rc=$?
eq "--apply again (own file, --access-checked) ok" "$rc" "0"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
