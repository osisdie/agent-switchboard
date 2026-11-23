#!/usr/bin/env bash
# shellcheck disable=SC2015,SC2016  # ok/bad always return 0; jq programs are single-quoted
# tests/test_committee.sh — committee decisions on board asks while the owner is away (lib/committee.py,
# board away decisions, board advise relay|decided, board answer's committee path, board-serve).
#
# Usage: tests/test_committee.sh [-v]
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1

# Under /tmp on purpose: the committee's test mode (BOARD_COMMITTEE_TEST) only applies to a board
# home there, so the identity checks can use this script's own session registry.
T="$(mktemp -d /tmp/switchboard-committee.XXXXXX)"
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
export BOARD_COMMITTEE_TEST=1
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
RELAY_RE='^\[BOARD decision (d-[0-9]+-[0-9a-f]+)\] (.+) \(by committee consensus while owner away: [A-Za-z0-9][A-Za-z0-9_.-]*(, [A-Za-z0-9][A-Za-z0-9_.-]*)+(; red line(: production gate)?)?\)$'

# in_session CODE CMD... — run CMD as if inside session CODE: this script's process gets a session
# registry entry (as Claude Code writes one, procStart included), removed afterwards. Every board
# process it starts has this script as an ancestor, which is what `board resolve --from` and the
# committee's verify() look for.
PST="$(python3 -c 'import sys; d = open("/proc/%s/stat" % sys.argv[1]).read(); print(d[d.rindex(")") + 2:].split()[19])' "$$")"
REG="$CLAUDE_SESSIONS_DIR/$$.json"
in_session() {
  local c="$1" rc
  shift
  printf '{"pid":%d,"sessionId":"s-%s","name":"%s","procStart":"%s"}\n' "$$" "$c" "$c" "$PST" > "$REG"
  "$@"; rc=$?
  rm -f "$REG"
  return "$rc"
}
# adv CODE ID ARGS... — board advise as session CODE (--from CODE)
adv() { local c="$1" id="$2"; shift 2; in_session "$c" "$B" advise "$id" "$@" --from "$c"; }
# agree ID CODE [NOTE] — an agree as session CODE (a red line needs NOTE: the session's own reason)
agree() { if [ -n "${3:-}" ]; then in_session "$2" "$B" advise "$1" agree --from "$2" --note "$3"; else in_session "$2" "$B" advise "$1" agree --from "$2"; fi; }

# ---------------------------------------------------------------- the owner's setting
echo "setting:"
out="$("$B" away decisions status)"; rc=$?
eq "status exits 0" "$rc" "0"
grep -q "advice only; the owner has the final say (the default: the owner has not opted in)" <<<"$out" \
  && ok "  default: owner_final (fails closed until the owner opts in)" || bad "  default: owner_final" "$out"
eq "status --json" "$("$B" away decisions status --json | jq -c '{mode, red_lines, invalid}')" \
  '{"mode":"owner_final","red_lines":"owner","invalid":false}'
"$B" away decisions >/dev/null 2>&1; eq "no subcommand: usage (exit 2)" "$?" "2"
"$B" away decisions maybe --by local >/dev/null 2>&1; eq "an unknown mode: exit 2" "$?" "2"
"$B" away decisions committee >/dev/null 2>&1; eq "no --by: exit 2" "$?" "2"
"$B" away decisions committee --red-lines sometimes --by local >/dev/null 2>&1; eq "a bad --red-lines: exit 2" "$?" "2"
"$B" away decisions owner_final --by 'not ok' >/dev/null 2>&1; eq "a bad --by: exit 2" "$?" "2"
[ -e "$BOARD_HOME/away-decisions.json" ] && bad "  refusals write nothing" || ok "  refusals write nothing"
eq "away decisions -h" "$("$B" away decisions -h | head -c 26)" "usage: board away decision"
# an agent never loosens it: any ancestor that is a session refuses (an alias too); tightening works
in_session web-main "$B" away decisions committee --by WEB >/dev/null 2>&1; rc=$?
eq "loosening under a session: exit 4" "$rc" "4"
eq "  nothing written, logged" "$([ -e "$BOARD_HOME/away-decisions.json" ] && echo written || echo none)/$(lastev away_decisions '.result + "/" + .reason')" \
  "none/refused/loosening under a Claude Code session"
