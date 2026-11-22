#!/usr/bin/env bash
# shellcheck disable=SC2015,SC2016  # ok/bad always return 0; jq programs are single-quoted
# tests/test_committee.sh — committee decisions on board asks while the owner is away (lib/committee.py,
# board away decisions, board advise relay|decided, board answer's committee path, board-serve).
#
# Usage: tests/test_committee.sh [-v]
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1

T="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-committee.XXXXXX")"
cleanup() {
  # the owner's after-the-fact notify runs in the background: wait (bounded) until no process has an
  # environment entry pointing into $T (grep runs with an empty environment so it never finds itself)
  for _ in $(seq 1 100); do
    env -i "$(command -v grep)" -qsazF "$T/" /proc/[0-9]*/environ || break
    sleep 0.1
  done
  rm -rf "$T"
}
trap cleanup EXIT

export BOARD_HOME="$T/home"
export CLAUDE_JSON="$T/claude.json"
export BOARD_CONFIG_DIR="$T/config"
export CLAUDE_SESSIONS_DIR="$T/sessions"
export CLAUDE_SETTINGS="$T/settings.json"
python3 "$REPO/tests/fake_proc.py" "$T/proc" && export BOARD_PROC_ROOT="$T/proc"
unset TMUX_PANE TMUX BOARD_PUBLIC_URL BOARD_NOW_MS
while IFS= read -r v; do unset "$v"; done < <(compgen -e | grep -E '^(SLACK_|SMTP_|ALERT_|LINE_|AWS_)')
export ALERT_DESKTOP=0
mkdir -p "$BOARD_CONFIG_DIR" "$CLAUDE_SESSIONS_DIR" "$T/bin" "$BOARD_HOME"
cat > "$BOARD_CONFIG_DIR/fleet.yaml" <<'EOF'
sessions:
  - {code: API, cwd: /a}
  - {code: WEB, cwd: /w, aliases: [web-main]}
  - {code: QA, cwd: /q}
  - {code: OPS, cwd: /o}
approvers: [local]
EOF

pass=0; fail=0
ok() { pass=$((pass + 1)); [ "$VERBOSE" = 1 ] && echo "  ok   $1"; return 0; }
bad() { fail=$((fail + 1)); echo "  FAIL $1"; [ -n "${2:-}" ] && echo "       $2"; return 0; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got '$2', want '$3'"; fi; }

B="$REPO/bin/board"
df() { jq -r "$2" "$BOARD_HOME/decisions/$1.json"; }
af() { jq -r "$2" "$BOARD_HOME/advice/$1.json"; }
lastev() { jq -s -r "[.[] | select(.type == \"$1\")] | last | $2" "$BOARD_HOME/events.jsonl"; }
jq -n '{generated_at: 1, sessions: (["API", "WEB", "QA", "OPS"] | map({code: ., state: "idle"}))}' > "$BOARD_HOME/snapshot.json"
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
case "$1" in display-message) echo "$FAKE_TMUX_SESSION" ;; esac
EOF
chmod +x "$T/bin/tmux"
RELAY_RE='^\[BOARD decision (d-[0-9]+-[0-9a-f]+)\] (.+) \(by committee consensus while owner away: [A-Za-z0-9][A-Za-z0-9_.-]*(, [A-Za-z0-9][A-Za-z0-9_.-]*)+(; red line(: production gate)?)?\)$'

# ---------------------------------------------------------------- the owner's setting
echo "setting:"
out="$("$B" away decisions status)"; rc=$?
eq "status exits 0" "$rc" "0"
grep -q "the committee decides when every session asked agrees; red lines and production gates included" <<<"$out" \
  && ok "  default: committee, red lines included" || bad "  default: committee" "$out"
eq "status --json" "$("$B" away decisions status --json | jq -c '{mode, red_lines, invalid}')" \
  '{"mode":"committee","red_lines":"committee","invalid":false}'
