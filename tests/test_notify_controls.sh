#!/usr/bin/env bash
# shellcheck disable=SC2015,SC2016  # ok/bad always return 0; jq programs are single-quoted
# tests/test_notify_controls.sh — notification controls (lib/prefs.py + lib/notify.py + bin/board):
# mutes and their expiry, urgent breakthrough, per-channel and global min levels, presence-aware
# reduction, suppression logging, escalation re-sends, `board notify`, CLI parsing and exit codes.
# Offline: curl, powershell.exe and aws are shimmed, SMTP goes to tests/fake_smtp.py, the clock is
# faked with BOARD_NOW_MS.
#
# Usage: tests/test_notify_controls.sh [-v]
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1

T="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-controls-test.XXXXXX")"
SMTP_PID=""
cleanup() { [ -n "$SMTP_PID" ] && kill "$SMTP_PID" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

export TZ=UTC
export BOARD_HOME="$T/home"
export BOARD_CONFIG_DIR="$T/config"
unset BOARD_NOW_MS BOARD_STATUSLINE_NEXT BOARD_PUBLIC_URL TMUX TMUX_PANE
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

# ---------------------------------------------------------------- shims (as in test_notify.sh)
cat >"$T/bin/curl" <<'EOF'
#!/usr/bin/env bash
f="$(mktemp "$SHIM_DIR/curl/call.XXXXXX")"
cat >"$f.cfg"; printf '%s\n' "$*" >"$f.args"; rm -f "$f"
printf '%s' "${FAKE_CURL_CODE:-200}"
EOF
cat >"$T/bin/powershell.exe" <<'EOF'
#!/usr/bin/env bash
f="$(mktemp "$SHIM_DIR/ps/call.XXXXXX")"; printf '%s\n' "$*" >"$f"
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

cat >"$BOARD_CONFIG_DIR/.env" <<EOF
SLACK_WEBHOOK_URL=https://hooks.example.test/services/T0/B0/SECRETHOOK123
SMTP_HOST=127.0.0.1
SMTP_PORT=$(cat "$T/smtp.port")
SMTP_SECURITY=none
SMTP_FROM=alerts@example.test
ALERT_EMAIL_TO=me@example.test
LINE_CHANNEL_ACCESS_TOKEN=line-secret-token-xyz
LINE_TO=U00000000000000000000000000000000
AWS_SNS_TOPIC_ARN=arn:aws:sns:us-east-1:000000000000:switchboard-test
ALERT_WEBHOOK_URL=https://webhook.example.test/alert
EOF
fleet() { printf 'approvers: [local, boss@example.com]\n%s\n' "${1:-}" >"$BOARD_CONFIG_DIR/fleet.yaml"; }
fleet

N="$REPO/bin/board-notify"
B="$REPO/bin/board"
P="$BOARD_HOME/notify-prefs.json"
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
# Desktop toasts and email arrive a little later: poll until the expected count, then compare.
wait_calls() { for _ in $(seq 1 30); do [ "$(calls)" = "$1" ] && return 0; sleep 0.1; done; }
expect() { wait_calls "$2"; eq "$1" "$(calls)" "$2"; }
at() { BOARD_NOW_MS="$1"; export BOARD_NOW_MS; }
alert() { jq -r --arg k "$1" ".[\$k] | $2" "$BOARD_HOME/alerts.json"; }
# held KEY -> "channel:reason ..." of the alert-suppressed events for KEY, sorted
held() { jq -r --arg k "$1" 'select(.type == "alert-suppressed" and .key == $k) | "\(.channel):\(.reason)"' \
           "$BOARD_HOME/events.jsonl" | sort | paste -sd ' ' -; }
send() { "$N" send --level "$1" --key "$2" --title "t $2"; }
fresh() { rm -f "$P" "$BOARD_HOME/presence.json" "$BOARD_HOME/alerts.json"; reset_calls; }
# present IDENT [AGE_S] [VISIBLE] [IDLE_S] — a presence entry as board-serve writes it
present() {
  local now="$BOARD_NOW_MS" age="${2:-10}" vis="${3:-true}" idle="${4:-5}"
  jq -n --arg id "$1" --argjson at $((now - age * 1000)) --argjson seen $((now - idle * 1000)) --argjson vis "$vis" \
    '{($id): {at: $at, visible: $vis, interaction_at: $seen, via: "loopback"}}' >"$BOARD_HOME/presence.json"
}

at "$T0"

echo "baseline:"
fresh
out="$(send high k:base)"
has "no controls: sent" "$out" "k:base high sent"
expect "high -> slack + desktop + email" "1/1/1/0/0/0"

echo "mute one channel:"
fresh
out="$("$B" mute slack --for 1h)"; rc=$?
eq "board mute slack --for 1h exits 0" "$rc" "0"
has "  says until when" "$out" "muted slack until 11:00"
eq "  stored with until = now + 1h" "$(jq -r '.mutes.slack.until' "$P")" "$((T0 + 60 * MIN))"
eq "  prefs file is 0600" "$(stat -c %a "$P")" "600"
out="$(send high k:m1)"
has "send reports the held channel" "$out" "slack=held(muted:slack)"
expect "  slack held, the rest delivered" "0/1/1/0/0/0"
eq "  suppression logged with its reason" "$(held k:m1)" "slack:muted:slack"
out="$(send urgent k:m2)"
expect "urgent breaks through a channel mute" "1/2/2/1/1/1"
eq "  nothing held" "$(held k:m2)" ""
at $((T0 + 61 * MIN)); reset_calls
send high k:m3 >/dev/null
expect "after the mute expires slack delivers again" "1/1/1/0/0/0"
eq "  the expired mute is not shown" "$("$B" mute status --json | jq -c .mutes)" "{}"
at "$T0"

echo "mute all:"
fresh
"$B" mute all --by CP >/dev/null
out="$(send warn k:a1)"
has "status suppressed when every channel is held" "$out" "k:a1 warn suppressed"
expect "  nothing delivered" "0/0/0/0/0/0"
eq "  both routed channels logged (muted:all)" "$(held k:a1)" "desktop:muted:all slack:muted:all"
eq "  the level counts as sent (no replay)" "$(alert k:a1 '.sent.warn')" "$T0"
eq "  and is marked suppressed" "$(alert k:a1 '.suppressed.warn')" "$T0"
send urgent k:a2 >/dev/null
expect "urgent breaks through mute all" "1/1/1/1/1/1"
"$B" mute all --include-urgent >/dev/null
reset_calls
out="$(send urgent k:a3)"
has "mute all --include-urgent: urgent suppressed too" "$out" "k:a3 urgent suppressed"
expect "  nothing delivered" "0/0/0/0/0/0"
eq "  all six logged" "$(held k:a3 | wc -w | tr -d ' ')" "6"
"$B" unmute all >/dev/null
send warn k:a4 >/dev/null
expect "unmute all: delivered again" "1/1/0/0/0/0"
"$B" mute all --for 1h >/dev/null
"$B" mute slack >/dev/null
out="$("$B" unmute slack 2>&1)"
has "unmute one channel while 'all' is muted warns" "$out" "still muted by 'mute all'"
out="$("$B" unmute all)"
has "unmute all removes every mute" "$out" "unmuted all"
eq "  no mutes left" "$(jq -c .mutes "$P")" "{}"

echo "min level per channel:"
fresh
"$B" notify-level slack high >/dev/null
send warn k:l1 >/dev/null
expect "slack high+: warn reaches desktop only" "0/1/0/0/0/0"
eq "  logged below_min_level" "$(held k:l1)" "slack:below_min_level"
send high k:l2 >/dev/null
expect "  high reaches slack" "1/2/1/0/0/0"
"$B" notify-level email urgent >/dev/null
reset_calls
send high k:l3 >/dev/null
expect "email urgent only: high skips email" "1/1/0/0/0/0"
send urgent k:l4 >/dev/null
expect "  urgent reaches email" "2/2/1/1/1/1"

echo "global min level:"
fresh
"$B" notify-level all high >/dev/null
out="$(send warn k:g1)"
has "everything high+: warn suppressed" "$out" "k:g1 warn suppressed"
expect "  nothing delivered" "0/0/0/0/0/0"
send high k:g2 >/dev/null
expect "  high delivered" "1/1/1/0/0/0"
"$B" notify-level slack info >/dev/null
reset_calls
send warn k:g3 >/dev/null
expect "the stricter floor wins (all=high beats slack=info)" "0/0/0/0/0/0"
"$B" notify-level all default >/dev/null
send warn k:g4 >/dev/null
expect "default removes the floor" "1/1/0/0/0/0"
fresh
fleet 'alerting:
  min_level: {slack: warn}'
send info k:c1 >/dev/null
expect "fleet.yaml alerting.min_level is the default" "0/0/0/0/0/0"
eq "  logged" "$(held k:c1)" "slack:below_min_level"
"$B" notify-level slack info >/dev/null
send info k:c2 >/dev/null
expect "prefs override fleet.yaml" "1/0/0/0/0/0"
out="$("$B" mute status)"
has "status names the source" "$out" "slack=info (prefs)"
"$B" notify-level slack default >/dev/null
has "  default falls back to fleet.yaml" "$("$B" mute status)" "slack=warn (config)"
fleet 'alerting:
  min_level: high'
has "a plain level in fleet.yaml is the global floor" "$("$B" mute status)" "all=high (config)"
fleet

echo "presence:"
fresh
present local
out="$(send warn k:p1)"
has "approver watching: warn suppressed" "$out" "k:p1 warn suppressed"
expect "  nothing pushed" "0/0/0/0/0/0"
eq "  logged owner_present" "$(held k:p1)" "desktop:owner_present slack:owner_present"
send high k:p2 >/dev/null
expect "  high still pushed (presence_min_level high)" "1/1/1/0/0/0"
reset_calls
send urgent k:p3 >/dev/null
expect "  urgent pushed" "1/1/1/1/1/1"
reset_calls
present local 121
send warn k:p4 >/dev/null
expect "heartbeat older than 120 s: not present" "1/1/0/0/0/0"
reset_calls
present local 10 false
send warn k:p5 >/dev/null
expect "page hidden: not present" "1/1/0/0/0/0"
reset_calls
present local 10 true 901
send warn k:p6 >/dev/null
expect "no interaction for 15 min: not present" "1/1/0/0/0/0"
reset_calls
present intruder@example.com
send warn k:p7 >/dev/null
expect "a non-approver's presence does not count" "1/1/0/0/0/0"
reset_calls
present BOSS@example.com
send warn k:p8 >/dev/null
expect "approver match is case-insensitive" "0/0/0/0/0/0"
"$B" notify-presence off >/dev/null
send warn k:p9 >/dev/null
expect "notify-presence off: pushed while watching" "1/1/0/0/0/0"
"$B" notify-presence on --level urgent >/dev/null
reset_calls
send high k:p10 >/dev/null
expect "notify-presence on --level urgent: high held while watching" "0/0/0/0/0/0"
fleet 'alerting:
  presence: {reduce: false}'
rm -f "$P"
send warn k:p11 >/dev/null
expect "fleet.yaml presence.reduce false is the default" "1/1/0/0/0/0"
fleet 'alerting:
  presence: {window_seconds: 600}'
present local 300
reset_calls
send warn k:p12 >/dev/null
expect "fleet.yaml presence.window_seconds" "0/0/0/0/0/0"
fleet

echo "escalation re-sends obey the controls:"
fresh
snap() {
  jq -n '{generated_at: 0, sessions: [],
    decisions: [ {id: "d-1-q", kind: "question", from: "CP", question: "Ship it?", options: ["yes","no"], status: "pending"} ],
    limit: null}' >"$BOARD_HOME/snapshot.json"
}
snap
"$B" mute slack --for 15m >/dev/null
"$N" tick >/dev/null
eq "new decision -> warn" "$(alert decision:d-1-q .level)" "warn"
expect "  warn: slack held, desktop delivered" "0/1/0/0/0/0"
at $((T0 + 10 * MIN)); reset_calls
"$N" tick >/dev/null
eq "10 min: escalated to high" "$(alert decision:d-1-q .level)" "high"
expect "  high re-send: slack still muted" "0/1/1/0/0/0"
eq "  both sends logged" "$(held decision:d-1-q)" "slack:muted:slack slack:muted:slack"
at $((T0 + 30 * MIN)); reset_calls
"$N" tick >/dev/null
expect "30 min: urgent, mute expired anyway: everything" "1/1/1/1/1/1"