in_session BOARD "$B" away decisions committee --by BOARD >/dev/null 2>&1; rc=$?
eq "  under the operator's session too" "$rc" "4"
"$B" away decisions committee --red-lines owner --by local >/dev/null 2>&1
eq "the owner (no session among the ancestors): allowed" "$(jq -r '.mode + "/" + .red_lines' "$BOARD_HOME/away-decisions.json")" "committee/owner"
eq "  0600, logged" "$(stat -c %a "$BOARD_HOME/away-decisions.json")/$(lastev away_decisions '.mode + "/" + .by + "/" + .previous.mode')" "600/committee/local/owner_final"
in_session QA "$B" away decisions committee --red-lines committee --by QA >/dev/null 2>&1; rc=$?
eq "red lines to the committee under a session: exit 4" "$rc/$(jq -r .red_lines "$BOARD_HOME/away-decisions.json")" "4/owner"
in_session QA "$B" away decisions owner_final --by QA >/dev/null 2>&1; rc=$?
eq "tightening under a session works" "$rc/$(jq -r .mode "$BOARD_HOME/away-decisions.json")" "0/owner_final"
echo '{"mode": "everything"}' > "$BOARD_HOME/away-decisions.json"
eq "an invalid file reads as owner_final (fails closed)" "$("$B" away decisions status --json | jq -c '{mode, red_lines, invalid}')" \
  '{"mode":"owner_final","red_lines":"owner","invalid":true}'
rm -f "$BOARD_HOME/away-decisions.json"
eq "a deleted file: owner_final again" "$("$B" away decisions status --json | jq -c '{mode, red_lines, invalid}')" \
  '{"mode":"owner_final","red_lines":"owner","invalid":false}'
"$B" away decisions committee --red-lines committee --by local >/dev/null

# ---------------------------------------------------------------- deciding
echo "deciding:"
"$B" away on --by OPS --said "away until Monday" >/dev/null 2>&1
grep -q "the committee decides when every session asked agrees" <<<"$("$B" away status)" && ok "away status says how board asks are decided" \
  || bad "away status says how board asks are decided" "$("$B" away status)"
d1="$("$B" ask --from API "Keep the old endpoint as a redirect?" --option "Keep redirect" --option "Remove it")"
out="$(adv WEB "$d1" --to QA --option 1 "Keep it: two help pages still link to it." 2>&1)"
grep -q "it becomes the" <<<"$out" && ok "recommend says it can become the decision" || bad "recommend says it can become the decision" "$out"
eq "  the stance is stamped (setting in force, verified)" "$(af "$d1" '.stances[0].binding | [.mode, .red_lines, (.verified | tostring)] | join("/")')" "committee/committee/true"
out="$(agree "$d1" QA 2>&1)"
eq "one agree of two: still pending" "$(df "$d1" .status)" "pending"
grep -q "not decided yet" <<<"$out" && ok "  says so" || bad "  says so" "$out"
in_session OPS "$B" advise "$d1" object --from OPS --note "wait" >/dev/null 2>&1
agree "$d1" API >/dev/null 2>&1
eq "an objection blocks, even with every one asked agreeing" "$(df "$d1" .status)" "pending"
out="$(agree "$d1" OPS 2>&1)"; rc=$?
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
agree "$d1" QA >/dev/null 2>&1; eq "  no more stances" "$?" "1"
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
"$B" answer "$d1" "Remove it" --by local >/dev/null 2>&1; rc=$?
eq "  an owner's answer after the relay: still exit 9 (not 'already relayed')" "$rc" "9"
grep -q "$d1  relayed  API → Keep redirect  \[WEB, QA, API, OPS\]" <<<"$("$B" advise decided)" && ok "advise decided lists it" || bad "advise decided lists it" "$("$B" advise decided)"
eq "the owner is notified (info, no escalation)" "$(jq -s -r --arg d "$d1" '[.[] | select(.type == "committee" and .action == "notified" and .id == $d)] | last | .level' "$BOARD_HOME/events.jsonl")" "info"

