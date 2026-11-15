#!/usr/bin/env bash
# board-hook.sh — Claude Code hook that records per-session state for agent-switchboard.
#
# Reads the hook JSON on stdin and writes $BOARD_HOME/state/<session_id>.json (atomically),
# appends one line to $BOARD_HOME/events.jsonl, and on a permission prompt creates
# $BOARD_HOME/decisions/<id>.json (kind=permission, deduplicated by tool_use_id). An AskUserQuestion
# picker becomes a decision of kind=question (source=pane) built from its questions and options.
# StopFailure / Notification text that looks like a usage limit records $BOARD_HOME/limit.json;
# the next PreToolUse or Stop after that ends the episode.
#
# Never blocks or changes Claude: prints nothing, always exits 0. Prompt text is never
# recorded; the only tool input captured is a <=200-char summary of the pending tool's input, and
# for AskUserQuestion its questions and options (redacted, clipped, control characters removed).
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

# Read fd 0 with cat, not $(</dev/stdin): Claude Code hands hooks stdin as a socket, and opening
# /dev/stdin on a socket fails (ENXIO), which silently dropped every event.
input="$(cat)"
[ -n "$input" ] || exit 0

BOARD_HOME="${BOARD_HOME:-$HOME/.agent-switchboard}"
state_dir="$BOARD_HOME/state"
dec_dir="$BOARD_HOME/decisions"
umask 077   # state, decisions and events are private to this user
[ -d "$BOARD_HOME" ] || { mkdir -p "$BOARD_HOME" && chmod 700 "$BOARD_HOME"; } 2>/dev/null || exit 0
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

# The tool input as one string: the field a human recognizes (command, path, url, ...) or JSON.
FULLIN='def fullin: (.tool_input // null)
  | if . == null then ""
    elif type == "object" then
      (.command // .file_path // .notebook_path // .url // .pattern // .query // .description
         // (.questions[0]?.question) // tojson)
    else . end
  | if type == "string" then . else tojson end;'
full_sha=""
case "$event" in
  PreToolUse|PermissionRequest)
    full_sha="$(printf '%s' "$input" | jq -j "$FULLIN fullin" 2>/dev/null | sha256sum 2>/dev/null)"
    full_sha="${full_sha%% *}" ;;
esac

# Token-like values never reach the state file, the db or Slack. (A quoted heredoc keeps the
# regexes readable; `read -d ''` is a builtin, so this costs no fork.)
IFS= read -r -d '' REDACT <<'JQ' || true
def redact:
  # Authorization headers
  gsub("(?<k>(?i:bearer|basic))\\s+[A-Za-z0-9._~+/=-]{6,}"; "\(.k) [REDACTED]")
  # URL userinfo: scheme://user:password@host
  | gsub("://[^/\\s:@]+:[^/\\s@]+@"; "://[REDACTED]@")
  # vendor tokens
  | gsub("xox[abposr]-[A-Za-z0-9-]+"; "[REDACTED]")
  | gsub("(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"; "[REDACTED]")
  | gsub("\\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{16,}"; "[REDACTED]")
  | gsub("glpat-[A-Za-z0-9_-]{20,}"; "[REDACTED]")
  | gsub("AIza[A-Za-z0-9_-]{35}"; "[REDACTED]")
  | gsub("npm_[A-Za-z0-9]{36}"; "[REDACTED]")
  | gsub("A[KS]IA[0-9A-Z]{16}"; "[REDACTED]")
  # command-line credentials: --password X, --token=X, -p X (not a path/number: mkdir -p, ssh -p 22),
  # curl -u / --user user:password
  | gsub("(?<f>(?<![\\w-])(?i:--(?:password|passwd|pass|token|secret|api[_-]?key))(?:\\s+|=))(?<v>\"[^\"]*\"|'[^']*'|[^\\s\"';|&]+)";
         "\(.f)[REDACTED]")
  | gsub("(?<f>(?<![\\w-])-p\\s+)(?<v>[^\\s\"'/.~0-9;|&-][^\\s\"';|&]*)"; "\(.f)[REDACTED]")
  | gsub("(?<f>(?<![\\w-])(?:-u|--user)(?:\\s+|=))(?<v>[^\\s:\"']+:[^\\s\"']+)"; "\(.f)[REDACTED]")
  # key=value / key: value / "key": "value" (JSON) for credential-like keys
  | gsub("(?<k>(?i:password|passwd|token|secret|api[_-]?key|access[_-]?key|client[_-]?secret))(?<q>[\"']?)(?<s>\\s*[=:]\\s*)(?<v>\"[^\"]*\"|'[^']*'|[^\\s&'\",}]+)";
         "\(.k)\(.q)\(.s)[REDACTED]")
  # long hex (except a 40-char git object id) and mixed-case base64-ish runs
  | gsub("(?<h>\\b[0-9a-fA-F]{32,}\\b)";
         if (.h | length) == 40 and (.h | test("^[0-9a-f]+$")) then .h else "[REDACTED]" end)
  | gsub("(?<b>[A-Za-z0-9+/_-]{40,}={0,2})";
         if (.b | test("[0-9]")) and (.b | test("[a-z]")) and (.b | test("[A-Z]"))
            and ((.b | split("/") | map(length) | max) >= 24) then "[REDACTED]" else .b end);
