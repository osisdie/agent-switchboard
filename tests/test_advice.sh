#!/usr/bin/env bash
# shellcheck disable=SC2015,SC2016  # ok/bad always return 0; jq programs are single-quoted
# tests/test_advice.sh — committee advice on board asks while the owner is away (board advise,
# lib/advice.py: the records, the low-priority digest, board-serve's state and watch line).
#
# Usage: tests/test_advice.sh [-v]
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1

T="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-advice.XXXXXX")"
trap 'rm -rf "$T"' EXIT

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
advice:
  digest_minutes: 30
EOF

pass=0; fail=0
ok() { pass=$((pass + 1)); [ "$VERBOSE" = 1 ] && echo "  ok   $1"; return 0; }
bad() { fail=$((fail + 1)); echo "  FAIL $1"; [ -n "${2:-}" ] && echo "       $2"; return 0; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got '$2', want '$3'"; fi; }

B="$REPO/bin/board"
af() { jq -r "$2" "$BOARD_HOME/advice/$1.json"; }
lastev() { jq -s -r "[.[] | select(.type == \"$1\")] | last | $2" "$BOARD_HOME/events.jsonl"; }
nowms() { python3 -c 'import time; print(int(time.time() * 1000))'; }
# every session live (the digest skips offline ones)
snap() { jq -n --arg off "${1:-}" '{generated_at: 1, sessions: (["API", "WEB", "QA", "OPS"]
  | map({code: ., state: (if . == $off then "offline" else "idle" end)}))}' > "$BOARD_HOME/snapshot.json"; }
snap
digest() { BOARD_NOW_MS="$1" "$B" advise digest --from BOARD "${@:2}"; }
# advice only here (the owner keeps the final say); the committee deciding is tests/test_committee.sh
"$B" away decisions owner_final --by local >/dev/null
sent() { BOARD_NOW_MS="$1" "$B" advise sent --from BOARD "${@:2}"; }

# ---------------------------------------------------------------- refusals
echo "refusals:"
d1="$("$B" ask --from API "Keep the old endpoint as a redirect?" --option "Keep redirect" --option "Remove it")"
"$B" advise "$d1" --from WEB "keep it" >/dev/null 2>&1; eq "not away: refused (exit 1)" "$?" "1"
[ -e "$BOARD_HOME/advice/$d1.json" ] && bad "  nothing written" || ok "  nothing written"
"$B" advise >/dev/null 2>&1; eq "no arguments: usage (exit 2)" "$?" "2"
"$B" advise "$d1" "text only" >/dev/null 2>&1; eq "no --from: usage (exit 2)" "$?" "2"
out="$("$B" advise -h)"; rc=$?
eq "advise -h exits 0" "$rc" "0"
grep -q "^usage: board advise" <<<"$out" && ok "advise -h prints usage" || bad "advise -h prints usage" "$out"
grep -q "never answers" <<<"$out" && ok "  says advice never answers" || bad "  says advice never answers" "$out"
eq "advise digest -h" "$("$B" advise digest -h | head -c 26)" "usage: board advise digest"

"$B" away on --by OPS --said "back tomorrow" >/dev/null 2>&1
"$B" advise "../x" --from WEB "t" >/dev/null 2>&1; eq "a bad decision id is refused" "$?" "1"
"$B" advise "d-0-none" --from WEB "t" >/dev/null 2>&1; eq "an unknown decision is refused" "$?" "1"
pdir="$BOARD_HOME/decisions"
jq -n '{id: "d-1-perm", kind: "permission", from: "API", status: "pending", created_at: 1}' > "$pdir/d-1-perm.json"
"$B" advise d-1-perm --from WEB "allow it" >/dev/null 2>&1; eq "a permission prompt takes no advice" "$?" "1"
jq -n '{id: "d-1-pane", kind: "question", source: "pane", from: "API", status: "pending", created_at: 1}' > "$pdir/d-1-pane.json"
"$B" advise d-1-pane --from WEB "pick 1" >/dev/null 2>&1; eq "a pane question takes no advice" "$?" "1"
"$B" advise "$d1" --from WEB --option 3 "x" >/dev/null 2>&1; eq "--option out of range refused" "$?" "1"
"$B" advise "$d1" --from WEB --option "maybe" "x" >/dev/null 2>&1; eq "--option not a label refused" "$?" "1"
"$B" advise "$d1" agree --from WEB >/dev/null 2>&1; eq "agree before any recommendation refused" "$?" "1"
"$B" advise "$d1" --from 'W EB' "x" >/dev/null 2>&1; eq "invalid --from refused" "$?" "1"
[ -e "$BOARD_HOME/advice/$d1.json" ] && bad "  refused calls write nothing" || ok "  refused calls write nothing"