"$B" away decisions >/dev/null 2>&1; eq "no subcommand: usage (exit 2)" "$?" "2"
"$B" away decisions maybe --by local >/dev/null 2>&1; eq "an unknown mode: exit 2" "$?" "2"
"$B" away decisions committee >/dev/null 2>&1; eq "no --by: exit 2" "$?" "2"
"$B" away decisions committee --red-lines sometimes --by local >/dev/null 2>&1; eq "a bad --red-lines: exit 2" "$?" "2"
"$B" away decisions owner_final --by 'not ok' >/dev/null 2>&1; eq "a bad --by: exit 2" "$?" "2"
[ -e "$BOARD_HOME/away-decisions.json" ] && bad "  refusals write nothing" || ok "  refusals write nothing"
eq "away decisions -h" "$("$B" away decisions -h | head -c 26)" "usage: board away decision"
"$B" away decisions owner_final --by local >/dev/null; eq "owner_final is saved" "$(jq -r .mode "$BOARD_HOME/away-decisions.json")" "owner_final"
eq "  0600" "$(stat -c %a "$BOARD_HOME/away-decisions.json")" "600"
eq "  logged" "$(lastev away_decisions '.mode + "/" + .by + "/" + .previous.mode')" "owner_final/local/committee"
# a board session never loosens it (inside tmux); tightening always works
TMUX=fake FAKE_TMUX_SESSION=web-main PATH="$T/bin:$PATH" "$B" away decisions committee --by WEB >/dev/null 2>&1; rc=$?
eq "loosening from a board session (an alias too): exit 4" "$rc" "4"
eq "  unchanged and logged" "$(jq -r .mode "$BOARD_HOME/away-decisions.json")/$(lastev away_decisions '.result + "/" + .tmux_session')" \
  "owner_final/refused/web-main"
TMUX=fake FAKE_TMUX_SESSION=BOARD PATH="$T/bin:$PATH" "$B" away decisions committee --by BOARD >/dev/null 2>&1; rc=$?
eq "  from the operator's session too" "$rc" "4"
TMUX=fake FAKE_TMUX_SESSION=owner-shell PATH="$T/bin:$PATH" "$B" away decisions committee --red-lines owner --by local >/dev/null 2>&1
eq "from a shell that is not a board session: allowed" "$(jq -r '.mode + "/" + .red_lines' "$BOARD_HOME/away-decisions.json")" "committee/owner"
TMUX=fake FAKE_TMUX_SESSION=QA PATH="$T/bin:$PATH" "$B" away decisions committee --red-lines committee --by QA >/dev/null 2>&1; rc=$?
eq "red lines back to the committee from a board session: exit 4" "$rc" "4"
TMUX=fake FAKE_TMUX_SESSION=QA PATH="$T/bin:$PATH" "$B" away decisions owner_final --by QA >/dev/null 2>&1; rc=$?
eq "tightening from a board session works" "$rc/$(jq -r .mode "$BOARD_HOME/away-decisions.json")" "0/owner_final"
echo '{"mode": "everything"}' > "$BOARD_HOME/away-decisions.json"
eq "an invalid file reads as owner_final (fails closed)" "$("$B" away decisions status --json | jq -c '{mode, red_lines, invalid}')" \
  '{"mode":"owner_final","red_lines":"owner","invalid":true}'
"$B" away decisions committee --by local >/dev/null
eq "setting it again clears that (red lines keep the safe value until set)" "$("$B" away decisions status --json | jq -c '{mode, red_lines, invalid}')" \
  '{"mode":"committee","red_lines":"owner","invalid":false}'
"$B" away decisions committee --red-lines committee --by local >/dev/null

# ---------------------------------------------------------------- deciding
echo "deciding:"
"$B" away on --by OPS --said "away until Monday" >/dev/null 2>&1
grep -q "the committee decides when every session asked agrees" <<<"$("$B" away status)" && ok "away status says how board asks are decided" \
  || bad "away status says how board asks are decided" "$("$B" away status)"
