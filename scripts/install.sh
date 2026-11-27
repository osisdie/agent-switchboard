#!/usr/bin/env bash
# shellcheck disable=SC2016  # jq programs are single-quoted on purpose
# install.sh — install agent-switchboard for the current user.
#
# Usage: scripts/install.sh [--dry-run] [--tab-bell] [--uninstall]
#
#   - symlinks bin/* into ~/.local/bin
#   - creates $BOARD_HOME (default ~/.agent-switchboard) and its subdirectories
#   - merges hooks/board-hook.sh into ~/.claude/settings.json for the events it handles,
#     keeping every existing hook; backs the file up to settings.json.bak-<date> first.
#   - wraps the statusLine command with hooks/board-statusline-tap.sh (usage meter); the previous
#     statusLine is saved to $BOARD_HOME/statusline.prev.json and restored by --uninstall.
#     Running it again changes nothing.
#   - with --tab-bell, also adds hooks/tab-bell.sh to the Notification event for the matchers
#     permission_prompt, elicitation_dialog, elicitation_url_dialog and agent_needs_input, so the
#     terminal tab of a session that needs you rings its bell (tmux: `bell-action any`, the default).
#
#   --dry-run    print the planned actions and the settings.json diff; change nothing
#   --tab-bell   also install the tab bell hook (opt-in)
#   --uninstall  remove the board-hook and tab-bell entries, restore the statusLine, remove the symlinks
#
# Env: BOARD_HOME, CLAUDE_SETTINGS (default ~/.claude/settings.json), BIN_DIR (default ~/.local/bin)
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
BOARD_HOME="${BOARD_HOME:-$HOME/.agent-switchboard}"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
HOOK="$REPO/hooks/board-hook.sh"
BELL="$REPO/hooks/tab-bell.sh"
BELL_MATCHERS='["permission_prompt","elicitation_dialog","elicitation_url_dialog","agent_needs_input"]'
BELL_RE='(^|/)hooks/tab-bell\.sh($|\s)'   # this repo's hook only, not a user's my-tab-bell.sh
EVENTS='["UserPromptSubmit","PreToolUse","PostToolUse","PostToolUseFailure","PermissionRequest","Notification","Stop","StopFailure","SessionStart","SessionEnd"]'

dry=0; uninstall=0; tab_bell=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|-n) dry=1 ;;
    --uninstall) uninstall=1 ;;
    --tab-bell) tab_bell=1 ;;
    -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1 && !/shellcheck/ { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
    *) echo "install.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v jq >/dev/null 2>&1 || { echo "install.sh: jq is required" >&2; exit 1; }
