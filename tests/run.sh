#!/usr/bin/env bash
# shellcheck disable=SC2015,SC2016  # ok/bad always return 0; literal $ in expected strings
# tests/run.sh — offline tests for agent-switchboard (no real tmux, Slack or Claude needed).
#
# Usage: tests/run.sh [-v]
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1

T="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-test.XXXXXX")"
trap 'rm -rf "$T"' EXIT

export BOARD_HOME="$T/home"
export BOARD_CONFIG_DIR="$T/config"
export CLAUDE_SESSIONS_DIR="$T/sessions"
export CLAUDE_SETTINGS="$T/settings.json"
unset TMUX_PANE
# Alert channels: nothing from the real environment, and never a real desktop toast.
while IFS= read -r v; do unset "$v"; done < <(compgen -e | grep -E '^(SLACK_|SMTP_|ALERT_|LINE_|AWS_)')
export ALERT_DESKTOP=0
mkdir -p "$BOARD_CONFIG_DIR" "$CLAUDE_SESSIONS_DIR" "$T/bin"

pass=0; fail=0
ok() { pass=$((pass + 1)); [ "$VERBOSE" = 1 ] && echo "  ok   $1"; return 0; }
bad() { fail=$((fail + 1)); echo "  FAIL $1"; [ -n "${2:-}" ] && echo "       $2"; return 0; }
eq() {  # eq NAME GOT WANT
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got '$2', want '$3'"; fi
}

HOOK="$REPO/hooks/board-hook.sh"
SID="11111111-2222-3333-4444-555555555555"
hook() {  # hook JSON — returns hook stdout (must be empty)
  printf '%s' "$1" | TMUX_PANE=%42 "$HOOK"
}
st() { jq -r "$1" "$BOARD_HOME/state/$SID.json"; }
ndec() { find "$BOARD_HOME/decisions" -name '*.json' | wc -l | tr -d ' '; }

echo "hook:"
out="$(hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"SessionStart\",\"cwd\":\"/w\",\"source\":\"startup\"}")"
eq "hook prints nothing" "$out" ""
eq "SessionStart -> rest" "$(st .state)" "rest"
eq "tmux_pane recorded" "$(st .tmux_pane)" "%42"

hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"UserPromptSubmit\",\"prompt\":\"SECRET PROMPT TEXT\"}"
eq "UserPromptSubmit -> working" "$(st .state)" "working"
if grep -rq "SECRET PROMPT" "$BOARD_HOME"; then bad "prompt text not recorded"; else ok "prompt text not recorded"; fi

hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"tool_use_id\":\"tu_read\",\"tool_input\":{\"file_path\":\"/w/a.txt\"}}"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PostToolUse\",\"tool_name\":\"Read\",\"tool_use_id\":\"tu_read\",\"tool_input\":{\"file_path\":\"/w/a.txt\"}}"
long="$(printf 'x%.0s' $(seq 1 300))"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_use_id\":\"tu_1\",\"tool_input\":{\"command\":\"rm -rf build $long\"}}"
eq "PreToolUse -> working" "$(st .state)" "working"
eq "PreToolUse cached" "$(st '.recent_pre | last | .tool_use_id')" "tu_1"
eq "recent_pre drops finished tools" "$(st '[.recent_pre[].tool_use_id] | join(",")')" "tu_1"
eq "no pending yet" "$(st .pending)" "null"

hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PermissionRequest\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"rm -rf build $long\"}}"
eq "PermissionRequest -> needs-you" "$(st .state)" "needs-you"
eq "pending tool_use_id from PreToolUse cache" "$(st .pending.tool_use_id)" "tu_1"
eq "pending tool_name" "$(st .pending.tool_name)" "Bash"
eq "summary capped at 200 chars" "$(st '.pending.summary | length')" "200"
eq "one permission decision" "$(ndec)" "1"
dec="$(find "$BOARD_HOME/decisions" -name '*.json' | head -1)"
eq "decision kind" "$(jq -r .kind "$dec")" "permission"
eq "decision tool_use_id" "$(jq -r .tool_use_id "$dec")" "tu_1"
eq "decision status" "$(jq -r .status "$dec")" "pending"
eq "decision id format" "$(jq -r '.id | test("^d-[0-9]+-[0-9a-f]+$")' "$dec")" "true"

hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"Notification\",\"notification_type\":\"permission_prompt\",\"message\":\"Claude needs your permission to use Bash\"}"
eq "Notification permission_prompt keeps needs-you" "$(st .state)" "needs-you"
eq "decision deduped by tool_use_id" "$(ndec)" "1"
eq "notification_type recorded" "$(st .notification_type)" "permission_prompt"

# A subagent's unrelated tool use does not hide the prompt.
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Grep\",\"tool_use_id\":\"tu_other\",\"tool_input\":{\"pattern\":\"foo\"}}"
eq "unrelated PreToolUse keeps needs-you" "$(st .state)" "needs-you"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PostToolUse\",\"tool_name\":\"Grep\",\"tool_use_id\":\"tu_other\",\"tool_input\":{\"pattern\":\"foo\"}}"
eq "unrelated PostToolUse keeps pending" "$(st .pending.tool_use_id)" "tu_1"

# ---- collector
cat > "$BOARD_CONFIG_DIR/fleet.yaml" <<'EOF'
collector: true
sessions:
  - code: CP
    cwd: /w
    autostart: false
  - code: INFRA
    cwd: /w/infra
    aliases: [Infra-old]
  - code: GONE
    cwd: /w/gone