d1="$("$B" ask --from API "Keep the old endpoint as a redirect?" --option "Keep redirect" --option "Remove it")"
out="$("$B" advise "$d1" --from WEB --to QA --option 1 "Keep it: two help pages still link to it." 2>&1)"
grep -q "it becomes the" <<<"$out" && ok "recommend says it can become the decision" || bad "recommend says it can become the decision" "$out"
out="$("$B" advise "$d1" agree --from QA 2>&1)"
eq "one agree of two: still pending" "$(df "$d1" .status)" "pending"
grep -q "not decided yet" <<<"$out" && ok "  says so" || bad "  says so" "$out"
"$B" advise "$d1" object --from OPS --note "wait" >/dev/null 2>&1
"$B" advise "$d1" agree --from API >/dev/null 2>&1
eq "an objection blocks, even with every one asked agreeing" "$(df "$d1" .status)" "pending"
out="$("$B" advise "$d1" agree --from OPS 2>&1)"; rc=$?
eq "the objector agrees: decided" "$rc/$(df "$d1" '.status + "|" + .answer + "|" + .answered_by')" "0/answered|Keep redirect|committee"
grep -q "decided by committee consensus" <<<"$out" && ok "  the agree says it decided" || bad "  the agree says it decided" "$out"
eq "  participants and stances recorded" "$(df "$d1" '[.committee.participants[]] | join(",")')/$(df "$d1" '[.committee.stances[] | .by + ":" + .stance] | join(",")')" \
  "WEB,QA,API,OPS/WEB:recommend,QA:agree,API:agree,OPS:agree"
eq "  the setting and the away period recorded" "$(df "$d1" '.committee.settings.mode + "/" + .committee.settings.red_lines + "/" + (.committee.away.since | tostring)')" \
  "committee/committee/$(jq -r .since "$BOARD_HOME/owner-away")"
eq "  the advice froze: decided" "$(af "$d1" '.status + "/" + .frozen_reason')" "frozen/decided"
eq "  advise show --json" "$("$B" advise show "$d1" --json | jq -r '.status + "/" + .frozen_reason + "/" + (.decided | tostring)')" "frozen/decided/true"
grep -q "decided by committee consensus while the owner was away" <<<"$("$B" advise show "$d1")" && ok "  advise show says decided" || bad "  advise show says decided"
eq "  events: answer by committee, committee decided, freeze" \
  "$(lastev answer '.by')/$(jq -s -r '[.[] | select(.type == "committee" and .action == "decided")] | last | .id' "$BOARD_HOME/events.jsonl")/$(lastev advice '.action + ":" + .reason')" "committee/$d1/freeze:decided"
"$B" advise "$d1" agree --from QA >/dev/null 2>&1; eq "  no more stances" "$?" "1"
eq "  no second decision event" "$(jq -s --arg d "$d1" '[.[] | select(.type == "committee" and .action == "decided" and .id == $d)] | length' "$BOARD_HOME/events.jsonl")" "1"

# the owner's answer comes too late: a committee decision stays taken (exit 9)
"$B" answer "$d1" "Remove it" --by local >/dev/null 2>&1; rc=$?
eq "the owner's later answer is refused (exit 9)" "$rc/$(df "$d1" '.answer + "|" + .answered_by')" "9/Keep redirect|committee"
eq "  logged" "$(lastev answer '.result + "/" + .reason')" "refused/decided by the committee"