fresh; at "$T0"
"$B" mute all --for 5m >/dev/null
"$N" tick >/dev/null
expect "mute all: the new alert is held" "0/0/0/0/0/0"
at $((T0 + 6 * MIN)); "$N" tick >/dev/null
expect "mute expired: nothing is replayed" "0/0/0/0/0/0"
at $((T0 + 10 * MIN)); "$N" tick >/dev/null
expect "  the open alert goes out at its next escalation step" "1/1/1/0/0/0"

fresh; at "$T0"
present local
"$N" tick >/dev/null
expect "owner watching: the new warn is held" "0/0/0/0/0/0"
at $((T0 + 10 * MIN)); rm -f "$BOARD_HOME/presence.json"
"$N" tick >/dev/null
expect "owner stopped watching: escalates normally at 10 min" "1/1/1/0/0/0"
at $((T0 + 30 * MIN)); reset_calls
"$N" tick >/dev/null
expect "30 min: urgent goes out" "1/1/1/1/1/1"
"$B" mute all --include-urgent >/dev/null
at $((T0 + 90 * MIN)); reset_calls
"$N" tick >/dev/null
expect "the hourly repeat is held by mute all --include-urgent" "0/0/0/0/0/0"
eq "  all six logged as muted:all" "$(jq -s --argjson t $((T0 + 90 * MIN)) '[.[] | select(.type == "alert-suppressed" and .ts == $t and .reason == "muted:all")] | length' "$BOARD_HOME/events.jsonl")" "6"
"$B" unmute all >/dev/null
at $((T0 + 120 * MIN)); "$N" tick >/dev/null
expect "  unmuted: nothing replayed before the next repeat" "0/0/0/0/0/0"
at $((T0 + 150 * MIN)); "$N" tick >/dev/null
expect "  the next repeat goes out" "1/1/1/1/1/1"
rm -f "$BOARD_HOME/snapshot.json"
at "$T0"

