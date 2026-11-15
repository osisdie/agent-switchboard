#!/usr/bin/env bash
# board-statusline-tap.sh — Claude Code statusLine wrapper that records usage for agent-switchboard.
#
# Install as the statusLine command, with the previous command in BOARD_STATUSLINE_NEXT:
#   "statusLine": {"type": "command",
#                  "command": "BOARD_STATUSLINE_NEXT='<previous command>' <repo>/hooks/board-statusline-tap.sh"}
#
# Reads the statusline JSON on stdin. When it carries rate_limits.five_hour / seven_day, writes
# $BOARD_HOME/usage.json atomically (temp file + mv, in a detached background job so the
# statusline never waits for jq):
#   {five_hour:{pct,resets_at}, seven_day:{pct,resets_at}, updated_at, session_id}
# (resets_at as epoch ms). For every session_id it also writes $BOARD_HOME/statusline/<sid>.json
# (0600) with only {ctx_pct, cost_usd, model, at}: context window used %, cost so far, the model's
# display name and the time; nothing else from the input is kept. Then execs BOARD_STATUSLINE_NEXT
# with exactly the same stdin, so the statusline output and exit status are the next command's. Without a next command it prints
# nothing. Never fails because of the tap itself. Needs bash and jq (no jq: pure passthrough).
#
# Env: BOARD_HOME (default ~/.agent-switchboard), BOARD_STATUSLINE_NEXT
set -u

case "${1:-}" in
  -h|--help)
    awk 'NR > 1 && !/^#/ { exit } NR > 1 && !/shellcheck/ { sub(/^# ?/, ""); print }' "$0"
    exit 0 ;;
esac

# Read stdin byte-exact without forking (read -d '' stops at EOF, keeps trailing newlines).
IFS= read -r -d '' input || true

# The session meter: only for a filename-safe session_id (matched without a fork).
sid=""
sid_re='"session_id"[[:space:]]*:[[:space:]]*"([A-Za-z0-9_-][A-Za-z0-9._-]{0,127})"'
[[ $input =~ $sid_re ]] && sid="${BASH_REMATCH[1]}"
usage=0
case "$input" in *'"rate_limits"'*) usage=1 ;; esac

if { [ -n "$sid" ] || [ "$usage" = 1 ]; } && command -v jq >/dev/null 2>&1; then
  home="${BOARD_HOME:-$HOME/.agent-switchboard}"
  if [ -n "${EPOCHREALTIME:-}" ]; then now="${EPOCHREALTIME/[.,]/}"; now="${now:0:13}"; else now="$(date +%s)000"; fi
  # Parse and write in the background, detached from the statusline's stdout/stderr, so the
  # statusline never waits for jq. One jq call prints two lines: the usage record (or null) and
  # the meter (or null).
  (
    umask 077   # only in this subshell: the next statusline command keeps the caller's umask
    [ -d "$home" ] || { mkdir -p "$home" && chmod 700 "$home"; } || exit 0
    out="$(jq -c --argjson now "$now" '
      def ms: if type == "number" then (if . < 1e12 then . * 1000 else . end | floor) else . end;
      def w(x): if (x | type) != "object" then null
                else {pct: (x.used_percentage // x.utilization // null), resets_at: (x.resets_at | ms)} end;
      def n: if type == "number" then . else null end;
      (.rate_limits // {}) as $r
      | (if ($r.five_hour == null and $r.seven_day == null) then null
         else {five_hour: w($r.five_hour), seven_day: w($r.seven_day), updated_at: $now,
               session_id: (.session_id // null)} end),
        # Only these four fields: context used %, cost so far, the model name and the time.
        ((.context_window // {}) as $c
         | ($c.current_usage // {}) as $u
         | (($c.used_percentage | n)
            // (if ($c.context_window_size | n) != null and $c.context_window_size > 0 and ($u | type) == "object"
                then ((($u.input_tokens // 0) + ($u.cache_creation_input_tokens // 0) + ($u.cache_read_input_tokens // 0))
                      * 100 / $c.context_window_size)
                else null end)) as $pct
         | {ctx_pct: (if $pct == null then null else ($pct * 10 | round / 10) end),
            cost_usd: (.cost.total_cost_usd | n),
            model: (.model.display_name // null | if type == "string" then .[0:40] else null end),
            at: $now}
         | if .ctx_pct == null and .cost_usd == null and .model == null then null else . end)
    ' <<<"$input" 2>/dev/null)" || exit 0
    { IFS= read -r usage_line; IFS= read -r meter_line; } <<<"$out"
    if [ "$usage" = 1 ] && [ -n "${usage_line:-}" ] && [ "$usage_line" != null ]; then
      tmp="$home/usage.json.tmp.$BASHPID"
      printf '%s\n' "$usage_line" >"$tmp" && mv -f "$tmp" "$home/usage.json"
    fi
    if [ -n "$sid" ] && [ -n "${meter_line:-}" ] && [ "$meter_line" != null ]; then
      mkdir -p "$home/statusline" || exit 0
      tmp="$home/statusline/.$sid.tmp.$BASHPID"
      printf '%s\n' "$meter_line" >"$tmp" && mv -f "$tmp" "$home/statusline/$sid.json"
    fi
  ) </dev/null >/dev/null 2>&1 &
fi

next="${BOARD_STATUSLINE_NEXT:-}"
[ -n "$next" ] || exit 0
# Same stdin for the next command. A here-string adds one newline, so use it only when the input
# already ends with one; otherwise feed it through a pipe.
case "$input" in
  *$'\n') exec /bin/sh -c "$next" <<<"${input%$'\n'}" ;;
  *) exec /bin/sh -c "$next" < <(printf '%s' "$input") ;;
esac
