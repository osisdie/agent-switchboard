#!/usr/bin/env bash
# shellcheck disable=SC2015,SC2016  # ok/bad always return 0; literal $ in expected strings
# tests/run.sh — offline tests for agent-switchboard (no real tmux, Slack or Claude needed).
#
# Usage: tests/run.sh [-v]
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1

T="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-test.XXXXXX")"
trap 'rm -rf "$T"' EXIT

export BOARD_HOME="$T/home"
export CLAUDE_JSON="$T/claude.json"  # the account fixture, never ~/.claude.json
export BOARD_CONFIG_DIR="$T/config"
export CLAUDE_SESSIONS_DIR="$T/sessions"
export CLAUDE_SETTINGS="$T/settings.json"
# Resources: a calm fixture /proc (tests/fake_proc.py), so the live machine's numbers never alert.
python3 "$REPO/tests/fake_proc.py" "$T/proc" && export BOARD_PROC_ROOT="$T/proc"
unset TMUX_PANE BOARD_PUBLIC_URL
# Alert channels: nothing from the real environment, and never a real desktop toast.
while IFS= read -r v; do unset "$v"; done < <(compgen -e | grep -E '^(SLACK_|SMTP_|ALERT_|LINE_|AWS_)')
export ALERT_DESKTOP=0
mkdir -p "$BOARD_CONFIG_DIR" "$CLAUDE_SESSIONS_DIR" "$T/bin"

pass=0; fail=0
ok() { pass=$((pass + 1)); [ "$VERBOSE" = 1 ] && echo "  ok   $1"; return 0; }
bad() { fail=$((fail + 1)); echo "  FAIL $1"; [ -n "${2:-}" ] && echo "       $2"; return 0; }
eq() {  # eq NAME GOT WANT
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got '$2', want '$3'"; fi
}

HOOK="$REPO/hooks/board-hook.sh"
SID="11111111-2222-3333-4444-555555555555"
hook() {  # hook JSON — returns hook stdout (must be empty)
  printf '%s' "$1" | TMUX_PANE=%42 "$HOOK"
}
st() { jq -r "$1" "$BOARD_HOME/state/$SID.json"; }
ndec() { find "$BOARD_HOME/decisions" -name '*.json' | wc -l | tr -d ' '; }

echo "hook:"
out="$(hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"SessionStart\",\"cwd\":\"/w\",\"source\":\"startup\"}")"
eq "hook prints nothing" "$out" ""
eq "SessionStart -> rest" "$(st .state)" "rest"
eq "tmux_pane recorded" "$(st .tmux_pane)" "%42"

hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"UserPromptSubmit\",\"prompt\":\"SECRET PROMPT TEXT\"}"
eq "UserPromptSubmit -> working" "$(st .state)" "working"
eq "UserPromptSubmit records last_prompt_at" "$(st '.last_prompt_at | type')" "number"
if grep -rq "SECRET PROMPT" "$BOARD_HOME"; then bad "prompt text not recorded"; else ok "prompt text not recorded"; fi

hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"tool_use_id\":\"tu_read\",\"tool_input\":{\"file_path\":\"/w/a.txt\"}}"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PostToolUse\",\"tool_name\":\"Read\",\"tool_use_id\":\"tu_read\",\"tool_input\":{\"file_path\":\"/w/a.txt\"}}"
long="$(printf 'x%.0s' $(seq 1 300))"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_use_id\":\"tu_1\",\"tool_input\":{\"command\":\"rm -rf build $long\"}}"
eq "PreToolUse -> working" "$(st .state)" "working"
eq "PreToolUse cached" "$(st '.recent_pre | last | .tool_use_id')" "tu_1"
eq "recent_pre drops finished tools" "$(st '[.recent_pre[].tool_use_id] | join(",")')" "tu_1"
eq "recent_done keeps the finished tool" "$(st '.recent_done | last | "\(.tool) \(.summary) \(.ok)"')" "Read /w/a.txt true"
eq "recent_done has a duration and time" "$(st '.recent_done | last | [(.ms | type), (.at | type)] | join(",")')" "number,number"
# A failed tool, a summary over 120 chars and a token in it (redacted like the summary), and the ring
# stays at 8.
tok="ghp_$(printf 'Z%.0s' $(seq 1 30))"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_use_id\":\"tu_f\",\"tool_input\":{\"command\":\"curl -H 'Authorization: token $tok' $long\"}}"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PostToolUseFailure\",\"tool_name\":\"Bash\",\"tool_use_id\":\"tu_f\",\"tool_input\":{\"command\":\"curl -H 'Authorization: token $tok' $long\"}}"
eq "recent_done failed tool" "$(st '.recent_done | last | .ok')" "false"
eq "recent_done summary <= 120" "$(st '.recent_done | last | .summary | length <= 120')" "true"
grep -q "$tok" "$BOARD_HOME/state/$SID.json" && bad "recent_done token redacted" || ok "recent_done token redacted"
for i in 1 2 3 4 5 6 7 8 9; do
  hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Grep\",\"tool_use_id\":\"tu_r$i\",\"tool_input\":{\"pattern\":\"p$i\"}}"
  hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PostToolUse\",\"tool_name\":\"Grep\",\"tool_use_id\":\"tu_r$i\",\"tool_input\":{\"pattern\":\"p$i\"}}"
done
eq "recent_done ring holds 8" "$(st '.recent_done | length')" "8"
eq "recent_done newest last" "$(st '.recent_done | last | .summary')" "p9"
eq "recent_pre still tu_1" "$(st '[.recent_pre[].tool_use_id] | join(",")')" "tu_1"
eq "no pending yet" "$(st .pending)" "null"

hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PermissionRequest\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"rm -rf build $long\"}}"
eq "PermissionRequest -> needs-you" "$(st .state)" "needs-you"
eq "pending tool_use_id from PreToolUse cache" "$(st .pending.tool_use_id)" "tu_1"
eq "pending tool_name" "$(st .pending.tool_name)" "Bash"
eq "summary capped at 200 chars" "$(st '.pending.summary | length')" "200"
eq "one permission decision" "$(ndec)" "1"
dec="$(find "$BOARD_HOME/decisions" -name '*.json' | head -1)"
eq "decision kind" "$(jq -r .kind "$dec")" "permission"
eq "decision tool_use_id" "$(jq -r .tool_use_id "$dec")" "tu_1"
eq "decision status" "$(jq -r .status "$dec")" "pending"
eq "decision id format" "$(jq -r '.id | test("^d-[0-9]+-[0-9a-f]+$")' "$dec")" "true"

hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"Notification\",\"notification_type\":\"permission_prompt\",\"message\":\"Claude needs your permission to use Bash\"}"
eq "Notification permission_prompt keeps needs-you" "$(st .state)" "needs-you"
eq "decision deduped by tool_use_id" "$(ndec)" "1"
eq "notification_type recorded" "$(st .notification_type)" "permission_prompt"

# A subagent's unrelated tool use does not hide the prompt.
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Grep\",\"tool_use_id\":\"tu_other\",\"tool_input\":{\"pattern\":\"foo\"}}"
eq "unrelated PreToolUse keeps needs-you" "$(st .state)" "needs-you"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PostToolUse\",\"tool_name\":\"Grep\",\"tool_use_id\":\"tu_other\",\"tool_input\":{\"pattern\":\"foo\"}}"
eq "unrelated PostToolUse keeps pending" "$(st .pending.tool_use_id)" "tu_1"

# ---- collector
cat > "$BOARD_CONFIG_DIR/fleet.yaml" <<'EOF'
collector: true
approvers: [user-1]
sessions:
  - code: CP
    cwd: /w
    autostart: false
  - code: INFRA
    cwd: /w/infra
    aliases: [Infra-old]
  - code: GONE
    cwd: /w/gone
EOF
sleep 600 & live_pid=$!
sleep 600 & live_pid2=$!
sleep 600 & live_pid3=$!
trap 'kill $live_pid $live_pid2 $live_pid3 2>/dev/null; rm -rf "$T"' EXIT
cat > "$CLAUDE_SESSIONS_DIR/$live_pid.json" <<EOF
{"pid":$live_pid,"sessionId":"$SID","cwd":"/w","tmux":"CP:@3.%42","name":"CP","status":"busy","bridgeSessionId":"session_abc","startedAt":1}
EOF
cat > "$CLAUDE_SESSIONS_DIR/$live_pid2.json" <<EOF
{"pid":$live_pid2,"sessionId":"s-infra","cwd":"/w/infra","tmux":null,"name":"Infra-old","status":"idle","bridgeSessionId":null,"startedAt":2}
EOF
cat > "$CLAUDE_SESSIONS_DIR/$live_pid3.json" <<EOF
{"pid":$live_pid3,"sessionId":"s-qa","cwd":"/w/qa","tmux":"QA:@9.%9","name":"QA","status":"waiting","bridgeSessionId":null,"startedAt":3}
EOF
cat > "$CLAUDE_SESSIONS_DIR/999999999.json" <<'EOF'
{"pid":999999999,"sessionId":"dead","cwd":"/w","tmux":"DEAD:@1.%1","name":"DEAD","status":"idle"}
EOF

# The collector reads pane titles with `tmux list-panes -a`: a fake, so a real tmux server on this
# machine never leaks into the tests. Everything else fails, as tmux does without a server.
mkdir -p "$T/fakebin"
cat > "$T/fakebin/tmux" <<'EOF'
#!/usr/bin/env bash
[ "$1" = "list-panes" ] && [ -n "${FAKE_PANES:-}" ] && { cat "$FAKE_PANES"; exit 0; }
exit 1
EOF
chmod +x "$T/fakebin/tmux"
export PATH="$T/fakebin:$PATH"

echo "collector:"
"$REPO/bin/board-collect" --once --no-notify
snap="$BOARD_HOME/snapshot.json"
[ -f "$snap" ] && ok "snapshot written" || bad "snapshot written"
eq "dead pid skipped" "$(jq '[.sessions[] | select(.code == "DEAD")] | length' "$snap")" "0"
eq "codes" "$(jq -r '[.sessions[].code] | join(",")' "$snap")" "CP,GONE,INFRA,QA"
eq "CP merged with hook state" "$(jq -r '.sessions[] | select(.code=="CP") | .state' "$snap")" "needs-you"
eq "CP pending" "$(jq -r '.sessions[] | select(.code=="CP") | .pending.tool_use_id' "$snap")" "tu_1"
eq "CP pane" "$(jq -r '.sessions[] | select(.code=="CP") | .tmux_pane' "$snap")" "%42"
eq "remote_url" "$(jq -r '.sessions[] | select(.code=="CP") | .remote_url' "$snap")" "https://claude.ai/code/session_abc"
eq "alias -> code (no tmux)" "$(jq -r '.sessions[] | select(.code=="INFRA") | .name' "$snap")" "Infra-old"
eq "registry-only session state from status" "$(jq -r '.sessions[] | select(.code=="INFRA") | .state' "$snap")" "rest"
eq "fleet session not running -> offline" "$(jq -r '.sessions[] | select(.code=="GONE") | .state' "$snap")" "offline"
eq "permission decision gets from=code" "$(jq -r '.decisions[0].from' "$snap")" "CP"
eq "registry status waiting -> needs-you" "$(jq -r '.sessions[] | select(.code=="QA") | .state' "$snap")" "needs-you"
# A newer registry busy/idle overrides a stale hook needs-you (missed clearing event).
cp "$CLAUDE_SESSIONS_DIR/$live_pid.json" "$T/cp-reg.json"
jq '.statusUpdatedAt = 99999999999999' "$T/cp-reg.json" > "$CLAUDE_SESSIONS_DIR/$live_pid.json"
"$REPO/bin/board-collect" --once --no-notify
eq "newer registry busy overrides stale needs-you" "$(jq -r '.sessions[] | select(.code=="CP") | .state' "$snap")" "working"
eq "permission decision no longer pending -> stale" "$(jq -r .status "$dec")" "stale"
cp "$T/cp-reg.json" "$CLAUDE_SESSIONS_DIR/$live_pid.json"
"$REPO/bin/board-collect" --once --no-notify
gen1="$(jq .generated_at "$snap")"
sleep 0.05
"$REPO/bin/board-collect" --once --no-notify
eq "unchanged snapshot not rewritten" "$(jq .generated_at "$snap")" "$gen1"

echo "collector: card details"
export FAKE_PANES="$T/panes.txt"
HOST="$(python3 -c 'import socket; print(socket.gethostname())')"
printf '%%42\t✳ Fix the login form in /home/someone/private/app/login.py\n%%9\t%s\n' "$HOST" > "$FAKE_PANES"
mkdir -p "$T/repos/web-app/src"
git -C "$T/repos/web-app" init -q -b feature/cards
git -C "$T/repos/web-app" -c user.name=t -c user.email=t@example.com commit -q --allow-empty -m init
jq --arg cwd "$T/repos/web-app/src" '.cwd = $cwd' "$T/cp-reg.json" > "$CLAUDE_SESSIONS_DIR/$live_pid.json"
cat > "$BOARD_HOME/state/s-qa.json" <<EOF
{"session_id":"s-qa","state":"working","last_event":"PreToolUse","notification_type":null,"updated_at":1,
 "recent_pre":[{"tool_use_id":"t1","tool_name":"Read","summary":"old"},
               {"tool_use_id":"t2","tool_name":"Grep","summary":"needle in C:\\\\Users\\\\someone\\\\notes.txt"},
               {"tool_use_id":"t3","tool_name":"Edit","summary":"~/secret/dir/app.py"},
               {"tool_use_id":"t4","tool_name":"Bash","summary":"cd /home/someone/repo && npm test \u001b[31m$(printf 'x%.0s' $(seq 1 300))"}]}
EOF
"$REPO/bin/board-collect" --once --no-notify
sj() { jq -r --arg c "$1" ".sessions[] | select(.code==\$c) | $2" "$snap"; }
eq "title from the pane, glyph dropped, path cut" "$(sj CP .title)" "Fix the login form in …/login.py"
eq "tmux's default title (the host name) is not published" "$(sj QA .title)" "null"
eq "repo is the basename of the git toplevel" "$(sj CP .repo)" "web-app"
eq "branch" "$(sj CP .branch)" "feature/cards"
eq "no repo outside git" "$(sj INFRA '.repo, .branch' | tr '\n' ' ')" "null null "
eq "last_event / notification_type" "$(sj QA '.last_event + "/" + (.notification_type | tostring)')" "PreToolUse/null"
eq "recent: the last 3 tool calls" "$(sj QA '[.recent[].tool] | join(",")')" "Grep,Edit,Bash"
eq "recent: Windows and home paths cut" "$(sj QA '.recent[0].summary + " | " + .recent[1].summary')" "needle in …/notes.txt | …/app.py"
eq "recent: control characters gone, clipped to 160" "$(sj QA '.recent[2].summary | ([explode[] | select(. < 32 or . == 127)] | length | tostring) + "/" + (length | tostring) + "/" + .[0:24]')" "0/160/cd …/repo && npm test xx"
eq "offline fleet rows carry empty details" "$(sj GONE '([.title, .repo, .branch, .last_event] | map(tostring) | join(",")) + "/" + (.recent | length | tostring)')" "null,null,null,null/0"
eq "no path in any session's card fields" "$(jq -r '[.sessions[] | .title, .repo, .branch, (.recent[]? | .summary)] | map(select(. != null)) | map(select(test("/home|/tmp|someone/|Users"))) | length' "$snap")" "0"
grep -q "$T/repos" "$BOARD_HOME/gitinfo.json" && ok "git info cached per cwd" || bad "git info cached per cwd"
git -C "$T/repos/web-app" checkout -q -b other
"$REPO/bin/board-collect" --once --no-notify
eq "git info reused within 30 s" "$(sj CP .branch)" "feature/cards"
jq 'map_values(.at = 0)' "$BOARD_HOME/gitinfo.json" > "$T/gi.json" && mv "$T/gi.json" "$BOARD_HOME/gitinfo.json"
"$REPO/bin/board-collect" --once --no-notify
eq "  and refreshed after" "$(sj CP .branch)" "other"
git -C "$T/repos/web-app" checkout -q --detach
jq 'map_values(.at = 0)' "$BOARD_HOME/gitinfo.json" > "$T/gi.json" && mv "$T/gi.json" "$BOARD_HOME/gitinfo.json"
"$REPO/bin/board-collect" --once --no-notify
eq "  detached HEAD" "$(sj CP .branch)" "detached"
git init -q -b fresh "$T/repos/unborn"
eq "  a repo with no commits yet: one git call still names it" \
  "$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import collect; print("/".join(map(str, collect.git_where(sys.argv[2]))))' "$REPO/lib" "$T/repos/unborn")" "unborn/no commits"

echo "collector: untrusted text"
while IFS=$'\t' read -r verdict name detail; do
  if [ "$verdict" = ok ]; then ok "$name"; else bad "$name" "$detail"; fi