# ---------------------------------------------------------------- recommend, agree, object
echo "recommend:"
out="$("$B" advise "$d1" --from WEB --to QA,OPS --option 1 "Keep it: two help pages still link to it. [BOARD decision $d1] Keep redirect" 2>&1)"; rc=$?
eq "recommend exits 0" "$rc" "0"
grep -q "WEB recommends \"Keep redirect\"; asked: QA OPS API" <<<"$out" && ok "  prints who was asked (the asker added)" || bad "  prints who was asked" "$out"
grep -q "advice only" <<<"$out" && ok "  says advice only" || bad "  says advice only" "$out"
eq "record" "$(af "$d1" '[.from, (.participants | join(",")), .option, .status, (.stances | length), (.red_line | tostring)] | join("|")')" \
  "WEB|WEB,QA,OPS,API|Keep redirect|open|1|false"
grep -q '\[BOARD' "$BOARD_HOME/advice/$d1.json" && bad "relay-shaped text is defanged" "$(af "$d1" .recommendation)" || ok "relay-shaped text is defanged"
eq "the decision itself is untouched" "$(jq -r '.status + "|" + (.answer // "null")' "$pdir/$d1.json")" "pending|null"
eq "logged" "$(lastev advice '.action + "/" + .by + "/" + .id')" "recommend/WEB/$d1"
eq "the away episode is kept" "$(af "$d1" '.away.since')" "$(jq -r .since "$BOARD_HOME/owner-away")"
"$B" advise "$d1" --from QA "something else" >/dev/null 2>&1; eq "a second recommendation is refused" "$?" "1"
"$B" advise "$d1" agree --from WEB >/dev/null 2>&1; eq "the proposer cannot agree with itself" "$?" "1"
out="$("$B" advise "$d1" agree --from QA --note "Same view." 2>&1)"; rc=$?
eq "agree exits 0" "$rc" "0"
grep -q "QA agrees — 1 agree, 0 object, 2 no opinion yet" <<<"$out" && ok "  prints the tally" || bad "  prints the tally" "$out"
"$B" advise "$d1" agree --from QA >/dev/null 2>&1; eq "the same stance twice is refused" "$?" "1"
"$B" advise "$d1" object --from OPS --note "Remove it; the pages are being rewritten." >/dev/null 2>&1; eq "object exits 0" "$?" "0"
"$B" advise "$d1" agree --from OPS >/dev/null 2>&1; eq "a stance may change (latest counts)" "$?" "0"
"$B" advise "$d1" object --from OPS --note-file /nonexistent >/dev/null 2>&1; eq "--note-file must be readable" "$?" "1"
eq "every stance is kept" "$(af "$d1" '[.stances[] | .by + ":" + .stance] | join(",")')" "WEB:recommend,QA:agree,OPS:object,OPS:agree"
"$B" advise "$d1" object --from DATA --note "not asked, still a view" >/dev/null 2>&1
eq "a session that was not asked may weigh in (joins the list)" "$(af "$d1" '.participants | join(",")')" "WEB,QA,OPS,API,DATA"
eq "no notification was attempted" "$(jq -s '[.[] | select(.type == "notify")] | length' "$BOARD_HOME/events.jsonl")" "0"

out="$("$B" advise ls)"
grep -q "$d1  open  WEB recommends \"Keep redirect\"" <<<"$out" && ok "ls lists it" || bad "ls lists it" "$out"
grep -q "QA, OPS agree; DATA object; no opinion: API" <<<"$out" && ok "  with who weighed in" || bad "  with who weighed in" "$out"
eq "show --json" "$("$B" advise show "$d1" --json | jq -r '.status + "|" + .option + "|" + (.red_line | tostring)')" "open|Keep redirect|false"

# red line: a production gate always; --red-line on any ask
tid="$("$B" task new --owner OPS "Rotate certificates" --stage production)"
d2="$("$B" ask --from OPS --task "$tid" --gate production "Release to production?")"
"$B" advise "$d2" --from API --option "request changes" "Wait for the owner." >/dev/null 2>&1; eq "a production gate takes advice" "$?" "0"
eq "  shown as a red line" "$("$B" advise show "$d2" --json | jq -r .red_line)" "true"
d3="$("$B" ask --from QA "Delete the old fixtures bucket?")"
"$B" advise "$d3" --from QA --red-line "Owner only: it deletes real data." >/dev/null 2>&1
eq "--red-line marks a plain ask" "$("$B" advise show "$d3" --json | jq -r .red_line)" "true"

