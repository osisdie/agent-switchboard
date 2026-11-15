#!/usr/bin/env bash
# shellcheck disable=SC2015,SC2016  # ok/bad always return 0; jq programs are single-quoted
# tests/test_away.sh — owner-away mode and consensus (board away / board consensus, the snapshot).
#
# Usage: tests/test_away.sh [-v]
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1

T="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-away.XXXXXX")"
trap 'rm -rf "$T"' EXIT

export BOARD_HOME="$T/home"
export BOARD_CONFIG_DIR="$T/config"
export CLAUDE_SESSIONS_DIR="$T/sessions"
export CLAUDE_SETTINGS="$T/settings.json"
unset TMUX_PANE
while IFS= read -r v; do unset "$v"; done < <(compgen -e | grep -E '^(SLACK_|SMTP_|ALERT_|LINE_|AWS_)')
export ALERT_DESKTOP=0
mkdir -p "$BOARD_CONFIG_DIR" "$CLAUDE_SESSIONS_DIR" "$T/bin"

pass=0; fail=0
ok() { pass=$((pass + 1)); [ "$VERBOSE" = 1 ] && echo "  ok   $1"; return 0; }
bad() { fail=$((fail + 1)); echo "  FAIL $1"; [ -n "${2:-}" ] && echo "       $2"; return 0; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got '$2', want '$3'"; fi; }

B="$REPO/bin/board"
AWF="$BOARD_HOME/owner-away"
cf() { jq -r "$2" "$BOARD_HOME/consensus/$1.json"; }
lastev() { jq -s -r "[.[] | select(.type == \"$1\")] | last | $2" "$BOARD_HOME/events.jsonl"; }

# ---------------------------------------------------------------- away
echo "away:"
"$B" away status >/dev/null; eq "status when not away exits 1" "$?" "1"
eq "status --quiet prints nothing" "$("$B" away status --quiet)" ""
eq "status --json is null" "$("$B" away status --json)" "null"
"$B" away on --by CP >/dev/null 2>&1; eq "on without --said is a usage error" "$?" "2"
"$B" away on --said "x" >/dev/null 2>&1; eq "on without --by is a usage error" "$?" "2"
"$B" away off >/dev/null 2>&1; eq "off without --by is a usage error" "$?" "2"
"$B" away sideways >/dev/null 2>&1; eq "unknown subcommand is a usage error" "$?" "2"
"$B" away on --by "$(printf 'C\033[2JP')" --said "x" >/dev/null 2>&1; eq "control characters in --by refused" "$?" "1"
[ -e "$AWF" ] && bad "refused calls write nothing" || ok "refused calls write nothing"

out="$("$B" away on --by CP --said "decide among yourselves; I'm back Monday" 2>&1)"; rc=$?
eq "on exits 0" "$rc" "0"
grep -q '^owner away since .* (by CP): "decide among yourselves' <<<"$out" && ok "on prints the record" || bad "on prints the record" "$out"
eq "record fields" "$(jq -r '[.by, .said, (.since | type)] | join("|")' "$AWF")" "CP|decide among yourselves; I'm back Monday|number"
eq "file mode 0600" "$(stat -c %a "$AWF")" "600"
eq "BOARD_HOME mode 0700" "$(stat -c %a "$BOARD_HOME")" "700"
"$B" away status >/dev/null; eq "status when away exits 0" "$?" "0"
eq "status --quiet prints nothing when away" "$("$B" away status --quiet; echo "rc=$?")" "rc=0"
eq "status --json" "$("$B" away status --json | jq -r .by)" "CP"
eq "on is logged" "$(lastev away '.action + "/" + .by + "/" + .said')" "on/CP/decide among yourselves; I'm back Monday"

since0="$(jq -r .since "$AWF")"
out="$("$B" away on --by API --said "something else" 2>&1)"; rc=$?
eq "on when already away exits 0" "$rc" "0"
grep -q "already away" <<<"$out" && grep -q "(by CP)" <<<"$out" && ok "  reports the existing record" || bad "  reports the existing record" "$out"
eq "  does not overwrite" "$(jq -r '.by + "|" + (.since | tostring)' "$AWF")" "CP|$since0"
"$B" away on --by API --said "new words" --replace >/dev/null 2>&1
eq "--replace overwrites" "$(jq -r '.by + "|" + .said' "$AWF")" "API|new words"
eq "  the replaced record is logged" "$(lastev away '.replaced.by')" "CP"

out="$("$B" away off --by API --said "I'm back" 2>&1)"; rc=$?
eq "off exits 0" "$rc" "0"
[ -e "$AWF" ] && bad "off removes the file" || ok "off removes the file"
eq "off is logged with the owner's words" "$(lastev away '.action + "/" + .by + "/" + .said')" "off/API/I'm back"
out="$("$B" away off --by API 2>&1)"; rc=$?
eq "off when not away exits 0" "$rc" "0"
grep -q "not away" <<<"$out" && ok "  says so" || bad "  says so" "$out"

# A free-text file written by hand (or by an older rule) still means "away".
printf 'owner said: decide among yourselves\n\033[31mred\033[0m\n' > "$AWF"
"$B" away status >/dev/null; eq "legacy free-text file counts as away" "$?" "0"
out="$("$B" away status)"
grep -q "free-text record" <<<"$out" && ok "  shown as a free-text record" || bad "  shown as a free-text record" "$out"
[[ $out == *$'\033'* ]] && bad "  control characters stripped on display" || ok "  control characters stripped on display"
eq "  status --json marks it legacy" "$("$B" away status --json | jq -r '.legacy')" "true"
"$B" away on --by CP --said "x" >/dev/null 2>&1
grep -q "^owner said:" "$AWF" && ok "  on does not overwrite a legacy file" || bad "  on does not overwrite a legacy file"
: > "$AWF"
"$B" away status --quiet; eq "an empty file counts as away" "$?" "0"
echo '"just a string"' > "$AWF"
"$B" away status --quiet; eq "a JSON non-object counts as away" "$?" "0"
"$B" away off --by CP >/dev/null
[ -e "$AWF" ] && bad "off removes a legacy file" || ok "off removes a legacy file"

# notify must never break the command (a webhook that fails, curl exiting non-zero).
cat > "$T/bin/curl" <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
echo "$*" >> "$FAKE_CURL_LOG"
printf '500'
exit 22
EOF
chmod +x "$T/bin/curl"
export FAKE_CURL_LOG="$T/curl.log"; : > "$FAKE_CURL_LOG"
echo 'SLACK_WEBHOOK_URL="http://example.invalid/hook"' > "$BOARD_CONFIG_DIR/.env"
PATH="$T/bin:$PATH" "$B" away on --by CP --said "notify test" >/dev/null 2>&1; rc=$?
eq "on with a failing webhook still exits 0" "$rc" "0"
[ -s "$FAKE_CURL_LOG" ] && ok "  a notify was attempted" || bad "  a notify was attempted"
[ -e "$AWF" ] && ok "  the record was written" || bad "  the record was written"
PATH="$T/bin:$PATH" "$B" away off --by CP >/dev/null 2>&1; rc=$?
eq "off with a failing webhook still exits 0" "$rc" "0"
rm -f "$BOARD_CONFIG_DIR/.env"
"$B" away on --by CP --said "no channels" >/dev/null 2>&1; eq "on with no channel configured exits 0" "$?" "0"
"$B" away off --by CP >/dev/null 2>&1; eq "off with no channel configured exits 0" "$?" "0"

# ---------------------------------------------------------------- consensus
echo "consensus:"
"$B" consensus propose --from CP --to API "x" >/dev/null 2>&1; rc=$?
eq "propose refused when the owner is not away" "$rc" "1"
[ -n "$(find "$BOARD_HOME/consensus" -name 'c-*.json' 2>/dev/null)" ] && bad "  nothing written" || ok "  nothing written"
"$B" away on --by CP --said "agree among yourselves" >/dev/null
"$B" consensus propose --from CP "x" >/dev/null 2>&1; eq "propose without --to is a usage error" "$?" "2"
"$B" consensus propose --from CP --to CP "x" >/dev/null 2>&1; eq "propose to oneself only refused" "$?" "1"
"$B" consensus propose --from CP --to 'A;B' "x" >/dev/null 2>&1; eq "bad session code refused" "$?" "1"

out="$("$B" consensus propose --from CP --to "API, INFRA,CP" --ref "PR-7" "share the fixture | v2" 2>"$T/err")"; rc=$?
eq "propose exits 0" "$rc" "0"
c1="$out"
[[ $c1 =~ ^c-[0-9]+-[a-z0-9]+$ ]] && ok "prints only the id on stdout ($c1)" || bad "prints only the id on stdout" "$out"
grep -q "production deploys" "$T/err" && grep -q "publishing externally" "$T/err" && ok "  reminds of the red lines (stderr)" \
  || bad "  reminds of the red lines (stderr)" "$(cat "$T/err")"
eq "participants: proposer first, deduped, trimmed" "$(cf "$c1" '.participants | join(",")')" "CP,API,INFRA"
eq "the proposer counts as agreeing" "$(cf "$c1" '.votes | map(.by + ":" + .vote) | join(",")')" "CP:agree"
eq "status pending, ref, away episode" "$(cf "$c1" '[.status, .ref, .away.by] | join("|")')" "pending|PR-7|CP"
eq "consensus file mode 0600" "$(stat -c %a "$BOARD_HOME/consensus/$c1.json")" "600"
eq "propose logged" "$(lastev consensus '.action + "/" + .id')" "propose/$c1"

"$B" consensus vote "$c1" agree --from NOPE >/dev/null 2>&1; eq "non-participant vote refused (exit 4)" "$?" "4"
eq "  no vote recorded" "$(cf "$c1" '.votes | length')" "1"
eq "  refusal logged" "$(lastev consensus '.result + "/" + .by')" "refused/NOPE"
"$B" consensus vote "$c1" maybe --from API >/dev/null 2>&1; eq "vote other than agree|object is a usage error" "$?" "2"
"$B" consensus vote "c-1-../x" agree --from API >/dev/null 2>&1; eq "bad id refused" "$?" "1"
"$B" consensus vote "c-1-nope" agree --from API >/dev/null 2>&1; eq "unknown id refused" "$?" "1"
"$B" consensus vote "$c1" agree --from CP >/dev/null 2>&1; eq "the proposer cannot vote agree twice" "$?" "1"
out="$("$B" consensus vote "$c1" agree --from API --note "fine by me" 2>&1)"; rc=$?
eq "participant vote exits 0" "$rc" "0"
grep -q "pending (2/3 agreed)" <<<"$out" && ok "  still pending with one silent participant" || bad "  still pending" "$out"
"$B" consensus vote "$c1" agree --from API >/dev/null 2>&1; eq "one vote each: a repeated agree refused" "$?" "1"
out="$("$B" consensus ls)"
grep -q "$c1 *pending *CP *CP:agree,API:agree,INFRA:-" <<<"$out" && ok "ls shows who agreed and who is silent" || bad "ls shows votes" "$out"
out="$("$B" consensus vote "$c1" agree --from INFRA 2>&1)"
grep -q "agreed (3/3 agreed)" <<<"$out" && ok "all participants agreed -> agreed" || bad "all agreed -> agreed" "$out"
eq "  decided_at set" "$(cf "$c1" '.decided_at > 0')" "true"
"$B" consensus vote "$c1" object --from INFRA >/dev/null 2>&1; eq "no vote after the outcome" "$?" "1"
eq "  stays agreed" "$(cf "$c1" .status)" "agreed"

md="$("$B" consensus show "$c1" --markdown)"
grep -q "^#### Owner-away consensus \`$c1\`: agreed" <<<"$md" && ok "markdown: heading with the outcome" || bad "markdown heading" "$md"
grep -qF -- "- **Ref:** PR-7" <<<"$md" && ok "markdown: ref" || bad "markdown: ref" "$md"
grep -qF -- "- **Proposal:** share the fixture | v2" <<<"$md" && ok "markdown: proposal" || bad "markdown: proposal" "$md"
eq "markdown: one row per vote, with timestamps" "$(grep -cE '^\| (CP|API|INFRA) \| agree \| [0-9]{4}-[0-9]{2}-[0-9]{2}T' <<<"$md")" "3"
grep -qF "| API | agree |" <<<"$md" && grep -qF "| fine by me |" <<<"$md" && ok "markdown: notes" || bad "markdown: notes" "$md"
grep -q "^\*\*Outcome:\*\* agreed at" <<<"$md" && ok "markdown: outcome" || bad "markdown: outcome" "$md"
grep -q "Red lines never pass by consensus" <<<"$md" && ok "markdown: red lines" || bad "markdown: red lines" "$md"
eq "show prints the JSON" "$("$B" consensus show "$c1" | jq -r .id)" "$c1"

c2="$("$B" consensus propose --from API --to CP,INFRA "rename the queue" 2>/dev/null)"
out="$("$B" consensus vote "$c2" object --from INFRA --note "breaks a consumer" 2>&1)"
grep -q "objected" <<<"$out" && ok "an objection parks it (objected)" || bad "an objection parks it" "$out"
eq "  status objected" "$(cf "$c2" .status)" "objected"
"$B" consensus vote "$c2" agree --from INFRA >/dev/null 2>&1; eq "  object -> agree refused" "$?" "1"
"$B" consensus vote "$c2" agree --from CP >/dev/null 2>&1; eq "  nobody can vote it back to agreed" "$?" "1"
grep -q "objected at .*parked for the owner" <<<"$("$B" consensus show "$c2" --markdown)" && ok "  markdown says parked for the owner" \
  || bad "  markdown says parked for the owner"
# agree -> object by the same session (changes the outcome while pending)
c3="$("$B" consensus propose --from CP --to API,INFRA "drop the cache" 2>/dev/null)"
"$B" consensus vote "$c3" agree --from API >/dev/null
"$B" consensus vote "$c3" object --from API --note "found a reader" >/dev/null 2>&1; eq "agree -> object allowed" "$?" "0"
eq "  status objected, both votes kept" "$(cf "$c3" '.status + "/" + (.votes | map(.by + ":" + .vote) | join(","))')" "objected/CP:agree,API:agree,API:object"
grep -q "INFRA | (no vote)" <<<"$("$B" consensus show "$c3" --markdown)" && ok "  markdown lists the silent participant" || bad "  markdown lists the silent participant"

# Owner back: pending items go back to the owner.
c4="$("$B" consensus propose --from CP --to API "pending one" 2>/dev/null)"
out="$("$B" away off --by CP --said "back" 2>&1)"
grep -q "1 pending consensus item(s) returned to the owner: $c4" <<<"$out" && ok "away off returns pending items" || bad "away off returns pending items" "$out"
eq "  status owner_back" "$(cf "$c4" '.status + "/" + .returned_by')" "owner_back/CP"
eq "  final items keep their outcome" "$(cf "$c1" .status)/$(cf "$c2" .status)" "agreed/objected"
eq "  off logs the returned ids" "$(lastev away '.returned | join(",")')" "$c4"
"$B" consensus vote "$c4" agree --from API >/dev/null 2>&1; eq "  no vote after owner_back" "$?" "1"
grep -q "returned to the owner at" <<<"$("$B" consensus show "$c4" --markdown)" && ok "  markdown says returned to the owner" \
  || bad "  markdown says returned to the owner"
# Removing the file by hand also returns pending items (on the next board consensus command).
"$B" away on --by CP --said "again" >/dev/null
c5="$("$B" consensus propose --from CP --to API "hand removed" 2>/dev/null)"
rm -f "$AWF"
"$B" consensus vote "$c5" agree --from API >/dev/null 2>&1; eq "vote after the file was removed by hand refused" "$?" "1"
eq "  item went back to the owner" "$(cf "$c5" .status)" "owner_back"

out="$("$B" consensus ls)"
grep -q "owner is not away" <<<"$out" && grep -q "$c1" <<<"$out" && ok "ls: away state + recently decided items" || bad "ls" "$out"
jq '.decided_at = 1 | .updated_at = 1' "$BOARD_HOME/consensus/$c1.json" > "$T/c1" && cp "$T/c1" "$BOARD_HOME/consensus/$c1.json"
grep -q "$c1" <<<"$("$B" consensus ls)" && bad "ls hides items decided over 24 h ago" || ok "ls hides items decided over 24 h ago"
grep -q "$c1" <<<"$("$B" consensus ls --all)" && ok "ls --all shows them" || bad "ls --all shows them"

# Help: free text may start with a dash, only a leading -h/--help asks for help.
eq "board away --help" "$("$B" away --help | head -1 | cut -c1-17)" "usage: board away"
eq "board consensus vote --help" "$("$B" consensus vote --help | head -1 | cut -c1-27)" "usage: board consensus vote"

# ---------------------------------------------------------------- snapshot
echo "snapshot:"
"$REPO/bin/board-collect" --once --no-notify >/dev/null 2>&1
S="$BOARD_HOME/snapshot.json"
eq "owner_away null when not away" "$(jq -c .owner_away "$S")" "null"
eq "consensus: recent items only, oldest first" "$(jq -r '[.consensus[].id] | join(",")' "$S")" "$c2,$c3,$c4,$c5"
"$B" away on --by CP --said "$(printf 'line one\nline two')" >/dev/null
c6="$("$B" consensus propose --from CP --to API "$(printf 'snap\033[2J shot')" 2>/dev/null)"
"$REPO/bin/board-collect" --once --no-notify >/dev/null 2>&1
eq "owner_away record" "$(jq -r '.owner_away | .by + "|" + .said' "$S")" "CP|line one line two"
eq "consensus item compact fields" "$(jq -r --arg id "$c6" '.consensus[] | select(.id == $id) | keys | join(",")' "$S")" \
  "created_at,decided_at,from,id,participants,proposal,ref,status,updated_at,votes"
eq "  proposal control characters stripped" "$(jq -r --arg id "$c6" '.consensus[] | select(.id == $id) | .proposal' "$S")" "snap?[2J shot"
printf 'free text\n' > "$AWF"
"$REPO/bin/board-collect" --once --no-notify >/dev/null 2>&1
eq "legacy file in the snapshot" "$(jq -r '.owner_away | "\(.legacy)|\(.said)|\(.since > 0)"' "$S")" "true|free text|true"

# ---------------------------------------------------------------- concurrency
echo "concurrency:"
"$B" away off --by CP >/dev/null 2>&1
"$B" away on --by CP --said "race" >/dev/null
c7="$("$B" consensus propose --from CP --to A1,A2,A3,A4,A5,A6 "race" 2>/dev/null)"
for i in 1 2 3 4 5 6; do "$B" consensus vote "$c7" agree --from "A$i" >/dev/null 2>&1 & done
wait
eq "parallel votes are all kept (lock)" "$(cf "$c7" '(.votes | length | tostring) + "/" + .status')" "7/agreed"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