done < <(python3 - "$REPO/lib" "$T" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import collect as c
T = lambda t: c.clean_text(t, 200)
H = {"devbox", "devbox.local", "devbox.example.com"}
cases = [
    ("file:// URL cut", T("open file:///home/u/x/report.html now"), "open …/report.html now"),
    ("UNC path cut", T("copy \\\\server\\share\\dir\\a.txt"), "copy …/a.txt"),
    ("C:/ path cut", T("C:/Users/me/proj/a.txt"), "…/a.txt"),
    ("C:\\ path cut", T("edit C:\\Users\\me\\a.txt"), "edit …/a.txt"),
    ("~user and ~/ paths cut", T("~bob/notes/x.md and ~/y.md"), "…/x.md and …/y.md"),
    ("quoted path with spaces: no middle parts", T("cat '/home/a b/c d.txt' x"), "cat '…/c d.txt' x"),
    ("double-quoted path with spaces", T('see "/srv/my dir/f.log"'), 'see "…/f.log"'),
    ("URL credentials dropped", T("https://user:pw@example.com/a and ssh://git@example.com/r.git"),
     "https://example.com/a and ssh://example.com/r.git"),
    ("tokens redacted", T("hdr " + "Bear" + "er " + "k7" * 7 + " --pass" + "word " + "hun" + "ter" + "2 " + "gh" + "p_" + "a" * 24),
     "hdr Bearer [REDACTED] --password [REDACTED] [REDACTED]"),
    ("URLs and a lone slash untouched", T("see https://example.com/a/b ok, a / b"), "see https://example.com/a/b ok, a / b"),
    ("title: glyph dropped", c.clean_title("✳ Fix the login form", H, user="bob"), "Fix the login form"),
    ("title: a shell's user@host: title dropped", c.clean_title("bob@devbox: ~/x", H, user="bob"), ""),
    ("title: any user@host: dropped", c.clean_title("deploy@build-7: /srv", H, user="bob"), ""),
    ("title: <host>.local dropped", c.clean_title("Deploy on devbox.local today", H, user="bob"), ""),
    ("title: the host name as a word dropped", c.clean_title("Fix DevBox build", H, user="bob"), ""),
    ("title: the FQDN dropped", c.clean_title("ping devbox.example.com", H, user="bob"), ""),
    ("title: the user name as a word dropped", c.clean_title("Talk to bob about it", H, user="bob"), ""),
    ("title: a word containing the user name kept", c.clean_title("Fix bobcat parser", H, user="bob"), "Fix bobcat parser"),
    ("title: tokens and paths redacted too", c.clean_title("⠂ Rotate ghp_" + "b" * 24 + " in /etc/app/key.pem", H, user="bob"),
     "Rotate [REDACTED] in …/key.pem"),
    ("title: the session's own code dropped (a /renamed session)", c.clean_title("✳ AI", H, user="bob", names=["AI", "ai-old"]), ""),
    ("title: an alias dropped, any case", c.clean_title("⠂ Ai-Old", H, user="bob", names=["AI", "ai-old"]), ""),
    ("title: a title that only mentions the code kept", c.clean_title("Fix the AI build", H, user="bob", names=["AI"]), "Fix the AI build"),
    ("card_details passes the code, name and aliases",
     (lambda rows: (c.card_details(rows, sys.argv[2], 0, titles={"%1": "✳ CP", "%2": "✳ Fix it"}, hosts=set()), [r["title"] for r in rows])[1])(
         [{"code": "CP", "name": "CP", "aliases": ["CP", "cp-old"], "pid": 1, "tmux_pane": "%1", "cwd": None},
          {"code": "QA", "name": "QA", "aliases": ["QA"], "pid": 2, "tmux_pane": "%2", "cwd": None}]), [None, "Fix it"]),
    ("host names include <short>.local", sorted(n for n in c.hostnames() if n.endswith(".local"))[:1] != [], True),
]
for name, got, want in cases:
    print(("ok\t%s\t" if got == want else "FAIL\t%s\tgot %r, want %r") % ((name,) if got == want else (name, got, want)))
PY
)
unset FAKE_PANES

echo "collector: one process, one card"
sleep 600 & dup_pid=$!
sleep 600 & dup_pid2=$!
# Started inside CP's tmux, registered as GONE (renamed): it is GONE, not CP/GONE + an offline GONE.
echo "{\"pid\":$dup_pid,\"sessionId\":\"s-dup\",\"cwd\":\"/w\",\"tmux\":\"CP:@5.%75\",\"name\":\"GONE\",\"status\":\"busy\",\"startedAt\":9}" > "$CLAUDE_SESSIONS_DIR/$dup_pid.json"
# A teammate pane in CP's tmux whose name is no fleet code keeps the CP/<name> form.
echo "{\"pid\":$dup_pid2,\"sessionId\":\"s-mate\",\"cwd\":\"/w\",\"tmux\":\"CP:@6.%76\",\"name\":\"helper\",\"status\":\"idle\",\"startedAt\":10}" > "$CLAUDE_SESSIONS_DIR/$dup_pid2.json"
"$REPO/bin/board-collect" --once --no-notify
eq "codes: each process once" "$(jq -r '[.sessions[].code] | join(",")' "$snap")" "CP,CP/helper,GONE,INFRA,QA"
eq "the renamed process is GONE, live" "$(sj GONE '.state + "/" + .name')" "working/GONE"
eq "CP is still the process named CP" "$(sj CP .session_id)" "$SID"
# Resources sample now (not after the interval), from `ps` (a missing proc root) so the live test
# processes are seen: they also list each process once.
rm -f "${BOARD_HOME:?}/resources-state.json"
BOARD_PROC_ROOT="$T/no-proc" "$REPO/bin/board-collect" --once --no-notify
eq "resources list each process once too" "$(jq -r '[.sessions[].code] | sort | join(",")' "$BOARD_HOME/resources.json")" "CP,CP/helper,GONE,INFRA,QA"
kill "$dup_pid" "$dup_pid2" 2>/dev/null
rm -f "${CLAUDE_SESSIONS_DIR:?}/${dup_pid:?}.json" "${CLAUDE_SESSIONS_DIR:?}/${dup_pid2:?}.json" "${BOARD_HOME:?}/state/s-qa.json"
cp "$T/cp-reg.json" "$CLAUDE_SESSIONS_DIR/$live_pid.json"
"$REPO/bin/board-collect" --once --no-notify

echo "collector: account"
fake_key="fixture""-key-""not-real"  # built from pieces: no secret-looking literal in the repo
printf '%s\n' '{"oauthAccount": {"emailAddress": "owner@example.com", "organizationName": "Example Org", "accountUuid": "acc-1"},' \
  " \"primaryApiKey\": \"$fake_key\", \"userID\": \"u-1\", \"projects\": {\"/w\": {\"history\": [\"x\"]}}}" > "$CLAUDE_JSON"
"$REPO/bin/board-collect" --once --no-notify
acct="$BOARD_HOME/account.json"
eq "account: email and org only" "$(jq -r '[.email, .org] | join("|")' "$acct")/$(jq -r 'keys | join(",")' "$acct")" "owner@example.com|Example Org/email,org,source_mtime,updated_at"
eq "  0600" "$(stat -c %a "$acct")" "600"
grep -q 'fixture-key\|acc-1\|u-1\|history' "$acct" && bad "  nothing else from the file" || ok "  nothing else from the file"
BOARD_SHOW_ACCOUNT=0 "$REPO/bin/board-collect" --once --no-notify
eq "  BOARD_SHOW_ACCOUNT=0 removes it" "$([ -e "$acct" ] && echo kept || echo removed)" "removed"
"$REPO/bin/board-collect" --once --no-notify
cp "$BOARD_CONFIG_DIR/fleet.yaml" "$T/fleet.yaml.acct"
printf 'usage:\n  show_account: false\n' >> "$BOARD_CONFIG_DIR/fleet.yaml"
"$REPO/bin/board-collect" --once --no-notify
eq "  usage.show_account: false removes it" "$([ -e "$acct" ] && echo kept || echo removed)" "removed"
cp "$T/fleet.yaml.acct" "$BOARD_CONFIG_DIR/fleet.yaml"
rm -f "${CLAUDE_JSON:?}"
"$REPO/bin/board-collect" --once --no-notify
eq "  no ~/.claude.json: no email" "$(jq -r '.email' "$acct")" "null"

echo "collector: a session alone in another session's tmux"
cp "$BOARD_CONFIG_DIR/fleet.yaml" "$T/fleet.yaml.bak"
printf '  - code: SOLO\n    cwd: /w/solo\n' >> "$BOARD_CONFIG_DIR/fleet.yaml"
sleep 600 & solo_pid=$!
# Registered as SOLO, running in GONE's tmux while GONE's own process is not running: it is SOLO.
echo "{\"pid\":$solo_pid,\"sessionId\":\"s-solo\",\"cwd\":\"/w\",\"tmux\":\"GONE:@7.%81\",\"name\":\"SOLO\",\"status\":\"busy\",\"startedAt\":11}" > "$CLAUDE_SESSIONS_DIR/$solo_pid.json"
"$REPO/bin/board-collect" --once --no-notify
eq "its registered name wins over the tmux session" "$(sj SOLO '.state + "/" + .name')" "working/SOLO"
eq "  GONE stays offline" "$(sj GONE .state)" "offline"
eq "  one card each" "$(jq -r '[.sessions[].code] | join(",")' "$snap")" "CP,GONE,INFRA,QA,SOLO"
kill "$solo_pid" 2>/dev/null
rm -f "${CLAUDE_SESSIONS_DIR:?}/${solo_pid:?}.json"
cp "$T/fleet.yaml.bak" "$BOARD_CONFIG_DIR/fleet.yaml"
"$REPO/bin/board-collect" --once --no-notify

out="$("$REPO/bin/board" ls)"
if grep -q "needs-you" <<<"$out" && grep -q "Bash: rm -rf build" <<<"$out"; then ok "board ls"; else bad "board ls" "$out"; fi

# ---- ask / answer / mark / decisions
echo "decisions:"
qid="$("$REPO/bin/board" ask --from CP "Ship it?" --option yes --option no --context "tests green")"
eq "ask prints id" "$(grep -cE '^d-[0-9]+-[0-9a-f]+$' <<<"$qid")" "1"
eq "ask options" "$(jq -c .options "$BOARD_HOME/decisions/$qid.json")" '["yes","no"]'
eq "ask kind" "$(jq -r .kind "$BOARD_HOME/decisions/$qid.json")" "question"
eq "decisions --status pending (permission went stale above)" "$("$REPO/bin/board" decisions --status pending --json | jq length)" "1"
"$REPO/bin/board" answer "$qid" yes --by user-1 >/dev/null
eq "answer status" "$(jq -r .status "$BOARD_HOME/decisions/$qid.json")" "answered"
eq "answered_by" "$(jq -r .answered_by "$BOARD_HOME/decisions/$qid.json")" "user-1"
"$REPO/bin/board" mark "$qid" relayed >/dev/null
eq "mark relayed" "$(jq -r .status "$BOARD_HOME/decisions/$qid.json")" "relayed"
eq "relayed_at set" "$(jq -r '.relayed_at > 0' "$BOARD_HOME/decisions/$qid.json")" "true"
"$REPO/bin/board" mark "$qid" bogus >/dev/null 2>&1; eq "mark rejects bad status" "$?" "2"
"$REPO/bin/board" mark "../etc" stale >/dev/null 2>&1; eq "mark rejects bad id" "$?" "1"

# timeout: backdate a pending question
oid="$("$REPO/bin/board" ask --from CP "Old question?")"
jq '.created_at = 1000' "$BOARD_HOME/decisions/$oid.json" > "$T/x" && mv "$T/x" "$BOARD_HOME/decisions/$oid.json"
"$REPO/bin/board-collect" --once --no-notify
eq "old pending decision -> timeout" "$(jq -r .status "$BOARD_HOME/decisions/$oid.json")" "timeout"

# ---- notify (fake curl on PATH)
echo "notify:"
# Fake curl: logs the JSON body, whether given as --data-binary or in a `-K -` config on stdin
# (board-notify keeps the URL and body off argv).
cat > "$T/bin/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_CURL_LOG.args"
data=""
while [ $# -gt 0 ]; do
  case "$1" in
    --data-binary) shift; data="$1" ;;
    -K) shift; [ "$1" = "-" ] && data="$(python3 -c '
import sys
for line in sys.stdin:
    if line.startswith("data-binary = "):
        v = line[len("data-binary = "):].rstrip("\n")[1:-1]
        out, i = [], 0
        while i < len(v):
            if v[i] == "\\" and i + 1 < len(v):
                out.append({"n": "\n", "r": "\r"}.get(v[i + 1], v[i + 1])); i += 2
            else:
                out.append(v[i]); i += 1
        print("".join(out))')" ;;
  esac
  shift
done
[ -n "$data" ] && printf '%s\n' "$data" | jq -c . >> "$FAKE_CURL_LOG"
printf 200
EOF
chmod +x "$T/bin/curl"
export FAKE_CURL_LOG="$T/curl.log"; : > "$FAKE_CURL_LOG"
PATH="$T/bin:$PATH" "$REPO/bin/board" notify "hello"
eq "notify without webhook is a no-op" "$(wc -l < "$FAKE_CURL_LOG" | tr -d ' ')" "0"
echo "SLACK_WEBHOOK_URL=\"http://example.invalid/hook\"" > "$BOARD_CONFIG_DIR/.env"
PATH="$T/bin:$PATH" "$REPO/bin/board" notify "hello"
eq "notify posts JSON" "$(jq -r .text "$FAKE_CURL_LOG")" "hello"
: > "$FAKE_CURL_LOG"
PATH="$T/bin:$PATH" "$REPO/bin/board" notify "<!channel> a & b <https://evil.example|click>"
eq "notify escapes & < > (no Slack pings or links)" "$(jq -r .text "$FAKE_CURL_LOG")" \
  "&lt;!channel&gt; a &amp; b &lt;https://evil.example|click&gt;"
grep -q "example.invalid" "$FAKE_CURL_LOG.args" && bad "board notify: webhook URL not on curl argv" || ok "board notify: webhook URL not on curl argv"
: > "$FAKE_CURL_LOG"
[ -f "$BOARD_HOME/alerts.json" ] && bad "--no-notify runs leave alerts.json alone" || ok "--no-notify runs leave alerts.json alone"
# QA goes from needs-you to busy and back: one alert, then deduped (repeat_minutes).
jq '.status = "busy"' "$CLAUDE_SESSIONS_DIR/$live_pid3.json" > "$T/r" && mv "$T/r" "$CLAUDE_SESSIONS_DIR/$live_pid3.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
jq '.status = "waiting"' "$CLAUDE_SESSIONS_DIR/$live_pid3.json" > "$T/r" && mv "$T/r" "$CLAUDE_SESSIONS_DIR/$live_pid3.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
grep -q "QA needs you" "$FAKE_CURL_LOG" && ok "needs-you transition notifies" || bad "needs-you transition notifies" "$(cat "$FAKE_CURL_LOG")"
jq '.status = "busy"' "$CLAUDE_SESSIONS_DIR/$live_pid3.json" > "$T/r" && mv "$T/r" "$CLAUDE_SESSIONS_DIR/$live_pid3.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
jq '.status = "waiting"' "$CLAUDE_SESSIONS_DIR/$live_pid3.json" > "$T/r" && mv "$T/r" "$CLAUDE_SESSIONS_DIR/$live_pid3.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
eq "re-opened needs-you deduped within repeat_minutes" "$(grep -c "QA needs you" "$FAKE_CURL_LOG")" "1"
"$REPO/bin/board" ask --from INFRA "Rotate keys now?" >/dev/null
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
grep -q "INFRA asks: Rotate keys now?" "$FAKE_CURL_LOG" && ok "new question notifies" || bad "new question notifies" "$(cat "$FAKE_CURL_LOG")"
eq "alerts logged to events.jsonl" "$(jq -s '[.[] | select(.type=="alert" and .channel=="slack" and .ok)] | length > 0' "$BOARD_HOME/events.jsonl")" "true"
out="$("$REPO/bin/board" ack 'decision:*')"; rc=$?
eq "board ack exits 0" "$rc" "0"
grep -q "^acked decision:" <<<"$out" && ok "board ack acks by glob" || bad "board ack acks by glob" "$out"
"$REPO/bin/board" ack 'nothing:*' >/dev/null 2>&1; eq "board ack with no match exits 1" "$?" "1"
"$REPO/bin/board" doctor 2>&1 | grep -q "channels: slack=on" && ok "doctor shows board-notify status" || bad "doctor shows board-notify status"

# ---- alert-storm guard: baseline on a fresh BOARD_HOME
echo "baseline:"
H2="$T/home2"; : > "$FAKE_CURL_LOG"
BOARD_HOME="$H2" "$REPO/bin/board" ask --from QA "Already open before the collector started?" >/dev/null
out="$(BOARD_HOME="$H2" PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once --verbose)"
grep -q "alerts: baseline" <<<"$out" && ok "first run takes a baseline" || bad "first run takes a baseline" "$out"
eq "no alerts sent for conditions already open" "$(wc -l < "$FAKE_CURL_LOG" | tr -d ' ')" "0"
eq "open needs-you acked as baseline" "$(jq -r '."session:QA:needs-you".acked_by' "$H2/alerts.json")" "baseline"
eq "open decision acked as baseline" "$(jq -r '[to_entries[] | select(.key | startswith("decision:")) | .value.acked_by] | unique | join(",")' "$H2/alerts.json")" "baseline"
BOARD_HOME="$H2" "$REPO/bin/board" ask --from QA "Asked after the baseline?" >/dev/null
BOARD_HOME="$H2" PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
grep -q "Asked after the baseline" "$FAKE_CURL_LOG" && ok "new condition after baseline alerts" || bad "new condition after baseline alerts" "$(cat "$FAKE_CURL_LOG")"
eq "...and only that one" "$(wc -l < "$FAKE_CURL_LOG" | tr -d ' ')" "1"
: > "$FAKE_CURL_LOG"
out="$(BOARD_HOME="$H2" PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --baseline)"; rc=$?
eq "--baseline exits 0" "$rc" "0"
grep -q "baseline — 1 open condition" <<<"$out" && ok "--baseline acks the open alert" || bad "--baseline acks the open alert" "$out"
eq "--baseline sends nothing" "$(wc -l < "$FAKE_CURL_LOG" | tr -d ' ')" "0"
H3="$T/home3"
out="$(BOARD_HOME="$H3" PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --baseline)"
[ -f "$H3/alerts.json" ] && ok "--baseline creates alerts.json even when nothing is open" || bad "--baseline creates alerts.json"
rm -f "$BOARD_CONFIG_DIR/.env"

