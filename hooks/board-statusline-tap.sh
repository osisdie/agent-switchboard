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
# (resets_at as epoch ms). Then execs BOARD_STATUSLINE_NEXT with exactly the same stdin, so the
# statusline output and exit status are the next command's. Without a next command it prints
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

case "$input" in
  *'"rate_limits"'*)
    if command -v jq >/dev/null 2>&1; then
      home="${BOARD_HOME:-$HOME/.agent-switchboard}"
      if [ -n "${EPOCHREALTIME:-}" ]; then now="${EPOCHREALTIME/[.,]/}"; now="${now:0:13}"; else now="$(date +%s)000"; fi
      # Parse and write in the background, detached from the statusline's stdout/stderr, so the
      # statusline never waits for jq.
      (
        [ -d "$home" ] || mkdir -p "$home" || exit 0
        tmp="$home/usage.json.tmp.$BASHPID"
        jq -c --argjson now "$now" '
          def ms: if type == "number" then (if . < 1e12 then . * 1000 else . end | floor) else . end;
          def w(x): if (x | type) != "object" then null
                    else {pct: (x.used_percentage // x.utilization // null), resets_at: (x.resets_at | ms)} end;
          (.rate_limits // {}) as $r
          | if ($r.five_hour == null and $r.seven_day == null) then empty
            else {five_hour: w($r.five_hour), seven_day: w($r.seven_day), updated_at: $now,
                  session_id: (.session_id // null)} end' <<<"$input" >"$tmp" 2>/dev/null
        if [ -s "$tmp" ]; then mv -f "$tmp" "$home/usage.json"; else rm -f "$tmp"; fi
      ) </dev/null >/dev/null 2>&1 &
    fi ;;
esac

next="${BOARD_STATUSLINE_NEXT:-}"
[ -n "$next" ] || exit 0
# Same stdin for the next command. A here-string adds one newline, so use it only when the input
# already ends with one; otherwise feed it through a pipe.
case "$input" in
  *$'\n') exec /bin/sh -c "$next" <<<"${input%$'\n'}" ;;
  *) exec /bin/sh -c "$next" < <(printf '%s' "$input") ;;
esac