# relay: the operator's, the exact line
out="$("$B" advise relay --from WEB 2>&1)"; rc=$?
eq "relay from a session that is not the operator: exit 4" "$rc" "4"
out="$("$B" advise relay --from BOARD)"
eq "relay header" "$(head -n 1 <<<"$out")" "== $d1 API"
line="$(sed -n 2p <<<"$out")"
eq "relay line" "$line" "[BOARD decision $d1] Keep redirect (by committee consensus while owner away: WEB, QA, API, OPS)"
[[ $line =~ $RELAY_RE ]] && ok "  machine-checkable (documented pattern)" || bad "  machine-checkable" "$line"
eq "relay --json" "$("$B" advise relay --from BOARD "$d1" --json | jq -r '.[0].to + "|" + (.[0].red_line | tostring)')" "API|false"
w="$(python3 "$REPO/lib/serve.py" --watch --once)"
grep -q "^decision $d1 committee$" <<<"$w" && ok "watch: decision <id> committee" || bad "watch: decision <id> committee" "$w"
grep -q "^decision $d1 answered$" <<<"$w" && bad "  never a plain answered line" "$w" || ok "  never a plain answered line"
"$B" mark "$d1" relayed >/dev/null
eq "after board mark relayed: nothing to relay" "$("$B" advise relay --from BOARD)" ""
eq "  relay ID of a relayed one: exit 1" "$("$B" advise relay --from BOARD "$d1" >/dev/null 2>&1; echo $?)" "1"
grep -q "$d1  relayed  API → Keep redirect  \[WEB, QA, API, OPS\]" <<<"$("$B" advise decided)" && ok "advise decided lists it" || bad "advise decided lists it" "$("$B" advise decided)"
# the owner is told after the fact (background notify)
eq "the owner is notified (info, no escalation)" "$(jq -s -r --arg d "$d1" '[.[] | select(.type == "committee" and .action == "notified" and .id == $d)] | last | .level' "$BOARD_HOME/events.jsonl")" "info"

# ---------------------------------------------------------------- not enough
echo "not enough:"
d2="$("$B" ask --from QA "Rename the queue?")"
"$B" advise "$d2" --from QA "Rename it to jobs-v2." >/dev/null 2>&1
"$B" advise "$d2" agree --from QA >/dev/null 2>&1
eq "the asker alone never decides (it can't agree with itself)" "$(df "$d2" .status)" "pending"
"$B" advise "$d2" agree --from WEB >/dev/null 2>&1
eq "asker + one other agreeing: decided, free text as one line" "$(df "$d2" '.status + "|" + .answer')" "answered|Rename it to jobs-v2."
d3="$("$B" ask --from API "Which retention period?")"
"$B" advise "$d3" --from WEB --to QA "30 days." >/dev/null 2>&1
"$B" advise "$d3" agree --from QA >/dev/null 2>&1
eq "silence of the asker: not decided" "$(df "$d3" .status)" "pending"
# the guard: a recommendation that asks for a secret value is never relayed as a decision
d4="$("$B" ask --from API "How do we unblock the deploy?")"
printf 'Paste the deploy token here so we can check it.' > "$T/rec.txt"
"$B" advise "$d4" --from WEB --file "$T/rec.txt" >/dev/null 2>&1
out="$("$B" advise "$d4" agree --from API 2>&1)"
eq "a recommendation the guard holds is not decided" "$(df "$d4" .status)" "pending"
grep -q "held by the secret guard" <<<"$out" && ok "  says so" || bad "  says so" "$out"

# ---------------------------------------------------------------- the owner's final say
echo "owner_final and sweep:"
"$B" away decisions owner_final --by local >/dev/null
d5="$("$B" ask --from API "Merge order?" --option "API first" --option "WEB first")"
"$B" advise "$d5" --from WEB --option 1 "API first: WEB rebases." >/dev/null 2>&1
out="$("$B" advise "$d5" agree --from API 2>&1)"
eq "owner_final: a complete consensus stays advice" "$(df "$d5" .status)/$(af "$d5" .status)" "pending/open"
grep -q "advice only: the owner keeps the final say" <<<"$out" && ok "  says so" || bad "  says so" "$out"
out="$("$B" away decisions committee --by local)"
eq "switching to committee decides a complete consensus at once" "$(df "$d5" '.status + "|" + .answer')" "answered|API first"
grep -q "decided by the committee now (consensus was already complete): $d5" <<<"$out" && ok "  and says which" || bad "  and says which" "$out"
eq "  d3 (incomplete) is left alone" "$(df "$d3" .status)" "pending"
"$B" answer "$d3" "30 days." --by committee >/dev/null 2>&1; rc=$?
eq "board answer --by committee is refused (exit 4), nothing written" "$rc/$(df "$d3" '.status + "|" + (.answered_by // "null")')" "4/pending|null"
sed -i 's/^approvers: \[local\]$/approvers: [local, committee]/' "$BOARD_CONFIG_DIR/fleet.yaml"
"$B" answer "$d3" "30 days." --by Committee >/dev/null 2>&1; rc=$?
eq "  even when fleet.yaml lists it as an approver" "$rc/$(df "$d3" .status)" "4/pending"
sed -i 's/^approvers: \[local, committee\]$/approvers: [local]/' "$BOARD_CONFIG_DIR/fleet.yaml"