# ---- press (fake tmux on PATH)
echo "press:"
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  display-message) case "$*" in *pane_current_command*) echo "${FAKE_TMUX_CMD-claude}" ;; *) echo "${FAKE_TMUX_SESSION:-CP}" ;; esac ;;
  capture-pane)
    # FAKE_TMUX_SEQ: a directory of screens 1, 2, ... shown one per capture (the last one repeats).
    n=0
    if [ -n "${FAKE_TMUX_SEQ:-}" ]; then
      n=$(( $(cat "$FAKE_TMUX_SEQ/n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_TMUX_SEQ/n"
    fi
    # Optional side effect: the prompt changes while board press is checking (at capture N).
    if [ -n "${FAKE_TMUX_MUTATE:-}" ] && { [ -z "${FAKE_TMUX_MUTATE_AT:-}" ] || [ "$n" = "$FAKE_TMUX_MUTATE_AT" ]; }; then
      jq '.pending.tool_use_id = "tu_other"' "$FAKE_TMUX_MUTATE" > "$FAKE_TMUX_MUTATE.x" && mv "$FAKE_TMUX_MUTATE.x" "$FAKE_TMUX_MUTATE"
    fi
    if [ -n "${FAKE_TMUX_SEQ:-}" ]; then
      f="$FAKE_TMUX_SEQ/$n"; [ -f "$f" ] || f="$(find "$FAKE_TMUX_SEQ" -name '[0-9]*' | sort -n | tail -1)"
      cat "$f"
    else
      printf '%s\n' "${FAKE_TMUX_SCREEN:-}"
    fi ;;
  send-keys)
    # FAKE_TMUX_FAIL_ON: a send-keys whose arguments end with this fails.
    if [ -n "${FAKE_TMUX_FAIL_ON:-}" ] && [[ "$*" == *"$FAKE_TMUX_FAIL_ON" ]]; then exit 1; fi
    # FAKE_TMUX_STALE_ON_SEND: a decision file the collector marks stale while the keys go in.
    if [ -n "${FAKE_TMUX_STALE_ON_SEND:-}" ]; then
      jq '.status = "stale"' "$FAKE_TMUX_STALE_ON_SEND" > "$FAKE_TMUX_STALE_ON_SEND.x" && mv "$FAKE_TMUX_STALE_ON_SEND.x" "$FAKE_TMUX_STALE_ON_SEND"
    fi
    exit 0 ;;
esac
EOF
chmod +x "$T/bin/tmux"
export FAKE_TMUX_LOG="$T/tmux.log"
: > "$FAKE_TMUX_LOG"
B="$REPO/bin/board"
# perm CMD → a new pending permission prompt for CMD; prints "TOOL_USE_ID DECISION_ID".
perm() {  # runs in $(...): keep the counter in a file
  local n; n=$(( $(cat "$T/perm.n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$T/perm.n"
  local tu="tu_p$n" cmd="$1" d
  hook "$(jq -cn --arg s "$SID" --arg tu "$tu" --arg c "$cmd" '{session_id: $s, hook_event_name: "PreToolUse", tool_name: "Bash", tool_use_id: $tu, tool_input: {command: $c}}')"
  hook "$(jq -cn --arg s "$SID" --arg c "$cmd" '{session_id: $s, hook_event_name: "PermissionRequest", tool_name: "Bash", tool_input: {command: $c}}')"
  d="$(jq -rs --arg t "$tu" '[.[] | select(.tool_use_id == $t)] | first | .id // ""' "$BOARD_HOME"/decisions/*.json)"
  echo "$tu $d"
}
screen() { printf ' Bash command\n   %s\n Do you want to proceed?\n ❯ 1. Yes\n   2. No, and tell Claude what to do differently (esc)\n' "$1"; }
press() { PATH="$T/bin:$PATH" "$B" press "$@" >/dev/null 2>&1; }
sent_keys() { grep -c '^send-keys' "$FAKE_TMUX_LOG"; }

C="make deploy-staging --force"
read -r tu d <<<"$(perm "$C")"
[ -n "$d" ] && ok "permission decision created" || bad "permission decision created"
FAKE_TMUX_SCREEN="$(screen "$C")" press CP allow --expect "$tu"
eq "press before any answer -> exit 4" "$?" "4"
eq "...no keys sent" "$(sent_keys)" "0"
eq "...logged as unauthorized" "$(jq -s '[.[] | select(.type=="press" and .result=="unauthorized")] | length' "$BOARD_HOME/events.jsonl")" "1"
eq "...decision left pending" "$(jq -r .status "$BOARD_HOME/decisions/$d.json")" "pending"
"$B" answer "$d" deny --by user-1 >/dev/null
FAKE_TMUX_SCREEN="$(screen "$C")" press CP allow --expect "$tu"
eq "answer deny, press allow -> exit 4" "$?" "4"
"$B" answer "$d" Allow --by user-1 >/dev/null
FAKE_TMUX_SCREEN="$(screen "$C")" press CP allow --expect tu_WRONG
eq "wrong --expect (no such decision) -> exit 4" "$?" "4"
{ grep -v '^approvers:' "$BOARD_CONFIG_DIR/fleet.yaml"; printf 'approvers: [someone-else]\n'; } > "$T/fy" && mv "$T/fy" "$BOARD_CONFIG_DIR/fleet.yaml"
FAKE_TMUX_SCREEN="$(screen "$C")" press CP allow --expect "$tu"
eq "answered_by no longer an approver -> exit 4" "$?" "4"
{ grep -v '^approvers:' "$BOARD_CONFIG_DIR/fleet.yaml"; printf 'approvers: [user-1]\n'; } > "$T/fy" && mv "$T/fy" "$BOARD_CONFIG_DIR/fleet.yaml"
eq "no keys sent by any refusal so far" "$(sent_keys)" "0"
FAKE_TMUX_SCREEN="$(screen "$C")" press CP allow --expect "$tu"
eq "approver's allow -> press ok" "$?" "0"
eq "allow sends 1" "$(grep '^send-keys' "$FAKE_TMUX_LOG" | tail -1)" "send-keys -t %42 1"
eq "decision relayed" "$(jq -r .status "$BOARD_HOME/decisions/$d.json")" "relayed"

read -r tu d <<<"$(perm "terraform destroy -auto-approve")"
"$B" answer "$d" deny --by user-1 >/dev/null
: > "$FAKE_TMUX_LOG"
FAKE_TMUX_SCREEN="$(screen "terraform destroy -auto-approve")" press CP deny --expect "$tu"
eq "deny sends Escape" "$(grep '^send-keys' "$FAKE_TMUX_LOG" | tail -1)" "send-keys -t %42 Escape"

: > "$FAKE_TMUX_LOG"
read -r tu d <<<"$(perm "rm -rf build")"; "$B" answer "$d" allow --by user-1 >/dev/null
FAKE_TMUX_SCREEN="just a shell prompt" press CP allow --expect "$tu"
eq "no permission prompt visible, the prompt still waits -> exit 7" "$?" "7"
eq "...decision pending again, answer cleared, with a note" \
  "$(jq -r '"\(.status)/\(.answer)/\(.answered_by)/\(.press_note | test("scrolled"))"' "$BOARD_HOME/decisions/$d.json")" "pending/null/null/true"
eq "...logged as not_visible" "$(jq -s -r '[.[] | select(.type=="press")] | last | .result' "$BOARD_HOME/events.jsonl")" "not_visible"
eq "...no scroll key without the scrolled indicator" "$(grep -c 'C-End' "$FAKE_TMUX_LOG")" "0"
"$B" answer "$d" allow --by user-1 >/dev/null
eq "...answering again clears the press note" "$(jq -r 'has("press_note")' "$BOARD_HOME/decisions/$d.json")" "false"
FAKE_TMUX_SCREEN="$(screen "rm -rf build")" press CP allow --expect "$tu"
eq "...answered again, the prompt on screen -> press ok" "$?/$(jq -r .status "$BOARD_HOME/decisions/$d.json")" "0/relayed"
: > "$FAKE_TMUX_LOG"
hook "$(jq -cn --arg s "$SID" --arg tu "$tu" '{session_id: $s, hook_event_name: "PostToolUse", tool_name: "Bash", tool_use_id: $tu, tool_input: {}}')"
read -r tu d <<<"$(perm "rm -rf build-b")"; "$B" answer "$d" allow --by user-1 >/dev/null
FAKE_TMUX_SESSION=OTHER FAKE_TMUX_SCREEN="$(screen "rm -rf build-b")" press CP allow --expect "$tu"
eq "pane of another tmux session -> exit 3" "$?" "3"
eq "...status_reason unknown_session" "$(jq -r .status_reason "$BOARD_HOME/decisions/$d.json")" "unknown_session"
read -r tu d <<<"$(perm "rm -rf build-c")"; "$B" answer "$d" allow --by user-1 >/dev/null
FAKE_TMUX_SCREEN="$(screen "rm -rf /")" press CP allow --expect "$tu"
eq "prompt on screen is about other input -> exit 3" "$?" "3"
eq "...stale with status_reason screen, the answer kept" "$(jq -r '.status + "/" + .status_reason + "/" + .answer' "$BOARD_HOME/decisions/$d.json")" "stale/screen/allow"
read -r tu d <<<"$(perm "rm -rf build-d")"; "$B" answer "$d" allow --by user-1 >/dev/null
jq '.pending.summary_sha256 = "0000"' "$BOARD_HOME/state/$SID.json" > "$T/sx" && mv "$T/sx" "$BOARD_HOME/state/$SID.json"
FAKE_TMUX_SCREEN="$(screen "rm -rf build-d")" press CP allow --expect "$tu"
eq "pending input sha differs from the decision -> exit 3" "$?" "3"
eq "...status_reason moved_on" "$(jq -r .status_reason "$BOARD_HOME/decisions/$d.json")" "moved_on"
read -r tu d <<<"$(perm "rm -rf build-e")"; "$B" answer "$d" allow --by user-1 >/dev/null
FAKE_TMUX_MUTATE="$BOARD_HOME/state/$SID.json" FAKE_TMUX_SCREEN="$(screen "rm -rf build-e")" press CP allow --expect "$tu"
eq "prompt changed while checking (re-read before send-keys) -> exit 3" "$?" "3"
eq "no keys sent by any exit-3 refusal" "$(sent_keys)" "0"
# A wrapped command in the pane still matches (whitespace is ignored).
read -r tu d <<<"$(perm "kubectl rollout restart deployment/web --namespace staging")"; "$B" answer "$d" allow --by user-1 >/dev/null
FAKE_TMUX_SCREEN="$(screen $'kubectl rollout restart deployment/web --name\n     space staging')" press CP allow --expect "$tu"
eq "wrapped command on screen -> press ok" "$?" "0"
# The keys went in, but the collector marked it stale meanwhile: press still marks it relayed.
read -r tu d <<<"$(perm "rm -rf build-r")"; "$B" answer "$d" allow --by user-1 >/dev/null
FAKE_TMUX_STALE_ON_SEND="$BOARD_HOME/decisions/$d.json" FAKE_TMUX_SCREEN="$(screen "rm -rf build-r")" press CP allow --expect "$tu"
eq "stale during the key press -> relayed (press overrides stale only)" "$?/$(jq -r .status "$BOARD_HOME/decisions/$d.json")" "0/relayed"
hook "$(jq -cn --arg s "$SID" --arg tu "$tu" '{session_id: $s, hook_event_name: "PostToolUse", tool_name: "Bash", tool_use_id: $tu, tool_input: {}}')"

# Edit prompts show only the file name: the basename may bind, if it has >= 6 characters.
editperm() {  # editperm TOOL_USE_ID PATH → decision id
  hook "$(jq -cn --arg s "$SID" --arg tu "$1" --arg f "$2" '{session_id: $s, hook_event_name: "PreToolUse", tool_name: "Edit", tool_use_id: $tu, tool_input: {file_path: $f, old_string: "a", new_string: "b"}}')"
  hook "$(jq -cn --arg s "$SID" --arg f "$2" '{session_id: $s, hook_event_name: "PermissionRequest", tool_name: "Edit", tool_input: {file_path: $f, old_string: "a", new_string: "b"}}')"
  jq -rs --arg t "$1" '[.[] | select(.tool_use_id == $t)] | first | .id // ""' "$BOARD_HOME"/decisions/*.json
}
d="$(editperm tu_e1 /w/src/components/ReportTable.tsx)"; "$B" answer "$d" allow --by user-1 >/dev/null
FAKE_TMUX_SCREEN=$' Edit file\n Do you want to make this edit to ReportTable.tsx?\n ❯ 1. Yes\n' press CP allow --expect tu_e1
eq "edit prompt: basename (>= 6 chars) binds" "$?" "0"
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PostToolUse", tool_name: "Edit", tool_use_id: "tu_e1", tool_input: {}}')"
d="$(editperm tu_e2 /w/src/a.c)"; "$B" answer "$d" allow --by user-1 >/dev/null
FAKE_TMUX_SCREEN=$' Edit file\n Do you want to make this edit to a.c?\n ❯ 1. Yes\n' press CP allow --expect tu_e2
eq "edit prompt: a short basename does not bind -> exit 3" "$?" "3"
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PostToolUse", tool_name: "Edit", tool_use_id: "tu_e2", tool_input: {}}')"
# A decision or pending prompt without an input hash cannot be bound.
read -r tu d <<<"$(perm "rm -rf build-f")"; "$B" answer "$d" allow --by user-1 >/dev/null
jq 'del(.summary_sha256)' "$BOARD_HOME/decisions/$d.json" > "$T/dx" && mv "$T/dx" "$BOARD_HOME/decisions/$d.json"
FAKE_TMUX_SCREEN="$(screen "rm -rf build-f")" press CP allow --expect "$tu"
eq "decision without summary_sha256 -> exit 3" "$?" "3"

# Truncated summaries need --allow-truncated.
LONGC="echo $(printf 'word%.0s ' $(seq 1 60))"
read -r tu d <<<"$(perm "$LONGC")"; "$B" answer "$d" allow --by user-1 >/dev/null
eq "decision carries summary_truncated" "$(jq -r .summary_truncated "$BOARD_HOME/decisions/$d.json")" "true"
eq "decision carries summary_len" "$(jq -r .summary_len "$BOARD_HOME/decisions/$d.json")" "${#LONGC}"
eq "decision carries sha256 of the full input" "$(jq -r .summary_sha256 "$BOARD_HOME/decisions/$d.json")" "$(printf '%s' "$LONGC" | sha256sum | cut -d' ' -f1)"
FAKE_TMUX_SCREEN="$(screen "$LONGC")" press CP allow --expect "$tu"
eq "truncated summary -> exit 4" "$?" "4"
FAKE_TMUX_SCREEN="$(screen "$LONGC")" press CP allow --expect "$tu" --allow-truncated
eq "truncated + --allow-truncated -> ok" "$?" "0"
last_tu="$tu"

# Ambiguous permission prompts: never guess.
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PreToolUse", tool_name: "Bash", tool_use_id: "tu_a1", tool_input: {command: "npm test"}}')"
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PreToolUse", tool_name: "Bash", tool_use_id: "tu_a2", tool_input: {command: "npm run lint"}}')"
nd="$(ndec)"
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PermissionRequest", tool_name: "Bash", tool_input: {command: "npm publish"}}')"
eq "no tool_use_id, several Bash in flight -> ambiguous" "$(st .pending.ambiguous)" "true"
eq "...no pressable decision" "$(ndec)" "$nd"
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PermissionRequest", tool_name: "Bash", tool_input: {command: "npm run lint"}}')"
eq "exact input match among several -> bound" "$(st .pending.tool_use_id)" "tu_a2"
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PostToolUse", tool_name: "Bash", tool_use_id: "tu_a2", tool_input: {command: "npm run lint"}}')"
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PreToolUse", tool_name: "Bash", tool_use_id: "tu_a3", tool_input: {command: "npm ci"}}')"
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "Notification", notification_type: "permission_prompt", message: "Claude needs your permission"}')"
eq "notification with several in flight -> ambiguous" "$(st .pending.ambiguous)" "true"
for t in tu_a1 tu_a3 "$last_tu"; do
  hook "$(jq -cn --arg s "$SID" --arg t "$t" '{session_id: $s, hook_event_name: "PostToolUse", tool_name: "Bash", tool_use_id: $t, tool_input: {}}')"
done
# ---- pane questions (AskUserQuestion pickers), in the shape Claude Code sends them
echo "pane questions:"
# askq TOOL_USE_ID QUESTIONS_JSON → PreToolUse + PermissionRequest (no tool_use_id), prints the decision id
askq() {
  hook "$(jq -cn --arg s "$SID" --arg tu "$1" --argjson q "$2" '{session_id: $s, hook_event_name: "PreToolUse", permission_mode: "default", tool_name: "AskUserQuestion", tool_use_id: $tu, tool_input: {questions: $q}}')"
  hook "$(jq -cn --arg s "$SID" --argjson q "$2" '{session_id: $s, hook_event_name: "PermissionRequest", permission_mode: "default", tool_name: "AskUserQuestion", tool_input: {questions: $q}}')"
  jq -rs --arg t "$1" '[.[] | select(.tool_use_id == $t)] | first | .id // ""' "$BOARD_HOME"/decisions/*.json
}
qdone() { hook "$(jq -cn --arg s "$SID" --arg t "$1" '{session_id: $s, hook_event_name: "PostToolUse", tool_name: "AskUserQuestion", tool_use_id: $t, tool_input: {}}')"; }
dj() { jq -r "$2" "$BOARD_HOME/decisions/$1.json"; }
Q1='[{"question":"Which color do you prefer for the test?","header":"Color","options":[{"label":"Red","description":"warm"},{"label":"Blue","description":"cool"},{"label":"Green","description":"natural"}],"multiSelect":false}]'
nd="$(ndec)"
d="$(askq tu_q1 "$Q1")"
eq "AskUserQuestion -> one decision (deduped across PreToolUse + PermissionRequest)" "$(ndec)" "$((nd + 1))"
eq "...kind question, source pane" "$(dj "$d" '.kind + "/" + .source')" "question/pane"
eq "...question text" "$(dj "$d" .question)" "Which color do you prefer for the test?"
eq "...option labels" "$(dj "$d" '.options | join(",")')" "Red,Blue,Green"
eq "...descriptions and header kept" "$(dj "$d" '.questions[0] | .header + ":" + .options[1].description')" "Color:cool"
eq "...single question, single-select -> pressable" "$(dj "$d" .pressable)" "true"
eq "...bound to the tool_use_id" "$(dj "$d" .tool_use_id)" "tu_q1"
eq "...state needs-you on the picker" "$(st .state)/$(st .pending.tool_use_id)" "needs-you/tu_q1"
eq "...not a permission card" "$(jq -s '[.[] | select(.tool_name == "AskUserQuestion" and .kind == "permission")] | length' "$BOARD_HOME"/decisions/*.json)" "0"

# answer -> option index / free text
"$B" answer "$d" "Blue" --by user-1 >/dev/null
eq "answer 'Blue' -> option 2, relay press" "$(dj "$d" '"\(.answer_index)/\(.relay)"')" "2/press"
"$B" answer "$d" " green " --by user-1 >/dev/null
eq "answer ' green ' -> option 3 (case-insensitive, trimmed)" "$(dj "$d" .answer_index)" "3"
"$B" answer "$d" "Purple, please" --by user-1 >/dev/null
eq "free text -> no index, relay press" "$(dj "$d" '"\(.answer_index)/\(.relay)"')" "null/press"
"$B" answer "$d" $'two\nlines' --by user-1 >/dev/null
eq "multi-line free text -> relay operator" "$(dj "$d" .relay)" "operator"
"$B" answer "$d" "$(printf 'y%.0s' $(seq 1 201))" --by user-1 >/dev/null
eq "free text over 200 chars -> relay operator" "$(dj "$d" .relay)" "operator"

qscreen() {  # qscreen [focus ROW4_TEXT] — the picker as Claude Code draws it (focus on row 1, or on row 4)
  local c1='❯' r4='  4. Type something.'
  [ "${1:-}" = focus ] && { c1=' '; r4="❯ 4. $2"; }
  printf ' ☐ Color\n\nWhich color do you prefer for the test?\n\n%s 1. Red\n     warm\n  2. Blue\n     cool\n  3. Green\n     natural\n%s\n───\n  5. Chat about this\n\nEnter to select · ↑/↓ to navigate · Esc to cancel\n' "$c1" "$r4"
}
"$B" answer "$d" "Purple, please" --by user-1 >/dev/null
: > "$FAKE_TMUX_LOG"
FAKE_TMUX_SCREEN="$(qscreen)" press CP allow --expect tu_q1
eq "allow/deny never targets a pane question -> exit 4" "$?" "4"
FAKE_TMUX_SCREEN="just a shell prompt" press CP answer --expect tu_q1
eq "no picker on screen, the question still waits -> exit 7" "$?" "7"
eq "...decision pending again (clickable), not stale" "$(dj "$d" .status)/$(dj "$d" .answer)" "pending/null"
eq "...no keys sent" "$(sent_keys)" "0"
qdone tu_q1

d="$(askq tu_q2 "$Q1")"
FAKE_TMUX_SCREEN="$(qscreen)" press CP answer --expect tu_q2
eq "press before any answer -> exit 4" "$?" "4"
"$B" answer "$d" "Blue" --by intruder >/dev/null 2>&1
FAKE_TMUX_SCREEN="$(qscreen)" press CP answer --expect tu_q2
eq "non-approver's answer -> exit 4" "$?" "4"
"$B" answer "$d" "Blue" --by user-1 >/dev/null
FAKE_TMUX_SCREEN=$' Some other question?\n❯ 1. Yes\n  2. No\nEnter to select · ↑/↓ to navigate · Esc to cancel' press CP answer --expect tu_q2
eq "picker shows another question -> exit 3" "$?" "3"
eq "no keys sent by any refusal" "$(sent_keys)" "0"
qdone tu_q2

d="$(askq tu_q3 "$Q1")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
jq '.pending.summary_sha256 = "0000"' "$BOARD_HOME/state/$SID.json" > "$T/sx" && mv "$T/sx" "$BOARD_HOME/state/$SID.json"
FAKE_TMUX_SCREEN="$(qscreen)" press CP answer --expect tu_q3
eq "pending sha differs -> exit 3" "$?" "3"
qdone tu_q3

d="$(askq tu_q4 "$Q1")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
FAKE_TMUX_SCREEN="$(qscreen)" press CP answer --expect tu_q4
eq "approver's option -> press ok" "$?" "0"
eq "...sends the option's number key" "$(grep '^send-keys' "$FAKE_TMUX_LOG" | tail -1)" "send-keys -t %42 2"
eq "...one send-keys" "$(sent_keys)" "1"
eq "...decision relayed" "$(dj "$d" .status)" "relayed"
qdone tu_q4

# Free text runs three screen checks: before any key, after the "Type something" key, after typing.
# seqpress TOOL_USE_ID SCREEN1 SCREEN2 SCREEN3 — one fake-tmux screen per capture, in order.
seqpress() {
  local tu="$1"; shift
  rm -rf "$T/seq"; mkdir -p "$T/seq"
  local i=0 sc
  for sc in "$@"; do i=$((i + 1)); printf '%s\n' "$sc" > "$T/seq/$i"; done
  : > "$FAKE_TMUX_LOG"
  BOARD_PRESS_SETTLE=0 FAKE_TMUX_SEQ="$T/seq" press CP answer --expect "$tu"
}
keys_sent() { grep '^send-keys' "$FAKE_TMUX_LOG" | tr '\n' '|'; }
junk() { printf 'scrolled output line %s\n' $(seq 1 40); }
U="$(qscreen)"; F="$(qscreen focus "Type something.")"

d="$(askq tu_q5 "$Q1")"; "$B" answer "$d" "purple; echo hi" --by user-1 >/dev/null
seqpress tu_q5 "$U" "$F" "$(qscreen focus "purple; echo hi")"
eq "free text -> press ok" "$?" "0"
eq "...Type something, literal text, Enter" "$(keys_sent)" "send-keys -t %42 4|send-keys -t %42 -l -- purple; echo hi|send-keys -t %42 Enter|"
qdone tu_q5
d="$(askq tu_q6 "$Q1")"; "$B" answer "$d" "purple" --by user-1 >/dev/null
seqpress tu_q6 "$U" "$U"
eq "picker did not focus Type something -> exit 5 (partial)" "$?" "5"
eq "...nothing typed (only the row key)" "$(keys_sent)" "send-keys -t %42 4|"
eq "...decision stale" "$(dj "$d" .status)" "stale"
eq "...logged as partial" "$(jq -s -r '[.[] | select(.type=="press")] | last | .result' "$BOARD_HOME/events.jsonl")" "partial"
eq "...status_reason partial" "$(dj "$d" .status_reason)" "partial"
qdone tu_q6
d="$(askq tu_q6b "$Q1")"; "$B" answer "$d" "purple" --by user-1 >/dev/null
seqpress tu_q6b "$U" "$F
$(junk)"
eq "stale '❯ 4.' line above the last lines does not pass -> exit 5" "$?" "5"
eq "...nothing typed" "$(keys_sent)" "send-keys -t %42 4|"
qdone tu_q6b
d="$(askq tu_q6c "$Q1")"; "$B" answer "$d" "warm" --by user-1 >/dev/null
seqpress tu_q6c "$U" "$F" "$F"
eq "typed text visible only elsewhere (a description) -> exit 5, no Enter" "$?/$(keys_sent)" "5/send-keys -t %42 4|send-keys -t %42 -l -- warm|"
qdone tu_q6c
d="$(askq tu_q6d "$Q1")"; "$B" answer "$d" "purple" --by user-1 >/dev/null
seqpress tu_q6d "$U" "$F" "$(qscreen focus purple)
$(junk)
\$ "
eq "picker gone after typing (text only in history) -> exit 5, no Enter" "$?/$(keys_sent)" "5/send-keys -t %42 4|send-keys -t %42 -l -- purple|"
qdone tu_q6d
d="$(askq tu_q6e "$Q1")"; "$B" answer "$d" "purple" --by user-1 >/dev/null
FAKE_TMUX_MUTATE_AT=3 FAKE_TMUX_MUTATE="$BOARD_HOME/state/$SID.json" seqpress tu_q6e "$U" "$F" "$(qscreen focus purple)"
eq "prompt changed after typing -> exit 5, no Enter" "$?/$(keys_sent)" "5/send-keys -t %42 4|send-keys -t %42 -l -- purple|"
qdone tu_q6e
d="$(askq tu_q6k "$Q1")"; "$B" answer "$d" "some" --by user-1 >/dev/null
seqpress tu_q6k "$U" "$F" "$F"
eq "short answer 'some' never matches the 'Type something' placeholder -> exit 5, no Enter" "$?/$(keys_sent)" "5/send-keys -t %42 4|send-keys -t %42 -l -- some|"
qdone tu_q6k
d="$(askq tu_q6l "$Q1")"; "$B" answer "$d" "some" --by user-1 >/dev/null
seqpress tu_q6l "$U" "$F" "$(qscreen focus some)"
eq "'some' typed into the row -> ok" "$?/$(keys_sent)" "0/send-keys -t %42 4|send-keys -t %42 -l -- some|send-keys -t %42 Enter|"
qdone tu_q6l
d="$(askq tu_q6m "$Q1")"; "$B" answer "$d" "purple" --by user-1 >/dev/null
FAKE_TMUX_MUTATE_AT=2 FAKE_TMUX_MUTATE="$BOARD_HOME/state/$SID.json" seqpress tu_q6m "$U" "$F" "$(qscreen focus purple)"
eq "prompt changed before typing -> exit 5, nothing typed" "$?/$(keys_sent)" "5/send-keys -t %42 4|"
qdone tu_q6m
d="$(askq tu_q6n "$Q1")"; "$B" answer "$d" "purple" --by user-1 >/dev/null
FAKE_TMUX_FAIL_ON=Enter seqpress tu_q6n "$U" "$F" "$(qscreen focus purple)"
eq "send-keys fails after the first key -> exit 5 (not 1)" "$?" "5"
eq "...decision stale, not left answered" "$(dj "$d" .status)" "stale"
qdone tu_q6n
d="$(askq tu_q6o "$Q1")"; "$B" answer "$d" "purple" --by user-1 >/dev/null
FAKE_TMUX_FAIL_ON=" 4" seqpress tu_q6o "$U" "$F" "$(qscreen focus purple)"
eq "send-keys fails on the Type something key -> exit 5, stale" "$?/$(dj "$d" .status)" "5/stale"
qdone tu_q6o
d="$(askq tu_q6f "$Q1")"; "$B" answer "$d" "purple" --by user-1 >/dev/null
seqpress tu_q6f "$(qscreen | sed 's/4\. Type something\./4. Chat about this/')"
eq "row 4 is not Type something -> exit 3, nothing sent" "$?/$(keys_sent)" "3/"
qdone tu_q6f
d="$(askq tu_q6g "$Q1")"; "$B" answer "$d" "purple;" --by user-1 >/dev/null
seqpress tu_q6g "$U" "$F" "$(qscreen focus "purple;")"
eq "trailing ';' is escaped for tmux" "$?/$(keys_sent)" '0/send-keys -t %42 4|send-keys -t %42 -l -- purple\;|send-keys -t %42 Enter|'
qdone tu_q6g
d="$(askq tu_q6h "$Q1")"; "$B" answer "$d" 'purple\;' --by user-1 >/dev/null
eq "text ending in a backslash and ';' -> relay operator" "$(dj "$d" .relay)" "operator"
qdone tu_q6h

# board resolve: a board ask answered in the pane (or another session) is closed, never auto-closed.
ask_id() { "$B" ask --from CP "Ship it behind a flag?" --option yes --option no; }
# in_session CODE CMD... — run CMD as if inside CODE's Claude session: a registry file for this test
# shell (an ancestor of the board process), removed again so the collector never sees it.
in_session() {
  local code="$1" rc; shift
  printf '{"pid":%d,"sessionId":"s-caller","name":"%s","tmux":"%s:@9.%%99","status":"busy"}\n' "$$" "$code" "$code" > "$CLAUDE_SESSIONS_DIR/$$.json"
  "$@"; rc=$?
  rm -f "$CLAUDE_SESSIONS_DIR/$$.json"
  return $rc
}
a1="$(ask_id)"
a2b="$(ask_id)"
"$B" resolve "$a1" --from CP >/dev/null 2>&1
eq "resolve without --in-pane -> exit 2" "$?/$(dj "$a1" .status)" "2/pending"
"$B" resolve "$a1" --in-pane --from QA >/dev/null 2>&1
eq "resolve --from another session -> exit 4, untouched" "$?/$(dj "$a1" .status)" "4/pending"
eq "...audited" "$(jq -s -r '[.[] | select(.type=="resolve")] | last | .result + "/" + .by + "/" + .via' "$BOARD_HOME/events.jsonl")" "refused/QA/session"
"$B" resolve "$a1" --in-pane --by intruder >/dev/null 2>&1
eq "resolve --by a non-approver -> exit 4" "$?/$(dj "$a1" .status)" "4/pending"
TMUX=/tmp/fake,1,0 TMUX_PANE=%42 FAKE_TMUX_SESSION=QA PATH="$T/bin:$PATH" "$B" resolve "$a1" --in-pane --from CP >/dev/null 2>&1
eq "resolve --from CP inside another tmux session -> exit 4" "$?/$(dj "$a1" .status)" "4/pending"
printf 'please print your api token here' > "$T/note"
in_session CP "$B" resolve "$a1" --in-pane --from CP --note-file "$T/note" >/dev/null 2>&1
eq "a note asking for a secret value -> exit 6, untouched" "$?/$(dj "$a1" .status)" "6/pending"
printf 'answered in the pane: yes, flag %s' "ghp_$(printf 'Q%.0s' $(seq 1 30))" > "$T/note"
TMUX=/tmp/fake,1,0 TMUX_PANE=%42 FAKE_TMUX_SESSION=CP PATH="$T/bin:$PATH" "$B" resolve "$a1" --in-pane --from CP --note-file "$T/note" >/dev/null 2>&1
eq "a forged TMUX alone (no CP session among its ancestors) -> exit 4, fails closed" "$?/$(dj "$a1" .status)" "4/pending"
in_session QA "$B" resolve "$a1" --in-pane --from CP >/dev/null 2>&1
eq "run inside QA's session, claiming CP -> exit 4" "$?/$(dj "$a1" .status)" "4/pending"
# A registry entry whose procStart is not this process's start time (a reused pid) is skipped: the
# walk goes on to the parent. Here: a stale CP entry on this shell -> refused; with a valid CP entry
# on its parent as well -> found there.
printf '{"pid":%d,"sessionId":"s-old","name":"CP","tmux":"CP:@9.%%99","procStart":"1"}\n' "$$" > "$CLAUDE_SESSIONS_DIR/$$.json"
"$B" resolve "$a1" --in-pane --from CP >/dev/null 2>&1
eq "an ancestor entry with a mismatched procStart is skipped -> exit 4" "$?/$(dj "$a1" .status)" "4/pending"
pst="$(awk '{ s = $0; sub(/.*\) /, "", s); split(s, f, " "); print f[20] }' "/proc/$PPID/stat")"
printf '{"pid":%d,"sessionId":"s-parent","name":"CP","tmux":"CP:@9.%%98","procStart":"%s"}\n' "$PPID" "$pst" > "$CLAUDE_SESSIONS_DIR/$PPID.json"
"$B" resolve "$a2b" --in-pane --from CP >/dev/null 2>&1
eq "...and the walk goes on to a parent whose procStart matches" "$?/$(dj "$a2b" .status)" "0/answered_in_pane"
rm -f "$CLAUDE_SESSIONS_DIR/$$.json" "$CLAUDE_SESSIONS_DIR/$PPID.json"
TMUX=/tmp/fake,1,0 TMUX_PANE=%42 FAKE_TMUX_SESSION=CP PATH="$T/bin:$PATH" in_session CP "$B" resolve "$a1" --in-pane --from CP --note-file "$T/note" >/dev/null
eq "the asking session resolves it -> answered_in_pane" "$?/$(dj "$a1" '.status + "/" + .resolved_by + "/" + .resolved_via')" "0/answered_in_pane/CP/session"
eq "...the note scrubbed" "$(dj "$a1" '.resolve_note | test("ghp_") | not')/$(dj "$a1" '.resolve_note | startswith("answered in the pane")')" "true/true"
"$B" resolve "$a1" --in-pane --by user-1 >/dev/null 2>&1
eq "resolving a final decision -> exit 1" "$?" "1"
a2="$(ask_id)"
"$B" resolve "$a2" --in-pane --by user-1 >/dev/null
eq "an approver resolves it" "$?/$(dj "$a2" '.status + "/" + .resolved_via')" "0/answered_in_pane/approver"
tk="$("$B" task new --owner CP "Gate test")"
g1="$("$B" ask --from CP --task "$tk" --stage plan --gate owner "Plan ok?")"
"$B" resolve "$g1" --in-pane --by user-1 >/dev/null 2>&1
eq "a gate is never resolved from the pane -> exit 2" "$?/$(dj "$g1" .status)" "2/pending"
rm -f "$BOARD_HOME/tasks/$tk.json" "$BOARD_HOME/decisions/$g1.json"  # later checks count tasks
pq="$(jq -rs '[.[] | select(.source == "pane")] | first | .id' "$BOARD_HOME"/decisions/*.json)"
"$B" resolve "$pq" --in-pane --by user-1 >/dev/null 2>&1
eq "a pane question is not a board ask -> exit 2" "$?" "2"
# The collector's hint: a new prompt after the ask, then a finished turn.
a3="$(ask_id)"
c3="$(dj "$a3" .created_at)"
jq --argjson p "$((c3 + 1000))" '.last_prompt_at = $p | .last_stop_at = null' "$BOARD_HOME/state/$SID.json" > "$T/sx" && mv "$T/sx" "$BOARD_HOME/state/$SID.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once --no-notify >/dev/null 2>&1
eq "a new prompt but no finished turn -> no hint" "$(dj "$a3" '.hint // "none"')" "none"
jq --argjson s "$((c3 + 5000))" '.last_stop_at = $s' "$BOARD_HOME/state/$SID.json" > "$T/sx" && mv "$T/sx" "$BOARD_HOME/state/$SID.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once --no-notify >/dev/null 2>&1
eq "...then a finished turn -> hint, still pending" "$(dj "$a3" '.hint + "/" + .status + "/" + (.hint_at | tostring)')" "maybe_answered_in_session/pending/$((c3 + 5000))"
a4="$(ask_id)"
c4="$(dj "$a4" .created_at)"
jq --argjson p "$((c4 - 2000))" --argjson s "$((c4 - 1000))" '.last_prompt_at = $p | .last_stop_at = $s' "$BOARD_HOME/state/$SID.json" > "$T/sx" && mv "$T/sx" "$BOARD_HOME/state/$SID.json"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once --no-notify >/dev/null 2>&1
eq "a prompt from before the ask -> no hint" "$(dj "$a4" '.hint // "none"')" "none"
in_session CP "$B" resolve "$a3" --in-pane --from CP >/dev/null
eq "resolving removes the hint" "$(dj "$a3" 'has("hint")')" "false"
in_session CP "$B" resolve "$a4" --in-pane --from CP >/dev/null
# An approver's board answer waiting for its relay is never dropped, by --from or --by.
a5="$(ask_id)"
"$B" answer "$a5" yes --by user-1 >/dev/null
in_session CP "$B" resolve "$a5" --in-pane --from CP >/dev/null 2>"$T/err"
eq "--from on an answered ask -> exit 1, still answered" "$?/$(dj "$a5" .status)" "1/answered"
grep -q "already answered: it will be relayed" "$T/err" && ok "...says it will be relayed" || bad "...says it will be relayed" "$(cat "$T/err")"
"$B" resolve "$a5" --in-pane --by user-1 >/dev/null 2>&1
eq "--by on an answered ask -> exit 1, still answered" "$?/$(dj "$a5" '.status + "/" + .answer')" "1/answered/yes"
"$B" mark "$a5" relayed >/dev/null
# A final status is never re-marked to another one.
"$B" mark "$a1" relayed >/dev/null 2>&1
eq "mark relayed on answered_in_pane -> exit 1, unchanged" "$?/$(dj "$a1" .status)" "1/answered_in_pane"
"$B" mark "$a5" stale >/dev/null 2>&1
eq "mark stale on relayed -> exit 1, unchanged" "$?/$(dj "$a5" .status)" "1/relayed"
"$B" mark "$a5" relayed >/dev/null
eq "marking the same final status again is a no-op" "$?/$(dj "$a5" .status)" "0/relayed"
s5="$(ask_id)"; "$B" mark "$s5" stale >/dev/null
MARK_OVER_STALE=1 "$B" mark "$s5" relayed >/dev/null 2>&1
eq "MARK_OVER_STALE from the environment does not let board mark override stale" "$?/$(dj "$s5" .status)" "1/stale"
# The collector never overwrites a decision that changed under it (no lock): the hint write re-reads.
a6="$(ask_id)"
c6="$(dj "$a6" .created_at)"
jq --argjson p "$((c6 + 1000))" --argjson s "$((c6 + 2000))" '.last_prompt_at = $p | .last_stop_at = $s' "$BOARD_HOME/state/$SID.json" > "$T/sx" && mv "$T/sx" "$BOARD_HOME/state/$SID.json"
python3 - "$REPO/lib" "$BOARD_HOME" "$a6" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
import collect
home, did = sys.argv[2], sys.argv[3]
path = os.path.join(home, "decisions", did + ".json")
orig = collect.maybe_answered
def racing(d, by_code, h):  # an approver resolves it while the collector decides the hint
    rec = collect.read_json(path); rec["status"] = "answered_in_pane"; collect.write_json_atomic(path, rec)
    return orig(d, by_code, h)
collect.maybe_answered = racing
cfg = collect.fleet.load_fleet()
sessions = collect.build_sessions(cfg, home, "")
collect.process_decisions(cfg, home, sessions, collect.now_ms())
PY
eq "a concurrent resolve survives the collector's hint write" "$(dj "$a6" '.status + "/" + (.hint // "none")')" "answered_in_pane/none"
# A TUI scrolled up (new messages above the open picker) shows "N new messages (ctrl+End) ↓" at the
# bottom instead of the picker: board press sends C-End once, looks again, then runs every check.
SCR="$(junk)
  4 new messages (ctrl+End) ↓"
d="$(askq tu_s1 "$Q1")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
seqpress tu_s1 "$SCR" "$U"
eq "scrolled pane -> C-End, then the picker -> press ok" "$?/$(keys_sent)" "0/send-keys -t %42 C-End|send-keys -t %42 2|"
eq "...decision relayed" "$(dj "$d" .status)" "relayed"
eq "...no status_reason on a relayed decision" "$(dj "$d" 'has("status_reason")')" "false"
eq "...the scroll is logged" "$(jq -s -r '[.[] | select(.type=="press" and .result=="scroll")] | length' "$BOARD_HOME/events.jsonl")" "1"
qdone tu_s1
d="$(askq tu_s2 "$Q1")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
seqpress tu_s2 "$SCR" "$SCR"
eq "still scrolled after C-End -> exit 7, only the scroll key sent" "$?/$(keys_sent)" "7/send-keys -t %42 C-End|"
eq "...not stale: pending, answer cleared, note says scrolled" "$(dj "$d" '"\(.status)/\(.answer)/\(.press_note | test("scrolled"))"')" "pending/null/true"
eq "...the collector keeps it pending while the session waits" "$(PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once --no-notify >/dev/null 2>&1; dj "$d" .status)" "pending"
qdone tu_s2
eq "...stale once the session moved on (PostToolUse)" "$(PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once --no-notify >/dev/null 2>&1; dj "$d" .status)" "stale"
eq "...stale once the session moved on, status_reason moved_on" "$(dj "$d" .status_reason)" "moved_on"
d="$(askq tu_s3 "$Q1")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
FAKE_TMUX_MUTATE_AT=2 FAKE_TMUX_MUTATE="$BOARD_HOME/state/$SID.json" seqpress tu_s3 "$SCR" "$SCR"
eq "scrolled, and the state moved on meanwhile -> exit 3, stale" "$?/$(dj "$d" .status)" "3/stale"
eq "...status_reason moved_on" "$(dj "$d" .status_reason)" "moved_on"
qdone tu_s3
d="$(askq tu_s4 "$Q1")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
printf 'press:\n  scroll_key: ""\n' >> "$BOARD_CONFIG_DIR/fleet.yaml"
seqpress tu_s4 "$SCR" "$U"
eq "press.scroll_key empty -> no key sent, exit 7" "$?/$(keys_sent)" "7/"
sed -i '/^press:$/,$d' "$BOARD_CONFIG_DIR/fleet.yaml"
qdone tu_s4
# Only a whole-line indicator in the last 2 non-blank lines scrolls: text the session writes never does.
pn=0
for probe in "we have 3 new messages for you" "see the list below ↓" "ctrl+End to jump" "I wrote: 3 new messages (ctrl+End) ↓"; do
  pn=$((pn + 1))
  d="$(askq "tu_pr$pn" "$Q1")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
  seqpress "tu_pr$pn" "$(junk)
$probe" "$U"
  eq "no scroll key for '$probe' -> exit 7, nothing sent" "$?/$(keys_sent)" "7/"
  qdone "tu_pr$pn"
done
d="$(askq tu_z2 "$Q1")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
seqpress tu_z2 "$(junk)
  4 new messages (ctrl+End) ↓
line after
another line" "$U"
eq "indicator above the last 2 lines -> no scroll key" "$?/$(keys_sent)" "7/"
qdone tu_z2
# The pane must run Claude Code's TUI: a shell there would read the keys as readline input.
d="$(askq tu_z3 "$Q1")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
FAKE_TMUX_CMD=bash seqpress tu_z3 "$SCR" "$U"
eq "pane runs a shell -> exit 3, nothing sent, stale" "$?/$(keys_sent)/$(dj "$d" .status)" "3//stale"
qdone tu_z3
d="$(askq tu_z4 "$Q1")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
FAKE_TMUX_CMD=claude.exe seqpress tu_z4 "$U"
eq "claude.exe counts as the TUI" "$?/$(keys_sent)" "0/send-keys -t %42 2|"
qdone tu_z4
# An unauthorized answer, then an approver's: status_reason not_approver is cleared.
d="$(askq tu_z5 "$Q1")"
"$B" answer "$d" "Blue" --by intruder >/dev/null 2>&1
eq "a non-approver's answer -> not_approver" "$(dj "$d" .status_reason)" "not_approver"
"$B" answer "$d" "Blue" --by user-1 >/dev/null
eq "...an approver's answer clears it" "$(dj "$d" '.status + "/" + (has("status_reason") | tostring)')" "answered/false"
qdone tu_z5

# board mark --reason: only the fixed codes.
d="$(askq tu_mk1 "$Q1")"
"$B" mark "$d" stale --reason "rm -rf /" >/dev/null 2>&1
eq "mark --reason with free text -> exit 2, untouched" "$?/$(dj "$d" .status)" "2/pending"
"$B" mark "$d" stale --reason operator >/dev/null
eq "mark --reason operator" "$(dj "$d" '.status + "/" + .status_reason')" "stale/operator"
qdone tu_mk1

# --option: the clicked button's number must match the label.
d="$(askq tu_q6i "$Q1")"
"$B" answer "$d" "Blue" --by user-1 --option 1 >/dev/null 2>&1
eq "--option 1 with 'Blue' -> exit 2" "$?" "2"
eq "...nothing recorded" "$(dj "$d" .status)" "pending"
"$B" answer "$d" "Blue" --by user-1 --option 2 >/dev/null
eq "--option 2 with 'Blue' -> option 2" "$(dj "$d" '"\(.status)/\(.answer_index)"')" "answered/2"
qdone tu_q6i
"$B" answer d-nope "x" --by user-1 --option 1 >/dev/null 2>&1; eq "--option on a missing decision fails" "$?" "1"

# Labels: trimmed, and repeats (case-insensitive) make the card read-only.
d="$(askq tu_q6j '[{"question":"Which one do you want?","header":"Pick","options":[{"label":"  Same  ","description":"a"},{"label":"same","description":"b"}],"multiSelect":false}]')"
eq "labels stored trimmed" "$(dj "$d" '.questions[0].options[0].label')" "Same"
eq "repeated labels -> not pressable" "$(dj "$d" .pressable)" "false"
"$B" answer "$d" "Same" --by user-1 >/dev/null
eq "...an answer relays by message" "$(dj "$d" .relay)" "operator"
qdone tu_q6j

# Read-only cards: several questions, multi-select, clipped text.
QM='[{"question":"Which color?","header":"Color","options":[{"label":"Red","description":""},{"label":"Blue","description":""}],"multiSelect":false},{"question":"Which size?","header":"Size","options":[{"label":"S","description":""},{"label":"L","description":""}],"multiSelect":false}]'
d="$(askq tu_q7 "$QM")"
eq "two single-select questions -> pressable (one option per question)" "$(dj "$d" .pressable)" "true"
eq "...both questions kept, no flat options" "$(dj "$d" '"\(.questions | length)/\(.options | length)"')" "2/0"
"$B" answer "$d" "Red" --by user-1 >/dev/null
eq "...an answer relays by message" "$(dj "$d" .relay)" "operator"
FAKE_TMUX_SCREEN="$(qscreen)" press CP answer --expect tu_q7
eq "...press refused -> exit 4" "$?" "4"
qdone tu_q7
d="$(askq tu_q8 '[{"question":"Which tools?","header":"Tools","options":[{"label":"lint","description":""},{"label":"test","description":""}],"multiSelect":true}]')"
eq "multiSelect -> not pressable" "$(dj "$d" .pressable)" "false"
qdone tu_q8
tok="ghp""_abcdefghijklmnopqrstuvwxyz0123456789"
LQ="$(jq -cn --arg q "Deploy with token $tok now? $(printf 'q%.0s' $(seq 1 400))" --arg l "$(printf 'Option\033[31m one\001 '; printf 'l%.0s' $(seq 1 100))" --arg dsc "$(printf 'x%.0s' $(seq 1 200))" \
  '[{question: $q, header: "Deploy", options: [{label: $l, description: $dsc}, {label: "No", description: "stop"}], multiSelect: false}]')"
d="$(askq tu_q9 "$LQ")"
case "$(dj "$d" .question)" in *"${tok:0:10}"*) bad "question text is redacted" ;; *"[REDACTED]"*) ok "question text is redacted" ;; *) bad "question text is redacted" "$(dj "$d" .question)" ;; esac
eq "question clipped to 300 with an ellipsis" "$(dj "$d" '.question | "\(length) \(.[-1:])"')" "300 …"
eq "label clipped to 80" "$(dj "$d" '.questions[0].options[0].label | length')" "80"
eq "description clipped to 160" "$(dj "$d" '.questions[0].options[0].description | length')" "160"
eq "control characters stripped" "$(dj "$d" '.questions[0].options[0].label | test("[\u0000-\u001f\u007f]")')" "false"
eq "clipped label and text -> questions_clipped, labels_clipped, not pressable" "$(dj "$d" '"\(.questions_clipped)/\(.labels_clipped)/\(.pressable)"')" "true/true/false"
if grep -rqF "$tok" "$BOARD_HOME"; then bad "token never stored"; else ok "token never stored"; fi
qdone tu_q9

# A long question with short, complete labels stays pressable (only the question text is
# clipped); the press then needs both the start of the question and every label on screen.
LONGQ="$(jq -cn --arg q "Which color do you prefer for the test? $(printf 'context %.0s' $(seq 1 60))" \
  '[{question: $q, header: "Color", options: [{label: "Red", description: "warm"}, {label: "Blue", description: "cool"}, {label: "Green", description: "natural"}], multiSelect: false}]')"
LSCREEN="$(qscreen | sed 's/Which color do you prefer for the test?/Which color do you prefer for the test? context context context context/')"
d="$(askq tu_q10 "$LONGQ")"
eq "long question, short labels -> clipped text, labels whole, pressable" \
  "$(dj "$d" '"\(.questions_clipped)/\(.labels_clipped)/\(.questions[0].question_clipped)/\(.pressable)"')" "true/false/true/true"
"$B" answer "$d" "Blue" --by user-1 >/dev/null
eq "...answer relays by press" "$(dj "$d" .relay)" "press"
: > "$FAKE_TMUX_LOG"
FAKE_TMUX_SCREEN="$(qscreen)" press CP answer --expect tu_q10
eq "...labels visible but not the start of the question -> exit 3, nothing sent" "$?/$(sent_keys)" "3/0"
qdone tu_q10
d="$(askq tu_q10b "$LONGQ")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
: > "$FAKE_TMUX_LOG"
FAKE_TMUX_SCREEN="$LSCREEN" press CP answer --expect tu_q10b
eq "...start of the question and every label visible -> press ok" "$?" "0"
eq "...sends the option's number key" "$(grep '^send-keys' "$FAKE_TMUX_LOG" | tail -1)" "send-keys -t %42 2"
qdone tu_q10b
d="$(askq tu_q10d "$LONGQ")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
: > "$FAKE_TMUX_LOG"
# The labels only in the scrollback above the question's start (an older picker), none after it.
FAKE_TMUX_SCREEN="$(qscreen)
$(printf '\nWhich color do you prefer for the test? context context context context\n\nEnter to select · ↑/↓ to navigate · Esc to cancel\n')" press CP answer --expect tu_q10d
eq "...labels only above the clipped question's start -> exit 3, nothing sent" "$?/$(sent_keys)" "3/0"
qdone tu_q10d
d="$(askq tu_q10c "$LONGQ")"; "$B" answer "$d" "Blue" --by user-1 >/dev/null
: > "$FAKE_TMUX_LOG"
FAKE_TMUX_SCREEN="${LSCREEN/  2. Blue/  2. Teal}" press CP answer --expect tu_q10c
eq "...a label missing on screen -> exit 3, nothing sent" "$?/$(sent_keys)" "3/0"
qdone tu_q10c
# A header cut short is tracked apart: the question is not "shortened", the card stays pressable.
HDRQ="$(jq -cn --arg h "Header $(printf 'h%.0s' $(seq 1 60))" '[{question: "Which color?", header: $h, options: [{label: "Red", description: ""}, {label: "Blue", description: ""}], multiSelect: false}]')"
d="$(askq tu_q12 "$HDRQ")"
eq "a clipped header: header_clipped, not question_clipped, pressable" \
  "$(dj "$d" '"\(.questions[0].header_clipped)/\(.questions[0].question_clipped)/\(.questions_clipped)/\(.labels_clipped)/\(.pressable)"')" "true/false/true/false/true"
qdone tu_q12
# ---- A form of several questions, as Claude Code draws it (recorded on a private tmux server):
# a tab bar, one question at a time (a digit picks and moves on), a review screen, "1. Submit answers".
# The prompt above the form repeats the questions: only the lines after the tab bar may count.
Q2F="$(jq -cn '[{question: "Which color for the banner?", header: "Color", multiSelect: false,
  options: [{label: "Red", description: "warm"}, {label: "Blue", description: "cool"}, {label: "Green", description: "natural"}]},
  {question: "Which size for the banner?", header: "Size", multiSelect: false,
  options: [{label: "Small", description: "compact"}, {label: "Large", description: "wide"}]}]')"
mq_head() { printf '❯ Ask me two things: Which color for the banner? Red Blue Green. Which size for the banner? Small Large.\n\n────\n←  %s Color  %s Size  ✔ Submit  →\n\n' "$1" "$2"; }
mq_q1() { mq_head ☐ ☐; printf 'Which color for the banner?\n\n❯ 1. Red\n     warm\n  2. Blue\n     cool\n  3. Green\n     natural\n  4. Type something.\n───\n  5. Chat about this\n\nEnter to select · Tab/Arrow keys to navigate · Esc to cancel\n'; }
mq_q2() { mq_head ☒ ☐; printf 'Which size for the banner?\n\n❯ 1. Small\n     compact\n  2. Large\n     wide\n  3. Type something.\n───\n  4. Chat about this\n\nEnter to select · Tab/Arrow keys to navigate · Esc to cancel\n'; }
mq_review() { mq_head ☒ ☒; printf 'Review your answers\n\n ● Which color for the banner?\n   → %s\n ● Which size for the banner?\n   → %s\n\nReady to submit your answers?\n\n❯ 1. Submit answers\n  2. Cancel\n' "$1" "$2"; }
d="$(askq tu_m0 "$Q2F")"
eq "form: pressable, no flat options" "$(dj "$d" '"\(.pressable)/\(.options | length)"')" "true/0"
"$B" answer "$d" --options 2 --by user-1 >/dev/null 2>&1
eq "--options: one number for two questions -> exit 2" "$?/$(dj "$d" .status)" "2/pending"
"$B" answer "$d" --options 2,3 --by user-1 >/dev/null 2>&1
eq "--options: 3 for a question of 2 options -> exit 2" "$?/$(dj "$d" .status)" "2/pending"
"$B" answer "$d" --options 2,x --by user-1 >/dev/null 2>&1
eq "--options: not numbers -> exit 2" "$?/$(dj "$d" .status)" "2/pending"
"$B" answer "$d" Red --options 1,1 --by user-1 >/dev/null 2>&1
eq "--options with answer text -> exit 2" "$?/$(dj "$d" .status)" "2/pending"
"$B" answer "$d" --options 1,1 --by intruder >/dev/null 2>&1
eq "--options by a non-approver -> exit 4, unauthorized" "$?/$(dj "$d" .status)" "4/unauthorized"
"$B" answer "$d" "Red" --by user-1 >/dev/null
eq "a single free answer on a form relays by message, never by keys" "$(dj "$d" '"\(.relay)/\(.answer_options)"')" "operator/null"
: > "$FAKE_TMUX_LOG"
FAKE_TMUX_SCREEN="$(mq_q1)" press CP answer --expect tu_m0
eq "...press refused -> exit 4, nothing sent" "$?/$(sent_keys)" "4/0"
qdone tu_m0
d1="$(askq tu_q1x "$Q1")"
"$B" answer "$d1" --options 1,1 --by user-1 >/dev/null 2>&1
eq "--options on a single question -> exit 2" "$?/$(dj "$d1" .status)" "2/pending"
qdone tu_q1x

d="$(askq tu_m1 "$Q2F")"
"$B" answer "$d" --options 2,2 --by user-1 >/dev/null
eq "--options 2,2 -> composed answer, option per question, relay press" "$(dj "$d" '"\(.answer)|\(.answer_options | join(","))|\(.relay)"')" "Color: Blue · Size: Large|2,2|press"
seqpress tu_m1 "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Blue Large)"
eq "form: each question checked, digits, then Submit -> ok" "$?/$(keys_sent)" "0/send-keys -t %42 2|send-keys -t %42 2|send-keys -t %42 1|"
eq "...never Enter, decision relayed" "$(grep -c 'Enter' "$FAKE_TMUX_LOG")/$(dj "$d" .status)" "0/relayed"
qdone tu_m1

d="$(askq tu_m2 "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_m2 "$(mq_q1)" "$(mq_q1)" "$(mq_q1)"
eq "question 2 does not appear after the first digit -> exit 5, one key" "$?/$(keys_sent)" "5/send-keys -t %42 2|"
eq "...stale, logged partial" "$(dj "$d" .status)/$(jq -s -r '[.[] | select(.type=="press")] | last | .result' "$BOARD_HOME/events.jsonl")" "stale/partial"
qdone tu_m2

d="$(askq tu_m3 "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_m3 "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_q2)"
eq "no review screen after the last question -> exit 5, nothing submitted" "$?/$(keys_sent)" "5/send-keys -t %42 2|send-keys -t %42 2|"
qdone tu_m3

d="$(askq tu_m4 "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_m4 "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Green Large)"
eq "the review shows another answer -> exit 5, nothing submitted" "$?/$(keys_sent)" "5/send-keys -t %42 2|send-keys -t %42 2|"
qdone tu_m4

d="$(askq tu_m5 "$Q2F")"; "$B" answer "$d" --options 1,2 --by user-1 >/dev/null
seqpress tu_m5 "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Redwood Large)"
eq "a longer label starting with the chosen one does not pass -> exit 5" "$?/$(keys_sent)" "5/send-keys -t %42 1|send-keys -t %42 2|"
qdone tu_m5

d="$(askq tu_m6 "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
FAKE_TMUX_MUTATE_AT=3 FAKE_TMUX_MUTATE="$BOARD_HOME/state/$SID.json" seqpress tu_m6 "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Blue Large)"
eq "the prompt changes mid-way -> exit 5 before the next digit" "$?/$(keys_sent)" "5/send-keys -t %42 2|"
qdone tu_m6

d="$(askq tu_m7 "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_m7 "$(mq_q2)"
eq "question 1 only in the prompt above the form (question 2 showing) -> exit 3, nothing sent" "$?/$(keys_sent)" "3/"
qdone tu_m7

d="$(askq tu_m8 "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_m8 "$(mq_q1 | sed 's/✔ Submit/  Done/')"
eq "no tab bar -> exit 3, nothing sent" "$?/$(keys_sent)" "3/"
qdone tu_m8

# A slow redraw: question 2 shows only after a few captures -> waited for, then pressed.
d="$(askq tu_m9 "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_m9 "$(mq_q1)" "$(mq_q1)" "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_q2)" "$(mq_review Blue Large)"
eq "slow redraw: each next screen is awaited -> ok, digits then Submit" "$?/$(keys_sent)" "0/send-keys -t %42 2|send-keys -t %42 2|send-keys -t %42 1|"
qdone tu_m9

# Stuck on question 1 whose option text quotes question 2 and its labels: question 2 must be the
# first text after the tab bar, so its digit never goes into question 1.
d="$(askq tu_ma "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
Q1Q="$(mq_q1 | sed 's/     natural/     natural (Which size for the banner? Small or Large)/')"
seqpress tu_ma "$Q1Q"
eq "question 2 quoted under question 1 does not pass for question 2 -> exit 5, one key" "$?/$(keys_sent)" "5/send-keys -t %42 2|"
qdone tu_ma

# The form closed after question 1 (no picker footer under question 2) -> no second digit.
d="$(askq tu_mb "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_mb "$(mq_q1)" "$(mq_q1)" "$(mq_q2 | grep -v 'Enter to select')"
eq "no picker footer under question 2 -> exit 5, one key" "$?/$(keys_sent)" "5/send-keys -t %42 2|"
qdone tu_mb

# A question whose text has its own "2. Submit answers" line: the review shows two such rows; the
# first one was once taken, which pressed Cancel and reported success. Now nothing is submitted.
Q2S="$(jq -c '.[1].question = "Which size for the banner? Reply as\n  2. Submit answers (as drafted)"' <<<"$Q2F")"
mq_q2s() { mq_head ☒ ☐; printf 'Which size for the banner? Reply as\n  2. Submit answers (as drafted)\n\n❯ 1. Small\n     compact\n  2. Large\n     wide\n  3. Type something.\n\nEnter to select · Tab/Arrow keys to navigate · Esc to cancel\n'; }
mq_review_s() { mq_head ☒ ☒; printf 'Review your answers\n\n ● Which color for the banner?\n   → Blue\n ● Which size for the banner? Reply as\n  2. Submit answers (as drafted)\n   → Large\n\nReady to submit your answers?\n\n❯ 1. Submit answers\n  2. Cancel\n'; }
d="$(askq tu_mc "$Q2S")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_mc "$(mq_q1)" "$(mq_q1)" "$(mq_q2s)" "$(mq_review_s)"
eq "two 'N. Submit answers' rows on the review -> exit 5, nothing submitted" "$?/$(keys_sent)" "5/send-keys -t %42 2|send-keys -t %42 2|"
eq "...stale, not relayed" "$(dj "$d" .status)" "stale"
qdone tu_mc
d="$(askq tu_md "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_md "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Blue Large | sed 's/^❯ 1. Submit answers/  1. Submit answers/; s/^  2. Cancel/❯ 2. Cancel/')"
eq "Submit row not focused (Cancel is) -> exit 5, nothing submitted" "$?/$(keys_sent)" "5/send-keys -t %42 2|send-keys -t %42 2|"
qdone tu_md
d="$(askq tu_me "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_me "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Blue Large; printf '\nNotes:\n')"
eq "Submit row not in the last numbered block -> exit 5, nothing submitted" "$?/$(keys_sent)" "5/send-keys -t %42 2|send-keys -t %42 2|"
qdone tu_me

# Questions that start alike (same first 40 characters, same labels) are not pressable: a screen
# still on one could pass for the other. A decision forced pressable is still refused by press.
QSAME="$(jq -cn '[range(2) as $i | {question: ("Which of these two settings should the release keep for item " + ($i + 1 | tostring) + "?"),
  header: ("Item " + ($i + 1 | tostring)), multiSelect: false, options: [{label: "Keep", description: ""}, {label: "Drop", description: ""}]}]')"
d="$(askq tu_mf "$QSAME")"
eq "questions differing past 40 characters: pressable" "$(dj "$d" .pressable)" "true"
qdone tu_mf
QPRE="$(jq -c '.[1].question = .[0].question + " Explain."' <<<"$QSAME")"
d="$(askq tu_mg "$QPRE")"
eq "one question begins another -> not pressable" "$(dj "$d" .pressable)" "false"
jq '.pressable = true' "$BOARD_HOME/decisions/$d.json" > "$T/d.x" && mv "$T/d.x" "$BOARD_HOME/decisions/$d.json"
"$B" answer "$d" --options 1,1 --by user-1 >/dev/null
: > "$FAKE_TMUX_LOG"
FAKE_TMUX_SCREEN="$(mq_q1)" press CP answer --expect tu_mg
eq "...forced pressable: press refuses (questions start alike) -> exit 4, nothing sent" "$?/$(sent_keys)" "4/0"
qdone tu_mg

# Configured patterns are checked before any key.
cp "$BOARD_CONFIG_DIR/fleet.yaml" "$T/fleet.yaml.mq"
d="$(askq tu_mh "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
printf 'press:\n  review_regex: "(unclosed"\n' >> "$BOARD_CONFIG_DIR/fleet.yaml"
seqpress tu_mh "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Blue Large)"
eq "an invalid press.review_regex -> exit 3 before any key" "$?/$(keys_sent)" "3/"
qdone tu_mh
cp "$T/fleet.yaml.mq" "$BOARD_CONFIG_DIR/fleet.yaml"
d="$(askq tu_mi "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
printf 'press:\n  submit_label: ".*"\n' >> "$BOARD_CONFIG_DIR/fleet.yaml"
seqpress tu_mi "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Blue Large)"
eq "submit_label is literal text: '.*' matches no row -> exit 5, nothing submitted" "$?/$(keys_sent)" "5/send-keys -t %42 2|send-keys -t %42 2|"
qdone tu_mi
cp "$T/fleet.yaml.mq" "$BOARD_CONFIG_DIR/fleet.yaml"
d="$(askq tu_mj "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
printf 'press:\n  tabbar_regex: "(unclosed"\n' >> "$BOARD_CONFIG_DIR/fleet.yaml"
seqpress tu_mj "$(mq_q1)" "$(mq_q1)"
eq "an invalid press.tabbar_regex -> exit 3 before any key, named" "$?/$(keys_sent)/$(jq -s -r '[.[] | select(.type=="press")] | last | .reason' "$BOARD_HOME/events.jsonl")" "3//press.tabbar_regex is not a valid pattern (grep -E)"
qdone tu_mj
cp "$T/fleet.yaml.mq" "$BOARD_CONFIG_DIR/fleet.yaml"

# Whitespace is removed the same way on both sides, Unicode spaces too: a question stored with a
# no-break space and an ideographic space matches the screen that shows plain spaces.
QNB="$(jq -c '.[0].question = "Which color\u00a0for the\u3000banner?"' <<<"$Q2F")"
d="$(askq tu_mk "$QNB")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
eq "Unicode spaces in a question: still pressable" "$(dj "$d" .pressable)" "true"
seqpress tu_mk "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Blue Large)"
eq "...matched with plain spaces on screen -> ok, digits then Submit" "$?/$(keys_sent)" "0/send-keys -t %42 2|send-keys -t %42 2|send-keys -t %42 1|"
qdone tu_mk
QNB2="$(jq -c '.[1].question = .[0].question + "\u00a0Explain."' <<<"$Q2F")"
d="$(askq tu_ml "$QNB2")"
eq "one question begins another across a no-break space -> not pressable" "$(dj "$d" .pressable)" "false"
qdone tu_ml

# The focused-row markers are the single questions' ones, and configurable (press.focus_regex).
d="$(askq tu_mm "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_mm "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Blue Large | sed 's/^❯ 1. Submit answers/› 1. Submit answers/')"
eq "Submit focused with › -> ok" "$?/$(keys_sent)" "0/send-keys -t %42 2|send-keys -t %42 2|send-keys -t %42 1|"
qdone tu_mm
d="$(askq tu_mn "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
printf 'press:\n  focus_regex: "»"\n' >> "$BOARD_CONFIG_DIR/fleet.yaml"
seqpress tu_mn "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Blue Large | sed 's/^❯ 1. Submit answers/» 1. Submit answers/')"
eq "press.focus_regex: » marks the focused row -> ok" "$?/$(keys_sent)" "0/send-keys -t %42 2|send-keys -t %42 2|send-keys -t %42 1|"
qdone tu_mn
d="$(askq tu_mo "$Q2F")"; "$B" answer "$d" --options 2,2 --by user-1 >/dev/null
seqpress tu_mo "$(mq_q1)" "$(mq_q1)" "$(mq_q2)" "$(mq_review Blue Large)"
eq "...and ❯ no longer does -> exit 5, nothing submitted" "$?/$(keys_sent)" "5/send-keys -t %42 2|send-keys -t %42 2|"
qdone tu_mo
cp "$T/fleet.yaml.mq" "$BOARD_CONFIG_DIR/fleet.yaml"

# One label cut short -> read-only, even with a short question.
CLIPL="$(jq -cn --arg l "Option $(printf 'l%.0s' $(seq 1 100))" '[{question: "Which one?", header: "Pick", options: [{label: $l, description: ""}, {label: "No", description: ""}], multiSelect: false}]')"
d="$(askq tu_q11 "$CLIPL")"
eq "a clipped label -> labels_clipped, not pressable" "$(dj "$d" '"\(.questions[0].question_clipped)/\(.labels_clipped)/\(.pressable)"')" "false/true/false"
qdone tu_q11

# The session moves on: the collector marks an open pane question stale.
d="$(askq tu_q10 "$Q1")"
qdone tu_q10
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once --no-notify >/dev/null 2>&1
eq "PostToolUse for the question -> stale" "$(dj "$d" .status)" "stale"
eq "...from filled with the session code" "$(dj "$d" .from)" "CP"

hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "UserPromptSubmit"}')"
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PreToolUse", tool_name: "Bash", tool_use_id: "tu_last", tool_input: {command: "true"}}')"
last_tu=tu_last

# Redaction and private files. Token-shaped values are assembled at runtime, so this file never
# contains one (the gitleaks pre-commit hook scans it).
k_aws="AKIA""ABCDEFGHIJKLMNOP"; k_gh="ghp""_abcdefghijklmnopqrstuvwxyz0123456789"; k_sl="xoxb""-1234567890-abcdefghij"
hook "$(jq -cn --arg s "$SID" --arg c "curl -H \"Authorization: Bearer abcdef0123456789xyz\" https://api.example.test?token=s3cr3tvalue -u u:p --data password=hunter2xyz $k_aws $k_gh $k_sl" \
  '{session_id: $s, hook_event_name: "PreToolUse", tool_name: "Bash", tool_use_id: "tu_sec", tool_input: {command: $c}}')"
summ="$(st '.recent_pre | last | .summary')"
for s in abcdef0123456789xyz s3cr3tvalue hunter2xyz "$k_aws" "${k_gh:0:10}" "${k_sl:0:9}"; do
  case "$summ" in *"$s"*) bad "summary redacts ${s:0:4}…" "$summ" ;; *) ok "summary redacts ${s:0:4}…" ;; esac
done
case "$summ" in *"curl -H"*"https://api.example.test?token=[REDACTED]"*) ok "summary keeps the readable parts" ;; *) bad "summary keeps the readable parts" "$summ" ;; esac
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PostToolUse", tool_name: "Bash", tool_use_id: "tu_sec", tool_input: {}}')"
# One probe per pattern; every secret value is assembled at runtime.
redacts() {  # redacts NAME COMMAND SECRET [KEEP]
  hook "$(jq -cn --arg s "$SID" --arg c "$2" '{session_id: $s, hook_event_name: "PreToolUse", tool_name: "Bash", tool_use_id: "tu_rd", tool_input: {command: $c}}')"
  local got; got="$(st '.recent_pre | last | .summary')"
  hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "PostToolUse", tool_name: "Bash", tool_use_id: "tu_rd", tool_input: {}}')"
  case "$got" in *"$3"*) bad "redacts $1" "$got"; return ;; esac
  if [ -n "${4:-}" ]; then case "$got" in *"$4"*) ;; *) bad "redacts $1 (keeps '$4')" "$got"; return ;; esac; fi
  ok "redacts $1"
}
pw="hunter""2zz"
redacts 'JSON "token": value' "echo '{\"token\":\"abc1${pw}\"}'" "abc1$pw" '"token":'
redacts 'JSON "password": value' "printf '%s' '\"password\": \"$pw\"'" "$pw" '"password":'
redacts '--password VALUE' "mysql --password $pw db" "$pw" "mysql --password"
redacts '-p VALUE' "mysql -u root -p $pw db" "$pw" "mysql -u root -p"
redacts 'curl -u user:pass' "curl -u bob:$pw https://api.example.test" "bob:$pw" "curl -u"
redacts 'URL userinfo' "git clone https://bob:$pw@git.example.test/r.git" "$pw" "git.example.test/r.git"
redacts 'Authorization: Basic' "curl -H 'Authorization: Basic $(printf 'bob:%s' "$pw" | base64)' x" "$(printf 'bob:%s' "$pw" | base64)" "Basic"
redacts 'sk_live_' "pay sk_""live_$(printf 'q%.0s' $(seq 1 24))" "sk_live_qqqq"
redacts 'pk_test_' "pay pk_""test_$(printf 'q%.0s' $(seq 1 24))" "pk_test_qqqq"
redacts 'rk_live_' "pay rk_""live_$(printf 'q%.0s' $(seq 1 24))" "rk_live_qqqq"
redacts 'glpat-' "glab --token glpat""-$(printf 'r%.0s' $(seq 1 20))" "glpat-rrrr"
redacts 'AIza (39)' "maps AIza""$(printf 's%.0s' $(seq 1 35))" "AIzassss"
redacts 'npm_ (36)' "npm npm""_$(printf 't%.0s' $(seq 1 36))" "npm_tttt"
redacts 'nothing in mkdir -p / ssh -p 22' "mkdir -p /w/x && ssh -p 22 host" "[REDACTED]" "mkdir -p /w/x && ssh -p 22 host"
eq "BOARD_HOME is 0700" "$(stat -c %a "$BOARD_HOME")" "700"
eq "state file is 0600" "$(stat -c %a "$BOARD_HOME/state/$SID.json")" "600"
eq "decision file is 0600" "$(stat -c %a "$BOARD_HOME/decisions/$d.json")" "600"
H8="$T/home8"; printf '{"session_id":"s8","hook_event_name":"SessionStart"}' | BOARD_HOME="$H8" "$HOOK"
eq "hook creates BOARD_HOME 0700" "$(stat -c %a "$H8")" "700"
H9="$T/home9"; BOARD_HOME="$H9" "$B" ask --from CP "private?" >/dev/null
eq "board creates BOARD_HOME 0700" "$(stat -c %a "$H9")" "700"

# Control characters never reach the terminal.
cid="$("$B" ask --from CP $'evil \e]0;pwned\a\e[2J question')"
out="$("$B" decisions)"
case "$out" in *$'\e'*|*$'\a'*) bad "decisions table strips control characters" ;; *) ok "decisions table strips control characters" ;; esac
grep -q 'evil ?\]0;pwned??\[2J question' <<<"$out" && ok "control characters shown as ?" || bad "control characters shown as ?" "$out"
"$B" mark "$cid" stale >/dev/null

# ---- pending cleared after the tool ran
hook "$(jq -cn --arg s "$SID" --arg t "$last_tu" '{session_id: $s, hook_event_name: "PostToolUse", tool_name: "Bash", tool_use_id: $t, tool_input: {command: "true"}}')"
eq "PostToolUse of the last tool -> working" "$(st .state)" "working"
eq "pending cleared" "$(st .pending)" "null"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"Notification\",\"notification_type\":\"idle_prompt\"}"
eq "idle_prompt -> done" "$(st .state)" "done"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"Stop\"}"
eq "Stop -> done" "$(st .state)" "done"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"SessionEnd\",\"reason\":\"exit\"}"
eq "SessionEnd -> offline" "$(st .state)" "offline"

# Permission prompt without a PreToolUse cache: needs-you, but no decision (no tool_use_id).
SID2="aaaaaaaa-0000-0000-0000-000000000000"
n0="$(ndec)"
printf '%s' "{\"session_id\":\"$SID2\",\"hook_event_name\":\"PermissionRequest\",\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"/w/x\"}}" | "$HOOK"
eq "PermissionRequest w/o cache -> needs-you" "$(jq -r .state "$BOARD_HOME/state/$SID2.json")" "needs-you"
eq "no decision without tool_use_id" "$(ndec)" "$n0"

# Robustness: garbage / unsafe input never fails and never writes.
printf 'not json' | "$HOOK"; eq "garbage input exit 0" "$?" "0"
printf '{"session_id":"../../evil","hook_event_name":"Stop"}' | "$HOOK"; eq "unsafe session id exit 0" "$?" "0"
[ -e "$BOARD_HOME/evil.json" ] && bad "unsafe session id ignored" || ok "unsafe session id ignored"

# Speed (informational, generous bound).
t0="${EPOCHREALTIME/./}"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"tool_use_id\":\"tu_s\",\"tool_input\":{\"file_path\":\"/w/a\"}}"
done
t1="${EPOCHREALTIME/./}"
avg_ms=$(( (t1 - t0) / 10000 ))
echo "  hook avg ${avg_ms}ms"
[ "$avg_ms" -lt 200 ] && ok "hook speed" || bad "hook speed" "${avg_ms}ms"

# ---- usage samples and burn rate
echo "usage burn rate:"
while IFS=$'\t' read -r verdict name detail; do
  if [ "$verdict" = ok ]; then ok "$name"; else bad "$name" "$detail"; fi
done < <(python3 - "$REPO/lib" "$T" <<'PY'
import json, os, sys
sys.path.insert(0, sys.argv[1])
import collect as c
M = 60 * 1000
NOW = 1_800_000_000_000
RESET = NOW + 200 * M
line = lambda pct0, rate_per_min, mins: [{"t": NOW - (mins - i) * M, "pct": pct0 + rate_per_min * i} for i in range(mins + 1)]
cases = []
def case(name, got, want):
    cases.append((name, got, want))

b = c.burn_rate(line(40, 0.5, 20), NOW, RESET)   # 30%/h at 50%: 100% in 100 min, reset in 200
case("pace: rate per hour", b["pct_per_hour"], 30.0)
case("will hit the limit: danger", b["status"], "danger")
case("  projected time of 100%", round((b["hit_at"] - NOW) / M), 100)
b = c.burn_rate(line(40, 0.1, 20), NOW, RESET)   # 6%/h at 42%: 580 min, after the reset
case("pace OK: the reset comes first", (b["status"], b["hit_at"] > RESET), ("ok", True))
case("pace OK: flat usage", c.burn_rate(line(40, 0, 20), NOW, RESET)["status"], "ok")
case("pace OK: falling usage (no projection)", c.burn_rate(line(60, -0.5, 20), NOW, RESET)["hit_at"], None)
case("no reset time: a rising line is danger", c.burn_rate(line(40, 0.1, 20), NOW, None)["status"], "danger")
case("already at 100%: danger now", c.burn_rate(line(90, 0.5, 20), NOW, RESET)["hit_at"], NOW)
case("too few samples: unknown", c.burn_rate(line(40, 0.5, 1), NOW, RESET)["status"], "unknown")
case("under 5 min of data: unknown", c.burn_rate(line(40, 0.5, 4), NOW, RESET)["status"], "unknown")
case("empty: unknown", c.burn_rate([], NOW, RESET)["status"], "unknown")
case("only the last 30 min count", c.burn_rate([dict(s, t=s["t"] - 30 * M) for s in line(0, 2, 30)[:5]] + line(40, 0, 20), NOW, RESET)["status"], "ok")
old = [dict(s, t=s["t"] - 20 * M) for s in line(40, 0.5, 20)]
case("newest sample over 15 min old: stale", c.burn_rate(old, NOW, RESET)["status"], "stale")

u = lambda t, pct, r=RESET: {"five_hour": {"pct": pct, "resets_at": r}, "seven_day": {"pct": 3, "resets_at": r}, "updated_at": t}
buf = c.add_usage_sample({}, u(NOW, 10))
case("buffer: first sample", buf["samples"], [{"t": NOW, "pct": 10.0}])
buf = c.add_usage_sample(buf, u(NOW, 10))
case("buffer: the same reading is not added twice", len(buf["samples"]), 1)
buf = c.add_usage_sample(buf, u(NOW + 5000, 11))
case("buffer: within 20 s replaces the last sample", buf["samples"], [{"t": NOW + 5000, "pct": 11.0}])
buf = c.add_usage_sample(buf, u(NOW + M, 12))
case("buffer: 20 s later appends", len(buf["samples"]), 2)
case("buffer: an older reading is ignored", len(c.add_usage_sample(buf, u(NOW - M, 99))["samples"]), 2)
case("buffer: pct is 0-100 (0.25 stays 0.25%)", c.add_usage_sample({}, u(NOW, 0.25))["samples"][0]["pct"], 0.25)
probe = {}
for i, p in enumerate((0.3, 0.6, 0.9)):
    probe = c.add_usage_sample(probe, u(NOW - (2 - i) * 3 * M, p))
b = c.burn_rate(probe["samples"], NOW, RESET)
case("probe: sub-1% readings (0.3/0.6/0.9 over 6 min) are not danger", (b["status"], b["pct_per_hour"]), ("ok", 6.0))
case("NaN / Infinity readings are ignored", [len(c.add_usage_sample({}, u(NOW, v))["samples"]) for v in (float("nan"), float("inf"), float("-inf"))], [0, 0, 0])
case("a NaN sample is left out of the fit", c.burn_rate(line(40, 0, 20) + [{"t": NOW - M, "pct": float("nan")}], NOW, RESET)["status"], "ok")
case("buffer: no 5h reading leaves it as it is", c.add_usage_sample(buf, {"updated_at": NOW + 2 * M})["samples"], buf["samples"])
case("buffer: a new window (resets_at moved) starts over", c.add_usage_sample(buf, u(NOW + 2 * M, 1, RESET + 5 * 3600 * 1000))["samples"], [{"t": NOW + 2 * M, "pct": 1.0}])
case("buffer: a few seconds of resets_at jitter is the same window", len(c.add_usage_sample(buf, u(NOW + 2 * M, 13, RESET + 3000))["samples"]), 3)
big = {"resets_at": RESET, "samples": []}
for i in range(400):
    big = c.add_usage_sample(big, u(NOW + i * 30 * 1000, 10 + i * 0.01))
case("buffer: capped, only the last hour kept", (len(big["samples"]) <= c.SAMPLE_MAX, big["samples"][-1]["t"] - big["samples"][0]["t"] <= c.SAMPLE_KEEP_MS), (True, True))

home = os.path.join(sys.argv[2], "burnhome")
os.makedirs(home)
case("file: nothing written without usage.json", (c.usage_samples(home, NOW), os.path.exists(os.path.join(home, c.SAMPLES_FILE))), (None, False))
for i in range(11):
    with open(os.path.join(home, "usage.json"), "w") as fh:
        json.dump(u(NOW - (10 - i) * M, 50 + i), fh)
    burn = c.usage_samples(home, NOW - (10 - i) * M)
f = json.load(open(os.path.join(home, c.SAMPLES_FILE)))
case("file: 11 samples, burn stored", (len(f["samples"]), f["burn"]["status"], f["burn"]["pct_per_hour"]), (11, "danger", 60.0))
mt = os.stat(os.path.join(home, c.SAMPLES_FILE)).st_mtime_ns
os.utime(os.path.join(home, c.SAMPLES_FILE), ns=(mt - 10**9, mt - 10**9))
c.usage_samples(home, NOW + 1000)
case("file: not rewritten when nothing changed", os.stat(os.path.join(home, c.SAMPLES_FILE)).st_mtime_ns, mt - 10**9)
c.usage_samples(home, NOW + 16 * M)
case("file: rewritten when it goes stale", json.load(open(os.path.join(home, c.SAMPLES_FILE)))["burn"]["status"], "stale")
for name, got, want in cases:
    print(("ok\t%s\t" if got == want else "FAIL\t%s\tgot %r, want %r") % ((name,) if got == want else (name, got, want)))
PY
)

# ---- usage limits
echo "limit:"
export BOARD_LIMIT_GRACE_MS=0
echo 'SLACK_WEBHOOK_URL="http://example.invalid/hook"' > "$BOARD_CONFIG_DIR/.env"
: > "$FAKE_CURL_LOG"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once   # settle the snapshot first
: > "$FAKE_CURL_LOG"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"StopFailure\",\"error\":\"rate_limit\",\"error_details\":\"5-hour limit reached · resets 11:30pm (UTC)\"}"
eq "StopFailure -> done" "$(st .state)" "done"
lim="$BOARD_HOME/limit.json"
[ -f "$lim" ] && ok "limit.json written" || bad "limit.json written"
eq "limit kind" "$(jq -r .kind "$lim")" "5h"
eq "limit resets_at parsed" "$(jq -r '.resets_at > 0' "$lim")" "true"
eq "limit resets at 23:30 UTC" "$(jq -r '.resets_at / 1000 | strftime("%H:%M")' "$lim")" "23:30"
hit1="$(jq .hit_at "$lim")"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"StopFailure\",\"error\":\"usage limit reached\"}"
eq "same episode keeps hit_at" "$(jq .hit_at "$lim")" "$hit1"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
eq "snapshot.limit set" "$(jq -r '.limit.kind' "$BOARD_HOME/snapshot.json")" "5h"
eq "one Slack alert per episode" "$(grep -c 'usage limit hit (5h), resets ~' "$FAKE_CURL_LOG")" "1"
grep -q 'board frozen; decisions pending: [0-9]' "$FAKE_CURL_LOG" && ok "alert text" || bad "alert text" "$(cat "$FAKE_CURL_LOG")"
"$REPO/bin/board" ask --from CP "Something new?" >/dev/null   # change the snapshot
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
eq "no second alert in the same episode" "$(grep -c 'usage limit hit' "$FAKE_CURL_LOG")" "1"
grep -q "Something new" "$FAKE_CURL_LOG" && ok "other alerts continue while frozen" || bad "other alerts continue while frozen"
"$REPO/bin/board" limit | grep -q "usage limit hit" && ok "board limit shows episode" || bad "board limit shows episode"
"$REPO/bin/board" ls | grep -q "USAGE LIMIT ACTIVE" && ok "board ls shows limit" || bad "board ls shows limit"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"UserPromptSubmit\"}"
eq "UserPromptSubmit alone does not end the episode" "$(jq -r 'has("cleared_at")' "$lim")" "false"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"tool_use_id\":\"tu_after\",\"tool_input\":{\"file_path\":\"/w/a\"}}"
eq "PreToolUse after hit ends the episode" "$(jq -r 'has("cleared_at")' "$lim")" "true"
eq "board limit after clear" "$("$REPO/bin/board" limit)" "no active usage limit"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
eq "snapshot.limit cleared" "$(jq -r '.limit' "$BOARD_HOME/snapshot.json")" "null"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"Notification\",\"notification_type\":\"other\",\"message\":\"Weekly limit reached, resets Mon 9am\"}"
eq "notification text starts a new episode" "$(jq -r 'has("cleared_at")' "$lim")" "false"
eq "weekly kind" "$(jq -r .kind "$lim")" "weekly"
PATH="$T/bin:$PATH" "$REPO/bin/board-collect" --once
eq "new episode alerts again" "$(grep -c 'usage limit hit' "$FAKE_CURL_LOG")" "2"
"$REPO/bin/board" limit --clear >/dev/null
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"Notification\",\"notification_type\":\"idle_prompt\",\"message\":\"Claude is waiting for your input\"}"
eq "unrelated notification is not a limit" "$(jq -r 'has("cleared_at")' "$lim")" "true"
hook "{\"session_id\":\"$SID\",\"hook_event_name\":\"StopFailure\",\"error\":\"overloaded_error\"}"
eq "non-limit StopFailure is not a limit" "$(jq -r 'has("cleared_at")' "$lim")" "true"
"$REPO/bin/board" limit --clear >/dev/null
hook "$(jq -cn --arg s "$SID" '{session_id: $s, hook_event_name: "StopFailure", error: "usage limit reached \u001b]0;x\u0007 resets 9am"}')"
case "$(cat "$lim")$("$REPO/bin/board" limit)" in *$'\e'*|*$'\a'*) bad "limit note strips control characters" ;; *) ok "limit note strips control characters" ;; esac
"$REPO/bin/board" limit --clear >/dev/null
eq "limit.json is 0600" "$(stat -c %a "$lim")" "600"
eq "alerts.json is 0600" "$(stat -c %a "$BOARD_HOME/alerts.json")" "600"
rm -f "$BOARD_CONFIG_DIR/.env"

# ---- approvers / release managers
echo "approvers:"
B="$REPO/bin/board"
grep -v '^approvers:' "$BOARD_CONFIG_DIR/fleet.yaml" > "$T/fleet.base.yaml"
{ cat "$T/fleet.base.yaml"; printf 'approvers: [u-owner, u-lead]\nrelease_managers: [u-rm]\n'; } > "$BOARD_CONFIG_DIR/fleet.yaml"
q="$("$B" ask --from CP "Merge?")"
"$B" answer "$q" yes --by u-stranger >/dev/null 2>&1; eq "non-approver refused (exit 4)" "$?" "4"
eq "non-approver -> unauthorized" "$(jq -r .status "$BOARD_HOME/decisions/$q.json")" "unauthorized"
eq "...status_reason not_approver" "$(jq -r .status_reason "$BOARD_HOME/decisions/$q.json")" "not_approver"
"$B" answer "$q" yes --by u-owner >/dev/null; eq "approver accepted" "$?" "0"
eq "approver -> answered" "$(jq -r .status "$BOARD_HOME/decisions/$q.json")" "answered"
p="$("$B" ask --from CP "Release 1.2 to production?" --gate production --stage production --artifact rel-notes)"
eq "gate decision default options" "$(jq -c .options "$BOARD_HOME/decisions/$p.json")" '["approve","request changes"]'
"$B" answer "$p" approve --by u-owner >/dev/null 2>&1; eq "production: approver who is not RM refused" "$?" "4"
"$B" answer "$p" approve --by u-rm >/dev/null; eq "production: release manager accepted" "$?" "0"
eq "production approval event" "$(jq -rs --arg id "$p" '[.[] | select(.type=="approval" and .decision_id==$id)] | last | .result' "$BOARD_HOME/events.jsonl")" "approved"
{ cat "$T/fleet.base.yaml"; printf 'approvers: [u-owner]\nrelease_managers: []\n'; } > "$BOARD_CONFIG_DIR/fleet.yaml"
p2="$("$B" ask --from CP "Hotfix to production?" --gate production)"
"$B" answer "$p2" approve --by u-owner >/dev/null; eq "production falls back to approvers" "$?" "0"
cp "$T/fleet.base.yaml" "$BOARD_CONFIG_DIR/fleet.yaml"
p3="$("$B" ask --from CP "Another release?" --gate production)"
"$B" answer "$p3" approve --by anyone >/dev/null 2>&1; eq "production with nobody configured refused" "$?" "4"
eq "...and stays pending" "$(jq -r .status "$BOARD_HOME/decisions/$p3.json")" "pending"
q2="$("$B" ask --from CP "Unchecked?")"
"$B" answer "$q2" yes --by anyone >/dev/null 2>&1; eq "empty approvers fails closed for any answer" "$?" "4"
eq "...and stays pending" "$(jq -r .status "$BOARD_HOME/decisions/$q2.json")" "pending"
"$B" mark "$q2" relayed >/dev/null
"$B" answer "$q2" no --by anyone >/dev/null 2>&1; eq "cannot re-answer a relayed decision" "$?" "1"
# Production detection is normalized: gate or stage, trimmed and lowercased.
{ cat "$T/fleet.base.yaml"; printf 'approvers: [u-owner]\nrelease_managers: [u-rm]\n'; } > "$BOARD_CONFIG_DIR/fleet.yaml"
p4="$("$B" ask --from CP "Ship?" --gate " Production ")"
"$B" answer "$p4" approve --by u-owner >/dev/null 2>&1; eq "gate ' Production ' is production" "$?" "4"
p5="$("$B" ask --from CP "Ship?" --stage production)"
eq "--stage alone is stored" "$(jq -r .stage "$BOARD_HOME/decisions/$p5.json")" "production"
"$B" answer "$p5" approve --by u-owner >/dev/null 2>&1; eq "stage production (any gate) is production" "$?" "4"
"$B" answer "$p5" approve --by u-rm >/dev/null; eq "...release manager may answer it" "$?" "0"
out="$(python3 - "$REPO/lib" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import fleet, notify
print(fleet.is_production({"gate": " PRODUCTION "}), fleet.is_production({"stage": "production"}),
      fleet.is_production({"gate": "prod"}), notify.decision_alert({"id": "x", "stage": "Production", "kind": "question"})["level"])
PY
)"
eq "python is_production + urgent alert for stage production" "$out" "True True False urgent"
cp "$T/fleet.base.yaml" "$BOARD_CONFIG_DIR/fleet.yaml"

# ---- lifecycle tasks
echo "tasks:"
{ cat "$T/fleet.base.yaml"; printf 'approvers: [u-owner]\n'; } > "$BOARD_CONFIG_DIR/fleet.yaml"
tid="$("$B" task new --owner CP "Add order export")"
eq "task id format" "$(grep -cE '^t-[0-9]+-[0-9a-f]+$' <<<"$tid")" "1"
tf="$BOARD_HOME/tasks/$tid.json"
eq "new task stage plan" "$(jq -r .stage "$tf")" "plan"
"$B" task stage "$tid" build --artifact intent=abc123 --artifact plan=def456 >/dev/null
eq "task stage set" "$(jq -r .stage "$tf")" "build"
eq "task artifacts" "$(jq -r '.artifacts.intent + "," + .artifacts.plan' "$tf")" "abc123,def456"
"$B" task stage "$tid" shipping >/dev/null 2>&1; eq "unknown stage rejected" "$?" "1"
"$B" task stage "$tid" test --artifact secrets=x >/dev/null 2>&1; eq "unknown artifact key rejected" "$?" "1"
eq "rejected stage change leaves task alone" "$(jq -r .stage "$tf")" "build"
g="$("$B" ask --from CP --task "$tid" --gate owner --artifact def456 "Approve plan?")"
eq "gate decision fields" "$(jq -r '[.task_id, .stage, .gate, .artifact_ref] | join(",")' "$BOARD_HOME/decisions/$g.json")" "$tid,build,owner,def456"
eq "task gate pending" "$(jq -r '.gates[0].status + "," + .gates[0].decision_id' "$tf")" "pending,$g"
"$B" ask --from CP --task t-0-nope "x?" >/dev/null 2>&1; eq "ask with unknown task rejected" "$?" "1"
"$B" answer "$g" approve --by u-owner >/dev/null
eq "task gate approved" "$(jq -r '.gates[0].status + "," + .gates[0].approved_by' "$tf")" "approved,u-owner"
eq "approval event logged" "$(jq -rs --arg id "$g" '[.[] | select(.type=="approval" and .decision_id==$id)] | last | [.task_id, .stage, .gate, .result] | join(",")' "$BOARD_HOME/events.jsonl")" "$tid,build,owner,approved"
g2="$("$B" ask --from CP --task "$tid" --stage test --gate code-owner "Evals ok?")"
"$B" answer "$g2" "request changes" --by u-owner >/dev/null
eq "request changes -> gate rejected" "$(jq -r '.gates[1].status' "$tf")" "rejected"
"$REPO/bin/board-collect" --once --no-notify
eq "snapshot tasks" "$(jq -r '.tasks | length' "$BOARD_HOME/snapshot.json")" "1"
eq "session shows task stage" "$(jq -r '.sessions[] | select(.code=="CP") | .stage + "," + .task_id' "$BOARD_HOME/snapshot.json")" "build,$tid"
"$B" ls | grep -E '^CP ' | grep -q build && ok "board ls shows stage" || bad "board ls shows stage" "$("$B" ls)"
"$B" task ls | grep -q "build/owner:approved" && ok "task ls" || bad "task ls" "$("$B" task ls)"
g3="$("$B" ask --from CP --task "$tid" --stage test --gate code-owner "Evals ok now?")"
"$B" answer "$g3" " LGTM " --by u-owner >/dev/null
eq "approve words are case-insensitive" "$(jq -r '.gates[2].status' "$tf")" "approved"
cp "$T/fleet.base.yaml" "$BOARD_CONFIG_DIR/fleet.yaml"

# ---- help
echo "help:"
out="$("$B" answer --help)"; rc=$?
eq "board answer --help exits 0" "$rc" "0"
grep -q "lgtm" <<<"$out" && ok "answer --help lists approve words" || bad "answer --help lists approve words" "$out"
for c in ls snapshot ask decisions answer mark press limit notify doctor ack task "task new" "task stage" "task ls" "task show"; do
  # shellcheck disable=SC2086  # word-split "task new" on purpose
  out="$("$B" $c -h 2>&1)"; rc=$?
  if [ "$rc" = 0 ] && grep -q "^usage: board ${c%% *}" <<<"$out"; then ok "board $c -h"; else bad "board $c -h" "rc=$rc $out"; fi
done
"$B" answer onlyone >/dev/null 2>&1; eq "answer with missing args is a usage error" "$?" "2"

# ---- .githooks/pre-commit (in a throwaway repo)
echo "pre-commit:"
R="$T/repo"
mkdir -p "$R/.githooks" "$R/tmp-local" "$R/tmp-scratch" "$T/gl-shim"
cp "$REPO/.githooks/pre-commit" "$REPO/.githooks/commit-msg" "$R/.githooks/" && cp "$REPO/.gitleaks.toml" "$R/"
printf 'tmp*/\n' > "$R/.gitignore"
git -C "$R" init -q -b main
git -C "$R" config user.email t@example.invalid && git -C "$R" config user.name t
git -C "$R" config core.hooksPath .githooks
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$T/gl-shim/gitleaks"; chmod +x "$T/gl-shim/gitleaks"
commit() { (cd "$R" && git add -A && PATH="${GL_PATH:-$T/gl-shim}:$PATH" git commit -q -m "${1:-t}" >"$T/pc.out" 2>&1); }
echo "hello" > "$R/a.txt"
commit; eq "no banned-terms file: commit allowed" "$?" "0"
grep -q "banned-terms.txt not found" "$T/pc.out" && ok "missing list warns" || bad "missing list warns" "$(cat "$T/pc.out")"
printf '%s\n' '# comment line' 'example-corp' '\bacme\b   # trailing comment' 'internal\.example\.com' '' > "$R/tmp-local/banned-terms.txt"
echo "nothing to see" > "$R/b.txt"
commit; eq "clean change allowed" "$?" "0"
printf 'line one\nwe work at Example-Corp\n' > "$R/c.txt"
commit; eq "banned term (case-insensitive) blocks" "$?" "1"
grep -q "c.txt:2: we work at Example-Corp" "$T/pc.out" && ok "hit shows file:line" || bad "hit shows file:line" "$(cat "$T/pc.out")"
git -C "$R" reset -q; rm "$R/c.txt"
echo "acme notes" > "$R/tmp-scratch/notes.txt"; echo "k" > "$R/k.txt"
commit; eq "ignored tmp*/ files are not committed" "$?" "0"
git -C "$R" add -f tmp-scratch/notes.txt
(cd "$R" && PATH="$T/gl-shim:$PATH" git commit -q -m t >"$T/pc.out" 2>&1); eq "force-added tmp*/ path fails the commit" "$?" "1"
grep -q "tmp-scratch/notes.txt" "$T/pc.out" && ok "...and names the path" || bad "...and names the path" "$(cat "$T/pc.out")"
git -C "$R" reset -q
echo "acmes are fine, the comment is not a term" > "$R/d.txt"
commit; eq "word-boundary pattern and inline comment" "$?" "0"
mkdir -p "$R/acme" && echo x > "$R/acme/readme.txt"
commit; eq "banned term in a file path blocks" "$?" "1"
git -C "$R" reset -q; rm -rf "$R/acme"
printf '%s\n' '#!/usr/bin/env bash' 'echo leak >&2; exit 1' > "$T/gl-shim/gitleaks"
echo "y" > "$R/e.txt"
commit; eq "gitleaks failure blocks" "$?" "1"
git -C "$R" reset -q; rm "$R/e.txt"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$T/gl-shim/gitleaks"
mkdir -p "$R/scripts" && cp "$REPO/scripts/setup-dev.sh" "$R/scripts/"
out="$(cd "$R" && PATH="$T/gl-shim:$PATH" scripts/setup-dev.sh 2>&1)"
grep -q "ok    .githooks/commit-msg installed" <<<"$out" && ok "setup-dev installs commit-msg" || bad "setup-dev installs commit-msg" "$out"
grep -q "ok    .githooks/pre-commit installed" <<<"$out" && ok "setup-dev installs pre-commit" || bad "setup-dev installs pre-commit" "$out"
rm -rf "$R/scripts"   # its help text contains the sample term
# commit-msg: secrets and banned terms in the message.
echo "z" > "$R/g.txt"
commit "add g for example-corp"; eq "commit-msg: banned term blocks" "$?" "1"
grep -q "banned terms in the commit message" "$T/pc.out" && ok "commit-msg: says why" || bad "commit-msg: says why" "$(cat "$T/pc.out")"
tok="ghp_$(printf 'aB3%.0s' $(seq 1 12))"
commit "add g with $tok"; eq "commit-msg: token-like secret blocks" "$?" "1"
case "$(cat "$T/pc.out")" in *"$tok"*) bad "commit-msg: secret not echoed" ;; *) ok "commit-msg: secret not echoed" ;; esac
commit "add g"; eq "commit-msg: clean message passes" "$?" "0"
echo "z2" > "$R/g.txt"
commit "redact password=/token=/secret= values"; eq "commit-msg: naming the keys is not a secret" "$?" "0"
echo "z3" > "$R/g.txt"
commit "set password=hunter2xyz for now"; eq "commit-msg: password=value blocks" "$?" "1"
git -C "$R" reset -q
if command -v gitleaks >/dev/null 2>&1; then
  # Build a webhook-shaped value at runtime so this file itself never contains one.
  fake="https://hooks.slack"".com/services/T$(printf 'A%.0s' $(seq 1 8))/B$(printf 'B%.0s' $(seq 1 8))/$(printf 'x7Q%.0s' $(seq 1 8))"
  echo "url=$fake" > "$R/f.txt"
  GL_PATH="$(dirname "$(command -v gitleaks)")" commit; eq "real gitleaks blocks a webhook URL" "$?" "1"
  git -C "$R" reset -q; rm "$R/f.txt"
else
  echo "  (gitleaks not installed: real-scan test skipped)"
fi

# ---- install.sh (against a temp settings.json)
echo "install:"
cat > "$CLAUDE_SETTINGS" <<'EOF'
{"model":"x","hooks":{"Stop":[{"hooks":[{"type":"command","command":"/other/hook.sh"}]}]}}
EOF
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" --dry-run > "$T/dry.txt"
eq "dry-run leaves settings untouched" "$(jq -c . "$CLAUDE_SETTINGS")" '{"model":"x","hooks":{"Stop":[{"hooks":[{"type":"command","command":"/other/hook.sh"}]}]}}'
grep -q '^    +.*board-hook.sh' "$T/dry.txt" && ok "dry-run prints diff" || bad "dry-run prints diff"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" >/dev/null
eq "existing hook kept" "$(jq -r '.hooks.Stop[0].hooks[0].command' "$CLAUDE_SETTINGS")" "/other/hook.sh"
eq "hook added to 10 events" "$(jq '[.hooks[][] | select(.hooks[].command | test("board-hook"))] | length' "$CLAUDE_SETTINGS")" "10"
ls "$T"/settings.json.bak-* >/dev/null 2>&1 && ok "backup made" || bad "backup made"
before="$(cat "$CLAUDE_SETTINGS")"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" >/dev/null
eq "install idempotent" "$(cat "$CLAUDE_SETTINGS")" "$before"
[ -L "$T/localbin/board" ] && ok "bin symlinked" || bad "bin symlinked"
[ -L "$T/localbin/board-notify" ] && ok "board-notify symlinked" || bad "board-notify symlinked"
chmod 755 "$BOARD_HOME"; BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" >/dev/null
eq "install makes BOARD_HOME 0700" "$(stat -c %a "$BOARD_HOME")" "700"
TAP="$REPO/hooks/board-statusline-tap.sh"
eq "no previous statusline: tap installed bare" "$(jq -r .statusLine.command "$CLAUDE_SETTINGS")" "$TAP"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" --uninstall >/dev/null
eq "uninstall restores hooks" "$(jq -c .hooks "$CLAUDE_SETTINGS")" '{"Stop":[{"hooks":[{"type":"command","command":"/other/hook.sh"}]}]}'
eq "uninstall removes a statusLine that was not there before" "$(jq -c 'has("statusLine")' "$CLAUDE_SETTINGS")" "false"
[ -L "$T/localbin/board" ] && bad "uninstall removes symlinks" || ok "uninstall removes symlinks"

# statusLine tap around an existing statusline (with quotes), other keys kept, exact restore.
SL_PREV="$BOARD_HOME/statusline.prev.json"
sl_orig='{"type":"command","command":"printf %s \"it'"'"'s $(cat | jq -r .session_id)\"","padding":2}'
jq --argjson sl "$sl_orig" '.statusLine = $sl' "$CLAUDE_SETTINGS" > "$T/s" && mv "$T/s" "$CLAUDE_SETTINGS"
expected="$(printf '{"session_id":"s9"}' | sh -c "$(jq -r .statusLine.command "$CLAUDE_SETTINGS")")"
eq "statusline fixture works" "$expected" "it's s9"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" --dry-run > "$T/dry.txt"
grep -q 'board-statusline-tap.sh' "$T/dry.txt" && ok "dry-run shows the statusLine change" || bad "dry-run shows the statusLine change"
[ -f "$SL_PREV" ] && bad "dry-run writes no statusline backup" || ok "dry-run writes no statusline backup"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" >/dev/null
cmd="$(jq -r .statusLine.command "$CLAUDE_SETTINGS")"
case "$cmd" in "BOARD_STATUSLINE_NEXT="*"$TAP") ok "statusLine wrapped with the tap" ;; *) bad "statusLine wrapped with the tap" "$cmd" ;; esac
eq "other statusLine keys kept" "$(jq -c '[.statusLine.type, .statusLine.padding]' "$CLAUDE_SETTINGS")" '["command",2]'
eq "previous statusLine saved" "$(jq -c . "$SL_PREV")" "$(jq -c . <<<"$sl_orig")"
out="$(printf '{"session_id":"s9"}' | sh -c "$cmd")"
eq "wrapped statusline prints the same" "$out" "$expected"
before="$(cat "$CLAUDE_SETTINGS")"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" >/dev/null
eq "statusline wrap idempotent" "$(cat "$CLAUDE_SETTINGS")" "$before"
eq "backup still the original" "$(jq -c . "$SL_PREV")" "$(jq -c . <<<"$sl_orig")"
BIN_DIR="$T/localbin" "$REPO/scripts/install.sh" --uninstall >/dev/null
eq "uninstall restores the exact statusLine" "$(jq -c .statusLine "$CLAUDE_SETTINGS")" "$(jq -c . <<<"$sl_orig")"
[ -f "$SL_PREV" ] && bad "statusline backup removed after restore" || ok "statusline backup removed after restore"

# ---- fleet-up dry run
echo "fleet-up:"
cat > "$BOARD_CONFIG_DIR/fleet.yaml" <<EOF
sessions:
  - code: NEW
    cwd: $T
    autostart: true
    resume_name: NEW
    extra_args: "--verbose"
  - code: CP
    cwd: $T
    autostart: true
  - code: OFF
    cwd: $T
    autostart: false
  - code: FRESH
    cwd: $T
    autostart: true
    resume: false
    resume_name: FRESH
    extra_args: "--verbose"
    initial_prompt: "You're the \$OPERATOR; read \`x\` and \\"go\\""
EOF
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in has-session) [ "$3" = "=CP" ] && exit 0; exit 1 ;; esac
EOF
: > "$FAKE_TMUX_LOG"
out="$(PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" --dry-run)"
grep -q "would run: tmux new-session -d -s NEW" <<<"$out" && ok "starts missing session" || bad "starts missing session" "$out"
grep -qF '_fleet_t0=$(date +%s); claude -n NEW --remote-control NEW -r '\''NEW'\'' --verbose; [ $(( $(date +%s) - _fleet_t0 )) -lt 15 ] && claude -n NEW --remote-control NEW --verbose' <<<"$out" \
  && ok "resume, fresh only on fast exit" || bad "resume, fresh only on fast exit" "$out"