echo "board notify:"
fresh
"$B" notify "task done"
expect "board notify: delivered with no controls" "1/0/0/0/0/0"
"$B" mute slack >/dev/null
"$B" notify "task done"
expect "board notify: slack muted -> nothing sent" "1/0/0/0/0/0"
eq "  logged (key notify)" "$(held notify)" "slack:muted:slack"
"$B" unmute slack >/dev/null
present local
"$B" notify "task done"
expect "board notify is info: held while an approver watches" "1/0/0/0/0/0"
"$N" gate --channel slack --level info; rc=$?
eq "board-notify gate: held -> exit 10" "$rc" "10"
"$N" gate --channel slack --level high; rc=$?
eq "board-notify gate: allowed -> exit 0" "$rc" "0"

echo "status:"
fresh
"$B" mute email --until 14:00 --by CP >/dev/null
"$B" notify-level slack high >/dev/null
present local
out="$("$N" status)"
has "board-notify status shows the mute" "$out" "controls: muted: email until 14:00 (by CP)"
has "  the floors" "$out" "slack=high (prefs)"
has "  who is watching" "$out" "watching now: local"
hasnt "  no secrets" "$out" "SECRETHOOK"
hasnt "  no LINE token" "$out" "line-secret"
js="$("$N" status --json)"
eq "  --json controls" "$(jq -r '.controls.mutes.email.by + "/" + .controls.effective_min_level.slack + "/" + (.controls.presence.present | join(","))' <<<"$js")" "CP/high/local"
hasnt "  --json has no secrets" "$js" "SECRETHOOK"
eq "board mute status --json: breakthrough level" "$("$B" mute status --json | jq -r .breakthrough)" "urgent"