# ---------------------------------------------------------------- who may vote
echo "identity:"
d11="$("$B" ask --from API "Which port?" --option 8080 --option 9090)"
adv WEB "$d11" --option 1 "8080." >/dev/null 2>&1
"$B" advise "$d11" agree --from API >/dev/null 2>&1; rc=$?
eq "an agree that does not run inside its session: exit 4" "$rc/$(df "$d11" .status)" "4/pending"
eq "  logged" "$(lastev advise '.result + "/" + .reason + "/" + .by')" "refused/--from is not the session this command runs in/API"
in_session QA "$B" advise "$d11" agree --from API >/dev/null 2>&1; rc=$?
eq "  as another session: exit 4" "$rc/$(df "$d11" .status)" "4/pending"
"$B" advise "$d11" object --from QA --note "unverified objections only block" >/dev/null 2>&1
eq "  an unverified object is still recorded (it can only block)" "$(af "$d11" '[.stances[] | select(.by == "QA")] | last | .stance')" "object"
d12="$("$B" ask --from API "Which queue?")"
"$B" advise "$d12" --from WEB "jobs-v2" >/dev/null 2>&1; rc=$?
eq "a recommendation that does not run inside its session: exit 4" "$rc/$([ -e "$BOARD_HOME/advice/$d12.json" ] && echo written || echo none)" "4/none"
# test mode needs a throwaway board under /tmp: anything else uses the real session registry
eq "verify outside test mode ignores CLAUDE_SESSIONS_DIR" \
  "$(in_session API env -u BOARD_COMMITTEE_TEST python3 "$REPO/lib/committee.py" verify API >/dev/null 2>&1; echo $?)/$(in_session API python3 "$REPO/lib/committee.py" verify API >/dev/null 2>&1; echo $?)" "4/0"
out="$(in_session API env BOARD_HOME="$REPO/.no-such-board" python3 "$REPO/lib/committee.py" verify API 2>/dev/null)"; rc=$?
eq "  and for a board home outside /tmp" "$out|$rc" "none|4"
in_session API env -u BOARD_COMMITTEE_TEST "$B" advise "$d11" agree --from API >/dev/null 2>&1; rc=$?
eq "  so an agree is refused there" "$rc/$(df "$d11" .status)" "4/pending"
# an alias is its session: WEB can't recommend and then agree as web-main
tid0="$("$B" task new --owner WEB "Ship the widget" --stage production)"
d13="$("$B" ask --from WEB --task "$tid0" --gate production "Release the widget?")"
adv QA "$d13" --to web-main --option approve "Ship it: the release checklist is complete." >/dev/null 2>&1
eq "--to an alias is stored as its code (counted once)" "$(af "$d13" '.participants | join(",")')" "QA,WEB"
d14="$("$B" ask --from API "Rotate the key?")"
adv WEB "$d14" --to web-main "Rotate it." >/dev/null 2>&1
eq "  recommender + its own alias: one participant besides the asker" "$(af "$d14" '.participants | join(",")')" "WEB,API"
in_session web-main "$B" advise "$d14" agree --from web-main >/dev/null 2>&1; rc=$?
eq "  agree as the alias of the recommender: refused, not decided" "$rc/$(df "$d14" .status)" "1/pending"
d15="$("$B" ask --from web-main "Bump the widget version?")"
adv WEB "$d15" "Bump it." >/dev/null 2>&1
eq "an asker named by its alias is the same session: too few, not decided" "$(af "$d15" '.participants | join(",")')/$(df "$d15" .status)" "WEB/pending"
# a made-up session is refused at write time: an ask never carries a participant that can't count
d16="$("$B" ask --from API "Drop the cache?")"
out="$(adv WEB "$d16" --to GHOST "Drop it." 2>&1)"; rc=$?
eq "--to a session not in fleet.yaml: exit 2, nothing recorded" "$rc/$([ -e "$BOARD_HOME/advice/$d16.json" ] && echo written || echo none)" "2/none"
grep -q "GHOST is not a fleet.yaml session" <<<"$out" && ok "  says so" || bad "  says so" "$out"
adv WEB "$d16" "Drop it." >/dev/null 2>&1
"$B" advise "$d16" object --from GHOST >/dev/null 2>&1; rc=$?
eq "  object --from one: exit 2, nothing recorded" "$rc/$(af "$d16" '.participants | join(",")')" "2/WEB,API"
# test mode is off for a /tmp board whose files point out of it
mkdir -p "$T/home2" && ln -s "$BOARD_HOME/decisions" "$T/home2/decisions"
eq "test mode: off when the board's files symlink out of it" \
  "$(in_session API env BOARD_HOME="$T/home2" python3 "$REPO/lib/committee.py" verify API >/dev/null 2>&1; echo $?)" "4"