grep -q "CP: tmux session exists" <<<"$out" && ok "skips existing session" || bad "skips existing session"
grep -q "OFF" <<<"$out" && bad "skips autostart:false" || ok "skips autostart:false"
grep -q "would run: tmux new-session -d -s switchboard" <<<"$out" && ok "starts collector" || bad "starts collector" "$out"
grep -q '^new-session' "$FAKE_TMUX_LOG" && bad "dry-run changes nothing" || ok "dry-run changes nothing"
grep -q "would run: .*board-collect --baseline" <<<"$out" && bad "no baseline when alerts.json exists" "$out" || ok "no baseline when alerts.json exists"
fresh_cmd="$(sed -n 's/^.*FRESH: starting in [^:]*: //p' <<<"$out")"
case "$fresh_cmd" in *" -r "*|*_fleet_t0*) bad "resume: false starts fresh" "$fresh_cmd" ;; *) ok "resume: false starts fresh" ;; esac
printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\n" "$@"' > "$T/bin/fake-claude"; chmod +x "$T/bin/fake-claude"
got="$(sh -c "$T/bin/fake-claude ${fresh_cmd#claude }")"
want="$(printf '%s\n' -n FRESH --remote-control FRESH --verbose "You're the \$OPERATOR; read \`x\` and \"go\"")"
eq "initial_prompt passed as one shell-safe argument" "$got" "$want"
# Lost race: new-session fails (session created meanwhile) -> no keys typed into that pane.
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in has-session) exit 1 ;; new-session) exit 1 ;; esac
EOF
: > "$FAKE_TMUX_LOG"
BOARD_HOME="$T/home5" PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" --only NEW >/dev/null 2>&1
grep -q '^send-keys' "$FAKE_TMUX_LOG" && bad "no send-keys when new-session fails" "$(cat "$FAKE_TMUX_LOG")" || ok "no send-keys when new-session fails"
grep -q "NEW: tmux new-session failed" "$T/home5/fleet-up.log" && ok "lost race logged" || bad "lost race logged"
# The tmux server started by fleet-up must not inherit (and keep) the fleet-up lock.
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  has-session) exit 1 ;;
  new-session) sleep 30 </dev/null >/dev/null 2>&1 & echo $! >> "$FAKE_TMUX_SERVERS" ;;  # "server" keeps our fds
