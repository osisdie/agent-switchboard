#!/usr/bin/env bash
# shellcheck disable=SC2015  # ok/bad always return 0
# tests/test_notify.sh — offline tests for board-notify / lib/notify.py and the statusline tap.
# No real Slack, LINE, AWS, SMTP server or Windows needed: curl, powershell.exe and aws are
# shimmed on PATH, SMTP goes to tests/fake_smtp.py, the clock is faked with BOARD_NOW_MS.
#
# Usage: tests/test_notify.sh [-v]
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1

T="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-notify-test.XXXXXX")"
SMTP_PID=""
cleanup() { [ -n "$SMTP_PID" ] && kill "$SMTP_PID" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

export TZ=UTC
export BOARD_HOME="$T/home"
export BOARD_CONFIG_DIR="$T/config"
unset BOARD_NOW_MS BOARD_STATUSLINE_NEXT
while IFS='=' read -r k _; do
  case "$k" in SLACK_*|SMTP_*|ALERT_*|LINE_*|AWS_*) unset "$k" ;; esac
done < <(env)
mkdir -p "$BOARD_HOME" "$BOARD_CONFIG_DIR" "$T/bin" "$T/curl" "$T/ps" "$T/aws" "$T/mail"

pass=0; fail=0
ok() { pass=$((pass + 1)); [ "$VERBOSE" = 1 ] && echo "  ok   $1"; return 0; }
bad() { fail=$((fail + 1)); echo "  FAIL $1"; [ -n "${2:-}" ] && echo "       $2"; return 0; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got '$2', want '$3'"; fi; }
has() { if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "'$3' not in: $(printf '%s' "$2" | head -c 300)"; fi; }
hasnt() { if printf '%s' "$2" | grep -qF -- "$3"; then bad "$1" "'$3' unexpectedly present"; else ok "$1"; fi; }

# ---------------------------------------------------------------- shims
cat >"$T/bin/curl" <<'EOF'
#!/usr/bin/env bash
# Records the config read from stdin (-K -) and the argv; prints FAKE_CURL_CODE as http_code.
f="$(mktemp "$SHIM_DIR/curl/call.XXXXXX")"
cat >"$f.cfg"; printf '%s\n' "$*" >"$f.args"; rm -f "$f"
[ -n "${FAKE_CURL_SLEEP:-}" ] && sleep "$FAKE_CURL_SLEEP"
printf '%s' "${FAKE_CURL_CODE:-200}"
EOF
cat >"$T/bin/powershell.exe" <<'EOF'
#!/usr/bin/env bash
f="$(mktemp "$SHIM_DIR/ps/call.XXXXXX")"; printf '%s\n' "$*" >"$f"
sleep "${FAKE_PS_SLEEP:-0}"
EOF
cat >"$T/bin/aws" <<'EOF'
#!/usr/bin/env bash
f="$(mktemp "$SHIM_DIR/aws/call.XXXXXX")"; printf '%s\n' "$*" >"$f"
echo '{"MessageId":"x"}'
EOF
chmod +x "$T/bin/"*
export SHIM_DIR="$T"
export PATH="$T/bin:$PATH"

python3 "$REPO/tests/fake_smtp.py" "$T/smtp.port" "$T/mail" &
SMTP_PID=$!
for _ in $(seq 1 50); do [ -s "$T/smtp.port" ] && break; sleep 0.1; done
SMTP_PORT_NUM="$(cat "$T/smtp.port")"

cat >"$BOARD_CONFIG_DIR/.env" <<EOF
SLACK_WEBHOOK_URL=https://hooks.example.test/services/T0/B0/SECRETHOOK123
SLACK_MENTION=<@U0TESTER>
SMTP_HOST=127.0.0.1
SMTP_PORT=$SMTP_PORT_NUM
SMTP_SECURITY=none
SMTP_FROM=alerts@example.test
ALERT_EMAIL_TO=me@example.test
LINE_CHANNEL_ACCESS_TOKEN=line-secret-token-xyz
LINE_TO=U00000000000000000000000000000000
AWS_SNS_TOPIC_ARN=arn:aws:sns:us-east-1:000000000000:switchboard-test
ALERT_WEBHOOK_URL=https://webhook.example.test/alert
EOF
: >"$BOARD_CONFIG_DIR/fleet.yaml"

N="$REPO/bin/board-notify"
T0=1794650400000          # 2026-11-14 10:00 UTC
MIN=60000
reset_calls() { rm -f "$T"/curl/* "$T"/ps/* "$T"/aws/* "$T"/mail/*; }
n_slack() { grep -l 'hooks.example.test' "$T"/curl/*.cfg 2>/dev/null | wc -l | tr -d ' '; }
n_line() { grep -l 'api.line.me' "$T"/curl/*.cfg 2>/dev/null | wc -l | tr -d ' '; }
n_hook() { grep -l 'webhook.example.test' "$T"/curl/*.cfg 2>/dev/null | wc -l | tr -d ' '; }
n_ps() { find "$T/ps" -type f | wc -l | tr -d ' '; }
n_aws() { find "$T/aws" -type f | wc -l | tr -d ' '; }
n_mail() { find "$T/mail" -name '*.eml' | wc -l | tr -d ' '; }
calls() { printf '%s/%s/%s/%s/%s/%s' "$(n_slack)" "$(n_ps)" "$(n_mail)" "$(n_line)" "$(n_aws)" "$(n_hook)"; }
at() { BOARD_NOW_MS="$1"; export BOARD_NOW_MS; }
wait_mail() { for _ in $(seq 1 30); do [ "$(n_mail)" -ge "$1" ] && return 0; sleep 0.1; done; }
# Desktop toasts run detached, so their shim may record the call a little later: poll, don't sleep.
wait_calls() { for _ in $(seq 1 50); do [ "$(calls)" = "$1" ] && return 0; sleep 0.1; done; }
alert() { jq -r "$2" <<<"$(jq --arg k "$1" '.[$k]' "$BOARD_HOME/alerts.json")"; }

echo "routing (slack/desktop/email/line/sns/webhook):"
at "$T0"
reset_calls
out="$("$N" send --level info --key t:info --title "fyi" --body "just so you know")"
has "send prints status" "$out" "t:info info sent"
eq "info -> slack only" "$(calls)" "1/0/0/0/0/0"
hasnt "info: no mention" "$(cat "$T"/curl/*.cfg)" "U0TESTER"

reset_calls
start=$(date +%s%N)
FAKE_PS_SLEEP=5 "$N" send --level warn --key t:warn --title "API needs you" >/dev/null
el=$(( ($(date +%s%N) - start) / 1000000 ))
wait_calls "1/1/0/0/0/0"
eq "warn -> slack + desktop" "$(calls)" "1/1/0/0/0/0"
has "warn: slack mention" "$(cat "$T"/curl/*.cfg)" "<@U0TESTER>"
if [ "$el" -lt 3000 ]; then ok "desktop does not block (${el}ms)"; else bad "desktop does not block" "${el}ms"; fi
has "desktop uses an encoded command" "$(cat "$T"/ps/*)" "-EncodedCommand"

reset_calls
"$N" send --level high --key t:high --title "decision waiting" --body "Q1" >/dev/null
wait_mail 1
wait_calls "1/1/1/0/0/0"
eq "high -> slack + desktop + email" "$(calls)" "1/1/1/0/0/0"
m="$(cat "$T"/mail/*.eml)"
has "email subject has HIGH" "$m" "Subject: [agent-switchboard] HIGH: decision waiting"
has "email to ALERT_EMAIL_TO" "$m" "X-Fake-Rcpt: <me@example.test>"
has "email: no AUTH over plaintext" "$m" "X-Fake-Auth: none"

reset_calls
long="$(head -c 6000 /dev/zero | tr '\0' 'x')"
"$N" send --level urgent --key t:urgent --title "prod gate" --body "$long" --url "https://example.test/a" >/dev/null
wait_mail 1
wait_calls "1/1/1/1/1/1"
eq "urgent -> all six channels" "$(calls)" "1/1/1/1/1/1"
lcfg="$(cat "$(grep -l 'api.line.me' "$T"/curl/*.cfg)")"
has "line: push endpoint" "$lcfg" 'url = "https://api.line.me/v2/bot/message/push"'
has "line: bearer token" "$lcfg" 'header = "Authorization: Bearer line-secret-token-xyz"'
ltext="$(sed -n 's/^data-binary = //p' <<<"$lcfg" | python3 -c 'import json,sys; print(json.loads(json.loads(sys.stdin.read()))["messages"][0]["text"].__len__())')"
eq "line: text capped at 5000" "$ltext" "5000"
has "line: to = LINE_TO" "$lcfg" 'U00000000000000000000000000000000'
has "sns: aws sns publish" "$(cat "$T"/aws/*)" "sns publish --topic-arn arn:aws:sns:us-east-1:000000000000:switchboard-test"
has "webhook: JSON payload" "$(cat "$(grep -l 'webhook.example.test' "$T"/curl/*.cfg)")" '\"level\": \"urgent\"'
hasnt "secrets not on curl argv" "$(cat "$T"/curl/*.args)" "SECRETHOOK"
hasnt "token not on curl argv" "$(cat "$T"/curl/*.args)" "line-secret"
ev="$(grep '"type": "alert"' "$BOARD_HOME/events.jsonl" | tail -n 1)"
has "event logged with channel/level/ok" "$ev" '"level": "urgent"'

echo "dedupe / repeat:"
reset_calls
out="$("$N" send --level warn --key t:warn --title "API needs you")"
has "same key+level within repeat -> deduped" "$out" "deduped"
eq "deduped: nothing sent" "$(calls)" "0/0/0/0/0/0"
at $((T0 + 61 * MIN))
"$N" send --level warn --key t:warn --title "API needs you" >/dev/null
eq "after repeat_minutes -> re-alert" "$(n_slack)" "1"

echo "adapter failure never raises:"
reset_calls
at $((T0 + 200 * MIN))
out="$(FAKE_CURL_CODE=500 "$N" send --level info --key t:fail --title "boom")"; rc=$?
eq "send exit 0 on failure" "$rc" "0"
has "failure reported" "$out" "slack=FAIL(http 500)"
has "failure logged" "$(tail -n 1 "$BOARD_HOME/events.jsonl")" '"error": "http 500"'
# A port that was just free refuses at once (some hosts silently drop low ports like 1 → 5 s timeout).
closed_port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
out="$(SMTP_PORT="$closed_port" "$N" test --channel email)"; rc=$?
eq "email to a closed port -> exit 1" "$rc" "1"
has "email failure reported" "$out" "email: FAIL"
for f in "$BOARD_HOME/events.jsonl" "$BOARD_HOME/alerts.json"; do
  for s in SECRETHOOK pw-secret line-secret; do hasnt "no $s in $(basename "$f")" "$(cat "$f")" "$s"; done
done

echo "email transport security:"
# The fake server has no TLS. Credentials must never go over it, and plaintext needs SMTP_SECURITY=none.
reset_calls
out="$(SMTP_USER=alerts-user SMTP_PASSWORD=pw-secret-123 "$N" test --channel email)"; rc=$?
eq "none + credentials -> refused (exit 1)" "$rc" "1"
has "none + credentials: reason" "$out" "refusing to send them in plaintext"
out="$(SMTP_SECURITY=auto SMTP_USER=alerts-user SMTP_PASSWORD=pw-secret-123 "$N" test --channel email)"; rc=$?
eq "auto without STARTTLS + credentials -> refused" "$rc" "1"
has "auto without STARTTLS: reason" "$out" "no STARTTLS"
out="$(SMTP_SECURITY=auto "$N" test --channel email)"; rc=$?
eq "auto without STARTTLS, no credentials -> still refused" "$rc" "1"
out="$(SMTP_SECURITY=bogus "$N" test --channel email)"; rc=$?
eq "unknown SMTP_SECURITY -> refused" "$rc" "1"
sleep 0.2
eq "nothing delivered over plaintext with credentials" "$(n_mail)" "0"
if grep -rqs "pw-secret" "$T/mail" "$BOARD_HOME/events.jsonl"; then bad "password never sent or logged"; else ok "password never sent or logged"; fi

echo "slack / desktop escaping:"
out="$(python3 - "$REPO/lib" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import notify
sent = []
notify.http_post = lambda url, payload, headers=None: (sent.append(payload) or (True, None))
msg = {"level": "warn", "key": "k", "title": "A <!channel> & <@U1>", "body": "see <https://evil.example|bank>",
       "url": "javascript:alert(1)"}
notify.a_slack(msg, {"SLACK_WEBHOOK_URL": "https://hooks.example.test/x", "SLACK_MENTION": "<@U0ME>"}, {"mention_from": "warn"})
print(sent[0]["text"])
msg["url"] = "https://board.example.test/a?b=1&c=2"
notify.a_slack(msg, {"SLACK_WEBHOOK_URL": "https://hooks.example.test/x"}, {"mention_from": "warn"})
print(sent[1]["text"])
argv = []
notify.shutil.which = lambda x: "/usr/bin/notify-send" if x == "notify-send" else None
notify.spawn_detached = lambda a, cwd=None: (argv.append(a) or (True, None))
notify.a_desktop({"level": "warn", "title": "-u critical", "body": "--help"}, {}, {"subject_prefix": ""})
print(" ".join(argv[0]))
PY
)"
has "slack: mention kept" "$out" "<@U0ME>"
has "slack: title escaped" "$out" "A &lt;!channel&gt; &amp; &lt;@U1&gt;"
has "slack: body link escaped" "$out" "see &lt;https://evil.example|bank&gt;"
has "slack: non-http url not a link" "$out" "javascript:alert(1)"
hasnt "slack: no injected link" "$out" "<javascript"
has "slack: http(s) url is a link" "$out" "<https://board.example.test/a?b=1&c=2>"
has "notify-send: -- before summary and body" "$out" "notify-send -u normal -- -u critical --help"

echo "test command:"
reset_calls
out="$("$N" test --channel email)"; rc=$?
wait_mail 1
eq "test email exit 0" "$rc" "0"
has "test email subject" "$(cat "$T"/mail/*.eml)" "Subject: [agent-switchboard] test alert"
out="$(LINE_TO='' LINE_CHANNEL_ACCESS_TOKEN='' "$N" test --channel line 2>&1)"; rc=$?
# the .env still has them; an empty environment value does not override the file
eq "env file still configures line" "$rc" "0"
mv "$BOARD_CONFIG_DIR/.env" "$T/env.saved"
out="$("$N" test --channel line)"; rc=$?
eq "unconfigured channel -> exit 3" "$rc" "3"
has "unconfigured message" "$out" "not configured"
mv "$T/env.saved" "$BOARD_CONFIG_DIR/.env"

echo "quiet hours:"
"$N" ack 't:*' >/dev/null   # earlier routing-test alerts (incl. the failed one awaiting retry)
cat >"$BOARD_CONFIG_DIR/alerting.yaml" <<'EOF'
alerting:
  quiet_hours: "23:00-08:00"
EOF
QT=1794699000000          # 2026-11-14 23:30 UTC
at "$QT"
reset_calls
out="$("$N" send --level high --key q:high --title "held" --no-escalate)"
has "high held in quiet hours" "$out" "quiet"
eq "quiet: nothing sent" "$(calls)" "0/0/0/0/0/0"
out="$("$N" send --level urgent --key q:urgent --title "wake up")"
has "urgent passes quiet hours" "$out" "sent"
reset_calls
at $((QT + 30 * MIN)); "$N" tick >/dev/null
eq "tick in quiet hours: still held" "$(n_slack)" "0"
at $((QT + 9 * 60 * MIN)); "$N" tick >/dev/null   # 08:30
eq "tick after quiet hours delivers held alert" "$(n_slack)" "1"
rm -f "$BOARD_CONFIG_DIR/alerting.yaml"

echo "escalation (tick + snapshot):"
rm -f "$BOARD_HOME/alerts.json"
E0=$((T0 + 24 * 60 * MIN))
snap() {  # snap CP_STATE  — CP needs-you (no pending), AP permission prompt, decisions Q and G
  jq -n --arg cp "$1" '{generated_at: 0,
    sessions: [ {code: "CP", state: $cp, pending: null, remote_url: "https://example.test/cp"},
                {code: "AP", state: "needs-you", pending: {tool_name: "Bash", summary: "rm -rf build", tool_use_id: "tu1"}} ],
    decisions: [ {id: "d-q", kind: "question", from: "CP", question: "Ship it?", options: ["yes","no"], status: "pending"},
                 {id: "d-p", kind: "permission", from: "AP", tool_name: "Bash", summary: "rm", status: "pending"},
                 {id: "d-g", kind: "question", from: "CP", question: "Deploy to production?", gate: "production", status: "pending"} ],
    limit: null}' >"$BOARD_HOME/snapshot.json"
}
snap needs-you
reset_calls
at "$E0"; "$N" tick >/dev/null
eq "new needs-you -> warn" "$(alert session:CP:needs-you .level)" "warn"
eq "permission prompt kind" "$(alert session:AP:needs-you .kind)" "permission"
eq "question decision -> warn" "$(alert decision:d-q .level)" "warn"
eq "permission decision folded into session alert" "$(alert decision:d-p .level)" "null"
eq "production gate -> urgent at once" "$(alert decision:d-g .level)" "urgent"
eq "gate reached LINE" "$(n_line)" "1"
reset_calls
at $((E0 + 5 * MIN)); "$N" tick >/dev/null
eq "5 min: nothing new" "$(calls)" "0/0/0/0/0/0"
"$N" ack decision:d-g >/dev/null
at $((E0 + 10 * MIN)); "$N" tick >/dev/null
eq "10 min: needs-you -> high" "$(alert session:CP:needs-you .level)" "high"
eq "10 min: decision -> high" "$(alert decision:d-q .level)" "high"
eq "10 min: permission prompt -> urgent" "$(alert session:AP:needs-you .level)" "urgent"
wait_mail 3
eq "10 min: emails for the two highs + one urgent" "$(n_mail)" "3"
eq "10 min: LINE only for the permission prompt" "$(n_line)" "1"
eq "ack recorded" "$(alert decision:d-g .acked_by)" "cli"
"$N" ack 'decision:d-q' >/dev/null
reset_calls
at $((E0 + 30 * MIN)); "$N" tick >/dev/null
eq "30 min: needs-you -> urgent" "$(alert session:CP:needs-you .level)" "urgent"
eq "30 min: acked decision stays high" "$(alert decision:d-q .level)" "high"
eq "30 min: one LINE push (CP only)" "$(n_line)" "1"
snap working
reset_calls
at $((E0 + 31 * MIN)); "$N" tick >/dev/null
eq "session left needs-you -> auto-acked" "$(alert session:CP:needs-you .acked_by)" "auto"
eq "auto-ack: nothing sent" "$(calls)" "0/0/0/0/0/0"
at $((E0 + 45 * MIN)); "$N" tick >/dev/null
eq "no escalation after auto-ack" "$(calls)" "0/0/0/0/0/0"
at $((E0 + 95 * MIN)); "$N" tick >/dev/null
eq "unacked urgent repeats after repeat_minutes" "$(n_line)" "1"
snap needs-you
at $((E0 + 96 * MIN)); "$N" tick >/dev/null
eq "needs-you again -> new episode at warn" "$(alert session:CP:needs-you .level)" "warn"
eq "new episode not acked" "$(alert session:CP:needs-you .acked_at)" "null"
jq '.limit = {hit_at: 1, kind: "5h", resets_at: null}' "$BOARD_HOME/snapshot.json" >"$T/s" && mv "$T/s" "$BOARD_HOME/snapshot.json"
reset_calls
at $((E0 + 97 * MIN)); "$N" tick >/dev/null
eq "limit hit -> urgent" "$(alert limit:1 .level)" "urgent"
has "limit title" "$(alert limit:1 .title)" "usage limit hit (5h)"
at $((E0 + 98 * MIN)); "$N" tick >/dev/null
eq "limit alert sent once" "$(n_line)" "1"

echo "usage thresholds:"
rm -f "$BOARD_HOME/alerts.json" "$BOARD_HOME/snapshot.json"
U0=$T0                                      # 10:00 UTC
R5=$((U0 / 1000 + 4 * 3600 + 20 * 60))      # 5h window resets 14:20 (epoch seconds)
RW=$(( $(date -u -d '2026-11-16T09:00:00Z' +%s) * 1000 ))   # weekly resets Mon 09:00 (ms)
usage() { jq -n --argjson p "$1" --argjson r "$2" --argjson wp "${3:-10}" --argjson wr "$RW" \
  '{five_hour: {pct: $p, resets_at: $r}, seven_day: {pct: $wp, resets_at: $wr}, updated_at: 0}' >"$BOARD_HOME/usage.json"; }
reset_calls
at "$U0"; usage 50 "$R5"; "$N" tick >/dev/null
eq "50%: no alert" "$(calls)" "0/0/0/0/0/0"
usage 85 "$R5"; "$N" tick >/dev/null
eq "85% -> usage:5h:80 warn" "$(alert usage:5h:80 .level)" "warn"
eq "85% message" "$(alert usage:5h:80 .title)" "5h usage 85% — resets ~14:20 — heads up"
usage 91 "$R5"; at $((U0 + MIN)); "$N" tick >/dev/null
eq "91% -> high" "$(alert usage:5h:90 .level)" "high"
eq "90% message" "$(alert usage:5h:90 .title)" "5h usage 91% — resets ~14:20 — prepare to switch account"
reset_calls
at $((U0 + 2 * MIN)); "$N" tick >/dev/null
at $((U0 + 90 * MIN)); "$N" tick >/dev/null
eq "once per threshold per window (no repeat)" "$(calls)" "0/0/0/0/0/0"
usage 96 "$R5"; "$N" tick >/dev/null
eq "96% -> urgent" "$(alert usage:5h:95 .level)" "urgent"
has "95% message" "$(alert usage:5h:95 .title)" "switch account now"
eq "urgent usage reached LINE" "$(n_line)" "1"
reset_calls
R5B=$((R5 + 5 * 3600))
at $(( (R5 + 60) * 1000 )); usage 91 "$R5B"; "$N" tick >/dev/null
eq "new window: 90% alerts again" "$(n_slack)" "1"
eq "old window alerts resolved" "$(alert usage:5h:95 .resolved)" "true"
reset_calls
usage 92 $(( R5 - 3600 )); "$N" tick >/dev/null
eq "stale reading (reset already passed) ignored" "$(calls)" "0/0/0/0/0/0"
usage 10 "$R5B" 95; "$N" tick >/dev/null
eq "weekly 95% message" "$(alert usage:7d:95 .title)" "weekly usage 95% — resets Mon 09:00 — switch account now"

echo "statusline tap:"
TAP="$REPO/hooks/board-statusline-tap.sh"
unset BOARD_NOW_MS
rm -f "$BOARD_HOME/usage.json"
in='{"session_id":"s1","model":{"display_name":"M"},"rate_limits":{"five_hour":{"used_percentage":42.5,"resets_at":1794666000},"seven_day":{"used_percentage":7,"resets_at":1794819600000}}}'
printf '%s\n' "$in" >"$T/tap.in"
out="$(BOARD_STATUSLINE_NEXT="cat > '$T/next.in'; echo LINE-OUT; exit 7" "$TAP" <"$T/tap.in")"; rc=$?
for _ in $(seq 1 40); do [ -s "$BOARD_HOME/usage.json" ] && break; sleep 0.05; done  # written in the background
eq "tap: next command's output" "$out" "LINE-OUT"
eq "tap: next command's exit status" "$rc" "7"
if cmp -s "$T/tap.in" "$T/next.in"; then ok "tap: stdin passed byte-exact"; else bad "tap: stdin passed byte-exact"; fi
eq "tap: usage pct" "$(jq -r .five_hour.pct "$BOARD_HOME/usage.json")" "42.5"
eq "tap: resets_at seconds -> ms" "$(jq -r .five_hour.resets_at "$BOARD_HOME/usage.json")" "1794666000000"
eq "tap: resets_at ms kept" "$(jq -r .seven_day.resets_at "$BOARD_HOME/usage.json")" "1794819600000"
eq "tap: session_id" "$(jq -r .session_id "$BOARD_HOME/usage.json")" "s1"
eq "tap: usage.json is 0600" "$(stat -c %a "$BOARD_HOME/usage.json")" "600"
before="$(cat "$BOARD_HOME/usage.json")"
out="$(printf '{"session_id":"s2"}' | BOARD_STATUSLINE_NEXT='cat' "$TAP")"
sleep 0.3
eq "tap: no rate_limits -> passthrough" "$out" '{"session_id":"s2"}'
eq "tap: no rate_limits -> usage.json untouched" "$(cat "$BOARD_HOME/usage.json")" "$before"
rm -f "$BOARD_HOME/usage.json"
out="$("$TAP" <"$T/tap.in")"; rc=$?
eq "tap: no next -> prints nothing" "$out" ""
eq "tap: no next -> exit 0" "$rc" "0"
# Wait for the background write to finish (usage.json reappears) before looking for leftovers.
for _ in $(seq 1 60); do [ -s "$BOARD_HOME/usage.json" ] && break; sleep 0.05; done
sleep 0.05
if ls "$BOARD_HOME"/usage.json.tmp.* >/dev/null 2>&1; then bad "tap: no temp files left"; else ok "tap: no temp files left"; fi
# overhead: tap+true vs plain true, 20 runs each
# Best of 3 rounds, so a load spike from another process doesn't fail the check.
runs=20; over=""
for _ in 1 2 3; do
  s=$(date +%s%N); for _ in $(seq "$runs"); do /bin/sh -c true <"$T/tap.in"; done; base=$(( ($(date +%s%N) - s) / runs / 1000000 ))
  s=$(date +%s%N); for _ in $(seq "$runs"); do BOARD_STATUSLINE_NEXT=true "$TAP" <"$T/tap.in"; done; with=$(( ($(date +%s%N) - s) / runs / 1000000 ))
  o=$((with - base)); { [ -z "$over" ] || [ "$o" -lt "$over" ]; } && over=$o
  [ "$over" -lt 30 ] && break
done
if [ "$over" -lt 30 ]; then ok "tap overhead ${over}ms < 30ms"; else bad "tap overhead < 30ms" "${over}ms"; fi
[ "$VERBOSE" = 1 ] && echo "  (tap overhead ${over}ms)"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