# ---------------------------------------------------------------- red lines
echo "red lines:"
"$B" away decisions committee --red-lines owner --by local >/dev/null
tid="$("$B" task new --owner OPS "Rotate certificates" --stage production)"
d6="$("$B" ask --from OPS --task "$tid" --gate production "Release to production?")"
"$B" advise "$d6" --from API --option approve "Ready: the canary is green." >/dev/null 2>&1
out="$("$B" advise "$d6" agree --from OPS 2>&1)"
eq "red lines: owner — a production gate waits" "$(df "$d6" .status)" "pending"
grep -q "the owner keeps the final say" <<<"$out" && ok "  says so" || bad "  says so" "$out"
d7="$("$B" ask --from QA "Drop the old fixtures bucket?")"
"$B" advise "$d7" --from QA --red-line --to WEB "Drop it." >/dev/null 2>&1
"$B" advise "$d7" agree --from WEB >/dev/null 2>&1
eq "  a flagged red line waits too" "$(df "$d7" .status)" "pending"
"$B" away decisions committee --red-lines committee --by local >/dev/null
eq "red lines to the committee: both decided by the sweep" "$(df "$d6" '.status + "|" + .answer')/$(df "$d7" .status)" "answered|approve/answered"
eq "  the production gate: approved by committee on the task" "$(jq -r --arg d "$d6" '.gates[] | select(.decision_id == $d) | .status + "|" + .approved_by + "|" + .answered_by' "$BOARD_HOME/tasks/$tid.json")" \
  "approved|committee|committee"
eq "  the approval event" "$(lastev approval 'select(.decision_id == "'"$d6"'") | .result + "|" + .answered_by')" "approved|committee"
eq "  marked on the record" "$(df "$d6" '(.committee.production | tostring) + "|" + (.committee.red_line | tostring)')" "true|true"
out="$("$B" advise relay --from BOARD)"
grep -qx "== $d6 OPS production-gate" <<<"$out" && ok "  relay header says production gate" || bad "  relay header" "$out"
grep -qx "\[BOARD decision $d6\] approve (by committee consensus while owner away: API, OPS; red line: production gate)" <<<"$out" \
  && ok "  relay text says red line: production gate" || bad "  relay text (production)" "$out"
grep -qx "\[BOARD decision $d7\] Drop it. (by committee consensus while owner away: QA, WEB; red line)" <<<"$out" \
  && ok "  relay text says red line" || bad "  relay text (red line)" "$out"
n=0; while IFS= read -r l; do case "$l" in "[BOARD"*) [[ $l =~ $RELAY_RE ]] || n=$((n + 1)) ;; esac; done <<<"$out"
eq "  every relay line matches the documented pattern" "$n" "0"
sleep 1
eq "red lines notify the owner at high (no escalation)" "$(jq -s -r --arg d "$d6" '[.[] | select(.type == "committee" and .action == "notified" and .id == $d)] | last | .level' "$BOARD_HOME/events.jsonl")" "high"
eq "  key committee:<id>, not escalating" "$(jq -r --arg k "committee:$d6" '.[$k] | (.level + "|" + (.escalate | tostring))' "$BOARD_HOME/alerts.json" 2>/dev/null)" "high|false"