EOF
sleep 600 & live_pid=$!
sleep 600 & live_pid2=$!
sleep 600 & live_pid3=$!
trap 'kill $live_pid $live_pid2 $live_pid3 2>/dev/null; rm -rf "$T"' EXIT
cat > "$CLAUDE_SESSIONS_DIR/$live_pid.json" <<EOF
{"pid":$live_pid,"sessionId":"$SID","cwd":"/w","tmux":"CP:@3.%42","name":"CP","status":"busy","bridgeSessionId":"session_abc","startedAt":1}
EOF
cat > "$CLAUDE_SESSIONS_DIR/$live_pid2.json" <<EOF
{"pid":$live_pid2,"sessionId":"s-infra","cwd":"/w/infra","tmux":null,"name":"Infra-old","status":"idle","bridgeSessionId":null,"startedAt":2}
EOF
cat > "$CLAUDE_SESSIONS_DIR/$live_pid3.json" <<EOF
{"pid":$live_pid3,"sessionId":"s-qa","cwd":"/w/qa","tmux":"QA:@9.%9","name":"QA","status":"waiting","bridgeSessionId":null,"startedAt":3}
EOF
cat > "$CLAUDE_SESSIONS_DIR/999999999.json" <<'EOF'
{"pid":999999999,"sessionId":"dead","cwd":"/w","tmux":"DEAD:@1.%1","name":"DEAD","status":"idle"}
EOF

echo "collector:"
"$REPO/bin/board-collect" --once --no-notify
snap="$BOARD_HOME/snapshot.json"
[ -f "$snap" ] && ok "snapshot written" || bad "snapshot written"
eq "dead pid skipped" "$(jq '[.sessions[] | select(.code == "DEAD")] | length' "$snap")" "0"
eq "codes" "$(jq -r '[.sessions[].code] | join(",")' "$snap")" "CP,GONE,INFRA,QA"
eq "CP merged with hook state" "$(jq -r '.sessions[] | select(.code=="CP") | .state' "$snap")" "needs-you"
eq "CP pending" "$(jq -r '.sessions[] | select(.code=="CP") | .pending.tool_use_id' "$snap")" "tu_1"
eq "CP pane" "$(jq -r '.sessions[] | select(.code=="CP") | .tmux_pane' "$snap")" "%42"
eq "remote_url" "$(jq -r '.sessions[] | select(.code=="CP") | .remote_url' "$snap")" "https://claude.ai/code/session_abc"
eq "alias -> code (no tmux)" "$(jq -r '.sessions[] | select(.code=="INFRA") | .name' "$snap")" "Infra-old"
eq "registry-only session state from status" "$(jq -r '.sessions[] | select(.code=="INFRA") | .state' "$snap")" "rest"
eq "fleet session not running -> offline" "$(jq -r '.sessions[] | select(.code=="GONE") | .state' "$snap")" "offline"
eq "permission decision gets from=code" "$(jq -r '.decisions[0].from' "$snap")" "CP"
eq "registry status waiting -> needs-you" "$(jq -r '.sessions[] | select(.code=="QA") | .state' "$snap")" "needs-you"
# A newer registry busy/idle overrides a stale hook needs-you (missed clearing event).
cp "$CLAUDE_SESSIONS_DIR/$live_pid.json" "$T/cp-reg.json"
jq '.statusUpdatedAt = 99999999999999' "$T/cp-reg.json" > "$CLAUDE_SESSIONS_DIR/$live_pid.json"
"$REPO/bin/board-collect" --once --no-notify
eq "newer registry busy overrides stale needs-you" "$(jq -r '.sessions[] | select(.code=="CP") | .state' "$snap")" "working"
eq "permission decision no longer pending -> stale" "$(jq -r .status "$dec")" "stale"
cp "$T/cp-reg.json" "$CLAUDE_SESSIONS_DIR/$live_pid.json"
"$REPO/bin/board-collect" --once --no-notify
gen1="$(jq .generated_at "$snap")"
sleep 0.05
"$REPO/bin/board-collect" --once --no-notify
eq "unchanged snapshot not rewritten" "$(jq .generated_at "$snap")" "$gen1"

out="$("$REPO/bin/board" ls)"
if grep -q "needs-you" <<<"$out" && grep -q "Bash: rm -rf build" <<<"$out"; then ok "board ls"; else bad "board ls" "$out"; fi

# ---- ask / answer / mark / decisions
echo "decisions:"
qid="$("$REPO/bin/board" ask --from CP "Ship it?" --option yes --option no --context "tests green")"
eq "ask prints id" "$(grep -cE '^d-[0-9]+-[0-9a-f]+$' <<<"$qid")" "1"
eq "ask options" "$(jq -c .options "$BOARD_HOME/decisions/$qid.json")" '["yes","no"]'
eq "ask kind" "$(jq -r .kind "$BOARD_HOME/decisions/$qid.json")" "question"
eq "decisions --status pending (permission went stale above)" "$("$REPO/bin/board" decisions --status pending --json | jq length)" "1"
"$REPO/bin/board" answer "$qid" yes --by user-1 >/dev/null
eq "answer status" "$(jq -r .status "$BOARD_HOME/decisions/$qid.json")" "answered"
eq "answered_by" "$(jq -r .answered_by "$BOARD_HOME/decisions/$qid.json")" "user-1"
"$REPO/bin/board" mark "$qid" relayed >/dev/null
eq "mark relayed" "$(jq -r .status "$BOARD_HOME/decisions/$qid.json")" "relayed"
eq "relayed_at set" "$(jq -r '.relayed_at > 0' "$BOARD_HOME/decisions/$qid.json")" "true"
"$REPO/bin/board" mark "$qid" bogus >/dev/null 2>&1; eq "mark rejects bad status" "$?" "2"
"$REPO/bin/board" mark "../etc" stale >/dev/null 2>&1; eq "mark rejects bad id" "$?" "1"

# timeout: backdate a pending question
oid="$("$REPO/bin/board" ask --from CP "Old question?")"
jq '.created_at = 1000' "$BOARD_HOME/decisions/$oid.json" > "$T/x" && mv "$T/x" "$BOARD_HOME/decisions/$oid.json"
"$REPO/bin/board-collect" --once --no-notify
eq "old pending decision -> timeout" "$(jq -r .status "$BOARD_HOME/decisions/$oid.json")" "timeout"

# ---- notify (fake curl on PATH)
echo "notify:"
# Fake curl: logs the JSON body, whether given as --data-binary or in a `-K -` config on stdin
# (board-notify keeps the URL and body off argv).
cat > "$T/bin/curl" <<'EOF'
#!/usr/bin/env bash
data=""
while [ $# -gt 0 ]; do
  case "$1" in
    --data-binary) shift; data="$1" ;;
    -K) shift; [ "$1" = "-" ] && data="$(python3 -c '
