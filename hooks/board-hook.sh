#!/usr/bin/env bash
# board-hook.sh — Claude Code hook that records per-session state for agent-switchboard.
#
# Reads the hook JSON on stdin and writes $BOARD_HOME/state/<session_id>.json (atomically),
# appends one line to $BOARD_HOME/events.jsonl, and on a permission prompt creates
# $BOARD_HOME/decisions/<id>.json (kind=permission, deduplicated by tool_use_id).
# StopFailure / Notification text that looks like a usage limit records $BOARD_HOME/limit.json;
# the next PreToolUse or Stop after that ends the episode.
#
# Never blocks or changes Claude: prints nothing, always exits 0. Prompt text is never
# recorded; the only tool input captured is a <=200-char summary of the pending tool's input.
#
# Env: BOARD_HOME (default ~/.agent-switchboard)
set -u

case "${1:-}" in
  -h|--help)
    awk 'NR > 1 && !/^#/ { exit } NR > 1 && !/shellcheck/ { sub(/^# ?/, ""); print }' "$0"
    exit 0 ;;
esac

# Nothing we do may reach Claude's stdout/stderr.
exec 1>/dev/null 2>/dev/null

command -v jq >/dev/null 2>&1 || exit 0

input="$(</dev/stdin)"
[ -n "$input" ] || exit 0

BOARD_HOME="${BOARD_HOME:-$HOME/.agent-switchboard}"
state_dir="$BOARD_HOME/state"
dec_dir="$BOARD_HOME/decisions"
mkdir -p "$state_dir" "$dec_dir" 2>/dev/null || exit 0

# session_id from input (no fork); only filename-safe ids are accepted.
re='"session_id"[[:space:]]*:[[:space:]]*"([A-Za-z0-9_-][A-Za-z0-9._-]*)"'
[[ $input =~ $re ]] || exit 0
sid="${BASH_REMATCH[1]}"

# Find the claude process (first ancestor that isn't a shell). Pure bash, no forks.
claude_pid=0
if [ -r "/proc/$$/status" ]; then
  p=$PPID
  for _ in 1 2 3 4 5 6; do
    [ -r "/proc/$p/comm" ] || break
    read -r comm < "/proc/$p/comm"
    case "$comm" in
      sh|bash|dash|zsh|env|timeout|flock) ;;
      *) claude_pid=$p; break ;;
    esac
    ppid=""
    while read -r k v; do [ "$k" = "PPid:" ] && { ppid=$v; break; }; done < "/proc/$p/status"
    [ -n "$ppid" ] && [ "$ppid" != 0 ] || break
    p=$ppid
  done
fi

if [ -n "${EPOCHREALTIME:-}" ]; then
  now_us="${EPOCHREALTIME/./}"
  now_ms=$(( now_us / 1000 ))
else
  now_ms="$(date +%s%3N 2>/dev/null || echo 0)"
fi
dec_id="d-$(( now_ms / 1000 ))-$(printf '%04x%04x' "$RANDOM" "$RANDOM")"

state_file="$state_dir/$sid.json"
self="$(readlink -f "$0")"
LIB="${self%/*}/../lib"
LIMIT_RE='usage[ _]limit|rate[ _]limit|limit reached|hit your (usage )?limit|resets? at|resets [0-9]|weekly limit|5-hour limit|out of (extra )?usage'
event_re='"hook_event_name"[[:space:]]*:[[:space:]]*"([A-Za-z]+)"'
event=""
[[ $input =~ $event_re ]] && event="${BASH_REMATCH[1]}"

