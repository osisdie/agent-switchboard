#!/usr/bin/env bash
# shellcheck disable=SC2015,SC2016  # ok/bad always return 0; jq programs are single-quoted
# tests/test_tab_bell.sh — hooks/tab-bell.sh and `scripts/install.sh --tab-bell` (no real tmux,
# no real terminal, no real settings.json).
#
# Usage: tests/test_tab_bell.sh [-v]
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1

T="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-bell.XXXXXX")"
trap 'rm -rf "$T"' EXIT

export BOARD_HOME="$T/home"
export BOARD_CONFIG_DIR="$T/config"
export CLAUDE_SETTINGS="$T/settings.json"
export BIN_DIR="$T/localbin"
unset TMUX_PANE TMUX BOARD_TAB_BELL BOARD_TAB_BELL_TTY
while IFS= read -r v; do unset "$v"; done < <(compgen -e | grep -E '^(SLACK_|SMTP_|ALERT_|LINE_|AWS_)')
export ALERT_DESKTOP=0
mkdir -p "$BOARD_CONFIG_DIR" "$T/bin"

pass=0; fail=0
ok() { pass=$((pass + 1)); [ "$VERBOSE" = 1 ] && echo "  ok   $1"; return 0; }
bad() { fail=$((fail + 1)); echo "  FAIL $1"; [ -n "${2:-}" ] && echo "       $2"; return 0; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got '$2', want '$3'"; fi; }

BELL="$REPO/hooks/tab-bell.sh"
PANE_TTY="$T/pane-tty"     # a regular file stands in for the pane's tty
FALLBACK="$T/fallback-tty" # ... and for /dev/tty
# A fake tmux: `display -p -t <pane> '#{pane_tty}'` prints $PANE_TTY for pane %7, a path that cannot
# be written for pane %8, and fails otherwise; `show -gv bell-action|visual-bell` print the contents
# of $T/bell-action and $T/visual-bell (tmux's defaults: any, off). Every call is logged.
echo any > "$T/bell-action"; echo off > "$T/visual-bell"
cat > "$T/bin/tmux" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$T/tmux.log"
case "\$*" in
  "display -p -t %7 #{pane_tty}") echo "$PANE_TTY" ;;
  "display -p -t %8 #{pane_tty}") echo "$T/gone/tty" ;;
  "show -gv bell-action") cat "$T/bell-action" ;;
  "show -gv visual-bell") cat "$T/visual-bell" ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$T/bin/tmux"
export PATH="$T/bin:$PATH"

JSON='{"session_id":"s1","hook_event_name":"Notification","notification_type":"permission_prompt","message":"Claude needs your permission"}'
bell() {  # bell [ENV=VAL ...] — run the hook with JSON on stdin; prints its output, sets rc
  : > "$PANE_TTY"; : > "$FALLBACK"
  out="$(printf '%s' "$JSON" | env BOARD_TAB_BELL_TTY="$FALLBACK" "$@" "$BELL" 2>&1)"; rc=$?
}
bytes() { od -An -tx1 "$1" | tr -d ' \n'; }

# ---------------------------------------------------------------- hook
echo "hook:"
bell TMUX_PANE=%7
eq "in tmux: exits 0" "$rc" "0"
eq "in tmux: prints nothing" "$out" ""
eq "in tmux: BEL on the pane tty" "$(bytes "$PANE_TTY")" "07"
eq "in tmux: nothing on /dev/tty" "$(bytes "$FALLBACK")" ""

bell
eq "no tmux: exits 0" "$rc" "0"
eq "no tmux: prints nothing" "$out" ""
eq "no tmux: BEL on the controlling terminal" "$(bytes "$FALLBACK")" "07"
eq "no tmux: pane tty untouched" "$(bytes "$PANE_TTY")" ""

bell TMUX_PANE=%99
eq "unknown pane: exits 0" "$rc" "0"
eq "unknown pane: falls back to /dev/tty" "$(bytes "$FALLBACK")" "07"

bell TMUX_PANE=%99 BOARD_TAB_BELL=tmux
eq "BOARD_TAB_BELL=tmux: exits 0" "$rc" "0"
eq "BOARD_TAB_BELL=tmux: no fallback" "$(bytes "$FALLBACK")" ""

bell TMUX_PANE=%7 BOARD_TAB_BELL=tmux
eq "BOARD_TAB_BELL=tmux with a pane: rings it" "$(bytes "$PANE_TTY")" "07"
eq "BOARD_TAB_BELL=tmux with a pane: no fallback" "$(bytes "$FALLBACK")" ""