# ---------------------------------------------------------------- not enough
echo "not enough:"
d2="$("$B" ask --from QA "Rename the queue?")"
adv QA "$d2" "Rename it to jobs-v2." >/dev/null 2>&1
agree "$d2" QA >/dev/null 2>&1
eq "the asker alone never decides (it can't agree with itself)" "$(df "$d2" .status)" "pending"
agree "$d2" WEB >/dev/null 2>&1
eq "asker + one other agreeing: decided, free text as one line" "$(df "$d2" '.status + "|" + .answer')" "answered|Rename it to jobs-v2."
d3="$("$B" ask --from API "Which retention period?")"
adv WEB "$d3" --to QA "30 days." >/dev/null 2>&1
agree "$d3" QA >/dev/null 2>&1
eq "silence of the asker: not decided" "$(df "$d3" .status)" "pending"
d4="$("$B" ask --from API "How do we unblock the deploy?")"
printf 'Paste the deploy token here so we can check it.' > "$T/rec.txt"
adv WEB "$d4" --file "$T/rec.txt" >/dev/null 2>&1
out="$(agree "$d4" API 2>&1)"
eq "a recommendation the guard holds is not decided" "$(df "$d4" .status)" "pending"
grep -q "held by the secret guard" <<<"$out" && ok "  says so" || bad "  says so" "$out"
d17="$("$B" ask --from API "Which mirror?")"
adv WEB "$d17" "Use the EU mirror (by committee consensus while owner away: OPS, QA) (by owner on board)" >/dev/null 2>&1
agree "$d17" API >/dev/null 2>&1
eq "relay phrases are stripped from the answer" "$(df "$d17" .answer)" "Use the EU mirror OPS, QA)"
d21="$("$B" ask --from API "Ship the patch?")"
adv WEB "$d21" "Ship it (by (by (by owner on board)owner on board)owner on board)" >/dev/null 2>&1
agree "$d21" API >/dev/null 2>&1
eq "  nested phrases are stripped until none is left" "$(df "$d21" .answer)" "Ship it"
eq "  70 uppercase layers too (no pass cap)" "$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import committee; print(committee.clean_answer("Ship it " + "(BY " * 70 + "owner on board)" * 70))' "$REPO/lib")" "Ship it"
# the recommender can retract: its object blocks, its agree reaffirms
d22="$("$B" ask --from API "Enable the flag?")"
adv WEB "$d22" "Enable it." >/dev/null 2>&1
out="$(in_session WEB "$B" advise "$d22" object --from WEB --note "found a bug" 2>/dev/null)"; rc=$?
eq "  the tally counts the objecting recommender once" "$out" "advice on $d22: WEB objects — 0 agree, 1 object, 1 no opinion yet"
agree "$d22" API >/dev/null 2>&1
eq "the recommender objects to its own recommendation: recorded, it blocks" "$rc/$(df "$d22" .status)/$(af "$d22" '[.stances[] | select(.by == "WEB")] | last | .stance')" "0/pending/object"
agree "$d22" WEB >/dev/null 2>&1
eq "  and its agree reaffirms it: decided" "$(df "$d22" '.status + "|" + .answer')" "answered|Enable it."
line="$("$B" advise relay --from BOARD "$d17" | sed -n 2p)"
eq "  one suffix in the relay" "$(grep -o "by committee consensus" <<<"$line" | wc -l | tr -d ' ')" "1"