# self_check: inside tmux, --from must be this session
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
case "$1" in display-message) echo "$FAKE_TMUX_SESSION" ;; esac
EOF
chmod +x "$T/bin/tmux"
TMUX=fake FAKE_TMUX_SESSION=API PATH="$T/bin:$PATH" "$B" advise "$d3" agree --from WEB >/dev/null 2>&1; rc=$?
eq "speaking as another session from inside tmux is refused (exit 4)" "$rc" "4"
eq "  logged" "$(lastev advise '.result + "/" + .by + "/" + .tmux_session')" "refused/WEB/API"
TMUX=fake FAKE_TMUX_SESSION=web-main PATH="$T/bin:$PATH" "$B" advise "$d3" agree --from WEB >/dev/null 2>&1; rc=$?
eq "an alias of the same code may" "$rc" "0"

# ---------------------------------------------------------------- digest
echo "digest:"
now="$(nowms)"
d4="$("$B" ask --from API "Which queue name?" --option alpha --option beta)"
# read-only (prints nothing, so nothing goes in flight): the decisions due for API at $now
due_api="$(python3 - "$REPO" "$now" <<'EOF'
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "lib"))
import advice, fleet
print(",".join(d["id"] for _k, d, _r, _x in advice.due(fleet.board_home(), fleet.load_fleet(), int(sys.argv[2])).get("API", [])))
EOF
)"
case ",$due_api," in *",$d4,"*) bad "an ask younger than 10 minutes is not in a digest yet" "$due_api" ;; *) ok "an ask younger than 10 minutes is not in a digest yet" ;; esac
later=$((now + 11 * 60000))
out="$(digest "$later")"
grep -q "^== API g-[0-9a-f]\{12\} " <<<"$out" && ok "one block for the asker" || bad "one block for the asker" "$out"
grep -q "Low priority: reply at your next natural pause (between tasks); do not stop current work" <<<"$out" && ok "  worded low priority" || bad "  worded low priority" "$out"
grep -q "never an answer or an approval" <<<"$out" && ok "  says it is not an answer" || bad "  says it is not an answer" "$out"
grep -q "board advise $d4 --from API" <<<"$out" && ok "  the asker is asked to propose" || bad "  the asker is asked to propose" "$out"
grep -q "board advise $d2 agree|object --from OPS" <<<"$out" && ok "  OPS is asked to weigh in on the gate" || bad "  OPS weighs in" "$out"
grep -q "red line: owner only" <<<"$out" && ok "  the red line is marked" || bad "  the red line is marked" "$out"
grep -q "$d1 agree|object --from API" <<<"$out" && ok "  the silent asker of d1 is asked" || bad "  the silent asker of d1" "$out"
grep -q "\-\-from QA.*$d1\|$d1.*--from QA" <<<"$out" && bad "  QA already weighed in on d1: not asked again" "$out" || ok "  QA already weighed in on d1: not asked again"
n_tags="$(grep -o '\[BOARD' <<<"$out" | wc -l | tr -d ' ')"
n_blocks="$(grep -c '^== ' <<<"$out")"
eq "exactly one [BOARD tag per message (quoted text defanged)" "$n_tags" "$n_blocks"
gid="$(grep '^== API ' <<<"$out" | awk '{print $3}')"
ogid="$(grep '^== OPS ' <<<"$out" | awk '{print $3}')"
[ -n "$ogid" ] && ok "  OPS gets its own block" || bad "  OPS gets its own block" "$out"
eq "a second digest before sent prints nothing again (one in flight per session)" "$(digest "$later")" ""
d4b="$("$B" ask --from API "And the topic name?")"
eq "a new item while a digest is in flight: no second batch" "$(digest "$later" API --json | jq length)" "0"
w="$(BOARD_NOW_MS="$later" python3 "$REPO/lib/serve.py" --watch --once)"
grep -q "^advice API due$" <<<"$w" && bad "  and no new watch line for it" "$w" || ok "  and no new watch line for it"
sent "$later" "$gid" >/dev/null; eq "sent records it" "$?" "0"
sent "$later" "$gid" >/dev/null 2>&1; eq "  the same id twice: exit 1" "$?" "1"
sent "$later" "nope" >/dev/null 2>&1; eq "  a malformed id: exit 2" "$?" "2"
eq "no second digest within digest_minutes" "$(digest $((later + 60000)) API --json | jq length)" "0"
late2=$((later + 31 * 60000))
eq "after the interval: only the item that was not sent" "$(digest "$late2" API --json | jq -r '[.[].decisions[]] | join(",")')" "$d4b"
snap OPS
eq "an offline session gets no digest" "$(digest "$late2" OPS --json | jq length)" "0"
snap
eq "an unsent digest comes back once it expires" "$(digest "$late2" OPS --json | jq -r '[.[].decisions[]] | join(",")')" "$d2"
sent "$late2" "$ogid" >/dev/null 2>&1; eq "  the expired id cannot be recorded (exit 1)" "$?" "1"
"$B" advise "$d1" agree --from API >/dev/null 2>&1
late3=$((late2 + 31 * 60000))
eq "a session that weighed in is not asked" "$(digest "$late3" API --json | jq -r '([.[].decisions[]] | join(",")) + "|" + ([.[].text] | join("\n") | test("'"$d1"' agree\\|object --from API") | tostring)')" "$d4b|false"
# digest and sent are the operator's
st0="$(cat "$BOARD_HOME/advice-digest.json")"
"$B" advise digest --from WEB >/dev/null 2>&1; eq "digest from a session that is not the operator: exit 4" "$?" "4"
eq "  logged" "$(lastev advise '.result + "/" + .reason + "/" + .by')" "refused/not the operator/WEB"
"$B" advise sent --from WEB "g-000000000000" >/dev/null 2>&1; eq "sent from another session: exit 4" "$?" "4"
eq "  nothing printed or recorded" "$(cat "$BOARD_HOME/advice-digest.json")" "$st0"
"$B" advise digest >/dev/null 2>&1; eq "digest without --from: usage (exit 2)" "$?" "2"
printf '{"operator_session": "OPS"}\n' > "$BOARD_HOME/operator.json"
eq "operator.json's operator_session may run it" "$(BOARD_NOW_MS="$late3" "$B" advise digest --from OPS QA --json)" "[]"
rm -f "$BOARD_HOME/operator.json"
TMUX=fake FAKE_TMUX_SESSION=API PATH="$T/bin:$PATH" "$B" advise digest --from BOARD QA >/dev/null 2>&1; rc=$?
eq "inside tmux, --from BOARD from another session: exit 4" "$rc" "4"
eq "  from the BOARD session itself" "$(TMUX=fake FAKE_TMUX_SESSION=BOARD PATH="$T/bin:$PATH" BOARD_NOW_MS="$late3" "$B" advise digest --from BOARD QA --json)" "[]"
# the interval counts from printing: an unsent digest that expires waits digest_minutes, not 30 min
sed -i 's/digest_minutes: 30/digest_minutes: 90/' "$BOARD_CONFIG_DIR/fleet.yaml"
d8="$("$B" ask --from QA "Which retention period?")"
t8=$((late3 + 60000))
eq "a digest is printed (never sent)" "$(digest "$t8" QA --json | jq -r '[.[].decisions[]] | join(",")')" "$d8"
eq "  expired after 30 min, still not due before digest_minutes since printing" "$(digest $((t8 + 31 * 60000)) QA --json | jq length)" "0"
eq "  due again once digest_minutes have passed" "$(digest $((t8 + 91 * 60000)) QA --json | jq -r '[.[].decisions[]] | join(",")')" "$d8"
sed -i 's/digest_minutes: 90/digest_minutes: 30/' "$BOARD_CONFIG_DIR/fleet.yaml"
eq "digest sends no notification" "$(jq -s '[.[] | select(.type == "notify")] | length' "$BOARD_HOME/events.jsonl")" "0"
grep -q '"digest-sent"' "$BOARD_HOME/events.jsonl" && ok "digest-sent is in events.jsonl" || bad "digest-sent is in events.jsonl"