echo "fails open:"
fresh
echo 'not json {' >"$P"
send warn k:f1 >/dev/null
expect "a broken prefs file never silences alerting" "1/1/0/0/0/0"
echo '{"mutes":{"slack":{"until":"soon"},"nope":{}},"min_level":{"slack":"loud"}}' >"$P"
reset_calls
send warn k:f2 >/dev/null
expect "  a malformed until (string) is ignored: the mute is dropped; unknown entries too" "1/1/0/0/0/0"
for bad in '1794654000000.5' 'true' '"1794654000000"'; do
  printf '{"mutes":{"all":{"until":%s,"include_urgent":true}}}\n' "$bad" >"$P"
  reset_calls
  send warn "k:f-$bad" >/dev/null
  expect "  a malformed until ($bad) on mute all --include-urgent is ignored" "1/1/0/0/0/0"
done
eq "  status shows no mute for it" "$("$B" mute status --json | jq -c .mutes)" "{}"
"$B" mute slack --for 1h >/dev/null
eq "  the next write drops the malformed entry" "$(jq -c '.mutes | keys' "$P")" '["slack"]'

echo "cli:"
fresh; rm -f "$BOARD_HOME/events.jsonl"
rc_of() { "$@" >/dev/null 2>&1; echo $?; }
eq "mute without a channel -> 2" "$(rc_of "$B" mute)" "2"
eq "unknown channel -> 2" "$(rc_of "$B" mute pager)" "2"
eq "--for 99x -> 2" "$(rc_of "$B" mute slack --for 99x)" "2"
eq "--for 0h -> 2" "$(rc_of "$B" mute slack --for 0h)" "2"
eq "--for 31d -> 2" "$(rc_of "$B" mute slack --for 31d)" "2"
eq "--for and --until -> 2" "$(rc_of "$B" mute slack --for 1h --until 12:00)" "2"
eq "--until in the past -> 2" "$(rc_of "$B" mute slack --until 2026-11-14T09:00:00Z)" "2"
eq "--until 25:00 -> 2" "$(rc_of "$B" mute slack --until 25:00)" "2"
eq "--by free text -> 2" "$(rc_of "$B" mute slack --by 'two words')" "2"
eq "unknown option -> 2" "$(rc_of "$B" mute slack --forever)" "2"
eq "unmute without a channel -> 2" "$(rc_of "$B" unmute)" "2"
eq "notify-level bad level -> 2" "$(rc_of "$B" notify-level slack loud)" "2"
eq "notify-level without a level -> 2" "$(rc_of "$B" notify-level slack)" "2"
eq "notify-presence maybe -> 2" "$(rc_of "$B" notify-presence maybe)" "2"
eq "notify-presence --level info -> 2" "$(rc_of "$B" notify-presence on --level info)" "2"
[ -e "$P" ] && bad "refused commands write nothing" "$(cat "$P")" || ok "refused commands write nothing"
"$B" mute slack --until 08:00 >/dev/null
eq "--until HH:MM already past today -> tomorrow" "$(jq -r .mutes.slack.until "$P")" "$((T0 + 22 * 60 * MIN))"
"$B" mute slack --until 14:30 >/dev/null
eq "--until HH:MM later today" "$(jq -r .mutes.slack.until "$P")" "$((T0 + 270 * MIN))"
"$B" mute slack --until 2026-11-14T12:00:00Z >/dev/null
eq "--until ISO" "$(jq -r .mutes.slack.until "$P")" "$((T0 + 120 * MIN))"
"$B" mute slack >/dev/null
eq "no expiry -> until null" "$(jq -r .mutes.slack.until "$P")" "null"
eq "mute is audited" "$(jq -s -r '[.[] | select(.type == "notify-prefs" and .action == "mute")] | length' "$BOARD_HOME/events.jsonl")" "4"
eq "board mute --help -> 0" "$(rc_of "$B" mute --help)" "0"
has "  mentions breakthrough" "$("$B" mute --help)" "break through"
eq "board mute status --help -> 0" "$(rc_of "$B" mute status --help)" "0"
eq "board notify-level --help -> 0" "$(rc_of "$B" notify-level --help)" "0"
eq "mute status exits 0" "$(rc_of "$B" mute status)" "0"
"$B" mute status --json | jq -e . >/dev/null && ok "mute status --json is JSON" || bad "mute status --json is JSON"
# Two writers at once: the flock serializes them, neither update is lost.
pids=()
for c in slack desktop email line sns webhook; do "$B" mute "$c" --for 1h >/dev/null & pids+=($!); done
wait "${pids[@]}"
eq "concurrent mutes: none lost" "$(jq -r '.mutes | keys | join(",")' "$P")" "desktop,email,line,slack,sns,webhook"
leftover="$(find "$BOARD_HOME" -name '*.tmp.*' | wc -l | tr -d ' ')"
eq "  no temp files left" "$leftover" "0"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