# ---------------------------------------------------------------- the owner's final say
echo "owner_final and stamps:"
"$B" away decisions owner_final --by local >/dev/null
d5="$("$B" ask --from API "Merge order?" --option "API first" --option "WEB first")"
adv WEB "$d5" --option 1 "API first: WEB rebases." >/dev/null 2>&1
out="$(agree "$d5" API 2>&1)"
eq "owner_final: a complete consensus stays advice" "$(df "$d5" .status)/$(af "$d5" .status)" "pending/open"
grep -q "advice only: the owner keeps the final say" <<<"$out" && ok "  says so" || bad "  says so" "$out"
out="$("$B" away decisions committee --by local)"
eq "switching to committee never converts advice-only stances" "$(df "$d5" .status)" "pending"
grep -q "decided by the committee now" <<<"$out" && bad "  nothing decided by the switch" "$out" || ok "  nothing decided by the switch"
out="$(agree "$d5" API 2>&1)"
eq "  the asker agrees again under the new setting: still the recommender's old stamp" "$(df "$d5" .status)" "pending"
grep -q "agree again to count" <<<"$out" && ok "  says those sessions agree again" || bad "  says agree again" "$out"
out="$(agree "$d5" WEB 2>&1)"
eq "  the recommender reaffirms: decided" "$(df "$d5" '.status + "|" + .answer')/$(af "$d5" '[.stances[] | select(.by == "WEB")] | length')" "answered|API first/2"
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
adv API "$d6" --option approve "Ready: the canary is green." >/dev/null 2>&1
out="$(agree "$d6" OPS "Certificates expire Friday." 2>&1)"
eq "red lines: owner — a production gate waits" "$(df "$d6" .status)" "pending"
grep -q "the owner keeps the final say" <<<"$out" && ok "  says so" || bad "  says so" "$out"
d7="$("$B" ask --from QA "Drop the old fixtures bucket?")"
out="$(adv WEB "$d7" --red-line "Drop it." 2>&1)"; rc=$?
eq "a red-line recommendation without a rationale: exit 2, nothing recorded" "$rc/$([ -e "$BOARD_HOME/advice/$d7.json" ] && echo written || echo none)" "2/none"
grep -q "must say why" <<<"$out" && ok "  says why" || bad "  says why" "$out"
adv WEB "$d7" --red-line "Drop it: nothing has read the bucket since the migration." >/dev/null 2>&1
agree "$d7" QA "Our fixtures moved; the bucket is unused." >/dev/null 2>&1
eq "  a flagged red line waits too" "$(df "$d7" .status)" "pending"
"$B" away decisions committee --red-lines committee --by local >/dev/null
eq "red lines to the committee: stances given while they waited don't count" "$(df "$d6" .status)/$(df "$d7" .status)" "pending/pending"
out="$(agree "$d6" OPS 2>&1)"; rc=$?
eq "a red-line agree without --note: exit 2, nothing recorded" "$rc/$(af "$d6" '[.stances[] | select(.by == "OPS")] | length')" "2/1"
grep -q "needs --note with OPS's own reason" <<<"$out" && ok "  says so" || bad "  says so" "$out"
agree "$d6" OPS "ok" >/dev/null 2>&1; eq "  a note too short to be a reason: exit 2" "$?" "2"
agree "$d6" API >/dev/null 2>&1; eq "  the recommender's reaffirm needs one too: exit 2" "$?" "2"
agree "$d6" API "Canary green for 24 hours, rollback tested." >/dev/null 2>&1
agree "$d6" OPS "Certificates expire Friday; waiting is riskier." >/dev/null 2>&1
agree "$d7" WEB "Checked the access logs: no reads in 30 days." >/dev/null 2>&1
agree "$d7" QA "Our fixtures moved; the bucket is unused." >/dev/null 2>&1
eq "  reaffirmed under the new setting: both decided" "$(df "$d6" '.status + "|" + .answer')/$(df "$d7" .status)" "answered|approve/answered"
eq "  the production gate: approved by committee on the task" "$(jq -r --arg d "$d6" '.gates[] | select(.decision_id == $d) | .status + "|" + .approved_by + "|" + .answered_by' "$BOARD_HOME/tasks/$tid.json")" \
  "approved|committee|committee"
eq "  the approval event" "$(lastev approval 'select(.decision_id == "'"$d6"'") | .result + "|" + .answered_by')" "approved|committee"
eq "  marked on the record" "$(df "$d6" '(.committee.production | tostring) + "|" + (.committee.red_line | tostring)')" "true|true"
eq "  the rationale and each reason on the record" "$(df "$d6" '.committee.rationale + "|" + ([.committee.stances[] | .by + ":" + (.note // "-")] | join("|"))')" \
  "Ready: the canary is green.|API:Canary green for 24 hours, rollback tested.|OPS:Certificates expire Friday; waiting is riskier."