# ---------------------------------------------------------------- board-serve
echo "board-serve:"
st="$(python3 - "$REPO" <<'EOF'
import json, os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "lib"))
import fleet, serve
s = serve.StateBuilder(fleet.board_home()).build(fleet.load_fleet())
print(json.dumps(s["advice"]))
EOF
)"
eq "state.advice carries the records" "$(jq -r --arg d "$d1" '.[] | select(.decision_id == $d) | .status + "|" + .option' <<<"$st")" "open|Keep redirect"
grep -q '\[BOARD' <<<"$st" && bad "  no relay-shaped text in state" || ok "  no relay-shaped text in state"
late4=$((late3 + 31 * 60000))
w="$(BOARD_NOW_MS="$late4" python3 "$REPO/lib/serve.py" --watch --once)"
grep -q "^advice API due$" <<<"$w" && ok "watch: advice <CODE> due" || bad "watch: advice <CODE> due" "$w"
grep -q "^decision $d1" <<<"$w" && bad "  advice is never a decision line" "$w" || ok "  advice is never a decision line"
n="$(python3 - "$REPO" <<'EOF'
import json, os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "lib"))
import fleet, notify
h = fleet.board_home()
snap = json.load(open(os.path.join(h, "snapshot.json")))
snap["decisions"] = [json.load(open(os.path.join(h, "decisions", f))) for f in os.listdir(os.path.join(h, "decisions"))]
print(notify._pending_count(snap, h))
EOF
)"
eq "operator:stale never counts advice as waiting (lib/notify.py _pending_count)" "$n" "0"