import sys
for line in sys.stdin:
    if line.startswith("data-binary = "):
        v = line[len("data-binary = "):].rstrip("\n")[1:-1]
        out, i = [], 0
        while i < len(v):
            if v[i] == "\\" and i + 1 < len(v):
                out.append({"n": "\n", "r": "\r"}.get(v[i + 1], v[i + 1])); i += 2
            else:
                out.append(v[i]); i += 1
        print("".join(out))')" ;;
  esac
  shift
done
[ -n "$data" ] && printf '%s\n' "$data" | jq -c . >> "$FAKE_CURL_LOG"
printf 200
EOF
chmod +x "$T/bin/curl"
export FAKE_CURL_LOG="$T/curl.log"; : > "$FAKE_CURL_LOG"
PATH="$T/bin:$PATH" "$REPO/bin/board" notify "hello"
eq "notify without webhook is a no-op" "$(wc -l < "$FAKE_CURL_LOG" | tr -d ' ')" "0"
echo "SLACK_WEBHOOK_URL=\"http://example.invalid/hook\"" > "$BOARD_CONFIG_DIR/.env"
PATH="$T/bin:$PATH" "$REPO/bin/board" notify "hello"
eq "notify posts JSON" "$(jq -r .text "$FAKE_CURL_LOG")" "hello"
: > "$FAKE_CURL_LOG"
[ -f "$BOARD_HOME/alerts.json" ] && bad "--no-notify runs leave alerts.json alone" || ok "--no-notify runs leave alerts.json alone"
# QA goes from needs-you to busy and back: one alert, then deduped (repeat_minutes).
jq '.status = "busy"' "$CLAUDE_SESSIONS_DIR/$live_pid3.json" > "$T/r" && mv "$T/r" "$CLAUDE_SESSIONS_DIR/$live_pid3.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
jq '.status = "waiting"' "$CLAUDE_SESSIONS_DIR/$live_pid3.json" > "$T/r" && mv "$T/r" "$CLAUDE_SESSIONS_DIR/$live_pid3.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
grep -q "QA needs you" "$FAKE_CURL_LOG" && ok "needs-you transition notifies" || bad "needs-you transition notifies" "$(cat "$FAKE_CURL_LOG")"
jq '.status = "busy"' "$CLAUDE_SESSIONS_DIR/$live_pid3.json" > "$T/r" && mv "$T/r" "$CLAUDE_SESSIONS_DIR/$live_pid3.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
jq '.status = "waiting"' "$CLAUDE_SESSIONS_DIR/$live_pid3.json" > "$T/r" && mv "$T/r" "$CLAUDE_SESSIONS_DIR/$live_pid3.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
eq "re-opened needs-you deduped within repeat_minutes" "$(grep -c "QA needs you" "$FAKE_CURL_LOG")" "1"
"$REPO/bin/board" ask --from INFRA "Rotate keys now?" >/dev/null
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
grep -q "INFRA asks: Rotate keys now?" "$FAKE_CURL_LOG" && ok "new question notifies" || bad "new question notifies" "$(cat "$FAKE_CURL_LOG")"
eq "alerts logged to events.jsonl" "$(jq -s '[.[] | select(.type=="alert" and .channel=="slack" and .ok)] | length > 0' "$BOARD_HOME/events.jsonl")" "true"
out="$("$REPO/bin/board" ack 'decision:*')"; rc=$?
eq "board ack exits 0" "$rc" "0"
grep -q "^acked decision:" <<<"$out" && ok "board ack acks by glob" || bad "board ack acks by glob" "$out"
"$REPO/bin/board" ack 'nothing:*' >/dev/null 2>&1; eq "board ack with no match exits 1" "$?" "1"
"$REPO/bin/board" doctor 2>&1 | grep -q "channels: slack=on" && ok "doctor shows board-notify status" || bad "doctor shows board-notify status"

# ---- alert-storm guard: baseline on a fresh BOARD_HOME
echo "baseline:"
H2="$T/home2"; : > "$FAKE_CURL_LOG"
BOARD_HOME="$H2" "$REPO/bin/board" ask --from QA "Already open before the collector started?" >/dev/null
out="$(BOARD_HOME="$H2" PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once --verbose)"
grep -q "alerts: baseline" <<<"$out" && ok "first run takes a baseline" || bad "first run takes a baseline" "$out"
eq "no alerts sent for conditions already open" "$(wc -l < "$FAKE_CURL_LOG" | tr -d ' ')" "0"
eq "open needs-you acked as baseline" "$(jq -r '."session:QA:needs-you".acked_by' "$H2/alerts.json")" "baseline"
eq "open decision acked as baseline" "$(jq -r '[to_entries[] | select(.key | startswith("decision:")) | .value.acked_by] | unique | join(",")' "$H2/alerts.json")" "baseline"
BOARD_HOME="$H2" "$REPO/bin/board" ask --from QA "Asked after the baseline?" >/dev/null
BOARD_HOME="$H2" PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
grep -q "Asked after the baseline" "$FAKE_CURL_LOG" && ok "new condition after baseline alerts" || bad "new condition after baseline alerts" "$(cat "$FAKE_CURL_LOG")"
eq "...and only that one" "$(wc -l < "$FAKE_CURL_LOG" | tr -d ' ')" "1"
: > "$FAKE_CURL_LOG"
out="$(BOARD_HOME="$H2" PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --baseline)"; rc=$?
eq "--baseline exits 0" "$rc" "0"
grep -q "baseline — 1 open condition" <<<"$out" && ok "--baseline acks the open alert" || bad "--baseline acks the open alert" "$out"
eq "--baseline sends nothing" "$(wc -l < "$FAKE_CURL_LOG" | tr -d ' ')" "0"
H3="$T/home3"
out="$(BOARD_HOME="$H3" PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --baseline)"
[ -f "$H3/alerts.json" ] && ok "--baseline creates alerts.json even when nothing is open" || bad "--baseline creates alerts.json"
rm -f "$BOARD_CONFIG_DIR/.env"