# AskUserQuestion: questions and options as the picker shows them, redacted and clipped. `clipped`
# is true when anything a human answers by (a question, an option label, a count) was cut short.
def qtext($n): (. // "") | tostring | gsub("[\u0000-\u001f\u007f-\u009f]+"; " ") | redact
  | if length > $n then {t: (.[0:$n - 1] + "…"), c: true} else {t: ., c: false} end;
def qinfo:
  (.tool_input.questions // null) as $qs
  | if ($qs | type) != "array" then null
    else [ $qs[0:6][] | select(type == "object")
           | (.question | qtext(300)) as $q | (.header | qtext(40)) as $h
           | [ (.options // [])[0:10][]? | (if type == "object" then . else {label: .} end)
               | {l: (.label | qtext(80) | .t |= gsub("^\\s+|\\s+$"; "")), d: (.description | qtext(160))} ] as $os
           | { question: $q.t, header: $h.t, multiSelect: (.multiSelect == true),
               options: [ $os[] | {label: .l.t, description: .d.t} ],
               c: ($q.c or any($os[]; .l.c) or (((.options // []) | length) > 10)) } ] as $out
      | { questions: [ $out[] | del(.c) ],
          clipped: (any($out[]; .c) or ($qs | length) > 6) } end;
JQ

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
    --arg limre "$LIMIT_RE" \
    --arg sha "$full_sha" "$FULLIN$REDACT"'
    def clean: with_entries(select(.value != null and .value != ""));
    def info($full; $sha):
      ($full | redact) as $r
      | { summary: $r[0:200], summary_truncated: (($r | length) > 200),
          summary_len: ($full | length), summary_sha256: (if $sha == "" then null else $sha end) };

    select((.session_id // "") == $sid)
    | . as $in
    | ($old[0] // {}) as $o
    | ($in.hook_event_name // "") as $ev
    | ($in.notification_type // "") as $nt
    | ($in.tool_use_id // "") as $tu
    | ($in.tool_name // "") as $tn
    | (info($in | fullin; $sha)
       + (if $tn == "AskUserQuestion" then ($in | qinfo) as $qi
            | if $qi == null then {} else {questions: $qi.questions, questions_clipped: $qi.clipped} end
          else {} end)) as $si
    | ($o.recent_pre // []) as $recent
    | ($o.pending // null) as $pend
    # Which PreToolUse does a permission prompt belong to? Never guess: when several in-flight
    # tool uses could match, the prompt is "ambiguous" and gets no pressable decision.
    | ( if $tu != "" then {tool_use_id: $tu, tool_name: $tn} + $si
        elif $ev == "PermissionRequest" then
          [ $recent[] | select(.tool_name == $tn and $sha != "" and .summary_sha256 == $sha) ] as $exact
          | [ $recent[] | select(.tool_name == $tn) ] as $named
          | if ($exact | length) == 1 then $exact[0]
            elif ($named | length) > 0 then {tool_use_id: null, tool_name: $tn, ambiguous: true} + $si
            else {tool_use_id: null, tool_name: $tn} + $si end
        elif ($recent | length) == 1 then $recent[0]
        elif ($recent | length) > 1 then {tool_use_id: null, tool_name: null, summary: "", ambiguous: true}
        else null end ) as $match
    # An AskUserQuestion picker is on screen from its PreToolUse on: it is a prompt event too.
    | ( $ev == "PermissionRequest" or ($ev == "Notification" and $nt == "permission_prompt")
        or ($ev == "PreToolUse" and $tn == "AskUserQuestion" and $tu != "") ) as $prompt_ev
    | ( if $prompt_ev then
          ( if $pend != null and ($ev == "Notification" or $match == null
                                   or ($pend.tool_use_id // "") == ($match.tool_use_id // "")) then $pend
            else ($match // null) end ) as $p
          | { state: "needs-you",
              pending: (if $p == null then null
                        else { tool_use_id: ($p.tool_use_id // null), tool_name: ($p.tool_name // null),
                               summary: ($p.summary // ""),
                               summary_truncated: ($p.summary_truncated // false),
                               summary_len: ($p.summary_len // null),
                               summary_sha256: ($p.summary_sha256 // null),
                               ambiguous: ($p.ambiguous // false),
                               decision_id: ($p.decision_id // null) }
                             + (if $p.questions == null then {}
                                else {questions: $p.questions, questions_clipped: ($p.questions_clipped // false)} end)
                        end) }
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
           and ($t.pending.ambiguous | not) and ($t.pending.decision_id // null) == null
        then $dec_id else null end ) as $new_dec
    | ( if $new_dec != null then $t.pending + {decision_id: $new_dec} else $t.pending end ) as $pending
    | ( if $ev == "PreToolUse" and $tu != "" then
          ($recent + [{tool_use_id: $tu, tool_name: $tn} + $si]) | .[-8:]
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
        elif $pending.tool_name == "AskUserQuestion" then
          ($pending.questions // []) as $qs
          | ($qs | length == 1 and ($qs[0].multiSelect | not) and ($pending.questions_clipped // false | not)
             and ($qs[0].options | length) >= 1 and ($qs[0].options | length) <= 8
             # repeated labels (case-insensitive) would make a label answer ambiguous
             and ([ $qs[0].options[].label | ascii_downcase ] | unique | length) == ($qs[0].options | length)
            ) as $pressable
          | { id: $new_dec, kind: "question", source: "pane", from: null,
              question: (if ($qs | length) > 0 then $qs[0].question else ($pending.summary // "") end),
              options: (if ($qs | length) == 1 then [ $qs[0].options[].label ] else [] end),
              questions: $qs, questions_clipped: ($pending.questions_clipped // false),
              pressable: $pressable, context: null,
              tool_use_id: $pending.tool_use_id, tool_name: $pending.tool_name,
              summary: $pending.summary, summary_truncated: $pending.summary_truncated,
              summary_len: $pending.summary_len, summary_sha256: $pending.summary_sha256,
              session_id: $in.session_id,
              tmux_pane: (if $pane != "" then $pane else ($o.tmux_pane // null) end),
              status: "pending", answer: null, answered_by: null, answered_at: null,
              relayed_at: null, created_at: $now }
        else { id: $new_dec, kind: "permission", from: null,
               question: ("Allow " + ($pending.tool_name // "tool") + "?"),
               options: ["allow", "deny"], context: null,
               tool_use_id: $pending.tool_use_id, tool_name: $pending.tool_name,
               summary: $pending.summary, summary_truncated: $pending.summary_truncated,
               summary_len: $pending.summary_len, summary_sha256: $pending.summary_sha256,
               session_id: $in.session_id,
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