esac
exit 0
EOF
export FAKE_TMUX_SERVERS="$T/tmux-servers"; : > "$FAKE_TMUX_SERVERS"
BOARD_HOME="$T/home6" PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" --only NEW >/dev/null 2>&1
eq "fleet-up exits 0" "$?" "0"
[ -s "$FAKE_TMUX_SERVERS" ] && ok "fake tmux server running" || bad "fake tmux server running"
flock -n "$T/home6/fleet-up.lock" true && ok "lock is free after fleet-up exits (no fd leak into tmux)" \
  || bad "lock is free after fleet-up exits (no fd leak into tmux)"
# A second fleet-up while one holds the lock leaves at once, quietly, with 0.
flock "$T/home6/fleet-up.lock" sleep 5 & holder=$!
sleep 0.2
t0="${EPOCHREALTIME/./}"
o2="$(BOARD_HOME="$T/home6" PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" 2>&1)"; rc=$?
el=$(( (${EPOCHREALTIME/./} - t0) / 1000 ))
eq "concurrent fleet-up exits 0" "$rc" "0"
eq "concurrent fleet-up is silent" "$o2" ""
[ "$el" -lt 2000 ] && ok "concurrent fleet-up does not wait (${el}ms)" || bad "concurrent fleet-up does not wait" "${el}ms"
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
while read -r p; do kill "$p" 2>/dev/null; done < "$FAKE_TMUX_SERVERS"
# Multi-line initial_prompt becomes one line (it is typed into the pane).
printf 'sessions:\n  - code: ML\n    cwd: %s\n    autostart: true\n    initial_prompt: "line one\\nline two"\n' "$T" > "$BOARD_CONFIG_DIR/fleet.yaml"
o2="$(BOARD_HOME="$T/home7" PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" --dry-run --only ML)"
grep -qF "claude -n ML --remote-control ML 'line one line two'" <<<"$o2" && ok "multi-line initial_prompt flattened" \
  || bad "multi-line initial_prompt flattened" "$o2"