# ---- press (fake tmux on PATH)
echo "press:"
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  display-message) echo "${FAKE_TMUX_SESSION:-CP}" ;;
  capture-pane) printf '%s\n' "${FAKE_TMUX_SCREEN:-}" ;;
  send-keys) exit 0 ;;
esac
EOF
chmod +x "$T/bin/tmux"
export FAKE_TMUX_LOG="$T/tmux.log"
: > "$FAKE_TMUX_LOG"
PROMPT=$' Bash command\n   rm -rf build\n Do you want to proceed?\n ❯ 1. Yes\n   2. No, and tell Claude what to do differently (esc)\n'

PATH="$T/bin:$PATH" FAKE_TMUX_SCREEN="$PROMPT" "$REPO/bin/board" press CP allow --expect tu_WRONG >/dev/null 2>&1
eq "press with wrong --expect exits 3" "$?" "3"
if grep -q '^send-keys' "$FAKE_TMUX_LOG"; then bad "no keys sent on mismatch"; else ok "no keys sent on mismatch"; fi
eq "refusal logged" "$(jq -s '[.[] | select(.type=="press" and .result=="refused")] | length' "$BOARD_HOME/events.jsonl")" "1"

PATH="$T/bin:$PATH" FAKE_TMUX_SCREEN="just a shell prompt" "$REPO/bin/board" press CP allow --expect tu_1 >/dev/null 2>&1
eq "press without visible prompt exits 3" "$?" "3"
if grep -q '^send-keys' "$FAKE_TMUX_LOG"; then bad "no keys sent without prompt"; else ok "no keys sent without prompt"; fi
eq "permission decision marked stale on refusal" "$(jq -r .status "$dec")" "stale"

PATH="$T/bin:$PATH" FAKE_TMUX_SESSION=OTHER FAKE_TMUX_SCREEN="$PROMPT" "$REPO/bin/board" press CP allow --expect tu_1 >/dev/null 2>&1
eq "press refuses pane of another tmux session" "$?" "3"

PATH="$T/bin:$PATH" FAKE_TMUX_SCREEN="$PROMPT" "$REPO/bin/board" press CP allow --expect tu_1 >/dev/null 2>&1
eq "press allow ok" "$?" "0"
eq "allow sends 1" "$(grep '^send-keys' "$FAKE_TMUX_LOG" | tail -1)" "send-keys -t %42 1"
: > "$FAKE_TMUX_LOG"
PATH="$T/bin:$PATH" FAKE_TMUX_SCREEN="$PROMPT" "$REPO/bin/board" press CP deny --expect tu_1 >/dev/null 2>&1
eq "deny sends Escape" "$(grep '^send-keys' "$FAKE_TMUX_LOG" | tail -1)" "send-keys -t %42 Escape"

# ---- pending cleared after the tool ran
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PostToolUse\",\"tool_name\":\"Bash\",\"tool_use_id\":\"tu_1\",\"tool_input\":{\"command\":\"rm -rf build\"}}"
eq "PostToolUse of pending tool -> working" "$(st .state)" "working"
eq "pending cleared" "$(st .pending)" "null"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"Notification\",\"notification_type\":\"idle_prompt\"}"
eq "idle_prompt -> done" "$(st .state)" "done"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"Stop\"}"
eq "Stop -> done" "$(st .state)" "done"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"SessionEnd\",\"reason\":\"exit\"}"
eq "SessionEnd -> offline" "$(st .state)" "offline"

# Permission prompt without a PreToolUse cache: needs-you, but no decision (no tool_use_id).
SID2="aaaaaaaa-0000-0000-0000-000000000000"
n0="$(ndec)"
printf '%s' "{\"session_id\":\"$SID2\",\"hook_event_name\":\"PermissionRequest\",\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"/w/x\"}}" | "$HOOK"
eq "PermissionRequest w/o cache -> needs-you" "$(jq -r .state "$BOARD_HOME/state/$SID2.json")" "needs-you"
eq "no decision without tool_use_id" "$(ndec)" "$n0"

# Robustness: garbage / unsafe input never fails and never writes.
printf 'not json' | "$HOOK"; eq "garbage input exit 0" "$?" "0"
printf '{"session_id":"../../evil","hook_event_name":"Stop"}' | "$HOOK"; eq "unsafe session id exit 0" "$?" "0"
[ -e "$BOARD_HOME/evil.json" ] && bad "unsafe session id ignored" || ok "unsafe session id ignored"

# Speed (informational, generous bound).
t0="${EPOCHREALTIME/./}"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"tool_use_id\":\"tu_s\",\"tool_input\":{\"file_path\":\"/w/a\"}}"
done
t1="${EPOCHREALTIME/./}"
avg_ms=$(( (t1 - t0) / 10000 ))
echo "  hook avg ${avg_ms}ms"
[ "$avg_ms" -lt 200 ] && ok "hook speed" || bad "hook speed" "${avg_ms}ms"