for v in 0 off false no OFF; do
  bell TMUX_PANE=%7 BOARD_TAB_BELL=$v
  eq "BOARD_TAB_BELL=$v: exits 0" "$rc" "0"
  eq "BOARD_TAB_BELL=$v: pane tty untouched" "$(bytes "$PANE_TTY")" ""
  eq "BOARD_TAB_BELL=$v: /dev/tty untouched" "$(bytes "$FALLBACK")" ""
done

# TMUX_PANE set but no tmux binary: a PATH with only what the hook needs (never the real tmux).
mkdir -p "$T/notmux"
for c in bash cat timeout; do p="$(command -v "$c")" && ln -sf "$p" "$T/notmux/$c"; done
bell TMUX_PANE=%7 PATH="$T/notmux"
eq "no tmux binary: exits 0" "$rc" "0"
eq "no tmux binary: falls back to /dev/tty" "$(bytes "$FALLBACK")" "07"
eq "no tmux binary: pane tty untouched" "$(bytes "$PANE_TTY")" ""

# The real fallback with no controlling terminal: setsid drops it, so opening /dev/tty fails.
if command -v setsid >/dev/null 2>&1; then
  out="$(printf '%s' "$JSON" | env -u BOARD_TAB_BELL_TTY -u TMUX_PANE setsid -w "$BELL" 2>&1)"; rc=$?
  eq "no controlling terminal: exits 0" "$rc" "0"
  eq "no controlling terminal: prints nothing" "$out" ""
else
  echo "  (setsid not installed: no-controlling-terminal test skipped)"
fi

: > "$FALLBACK"
out="$(printf '%s' "$JSON" | BOARD_TAB_BELL_TTY="$T/no/such/dir/tty" "$BELL" 2>&1)"; rc=$?
eq "unwritable terminal: exits 0" "$rc" "0"
eq "unwritable terminal: prints nothing" "$out" ""

bell TMUX_PANE=%8
eq "unwritable pane tty: exits 0" "$rc" "0"
eq "unwritable pane tty: prints nothing" "$out" ""
eq "unwritable pane tty: falls back" "$(bytes "$FALLBACK")" "07"

out="$(TMUX_PANE=%7 BOARD_TAB_BELL_TTY="$FALLBACK" "$BELL" </dev/null 2>&1)"; rc=$?
eq "empty stdin: exits 0" "$rc" "0"
# A large payload must be drained without the writer blocking (SIGPIPE) or the hook failing.
head -c 300000 /dev/zero | tr '\0' 'x' > "$T/big.json"
rc="$( { BOARD_TAB_BELL_TTY="$FALLBACK" "$BELL" < "$T/big.json"; echo $?; } 2>&1)"
eq "large stdin: exits 0" "$rc" "0"
"$BELL" --help | grep -q 'bell-action any' && ok "--help prints the header" || bad "--help prints the header"

# ---------------------------------------------------------------- install.sh --tab-bell
echo "install --tab-bell:"
# A user's own Notification hooks: one on idle_prompt, and /other/my-tab-bell.sh already on
# permission_prompt (a name the repo's hook must not be confused with).
cat > "$CLAUDE_SETTINGS" <<'JSON'
{"model":"x","hooks":{"Notification":[
   {"matcher":"idle_prompt","hooks":[{"type":"command","command":"/other/idle.sh"}]},
   {"matcher":"permission_prompt","hooks":[{"type":"command","command":"/other/my-tab-bell.sh"}]}],
 "Stop":[{"hooks":[{"type":"command","command":"/other/hook.sh"}]}]}}
JSON
orig="$(jq -c . "$CLAUDE_SETTINGS")"
IS_BELL='.command | test("(^|/)hooks/tab-bell\\.sh($|\\s)")'
nbell() { jq "[.hooks.Notification[]? | select(any(.hooks[]; $IS_BELL))] | length" "$CLAUDE_SETTINGS"; }
user_bell() {  # the user's own my-tab-bell.sh entry, as it stands
  jq -c '[.hooks.Notification[]? | select(any(.hooks[]; .command == "/other/my-tab-bell.sh"))]' "$CLAUDE_SETTINGS"
}
user_bell_orig="$(user_bell)"

"$REPO/scripts/install.sh" --tab-bell --dry-run > "$T/dry.txt"
eq "dry-run leaves settings untouched" "$(jq -c . "$CLAUDE_SETTINGS")" "$orig"
grep -q '^    +.*hooks/tab-bell.sh' "$T/dry.txt" && ok "dry-run diff shows the tab bell" || bad "dry-run diff shows the tab bell" "$(cat "$T/dry.txt")"

"$REPO/scripts/install.sh" > /dev/null
eq "without --tab-bell: no tab bell" "$(nbell)" "0"