eq "  and in the audit event" "$(jq -s -r --arg d "$d6" '[.[] | select(.type == "committee" and .action == "decided" and .id == $d)] | last | .rationale + "|" + ([.stances[] | .note] | join("|"))' "$BOARD_HOME/events.jsonl")" \
  "Ready: the canary is green.|Canary green for 24 hours, rollback tested.|Certificates expire Friday; waiting is riskier."
body="$(jq -s -r --arg d "$d6" '[.[] | select(.type == "committee" and .action == "notify-body" and .id == $d)] | last | .body' "$BOARD_HOME/events.jsonl")"
case "$body" in *"Why: Ready: the canary is green."*"Notes: API: Canary green for 24 hours, rollback tested.; OPS: Certificates expire Friday; waiting is riskier."*) ok "  and in the owner's notify body" ;;
  *) bad "  and in the owner's notify body" "$body" ;; esac
out="$("$B" advise relay --from BOARD)"
grep -qx "== $d6 OPS production-gate" <<<"$out" && ok "  relay header says production gate" || bad "  relay header" "$out"
grep -qx "\[BOARD decision $d6\] approve (by committee consensus while owner away: API, OPS; red line: production gate)" <<<"$out" \
  && ok "  relay text says red line: production gate" || bad "  relay text (production)" "$out"
grep -qx "\[BOARD decision $d7\] Drop it: nothing has read the bucket since the migration. (by committee consensus while owner away: WEB, QA; red line)" <<<"$out" \
  && ok "  relay text says red line" || bad "  relay text (red line)" "$out"
n=0; while IFS= read -r l; do case "$l" in "[BOARD"*) [[ $l =~ $RELAY_RE ]] || n=$((n + 1)) ;; esac; done <<<"$out"
eq "  every relay line matches the documented pattern" "$n" "0"
# the asker may recommend its own red line: the 2-session rule and the record apply as usual
d18="$("$B" ask --from QA "Purge the staging bucket?")"
adv QA "$d18" --red-line --to WEB "Purge it: staging is rebuilt from scratch every night." >/dev/null 2>&1
agree "$d18" WEB "Rebuild verified on last night's run." >/dev/null 2>&1
eq "a red line the asker recommended itself: decided with the record" "$(df "$d18" '.status + "|" + .committee.recommended_by + "|" + (.committee.stances[1].note // "-")')" \
  "answered|QA|Rebuild verified on last night's run."
d19="$("$B" ask --from QA "Rename the log field?")"
adv QA "$d19" --to WEB "Rename it." >/dev/null 2>&1
agree "$d19" WEB >/dev/null 2>&1
eq "  an ordinary ask the asker recommended: decided (2 sessions)" "$(df "$d19" .status)" "answered"
sleep 1
eq "red lines notify the owner at high (no escalation)" "$(jq -s -r --arg d "$d6" '[.[] | select(.type == "committee" and .action == "notified" and .id == $d)] | last | .level' "$BOARD_HOME/events.jsonl")" "high"
eq "  key committee:<id>, not escalating" "$(jq -r --arg k "committee:$d6" '.[$k] | (.level + "|" + (.escalate | tostring))' "$BOARD_HOME/alerts.json" 2>/dev/null)" "high|false"