# ---- usage limits
echo "limit:"
export BOARD_LIMIT_GRACE_MS=0
echo 'SLACK_WEBHOOK_URL="http://example.invalid/hook"' > "$BOARD_CONFIG_DIR/.env"
: > "$FAKE_CURL_LOG"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once   # settle the snapshot first
: > "$FAKE_CURL_LOG"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"StopFailure\",\"error\":\"rate_limit\",\"error_details\":\"5-hour limit reached · resets 11:30pm (UTC)\"}"
eq "StopFailure -> done" "$(st .state)" "done"
lim="$BOARD_HOME/limit.json"
[ -f "$lim" ] && ok "limit.json written" || bad "limit.json written"
eq "limit kind" "$(jq -r .kind "$lim")" "5h"
eq "limit resets_at parsed" "$(jq -r '.resets_at > 0' "$lim")" "true"
eq "limit resets at 23:30 UTC" "$(jq -r '.resets_at / 1000 | strftime("%H:%M")' "$lim")" "23:30"
hit1="$(jq .hit_at "$lim")"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"StopFailure\",\"error\":\"usage limit reached\"}"
eq "same episode keeps hit_at" "$(jq .hit_at "$lim")" "$hit1"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
eq "snapshot.limit set" "$(jq -r '.limit.kind' "$BOARD_HOME/snapshot.json")" "5h"
eq "one Slack alert per episode" "$(grep -c 'usage limit hit (5h), resets ~' "$FAKE_CURL_LOG")" "1"
grep -q 'board frozen; decisions pending: [0-9]' "$FAKE_CURL_LOG" && ok "alert text" || bad "alert text" "$(cat "$FAKE_CURL_LOG")"
"$REPO/bin/board" ask --from CP "Something new?" >/dev/null   # change the snapshot
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
eq "no second alert in the same episode" "$(grep -c 'usage limit hit' "$FAKE_CURL_LOG")" "1"
grep -q "Something new" "$FAKE_CURL_LOG" && ok "other alerts continue while frozen" || bad "other alerts continue while frozen"
"$REPO/bin/board" limit | grep -q "usage limit hit" && ok "board limit shows episode" || bad "board limit shows episode"
"$REPO/bin/board" ls | grep -q "USAGE LIMIT ACTIVE" && ok "board ls shows limit" || bad "board ls shows limit"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"UserPromptSubmit\"}"
eq "UserPromptSubmit alone does not end the episode" "$(jq -r 'has("cleared_at")' "$lim")" "false"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"tool_use_id\":\"tu_after\",\"tool_input\":{\"file_path\":\"/w/a\"}}"
eq "PreToolUse after hit ends the episode" "$(jq -r 'has("cleared_at")' "$lim")" "true"
eq "board limit after clear" "$("$REPO/bin/board" limit)" "no active usage limit"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
eq "snapshot.limit cleared" "$(jq -r '.limit' "$BOARD_HOME/snapshot.json")" "null"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"Notification\",\"notification_type\":\"other\",\"message\":\"Weekly limit reached, resets Mon 9am\"}"
eq "notification text starts a new episode" "$(jq -r 'has("cleared_at")' "$lim")" "false"
eq "weekly kind" "$(jq -r .kind "$lim")" "weekly"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
eq "new episode alerts again" "$(grep -c 'usage limit hit' "$FAKE_CURL_LOG")" "2"
"$REPO/bin/board" limit --clear >/dev/null
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"Notification\",\"notification_type\":\"idle_prompt\",\"message\":\"Claude is waiting for your input\"}"
eq "unrelated notification is not a limit" "$(jq -r 'has("cleared_at")' "$lim")" "true"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"StopFailure\",\"error\":\"overloaded_error\"}"
eq "non-limit StopFailure is not a limit" "$(jq -r 'has("cleared_at")' "$lim")" "true"
rm -f "$BOARD_CONFIG_DIR/.env"

# ---- approvers / release managers
echo "approvers:"
B="$REPO/bin/board"
cp "$BOARD_CONFIG_DIR/fleet.yaml" "$T/fleet.base.yaml"
{ cat "$T/fleet.base.yaml"; printf 'approvers: [u-owner, u-lead]\nrelease_managers: [u-rm]\n'; } > "$BOARD_CONFIG_DIR/fleet.yaml"
q="$("$B" ask --from CP "Merge?")"
"$B" answer "$q" yes --by u-stranger >/dev/null 2>&1; eq "non-approver refused (exit 4)" "$?" "4"
eq "non-approver -> unauthorized" "$(jq -r .status "$BOARD_HOME/decisions/$q.json")" "unauthorized"
"$B" answer "$q" yes --by u-owner >/dev/null; eq "approver accepted" "$?" "0"
eq "approver -> answered" "$(jq -r .status "$BOARD_HOME/decisions/$q.json")" "answered"
p="$("$B" ask --from CP "Release 1.2 to production?" --gate production --stage production --artifact rel-notes)"
eq "gate decision default options" "$(jq -c .options "$BOARD_HOME/decisions/$p.json")" '["approve","request changes"]'
"$B" answer "$p" approve --by u-owner >/dev/null 2>&1; eq "production: approver who is not RM refused" "$?" "4"
"$B" answer "$p" approve --by u-rm >/dev/null; eq "production: release manager accepted" "$?" "0"
eq "production approval event" "$(jq -rs --arg id "$p" '[.[] | select(.type=="approval" and .decision_id==$id)] | last | .result' "$BOARD_HOME/events.jsonl")" "approved"
{ cat "$T/fleet.base.yaml"; printf 'approvers: [u-owner]\nrelease_managers: []\n'; } > "$BOARD_CONFIG_DIR/fleet.yaml"
p2="$("$B" ask --from CP "Hotfix to production?" --gate production)"
"$B" answer "$p2" approve --by u-owner >/dev/null; eq "production falls back to approvers" "$?" "0"
cp "$T/fleet.base.yaml" "$BOARD_CONFIG_DIR/fleet.yaml"
p3="$("$B" ask --from CP "Another release?" --gate production)"
"$B" answer "$p3" approve --by anyone >/dev/null 2>&1; eq "production with nobody configured refused" "$?" "4"
eq "...and stays pending" "$(jq -r .status "$BOARD_HOME/decisions/$p3.json")" "pending"
q2="$("$B" ask --from CP "Unchecked?")"
"$B" answer "$q2" yes --by anyone >/dev/null; eq "empty approvers = unchecked" "$?" "0"
"$B" mark "$q2" relayed >/dev/null
"$B" answer "$q2" no --by anyone >/dev/null 2>&1; eq "cannot re-answer a relayed decision" "$?" "1"