update() {
  local old="/dev/null" out st ev dec lim tmp
  [ -s "$state_file" ] && old="$state_file"
  out="$(printf '%s' "$input" | jq -c \
    --slurpfile old "$old" \
    --argjson now "$now_ms" \
    --argjson pid "$claude_pid" \
    --arg pane "${TMUX_PANE:-}" \
    --arg dec_id "$dec_id" \
    --arg sid "$sid" \
    --arg limre "$LIMIT_RE" '
    def summ: (.tool_input // null)
      | if . == null then ""
        elif type == "object" then
          (.command // .file_path // .notebook_path // .url // .pattern // .query // .description
             // (.questions[0]?.question) // tojson)
        else . end
      | if type == "string" then . else tojson end
      | .[0:200];
    def clean: with_entries(select(.value != null and .value != ""));

    select((.session_id // "") == $sid)
    | . as $in
    | ($old[0] // {}) as $o
    | ($in.hook_event_name // "") as $ev
    | ($in.notification_type // "") as $nt
    | ($in.tool_use_id // "") as $tu
    | ($in.tool_name // "") as $tn
    | ($in | summ) as $sm
    | ($o.recent_pre // []) as $recent
    | ($o.pending // null) as $pend
    # Look up the PreToolUse that a permission prompt belongs to.
    | ( if $tu != "" then {tool_use_id: $tu, tool_name: $tn, summary: $sm}
        else ( [ $recent[] | select(.tool_name == $tn and .summary == $sm) ] | last )
          // ( [ $recent[] | select(.tool_name == $tn) ] | last )
          // ( if $tn == "" then ($recent | last) else null end )
          // (if $tn != "" then {tool_use_id: null, tool_name: $tn, summary: $sm} else null end)
        end ) as $match
    | ( if $ev == "PermissionRequest" or ($ev == "Notification" and $nt == "permission_prompt") then
          ( if $pend != null and ($ev == "Notification" or $match == null
                                   or ($pend.tool_use_id // "") == ($match.tool_use_id // "")) then $pend
            else ($match // null) end ) as $p
          | { state: "needs-you",
              pending: (if $p == null then null
                        else { tool_use_id: ($p.tool_use_id // null), tool_name: ($p.tool_name // null),
                               summary: ($p.summary // ""), decision_id: ($p.decision_id // null) } end) }
        elif $ev == "UserPromptSubmit" then {state: "working", pending: null}
        elif $ev == "Stop" or $ev == "StopFailure" then {state: "done", pending: null}
        elif $ev == "Notification" and $nt == "idle_prompt" then {state: "done", pending: $pend}
        elif $ev == "PostToolUse" or $ev == "PostToolUseFailure" then
          ( if $pend == null or ($pend.tool_use_id // "") == "" or $tu == "" or $pend.tool_use_id == $tu
            then {state: "working", pending: null}
            else {state: "needs-you", pending: $pend} end )
        elif $ev == "PreToolUse" then
          ( if $pend != null and ($pend.tool_use_id // "") != $tu
            then {state: "needs-you", pending: $pend}
            else {state: "working", pending: $pend} end )
        elif $ev == "SessionStart" then {state: "rest", pending: null}
        elif $ev == "SessionEnd" then {state: "offline", pending: null}
        else {state: ($o.state // "rest"), pending: $pend} end ) as $t
    # A permission decision is created once per tool_use_id.
    | ( if $t.state == "needs-you" and $t.pending != null and ($t.pending.tool_use_id // "") != ""
           and ($t.pending.decision_id // null) == null then $dec_id else null end ) as $new_dec
    | ( if $new_dec != null then $t.pending + {decision_id: $new_dec} else $t.pending end ) as $pending
    | ( if $ev == "PreToolUse" and $tu != "" then
          ($recent + [{tool_use_id: $tu, tool_name: $tn, summary: $sm}]) | .[-8:]
        elif ($ev == "PostToolUse" or $ev == "PostToolUseFailure") and $tu != "" then
          [ $recent[] | select(.tool_use_id != $tu) ]
        elif $ev == "SessionEnd" or $ev == "Stop" or $ev == "StopFailure" then []
        else $recent end ) as $recent2
    | ( { session_id: $in.session_id,
          pid: (if $pid > 0 then $pid else ($o.pid // null) end),
          cwd: ($in.cwd // $o.cwd // null),
          tmux_pane: (if $pane != "" then $pane else ($o.tmux_pane // null) end),
          state: $t.state,
          last_event: $ev,
          notification_type: (if $ev == "Notification" then $nt else ($o.notification_type // null) end),
          pending: $pending,
          recent_pre: $recent2,
          updated_at: $now } ),
      ( { ts: $now, type: "hook", session_id: $in.session_id, event: $ev,
          notification_type: $nt, tool_name: $tn, tool_use_id: $tu, state: $t.state } | clean ),
      ( if $new_dec == null then null
        else { id: $new_dec, kind: "permission", from: null,
               question: ("Allow " + ($pending.tool_name // "tool") + "?"),
               options: ["allow", "deny"], context: null,
               tool_use_id: $pending.tool_use_id, tool_name: $pending.tool_name,
               summary: $pending.summary, session_id: $in.session_id,
               tmux_pane: (if $pane != "" then $pane else ($o.tmux_pane // null) end),
               status: "pending", answer: null, answered_by: null, answered_at: null,
               relayed_at: null, created_at: $now } end ),
      # Usage-limit text: all of a StopFailure (error details), or a matching notification message.
      ( if $ev == "StopFailure" then
          ([ $in | del(.session_id, .transcript_path, .cwd, .hook_event_name, .permission_mode)
             | .. | strings ] | join(" | ") | .[0:600])
        elif $ev == "Notification" and (($in.message // "") | test($limre; "i")) then $in.message
        else null end )
  ' 2>/dev/null)" || return 0

  { IFS= read -r st; IFS= read -r ev; IFS= read -r dec; IFS= read -r lim; } <<<"$out"
  [ -n "${st:-}" ] || return 0

  tmp="$state_file.tmp.$$"
  printf '%s\n' "$st" > "$tmp" && mv -f "$tmp" "$state_file"
  [ -n "${ev:-}" ] && printf '%s\n' "$ev" >> "$BOARD_HOME/events.jsonl"
  if [ -n "${dec:-}" ] && [ "$dec" != "null" ]; then
    tmp="$dec_dir/.$dec_id.tmp.$$"
    printf '%s\n' "$dec" > "$tmp" && mv -f "$tmp" "$dec_dir/$dec_id.json"
  fi
  if [ -n "${lim:-}" ] && [ "$lim" != "null" ]; then
    python3 "$LIB/limit.py" record --event "$event" --text "$lim"
  fi
}

# End an active usage-limit episode once a model response gets through again.
maybe_clear_limit() {
  local f="$BOARD_HOME/limit.json"
  case "$event" in PreToolUse|Stop) ;; *) return 0 ;; esac
  [ -f "$f" ] || return 0
  grep -q '"cleared_at"' "$f" && return 0
  python3 "$LIB/limit.py" clear --after-ms "$(( now_ms - ${BOARD_LIMIT_GRACE_MS:-5000} ))"
}

# Serialize concurrent hooks of the same session (parallel tool calls); give up after 2 s.
if command -v flock >/dev/null 2>&1; then
  exec 9>>"$state_dir/.$sid.lock" || exit 0
  flock -w 2 9 || exit 0
fi
update
maybe_clear_limit
exit 0