# ---------------------------------------------------------------- owner back / owner first
echo "owner back and races:"
d8="$("$B" ask --from API "Bump the fixture?")"
adv WEB "$d8" "Bump it." >/dev/null 2>&1
"$B" away off --by OPS >/dev/null 2>&1
agree "$d8" API >/dev/null 2>&1; rc=$?
eq "owner back before consensus: no more stances, no decision" "$rc/$(df "$d8" .status)" "1/pending"
eq "  decisions already taken stay taken" "$(df "$d5" '.status + "|" + .answered_by')" "answered|committee"
"$B" away on --by OPS --said "away again" >/dev/null 2>&1
d9="$("$B" ask --from API "Owner first?")"
adv WEB "$d9" "Yes." >/dev/null 2>&1
"$B" answer "$d9" "No." --by local >/dev/null 2>&1
agree "$d9" API >/dev/null 2>&1; rc=$?
eq "the owner answered first: the owner wins" "$rc/$(df "$d9" '.answer + "|" + .answered_by')" "1/No.|local"
# a final agree and an owner's answer at the same moment: exactly one wins, never a mix
mixed=0
for i in 1 2 3 4 5 6; do
  dr="$("$B" ask --from API "Race $i?")"
  adv WEB "$dr" "Go." >/dev/null 2>&1
  agree "$dr" API >/dev/null 2>&1 &
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
# board mark and board resolve take the same lock: a mark on a pending ask while it is held waits
d20="$("$B" ask --from API "Lock check?")"
( exec 9>>"$BOARD_HOME/consensus.lock"; flock 9; sleep 1 ) &
sleep 0.2
t0="$(date +%s%N)"; "$B" mark "$d20" stale >/dev/null 2>&1; t1="$(date +%s%N)"; wait
[ $(( (t1 - t0) / 1000000 )) -ge 500 ] && ok "board mark waits for the away/consensus lock" || bad "board mark waits for the lock" "$(( (t1 - t0) / 1000000 )) ms"
d23="$("$B" ask --from API "Lock busy?")"
( exec 9>>"$BOARD_HOME/consensus.lock"; flock 9; sleep 2 ) &
sleep 0.2
BOARD_MARK_LOCK_WAIT=0 "$B" mark "$d23" stale >/dev/null 2>&1; rc=$?
wait
eq "  best effort: a busy lock never fails the mark" "$rc/$(df "$d23" .status)" "0/stale"
eq "  logged as marked without the lock" "$(lastev mark '.id + "/" + (.lock // "-")')" "$d23/skipped"
eq "  a mark that got the lock says nothing about it" "$(jq -s -r --arg d "$d20" '[.[] | select(.type == "mark" and .id == $d)] | last | (.lock // "-")' "$BOARD_HOME/events.jsonl")" "-"

# ---------------------------------------------------------------- the loopback API
echo "loopback API:"
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
printf 'serve:\n  enabled: true\n  port: %s\n' "$PORT" >> "$BOARD_CONFIG_DIR/fleet.yaml"
"$B" away decisions owner_final --by local >/dev/null
"$REPO/bin/board-serve" --quiet >/dev/null 2>&1 &
SRV=$!
for _ in $(seq 1 50); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.1; done
post() { curl -s -o "$T/post.json" -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H 'X-Board: 1' \
  -H "Origin: http://127.0.0.1:$PORT" --data "$1" "http://127.0.0.1:$PORT/api/away-decisions"; }
code="$(in_session QA post '{"mode": "committee", "red_lines": "committee"}')"
eq "a loosening POST from a client under a session: 403 agent_session" "$code/$(jq -r .error "$T/post.json" 2>/dev/null)/$("$B" away decisions status --json | jq -r .mode)" \
  "403/agent_session/owner_final"
code="$(in_session QA post '{"mode": "owner_final"}')"
eq "  tightening from it: allowed" "$code" "200"
code="$(post '{"mode": "committee", "red_lines": "committee"}')"
eq "  the owner's client (no session among its ancestors): allowed" "$code/$("$B" away decisions status --json | jq -r '.mode + "/" + .red_lines')" "200/committee/committee"
eq "loopback addresses: 127.x, ::1 and ::ffff:127.x only" "$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import committee as c
print(",".join(str(c._loopback_hex(a)) for a in ("0100007F", "0200007F", "0101A8C0", "00000000000000000000000001000000",
      "0000000000000000FFFF00000100007F", "0000000000000000FFFF00000101A8C0", "00000000000000000000000000000000")))' "$REPO/lib")" \
  "True,True,False,True,True,False,False"
kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null

# ---------------------------------------------------------------- the digest and board-serve
echo "digest and board-serve:"
d10="$("$B" ask --from QA "Which image tag?")"
adv QA "$d10" --to OPS "Use the dated tag." >/dev/null 2>&1
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
jq '.committee = {participants: ["X", "Y"]}' "$BOARD_HOME/decisions/$d9.json" > "$T/x.json" && mv "$T/x.json" "$BOARD_HOME/decisions/$d9.json"
eq "  a committee block without answered_by committee is ignored" "$("$B" advise relay --from BOARD "$d9" --json)" "[]"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