say() { echo "$*"; }
run() { if [ "$dry" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }

# ---- symlinks
say "bin: $BIN_DIR"
run mkdir -p "$BIN_DIR"
for f in "$REPO"/bin/*; do
  [ -f "$f" ] || continue
  name="$(basename "$f")"
  dest="$BIN_DIR/$name"
  if [ "$uninstall" = 1 ]; then
    if [ -L "$dest" ] && [ "$(readlink -f "$dest")" = "$(readlink -f "$f")" ]; then run rm -f "$dest"; fi
    continue
  fi
  if [ -L "$dest" ] && [ "$(readlink -f "$dest")" = "$(readlink -f "$f")" ]; then
    say "  $name: already linked"
  elif [ -e "$dest" ] && [ ! -L "$dest" ]; then
    say "  $name: $dest exists and is not a symlink — skipped"
  else
    run ln -sfn "$f" "$dest"
  fi
done

# ---- BOARD_HOME
if [ "$uninstall" = 0 ]; then
  say "state: $BOARD_HOME"
  run mkdir -p "$BOARD_HOME"
  run chmod 700 "$BOARD_HOME"   # private: state, decisions, events, alerts
  run mkdir -p "$BOARD_HOME/state" "$BOARD_HOME/decisions" "$BOARD_HOME/outbox"
  run chmod +x "$HOOK" "$BELL" "$REPO"/bin/*
fi

# ---- settings.json
say "settings: $SETTINGS"
if [ -f "$SETTINGS" ]; then
  jq -e . "$SETTINGS" >/dev/null 2>&1 || { echo "install.sh: $SETTINGS is not valid JSON — aborting" >&2; exit 1; }
  current="$(cat "$SETTINGS")"
else
  current='{}'
fi

if [ "$uninstall" = 1 ]; then
  filter='
    .hooks = ((.hooks // {}) | with_entries(
      .value |= ( map(.hooks |= map(select((.command // "") | (test("board-hook\\.sh") or test($bell_re)) | not)))
                  | map(select((.hooks | length) > 0)) ))
      | with_entries(select((.value | length) > 0)))
    | if (.hooks | length) == 0 then del(.hooks) else . end'
else
  filter='
    .hooks = ( reduce $events[] as $ev ((.hooks // {});
      if ([ .[$ev][]?.hooks[]?.command // empty | select(test("board-hook\\.sh")) ] | length) > 0 then .
      else .[$ev] = ((.[$ev] // []) + [ { hooks: [ { type: "command", command: $hook, timeout: 5 } ] } ]) end ))'
fi
new="$(jq --indent 2 --arg hook "$HOOK" --arg bell_re "$BELL_RE" --argjson events "$EVENTS" "$filter" <<<"$current")" \
  || { echo "install.sh: failed to merge settings" >&2; exit 1; }

# ---- tab bell (opt-in): one Notification entry per matcher, added only where none rings tab-bell.sh.
if [ "$uninstall" = 0 ] && [ "$tab_bell" = 1 ]; then
  say "  tab bell: Notification hook for $(jq -r 'join(", ")' <<<"$BELL_MATCHERS")"
  new="$(jq --indent 2 --arg bell "$BELL" --arg bell_re "$BELL_RE" --argjson matchers "$BELL_MATCHERS" '
    .hooks.Notification = ( reduce $matchers[] as $m ((.hooks.Notification // []);
      if ([ .[] | select(.matcher == $m) | .hooks[]?.command // empty | select(test($bell_re)) ]
          | length) > 0 then .
      else . + [ { matcher: $m, hooks: [ { type: "command", command: $bell, timeout: 3 } ] } ] end ))' \
    <<<"$new")" || { echo "install.sh: failed to merge the tab bell hook" >&2; exit 1; }
fi

# ---- statusLine tap (usage meter): wrap the existing statusline command, keep its other keys.
TAP="$REPO/hooks/board-statusline-tap.sh"
SL_PREV="$BOARD_HOME/statusline.prev.json"
cur_sl="$(jq -r '.statusLine.command // empty' <<<"$current")"
sl_save=0
if [ "$uninstall" = 1 ]; then
  case "$cur_sl" in
    *board-statusline-tap.sh*)
      if [ -f "$SL_PREV" ]; then
        new="$(jq --indent 2 --slurpfile p "$SL_PREV" \
          'if $p[0] == null then del(.statusLine) else .statusLine = $p[0] end' <<<"$new")"
        say "  statusLine: restore the previous setting from $SL_PREV"
      elif [ "$cur_sl" = "$TAP" ]; then
        new="$(jq --indent 2 'del(.statusLine)' <<<"$new")"
        say "  statusLine: remove the tap (there was no previous statusline)"
      else
        say "  statusLine: WARNING — $SL_PREV is missing; the tap is left in place. Edit statusLine by hand."
      fi ;;
  esac
else
  case "$cur_sl" in
    *board-statusline-tap.sh*) say "  statusLine: tap already installed" ;;
    *)
      if [ -n "$cur_sl" ]; then
        q="$(printf '%s' "$cur_sl" | sed "s/'/'\\\\''/g")"   # single-quote for sh
        sl_cmd="BOARD_STATUSLINE_NEXT='$q' $TAP"
      else
        sl_cmd="$TAP"   # no previous statusline: the tap prints nothing
      fi
      new="$(jq --indent 2 --arg c "$sl_cmd" '.statusLine = ((.statusLine // {}) + {type: "command", command: $c})' <<<"$new")"
      sl_save=1
      say "  statusLine: wrap with the usage tap (previous setting saved to $SL_PREV)" ;;
  esac
fi
old_pretty="$(jq --indent 2 . <<<"$current")"

if [ "$new" = "$old_pretty" ]; then
  say "  settings.json: no change needed"
else
  if [ "$dry" = 1 ]; then
    say "  settings.json diff:"
    diff -u --label "settings.json (current)" --label "settings.json (new)" \
      <(printf '%s\n' "$old_pretty") <(printf '%s\n' "$new") | sed 's/^/    /'
  else
    mkdir -p "$(dirname "$SETTINGS")"
    if [ "$sl_save" = 1 ]; then
      mkdir -p "$BOARD_HOME" && chmod 700 "$BOARD_HOME"
      (umask 077; jq '.statusLine // null' <<<"$current" > "$SL_PREV")
    fi
    if [ -f "$SETTINGS" ]; then
      backup="$SETTINGS.bak-$(date +%Y%m%d-%H%M%S)"
      cp -p "$SETTINGS" "$backup"
      say "  backup: $backup"
    fi
    tmp="$SETTINGS.tmp.$$"
    printf '%s\n' "$new" > "$tmp" && mv -f "$tmp" "$SETTINGS"
    say "  settings.json updated"
    if [ "$uninstall" = 1 ] && [ -f "$SL_PREV" ] && \
       ! jq -e '.statusLine.command // "" | test("board-statusline-tap\\.sh")' "$SETTINGS" >/dev/null 2>&1; then
      rm -f "$SL_PREV"
    fi
  fi
fi

if [ "$uninstall" = 0 ]; then
  say ""
  say "next: put fleet.yaml and .env in your config dir (see fleet.example.yaml), then run: board doctor"
  if [ "$tab_bell" = 1 ]; then
    # tmux defaults pass the bell through; warn only when the running server's settings do not.
    if ba="$(tmux show -gv bell-action 2>/dev/null)" && [ -n "$ba" ]; then
      vb="$(tmux show -gv visual-bell 2>/dev/null)" || vb=""
      if [ "$ba" != any ] || [ "$vb" = on ]; then
        say "tab bell: tmux has bell-action $ba, visual-bell ${vb:-?}; set 'bell-action any' and 'visual-bell off' in ~/.tmux.conf so the bell reaches the terminal"
      fi
    fi
    say "tab bell: turn on your terminal's bell indicator (Windows Terminal: add \"bellStyle\": [\"window\", \"taskbar\"] under profiles.defaults)"
  fi
fi
exit 0