# ---- lifecycle tasks
echo "tasks:"
{ cat "$T/fleet.base.yaml"; printf 'approvers: [u-owner]\n'; } > "$BOARD_CONFIG_DIR/fleet.yaml"
tid="$("$B" task new --owner CP "Add order export")"
eq "task id format" "$(grep -cE '^t-[0-9]+-[0-9a-f]+$' <<<"$tid")" "1"
tf="$BOARD_HOME/tasks/$tid.json"
eq "new task stage plan" "$(jq -r .stage "$tf")" "plan"
"$B" task stage "$tid" build --artifact intent=abc123 --artifact plan=def456 >/dev/null
eq "task stage set" "$(jq -r .stage "$tf")" "build"
eq "task artifacts" "$(jq -r '.artifacts.intent + "," + .artifacts.plan' "$tf")" "abc123,def456"
"$B" task stage "$tid" shipping >/dev/null 2>&1; eq "unknown stage rejected" "$?" "1"
"$B" task stage "$tid" test --artifact secrets=x >/dev/null 2>&1; eq "unknown artifact key rejected" "$?" "1"
eq "rejected stage change leaves task alone" "$(jq -r .stage "$tf")" "build"
g="$("$B" ask --from CP --task "$tid" --gate owner --artifact def456 "Approve plan?")"
eq "gate decision fields" "$(jq -r '[.task_id, .stage, .gate, .artifact_ref] | join(",")' "$BOARD_HOME/decisions/$g.json")" "$tid,build,owner,def456"
eq "task gate pending" "$(jq -r '.gates[0].status + "," + .gates[0].decision_id' "$tf")" "pending,$g"
"$B" ask --from CP --task t-0-nope "x?" >/dev/null 2>&1; eq "ask with unknown task rejected" "$?" "1"
"$B" answer "$g" approve --by u-owner >/dev/null
eq "task gate approved" "$(jq -r '.gates[0].status + "," + .gates[0].approved_by' "$tf")" "approved,u-owner"
eq "approval event logged" "$(jq -rs --arg id "$g" '[.[] | select(.type=="approval" and .decision_id==$id)] | last | [.task_id, .stage, .gate, .result] | join(",")' "$BOARD_HOME/events.jsonl")" "$tid,build,owner,approved"
g2="$("$B" ask --from CP --task "$tid" --stage test --gate code-owner "Evals ok?")"
"$B" answer "$g2" "request changes" --by u-owner >/dev/null
eq "request changes -> gate rejected" "$(jq -r '.gates[1].status' "$tf")" "rejected"
"$REPO/bin/board-collect" --once --no-notify
eq "snapshot tasks" "$(jq -r '.tasks | length' "$BOARD_HOME/snapshot.json")" "1"
eq "session shows task stage" "$(jq -r '.sessions[] | select(.code=="CP") | .stage + "," + .task_id' "$BOARD_HOME/snapshot.json")" "build,$tid"
"$B" ls | grep -E '^CP ' | grep -q build && ok "board ls shows stage" || bad "board ls shows stage" "$("$B" ls)"
"$B" task ls | grep -q "build/owner:approved" && ok "task ls" || bad "task ls" "$("$B" task ls)"
g3="$("$B" ask --from CP --task "$tid" --stage test --gate code-owner "Evals ok now?")"
"$B" answer "$g3" " LGTM " --by u-owner >/dev/null
eq "approve words are case-insensitive" "$(jq -r '.gates[2].status' "$tf")" "approved"
cp "$T/fleet.base.yaml" "$BOARD_CONFIG_DIR/fleet.yaml"

# ---- help
echo "help:"
out="$("$B" answer --help)"; rc=$?
eq "board answer --help exits 0" "$rc" "0"
grep -q "lgtm" <<<"$out" && ok "answer --help lists approve words" || bad "answer --help lists approve words" "$out"
for c in ls snapshot ask decisions answer mark press limit notify doctor ack task "task new" "task stage" "task ls" "task show"; do
  # shellcheck disable=SC2086  # word-split "task new" on purpose
  out="$("$B" $c -h 2>&1)"; rc=$?
  if [ "$rc" = 0 ] && grep -q "^usage: board ${c%% *}" <<<"$out"; then ok "board $c -h"; else bad "board $c -h" "rc=$rc $out"; fi
done
"$B" answer onlyone >/dev/null 2>&1; eq "answer with missing args is a usage error" "$?" "2"

# ---- .githooks/pre-commit (in a throwaway repo)
echo "pre-commit:"
R="$T/repo"
mkdir -p "$R/.githooks" "$R/tmp-local" "$R/tmp-scratch" "$T/gl-shim"
cp "$REPO/.githooks/pre-commit" "$R/.githooks/" && cp "$REPO/.gitleaks.toml" "$R/"
git -C "$R" init -q -b main
git -C "$R" config user.email t@example.invalid && git -C "$R" config user.name t
git -C "$R" config core.hooksPath .githooks
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$T/gl-shim/gitleaks"; chmod +x "$T/gl-shim/gitleaks"
commit() { (cd "$R" && git add -A && PATH="${GL_PATH:-$T/gl-shim}:$PATH" git commit -q -m t >"$T/pc.out" 2>&1); }
echo "hello" > "$R/a.txt"
commit; eq "no banned-terms file: commit allowed" "$?" "0"
grep -q "banned-terms.txt not found" "$T/pc.out" && ok "missing list warns" || bad "missing list warns" "$(cat "$T/pc.out")"
printf '%s\n' '# comment line' 'example-corp' '\bacme\b   # trailing comment' 'internal\.example\.com' '' > "$R/tmp-local/banned-terms.txt"
echo "nothing to see" > "$R/b.txt"
commit; eq "clean change allowed" "$?" "0"
printf 'line one\nwe work at Example-Corp\n' > "$R/c.txt"
commit; eq "banned term (case-insensitive) blocks" "$?" "1"
grep -q "c.txt:2: we work at Example-Corp" "$T/pc.out" && ok "hit shows file:line" || bad "hit shows file:line" "$(cat "$T/pc.out")"
git -C "$R" reset -q; rm "$R/c.txt"
echo "acme notes" > "$R/tmp-scratch/notes.txt"
commit; eq "tmp*/ paths excluded" "$?" "0"
echo "acmes are fine, the comment is not a term" > "$R/d.txt"
commit; eq "word-boundary pattern and inline comment" "$?" "0"
mkdir -p "$R/acme" && echo x > "$R/acme/readme.txt"
commit; eq "banned term in a file path blocks" "$?" "1"
git -C "$R" reset -q; rm -rf "$R/acme"
printf '%s\n' '#!/usr/bin/env bash' 'echo leak >&2; exit 1' > "$T/gl-shim/gitleaks"
echo "y" > "$R/e.txt"
commit; eq "gitleaks failure blocks" "$?" "1"
git -C "$R" reset -q; rm "$R/e.txt"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$T/gl-shim/gitleaks"
if command -v gitleaks >/dev/null 2>&1; then
  # Build a webhook-shaped value at runtime so this file itself never contains one.
  fake="https://hooks.slack"".com/services/T$(printf 'A%.0s' $(seq 1 8))/B$(printf 'B%.0s' $(seq 1 8))/$(printf 'x7Q%.0s' $(seq 1 8))"
  echo "url=$fake" > "$R/f.txt"
  GL_PATH="$(dirname "$(command -v gitleaks)")" commit; eq "real gitleaks blocks a webhook URL" "$?" "1"
  git -C "$R" reset -q; rm "$R/f.txt"