# ---------------------------------------------------------------- owner back / owner first
echo "owner back and races:"
d8="$("$B" ask --from API "Bump the fixture?")"
"$B" advise "$d8" --from WEB "Bump it." >/dev/null 2>&1
"$B" away off --by OPS >/dev/null 2>&1
"$B" advise "$d8" agree --from API >/dev/null 2>&1; rc=$?
eq "owner back before consensus: no more stances, no decision" "$rc/$(df "$d8" .status)" "1/pending"
eq "  decisions already taken stay taken" "$(df "$d5" '.status + "|" + .answered_by')" "answered|committee"
"$B" away on --by OPS --said "away again" >/dev/null 2>&1
d9="$("$B" ask --from API "Owner first?")"
"$B" advise "$d9" --from WEB "Yes." >/dev/null 2>&1
"$B" answer "$d9" "No." --by local >/dev/null 2>&1
"$B" advise "$d9" agree --from API >/dev/null 2>&1; rc=$?
eq "the owner answered first: the owner wins" "$rc/$(df "$d9" '.answer + "|" + .answered_by')" "1/No.|local"
# a final agree and an owner's answer at the same moment: exactly one wins, never a mix
mixed=0
for i in 1 2 3 4 5 6; do
  dr="$("$B" ask --from API "Race $i?")"
  "$B" advise "$dr" --from WEB "Go." >/dev/null 2>&1
  "$B" advise "$dr" agree --from API >/dev/null 2>&1 &
  "$B" answer "$dr" "Stop." --by local >/dev/null 2>&1 &
  wait
  st="$(df "$dr" '.status + "|" + .answered_by + "|" + .answer + "|" + ((.committee != null) | tostring)')"
  rec="$(af "$dr" '.frozen_reason // "open"')"
  case "$st/$rec" in
    "answered|committee|Go.|true/decided"|"answered|local|Stop.|false/open"|"answered|local|Stop.|false/answered") ;;
    *) mixed=$((mixed + 1)); bad "  race $i" "$st/$rec" ;;
  esac
done
eq "a final agree racing an owner's answer: always one clean winner" "$mixed" "0"

# ---------------------------------------------------------------- the digest and board-serve
echo "digest and board-serve:"
d10="$("$B" ask --from QA "Which image tag?")"
"$B" advise "$d10" --from QA --to OPS "Use the dated tag." >/dev/null 2>&1
later=$(( $(python3 -c 'import time; print(int(time.time() * 1000))') + 11 * 60000 ))
out="$(BOARD_NOW_MS="$later" "$B" advise digest --from BOARD OPS)"
grep -q "the recommendation becomes the decision and BOARD relays it" <<<"$out" && ok "the digest says it can become the decision" || bad "the digest wording" "$out"
st="$(python3 - "$REPO" <<'EOF'
import json, os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "lib"))
import fleet, serve
s = serve.StateBuilder(fleet.board_home()).build(fleet.load_fleet())
print(json.dumps({"ad": s["away_decisions"], "d": [d for d in s["decisions"] if d.get("committee")]}))
EOF
)"
eq "state.away_decisions (no owner fields for everyone)" "$(jq -c .ad <<<"$st")" '{"mode":"committee","red_lines":"committee"}'
eq "state decisions carry the committee block" "$(jq -r --arg d "$d6" '.d[] | select(.id == $d) | .committee | (.participants | join(",")) + "|" + (.production | tostring)' <<<"$st")" "API,OPS|true"
eq "  only on committee decisions" "$(jq -r --arg d "$d9" '[.d[] | select(.id == $d)] | length' <<<"$st")" "0"
grep -q '\[BOARD' <<<"$st" && bad "  no relay-shaped text in state" || ok "  no relay-shaped text in state"
# a hand-written committee block on an owner's answer is not a committee decision
jq '.committee = {participants: ["X", "Y"]}' "$BOARD_HOME/decisions/$d9.json" > "$T/x.json" && mv "$T/x.json" "$BOARD_HOME/decisions/$d9.json"
eq "  a committee block without answered_by committee is ignored" "$("$B" advise relay --from BOARD "$d9" --json)" "[]"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