out="$("$REPO/scripts/install.sh" --tab-bell)"
eq "4 tab bell entries (a user's my-tab-bell.sh does not count)" "$(nbell)" "4"
eq "the 4 matchers" \
  "$(jq -c "[.hooks.Notification[] | select(any(.hooks[]; $IS_BELL)) | .matcher]" "$CLAUDE_SETTINGS")" \
  '["permission_prompt","elicitation_dialog","elicitation_url_dialog","agent_needs_input"]'
eq "entry shape: its own entry, the repo's hook, timeout 3" \
  "$(jq -c "[.hooks.Notification[] | select(any(.hooks[]; $IS_BELL)) | select(.matcher == \"permission_prompt\")][0]" "$CLAUDE_SETTINGS")" \
  "$(jq -cn --arg c "$BELL" '{matcher: "permission_prompt", hooks: [{type: "command", command: $c, timeout: 3}]}')"
eq "user's permission_prompt entry unchanged" "$(user_bell)" "$user_bell_orig"
eq "idle_prompt hook kept, no bell on idle" \
  "$(jq -c '[.hooks.Notification[] | select(.matcher == "idle_prompt") | .hooks[].command]' "$CLAUDE_SETTINGS")" '["/other/idle.sh"]'
eq "unrelated Stop hook kept" "$(jq -r '.hooks.Stop[0].hooks[0].command' "$CLAUDE_SETTINGS")" "/other/hook.sh"
eq "board-hook still on Notification" \
  "$(jq '[.hooks.Notification[].hooks[].command | select(test("board-hook\\.sh"))] | length' "$CLAUDE_SETTINGS")" "1"
grep -q 'bellStyle.*under profiles.defaults' <<<"$out" && ok "prints the terminal hint" || bad "prints the terminal hint" "$out"
grep -q 'bell-action' <<<"$out" && bad "tmux defaults (any, off): no tmux hint" "$out" || ok "tmux defaults (any, off): no tmux hint"

before="$(cat "$CLAUDE_SETTINGS")"
"$REPO/scripts/install.sh" --tab-bell > /dev/null
eq "--tab-bell twice: no change" "$(cat "$CLAUDE_SETTINGS")" "$before"
"$REPO/scripts/install.sh" > /dev/null
eq "plain re-install keeps the tab bell" "$(nbell)" "4"

echo none > "$T/bell-action"
out="$("$REPO/scripts/install.sh" --tab-bell)"
grep -q "bell-action none.*'bell-action any'" <<<"$out" && ok "bell-action none: tmux hint" || bad "bell-action none: tmux hint" "$out"
echo any > "$T/bell-action"; echo on > "$T/visual-bell"
out="$("$REPO/scripts/install.sh" --tab-bell)"
grep -q "visual-bell on.*'visual-bell off'" <<<"$out" && ok "visual-bell on: tmux hint" || bad "visual-bell on: tmux hint" "$out"
echo both > "$T/visual-bell"
out="$("$REPO/scripts/install.sh" --tab-bell)"
grep -q 'bell-action' <<<"$out" && bad "visual-bell both: no tmux hint" "$out" || ok "visual-bell both: no tmux hint"
echo off > "$T/visual-bell"

"$REPO/scripts/install.sh" --uninstall > /dev/null
eq "uninstall removes the tab bell" "$(nbell)" "0"
eq "uninstall keeps the user's my-tab-bell.sh" "$(user_bell)" "$user_bell_orig"
eq "uninstall restores the original hooks" "$(jq -c .hooks "$CLAUDE_SETTINGS")" "$(jq -c .hooks <<<"$orig")"

"$REPO/scripts/install.sh" --tab-bell > /dev/null
out="$("$REPO/scripts/install.sh" --tab-bell --uninstall)"
eq "--tab-bell --uninstall: uninstall wins" "$(nbell)" "0"
eq "--tab-bell --uninstall: original hooks" "$(jq -c .hooks "$CLAUDE_SETTINGS")" "$(jq -c .hooks <<<"$orig")"
grep -q 'tab bell' <<<"$out" && bad "--tab-bell --uninstall: no tab bell hint" "$out" || ok "--tab-bell --uninstall: no tab bell hint"

rm -f "$CLAUDE_SETTINGS"
"$REPO/scripts/install.sh" --tab-bell > /dev/null
eq "missing settings.json: created with the tab bell" "$(nbell)" "4"
eq "missing settings.json: board-hook on 10 events" \
  "$(jq '[.hooks[][] | select(.hooks[].command | test("board-hook\\.sh"))] | length' "$CLAUDE_SETTINGS")" "10"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