else
  echo "  (gitleaks not installed: real-scan test skipped)"
fi

# ---- install.sh (against a temp settings.json)
echo "install:"
cat > "$CLAUDE_SETTINGS" <<'EOF'
{"model":"x","hooks":{"Stop":[{"hooks":[{"type":"command","command":"/other/hook.sh"}]}]}}
EOF
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" --dry-run > "$T/dry.txt"
eq "dry-run leaves settings untouched" "$(jq -c . "$CLAUDE_SETTINGS")" '{"model":"x","hooks":{"Stop":[{"hooks":[{"type":"command","command":"/other/hook.sh"}]}]}}'
grep -q '^    +.*board-hook.sh' "$T/dry.txt" && ok "dry-run prints diff" || bad "dry-run prints diff"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" >/dev/null
eq "existing hook kept" "$(jq -r '.hooks.Stop[0].hooks[0].command' "$CLAUDE_SETTINGS")" "/other/hook.sh"
eq "hook added to 10 events" "$(jq '[.hooks[][] | select(.hooks[].command | test("board-hook"))] | length' "$CLAUDE_SETTINGS")" "10"
ls "$T"/settings.json.bak-* >/dev/null 2>&1 && ok "backup made" || bad "backup made"
before="$(cat "$CLAUDE_SETTINGS")"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" >/dev/null
eq "install idempotent" "$(cat "$CLAUDE_SETTINGS")" "$before"
[ -L "$T/localbin/board" ] && ok "bin symlinked" || bad "bin symlinked"
[ -L "$T/localbin/board-notify" ] && ok "board-notify symlinked" || bad "board-notify symlinked"
TAP="$REPO/hooks/board-statusline-tap.sh"
eq "no previous statusline: tap installed bare" "$(jq -r .statusLine.command "$CLAUDE_SETTINGS")" "$TAP"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" --uninstall >/dev/null
eq "uninstall restores hooks" "$(jq -c .hooks "$CLAUDE_SETTINGS")" '{"Stop":[{"hooks":[{"type":"command","command":"/other/hook.sh"}]}]}'
eq "uninstall removes a statusLine that was not there before" "$(jq -c 'has("statusLine")' "$CLAUDE_SETTINGS")" "false"
[ -L "$T/localbin/board" ] && bad "uninstall removes symlinks" || ok "uninstall removes symlinks"

# statusLine tap around an existing statusline (with quotes), other keys kept, exact restore.
SL_PREV="$BOARD_HOME/statusline.prev.json"
sl_orig='{"type":"command","command":"printf %s \"it'"'"'s $(cat | jq -r .session_id)\"","padding":2}'
jq --argjson sl "$sl_orig" '.statusLine = $sl' "$CLAUDE_SETTINGS" > "$T/s" && mv "$T/s" "$CLAUDE_SETTINGS"
expected="$(printf '{"session_id":"s9"}' | sh -c "$(jq -r .statusLine.command "$CLAUDE_SETTINGS")")"
eq "statusline fixture works" "$expected" "it's s9"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" --dry-run > "$T/dry.txt"
grep -q 'board-statusline-tap.sh' "$T/dry.txt" && ok "dry-run shows the statusLine change" || bad "dry-run shows the statusLine change"
[ -f "$SL_PREV" ] && bad "dry-run writes no statusline backup" || ok "dry-run writes no statusline backup"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" >/dev/null
cmd="$(jq -r .statusLine.command "$CLAUDE_SETTINGS")"
case "$cmd" in "BOARD_STATUSLINE_NEXT="*"$TAP") ok "statusLine wrapped with the tap" ;; *) bad "statusLine wrapped with the tap" "$cmd" ;; esac
eq "other statusLine keys kept" "$(jq -c '[.statusLine.type, .statusLine.padding]' "$CLAUDE_SETTINGS")" '["command",2]'
eq "previous statusLine saved" "$(jq -c . "$SL_PREV")" "$(jq -c . <<<"$sl_orig")"
out="$(printf '{"session_id":"s9"}' | sh -c "$cmd")"
eq "wrapped statusline prints the same" "$out" "$expected"
before="$(cat "$CLAUDE_SETTINGS")"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" >/dev/null
eq "statusline wrap idempotent" "$(cat "$CLAUDE_SETTINGS")" "$before"
eq "backup still the original" "$(jq -c . "$SL_PREV")" "$(jq -c . <<<"$sl_orig")"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" --uninstall >/dev/null
eq "uninstall restores the exact statusLine" "$(jq -c .statusLine "$CLAUDE_SETTINGS")" "$(jq -c . <<<"$sl_orig")"
[ -f "$SL_PREV" ] && bad "statusline backup removed after restore" || ok "statusline backup removed after restore"