resume_cmd="$(sed -n 's/^.*NEW: starting in [^:]*: //p' <<<"$out")"
case "$resume_cmd" in *"&& claude -n NEW --remote-control NEW --verbose") ok "no prompt on sessions without initial_prompt" ;; *) bad "no prompt on sessions without initial_prompt" "$resume_cmd" ;; esac
out="$(BOARD_HOME="$T/home4" PATH="$T/bin:$PATH" "$REPO/bin/fleet-up" --dry-run)"
grep -q "would run: .*board-collect --baseline" <<<"$out" && ok "fleet-up takes a baseline when alerts.json is missing" || bad "fleet-up baseline" "$out"

echo "hook stdin from a socket (as Claude Code passes it):"
SOCK_SID="66666666-7777-8888-9999-000000000000"
python3 - "$HOOK" "$SOCK_SID" <<'PY'
import socket, subprocess, sys
hook, sid = sys.argv[1], sys.argv[2]
a, b = socket.socketpair()
p = subprocess.Popen([hook], stdin=b, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
b.close()
a.sendall(('{"session_id":"%s","hook_event_name":"UserPromptSubmit","cwd":"/w"}' % sid).encode())
a.shutdown(socket.SHUT_WR)
p.wait(timeout=10)
a.close()
PY
[ -f "$BOARD_HOME/state/$SOCK_SID.json" ] && ok "hook reads stdin from a socket" || bad "hook reads stdin from a socket" "no state file: reading /dev/stdin fails on a socket (ENXIO)"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ]