# ---------------------------------------------------------------- freeze
echo "freeze:"
"$B" answer "$d3" "keep the bucket" --by local >/dev/null 2>&1
eq "an answered ask: advice shows frozen" "$("$B" advise show "$d3" --json | jq -r '.status + "/" + .frozen_reason')" "frozen/answered"
"$B" advise "$d3" object --from OPS >/dev/null 2>&1; eq "  and takes no more" "$?" "1"
eq "  the record is frozen on disk" "$(af "$d3" '.status + "/" + .frozen_reason')" "frozen/answered"
eq "  the freeze is logged" "$(lastev advice '.action + "/" + .reason + "/" + .id')" "freeze/answered/$d3"
eq "  the answer is the owner's" "$(jq -r '.answer + "|" + .answered_by' "$pdir/$d3.json")" "keep the bucket|local"
out="$("$B" away off --by OPS 2>&1)"
grep -q "committee advice frozen on 2 ask(s)" <<<"$out" && ok "away off freezes open advice" || bad "away off freezes open advice" "$out"
eq "  frozen/owner_back" "$(af "$d1" '.status + "/" + .frozen_reason')" "frozen/owner_back"
eq "  the asks still wait for the owner" "$(jq -r .status "$pdir/$d1.json")" "pending"
"$B" advise "$d1" object --from QA >/dev/null 2>&1; eq "  no more stances once back" "$?" "1"
eq "  digest: nothing while the owner is here" "$(digest "$late2" --json | jq length)" "0"
"$B" away on --by OPS --said "away again" >/dev/null 2>&1
"$B" advise "$d1" object --from QA >/dev/null 2>&1; eq "a new away period does not reopen earlier advice" "$?" "1"
eq "  still shown frozen" "$("$B" advise show "$d1" --json | jq -r .status)" "frozen"
d6="$("$B" ask --from WEB "New question?")"
"$B" advise "$d6" --from WEB "yes" >/dev/null 2>&1
jq '.away.since = 1' "$BOARD_HOME/advice/$d6.json" > "$T/a.json" && mv "$T/a.json" "$BOARD_HOME/advice/$d6.json"
"$B" advise "$d6" agree --from QA >/dev/null 2>&1; eq "advice from another away period takes no stance" "$?" "1"
eq "  frozen and logged" "$(af "$d6" .frozen_reason)/$(lastev advice '.action + "/" + .reason')" "away_changed/freeze/away_changed"
d6="$("$B" ask --from WEB "Another new question?")"
"$B" advise "$d6" --from WEB "yes" >/dev/null 2>&1
"$B" away on --by OPS --said "replaced" --replace >/dev/null 2>&1
eq "a replaced away record freezes advice of the earlier one" "$(af "$d6" '.status + "/" + .frozen_reason')" "frozen/away_changed"

# ---------------------------------------------------------------- concurrency
echo "concurrency:"
d7="$("$B" ask --from API "Race?")"
"$B" advise "$d7" --from API "race" >/dev/null 2>&1
for c in A1 A2 A3 A4 A5 A6; do "$B" advise "$d7" agree --from "$c" >/dev/null 2>&1 & done
wait
eq "parallel stances are all kept (lock)" "$(af "$d7" '.stances | length')" "7"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