# ---- fleet-up dry run
echo "fleet-up:"
cat > "$BOARD_CONFIG_DIR/fleet.yaml" <<EOF
sessions:
  - code: NEW
    cwd: $T
    autostart: true
    resume_name: NEW
    extra_args: "--verbose"
  - code: CP
    cwd: $T
    autostart: true
  - code: OFF
    cwd: $T
    autostart: false
  - code: FRESH
    cwd: $T
    autostart: true
    resume: false
    resume_name: FRESH
    extra_args: "--verbose"
    initial_prompt: "You're the \$OPERATOR; read \`x\` and \\"go\\""
EOF
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in has-session) [ "$3" = "=CP" ] && exit 0; exit 1 ;; esac
EOF
: > "$FAKE_TMUX_LOG"
out="$(PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" --dry-run)"
grep -q "would run: tmux new-session -d -s NEW" <<<"$out" && ok "starts missing session" || bad "starts missing session" "$out"
grep -qF '_fleet_t0=$(date +%s); claude -n NEW --remote-control NEW -r '\''NEW'\'' --verbose; [ $(( $(date +%s) - _fleet_t0 )) -lt 15 ] && claude -n NEW --remote-control NEW --verbose' <<<"$out" \
  && ok "resume, fresh only on fast exit" || bad "resume, fresh only on fast exit" "$out"
grep -q "CP: tmux session exists" <<<"$out" && ok "skips existing session" || bad "skips existing session"
grep -q "OFF" <<<"$out" && bad "skips autostart:false" || ok "skips autostart:false"
grep -q "would run: tmux new-session -d -s switchboard" <<<"$out" && ok "starts collector" || bad "starts collector" "$out"
grep -q '^new-session' "$FAKE_TMUX_LOG" && bad "dry-run changes nothing" || ok "dry-run changes nothing"
grep -q "would run: .*board-collect --baseline" <<<"$out" && bad "no baseline when alerts.json exists" "$out" || ok "no baseline when alerts.json exists"
fresh_cmd="$(sed -n 's/^.*FRESH: starting in [^:]*: //p' <<<"$out")"
case "$fresh_cmd" in *" -r "*|*_fleet_t0*) bad "resume: false starts fresh" "$fresh_cmd" ;; *) ok "resume: false starts fresh" ;; esac
printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\n" "$@"' > "$T/bin/fake-claude"; chmod +x "$T/bin/fake-claude"
got="$(sh -c "$T/bin/fake-claude ${fresh_cmd#claude }")"
want="$(printf '%s\n' -n FRESH --remote-control FRESH --verbose "You're the \$OPERATOR; read \`x\` and \"go\"")"
eq "initial_prompt passed as one shell-safe argument" "$got" "$want"
# Lost race: new-session fails (session created meanwhile) -> no keys typed into that pane.
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in has-session) exit 1 ;; new-session) exit 1 ;; esac
EOF
: > "$FAKE_TMUX_LOG"
BOARD_HOME="$T/home5" PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" --only NEW >/dev/null 2>&1
grep -q '^send-keys' "$FAKE_TMUX_LOG" && bad "no send-keys when new-session fails" "$(cat "$FAKE_TMUX_LOG")" || ok "no send-keys when new-session fails"
grep -q "NEW: tmux new-session failed" "$T/home5/fleet-up.log" && ok "lost race logged" || bad "lost race logged"
# The tmux server started by fleet-up must not inherit (and keep) the fleet-up lock.
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  has-session) exit 1 ;;
  new-session) sleep 30 </dev/null >/dev/null 2>&1 & echo $! >> "$FAKE_TMUX_SERVERS" ;;  # "server" keeps our fds
esac
exit 0
EOF
export FAKE_TMUX_SERVERS="$T/tmux-servers"; : > "$FAKE_TMUX_SERVERS"
BOARD_HOME="$T/home6" PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" --only NEW >/dev/null 2>&1
eq "fleet-up exits 0" "$?" "0"
[ -s "$FAKE_TMUX_SERVERS" ] && ok "fake tmux server running" || bad "fake tmux server running"
flock -n "$T/home6/fleet-up.lock" true && ok "lock is free after fleet-up exits (no fd leak into tmux)" \
  || bad "lock is free after fleet-up exits (no fd leak into tmux)"
# A second fleet-up while one holds the lock leaves at once, quietly, with 0.
flock "$T/home6/fleet-up.lock" sleep 5 & holder=$!
sleep 0.2
t0="${EPOCHREALTIME/./}"
o2="$(BOARD_HOME="$T/home6" PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" 2>&1)"; rc=$?
el=$(( (${EPOCHREALTIME/./} - t0) / 1000 ))
eq "concurrent fleet-up exits 0" "$rc" "0"
eq "concurrent fleet-up is silent" "$o2" ""
[ "$el" -lt 2000 ] && ok "concurrent fleet-up does not wait (${el}ms)" || bad "concurrent fleet-up does not wait" "${el}ms"
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
while read -r p; do kill "$p" 2>/dev/null; done < "$FAKE_TMUX_SERVERS"
# Multi-line initial_prompt becomes one line (it is typed into the pane).
printf 'sessions:\n  - code: ML\n    cwd: %s\n    autostart: true\n    initial_prompt: "line one\\nline two"\n' "$T" > "$BOARD_CONFIG_DIR/fleet.yaml"
o2="$(BOARD_HOME="$T/home7" PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" --dry-run --only ML)"
grep -qF "claude -n ML --remote-control ML 'line one line two'" <<<"$o2" && ok "multi-line initial_prompt flattened" \
  || bad "multi-line initial_prompt flattened" "$o2"
resume_cmd="$(sed -n 's/^.*NEW: starting in [^:]*: //p' <<<"$out")"
case "$resume_cmd" in *"&& claude -n NEW --remote-control NEW --verbose") ok "no prompt on sessions without initial_prompt" ;; *) bad "no prompt on sessions without initial_prompt" "$resume_cmd" ;; esac
out="$(BOARD_HOME="$T/home4" PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" --dry-run)"
grep -q "would run: .*board-collect --baseline" <<<"$out" && ok "fleet-up takes a baseline when alerts.json is missing" || bad "fleet-up baseline" "$out"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
