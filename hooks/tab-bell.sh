#!/usr/bin/env bash
# tab-bell.sh — Claude Code Notification hook: ring the terminal bell of this session when it needs
# you, so the terminal tab that hosts it shows a bell and the taskbar flashes.
#
# Installed by `scripts/install.sh --tab-bell` for the Notification matchers permission_prompt,
# elicitation_dialog, elicitation_url_dialog and agent_needs_input (never idle_prompt: every
# finished turn would ring).
#
# Where the BEL goes:
#   - in tmux ($TMUX_PANE set): the pane's tty (`tmux display -p -t $TMUX_PANE '#{pane_tty}'`).
#     tmux passes it to the client terminal with its defaults (`bell-action any`, `monitor-bell
#     on`, `visual-bell off`); a config with `bell-action none` swallows it and `visual-bell on`
#     shows a message instead.
#   - otherwise, or when tmux cannot name the pane's tty: the controlling terminal (/dev/tty), only
#     if it can be opened for writing. A BEL moves no cursor and prints nothing, so it cannot
#     disturb Claude's screen; with no controlling terminal (a detached or headless run) the open
#     fails and nothing happens.
# The BEL comes from a separate process, so in rare cases it can split an escape sequence Claude is
# writing at that moment and garble one frame.
#
# Never blocks or changes Claude: reads and discards the hook JSON, prints nothing, always exits 0.
#
# Env: BOARD_TAB_BELL=0    ring nothing (also off, false, no); turns the hook off without editing
#                          settings.json
#      BOARD_TAB_BELL=tmux ring only through tmux; never fall back to /dev/tty
#      BOARD_TAB_BELL_TTY  the fallback terminal (default /dev/tty; for tests)
set -u

case "${1:-}" in
  -h|--help)
    awk 'NR > 1 && !/^#/ { exit } NR > 1 && !/shellcheck/ { sub(/^# ?/, ""); print }' "$0"
    exit 0 ;;
esac

# Drain the hook's stdin (never wait on an interactive terminal), then go silent.
[ -t 0 ] || cat >/dev/null 2>&1
exec 1>/dev/null 2>/dev/null

mode="${BOARD_TAB_BELL:-1}"
case "$mode" in 0|[Oo][Ff][Ff]|[Ff][Aa][Ll][Ss][Ee]|[Nn][Oo]) exit 0 ;; esac

ring() { [ -n "$1" ] && [ -w "$1" ] && printf '\a' >"$1"; }

if [ -n "${TMUX_PANE:-}" ] && command -v tmux >/dev/null 2>&1; then
  if command -v timeout >/dev/null 2>&1; then
    tty="$(timeout 1 tmux display -p -t "$TMUX_PANE" '#{pane_tty}' 2>/dev/null)" || tty=""
  else
    tty="$(tmux display -p -t "$TMUX_PANE" '#{pane_tty}' 2>/dev/null)" || tty=""
  fi
  ring "$tty" && exit 0
fi
[ "$mode" = tmux ] && exit 0

# No tmux pane: the controlling terminal, if there is one. The open happens in a subshell so an
# ENXIO (no controlling terminal) cannot end this script with a non-zero status.
fallback="${BOARD_TAB_BELL_TTY:-/dev/tty}"
( printf '\a' >"$fallback" ) 2>/dev/null
exit 0
