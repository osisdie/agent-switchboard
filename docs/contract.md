# agent-switchboard: data contract (v1)

This repo is generic. Do not write any machine-, company-, product- or person-specific names, paths or URLs into tracked files. Machine-specific config lives in `tmp-local/`, which is gitignored.

## Local state (`$BOARD_HOME`, default `~/.agent-switchboard`)

```
$BOARD_HOME/
  state/<session_id>.json   # written by hooks/board-hook.sh, one per Claude Code session
  events.jsonl              # append-only audit log (hook events, presses, relays)
  decisions/<id>.json       # written by `board ask` (kind=question) and by the hook (kind=permission,
                            # or kind=question source=pane for an AskUserQuestion picker)
  outbox/                   # reserved
  snapshot.json             # written by bin/board-collect (merged view, see below)
  alerts.json               # board-notify alert state {key: {level, first_at, last_at, acked_at}}; dedupe/repeat per
                            # key and level follow the alerting policy (alerting.yaml / fleet.yaml `alerting:`)
  notify-prefs.json         # notification controls: mutes, min levels, presence policy (see "Notification controls")
  notify-prefs.lock         # flock held by `board mute|unmute|notify-level|notify-presence`
  presence.json             # board-serve: which approver has the board open and visible (presence heartbeats)
  presence.lock             # flock held by board-serve while it writes presence.json
  owner-away                # present = the owner is away (see "Owner away & consensus")
  consensus/<id>.json       # written by `board consensus` while the owner is away
  consensus.lock            # flock held by `board away on|off` and `board consensus propose|vote`
  resources.json            # system resources, sampled by the collector (see "Resources")
  resources-state.json      # the sampler's memory: previous counters, dwell state, teammate activity
  usage-samples.json        # the collector's 5h usage samples and burn rate (see "Usage meter")
  statusline/<session_id>.json  # the statusline tap's session meter {ctx_pct, cost_usd, model, at}
  cards.json                # card extras for board-serve, written by the collector (see "Card extras")
  cards-state.json          # their cache: transcript tails by mtime, git info, when each state began
  rc-prefs.json             # the owner's Remote Control choices (see "Remote Control"); rc-prefs.lock
  session-actions.json      # queued session actions (compact) per session; session-actions.lock
  actions-last.json         # the last attempt per session and action; actions-last.lock
  actions-run.lock          # flock held while a session action types into a pane (one at a time)
  pomodoro.json             # pomodoro timers and 14 days of daily tallies, written by the collector (see "Pomodoro")
  tasks/<id>.json           # lifecycle tasks (`board task`), with optional scheduling fields (see "Scheduler")
  scheduler.json            # the scheduler's decision, written by the collector when it changes (see "Scheduler")
  schedule-state.json       # jam episodes and advisory delivery (see "Scheduler"); schedule.lock
  schedule-off              # present = the scheduler is paused (`board schedule off`)
  lessons.jsonl             # what the scheduler learned (append-only, 0600; see "Lessons"); lessons.lock
  apikeys/<kid>.json        # one API key: its peppered hash and metadata, never the key (0600; see "API keys")
  apikeys.lock              # flock held by `board apikey` while it changes a key
  secrets/apikey.pepper     # 32 random bytes for the key hashes (0600, made on the first create or request)
```

### state/<session_id>.json
```json
{ "session_id": "...", "pid": 123, "cwd": "...", "tmux_pane": "%7",
  "state": "working|needs-you|done|rest|offline",
  "last_event": "Stop", "notification_type": "permission_prompt",
  "pending": { "tool_use_id": "...", "tool_name": "Bash", "summary": "first 200 chars of input (redacted)",
               "summary_truncated": false, "summary_len": 57, "summary_sha256": "<hex of the full input>",
               "ambiguous": false, "decision_id": "d-..." } | null,
  "recent_pre": [ {"tool_use_id": "...", "tool_name": "Bash", "summary": "...", "at": 0, ...} ],
  "recent_done": [ {"tool": "Bash", "summary": "npm test (redacted, <= 120 chars)", "ms": 1840,
                    "ok": true, "at": 1731500000000} ],
  "updated_at": 1731500000000 }
```

`recent_pre` holds the tool calls in flight (from `PreToolUse`, dropped at `PostToolUse` /
`PostToolUseFailure`, emptied at `Stop`). `recent_done` is a ring of the last 8 completed ones,
built in the same jq call and state file write: the tool name, the in-flight entry's redacted
summary clipped to 120 chars, the duration since its `PreToolUse` (`null` if unknown), `ok: false`
for `PostToolUseFailure`, and the end time. It survives `Stop`.

Map hook events to states like this:

| Hook event | State |
|---|---|
| `UserPromptSubmit`, `PreToolUse`, `PostToolUse` | working |
| `PermissionRequest`, Notification `permission_prompt` | needs-you, with `pending` set |
| Notification `idle_prompt`, `Stop` | done |
| `SessionEnd` | offline |
| `SessionStart` | rest |

`pending` is cleared on the next `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit` or `Stop`. The hook writes the state file atomically (write to a temp file, then `mv`). It never blocks Claude, prints nothing to stdout and always exits 0. It does not record prompt text. The only input it captures is a ≤200-char summary of the pending tool's input, and for `AskUserQuestion` its questions and options (redacted and clipped, see Pane questions). Token-like values (bearer tokens, Slack/GitHub/AWS keys, board API keys, `password=`/`token=`/`secret=`/`api_key=` values, long base64/hex runs) are replaced by `[REDACTED]` before storing. `summary_truncated`, `summary_len` and `summary_sha256` describe the full input string. When a `PermissionRequest` carries no `tool_use_id` and more than one in-flight tool use could match it, the hook does not guess: `pending.ambiguous` is `true` and no permission decision is created. `$BOARD_HOME` is mode 0700 and its files 0600 (umask 077 in every writer).

### snapshot.json (collector output)
```json
{ "generated_at": 1731500000000,
  "sessions": [ { "code": "CP", "aliases": ["CP"], "name": "CP", "pid": 1, "session_id": "...",
                  "cwd": "...", "tmux": "CP:@3.%7", "tmux_pane": "%7",
                  "status": "busy|idle", "state": "working|needs-you|done|rest|offline",
                  "pending": {...}|null, "remote_url": "https://claude.ai/code/<bridgeSessionId>"|null,
                  "updated_at": 0,
                  "title": "Fix the login form"|null, "repo": "web-app"|null, "branch": "main"|null,
                  "last_event": "PreToolUse"|null, "notification_type": "permission_prompt"|null,
                  "recent": [ {"tool": "Bash", "summary": "npm test -- …/login.spec.ts"} ],
                  "rc": { "on": true|false|null, "want": "on|off", "source": "session|all|fleet|default",
                          "blocked": "do_not_track"|null, "pending": false,
                          "last": {"want","result","reason","at"}|null },
                  "compact": { "queued": {"at","by"}|null, "last": {"result","at"}|null, "done_at": 0|null } } ],
  "decisions": [ { ...decision file... } ],
  "tasks": [ ... ], "limit": {...}|null,
  "owner_away": { "since": 0, "by": "CP", "said": "..." } | null,
  "resources": { "level": "ok|warn|high|critical", "worst": "mem|swap|disk:<id>|load|oom"|null } | null,
  "schedule": { "mode": "normal|conserve|wait-reset|degraded|off|error", "due": ["API"], "jams": 0 } | null,
  "consensus": [ { "id": "c-...", "from": "CP", "participants": ["CP","API"], "ref": "...", "proposal": "...",
                   "status": "pending|agreed|objected|owner_back", "votes": [ {"by","vote","at","note"} ],
                   "created_at": 0, "updated_at": 0, "decided_at": 0|null } ] }
```

- **Where sessions come from:** live entries in `~/.claude/sessions/<pid>.json`, where `/proc/<pid>` exists. Each is merged with `state/<session_id>.json`.
- **`code`:** the tmux session name (the part before `:` in `tmux`). If there is no tmux, use the registered `name`. `fleet.yaml` may map aliases to codes.
- **The registered name wins over the tmux session.** A process whose registered `name` maps to a
  fleet code (code or alias) gets that code, even when it runs inside another session's tmux.
- **One process, one card.** When several live processes resolve to the same code (teammate panes
  inside a session's tmux, a session started or renamed inside another session's tmux), the one
  whose registered name is the code or an alias and that started first keeps it. Each other one
  takes the fleet code its own registered name maps to, if no live process holds that code;
  otherwise it becomes `CODE/<name>`.
- **Sessions in `fleet.yaml` that aren't running** appear with state `offline`.
- **Card details** (all untrusted text: control characters and ANSI escapes removed; URL
  credentials (`scheme://user:pass@`) dropped; tokens redacted with the hook's patterns; any path
  (absolute, `~/`, `~user/`, `C:\` or `C:/`, UNC `\\server\share`, `file://`, quoted with spaces)
  cut to `…/<last part>`; clipped):
  - `title`: the pane title Claude Code sets (`✳ <summary>`), from one `tmux list-panes -a` per run,
    leading status glyph dropped, at most 120 chars. Dropped entirely when it is tmux's default (the
    host name), a shell's own title (`user@host: dir`), or names this host (short name, FQDN,
    `<short>.local`) or the local user as a word.
  - `repo` / `branch`: from one `git rev-parse --show-toplevel --abbrev-ref HEAD` in `cwd`: the
    basename of the top level and the current branch (`detached` for a detached HEAD, `no commits`
    on a new branch), cached per cwd for 30 s in `$BOARD_HOME/gitinfo.json`.
    Never the path itself.
  - `last_event`, `notification_type`: from the hook state file.
  - `recent`: the last 3 tool calls in flight (the hook's `recent_pre`): tool name and the hook's
    redacted summary, at most 160 chars. Self-hosted board only: the operator never mirrors it.
- **`rc`** (Remote Control, see "Remote Control"): `on` is whether the running process has a
  `bridgeSessionId` (`null` when offline); `want` / `source` come from `rc-prefs.json`, then
  `fleet.yaml`, then the default (on); `pending` is true when an explicit board choice (`source`
  `session` or `all`) differs from `on` on a running session that can get it; `blocked` is
  `do_not_track` when the process has `DO_NOT_TRACK` set (read from `/proc/<pid>/environ`, the
  name only), so Remote Control is unavailable in it; `last` is the last attempt for this
  session process.
- **`compact`** (see "Session actions"): a queued compact, the last attempt for this session
  process, and when the board last saw it finish (`done_at`).
- **`owner_away`** is `null` when `$BOARD_HOME/owner-away` does not exist, else its record. A file
  that is not a JSON object (free text, empty) still means away: `{since: <mtime ms>, by: null,
  said: <first 300 chars>, legacy: true}`.
- **`resources`** is only the digest of `resources.json`: it changes when a level changes, never
  for raw numbers, so a Monitor on `snapshot.json` does not wake every sample. `null` when
  `resources.enabled: false`.
- **`consensus`** lists every `pending` item, plus `agreed` / `objected` / `owner_back` items for
  24 h after `decided_at`, oldest first. Text fields are clipped (proposal, ref, said: 300 chars;
  note: 200) and control characters become `?`.
- **Not in the snapshot:** the card extras (next section). They change with raw numbers (RSS, cost,
  context %), so they would wake the operator's Monitor; and some are approver-only.

### Card extras: cards.json (collector output, board-serve only)

`lib/cards.py` writes `$BOARD_HOME/cards.json` (0600) on every collector run, rewriting it only
when it changed. board-serve adds each entry to its session row in `/api/state` as `card`. The
operator never mirrors it to the Artifact, and no alert carries any of it.

```json
{ "sessions": { "CP": {
    "state_since": 1731500000000,
    "teammates": [ {"name": "dev", "title": "Owner away mode and consensus", "busy": true,
                    "idle": false, "idle_s": 12, "rss_mb": 310} ],
    "meter": {"ctx_pct": 34, "cost_usd": 2.1, "model": "<display name>", "at": 0} | null,
    "done": [ {"tool": "Bash", "summary": "...", "ms": 1840, "ok": true, "at": 0} ],
    "doing": "the last assistant text, one line, <= 280 chars" | null,
    "prompt": "the first line of the last prompt, <= 120 chars" | null,
    "branch": {"base": "origin/main", "ahead": 2, "behind": 0, "subject": "feat: ..."} | null } } }
```

- **`state_since`**: when the collector first saw the card in its current `state`. Offline cards
  carry only this.
- **Pruning**: at most every 10 minutes the collector removes `statusline/<sid>.json` and
  `state/<sid>.json` (with the hook's `state/.<sid>.lock`) of sessions that are no longer live and
  whose file was not written for 24 h.
- **`teammates`**: the teammate processes `resources.json` maps to the session (its `idle`,
  `idle_s`, `rss_mb`). `title` is the teammate's tmux pane title, cleaned like a card `title`: the
  pane is found by walking up to 6 ancestors of the teammate's pid (`<proc>/<pid>/stat`) to a
  `#{pane_pid}` from the same batched `tmux list-panes -a`. `busy` is `true` for a braille spinner
  glyph, `false` for `✳`, `null` when unknown.
- **`meter`**: `$BOARD_HOME/statusline/<session_id>.json` (see "Usage meter"), dropped after 24 h.
- **`done`**: the hook's `recent_done`, run through the secret guard and clean_text again.
- **`doing` / `prompt`**: from the session transcript, `<projects>/<cwd, each non-alphanumeric
  character as ->/<sessionId>.jsonl`, else a glob over `<projects>/*/<sessionId>.jsonl` (retried
  at most once a minute). `<projects>` is `CLAUDE_PROJECTS_DIR`, else `projects` next to the
  session registry (`~/.claude/projects`). Only the last 64 KB is read, and only when the file's
  mtime or size changed; at most 8 tails and 8 globs per run, busiest sessions first. `doing` is
  the text of the last assistant message with text; `prompt` the first line of the last user
  prompt (a teammate message's wrapper removed; tool results, meta entries, command output and
  system reminders skipped). A value not found in the tail keeps the cached one. Both go through
  `guard.scrub` and clean_text (no paths, no control characters, the hook's redaction).
  `prompt` is only kept with `fleet.yaml` `cards.show_prompt: true` (the default).
  A transcript is read only when its real path (symlinks resolved) is under `<projects>`; a cached
  path that is not is dropped.
- **`branch`**: `git rev-list --left-right --count HEAD...<base>` where `<base>` is `origin/HEAD`
  (else `origin/main`, else `origin/master`) as the local refs know it; never a fetch. `subject`
  is `git log -1 --format=%s`, scrubbed and clipped to 100. Cached per cwd for 60 s.

**Who sees what.** `doing` and `prompt` are approver-only: `/api/state` (and its SSE stream) drops
them for anyone not in `approvers`, the rule `account` uses, and puts `card.private: "hidden"`
instead. Everything in `card` also passes the guard's `scrub_tree` like the rest of the state.

### decisions/<id>.json
```json
{ "id": "d-<epoch>-<rand>", "kind": "question|permission", "from": "CP",
  "question": "...", "options": ["A","B"], "context": "optional short text",
  "tool_use_id": "... (permission only)", "tool_name": "...", "summary": "...",
  "summary_truncated": false, "summary_len": 0, "summary_sha256": "... (permission only)",
  "status": "pending|answered|relayed|stale|timeout|unauthorized|answered_in_pane",
  "answer": null, "answered_by": null, "answered_at": null, "relayed_at": null,
  "created_at": 0 }
```

**Board asks answered elsewhere.** A plain board ask (`kind: "question"`, no `source: "pane"`, no
`gate` / `task_id`) that was answered in the session's pane, or by the owner in another session,
is closed with `board resolve <id> --in-pane --from <CODE>|--by <approver> [--note-file F]`:

- `--from` must be the ask's `from` (or an alias of the same fleet code), inside tmux this tmux
  session, and the command must run inside that session: the first ancestor of the `board` process
  with a registry file (`~/.claude/sessions/<pid>.json`) that describes that very process (not a
  zombie, and its `procStart` matches the start time in `/proc/<pid>/stat`: lib/actions.py
  `same_process`; a reused pid is skipped and the walk goes on) must be that session (its tmux
  session name, else its registered name). No such ancestor fails closed, so a forged `TMUX` / `TMUX_PANE`
  or a shell outside the session cannot use `--from`; a teammate (its own registered process) cannot
  either: the session itself or an approver closes it. `--by` must be in `approvers`. Anyone else:
  exit 4, audited (`type: "resolve"`, `result: "refused"`). A permission, pane question or gate:
  exit 2, nothing changed (a gate is approved by an answer, never closed from the pane).
- Only `pending` or `unauthorized`: an `answered` ask (an approver's board answer waiting for its
  relay) is refused with exit 1, "already answered: it will be relayed", for `--from` and `--by`
  alike. Already final: exit 1.
- **Trust model.** `--from` guards against honest mistakes (a session closing another session's
  ask, a shell outside any session), within same-user trust: it is not a security boundary, since
  any process of the same user can write a registry file. It proves only that the command runs
  inside a process tree a registered session started; a session can still act as itself. Where it
  cannot tell, it refuses: no `/proc` (the start-time check needs it), or a sandbox that unshares
  the pid namespace (the session's process is not visible); use `--by` there. `--by` is a claim the local CLI cannot verify
  (any local user can type it), like `board answer --by`; on the board the identity comes from
  board-serve (loopback or a verified Access JWT).
- `board mark` never re-marks a final status (`relayed`, `stale`, `timeout`, `answered_in_pane`) to
  another one: exit 1, unchanged. Marking the status it already has is a no-op (exit 0). The one
  exception is inside `board press`: its keys went in, so it marks `relayed` over `stale` (the
  collector may have marked the prompt stale between the key press and the mark); never over any
  other final status, and never from `board mark` or the environment.
- The note goes through the guard: refused with exit 6 when it asks for a secret value, otherwise
  scrubbed and clipped to 500 characters.
- The record gets `status: "answered_in_pane"` (final, like `relayed`), `resolved_by`,
  `resolved_via` (`session` | `approver`), `resolved_at`, `resolve_note`; the hint is removed.
  The operator mirrors the status and never relays an answer for it.
- **Hint, never an auto-close.** The hook records `last_prompt_at` (UserPromptSubmit) and
  `last_stop_at` (Stop / StopFailure) in the state file. On every run the collector sets
  `hint: "maybe_answered_in_session"` and `hint_at` (that Stop) on a pending plain ask when its
  asking session's `last_prompt_at` is after the ask's `created_at` and its `last_stop_at` is after
  that prompt: someone may have answered in the pane. It is set once and only shown. Like every
  collector write to a decision, it re-reads the file right before writing and skips it when the
  status moved meanwhile (an answer or a resolve wins).
- board-serve: `POST /api/decisions/<id>/resolve` `{"confirm": "resolve", "note"?}`, approvers only
  (403 `not_approver`, audited), runs `board resolve <id> --in-pane --by <approver>` (the note by a
  0600 file) → 200 `{status: "answered_in_pane"}`; exit 2 → 409 `not_a_board_ask`; exit 6 → 400
  `held_note`; otherwise 409 `gone`. The page offers it as **Mark answered in session** with a
  confirm step, on plain asks, to approvers on the self-hosted board.

**Pane questions** (`kind: "question"`, `source: "pane"`): the hook's record of an
`AskUserQuestion` picker. Claude Code sends `tool_input: {questions: [{question, header,
multiSelect, options: [{label, description}]}]}` on `PreToolUse` (with `tool_use_id`) and again on
`PermissionRequest` (without it). The hook treats that `PreToolUse` as the prompt (state
`needs-you`, `pending` set) and creates one decision per `tool_use_id`:

```json
{ "id": "d-...", "kind": "question", "source": "pane", "from": null,
  "question": "first question", "options": ["label", "..."],
  "questions": [{ "question": "...", "header": "...", "multiSelect": false, "question_clipped": false,
                  "options": [{ "label": "...", "description": "..." }] }],
  "questions_clipped": false, "labels_clipped": false, "pressable": true,
  "tool_use_id": "toolu_...", "tool_name": "AskUserQuestion", "summary": "first question",
  "summary_sha256": "...", "session_id": "...", "tmux_pane": "%3", "status": "pending", ... }
```

- Text is redacted like a summary, control characters become spaces, and fields are clipped:
  question 300, header 40, label 80, description 160 characters (at most 6 questions and 10
  options each). `questions_clipped` is true when anything was cut; `labels_clipped` only when what
  an answer is matched by was: an option label, or options or questions left out. Each question's
  `question_clipped` says its text was cut (the page shows it as stored, marked "(shortened)"),
  `header_clipped` that its header was. Decisions without `labels_clipped` (older hooks) count any
  clipping.
- `options` is the label list of a single question (empty for several). `from` is filled with the
  session code by the collector, which also marks the decision `stale` when the session no longer
  shows that `tool_use_id` pending (the human answered in the pane, `PostToolUse`, `Stop`).
- `pressable` = 1–4 questions, none `multiSelect`, no label clipped (`labels_clipped`
  false; a long question or header alone is fine), 1–8 options each, labels unique within each
  question, and no question's start (as stored, up to a clip, whitespace removed) begins
  another's. Otherwise the card is read-only ("answer in the session") and board-serve refuses
  answers (409 `answer_in_session`). A pressable form of several questions is answered with one
  option per question (below); the Artifact page has no relay for it and keeps it read-only.
- `board answer` adds `answer_index` (1-based option number when the answer equals a label, exact
  or a unique case-insensitive match; else `null` = free text) and `relay`: `"press"` when
  `pressable` and the answer is an option or one line of ≤200 printable characters, else
  `"operator"` (sent as `[BOARD decision <id>] <answer> (by owner on board)`).

### Permission decisions: who may press

- `board press` sends keys only after `board answer <id> allow|deny --by <id>` recorded an
  approver's answer with that same action (exit 4 otherwise), and only while the hook state still
  shows that `tool_use_id` (and `summary_sha256`) pending, the pane shows a permission prompt, and
  the start of the summary is visible in the pane (exit 3 otherwise, decision `stale`; exit 7 when
  no prompt is on screen at all, see "A scrolled pane").
- **No approvers configured fails closed** for every decision, not only production. A decision is a
  production decision when `gate` or `stage`, trimmed and lowercased, is `"production"`.
- **Truncated summaries:** when `summary_truncated` is true the human cannot see the whole input on
  the board. The page must **not offer one-click allow** for such a card (show "answer in the
  session's pane" instead); `board press` refuses unless `--allow-truncated` is passed by a human
  who read the full input in the pane.

### Pane questions: who may press

`board press <CODE> answer --expect <tool_use_id>` answers a pressable pane question:

- exit 4 (nothing sent) unless an approver answered it, `relay` is `"press"` and `answer_index`
  matches the answer again;
- exit 3 (nothing sent, `stale`) unless the state still shows that `tool_use_id` and
  `summary_sha256` pending in that pane, the last `press.lines` lines match `press.question_regex`
  (default `Enter to select.*navigate`, the picker footer), and the start of the question (≥ 8
  characters, whitespace ignored) or every option label in order is visible. When the question
  was clipped (`questions_clipped`), both are needed: its start, which is never in the clipped
  tail, and every label in order. The state is read again right before any key.
- Keys: an option sends its number (`1`–`8`), which selects it at once in the picker (measured on
  Claude Code's picker: digits select, they do not only move).
- Free text: before any key, row N = options + 1 within the last `press.lines` lines must match
  `press.free_text_label` (default `Type something`), else exit 3. Then it sends N, and needs a
  focused `^\s*(❯|>|›)\s*N\.` row with that label in the last `press.lines` lines; types the text
  with `send-keys -l` (a trailing `;` is sent as `\;`, which tmux would otherwise eat; text ending
  in `\;` is never typeable, so it relays by message); then needs a focused row N **containing**
  the typed text (first 20 non-space characters) in the last `press.lines` lines, and the state
  unchanged (same `tool_use_id` and `summary_sha256`), before it sends `Enter`. Text visible
  anywhere else (a description, scrollback) does not count: Enter outside the picker would send
  the text to the session as a prompt.
- **Claude Code in the pane.** Before any key, the pane's `#{pane_current_command}` must match
  `press.tui_command_regex` (default `^(claude(\.exe)?|node|[0-9]+(\.[0-9]+){2})$`: `claude`, `claude.exe`, `node`, or a version number
  `X.Y.Z`, the native installer's binary, named after its version; empty = not
  checked), else exit 3 (`stale`, `status_reason: "screen"`): a TUI that crashed back to a shell
  would read the keys, the scroll key included, as readline input.
- **A scrolled pane (exit 7).** When the last lines match neither `press.prompt_regex` /
  `press.question_regex` but one of the last 2 non-blank lines matches `press.scrolled_regex`
  (default `^[[:space:]]*[0-9]+ new messages? .*ctrl\+End.*$`, a whole line: the TUI's "4 new messages (ctrl+End) ↓"; a line the session writes,
  such as "3 new messages for you" or "see below ↓", does not match), press sends
  `press.scroll_key` (default `C-End`, scroll to the bottom; it never submits in Claude Code's TUI)
  once, logs
  `result: "scroll"`, waits `BOARD_PRESS_SETTLE` and captures again; every later check uses the new
  capture. If the prompt / picker is still not visible while the state still shows the same
  `tool_use_id` and `summary_sha256` waiting, it exits 7: nothing sent but the scroll key, attempt
  logged `result: "not_visible"`, and the decision goes back to `pending` with `answer`,
  `answered_by`, `answered_at`, `answer_index` and `relay` cleared and `press_note` /
  `press_note_at` set (a `mark` event with `status: "pending"`). board-serve answers 409
  `screen_not_ready`. The collector marks it `stale` once the session moved on (PostToolUse, Stop or a
  different pending prompt). If the state changed meanwhile it is exit 3 and `stale` as before. A
  prompt on screen about other input is still exit 3. On the page, the decision card shows the note
  and its buttons; a session card whose prompt still waits but whose decision is no longer pending
  shows "Answer in the session (screen scrolled)" (or "... the board can no longer press this
  prompt" without a `press_note`) instead of a link.
- **Not delivered.** A decision whose answer was recorded but never reached the session carries
  `status_reason`, a fixed code (never free text): `screen` (press refused, exit 3), `partial`
  (exit 5), `moved_on` (the collector marked it stale: the session moved on), `timeout`,
  `not_approver` (unauthorized), `unknown_session` (no snapshot, row, pane or hook state for the
  session), `config` (press settings incomplete), `operator` (`board mark ID stale --reason
  operator`). `screen` is only for what the pane shows; a hook state past the prompt is `moved_on`.
  `board answer` clears `status_reason` and `press_note` when it records an approver's answer. Relayed clears it; exit 7 removes it (pending again). board-serve passes it to the
  page and, for a record from before the code, derives `screen` / `partial` / `not_approver` from
  the last `mark` / `unauthorized` event's reason prefix (never its text). The page's Recent
  decisions log shows such a decision (an answer, status `stale` / `timeout` / `unauthorized`) with a
  `not delivered` (or `partial`) chip and "Not delivered: <reason> → <answer>"; a session card whose
  prompt still waits says "Your answer was not delivered (<reason>). Answer in the session."
- **exit 5** (partial): a check failed after the first key. Some keys were sent, Enter was not
  pressed; the decision becomes `stale` with reason `press partial: ...`, the attempt is logged
  with `result: "partial"`, and board-serve answers 409 `partial` ("keys were sent but Enter was
  not pressed; finish in the session"). Never retry it.
- `board answer <id> <label> --by <who> --option N` (sent by the page's option buttons through
  board-serve as `{answer, option}`): refused with exit 2, nothing written, unless option N's
  label is the answer (both trimmed); board-serve answers 400 `bad_option`. Labels are stored
  trimmed by the hook, and a question whose labels repeat (case-insensitive) is not `pressable`.

### Forms of several questions

Claude Code draws them with a tab bar (`←  ☐ Color  ☐ Size  ✔ Submit  →`), one question at a
time: a digit picks an option and moves to the next question; after the last one a review screen
(`Review your answers`, one `→ <label>` line per question, `Ready to submit your answers?`,
`1. Submit answers`, `2. Cancel`, no footer) waits, and its digit submits.

- `board answer <id> --form-file <path> --by <who>`: the same with free text for some questions.
  The file holds `[{"question_index": 0, "option": 2}, {"question_index": 1, "text": "..."}, ...]`:
  one entry per question, in order (`question_index` optional, but when given it must be the
  entry's position), each with exactly one of `option` (within its question's options, ≤ 8) or
  `text`. Free text is untrusted: it only ever comes by file (never a shell argument). Control,
  escape and line-break characters (C0, C1, U+2028/2029), zero-width and bidi controls become
  spaces, runs of space one, trimmed; it must then be 1–200 characters, not end in `\;`, and its
  question must have ≤ 8 options (its "Type something" row is digit N+1). Anything else → exit 2,
  nothing written; so is a text that reads as the "Type something" row itself
  (`press.free_text_label`, any case, trailing dots), which the echo check could not tell apart
  from that row. Each text then goes through the secret guard after the approver check: a request
  for a secret value (`guard.py screen-answer`) and a secret-shaped value (one the scrubber would
  change) are both held, exit 6, nothing written, audited. Such a text is never typed redacted. It records `answer` = `Header: label ·
  Header: text`, `answer_options` (a text question gets its "Type something" row, options + 1)
  and `answer_texts` (the cleaned text, or `null` for an option); `--options` records
  `answer_texts: null`.
- `board answer <id> --options 2,1 --by <who>`: one option number per question, nothing else (no
  answer text, no `--option`). Refused with exit 2, nothing written, unless the decision is a pane
  question of ≥ 2 questions, the count matches, every number is within its question's options,
  and no question is `multiSelect`. It records `answer` = `Header: label · Header: label` (`Q<n>`
  without a header), `answer_options` (the numbers), `answer_index: null`, and `relay: "press"`
  when `pressable`. Any other answer to a form (free text) relays by message (`"operator"`), and
  `board press` refuses it (exit 4).
- `board press <CODE> answer --expect <tool_use_id>`, with the same approver, state and hash
  checks as one question. `press.tabbar_regex`, `press.review_regex`, `press.question_regex` and
  `press.focus_regex` (the focused-row marker, default `(❯|>|›)`, shared with single questions)
  must be valid extended regexes (the tab bar and focus ones for awk too), and `press.submit_label` plain text (matched literally), checked
  before any key. Whitespace is removed the same way from the screen and the stored text, Unicode spaces included
  (NBSP, U+2000–U+200B, U+3000, ...). Only the lines after the last tab bar count: the prompt above the form can
  repeat the questions. For each question in order: the first text after the tab bar is that
  question (all of it, or its stored start when clipped), then its labels in order, then the
  picker footer (`press.question_regex`); after a digit, the screen must also differ from the one
  the digit was sent on. Each later screen is awaited up to `BOARD_PRESS_TRIES` captures (default
  20, `BOARD_PRESS_SETTLE` seconds apart), and the state is re-read before each. On the review
  screen (`press.review_regex`): every question's start in order, exactly one `→` line per
  question, each equal to the chosen label (whitespace ignored), and exactly one row containing
  `press.submit_label`: the focused (`press.focus_regex`) `N.` row, after the answers, followed only by numbered rows;
  then its digit N. **Enter is never sent on the review screen.** A mismatch before the first digit → exit 3, nothing
  sent; after it → exit 5 (partial), `stale`, logged `partial`.
- **Free text on a question** (`answer_texts[i]`), as recorded on a private tmux server: the
  digit of the "Type something" row focuses it without leaving the question, typed text replaces
  the row's label (`❯ 3. teal green`), Enter confirms it and moves to the next question, and the
  review shows `→ <text>`. `board press` checks that row N+1 of the question shows
  `press.free_text_label` before the digit; after it, that row must be the focused one, still
  showing the label; then it types the text with `send-keys -l --` (a trailing `;` sent as `\;`);
  then the focused row N+1 must show the typed text right after `N+1.` (its first 20 non-space
  characters), and not still show the unedited label, and the state must be unchanged, and only
  then it sends `Enter`. The review line of that question must equal the text; a text the review
  wraps continues on lines indented 4+ spaces (not a `●`, `→` or numbered row), which are joined
  to it first. A failed check after the first digit → exit 5 (partial):
  typed text whose echo does not match is never confirmed with Enter. A form hidden by a scrolled pane goes
  back to pending (exit 7) with `answer_options` and `answer_texts` cleared. The decision's
  `status_reason` on these paths: `screen` (what the pane shows), `moved_on` (the hook state moved
  past the prompt, before or after a key), `config` (`press.free_text_label` missing or not a
  valid pattern), `partial` (keys sent, the form not submitted; the board words it so for a form).

## Artifact db (written by the BOARD operator session via Artifact write_db; page reads/writes)

The page declares exactly these capabilities:

```json
{ "db": { "rules": [
    { "path": "", "read": "view", "write": "admin" },
    { "path": "answers", "read": "view", "write": "self" },
    { "path": "answers/{self}", "read": "view", "write": "interact" } ] },
  "user": {}, "room": {} }
```

Rules:
- Everything is `read: "view"`. Shared documents need `"admin"` to write (Owner and Editors).
- `answers/<userId>/...` is each viewer's own subtree. Only that viewer writes it, from Contributor
  (`interact`) up. `write: "self"` on the prefix means nobody else can, **not even the owner**.
- So **who answered is the `<userId>` in the path**, which the db guarantees, not a field the page
  writes. Approvers are shared as **Contributor**: they can answer only as themselves, and they can't
  publish code, rules or shared documents.

| Collection / doc | Writer | Fields |
|---|---|---|
| `sessions/{code}` | operator | `code, aliases[], state, status, task` (short summary, never raw prompt), `tmux, remote_url, pending{tool_name,summary,tool_use_id}, updated_at, title, repo, branch, last_event, notification_type`. **Never `recent`**: tool summaries can hold command text, so they stay on the self-hosted board |
| `decisions/{id}` | operator | fields as in `decisions/<id>.json` above. Only the operator writes `answer, answered_by` (= the path id of the answer it relayed), `answered_at`, `status`, `relayed_at`. An Editor's page may set `status: "answered"` as a display hint. The operator ignores it, and ignores who set it. |
| `answers/{userId}/d/{decisionId}` | that user (page) | `{answer, answered_at, note}`. For permission cards, `answer` is `"allow"` or `"deny"`; the page never offers `"allow"` when the decision has `summary_truncated: true`. The operator lists `answers/<id>/d` for each id in `approver_ids` ∪ `release_manager_ids`, takes the identity **from the path**, and runs `board answer <decisionId> <answer> --by <id>`. If several approvers answered, the earliest `answered_at` wins. |
| `messages/{id}` | page (Editors) creates; operator updates | `to` (code or `"all"`), `body`, `by` (user id), `created_at`, `status: "new"\|"relayed"\|"replied"`. The operator adds `relayed_to[]` and `relayed_at`. |
| `messages/{id}/replies/{rid}` | operator | `from` (code), `body`, `created_at` |
| `meta/operator` | operator | `heartbeat_at, version, owner_id`. The page shows "operator offline" if the heartbeat is older than 10 minutes. |
| `meta/away` | operator | `{active: true, since, by, said, legacy?, updated_at}` while the owner is away, `{active: false, updated_at}` otherwise. The page shows the away banner only for `active: true`. |
| `consensus/{id}` | operator | a `snapshot.consensus` item as it is. Read-only on the page: no page write path, never relayed as a `[BOARD decision]`. |
| `meta/notify` | operator | the notification controls, read-only on the page: `board-notify status --json` → `.controls` without `presence.present`, plus `channels` (`{slack: true, ...}`, booleans only). See "Notification controls". |
| `meta/resources` | operator | `board resources --mirror`: the `/api/state` `resources` block with percentages bucketed to 5% and sizes to 100 MB. Written only when `snapshot.resources` changes, at most every 10 minutes. Read-only on the page. |
| `meta/pomodoro` | operator (optional) | `board pomodoro --mirror`: today's fleet and per-stream (session and teammate) pomodoro counts and focus, and 14 days of totals. Written when today's fleet count changes or the day rolls over. Read-only on the page; no running clock. See "Pomodoro". |
| `meta/ledger` | operator | `rows[]`: `{version, date, what, review, lead:{input,cache_read,cache_write,output,total}, teammate, note}` and `total{}`. The page renders this as the "版本與花費" (versions and cost) table in an Info dialog/footer. |

## Local adapter API (`bin/board-serve`, `lib/serve.py`)

The self-hosted alternative to the Artifact db. `board-serve` serves `artifact/board.html` with an
injected `<meta name="board-mode" content="local">`. The page then uses its local adapter instead
of `claude.use("db")`. It runs one or two listeners, both on `127.0.0.1`:

| listener | address | Host allowed | identity |
|---|---|---|---|
| local | `127.0.0.1:serve.port` (39410) | `127.0.0.1:<port>`, `localhost:<port>` | `local`, but only for a direct request. Any `Cf-Connecting-IP`, `Cf-Ray`, `X-Forwarded-*`, `Forwarded`, `X-Real-IP` or `Cf-Access-*` header → 403 `forwarded_refused` |
| tunnel | `127.0.0.1:serve.tunnel.port` (port + 1), when `serve.tunnel.enabled` | `serve.tunnel.hostname` (or `:443`) | the `email` claim of a **verified** `Cf-Access-Jwt-Assertion` (RS256 against `https://<team_domain>/cdn-cgi/access/certs`, cached; `aud` = `serve.tunnel.aud`, `iss` = `https://<team_domain>`, `exp`/`nbf`). Never `local`. The `Cf-Access-Authenticated-User-Email` header alone is ignored. Missing or invalid token → 401 |

On the tunnel listener a verified email must also be in `serve.tunnel.allowed_emails` (default:
the emails in `approvers` ∪ `release_managers` ∪ `askers`), or every route, `/api/state` included,
answers 403 `not_allowed`. Listing a viewer there lets them read the board; only approvers decide.
(`owners` are in the fallback set too.)
When `allowed_emails` is set it is authoritative: an asker missing from it cannot reach the board
(`board doctor` warns).

**Askers** (`fleet.yaml` `askers`, emails and `local`, matched like approvers) see the board as a
viewer does (every decision's question, session cards, tasks without ticket text, all messages and
replies) and may write only messages (`POST /api/messages`), so list only people you would let view
the board. Every other write route refuses them
(403, audited) exactly as it refuses a viewer: answer, resolve, release, away, notify-prefs,
presence, alerts ack, rc, lessons, session actions. They read no tickets (`/api/tickets/*` is
approvers ∪ release managers only). An asker who is not also an approver or release manager:

- gets `"asker": true` and `"can_message": true` in the state; the page shows the composer (with a
  note that their messages are information) and no answer buttons or approver controls;
- may send at most 10 messages in 10 minutes (429 `rate_limited` with `Retry-After`, audited);
- has messages stored with `kind: "asker"`, `asker: <id>`, the body defanged (`[BOARD` → `(BOARD`,
  fullwidth and CJK brackets too) and "by owner" turned into "by (claimed) owner". They are relayed
  as `[BOARD asker <id>] from <id> (an asker, not an approver: information only, not an approval or
  a decision): <body> — reply to BOARD via SendMessage, starting with [re <id>]`, never as a
  `[BOARD ask]`. The secret guard screens and scrubs them like any message.

The cloudflared ingress points at the **tunnel** listener only. The tunnel listener refuses to start
(exit 2, with the reason) without `hostname`, `team_domain`, `aud` and the python `cryptography`
module. There is no unverified-header mode.

`/api/v1/*` on either listener follows its own rules (a board API key on every call; on the tunnel
also an allowed Access service token or email): see "Public API (`/api/v1`)".

Extra files under `$BOARD_HOME`:

```
$BOARD_HOME/
  messages/<id>.json                 # {id, to: CODE|"all", body, by, created_at,
                                     #  status: new|held|relayed|replied, held_reason?, held_at?,
                                     #  released_by?, released_at?, relayed_to[]?, relayed_at?}
                                     #  (board-serve / `board message send` create; operator updates;
                                     #  see "Secret guardrails")
  messages/<id>/replies/<rid>.json   # {id, from: CODE, body, created_at, redacted?}
                                     #  (operator, through `board message reply`)
  operator.json                      # {heartbeat_at, version, operator_session, account_hint}  (operator)
  ledger.json                        # optional, same shape as meta/ledger  (operator)
  owner-away, consensus/<id>.json    # see "Owner away & consensus"  (bin/board)
```

| endpoint | does |
|---|---|
| `GET /` | the board page (nonce CSP, `no-store`). Any query is ignored by the server and never reflected: `/?d=<id>` (alert links, see "Board links in alerts") serves the same page, which reads the query itself. |
| `GET /api/state` | the state below, built from `$BOARD_HOME`, plus `page_version`: a short hash of `artifact/board.html` as it is now. The page board-serve renders carries its own as `<meta name="board-version">` (both hash the file's bytes as they are on disk). When the two differ (board.html was updated after the tab opened) the page shows a "newer version — Reload" banner and never reloads by itself. The banner comes with the next state frame, which is sent when anything on the board changes (keepalives carry no state), so it can show up some time after the update |
| `GET /api/me` | who is asking, from the verified identity and `fleet.yaml` only (never from request headers): see "Who am I" below. Same `Host` and identity checks as `/api/state` (no or bad JWT on the tunnel → 401, a forwarded request on the local listener → 403) |
| `GET /icon.svg`, `/favicon.png`, `/favicon.ico` (the PNG), `/apple-touch-icon.png` (+ `-precomposed`), `/icons/icon-192.png`, `/icons/icon-512.png`, `/manifest.webmanifest` | a fixed map to files under `artifact/` (nothing else there is served; the request path is never joined onto a directory), with `image/svg+xml`, `image/png` or `application/manifest+json`, `Cache-Control: private, max-age=86400` and `Content-Security-Policy: default-src 'none'`. Same Host and identity checks as every route, so through the tunnel they need Access too. The page itself gets `manifest-src 'self'` in its CSP |
| `GET /api/pomodoro/day?d=YYYY-MM-DD` | one kept day's per-stream pomodoro breakdown, read-only and on demand (the pushed `pomodoro` view has daily totals only, so SSE stays small): see "Pomodoro". Exactly one `d`, a real date, nothing else in the query (else 400 `bad_request`); a day outside the 14 kept, or no `pomodoro.json` → 404. Same `Host` and identity checks as `/api/state` (anyone who may view the board may read it) |
| `GET /api/tickets/<ref>` | one tracker ticket, see "Tickets". Because it spends the tracker token's rate limit, it needs `X-Board: 1` (a custom header: a cross-site page can only send it after a CORS preflight, which is never granted) and a `Sec-Fetch-Site` that is absent, `same-origin` or `none`; else 403 `csrf` **before** the identity, the audit or any tracker call. Only `approvers` ∪ `release_managers` (both empty → 403 `no_approvers`; anyone else → 403 `not_approver`), checked **before** any tracker call; refusals are audited (`type: "ticket"`, `result: "refused"`). `<ref>` is percent-decoded once: `KEY` or `tracker:KEY`. → 200 the ticket, or the ticket errors below (`Retry-After` on 429). Same `Host` and identity checks as `/api/state` |
| `POST /api/tickets/take` `{ref, session, comment?, transition?, assign?}` | take a ticket for a session, see "Tickets: take". Approvers only (empty → 403 `no_approvers`; else 403 `not_approver`), checked and a refusal audited **before** anything is fetched, sent or written. `session` must be a session in the snapshot or `fleet.yaml` (else 400 `unknown_session`); `comment` a string (scrubbed, footer added) or `false` (skip) or absent (the tracker's `take.comment`); `transition`, `assign` booleans (default true; else 400 `bad_body`). → 200 `{status: "taken"\|"partial", task_id, msg_id, failed, link, ticket}`; an active take → 409 `already_taken {task_id, session}`, nothing written |
| `POST /api/tickets/<ref>/comment` `{text}`, `/transition` `{to}`, `/assign` `{who}`, `/release` `{}` | the tracker writes and the end of a take, see "Tickets: writes". Approvers only, checked first and refusals audited. → 200 `{status: "ok", ref, ticket}` (`release`: `{status: "released", msg_id, link}`); `to` not in the tracker's `states` → 400 `bad_transition`; no active take → 409 `not_taken`; a tracker without that write → 400 `ticket_unsupported` |
| `GET /api/events` | `text/event-stream`: `event: state` with the full state at once and after every change (1 s mtime poll; only when the state differs), `: keepalive` every 25 s |
| `POST /api/decisions/<id>/answer` `{answer}` | `<id>` must match `^d-[0-9]+-[a-z0-9]+$`. With empty `approvers` → 403 `no_approvers` (fails closed). The identity is then checked **before anything is written**, with the rule of `board answer` (`approvers`; for a production decision, gate or stage, `release_managers`, falling back to `approvers`): not allowed → 403 `not_approver`, audited, file untouched (so it can never cancel an approver's answer that waits for the relay). `board answer` exit 8 (the gate's task was busy) → 409 `task_busy`: try again. Otherwise `board answer <id> --answer-file <tmp> --by <identity>` (argv, no shell; the answer goes by a 0600 file under `$BOARD_HOME` that is removed afterwards, so `-`, `--help` or `--answer-file` typed as an answer are recorded literally) → 200 `{status: "answered"}`. For `kind: "permission"` (`answer` = `allow`\|`deny`; `allow` with `summary_truncated` → 409 `review_in_session`), it then runs `board press <from> <answer> --expect <tool_use_id>` → 200 `{status: "relayed"}`; exit 3 → 409 `stale`; exit 4 → 403 `press_refused`; exit 7 → 409 `screen_not_ready` (the decision is pending again). A pane question (`source: "pane"`) that is not `pressable` → 409 `answer_in_session`, untouched. A pressable form of several questions takes `{options: [i, j, ...]}` instead of `answer` (2–4 numbers, one per question, 1–8, no `answer`/`option`); a wrong count, a number out of its question's range, a boolean, free text, or `options` on any other decision → 400 `bad_options`, untouched. It runs `board answer <id> --options i,j --by <identity>`, then `board press` like below. With free text for some questions it takes `{answers: [{question_index, option} \| {question_index, text}, ...]}` instead (one entry per question, in order, exactly one of `option` 1–8 or `text` per entry, no `options`/`answer`/`option`); text is cleaned as by `board answer --form-file` and must then be 1–200 characters, not ending in `\;` and not the "Type something" row itself (else 400 `bad_answers`; `question_index`, when given, an integer equal to the entry's position), and each text is screened by the secret guard before anything is written: a request for a secret or a secret-shaped value → 400 `held_answer`, audited; it goes to `board answer <id> --form-file <0600 tmp file> --by <identity>` (removed afterwards), then `board press`; an optional `option` (1–10, a pane question's clicked button) goes to `board answer --option` (mismatch → 400 `bad_option`, untouched; `option` on any other decision → 400); otherwise, when `board answer` set `relay: "press"`, it runs `board press <from> answer --expect <tool_use_id>` with the same results (plus exit 5 → 409 `partial`), and with `relay: "operator"` it returns 200 `{status: "answered"}` for the operator to relay. A decision already `relayed`/`stale`/`timeout` → 409 `gone`. |
| `POST /api/messages` `{to, body}` | only for `approvers` ∪ `release_managers` (else 403 `not_approver`); writes `messages/<id>.json` (`^m-[0-9]+-[a-z0-9]+$`) with `by: <identity>`, the body scrubbed, and `status: "new"`, or `"held"` when the secret guard flags it → 201 `{id, status, reason?}` (see "Secret guardrails") |
| `POST /api/messages/<id>/release` `{confirm: "release"}` | releases a `held` message. Approvers only (empty `approvers` → 403 `no_approvers`; else 403 `not_approver`, audited). Without the confirm body → 400 `confirm_required`; unknown id → 404; not held → 409 `not_held`. A channel other than the two verified listeners (a future API key) → 403 `release_refused`; with more than one approver, the message's own sender → 403 `self_release` (except `local`: on the loopback listener the identity is always `local`, the owner at this machine). Sets `status: "new"`, `released_by`, `released_at` → 200 `{id, status: "new"}`, audited (`message-release`) |
| `POST /api/notify-prefs` `{op, ...}` | Notification controls. `op` is `mute` `{channel, for?\|until?, include_urgent?}`, `unmute` `{channel}`, `level` `{channel, level}` or `presence` `{reduce, level?}`. `channel` is `slack`, `desktop`, `email`, `line`, `sns`, `webhook` or `all`; `for` matches `^[1-9][0-9]{0,4}[mhd]$` (at most 30 days); `until` is `HH:MM` or an ISO time in the next 30 days (a string of at most 64 characters); `include_urgent` and `reduce` are booleans; `level` is `info\|warn\|high\|urgent\|default` (`presence`: `warn\|high\|urgent`). Anything else → 400 `bad_op` / `bad_channel` / `bad_duration` / `bad_level` / `bad_body`, nothing run. Empty `approvers` → 403 `no_approvers`; identity not in `approvers` → 403 `not_approver`, audited. Otherwise `board mute\|unmute\|notify-level\|notify-presence … --by board:<identity>` (argv, no shell) → 200 `{status: "ok", notify}` (the new `notify` state). Failure → 500 `prefs_failed`. |
| `POST /api/presence` `{visible, idle_ms?}` | The page's presence heartbeat. `visible` boolean; `idle_ms` integer ms since the viewer last clicked, tapped, scrolled or typed, at most 86400000 (a day), or null (else 400 `bad_body`). The server records `interaction_at = <server now> − max(0, idle_ms)`, so a page clock that runs fast changes nothing. Any time the page sends (`interaction_at` in the body) is ignored: without `idle_ms` the last interaction is unknown, so the viewer does not count as watching. Same approver rule as `/api/notify-prefs`. Recorded in `presence.json` under the listener's verified identity, never one from the body → 200 `{status: "ok", visible}`. Audited only when the identity's `visible` changes. |
| `POST /api/alerts/ack` `{scope: "all"}` | The Deck panel's "acknowledge all". `scope` other than `"all"` → 400 `bad_scope`. Same approver rule as `/api/notify-prefs` (empty `approvers` → 403 `no_approvers`; not an approver → 403 `not_approver`, audited, nothing acked). Otherwise `board ack --by board:<identity> '*'` (argv, no shell): every unacked key in `alerts.json` gets `acked_at` and `acked_by: board:<identity>`, the same as `board ack '*'` in a terminal → 200 `{status: "ok", acked: <n>}` (`0` when nothing was open). Escalation stops; nothing leaves the board, and a condition that opens again alerts again. Failure → 500 `ack_failed`. |
| `POST /api/sessions/<code>/action` `{action}` | A session action. `action` other than `compact`, `rc_on`, `rc_off` → 400 `bad_action`; `<code>` not a session in the snapshot or `fleet.yaml` → 404 `not_found`; nothing run. Empty `approvers` → 403 `no_approvers`; identity not in `approvers` → 403 `not_approver`, audited (`session-action`), nothing written. Otherwise `board session <code> compact --by board:<identity> --queue-only`, or `board rc on\|off <code> --by board:<identity> --no-apply` (argv, no shell) → 200 `{status: "ok", action, code, rc}`; then `board session apply <code> --only compact\|rc` starts in the background, and the page follows the result through the snapshot. Failure → 500 `action_failed`. |
| `POST /api/rc` `{code, state}` | Remote Control. `state` other than `on`/`off` → 400 `bad_state`; `code` not `"all"` and not `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`, or not a session in the snapshot or `fleet.yaml` → 400 `bad_code`; nothing run. Empty `approvers` → 403 `no_approvers`; identity not in `approvers` → 403 `not_approver`, audited, nothing written. Otherwise `board rc on\|off <code> --by board:<identity> --no-apply` (argv, no shell) → 200 `{status: "ok", rc}` (the new `rc` state), then `board rc apply <code>` (`--pending` for `all`) starts in the background: the page follows the result through the snapshot. Failure → 500 `rc_failed`. |
| `POST /api/away` `{state: "on"\|"off", said?}` | `state` other than `on`/`off` → 400 `bad_state`; `said` not a string or over 500 chars → 400 `bad_said`. Empty `approvers` → 403 `no_approvers`; identity not in `approvers` → 403 `not_approver`, audited, nothing written. Otherwise `board away on\|off --by board:<identity> [--said <said>]` (argv, no shell; `on` without `said` records "away (set on the board)") → 200 `{away: true\|false, status: "on"\|"off"\|"unchanged"}`. `on` while already away keeps the existing record (`unchanged`). Failure → 500 `away_failed`. |

Every request: `Host` from the listener's allowlist, else 421. Per listener, at most 64
connections (more get a bare 503) and 32 SSE streams (503 `too_many_streams`); the socket times
out after 60 s. Error bodies are generic `{error, detail}`. Token reasons and `board` output are
logged to stderr only. Identities are matched against `approvers` / `release_managers` /
`allowed_emails` case-insensitively, and `--by` gets the configured spelling. POST: `X-Board: 1` and an `Origin`
from the listener's allowlist (`http://127.0.0.1:<port>`, `http://localhost:<port>` /
`https://<hostname>`; missing or `null` → 403 `csrf`), `Content-Type: application/json` (else
415), at most 16 KiB (else 413). A POST refused before its body is read (413, 415, 403, ...) has
up to 64 KiB of the body read and dropped (2 s at most in all, however slowly it arrives) and is
answered with `Connection: close`, so the client gets the refusal rather than a connection reset.
Request framing, for every method: `Transfer-Encoding` is not accepted, nor several
`Content-Length` headers or one that is not digits (400 `bad_request`, connection closed); a body
on a method that takes none (GET, HEAD, PUT, OPTIONS, ...) is dropped the same way and the
connection closes after the one reply, so it is never read as another request. `OPTIONS` and every other method → 405, and no
`Access-Control-*` header is ever sent. Every write appends `{type: "serve", action:
"answer"|"press"|"message"|"message-release"|"away"|"notify-prefs"|"presence"|"alerts-ack"|"rc"|"session-action", by, via: "loopback"|"cf-jwt", id, result}` to
`events.jsonl` (`presence` only when visibility changes).

State (`/api/state` and every SSE frame):

```json
{ "mode": "local", "me": "local|<email>", "writable": true, "can_message": true|false,
  "asker": true|false, "generated_at": 0,
  "account": {"email": "...", "org": "..."|null} | {"hidden": true} | null,
  "sessions":  [ {code, aliases, state, status, task, tmux, remote_url, pending, updated_at, rc, compact} ],
  "decisions": [ decision files, newest 200, without session_id/tmux_pane ],
  "tasks":     [ task files, newest 50 ],
  "approvals": [ events.jsonl type:"approval" rows (+ relayed_at from the decision), newest 100 ],
  "messages":  [ newest 20 ], "replies": { "<msgId>": [ ... ] },
  "operator":  { heartbeat_at, version, operator_session, account_hint, approver_ids,
                 release_manager_ids, usage: {five_hour, seven_day, updated_at,
                 burn?: {status, pct_per_hour, hit_at, n}}, limit|null },
  "ledger": {...}|null,
  "owner_away": {since, by, said, legacy?}|null,
  "consensus": [ snapshot.consensus items, newest 50 ],
  "notify":    { mutes, min_level, effective_min_level, presence: {reduce, min_level, window_seconds,
                 idle_seconds}, breakthrough, updated_at, updated_by, channels: {slack: bool, ...} } | null,
  "resources": { see "Resources": resources.json without pids, paths or unknown fields }|null,
  "rc":        { "all": "on"|"off"|null, "sessions": {"<code>": "on"|"off"}, "updated_at" } | null,
  "pomodoro":  { see "Pomodoro": today's tallies, each session's current pomodoro, 14 days }|null,
  "lessons":   [ see "Lessons": the newest 100, without signals or fingerprints ],
  "scheduler": { "mode": "normal|...", "applied": ["l-.."], "hints": [{"lesson", "text"}] }|null }
```

`account` is the Claude account the sessions run on, from `$BOARD_HOME/account.json` (the collector
copies only `oauthAccount.emailAddress` / `organizationName` from `~/.claude.json`, mode 0600). It
is personal data: only an **owner** gets the email (fleet.yaml `owners`; when `owners` is empty,
`approvers` count as owners, so a single-user board keeps working, and `board doctor` says to set
`owners`). Approvers who are not owners, askers and viewers get `{"hidden": true}`, and so does
every `/api/v1` key (`null` there). `null` when it is unknown or switched off
(`BOARD_SHOW_ACCOUNT=0`, or `usage.show_account: false` in fleet.yaml). The operator's
`operator.account_hint` follows the same rule (owners only; `null` for everyone else). The operator
never mirrors either to the Artifact db.

**Owners on the board vs. API keys.** The board's owner-only surfaces fall back to `approvers` when
`owners` is empty. API key administration does not: with empty `owners` it fails closed (see "API
keys").

The shape matches what the page reads from the Artifact db (`sessions/*`, `decisions/*`, `tasks/*`,
`approvals/*`, `messages/*` + replies, `meta/operator`, `meta/ledger`, `meta/resources`). `task` is the title of the
session's current task. `approver_ids` is `[]` when nobody may answer.

### Who am I (`GET /api/me`) and API keys

```json
{ "identity": "local|<email>", "via": "local|access",
  "roles": ["approver", "release_manager", "asker"] | ["viewer"],
  "is_approver": true, "is_release_manager": false, "is_asker": false,
  "api_keys": [ ... ] }
```

- `via` is `local` for the local listener and `access` for the tunnel (a verified Cloudflare Access
  JWT). The audit log keeps its own names (`loopback` / `cf-jwt`).
- `is_approver`: the identity is in `approvers`. `is_release_manager` is the effective rule for a
  production decision: in `release_managers`, or in `approvers` when `release_managers` is empty.
  `is_asker`: the identity is in `askers` (messages only). `roles` lists what is true, or
  `["viewer"]` when none is. All matches are case-insensitive.
- `api_keys` is **reserved**: the key store exists (`board apikey`, see "API keys"), but this route
  does not fill it yet, so the page shows "API keys are not enabled on this board". Its shape, one
  entry per key issued to this identity (an owner email, or `local`):

  ```json
  { "id": "<kid>", "name": "Phone shortcut", "scopes": ["read:state"],
    "created_at": 0, "expires_at": 0, "last_used_at": 0|null,
    "status": "active|pending|revoked|expired" }
  ```

  Times are epoch ms; `null` means never used. Every key expires. `id` is the key's 12-character
  kid. **The key secret is never returned** by this or any later read: it is shown once, when the
  key is created.

The page's profile menu (header, top right) reads this route in local mode. In Artifact mode it
uses the `user` capability instead (`me()`: `canEdit`, `isOwner`; the name stays empty because the
page does not declare the `profile` scope) and takes roles from `meta/operator.approver_ids` /
`release_manager_ids`. Sign-out links to `/cdn-cgi/access/logout` and is shown only when
`via` is `access`.

**Operator in local mode** (`prompts/board-operator.md` section 9): `board-serve --watch` prints
`decision <id> answered`, `message <id> new`, `away on|off`, `consensus <id> <status>` and
`schedule <CODE> due` (scheduler advisories to deliver, section 12; once per due set; never
while the scheduler is off) and `lesson <id> candidate` (a new auto lesson without a note,
section 12), once each, oldest first, and `tick` every 8 minutes.
The operator relays questions and gates (then `board mark <id> relayed`) and messages, writes
replies, and keeps `operator.json.heartbeat_at` fresh. Permission decisions are already pressed by
`board-serve`.

## Tickets

Issue trackers on the board (`lib/tickets/`, design: `docs/design/tickets-and-api.md`): read a
ticket, write to it (comment, transition, assign) and **take** it: give it to a session as a task,
with a `[BOARD ticket]` relay that authorizes that session to work on it (below). The board panel
comes later.

**Config** (`fleet.yaml`, documented in `fleet.example.yaml`):

```yaml
tickets:
  cache_ttl_seconds: 60          # 0–3600
  not_found_ttl_seconds: 30
  trackers:
    - name: tracker              # [a-z][a-z0-9_-]{0,31}, unique
      type: http-json            # or fake
      base_url: https://tracker.example.com
      auth: {header: Authorization, scheme: Bearer, env: EXAMPLE_TRACKER_TOKEN}  # or {token_file: F} | none
      key_pattern: '^(WEB|API)-[0-9]+$'
      rate_per_min: 30
      retry_wait_max_seconds: 2
      resolve: {method: GET, path: ..., query: {...}, set: {var: "$.path"}}      # optional
      get: {method: GET, path: ..., map: {title, status?, assignee?, description?, updated_at?, id?},
            link: "https://...{key}", status_category: {todo: [...], doing: [...], done: [...]}}
```

- **Routing.** A ref is `KEY` or `tracker:KEY`; `KEY` matches `^[A-Za-z0-9][A-Za-z0-9._#-]{0,99}$`
  (else 400 `bad_ticket_ref`). `tracker:KEY` picks that tracker (unknown name, or a KEY its
  `key_pattern` does not match → 400 `unknown_ticket_ref`). A bare KEY is matched (`fullmatch`)
  against every tracker's `key_pattern`: one → it; none → 400 `unknown_ticket_ref`; several → 409
  `ticket_ambiguous` with `candidates: ["tracker:KEY", ...]`, and nothing is sent anywhere. List
  project prefixes in `key_pattern`, not `[A-Z]+-[0-9]+`.
- **Templates.** `{key}` is the KEY; `{id}` defaults to it; `resolve.set` adds variables from the
  resolve answer; a value it gives must match `[A-Za-z0-9._~-]{1,200}` and not be `.` or `..`
  (else `ticket_upstream`, before the next request). In a `path`, each value is one percent-encoded segment (`/ ? # % : @` encoded;
  `""`, `.`, `..` refused). `query` values are URL-encoded. A `path` must start with one `/` and
  have no scheme, `//`, query, fragment or `.`/`..` segment (else the tracker is unconfigured).
  Paths into the JSON answer: `$`, `.name`, `[n]`, `[?field=={var}]`; no eval.
- **HTTP.** https only (http only for `127.0.0.1`/`localhost`); `base_url` without user, query
  or fragment; the request URL is checked again against the base (scheme, host, port) before it
  is sent; redirects are never followed (→ `ticket_upstream`); no proxy for local hosts; 10 s
  per socket operation and 20 s for the whole exchange; answers over 1 MiB refused; a header value with CR/LF refused. Reads are `GET` only.
- **Headers.** `auth.header` (default `Authorization`) may not be a header the client sets itself
  (`Host`, `Content-Length`, `Content-Type`, `Transfer-Encoding`, `Connection`, `Cookie`, …).
  `board ticket doctor` also warns about a `base_url` that is an IP address. The `link` template's
  values are percent-encoded like path segments.
- **Tokens.** `auth.env` names a variable, read from the process environment, then
  `<config dir>/.env`; `auth.token_file` names a file (relative to the config dir) that must be
  a regular file not readable by group or others. Every token value loaded (and its URL-encoded and
  base64 forms) is replaced by `[redacted:<env name>]` (`[redacted:<tracker>_token]` for a file)
  in every string the tickets code returns, prints, logs, caches or audits, and the result then
  goes through the secret guard (`guard.scrub`). `board ticket doctor` shows `set`/`unset` and
  the source, never a value.
- **Rate limits.** Per tracker: a token bucket of `rate_per_min` (in-process; board-serve keeps
  it across requests); a 429 with `Retry-After` ≤ `retry_wait_max_seconds` is waited out once,
  a longer one stores `blocked_until` in `$BOARD_HOME/tickets/state/<tracker>.json` and every
  request until then fails at once with `ticket_rate_limited`; 5 upstream failures in a row (a 5xx
  answer, or a network error: unreachable, timeout, redirect, a cut-off, too slow or over-1-MiB
  answer) open a breaker for 60 s (`ticket_upstream`, "paused"); any 2xx resets the count.
- **Cache.** `$BOARD_HOME/tickets/cache/<tracker>__<key>.json` (0600; characters outside
  `[A-Za-z0-9._-]` are `%XX`). A ticket is served from it for `cache_ttl_seconds`, a 404 for
  `not_found_ttl_seconds`; `board ticket get --refresh` skips it. Files older than 7 days are
  removed on write unless `tickets/links/` has one of the same name.
- **`BOARD_MOCK=1`**: only `fake` trackers load.

Ticket (CLI `--json`, the route, the cache):

```json
{ "provider": "tracker", "key": "WEB-12", "id": "u-12", "title": "...", "status": "in_progress",
  "status_category": "todo|doing|done|other", "assignee": {"id": "..."|null, "name": "..."|null}|null,
  "excerpt": "plain text, at most 500 characters", "url": "https://..."|null,
  "updated_at": "..."|0|null, "fetched_at": 0 }
```

`status_category` uses the tracker's `status_category` lists, then built-in names (`todo`,
`backlog`, `open` …, `in progress`, `review` …, `done`, `closed`, `cancelled` …), else `other`.
HTML in the description is reduced to text; whitespace is collapsed.

Errors (`{error, detail, retry_after?, candidates?}`; CLI exit code in brackets):

| error | HTTP | when |
|---|---|---|
| `bad_ticket_ref` | 400 [2] | not KEY or tracker:KEY |
| `unknown_ticket_ref` | 400 [2] | no tracker takes it |
| `ticket_ambiguous` | 409 [2] | several trackers take it (`candidates`) |
| `ticket_not_found` | 404 [3] | the tracker said 404/410, or `resolve` found nothing |
| `ticket_rate_limited` | 429 [5] | our bucket, or the tracker's 429 (`retry_after`, header `Retry-After`) |
| `ticket_auth` | 502 [1] | the tracker answered 401/403 to our token |
| `ticket_upstream` | 502 [1] | network error, redirect, 5xx, not JSON, over 1 MiB, unsafe path value, breaker open |
| `ticket_unconfigured` | 503 [1] | the tracker's config has problems (`board ticket doctor`) |

Upstream error bodies never reach a caller: the first 300 bytes, redacted, go to stderr.

**Audit.** Every get appends `{ts, type: "ticket", action: "get", provider, key, by, via, result}`
to `events.jsonl` (`result`: `ok`, `cache`, or the error code; `cached: true` for a remembered
404; refusals on the route: `result: "refused"`, `reason`, `ref`). `by` is the matched
configured identity on the route and `local` for the CLI; `via` is `loopback`/`cf-jwt` or `cli`.
Never a body, a token or a header.

**CLI.** `board ticket get REF [--refresh] [--json]`, `board ticket doctor [--json]` (exit 1
when a tracker has problems), `show REF [--json]`, `take REF --session CODE --by WHO
[--comment-file F | --no-comment] [--no-transition] [--no-assign] [--json]`, `release REF --by WHO`,
`comment REF --by WHO --file F` (`-`: stdin), `transition REF STATE --by WHO`, `assign REF NAME --by WHO`.
Exit codes: 0 ok (a `partial` take too, the failed writes on stderr), 1 error (upstream, auth,
unconfigured, `already_taken`, `not_taken`; `show`: no active take), 2 usage, bad/unknown/ambiguous
ref, bad value, `unknown_session`, `ticket_unsupported`, 3 not found, 4 not an approver
(`not_approver`, `no_approvers`), 5 rate limited. Output goes through `guarded` as well.

### Tickets: writes

```yaml
    - name: tracker
      # ... as above, plus any of:
      comment:    {method: POST,  path: "/api/v1/projects/{project}/issues/{id}/comments", body: {body: "{text}"}, ref: "$.data.id"}
      transition: {method: PATCH, path: "/api/v1/projects/{project}/issues/{id}", body: {status: "{to}"},
                   states: [backlog, todo, in_progress, done, cancelled]}
      assign:     {method: PATCH, path: "/api/v1/projects/{project}/issues/{id}", body: {assigneeName: "{who}"}}
      take: {transition: in_progress, assign: "Board Bot", comment: "Picked up as {task_id} ({session})."}
```

- A write has `method` POST, PUT or PATCH, the same `path` rules as a read, and a JSON object
  `body` template that must use `{text}`, `{to}` or `{who}`. Values are substituted **by type** into
  the parsed template (a string that is exactly `"{var}"` takes the value as is), never by building
  JSON text. Every other variable must come from `resolve.set` (or be `{key}`/`{id}`). The optional
  `ref` is a path into the answer (the new comment's id). An empty 2xx answer is fine. A tracker's
  `capabilities` are `read` plus the writes it configures; `fake` trackers have all of them and
  append each write to `$BOARD_HOME/tickets/fake-writes.jsonl` (an op in a fixture ticket's `fail`
  list fails).
- `transition` only moves to one of `states` (else 400 `bad_transition`, nothing sent);
  `take.transition` must be one of them.
- **Who:** approvers only (`approvers`; release managers read but do not write). Empty → 403
  `no_approvers`, anyone else → 403 `not_approver`; the check comes before any tracker call and the
  refusal is audited (`result: "refused"`, `reason`). On the CLI, `--by` is checked the same way.
  `--by` names the person acting: a session never passes someone else's identity there.
- **The session's own take** (owner decision Q4; CLI only, no board route):
  `board ticket comment REF --session CODE --file F` and `board ticket transition REF [STATE]
  --session CODE`. Allowed only when (1) the command runs inside session CODE: walking up from it,
  an ancestor process has a registry file (`$CLAUDE_SESSIONS_DIR/<pid>.json`) that describes that
  very process (not a zombie, `procStart` matches) and names CODE (tmux session or name; aliases
  count), the same check as `board resolve --from`; nothing else counts, `TMUX` included (else 403
  `not_this_session`, exit 4); and (2) CODE holds the **active** take of that ticket (else 403
  `not_your_ticket`, exit 4). A comment ends with `(via agent-switchboard, session CODE)`; a
  transition goes only to the tracker's `take.transition` (in progress; any other STATE → 400
  `bad_transition`, exit 2). Audited as `by: "session:CODE"`, `via: "session"`, with the task id;
  refusals audited too. Assign, release and other states stay with approvers. `--session` is
  plumbing for honest sessions, not a security boundary against a hostile local process: it trusts
  `$CLAUDE_SESSIONS_DIR` and the registry entries in it, the same trust level as `--by` and `--from`.
- **Comments** (owner decision Q5): the text is cleaned (control characters but newline and tab
  become spaces), redacted (tracker tokens, then the secret guard), at most 4000 characters (empty
  or longer → 400 `bad_comment`), and always ends with a blank line and the footer
  `(via agent-switchboard)`. The tracker token posts as its owner, so the footer says where the
  text came from; it carries no email. `tickets.comment_identity: true` (opt-in) makes it
  `(via agent-switchboard, by <identity>)`.
- Each write invalidates the cache entry and refetches the ticket (returned as `ticket`, or null when
  the refetch fails). Audit: `{type: "ticket", action: "comment"|"transition"|"assign", provider,
  key, by, via, result, ref?, to?, body_sha256?}`, never a body. 429 and the breaker apply as for
  reads; a 429 write is sent once more after a short `Retry-After` (it was not processed).

### Tickets: take

An approver gives a ticket to a session (`POST /api/tickets/take`, `board ticket take`):

1. the approver check (above) and the session check (`unknown_session`); a comment given with the
   request is checked too;
2. a fresh fetch (`ticket_not_found` etc. stop here; nothing is written);
3. under `tickets/links/.lock`: an **active** link for the ticket → 409 `already_taken {task_id,
   session}`, audited, nothing written;
4. `board task new --owner CODE --ticket TRACKER:KEY [--ticket-url URL] "KEY: title"` (the task gets
   `ticket: {provider, key, url, title_at_take}`; failure → 500 `take_failed`, nothing else written);
5. the link `tickets/links/<tracker>__<key>.json` (0600): `{provider, key, task_id, session,
   taken_by, taken_at, status: "active", msg_id, upstream: []}`;
6. `messages/<msgId>.json` `{id, kind: "ticket", event: "taken", to: CODE, by, created_at,
   status: "new", ticket: {provider, key, title, url}, task_id, body}`;
7. the tracker's `take:` writes, each optional and skipped when the tracker lacks that write: the
   comment (the request's, else `take.comment` with `{task_id}`, `{session}`, `{key}` filled, both
   with the footer; `comment: false` / `--no-comment` skips it), the transition to
   `take.transition`, the assignment to `take.assign`. Each is audited and recorded in the link's
   `upstream: [{op, result, ref?, at}]`. A failure does not roll anything back: the result is
   `status: "partial"` with `failed: [ops]` (the task is the source of truth).

The result is audited as `{action: "take", result: "taken"|"partial"|"already_taken", task_id,
msg_id, session, failed}`. `board-serve --watch` prints `ticket <KEY> taken <msgId>`; the operator
relays `board message show <msgId>` (section "Relay message formats") and marks the message
`relayed` like any message.

- **What viewers see** (Q3). A take writes the tracker's title into the task, the session's
  current task and the relay message. `/api/state` and every SSE frame carry that text only for
  `approvers` ∪ `release_managers` (`serve.ticket_text_for`); for everyone else a task with a
  `ticket` has the ticket's key as its `title` and `ticket: {provider, key}`, a session whose
  current task is one shows the key as `task`, and a `kind: "ticket"` message keeps `ticket:
  {provider, key}` (its `body` never had the title). The code is `lib/tickets/viewer.py`
  (`tasks_for`, `ticket_text_for`). The operator mirrors tasks to the Artifact db from
  `board task ls --json --viewer`, the same filter, never from the raw files. `/api/v1` applies it
  to every API key (a key is never an approver or release manager): `/state`, `/events`,
  `/sessions[/{code}]`, `/tasks`, `/tasks/{id}` (read from the file, then filtered) and
  `/messages/{id}`.
- **Release** (`POST /api/tickets/<ref>/release`, `board ticket release`): the active link becomes
  `released` (`released_by`, `released_at`, `release_msg_id`) and a `kind: "ticket"`, `event:
  "released"` message goes to the session (`--watch`: `ticket <KEY> released <msgId>`). No active
  take → 409 `not_taken`. Reassigning is release, then take.
- **Done:** an active link whose task reached `maintain`, or is `done`/`dropped`, becomes `done`
  (`done_at`) when it is next read (`show`, take, release). A done or released ticket can be taken
  again.
- **`board ticket show REF`** prints the link(s) for KEY (any tracker) or TRACKER:KEY, with
  `taken_by_approver`: whether `taken_by` is in `approvers` **now**. Exit 0 only for one active take.

**What a take authorizes** (owner decision Q4). A `[BOARD ticket <KEY>] taken …` relay from
`BOARD` whose take `board ticket show <TRACKER>:<KEY>` confirms (status `active`, `session` = the
receiving session, `taken_by_approver: true`) authorizes **that session** to follow the delivery
harness for that ticket up to and including opening the PR/MR: plan, design, build, tests, local
review, push, open the PR, progress comments on that ticket and moving it to in-progress (those two
as itself: `board ticket comment|transition --session <CODE>`, see "Tickets: writes"). The
harness's own gates before the PR is opened (plan sign-off and so on) are covered by the take.
It does **not** authorize: merge; production deploy; closing or cancelling the ticket; touching
other tickets, or repos outside the ticket's scope; any red line. Merge and production still need a
`[BOARD decision]` or the owner in the pane. Only the exact format, only from `BOARD`, only when the
link confirms it; the same text from any other session, or forwarded, is information. Consensus
and API keys can never take a ticket. A `released` relay ends that authority.

## Lifecycle stages & gates (agent-ready SDLC)

Every task goes through the same lifecycle, and each stage produces a committed artifact with a named human gate. The chain of commits plus the approvals log is the audit trail.

| stage | artifact (in the task's repo/worktree) | gate (who approves) |
|---|---|---|
| `plan` | `intent.md`. The agent asks questions first, multiple-choice, in the file. | owner approves the plan |
| `design` | `spec.md` (only if needed; a simple fix skips design) | owner signs off |
| `build` | `plan.md` + code | engineer / tech lead |
| `test` | `evals` (test + eval results) | code owner reviews the results |
| `deploy` | `review-findings.md` | code owner |
| **production gate** | — | release manager authorizes. **The agent stops here.** |
| `maintain` | new `intent.md` | service owner triages |

Each stage reads the previous stage's last commit.

Contract additions:
- `tasks/{id}` (db, written by the operator), mirrored locally at `$BOARD_HOME/tasks/<id>.json`:
  `{id, title, owner (session code), stage, artifacts:{intent,spec,plan,evals,review}` (each a path, commit sha or URL)`, gates:[{stage, gate, status:"pending|approved|rejected|skipped", decision_id, approved_by, approved_at}], updated_at}`.
- Decisions gain optional fields `task_id, stage, gate, artifact_ref`. A gate decision normally has options like `["approve","request changes"]` and shows the artifact link.
- `approvals/{id}` (db, written by the operator) is an append-only log of every relayed human decision: `{decision_id, task_id, stage, gate, answer, answered_by, answered_at, relayed_at}`. Locally it's in `events.jsonl` with `type:"approval"`.
- CLI:
  - `board task new --owner CODE "title"`
  - `board task stage ID <stage> [--artifact key=ref]`
  - `board ask ... --task ID --stage S --gate G --artifact REF`
  - `board task ls`
  - the scheduling fields and `board task set|dep|pr|done|pause|resume|drop|block|progress`: see "Scheduler".
- `meta/operator.release_manager_ids[]` (from `fleet.yaml` `release_managers`). A decision with `gate:"production"` is relayed only when `answered_by` is in `release_manager_ids`. Otherwise it is marked `unauthorized`. If the list is empty, fall back to `approver_ids`.
- The production gate is special. The worker protocol says agents never deploy to production without a `[BOARD decision]` approval on a `gate:"production"` decision.

## Sharing and who may decide

- **Never share the board org-editable, and never make an approver an Editor.** Editors (and the
  owner) hold `admin`. They can write every shared document, and they can **publish new page code
  and new db rules**. Such a version could write answers under rules looser than `answers/{self}`,
  or show people something else. Editors are the owner and, if you need one, a backup operator
  account, each invited by email.
- **Approvers are Contributors** (`interact`), invited by email. They write only their own
  `answers/<their id>/` subtree. Messages are shared documents, so in Artifact mode only Editors
  send them.
- `meta/operator.approver_ids[]` lists the user ids allowed to decide. It is seeded with the owner
  id and set in `fleet.yaml` as `approvers` (kept local only). **The operator reads answers only
  under those ids** (and `release_manager_ids` for production), and the id comes from the path.
  An answer anywhere else is never read, so it is never relayed.
- **Two id namespaces.** In Artifact mode, `approvers` / `release_managers` hold claude.ai user ids
  (the page's `user.id()`), which are opaque and scoped to the artifact's organization. The
  self-hosted board (`board-serve`) uses **emails** (verified by Cloudflare Access) and the literal
  `local`, which counts only when it is listed. `local` means **any process on this machine** that
  reaches the local listener with the right `Origin` and `X-Board` headers, other OS users
  included. Never list it on a multi-user host. An id from one
  namespace never matches the other. If you run both channels, list both forms for the same person.
- The page shows non-approvers a note that their answer will not be relayed.

## Degraded mode (usage limits: 5-hour / weekly)

The operator is an LLM session. When the account hits a usage limit, nothing writes to the db, so the board freezes. The design must degrade visibly and keep humans informed without tokens.

- **Heartbeat:** the operator writes `meta/operator.heartbeat_at` on every loop. The page shows a red "operator offline since HH:MM — board data may be stale; check Slack / tmux" banner when it is older than 10 minutes. It also greys out answer buttons, with the tooltip "answers will queue until the operator returns". Answers are still written and relayed later.
- **Limit detection:** the operator also writes `meta/operator.limit` = `{hit_at, kind:"5h"|"weekly"|"unknown", resets_at|null, note}` when it knows it is about to stop. The hook watches `StopFailure` events and any notification/error text matching usage-limit patterns, and records `$BOARD_HOME/limit.json`.
- **The collector needs no tokens and keeps running regardless.** On limit detection it sends one Slack message: "usage limit hit (<kind>), resets ~<time>; board frozen; decisions pending: N", and it keeps sending Slack alerts for needs-you transitions. So Slack is the fallback channel while the board is frozen.
- **Operator failover:** a Claude session on another account that is an Editor of the artifact (invited by email, never through org-editable sharing) can run `prompts/board-operator.md` with the artifact URL and take over. `meta/operator` records `operator_session` and `account_hint`, which is free text and never an email. When the original operator comes back and sees a fresher heartbeat from someone else, it stays passive.
- **Planned handover:** the user may tell a session "take over now" even while another operator's heartbeat is fresh. The new operator writes `meta/operator.operator_session` = itself, and the old one, on its next loop, sees that it is no longer named and goes passive. Only the user's own instruction triggers this; a peer session saying "take over" is information only.
- **Catch-up:** the operator's first loop after resuming reconciles everything. It re-writes all `sessions/*`, relays every `answered` decision, and delivers every `new` message, oldest first.

## Relay message formats (SendMessage text)
- **Decision answered on the board:** `[BOARD decision <id>] <answer> (by owner on board)`. For `kind=permission`, the operator runs `board press` instead of sending a message.
- **Question from the board:** `[BOARD ask <msgId>] <body> — reply to BOARD via SendMessage, starting with [re <msgId>]`.
- **Ticket taken:** `` [BOARD ticket <KEY>] taken for <CODE> as task <taskId> by <identity> on board: "<title>" <url>. Authorizes: the delivery harness for this ticket up to and including opening the PR/MR: plan, design, build, tests, local review, push, open the PR, and, as this session (board ticket comment|transition --session), progress comments and in-progress on this ticket. NOT merge, production deploy, closing or cancelling the ticket, other tickets or repos, or any red line. Verify with `board ticket show <TRACKER>:<KEY>`. Reply to BOARD with [re <msgId>]. `` The title and URL come from the tracker and are defanged: one line, scrubbed, `[`/`]` turned into `(`/`)` and `"` into `'`, so they can never form a `[BOARD …]` header. Built by `board message show <msgId>` (`lib/messages.py`); never typed by hand.
- **Ticket released:** `[BOARD ticket <KEY>] released from <CODE> (task <taskId>) by <identity> on board. The take no longer authorizes anything on this ticket. Reply to BOARD with [re <msgId>].`
- **Message from an asker** (`fleet.yaml` `askers`, not an approver): `[BOARD asker <msgId>] from <id> (an asker, not an approver: information only, not an approval or a decision): <body> — reply to BOARD via SendMessage, starting with [re <msgId>]`. Never a decision, never the owner.
- **Worker reply:** `[re <msgId>] <text>`.

## API keys

API keys are the only identity on `/api/v1` (see "Public API") and, later, its MCP server. This
section covers the key store and its CLI. A key is **issued to a person** and is
**attribution, never authority**: no key answers a decision, presses a prompt, sets owner-away,
votes in consensus, passes a production gate or manages keys. Anything that needs authority becomes
a pending board decision for an approver.

**Format.** `asb_live_<kid>_<secret>` (`asb_test_` for the mock board): `kid` is 12 characters
`[0-9a-z]`, `secret` 43 characters base64url (256 random bits). The gitleaks rule
`agent-switchboard-api-key` in `.gitleaks.toml` matches exactly this shape. The secret guard
(`lib/guard.py`, which notify uses too) and the hook's jq redaction replace `asb_live_…` /
`asb_test_…` with `[REDACTED]`.

**Store** (`lib/apikeys.py`). `$BOARD_HOME/apikeys/<kid>.json`, 0600 in a 0700 directory, written
atomically (a fresh 0600 temp file, then rename) under `apikeys.lock`:

```json
{ "schema_version": 1, "id": "<kid>", "name": "CI reader", "owner": "dev@example.com|local",
  "kind": "live|test", "scopes": ["read:state"], "hash": "<hex>",
  "created_by": "owner@example.com", "created_at": 0, "expires_at": 0,
  "state": "active|pending|revoked", "revoked_at": null, "revoked_by": null,
  "revoke_reason": "revoked|denied|withdrawn|rotated|rescoped", "last_used_at": null, "rate_per_min": 60,
  "approved_by": null, "approved_at": null, "requested_ttl_ms": 0,
  "predecessor": null, "successor": null, "grace_until": null }
```

- `hash` = HMAC-SHA256(pepper, the whole key), compared with `hmac.compare_digest`. The pepper is
  32 random bytes in `secrets/apikey.pepper` (0600). It is made on the first create or request,
  atomically (a full temp file, then a hard link), and **only while no key exists**: with keys
  present and the pepper missing, every command that would need it exits 1 and says to restore it
  from a backup. A copy of `apikeys/` alone is useless. A pepper that is a symlink, not owned by
  this user, readable by others or not 32 bytes is refused, not used.
- **The key is shown once**: create, request, rotate and scopes print it alone on stdout (or
  `{"id", "key", ...}` with `--json`) and say on stderr that it is never shown again. It is never
  stored, logged or audited, and `ls`/`show` never print the hash.
- **Status**, computed as of now: `revoked` (`state` revoked, or a rotated key whose `grace_until`
  has passed), `pending` (a request no owner has approved yet), `expired` (`expires_at` passed, or
  a request older than `apikeys.request_ttl_days`, default 14: **lapsed**), else `active`. Only an
  active key verifies. `ls`/`show` add `reason`: `lapsed`, or the `revoke_reason`. A lapsed
  request can never be approved or re-expired ("request expired; ask again"): its secret was
  handed out when it was requested. `last_used_at` is written at most once a minute.
- Expiry: `--expires Nd|YYYY-MM-DD` (a date means through that day, UTC). Default 90 days, at most
  365 days from now, never "never". A request's lifetime starts when it is approved.
- Scopes: `read:state`, `read:decisions`, `read:tasks`, `read:messages`, `tasks:write`,
  `write:messages`, `tickets:read`, `tickets:write`, `tickets:take`. An unknown scope is refused
  (exit 2). There is no answer, press, away, consensus, production or key-admin scope.

**Who may do what.** `--by WHO` is an email or `local`; `board:<who>` means the board page asks on
`<who>`'s behalf (audited as `via: board`). Matching is case-insensitive.

| Action | `owners` | `approvers` | anyone else |
|---|---|---|---|
| `create --owner P` (active at once) | any P | P = themselves or a listed person | no: `request` |
| `request` (pending, for themselves) | yes | yes | yes (at most 5 pending each) |
| `approve [--scopes a,b]` a request (the requested scopes, or fewer) | yes | no | no |
| `ls` / `show` | every key | their own keys | their own keys |
| `revoke` | any key (someone else's pending request = denied) | their own, or keys they issued | their own (their own pending request = withdrawn) |
| `rotate [--grace D]` | yes (active keys) | no | no |
| `expire --expires D` (set or extend) | yes | no | no |
| `scopes --scopes a,b` (revoke + reissue) | yes (active keys) | no | no |

- `owners` is a list in `fleet.yaml`, above approvers for key administration. Empty `owners`
  fails closed here: nobody can approve a request, rotate, re-expire or rescope. (The board's
  owner-only surfaces, such as the account email, fall back to approvers instead.)
- A listed person is anyone in `owners`, `approvers`, `release_managers` or
  `serve.tunnel.allowed_emails`.
- Someone else's key is "no such key" (exit 1) to a non-owner's `show` and `revoke`, exactly as an
  unknown id: not even its existence.
- `approve` prints and audits the scopes it grants (and the requested ones when narrowed) and the
  request's age.
- Names and identities refuse control characters and Unicode format or separator characters
  (bidi controls, zero-width, U+2028/2029).
- **Rotate** creates a successor with the same owner, name and scopes (new expiry: `--expires`, else
  90 days) and links both ways (`successor` / `predecessor`). The old key keeps working until
  `grace_until` (`--grace 0|Nm|Nh|Nd`, at most 7 days; default 0 = revoked now). **Scopes** does
  the same with new scopes, the old key revoked at once and the expiry kept. Only an active key that
  was not replaced yet can be rotated or rescoped.

**Audit.** Every change and every refusal (a `show` of someone else's key included) appends to
`events.jsonl`: `{"ts", "type": "apikey", "action": "create|request|approve|revoke|rotate|scopes|
expire|show", "by", "via": "cli|board", "key_id", "owner", "name", "result": "ok|refused", "reason"?,
...}` (`approve` adds `scopes`, `requested_scopes` when narrowed, `request_age_ms`). Never the key,
never the hash.

**Exit codes** (`board apikey`): 0 ok, 1 unknown key or a key in the wrong state, 2 usage (an
unknown scope, a bad date or identity), 4 not authorized (nothing written), 8 the key store is busy.

## Public API (`/api/v1`)

A read-only JSON API on `board-serve` for scripts and services (`lib/serve_api.py`). Its routes
are one table, `lib/api_spec.py`: the router dispatches from it and `GET /api/v1/openapi.json` is
generated from it (a test checks that the two are equal). Writes, tickets and MCP come later.

**Identity: a board API key, always.** `Authorization: Bearer asb_live_…` (see "API keys"). The
local listener's `local` and a tunnel email never count on their own: without a key every route
but `openapi.json` answers 401. A key is attribution only; the state it reads is the **viewer's**
view (no private card text, no account, `writable: false`, and no ticket text: owner decision Q3,
see "Tickets": a task taken from a ticket has its key as its title on every route, `/tasks/{id}`
and `/messages/{id}` included).

| Route (GET; HEAD too, except events) | Scope | |
|---|---|---|
| `/api/v1/openapi.json` | none (no key) | this API's OpenAPI 3.1 document |
| `/api/v1/whoami` | any valid key | `{key_id, name, owner, scopes, expires_at, mode: "live"}` |
| `/api/v1/state` | `read:state` | the board state as a viewer sees it (`mode: "api"`) |
| `/api/v1/events` | `read:state` | SSE, `event: state` on every change; the key is checked before a stream slot is taken and again before each frame and keepalive, and a revoked or expired key ends the stream with `event: revoked`. At most 8 API streams per listener, apart from the page's own (503 `too_many_streams`) |
| `/api/v1/sessions`, `/api/v1/sessions/{code}` | `read:state` | session rows (cards without private text) |
| `/api/v1/pomodoro`, `/api/v1/pomodoro/day?d=YYYY-MM-DD` | `read:state` | the pomodoro view; one kept day |
| `/api/v1/decisions[?status=S]`, `/api/v1/decisions/{id}` | `read:decisions` | the latest 200 decisions; one decision |
| `/api/v1/tasks`, `/api/v1/tasks/{id}`, `/api/v1/schedule` | `read:tasks` | the latest 50 tasks; one task; the scheduler's mode and lessons |
| `/api/v1/messages/{id}` | `read:messages` | one message with its replies (a held message stays held) |

Everything served is scrubbed by the secret guard, as on the board. Path parameters and query
values must match their patterns in the table; any other query parameter is a 400.

**Order of checks** (after the framing rules every board-serve route has: one Content-Length, no
Transfer-Encoding, a body on a GET dropped and the connection closed):

0. **No cross-site browser requests**, before anything else: `Sec-Fetch-Site: cross-site` or
   `same-site` → 403 `cross_site`; after the Host check, an `Origin` that is not this listener's
   own (`http://127.0.0.1:<port>` / `http://localhost:<port>`, or `https://<hostname>` on the
   tunnel; `null` included) → 403 `cross_site`. API clients send neither, so a hostile page can't
   make a browser call the API, not even without a key.
1. Method: GET or HEAD, else 405 (`Allow: GET, HEAD`; a POST body is drained). Host allowlist: 421.
2. The listener. **Local**: a direct loopback request (forwarding headers: 403). **Tunnel**:
   `serve.api.remote` not `false` (else 403 `api_remote_disabled`), and a verified Cloudflare
   Access JWT (401 without or with a bad one) that is either a **service token** whose
   `common_name` is in `serve.tunnel.api_service_tokens`, or an email in the tunnel's allowed
   emails (for a person using the API from a browser tool). Anything else: 403 `not_allowed`. A
   service token is never a person: the board's own routes still refuse it (401).
3. The route (404) and its query (400).
4. The key. **No `Authorization` header** → 401 `unauthenticated` with `WWW-Authenticate: Bearer`
   at once: not counted toward any limit and not audited (only an in-memory count), so keyless
   requests can't burn anyone's budget or flood the audit log. A header that is not exactly one
   `Bearer <key>` in the key's shape → 401 `invalid_key` (`reason: malformed`; the shape is checked
   before any file is read); unknown, tampered, pending, revoked or expired → 401 `invalid_key`
   (one generic answer); an `asb_test_` key → 403 `key_wrong_env`. These failures are audited and
   counted **per kid** when the key has the shape of one (someone guessing that key's secret,
   from anywhere), else **per source** (the tunnel's `Cf-Connecting-Ip`, else the listener), up to
   `serve.api.auth_fail_per_min` (default 10) a minute; past it, 429 with `Retry-After` and no
   audit row. A spent kid bucket still lets the **real** key through (it is checked first), so
   guessing a kid's secret can't lock its holder out; a spent source bucket refuses its junk.
   The limiter tables live in memory and drop entries idle for a minute. Then the route's scope (403 `insufficient_scope`), then the key's rate: a
   token bucket of `rate_per_min` with a burst of 20 (429 `rate_limited`, `Retry-After`).
   `last_used_at` is recorded (at most once a minute). The Access cookie (`CF_Authorization`) and
   `local` are never credentials here.
5. The handler.

No response carries an `Access-Control-*` header: a cross-origin browser call fails its preflight
(OPTIONS is 405). Errors are `{"error": code, "detail": text}`.

**Exposure** (`fleet.yaml` `serve.api`, owner decision: Access in front, plus a key):

```yaml
serve:
  api:
    enabled: true          # false: /api/v1 is off on both listeners (404)
    remote: true           # false: local listener only; the tunnel answers 403 api_remote_disabled
    auth_fail_per_min: 10
  tunnel:
    api_service_tokens: [<client id>.access]   # Access service tokens allowed on /api/v1
```

On Cloudflare, add a **Service Auth** policy for the service token to the board's Access
application. A client then sends `CF-Access-Client-Id` / `CF-Access-Client-Secret` (Access turns
them into the JWT) and `Authorization: Bearer asb_live_…`: two independent secrets, and the internet
never reaches board-serve unauthenticated. There is no Access bypass for the API.

**Audit** (`events.jsonl`, type `api`): every refused key check (`action: "auth_fail"`, `key_id`
when the key had the right shape, `reason`, `route`, `via`, `client`, `result`), every
`insufficient_scope`, and each key's successful use of a route at most once a minute per
board-serve process (`action` = the route's handler, with `owner` and `name`). `client` is `loopback`,
`service:<common_name>` or `email:<email>`. Never a key.

## Secret guardrails

Nobody may use the board to dig up credentials: not an approver, not a phone that reaches the
board through the tunnel, and not a stolen API key. There are four layers, and each one covers its
own part even when another fails.

**1. Outbound scrubber** (`lib/guard.py`, `board guard scrub|scrub-json|scrub-out`). Every free text
the board stores, serves, mirrors or alerts goes through it:

- message bodies and replies (when stored);
- decision questions, options, context and answers (`board ask`, `board answer`, the answer route);
- pane questions (AskUserQuestion): questions, headers, option labels and descriptions, in the
  session's state file and the pane-question decision, when the hook stores them (on top of the
  hook's jq redaction; only for pickers). The stored labels are what the board shows and what
  `board answer --option N` compares an answer with, so an option click still matches;
- task titles and tool summaries (as served);
- consensus proposals, refs, notes and the owner's words (when written);
- alert titles and bodies (stored in `alerts.json`, and again on every delivery), and adapter
  errors;
- everything in `/api/state` and every SSE frame (the whole tree);
- what `board ls|snapshot|decisions|task|away|consensus` print, since the operator mirrors them.

It replaces these with `[REDACTED]`:

- key=value / key: value / JSON pairs for credential-like keys (a colon value only when it looks
  like a value);
- Bearer/Basic values (only token-looking ones), `Authorization:` headers;
- URL userinfo, `--password`/`--token` flags, `-p <password>`, `-u user:pass`;
- vendor tokens (Slack, GitHub, GitLab, Stripe, Google, npm, AWS, `sk-…` API keys), Slack and
  Discord webhook URLs;
- PEM/PGP private key blocks (also a clipped block without its END line);
- JWTs and `asb_live_`/`asb_test_` keys;
- hex runs of 32+ characters (a 40-character lower-case git sha stays);
- mixed-case base64 runs of 32+ characters (lower, upper and digits mixed like random output, with a
  `/`-free stretch of 24+, so CamelCase names and paths stay).

It also replaces the **literal value** of every secret in `$BOARD_CONFIG_DIR/.env` with
`[redacted:NAME]`. The same goes for the env files listed in `fleet.yaml`
`guard.secret_files: [path, ...]` (relative to the config dir; `~` expanded).

- An entry counts as a secret when its key names a credential, or when its value looks random.
  Credential keys include `TOKEN`, `PASSWORD`, `SECRET`, `KEY`, `WEBHOOK` and `_URL`. Keys ending
  in `_HOST`, `_PORT`, `_USER`, `_FROM`, `PUBLIC_URL` and the like do not count.
- Values shorter than 8 characters are skipped.
- The values are loaded in memory only and reloaded when a file changes. They are never logged
  and never written. `board guard secrets` lists the key names per file, never a value.

Ids, hashes and urls (`*_id`, `summary_sha256`, `remote_url`, `tmux`, …) get the literal and token
rules but not the key=value, hex and base64 rules, so a `tool_use_id` stays usable. The hook keeps its own jq redaction on its hot
path; a test checks that every secret it redacts, the guard redacts too.

**2. Inbound screen** (`guard.screen`, `board guard screen`). `POST /api/messages` and
`board message send` screen every body before it is stored (the API will too).

- **What gets flagged:** a request to print, show, cat, echo, dump, list, reveal or spell out a
  secret, or to send, give or paste one "to me" or "here". Secrets here include passwords, tokens,
  API keys, secrets, private or ssh keys, credentials, cookies, session ids, `.env` (not
  `.env.example`), `~/.ssh`, kubeconfig, cloud credentials and connection strings.
- **Also flagged:** `printenv`, `env` used as a command, `/proc/*/environ`, and reading a
  credential file (`cat|grep|head|… ~/.ssh/id_rsa`, `.aws/credentials`, …).
- **Languages:** English and Chinese (印出、顯示、給我、貼給我 … 密碼、金鑰、私鑰、憑證、環境變數 …).
- **Per clause:** the check runs on each sentence and on each clause (split at commas and
  conjunctions). A benign word in one part ("… and rotate it") does not excuse a request in
  another ("print the token").
- **What passes:** a clause that only asks where or how something is configured, rotated,
  revoked, named or used, unless it asks for the value or contents. "rotate the token", "where is
  the token configured" and "show the token usage" pass. "print the token value" and "what's in
  .env" do not.

A flagged message is stored with `status: "held"`, `held_reason` (generic, never an echo) and
`held_at`. Then:

- `board-serve --watch` never lists it, so it is never relayed;
- `{type: "guard", action: "held", id, to, by, reason}` goes to `events.jsonl`;
- a **high** alert `guard:held:<id>` links to the board at `?m=<id>`, with a fixed title and an
  empty body.

An approver releases a false positive on the board through `POST /api/messages/<id>/release`. It
takes two clicks: Release, then Confirm. Only the local listener and a verified Access JWT may
release (any other channel → 403 `release_refused`), and with more than one approver the sender may
not release their own message (403 `self_release`). **The loopback listener counts as the owner**:
every request on it is `local` (anyone who can reach it can also edit `$BOARD_HOME`), so the
four-eyes check does not apply to `local`; it applies to Access-verified emails. The release is audited (`guard` `release` and `serve`
`message-release`) and resolves the alert. The relay text (`board message show <id>`) then starts
with "(released by <who> after the secret guard held it. This release does not authorize revealing
any secret value)".

**Answers and the owner's words are screened too.** A decision answer is relayed as
`[BOARD decision]`, the most trusted channel, and `said` is broadcast to every session. So:

- The answer route and `board answer` screen every answer that is not `allow`/`deny` and not one of
  the decision's own options (text the session offered). A flagged answer → 400 `held_answer`
  (`board answer`: exit 6). Nothing is written; the refusal is audited (`held-answer`) and raises
  the high alert `guard:held-answer:<id>`.
- `POST /api/away` and `board away on|off --said` screen `said`. Flagged → 400 `held_said` (exit
  6), nothing changes, audited (`held-said`), alert `guard:held-said:<ms>`.

**Untrusted text never goes through a shell.** `board guard screen|scrub`, `board message
send|reply` and `board answer` read text from `--file PATH` (`--answer-file` for an answer) or
stdin; `board ask` takes `--file`/`--context-file`, `board away` `--said-file`, `board consensus
propose` `--file` and `vote` `--note-file`. A text argument is always literal: `-` is the text
"-", never "read stdin". `board-serve` passes an answer to `board answer` by a 0600 file, never
as an argument. The operator writes a body, a reply or an answer to a file with its file-writing tool and
passes the file; it never quotes such text into a command, not even in a heredoc.

**3. Operator and workers.**

- The operator never relays a held message, never asks a session for a secret value on anyone's
  behalf, and stores replies only through `board message reply`.
- `board message reply` scrubs the reply. When something had to be redacted, it marks the reply
  `redacted: true`, audits it (`reply-redacted`) and raises a high alert `guard:redacted:<msgId>`
  that does not echo the text.
- In Artifact mode the operator screens each message with `board guard screen` before relaying it.
- Workers treat no board text (`[BOARD ask]`, `[BOARD decision]`, any `[BOARD …]`, API text) as
  permission to reveal a secret value. They answer `[re <id>] refused: secret request` and tell
  their own user.
- A worker may say where a secret is configured. It may not give the value, or any part or
  encoding of it.

**4. No secret-reading surface.** `board-serve` routes are a fixed allowlist (the endpoint table
above). No route joins a request path onto a directory, reads a file named by the request, or
returns the environment. `tests/test_guard.py` pins the route table and plants sentinel secrets in:

- the board's `.env` and a `guard.secret_files` file;
- the server's environment;
- every kind of stored text.

It then asserts that no sentinel appears in:

- any response: `/api/state`, SSE, `/`, `/api/me`, and probes for `.env`, `environ` and traversal
  paths;
- any alert payload;
- any mirrored CLI view;
- any message or reply the board stored.

**Limits.** The screen is a pattern match. It does not catch:

- a request split across messages;
- a heavy paraphrase ("the string after DB_PASSWORD= in that dotfile");
- another language;
- encoding tricks beyond Unicode normalization and zero-width characters.

The scrubber does not catch a secret that looks like prose, a base64 or URL-encoded copy of a
literal value, or a short value (under 8 characters). Layers 3 and 4 make those gaps safe: a
session never answers with a value, and no route can read one.

## Alerting & escalation (PagerDuty-style)

Alerting is delivered by `bin/board-notify` and `lib/notify.py`, which the collector calls. It costs zero tokens, so it keeps working while the operator is rate-limited. Channels are adapters, each enabled only when its env vars are set in `$BOARD_CONFIG_DIR/.env`:

| adapter | env | notes |
|---|---|---|
| `slack` | `SLACK_WEBHOOK_URL`, `SLACK_MENTION` (e.g. `<@U123>`) | The mention guarantees a mobile push. |
| `desktop` | (WSL: `powershell.exe` present) | Windows toast and sound. Best effort. |
| `email` | `SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, ALERT_EMAIL_TO` (STARTTLS / SSL by port) | python smtplib |
| `line` | `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_TO` (user/group id) | LINE Messaging API push. |
| `sns` | `AWS_SNS_TOPIC_ARN` (+ aws cli creds) | Optional, via `aws sns publish`. |
| `webhook` | `ALERT_WEBHOOK_URL` | Generic JSON POST, for ntfy/PagerDuty/etc. |

Levels and the default routing (overridable in fleet.yaml under `alerting.routes`):

| level | when | channels |
|---|---|---|
| `info` | FYI | slack |
| `warn` | **L1**: a new needs-you or decision, or usage ≥ 80% | slack (with mention) + desktop |
| `high` | **L2**: unacknowledged 10 min, or usage ≥ 90% ("prepare to switch account") | + email |
| `urgent` | **L3**: unacknowledged 30 min, a production gate, a permission prompt unacknowledged 10 min, usage ≥ 95%, or limit hit | + line (+ sns) |

- **Ack:** a decision leaves `pending`, or a session leaves needs-you. An ack stops escalation.
- **State:** kept in `$BOARD_HOME/alerts.json` as `{key: {level, first_at, last_at, acked_at}}`. The key is `decision:<id>`, `session:<code>:needs-you`, `usage:<window>:<threshold>`, `limit:<hit_at>`, or one of the resource and operator keys in "Resources".
- **Quiet hours:** `alerting.quiet_hours: "23:00-08:00"`. Only `urgent` passes during them.
- **Notification controls:** mutes, per-channel and global min levels and presence-aware reduction
  apply at delivery time; see "Notification controls".
- **Dedupe:** at most one send per key per level. A re-alert at the same level happens only after `alerting.repeat_minutes` (default 60).

### Board links in alerts

An optional public board URL makes every alert carry a link that opens the board on the item.

- **Config:** env `BOARD_PUBLIC_URL` (process environment, then `$BOARD_CONFIG_DIR/.env`), else
  `board_url` in fleet.yaml. It must be `https` (`http` only for `127.0.0.1` or `localhost`), with no
  user name or password, no query and no fragment; a trailing `/` is dropped. Unset or rejected →
  no link, and every message is exactly what it would be without this feature. `board doctor` and
  `lib/fleet.py board-url-check` say why a value is rejected.
- **Link:** `<base>/?d=<decision id>`, `?c=<consensus id>`, `?m=<message id>`; anything else (usage,
  limit, operator and ad-hoc alerts) links to `<base>/`. Each id must match its strict pattern
  (`^d-[0-9]+-[a-z0-9]+$`, `^c-…`, `^m-…`); an id that does not links to the root instead. A query,
  not a `#fragment`, because the Cloudflare Access login redirect and some mail clients drop
  fragments.
- **Which item:** an alert's optional `board` id (`board-notify send --board ID`), else the id in a
  `decision:`, `consensus:` or `message:` key. A `session:<code>:needs-you` alert for a permission
  prompt links to that prompt's pending permission decision (same `tool_use_id`) and follows the
  session to its next prompt.
- **Delivery:** the link is worked out when an alert is delivered, so escalations and repeats carry
  it. It is the message's `url` (Slack `<link>`, a plain line in email, LINE, SNS and the desktop
  toast, `url` in the webhook JSON). The caller's own url (an artifact or session link) moves to the
  next line as `ref` (`ref` in the webhook JSON). Both lines are labelled so they are easy to tell
  apart on a phone: `Decide on the board: <link>` (`View on the board:` for consensus and
  messages, `Open the board:` for the root), then `Open the session: <link>` for needs-you alerts,
  `Open the artifact: <link>` for decisions, `Link: <link>` otherwise. Both links get the same
  Slack handling: only a plain http(s) URL becomes a `<link>`, anything else is escaped text.
  Without a board URL there are no labels and no `ref`: the output is byte for byte the same as
  without this feature (pinned by `tests/test_notify.sh`).
- `board notify --link ID|root "text"` appends the link to a plain notify. The `agreed` and
  `objected` consensus notifies link to their item, as does the notify for an item returned
  because it belongs to an earlier away period. The owner-back notify (`board away off`, or the
  away file removed by hand) links to the item only when exactly one item came back, and to the
  board root when several did.
- **Page:** `board.html` reads `d`/`m`/`c` from `location.search` (or a `#<id>` / `#dec-<id>` hash),
  validates the id with the same pattern, and once that feed has loaded scrolls the card into view
  and highlights it briefly (no smooth scroll or fade with `prefers-reduced-motion`).
  - **Focus** goes to the card's free-text answer field when it has one, otherwise to the card
    itself (`tabindex=-1`). It never goes to an option button, so a single Space or Enter cannot
    answer a question, a gate or a permission prompt. The focus is set after the browser's own
    fragment scroll, so the `#dec-<id>` form keeps it too. If the viewer has already clicked,
    tapped or typed since the page opened (or focus is somewhere else), the card is only
    highlighted: no scroll and no focus change, so typing never moves into the linked card.
  - **Afterwards** the `d`/`m`/`c` parameter is removed from the address bar (`history.replaceState`),
    so a reload does not open the card again. A hash is left as it is.
  - **Unknown or already answered** id (a valid pattern, but no card): a small notice says so.
  - **Malformed** id (fails its pattern): ignored. Nothing is shown and the parameter stays in the
    address bar.
  - In the claude.ai Artifact the query may not reach the page; then nothing happens.

## Notification controls (mute, triage, presence)

The owner's Slack-like control over push. It changes **what is pushed**, never what is recorded:
the board always shows every session, decision and alert, alerts.json keeps escalating, and every
held send is logged.

**Prefs file** (`$BOARD_HOME/notify-prefs.json`, 0600, written atomically under an exclusive
`flock` on `notify-prefs.lock`, 10 s timeout, by `lib/prefs.py` only):

```json
{ "version": 1,
  "mutes":     { "slack": {"until": 1794664800000, "by": "board:local", "at": 0, "include_urgent": false},
                 "all":   {"until": null, "by": "CP", "at": 0, "include_urgent": true} },
  "min_level": { "all": "high", "email": "urgent" },
  "presence":  { "reduce": true, "min_level": "high" },
  "updated_at": 0, "updated_by": "board:local" }
```

- Keys of `mutes` and `min_level`: `slack`, `desktop`, `email`, `line`, `sns`, `webhook`, `all`.
  Unknown keys and malformed values are ignored. `until: null` means until unmuted; a malformed
  `until` (a string, a float, a bool) is ignored: the whole mute is dropped (fails open), and the
  next write removes it; an expired mute is ignored at once and dropped on the next write.
- `--by` follows the `board away` rule (`^(board:)?[A-Za-z0-9][A-Za-z0-9@._+:-]{0,199}$`).
  Every change appends `{type: "notify-prefs", action: "mute"|"unmute"|"level"|"presence", ...}`.

**Defaults from config.** `alerting.min_level` in fleet.yaml (or alerting.yaml) is a level (the
global floor) or `{all|channel: level}`; `alerting.presence` is `{reduce, min_level,
window_seconds, idle_seconds}` (defaults `true`, `high`, `120`, `900`). The prefs file overrides
`min_level` per key and `presence.reduce` / `presence.min_level`; `board notify-level X default`
removes a prefs floor so the config (else `info`) applies again.

**CLI** (`bin/board` → `lib/prefs.py`; exit 0 ok, 1 error such as a held lock, 2 usage, nothing written):

- `board mute CHANNEL|all [--for 30m|1h|4h|2d | --until HH:MM|ISO] [--include-urgent] [--by WHO]`.
  `--until HH:MM` is the next such local time; ISO without an offset is local time; at most 30
  days ahead, never in the past.
- `board unmute CHANNEL|all [--by WHO]`: `all` removes **every** mute. Unmuting a channel while
  `all` is muted says so on stderr (the channel stays muted).
- `board mute status [--json]`: mutes in force, floors with their source (`prefs`, `config`,
  `default`), the effective floor per channel, the presence policy and who is watching now.
- `board notify-level CHANNEL|all info|warn|high|urgent|default [--by WHO]`.
- `board notify-presence on|off [--level warn|high|urgent] [--by WHO]`.

**Presence** (`$BOARD_HOME/presence.json`, 0600, `flock` on `presence.lock`, written by
`board-serve` only): `{<identity>: {at, visible, interaction_at, via}}`. The page sends a
heartbeat on load, every 30 s while visible, on every visibility change and on `pagehide`
(`POST /api/presence`, approvers only; the identity is the listener's verified one; the page
sends how long it has been idle, `idle_ms`, and the server turns it into `interaction_at` with
its own clock). An identity
**is watching** when it is in `approvers` (case-insensitive), its last heartbeat says `visible`,
is at most `window_seconds` old, and its `interaction_at` (last click, tap, scroll or key) is at
most `idle_seconds` old (`0` = no idle limit). A page left open on an unattended screen therefore
stops counting after 15 minutes. Entries older than a day are dropped; at most 50 are kept.
Two limits, by design:

- A heartbeat that arrives late (after the page's `pagehide` beacon, or when that beacon is
  lost, for example on a dropped connection) can keep the identity counted as watching for at
  most `window_seconds` after the page went away. Escalations that fall into that window are
  held only if they are below `presence.min_level`.
- Presence is not per person: **any one** approver watching reduces push for everyone, because
  the channels are shared (one Slack webhook, one email list).

**Delivery rule** (`lib/notify.py`, after dedupe and quiet hours, for every routed and configured
channel of every delivery: first sends, escalation re-sends, repeats and retries):

1. **Muted:** a mute of `all` or of the channel that has not expired holds it (`muted:all`,
   `muted:<channel>`), **unless the level is `urgent` and that mute does not have
   `include_urgent`**.
2. **Triage:** the level must reach the stricter of the `all` floor and the channel's own floor,
   else `below_min_level`.
3. **Presence:** while `presence.reduce` is on and an approver is watching, a level below
   `presence.min_level` is held (`owner_present`).

Each held channel appends `{ts, type: "alert-suppressed", key, channel, level, reason}` to
`events.jsonl`. When **every** routed channel is held, the level counts as delivered
(`sent[level]` and `suppressed[level]` are set, status `suppressed`): nothing is replayed when the
mute ends or the owner leaves, and an alert that is still open and unacked goes out at its next
escalation step or `repeat_minutes` repeat, under the rules in force then. A partly held delivery
is a normal delivery to the other channels. A prefs or presence file that cannot be read **fails
open** (everything is delivered).

**Breakthrough.** `urgent` is the top level, so it always passes triage and presence; only a mute
with `include_urgent` (`board mute all --include-urgent`: "mute everything, including urgent")
stops it. Urgent covers a production gate (urgent at once), a usage-limit hit, usage ≥ 95%, a
permission prompt unacknowledged for 10 minutes, anything unacknowledged for 30 minutes, and any
`board-notify send --level urgent`. (There is no operator-silent alert yet; it arrives with the
resources change and is urgent, so it breaks through the same way.)

**Other senders.** `board notify` (the plain Slack post) is an `info` message on `slack`: it asks
`board-notify gate --channel slack --level info --key notify` (exit 0 = send, 10 = held and
logged) and sends nothing when held. `board-notify test` ignores the controls (it is an explicit
check of a channel). `board-notify status [--json]` shows the effective controls (`controls`),
never a credential.

**Board page.** A Notifications dialog (mute all with a duration and "include urgent", per-channel
mute and min level, the global min level, "reduce push while I'm watching" and its level) and a
banner while anything is muted ("Slack muted until 14:00", and whether urgent breaks through).
Self-hosted: approvers change it through `POST /api/notify-prefs`, everyone else sees it
read-only, and `/api/state.notify` carries it (no watcher list; `presence.json` does not wake the
SSE stream). Artifact: read-only from `meta/notify`, which the operator mirrors
(`prompts/board-operator.md`); the page drops mutes whose `until` has passed by itself.

## Usage meter (approaching 5h / weekly limits)

Claude Code's statusline input JSON carries `rate_limits.five_hour` and `rate_limits.seven_day`, each `{used_percentage, resets_at}`.

- `hooks/board-statusline-tap.sh` wraps the user's existing statusline command (`BOARD_STATUSLINE_NEXT`). It writes `$BOARD_HOME/usage.json` as `{five_hour:{pct,resets_at}, seven_day:{pct,resets_at}, updated_at, session_id}` atomically, then execs the original command with the same stdin. It never changes the statusline output.
- The same tap writes the session meter, `$BOARD_HOME/statusline/<session_id>.json` (0600), with only `{ctx_pct, cost_usd, model, at}`: `context_window.used_percentage` (else computed from `current_usage` and `context_window_size`), `cost.total_cost_usd`, `model.display_name` (≤ 40 chars) and the time. Nothing else from the input is kept, and a session id that is not filename-safe is ignored. One jq call, in the background job the usage record already uses.
- The collector crosses thresholds 80/90/95 (configurable) per window and raises the levels above. Message examples:
  - "5h usage 90% — resets ~14:20 — prepare to switch account"
  - "weekly usage 95% — resets Mon 09:00 — switch account now"
- The operator mirrors usage into `meta/operator.usage`. The page shows two meters (5h and 7d) with reset countdowns. At ≥90% it shows an amber "Prepare to switch account" banner, and at ≥95% or on limit-hit a red one, with a link to the failover section in Info.

### Burn rate (`usage-samples.json`)

The collector feeds the 5-hour reading of `usage.json` into a small ring buffer on every run
(`lib/collect.py`, `add_usage_sample` / `burn_rate`):

```json
{ "resets_at": 1731500000000,
  "samples": [ {"t": 1731490000000, "pct": 61.0}, ... ],
  "burn": {"status": "danger|ok|unknown|stale", "pct_per_hour": 6.0, "hit_at": 1731498000000, "n": 58},
  "computed_at": 1731490001000 }
```

- A sample is taken at the reading's own `updated_at`, never the collector's clock. A reading no
  newer than the last sample is ignored; one within 20 s of the last sample replaces it (several
  sessions report the same account-wide number). Samples older than an hour are dropped, at most 180
  are kept. When `five_hour.resets_at` moves by more than 5 minutes, the window reset: the buffer
  starts over. `pct` is read as 0–100, as the tap writes it (a reading under 1 is under 1%).
- `burn` is a least-squares line through the samples of the **last 30 minutes**, projected to 100%:
  - `unknown`: fewer than 3 samples, or under 5 minutes between the first and the last;
  - `stale`: the newest sample is over 15 minutes old (no session reported usage since);
  - `danger`: the line reaches 100% (`hit_at`, epoch ms) before `resets_at` (or there is no reset
    time and the line rises; already at 100% → `hit_at` is that sample);
  - `ok`: the line is flat or falling (`hit_at` null), or the reset comes first.
  - `pct_per_hour` is the slope, rounded to 0.01; `n` the number of samples in the fit.
- The file is rewritten only when the samples or `burn` changed. `board-serve` adds `burn` (those
  four fields only) to `operator.usage`. The operator does not mirror it, so the Artifact page shows
  no burn rate. Nothing alerts on it: it is a glance on the Deck panel, the 80/90/95% alerts stay
  as they are.

## Deck panel

A key-deck panel near the top of the board (`artifact/board.html`, `#deck`): 5 × 3 keys and a wide
strip (3 columns on a phone), every key a `<button>` with an `aria-label`, every label through
`tr()`. It shows only data the page already has, and adds **no new power**: a key navigates (scroll
and focus a card, open a dialog, a Remote Control link) or, for approvers on the self-hosted board,
calls an existing approver-only route after a confirm step.

| key | shows | press | second action (approvers, board-serve and demo only, after a confirm) |
|---|---|---|---|
| HITL | sessions waiting and the longest wait (open decisions, then needs-you sessions without one); a green ✓ when nothing waits | scroll to the longest-waiting decision card and focus it (its answer field, else the card; never an option button), like `?d=`. Again within 5 s: the next one, oldest first | ✓✓ in the corner, or a long press (≥ 0.8 s): acknowledge all open alerts (`POST /api/alerts/ack`) |
| S1–S8 | the first eight sessions in board order, offline ones last: code, state colour, time since the last update, title. A waiting key blinks: purple for a question, orange for a plan (`ExitPlanMode` or a lifecycle gate), red for a permission | the decision of the prompt the pane shows, else the session's oldest pending decision, else its card (opened) | ↗ the Remote Control link (everyone who sees the link on the card) |
| 5h, 7d | usage rings with the reset countdown; dimmed when `usage.updated_at` is over 15 min old | focus the usage meter (opens its popover) | — |
| 5h burn | `⚠ 1h 29m to the 5h limit`, `pace OK`, `pace …` (too few samples) or stale, with the slope in %/h | as 5h | — |
| System | the resources level, mem and swap bars, the OOM badge | scroll to Resources | — |
| Quiet | the notification mute state | open the Notifications dialog | mute all push for 1 h / unmute all (`POST /api/notify-prefs`) |
| Owner | owner-away on or off, since when | scroll to Consensus | away on / off (`POST /api/away`) |
| 🍅 Today | the fleet's pomodoros today (sessions and teammates), focus time and a 7-day sparkline (see "Pomodoro") | open the pomodoro panel: per-session counts today with each teammate under its session, and 14 days | — |
| Fleet (wide) | up to 4 sessions with state pills (waiting first), the 5h and 7d bars and the burn rate; it shares the last row with 🍅 Today | scroll to Board | — |

- **Alert wave:** when a session newly enters needs-you (not on the first render), a ring in its
  waiting colour spreads across the deck from its key (from HITL when it has no key), once.
- **Reduced motion** (`prefers-reduced-motion: reduce`): no blink (the colour stays), no wave.
- **Adapters:** the Artifact page (claude.ai db) shows a read-only deck: no acknowledge, mute or
  away actions, for anyone. The demo is an approver whose actions change the page only.
  `board-serve` shows the actions to approvers only (`approver_ids` contains the viewer) and checks
  again on every POST.
- **Collapse:** the Hide deck button or Account menu → Deck panel; kept in `localStorage`
  (`switchboard.deck` = `collapsed`).
- Out of scope on purpose: keys that type into a session (prompts, Enter, `/compact`, Esc). A
  session's prompt is answered on its decision card, by the existing rules.
- Related project: [ulanzi-lab](https://github.com/osisdie/ulanzi-lab), a hardware key-deck dashboard (5 × 3 keys) that shows the same HITL and usage
  signals from the same hooks. The two run side by side (independent hooks, separate state; its hub listens on
  `127.0.0.1:39300`). The board never calls it.

## Pomodoro

Every Claude session, teammates included, runs its own pomodoro while it works, and the board keeps
a daily tally per stream and for the fleet (`lib/pomodoro.py`; the idea comes from the Focus Timer of
[ulanzi-lab](https://github.com/osisdie/ulanzi-lab)). The collector advances it on every run from
data it already has: zero tokens, no transcript read.

**Streams.** One per top-level session (key = its code), focused while the snapshot's `state` is
`working`; and one per teammate (key `CODE/teammate-name`, the name reduced to `[A-Za-z0-9_.-]`, at
most 64 characters), focused while `resources.json` reports it active: `idle` is not true and
`idle_s` is known and below `pomodoro.teammate_idle_s` (default 60), **or** its pane title shows the
working spinner (`busy` on the card, `lib/cards.py`; true through a long model turn at ~0% CPU), so
`working`, otherwise `rest`.
A lead's stream counts only its own state, never its teammates' work. When `resources.json` is
missing, disabled or older than 2 minutes plus two `resources.interval`s, and when a teammate is no
longer listed, its stream is `gone`.

**Timer rules** (deterministic, per stream; FOCUS = `pomodoro.focus_minutes`, default 25;
GRACE = `pomodoro.grace_minutes`, default 2; MODE = `pomodoro.mode`, `accumulate` (default) or
`strict`):

| state seen | the current pomodoro |
|---|---|
| `working` | runs. At FOCUS minutes of focus it completes at that exact moment: the stream's tally for the local day it completed in goes up by one, and the next pomodoro starts at once with the remainder (50 minutes of work = 2) |
| `done`, `rest` | paused. `accumulate`: progress kept, however long the break (agents pause between prompts all the time). `strict` (the human pomodoro): a done/rest run longer than GRACE resets it to 0:00; a shorter one keeps its progress |
| `needs-you` | paused, progress kept, however long the human takes |
| `gone` (teammate streams only) | paused, progress kept. A gone stream with no progress is dropped; one with progress is dropped after 24 hours (its day tallies stay) |
| `offline` (or a session code no longer in the snapshot) | reset |
| no collector run for longer than NODATA = max(GRACE, 60 s, 20 s + 2 × `poll_interval`) | that gap is a break. `strict`: every current pomodoro resets. `accumulate`: progress kept; the gap is never credited as focus |

Day tallies are per stream; the fleet's day (Today, the milestones) is the sum over all streams.

- The time between two collector runs counts for the state seen at the first of them; nothing is
  extrapolated past the last run.
- Focused minutes are split at local midnight in `pomodoro.timezone` (an IANA name; empty = the
  system's local time, DST-aware). A pomodoro counts on the day it completes, even if it started the
  day before. 14 days are kept: only days older than that window are dropped. A day later than
  today stays (the clock moved back across midnight, or `pomodoro.timezone` moved west).
- A clock that moves back credits nothing for that run, and a done/rest run never starts after
  `now`.
- No short-break counting: a break is just a pause (or, in `strict`, a reset after GRACE).

**`$BOARD_HOME/pomodoro.json`** (0600, written by the collector only):

```json
{ "version": 2,
  "at": 1763193600000,                       // the last run this file reflects (epoch ms)
  "settings": { "focus_ms": 1500000, "grace_ms": 120000, "tz": "Asia/Taipei", "mode": "accumulate" },
  "sessions": { "API": { "state": "working", "acc": 600000,   // ms into the current pomodoro
                         "away_since": null,                  // start of the current done/rest run
                         "done_at": 1763192000000,            // the last completion
                         "seen_at": 1763193600000 },          // last run it was not offline/gone
                "API/tests": { "state": "rest", "acc": 420000, "away_since": 1763193300000,
                               "done_at": 1763191000000, "seen_at": 1763193600000 } },
  "days": { "2026-11-15": { "API": { "done": 3, "focus_ms": 5400000 },
                            "API/tests": { "done": 2, "focus_ms": 3420000 } } },
  "notified": { "2026-11-15": 10 } }          // the last fleet milestone reached today
```

Only stream keys, states and numbers: no prompt, title or cwd. Version 1 (no teammate streams,
no `mode`, no `seen_at`) is read as is: its sessions and days carry over (today's tallies kept),
`seen_at` starts at its `at`, and the next write is version 2.

It is rewritten when something a viewer sees changes (a state, a tally, a reset, a milestone) and
at least every 20 s while a pomodoro runs (in `strict`, also while one is paused: a gap would
reset it). A run that skips the write loses nothing:
the next run integrates again from the stored `at`. So a restarted collector carries on: the stored
`at` can lag the last run by up to 20 s, so a restart shorter than NODATA − 20 s (100 s with the
defaults) keeps every pomodoro. `pomodoro.enabled: false` removes the file.

**`/api/state` `pomodoro`** (`board_view`, numbers, session codes and teammate names only; null when
there is no file):

```json
{ "enabled": true, "focus_ms": 1500000, "grace_ms": 120000, "tz": "", "mode": "accumulate",
  "today": "2026-11-15", "day_start": 1763164800000,      // today's local midnight
  "seen": 1763193600000, "seen_step_ms": 300000,          // the last run, floored to 5 min (null: nothing runs)
  "fleet": { "done": 14, "focus_ms": 20400000, "teammates_done": 2 },   // every stream, teammates included
  "sessions": {                                           // the streams worth a row (below): a teammate's right after its session
    "API": { "done": 3, "focus_ms": 4800000, "running": true, "state": "working",
             "started_at": 1763193000000, "progress_ms": null, "done_at": 1763192000000 },
    "API/tests": { "done": 2, "focus_ms": 3420000, "running": false, "state": "rest",
                   "started_at": null, "progress_ms": 420000, "done_at": 1763191000000 },
    "WEB": { "done": 1, "focus_ms": 1800000, "running": false, "state": "needs-you",
             "started_at": null, "progress_ms": 360000, "done_at": 1763180000000 } },
  "days": [ { "date": "2026-11-02", "done": 9, "focus_ms": 14580000 },   // no per-stream map (GET /api/pomodoro/day and `board pomodoro --json` have it)
            … 14 entries, oldest first, today last … ] }
```

The view changes only when something a viewer sees changes, never with the collector's heartbeat,
so a fleet that keeps working (or keeps waiting) costs no SSE frame beyond one per 5-minute `seen`
bucket (none while nothing runs):
- A running pomodoro has a fixed `started_at` (= `at` − progress) and `progress_ms: null`; a paused
  one has `progress_ms` and `started_at: null`.
- Today's `focus_ms` (per session, `fleet`, the last `days` entry) leaves out the running stretches.
  The page adds `min(now, started_at + FOCUS) − max(started_at, day_start)` for each running
  session. This base can be negative: when a pomodoro paused across midnight, part of its progress
  counts for yesterday, but the formula above credits all of it after `day_start`. The server never
  clamps it (that would break base + live); the page clamps only the sum it shows at 0.
- The page runs the clocks while `now − seen` < `seen_step_ms` + 3 minutes, and otherwise holds them
  at `seen + seen_step_ms` and shows them as paused (the collector stopped).
- No `at`: `board pomodoro --json` (and `--mirror`) give the numbers as of the last run instead.
- Bounded however many teammates came and went: `sessions` lists only the streams with a 🍅 or focus
  today, or a current pomodoro (running, or paused with progress). A session with more than 50 such
  teammates keeps the running ones and then the most 🍅 and focus; the rest fold into one
  `CODE/+others` row (`done`, `focus_ms`, `streams`: how many, `active`: how many with a 🍅 or focus
  today, `state: "folded"`, `progress_ms: 0`, never running; `+` never starts a teammate name). `fleet` and `days` count every stream, so they stay exact.
- The page shows a session's own stream on its card's ring, its teammates' combined count and focus
  next to it ("teammates +N 🍅 · focus"), the fleet total on the 🍅 Today key, and in the panel the
  totals (kept in view while it scrolls), the champion, 14 days, then one row per session with its
  teammates included and folded under it: the top 10 sessions with something today, "Show all", and
  a filter by session or teammate once there are more than 10.
- Each of the 14 days is a tab (click, tap, Enter/Space; ←/→ move between days, Home/End jump to the
  oldest/today; `aria-selected` and a tinted, underlined bar mark the pick). Today is picked by
  default and uses this view. Picking a past day shows its total, its champion and its sessions
  (same rows, teammates folded) below the chart, from the endpoint below, fetched only then and
  cached while the panel is open (a loading line, an error with "Try again"); the fetch waits
  150 ms, so holding an arrow key fetches only the day it stops on. The demo makes a
  day's breakdown up from its totals (the same every time); the Artifact keeps the days pickable
  for their totals and says "Daily totals only on this board". Nothing polls.

**`GET /api/pomodoro/day?d=YYYY-MM-DD`** (`day_view`; board-serve only): one of the 14 kept days
(today included), in the document's `settings.tz`:

```json
{ "date": "2026-11-10", "today": "2026-11-15", "tz": "",
  "fleet": { "done": 12, "focus_ms": 19440000, "teammates_done": 3 },   // every stream that day
  "sessions": { "API": { "done": 4, "focus_ms": 6600000 },
                "API/tests": { "done": 3, "focus_ms": 4800000 },
                "API/+others": { "done": 1, "focus_ms": 1700000, "streams": 9, "active": 9 },
                "WEB": { "done": 4, "focus_ms": 6340000 } } }
```

Numbers, codes and teammate names only (scrubbed like the state), no clock. `sessions` lists the
streams with a 🍅 or focus that day, at most 50 teammates per session (the most 🍅, then focus) and
the rest folded into `CODE/+others` (`streams`, `active`), so every sum over the rows equals
`fleet`. `tz` is the setting only when it names a real zone (else `""`). A malformed or impossible
date → 400; a day before the 14 kept or after today, or no file → 404. Around local midnight the
page's view and the endpoint can disagree on which day is today until the next state frame: the
oldest bar may then get a 404 (the page shows its error state), and today's rows always come from
the view.

**Artifact:** the operator may mirror `board pomodoro --mirror` to
`meta/pomodoro` (`today, tz, focus_ms, mode, fleet, sessions: {stream: {done, focus_ms}}, days,
updated_at`): daily totals only, no running clock, nothing sensitive. The page reads it read-only.
Bounded like the board view: only streams with a 🍅 or focus today, at most 50 teammates per session
plus a `CODE/+others` row (`streams`, `active`), and `days` without per-stream maps; `fleet` and
`days` stay exact.

**CLI:** `board pomodoro [today|week] [--json|--mirror]`.

**Notify** (off by default): with `pomodoro.notify: true`, every `pomodoro.notify_every`-th (10)
fleet pomodoro of the day sends one `info` alert, key `pomodoro:<day>:<n>`, title "AI fleet finished
its 10th 🍅 today", through lib/notify.py (no escalation, no repeat). Milestones are recorded even
while it is off, so switching it on mid-day does not replay them. `board-collect --no-notify` and
`--baseline` hold it.

## Resources

The collector (`lib/resources.py`, zero tokens) samples the machine every `resources.interval`
seconds (default 15) into `$BOARD_HOME/resources.json`. It is rewritten when something changed
beyond the board's buckets (percentages to 5%, sizes to 100 MB, ages to the minute; levels, OOM,
teammates and the operator watch exactly), and at least every 60 s so its `at` shows the sampler
is alive; `snapshot.json` carries
only `{level, worst}`. The board shows it; **nothing in the board
ever stops a process**.

```json
{ "at": 0, "platform": "linux|wsl|darwin|other", "level": "ok|warn|high|critical", "worst": "mem"|null,
  "levels": { "mem": "ok", "swap": "ok", "load": "ok", "disk:disk1": "ok", "oom": "critical" },
  "cpu": {"pct": 37, "ncpu": 16, "load": [1.2, 0.9, 0.8], "load_per_cpu": 0.08},
  "mem": {"total_mb": 0, "available_mb": 0, "available_pct": 0, "psi_some_avg60": 0.0},
  "swap": {"total_mb": 0, "used_pct": 0}, "oom_kills": 2, "oom_kills_delta": 0, "oom_recent": false,
  "last_oom_at": null,
  "disks": [ {"id": "disk1", "mount": "/", "total_mb": 0, "free_mb": 0, "free_pct": 0, "level": "ok",
              "labels": ["$BOARD_HOME", "API"], "unresponsive": true?} ],
  "sessions": [ {"code": "API", "pid": 123, "rss_mb": 0, "cpu_pct": 0, "tree_rss_mb": 0, "heavy_n": 0,
     "classes": {"test": {"n": 1, "rss_mb": 0, "cpu_pct": 0}},
     "children": [ {"class": "test|build|browser|container|claude|other", "comm": "pytest", "pid": 0,
                    "rss_mb": 0, "cpu_pct": 0, "age_s": 0, "heavy": true, "via": "<teammate>"|null} ],
     "teammates": [ {"name": "reviewer", "pid": 0, "rss_mb": 0, "cpu_pct": 0, "age_s": 0, "idle_s": 0|null,
                     "idle": false} ],
     "teammates_n": 0, "teammates_rss_mb": 0, "idle_teammates_n": 0} ],
  "orphan_teammates": [ teammate rows whose parent session is not live ],
  "heavy_running": 1, "teammate_idle_minutes": 30, "operator": {"watch": true|false|null},
  "unavailable": ["psi", "swap", "disk"], "interval": 15, "config_errors": [],
  "thresholds": {"load_per_cpu": [1.5, 3.0], ...} }
```

- **`thresholds`** are the `[warn, high]` pairs in use (`resources.thresholds` after defaults and
  validation). They go to `/api/state` and `meta/resources`, so the System tile can say why its
  level is what it is ("Load 27.7 / 4 cpu = 6.93 per cpu (high ≥ 3.0)"). A board without them uses
  the defaults.

- **Sources.** Linux/WSL: `/proc/{stat,loadavg,meminfo,vmstat,pressure/memory,uptime}` and
  `/proc/<pid>/{stat,cmdline}`. Elsewhere: `os.getloadavg()`, `statvfs` and `ps -axo
  pid=,ppid=,rss=,%cpu=,time=,etime=,args=`. A metric that cannot be read is listed in
  `unavailable`, never reported as 0. CPU% is the delta of cumulative CPU time (`/proc` ticks or
  ps `time`) against the previous sample; on the first one it is `null` (ps: its own `%cpu`).
- **Sessions.** Each live session's process tree is walked by ppid from its registry pid.
  `children` keeps the top 3 per class; `classes` sums all of them. `rss_mb`/`cpu_pct` are the
  claude process itself.
- **Teammates** are separate `claude` processes started with `--agent-id <name>@<team>` /
  `--agent-name`, usually in extra tmux panes (so not in the session's tree). A teammate belongs to
  the session whose `session_id` equals its `--parent-session-id`; failing that, the session whose
  tree contains it, or whose code or alias equals the `@<team>` part. The rest are
  `orphan_teammates`. A teammate's `rss_mb`/`cpu_pct` cover its whole subtree, and its processes
  also appear in the session's `children` with `via`. `idle_s` is the time since its subtree last
  used ≥ 1% CPU or started a process (`null` on first sight); `idle` means `idle_s` ≥
  `teammate_idle_minutes` × 60.
- **Classes** match `comm` plus the base names of the first argv words (a shell is never a test,
  nor is a wrapper or launcher that only starts the job: `timeout`, `env`, `nice`, `ionice`,
  `nohup`, `stdbuf`, `xargs`, `time`, `setsid`, `chrt`, `taskset`, `flock`, `npx`, `npm`, `pnpm`,
  `yarn`, `uv`, `uvx` by the first word of `comm`, or node running `npx-cli.js`,
  `npm-cli.js`, `pnpm.cjs`, ...; they are `other` and their child is classed on its own):
  test `pytest|jest|vitest|go test|cargo test|mvn|gradle`, build `tsc|webpack|vite build|cargo
  build|docker build`, container `docker|containerd`, and browser `chrome|chromium|firefox|headless_shell`
  matched on `comm` alone (an idle Playwright MCP server mentions them in its argv but is not a
  browser; `playwright test` is a test); `resources.process_classes` (a map of class: regex) overrides or adds patterns; anything else,
  or a pattern that does not compile, is ignored and listed in `config_errors`. On the `ps` path
  `comm` is the base name of the executable path up to the first ` -`, so a path with spaces (a
  macOS app bundle) stays whole. **Heavy** = test,
  build or browser, or RSS > 1 GiB; `heavy_running` counts heavy processes whose parent is not
  heavy (one browser = one).
- **Privacy.** Command lines are matched and never stored (a teammate's argv carries keys): only
  `comm`, the class and the teammate name leave the sampler. `/api/state` and `meta/resources`
  drop pids and the local `mount`: a disk is an opaque id (`disk1`, stable across samples) with the
  labels of what lives there (`$BOARD_HOME`, session codes, or `disks[i]` for configured paths),
  and level keys, `worst` and alert keys use that id.
- **Disks.** `resources.disks: auto` probes `$BOARD_HOME` and each live session's cwd; a list
  probes those paths. nfs, cifs/smb, 9p, FUSE and similar mounts are skipped in `auto` (decided
  from `/proc/self/mountinfo`, without touching the path). The probes run in threads with a 2 s
  budget per sample; a path that does not answer is reported `unresponsive` (and `disk` in
  `unavailable`) and is not probed again for 10 minutes.
- **Levels.** Per metric, from `resources.thresholds` `[warn, high]`: `mem_available_pct`,
  `mem_available_mb`, `psi_mem_some_avg60` (the worst of the three is `mem`), `swap_used_pct`,
  `load_per_cpu`, and per disk `disk_free_pct`/`disk_free_mb`. Swap counts only while `mem` is at
  warn or worse (swap use alone is normal). Hysteresis: a lower-is-worse value leaves a level at
  threshold × 4/3 (enter at 15% available, leave at 20%), a higher-is-worse one at threshold × 3/4.
  Dwell, per direction: 2 samples in a row worse than the committed level move it up, 2 better
  move it down, to the step of that streak closest to the committed level. `critical` = `high` for
  2 samples, or a new OOM kill (`oom`, held 10 minutes; a counter that goes backwards after a
  reboot counts from 0). `level`/`worst` are the maximum. A threshold that is not two numbers falls
  back to its default and is listed in `config_errors` (and by `board doctor`).
- **Operator watch.** `operator.watch` is whether a `board-serve --watch` (`serve.py --watch`)
  process exists. It is checked even with `resources.enabled: false` (then `resources.json` holds
  only `{at, enabled: false, platform, operator}`).

Alerts (in `notify.tick()`, auto-resolved when the condition is gone; a `resources.json` older than
4 intervals leaves `res:*` as they are):

| key | level | notes |
|---|---|---|
| `res:mem`, `res:swap`, `res:disk:<id>` | warn → high (resource high or critical) | no time-based escalation; repeats every `repeat_minutes`. Memory alerts name idle teammates to reclaim first and the largest heavy processes. |
| `res:oom:<count>` | high | once per OOM kill |
| `res:idle-teammates:<code>`, `res:orphan-teammates` | info; warn while memory (PSI included) or OOM is at warn or worse, never for swap alone | teammates idle past `teammate_idle_minutes` (default 30); no repeat |
| `operator:stale` | high → urgent after 30 min | only with `serve.enabled` (local mode): `operator.json` `heartbeat_at` older than `schedule.operator_stale_minutes` (10) or missing, or no `board-serve --watch` process, **and** something waits for the operator: what `--watch` would hand it (an answered question or gate, not a permission prompt; a new message). A decision still pending waits on the owner, not the operator. In Artifact mode the heartbeat is in `meta/operator`, which the collector cannot read, so nothing is judged. Held in quiet hours until it turns urgent. |

The page shows a "System 系統" header tile (memory and swap bars, load, CPU, fullest disk, heavy
jobs, OOM count) and a Resources section (per session: claude RSS/CPU, processes by class with
the top 3, teammates with idle ones flagged; orphaned teammates; disks; unavailable metrics). A
red banner shows at `critical`, and in local mode when `operator.watch` is `false`.

`board resources [--json|--mirror] [--refresh]` prints the last sample (`--refresh`: a fresh one,
written nowhere, so the collector's dwell state does not move); `board doctor` reports
the platform, missing metrics, the sample's age and the operator watch.

## Scheduler

The collector feeds every run's tasks, session states and `resources.json` to
`lib/scheduler.py` `decide()`, a pure function (no I/O; the same inputs give the same output and
digest). It writes `$BOARD_HOME/scheduler.json` (0600, atomic) only when the decision changes, so
the file has no heartbeat and `generated_at` is when the decision last changed. Jam episodes and
advisory delivery are remembered in `schedule-state.json` (0600, `lib/advisories.py`; both files
are written under `schedule.lock`). `snapshot.json` carries only `schedule`:
`{mode, due: [codes with advice to send], jams: <open jams>}` (null when the step failed), so the
operator wakes only when one of those changes. `schedule.enabled: false` writes an
`enabled: false, mode: "off"` document; `board schedule off --by WHO` (the `schedule-off` file,
audited) does the same with `paused: true` until `board schedule on`.

### Task fields

All optional; task files without them stay valid. `lib/tasks.py` writes a task atomically, mode
0600, with `schema_version: 1` (`board task new` too), and refuses to modify a file with a higher
`schema_version`. Each change sets `updated_at` and appends `{ts, type: "task", id, action, ...}` to
`events.jsonl`. Every read-modify-write of a task (`board task`, the gate entries `board ask --task`
and `board answer` add) holds `tasks/<id>.json.lock` (flock, 10 s), so no update is lost. A file
whose `id` is not its name is never modified.

Exit codes of the lock: `board ask --task` takes the task's lock **before** it writes the decision
(the secret guard runs before that, never under the lock); `board answer` on a task's gate takes it
before it marks the decision answered. **Exit 8** means the task was busy (10 s) or its gate
could not be written: `ask` created nothing; `answer` recorded nothing, or kept the answer without
updating the gate. Either way, run the same command again; the operator relays only after exit 0.
Exit 1 keeps meaning "not found".

| field | values | set by |
|---|---|---|
| `status` | `active` (default) `paused` `blocked` `done` `dropped` | `board task resume|pause|block|done|drop ID` (`done` sets `done_at`, the others clear it) |
| `repo`, `branch`, `base` | `[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,199}`, no `..` | `board task set ID --repo R --branch B --base B` |
| `size` | `S` `M` `L` (unset = M for scoring) | `--size` |
| `heavy` | `true` / `false` (test runs, builds, browsers) | `--heavy yes|no` |
| `priority` | 0–3 (unset = 1) | `--priority` |
| `depends_on` | `[{task, kind: "merge"|"deploy"|"done"}]` | `board task dep ID --on T [--kind K]` (default `done`), `--remove T`. The task must exist; self-dependencies and cycles are refused |
| `pr` | `{url, state: "open"|"merged"|"closed", head_sha?, method?: "squash"|"merge"|"rebase", merged_at?}` | `board task pr ID --url U --state S [--head SHA] [--method M]`, reported by the session itself (no forge polling). The same URL keeps fields not repeated |
| `progress_at` | epoch ms | `board task progress ID` ("still moving": resets the stall clock) |

A dependency is met when the task it names is `done`, or for `merge` its `pr.state` is `merged`,
or for `deploy` it reached stage `maintain` (owner decision: an approved `production` gate alone
only allows the deploy and never satisfies a `deploy` dependency). A bad value exits 1 and
leaves the file alone; a usage error exits 2.

### scheduler.json

```json
{ "schema_version": 1, "enabled": true, "mode": "normal|conserve|wait-reset|degraded|off|error",
  "error": "ValueError"?, "paused": true?,
  "usage": {"mode": "conserve|wait-reset", "why": "limit|5h|burn|weekly", "resets_at": 0}|null,
  "level": "ok|warn|high|critical"|null, "resources": "ok|stale|missing|disabled",
  "max_heavy": 2, "heavy_used": 1, "start": ["t-.."],
  "queue": [ {"rank": 1, "task": "t-..", "owner": "API", "session": "API"|null,
              "state": "run|ready|queued|deferred|blocked|paused", "score": 71, "heavy": false,
              "blocked_by": [{"kind": "manual|redline|human|task|session|resource|reset", "ref": ".."}],
              "why": ["unblocks 2", "after t-..", "finish-first t-.."], "stuck": "warn|high"?} ],
  "stuck": [ {"task": "t-..", "session": "API", "severity": "warn|high", "since": 0} ],
  "advisories": [ {"id": "a-<12 hex>", "to": "API",
                   "kind": "defer|resume|finish-first|rebase-needed|wait-for-reset|stop-own-heavy",
                   "task": "t-.."|null, "severity": "info|warn|high", "text": "..",
                   "pid": 0, "comm": "pytest", "class": "test", "rss_mb": 1024,
                   "delivery": {"state": "board|shadow|due|held|sent", "sent_at": 0?, "n": 1?,
                                "reply": "ack|done|cannot"|null?}} ],
  "due": {"API": ["a-.."]},
  "answer_first": [ {"decision": "d-..", "unblocks_sessions": 2, "unblocks_tasks": ["t-.."]} ],
  "jams": [ {"id": "j-<rule>-<scope>", "rule": "J1".."J8", "severity": "info|warn|high", "since": 0,
             "sessions": ["API"], "text": "..", "alert": "info|warn|high"|null,
             "tasks": [..]?, "task": ".."?, "decision": "d-.."?} ],
  "digest": "<sha1 of everything but digest and generated_at>", "generated_at": 0 }
```

- **Tasks.** `done` and `dropped` tasks are left out. A task's owner is matched to a session by
  code or alias. `level` is used only when `resources.json` is enabled and under 5 minutes old (or
  at most 30 s ahead of the clock); otherwise no resource gate applies.
- **Inputs are untrusted.** Tasks, sessions and `resources.json` are normalised first: a task
  without a valid id is skipped, a field of the wrong type is unset, a list that is not a list is
  empty, and only finite numbers count. If the decision still fails, the file becomes
  `mode: "error"` with no queue and no advisories, rather than keeping old advice.
- **Gates, in order:** `paused`; `blocked` by `manual` (status blocked), `redline` (stage
  `production`: the scheduler never lets it run or start), `human` (a pending gate) or `task` (an
  unmet dependency); `deferred` by `session` (the owner is offline or unknown), `resource`, or
  `reset` (wait-reset mode: no new starts until `resets_at`; running work is left alone).
- **Modes** (first match): `wait-reset` when a usage-limit episode is active (`limit.json`; no longer
  once 10 minutes past its `resets_at`) or the
  5-hour window is at ≥ 95%; `conserve` when it is at ≥ `conserve.five_hour_pct` (85), the burn rate
  (`usage-samples.json`) reaches 100% more than `margin_minutes` (20) before the reset, or the weekly
  window is more than `weekly_pace_slack_pct` (15) points ahead of the elapsed week; `degraded` at
  critical resources; else `normal`. `usage.json` older than 15 minutes is ignored (a limit episode
  still counts). Conserve scores with 90 instead of 60 and −20 for `plan`/`design`, so nearly
  finished work wins.
- **One task per session.** A session's current task is the non-paused task it touched last
  (`max(updated_at, progress_at)`). While the session is `working` or `needs-you` and that task is
  not blocked, it is `run` and the session's other open tasks are `queued`. Otherwise (the session
  is `done`/`rest`, or its current task is blocked) its best open task is `ready` (listed in
  `start`) and the rest are `queued`.
- **Score** = `60 × stage weight (plan .15, design .25, build .45, test .70, deploy/production/maintain .85)
  × size weight (S 1, M .8, L .6)` + `15 × min(tasks it unblocks, 3)` + `10 × priority` +
  `2 × hours since touched` (at most 10) − 15 for a heavy task at `warn` or worse (conserve: see
  Modes). Ties: the
  longer wait, then the id.
- **Heavy slots.** `max_heavy` is `schedule.max_heavy_concurrent` (`auto`: 1 under 16 GiB of
  memory, else 2). `heavy_used` is the larger of the running heavy tasks and `resources.json`
  `heavy_running`. At `high` or `critical` there are no free slots. A free session whose best task
  is heavy asks for a slot; slots go first to sessions holding fewer heavy processes
  (`resources.json` `heavy_n`), then by score, then the longer wait. Without a slot the heavy task
  is `deferred{resource}`, the session's best light task becomes `ready`, and the session gets a
  `defer` advisory.
- **Stuck.** A `run` task of a `working` session with no progress for `stall_minutes` (45 / 90) is
  `stuck: warn|high`. Waiting on the owner (`needs-you`) is not stuck.
- **Advisories.** Ids hash `(kind, to, task, ref)`, so the same condition keeps its id. Texts name
  only task ids, validated branch names and process names; a text that would contain "approv",
  "deploy", "decision" or `[BOARD` falls back to a generic one that names nothing.
  A resource level is named by its cause, from `resources.json` `levels` (else `worst`), memory
  first, and the configured threshold it crossed (`resources.json` `thresholds`, the warn value at
  `warn`, else the high one; within a family the first in a fixed order (available %, available
  MB, PSI; disk: free %, free MB) that is past its threshold, counting the hysteresis band, so the
  named measure changes only when it leaves its level): "Memory is
  critical (available below 8%)", "Memory is critical (PSI above 25%)", "Memory is critical after
  an OOM kill", "Swap is critical (above 80% used)", "Disk space is critical (free below 3%)", "CPU
  load is critical (above 3.0 per CPU)", or "Resources are critical" when the cause is unknown.
  Never a live number: a value that hovers at critical does not change the text, the digest, the
  jams or the board push (the Resources view shows the numbers).
  - `defer` (warn): the heavy task a session would start next has no slot.
  - `resume` (info): a session has been `done`/`rest` for `idle_with_work_minutes` (30) with a ready task.
  - `finish-first` (warn, `one_pr_per_repo`): another task in the same `repo` has the open PR (the
    best-ranked open PR goes first); a `run`/`ready` task holds its push and PR.
  - `rebase-needed` (warn): a task whose `base` is the `branch` of a squash-merged task gets the
    `git rebase --onto origin/<base> <head_sha> <branch>` line (`<base>` stays a placeholder when
    the merged task has none), force-push only with
    `--force-with-lease` on its own branch, and the PR base to retarget.
  - `stop-own-heavy` (high, `stop_own_heavy`): at `critical` driven by memory (`mem`, which
    includes PSI, `swap`, or an OOM kill), the `stop_max` (2) sessions holding the largest heavy
    process by RSS (`resources.json` children) are asked to stop it if they or their teammate
    started it, and never one they did not start. The board never stops a process. `rss_mb` is
    floored to 256 MB, so a growing process does not change the decision. It never names a wrapper
    (above) or a process under 64 MB. Its id hashes the session, `via` (a `via` that is not a plain
    name is hashed, so two such teammates keep apart) and the class, not the pid.
    At `critical` from CPU
    load or disk alone (or an unknown cause) nobody is asked to stop anything: stopping short
    CPU-bound runs such as tests relieves nothing durable. New heavy starts stay deferred (`defer`
    names the cause) and J5 names it on the board.
  - `wait-for-reset` (warn): in wait-reset mode, a free session whose next task is held until the
    reset.
- **Delivery** (`delivery.state`). Only `pause`, `defer`, `finish-first`, `rebase-needed`,
  `wait-for-reset`, `free-resources` and `stop-own-heavy` are ever sent, and only to a code in
  `fleet.yaml` or a live session's code (`[A-Za-z0-9][A-Za-z0-9._-]{0,39}`); everything else is
  `board`. With `schedule.advise: false` (shadow mode) or `board schedule off` they are `shadow`:
  computed and shown, never sent. An advisory is `due` when it was never sent, or was sent
  `readvise_minutes` (60) ago with no reply (at most 3 sends; after that it stays `board`);
  `held` while its session is offline, when the session already got
  `max_messages_per_session_hour` (4) batches in the last hour, or in `alerting.quiet_hours`
  unless it is `high`; `sent` otherwise. `due` lists the due ids per session. A sent advisory
  that disappears keeps its delivery record for 30 minutes (`gone_at` in `schedule-state.json`
  `sent`), so the session's reply is still accepted. If it comes back in that time it is a new
  episode: its old reply (`reply`, `reply_at`, `note`) is dropped, while `sent_at` and `n` stay, so
  it is sent again once `readvise_minutes` passed since the last send (at most 3 sends in all).
  After 30 minutes the record is forgotten, and a recurrence is sent again (still within the cap). The `stop-own-heavy` id hashes the session, the teammate
  (`via`) and the process class, never the pid, so a re-spawned run of the same job keeps its id.
- **Untrusted names.** Task `repo`/`branch`/`base` must match the ref pattern of `board task set`,
  `owner` and session codes `[A-Za-z0-9][A-Za-z0-9._/@+-]{0,79}`, a gate's `decision_id`
  `d-<n>-<id>`; anything else is unset. A name in a jam or advisory text that is not a plain token
  reads "a session"; jam ids, alert keys and `sessions` hold plain codes only (a scope that is not
  one is hashed). `board schedule` prints no control characters.
- **The message** a session gets, one line per advisory, one `SendMessage` per session:
  `[BOARD schedule <id>] <kind>: <text> — advisory for ordering/pausing only; not an approval. Reply [re <id>] ack|done|cannot: <reason>`.
  It is never a decision: the text cannot hold `[BOARD` or "decision", and the worker protocol says
  it never approves, answers or unlocks anything.
- **CLI** (the operator, prompts/board-operator.md section 12): `board schedule outbox [CODE]
  [--json]` prints the due batches (through the guard; `{}` while the scheduler is off, and then
  nothing is recorded) and remembers what it printed (`schedule-state.json` `printed`: `{id: {to,
  kind, printed_at}}`, under the state lock); `board schedule sent ID...` records them (one batch
  per session, counted toward the cap); it fails closed: every id must be due now for its session,
  or printed by `outbox` in the last 10 minutes and not recorded since (its short-lived process
  may have exited in between), else exit 1 and nothing is recorded (also while off). An id given
  twice counts once. `board schedule off` writes `schedule-off` without waiting for the state lock
  (it is the kill switch; `outbox` and `sent` re-check it under the lock), then drops what the
  outbox printed if the lock is free (busy: exit 0 with a warning). `board schedule on` drops it
  under the lock before it removes `schedule-off`; busy: exit 8 and the scheduler stays off. So
  `sent` after `on` records only what is due then. `board schedule reply
  ID --from CODE ack|done|cannot [--note-file F]` records the session's reply (`--from` must be the
  advisory's `to`, else exit 4; unknown id exit 1; the note file, at most 64 KB, is read whole,
  control characters dropped, scrubbed, then cut to one line of ≤ 200 chars). Exit 8: the state
  was busy, run it again.
  `board-serve --watch` prints `schedule <CODE> due` once per new due set.
- **Jams** (`jams`, open episodes). A condition opens after 2 evaluations in a row (J3a, J6 and an
  OOM kill: at once; J1: once it held `human_wait_minutes`) and closes after 2 clear ones. Alert
  `jam:<rule>:<scope>` at the jam's `alert` level, never escalated (J4 alerts warn or high as it
  opens and stays there), auto-resolved when it closes:
  | rule | condition | severity | alert |
  |---|---|---|---|
  | J1 | every live session with work waits on the owner (a pending gate, directly or through a dependency) and has no other ready task | warn (info while away) | yes; `answer_first[0]` named |
  | J2 | a dependency cycle among live tasks | high | yes |
  | J3a | two live sessions on the same repo + branch (the collector's `repo`/`branch`, from each session's start cwd), not a default branch (`main master trunk develop`), detached or without commits | high | no: board-only until worktree facts land |
  | J3b | two open PRs in one repo (`one_pr_per_repo`) | warn | yes |
  | J4 | a stuck task (warn/high), or a session idle with a ready task (info) | warn/high, info | stuck only |
  | J5 | resources critical (2 samples), or an OOM kill (at once); the text names the cause | high | no (`res:*`) |
  | J6 | `operator:stale` is open | high | no (`operator:stale`) |
  | J7 | conserve or wait-reset with ≥ 2 sessions working; scope = the reset time | warn | yes |
  | J8 | a `rebase-needed` advisory sent over an hour ago, not `done` | info | no |
  Resolved episodes (the last 50, with `lost_min` = minutes × sessions) stay in
  `schedule-state.json` for lessons.
- `board schedule [--json]` prints the last decision (read-only, through the secret guard).
- TODO (PR 3, UI): escape PR URLs where they are rendered.

Settings (`fleet.yaml`):

```yaml
schedule:
  enabled: true
  max_heavy_concurrent: auto   # or a number
  one_pr_per_repo: true
  stall_minutes: [45, 90]
  idle_with_work_minutes: 30
  stop_own_heavy: true
  stop_max: 2
  advise: true                 # false = shadow mode: computed and shown, never sent
  readvise_minutes: 60
  max_messages_per_session_hour: 4
  human_wait_minutes: 10
  conserve: {five_hour_pct: 85, margin_minutes: 20, weekly_pace_slack_pct: 15}
```

## Lessons

`lib/lessons.py` keeps what the scheduler learned from jams in `$BOARD_HOME/lessons.jsonl` (0600,
append-only, written under `lessons.lock`). Each line is a full version of one lesson; the last
line per id wins. A delete appends a tombstone `{id, deleted: true, by, at, title, fingerprint}`;
nothing is rewritten, so the file is its own audit trail (plus `{type: "lesson", action, id, by}`
events). **Delete = forget**: the scheduler never learns that pattern (fingerprint) again.
**Retire = stop applying**: the lesson stays, and occurrences still merge into it.

```json
{ "id": "l-<epoch s>-<8 hex>", "schema_version": 1, "status": "candidate|active|retired",
  "source": "auto|owner", "rule_kind": "J1".."J8"|null, "fingerprint": "<16 hex>"|null,
  "title": "..", "signals": {}, "cause": ".."|null, "resolution": ".."|null,
  "occurrences": 2, "lost_min_total": 95, "first_seen": 0, "last_seen": 0,
  "rule": {"when": {"all": [{"metric": "mem_available_pct", "lt": 30}]},
           "then": {"action": "max_heavy:1", "reason": ".."?}}|null,
  "hits": 0, "last_hit_at": null, "retired_by": null, "updated_at": 0, "updated_by": "auto|<who>" }
```

- **Capture** (the collector, zero tokens): a jam episode that resolves after costing at least
  `lessons.min_lost_minutes` (10; minutes × sessions) merges into the lesson with the same
  fingerprint (rule + signals such as the J4 kind, an OOM or the usage mode; never session codes,
  repos or times), adding an occurrence and the minutes; otherwise it starts a `candidate` with a
  fixed title per rule. J3b and J5 lessons come with a default rule (`serialize_repo` while the repo
  has an open PR; `max_heavy:1` under 30% available memory); the others are text lessons until
  someone adds a rule. `board-serve --watch` prints `lesson <id> candidate` for an auto candidate
  with no note yet; the operator adds one (`board lesson note`).
- **Activation.** A candidate becomes `active` at `lessons.auto_activate_after` (2) occurrences,
  while fewer than 100 are active. An auto lesson unseen and unused for `ttl_days` (30) is retired
  (`retired_by: "ttl"`); owner lessons never expire. A lesson's rule that starts to match after it
  has been off for 30 minutes counts a hit (`hits`, `last_hit_at`), so a flapping metric does not
  grow the file. Episodes that could not be captured (the file was busy) wait in
  `schedule-state.json` (`to_learn`) for the next pass.
- **Rules** are declarative, from a fixed vocabulary, and **only tighten**:
  - `when.all`: 1–6 conditions `{metric, <op>: value}`, `op` in `lt lte gt gte eq`; metrics
    `mem_available_mb mem_available_pct swap_used_pct psi_mem load_per_cpu disk_free_pct
    heavy_running five_hour_pct five_hour_min_to_reset weekly_pct` (from `resources.json` and
    `usage.json`) and, per task, `open_prs_in_repo` and `stage` (`eq` only). A metric that is not
    known never matches.
  - `then.action`: `defer_heavy` (a heavy task gets no slot: `deferred{lesson}`), `max_heavy:N`
    (0–8; the lower of N and the scheduler's own limit), `pause_new_starts` (`deferred{lesson}`),
    `serialize_repo` (no new start in a repo where a task runs), `raise_severity` (warn advisories
    rank as high; `raised_from` keeps the original, which quiet hours still judge),
    `advise:<text>` (≤ 160 chars; a hint in `scheduler.json` `lessons.hints`, never sent).
  - The hint text and `then.reason` go through the same path as other text: control characters
    removed, screened by the secret guard (exit 6), scrubbed. A hint that mentions approvals,
    deploys, decisions or `[BOARD` is refused (exit 2): it orders work and never authorizes
    anything. The scheduler re-checks hand-edited lines the same way and skips a failing rule.
  - Anything else is refused with a message (exit 2 / 400 `bad_lesson`), for the owner's rules too:
    a lesson cannot loosen the schedule; to undo one, retire or delete it. A rule that fails
    validation when the scheduler reads it is skipped, never half-applied. `scheduler.json` lists
    the lessons that matched in `lessons.applied`.
- **CLI.** `board lesson ls [--all] [--status S] [--search T] [--json]`, `show ID`, `export
  [--generic]` (active lessons; `--generic` leaves out ids and signals; the text is as written);
  `add --by WHO --file F` and `edit ID --by WHO --file F` (JSON `{title, rule, cause, resolution}`),
  `activate|retire|delete ID --by WHO`: WHO must be in `approvers` (a `board:` prefix is allowed),
  else exit 4. `note ID --by WHO --file F` (`{cause, resolution}` only, auto lessons only; exit 2
  otherwise) is for the operator. Text is
  one line, capped (title 120, others 400), screened by the secret guard (a secret request: exit
  6, nothing written) and scrubbed. Exit 1: no such lesson; 8: the file is busy.
- **API** (`board-serve`): `GET /api/lessons[?q=TEXT]` → `{lessons, can_edit}` (the newest 100
  are also in `/api/state` as `lessons`); `POST /api/lessons` `{title, rule?, cause?, resolution?}`
  and `POST /api/lessons/<id>` `{action: "edit"|"activate"|"retire"|"delete", ...}` (delete needs
  `confirm: "delete"`; activate/retire/delete take no fields). Approvers only (403 `not_approver`
  / `no_approvers`), with the usual Host, CSRF and identity checks; the body reaches the CLI by a
  0600 file, never argv; audited as `{type: "serve", action: "lesson", op, lesson, rc}`. Errors: 400
  `bad_lesson` (with the validator's message in `detail`), 400 `held_text`, 400
  `confirm_required`, 404 `not_found`, 409 `busy`.
- **Board** (self-hosted page, section "Lessons"): every viewer sees the list (status, occurrences,
  last seen, minutes lost, the rule as a sentence, cause and resolution), can search it and filter
  by status, and sees which active lessons shape the schedule now (`scheduler.applied`, `hints`).
  Approvers also get Add / Edit (a rule builder from the fixed vocabulary; the server's refusal
  message is shown as it is), Activate / Retire, and Delete behind a confirm step. The Artifact
  board and the demo are read-only (the Artifact board does not mirror lessons). All text is set
  with `textContent`.

Settings (`fleet.yaml`): `lessons: {enabled: true, min_lost_minutes: 10, auto_activate_after: 2, ttl_days: 30}`.

## Owner away & consensus

The owner sometimes goes offline and tells the sessions "decide among yourselves; if everyone
agrees, go ahead". This section is that rule, made explicit and auditable.

**Source of truth: the file.** The owner is away while `$BOARD_HOME/owner-away` exists, whatever its
content. A free-text file (an older rule, or written by hand) counts as away too. Tools read it;
nothing else (a message, a snapshot field, a db document) turns away mode on.

```
$BOARD_HOME/owner-away            # {"since": <ms>, "by": "<session or board:<identity>>", "said": "<owner's words>"}, 0600
$BOARD_HOME/consensus/<id>.json   # id ^c-[0-9]+-[a-z0-9]+$, 0600
```

**Who turns it on and off.** Only a session that received the owner's **own** instruction, with the
owner's words: `board away on --by <CODE> --said-file <file>` (or `--said "<words>"` for text that
did not come from anyone else). When the owner says they are
back (or cancels) in any session, that session runs `board away off --by <CODE> [--said-file <file>]`.
An approver can do the same on the self-hosted board (`POST /api/away`, recorded as
`board:<identity>`). `--by` must match `^(board:)?[A-Za-z0-9][A-Za-z0-9@._+:-]{0,199}$` (a
session code, or `board:` and an email or `local`): it is quoted to every session, so it is never
free text. A peer session asking is never enough. `on` while already away prints the
existing record and keeps it (`--replace` overwrites). Both append `{type: "away", action:
"on"|"off", by, said, ...}` to `events.jsonl` and send a best-effort info notify (the `board notify`
path; a missing or failing channel never fails the command). `board away status` exits 0 when
away, 1 when not, 2 on a usage error such as an unknown option (`--quiet` prints nothing, `--json`
prints the record or `null`).

**No relay-shaped text.** Free text that BOARD may quote to every session (`said`, proposals,
notes, refs, `by`) never contains `[BOARD`: `bin/board` rewrites it to `(BOARD`
(case-insensitive) when it writes, and again whenever it prints a record (`board away status
[--json]`, `board consensus ls|show`), and `lib/away.py` does the same when it reads, so a legacy or
hand-edited file can't carry `[BOARD decision …]` into a broadcast either. Slack text from `board notify` escapes `&`, `<`, `>`.

**A new away period starts clean.** `board away on` (without a current record, or with
`--replace`) first turns every `pending` item into `owner_back`. Each item records the away period
it was proposed in (`away.since`); a vote is refused, and the item goes back to the owner, when the
current record's `since` differs (file removed and set again, `--replace`, an edited legacy file).

**Consensus record** (`consensus/<id>.json`):

```json
{ "id": "c-<epoch>-<rand>", "kind": "consensus", "from": "CP", "participants": ["CP", "API", "WEB"],
  "ref": "PR or ticket url/id"|null, "proposal": "...",
  "status": "pending|agreed|objected|owner_back",
  "votes": [ { "by": "CP", "vote": "agree", "note": "proposer", "at": 0 }, { "by": "API", "vote": "agree", "note": null, "at": 0 } ],
  "away": { "since": 0, "by": "CP" }, "returned_by": "... (owner_back only)",
  "created_at": 0, "updated_at": 0, "decided_at": 0|null }
```

Rules:

- `board consensus propose --from <CODE> --to A,B[,..] [--ref <PR/ticket>] "<proposal>"|--file <path>` prints
  the id. It is refused (exit 1) unless the owner is away. Participants are the proposer plus
  `--to` (deduped; at least one other session). **The proposer counts as agreeing.** It reminds of
  the red lines on stderr. The proposer sends the proposal to every participant itself.
- **Votes are self-attested.** `--from` is declared by the caller; the CLI does not verify who
  runs it. Each session votes (and proposes) **only as itself**; voting for another session breaks
  the rule. The one check: inside tmux (`$TMUX` set), a `--from` that is neither this tmux session
  (`tmux display-message -p '#S'`) nor an alias of the same fleet code is refused with exit 4 and
  logged. A human running `board consensus` from a personal tmux session gets that refusal too;
  running it outside tmux (or `TMUX= board consensus …`) is the deliberate way around it. Aliases
  come from `fleet.yaml`: without PyYAML they don't load, so a tmux session named after an alias
  is refused (it fails safe: use the code).
- `board consensus vote <id> agree|object --from <CODE> [--note T]`: only participants vote
  (otherwise exit 4, logged). Votes are accepted only while `pending`. One vote each: repeating the
  same vote is refused; **agree → object is allowed** (it parks the item); **object → agree never
  is**. Votes are appended, never rewritten, so the record shows every change.
- Status is the tally of each participant's latest vote: any `object` → **`objected`** (parked for
  the owner); every participant `agree` → **`agreed`**; otherwise `pending`. **Silence is not
  agreement**: there is no timeout that agrees. `objected` and `agreed` are final.
- `board away off` turns every `pending` item into **`owner_back`** (the question goes back to the
  owner). If the file was removed by hand, the next `board consensus vote|ls|show` does the same.
- An info notify goes out on `agreed`, `objected` and `owner_back`. Every change appends
  `{type: "consensus", action: "propose"|"vote"|"owner_back", id, by, ...}` to `events.jsonl`.
- **The record goes into the PR or ticket.** `board consensus show <id> --markdown` prints a block
  with the proposal, ref, participants, every vote with its UTC timestamp and the outcome. The
  proposer pastes it into the `--ref` PR/ticket once the item is final.
- `board consensus ls [--all]` lists pending items and those decided in the last 24 h (`--all`:
  every item).
- Writers (`away on|off`, `propose`, `vote`) hold an exclusive `flock` on
  `$BOARD_HOME/consensus.lock` (10 s timeout) and write atomically. Without `flock` they refuse to
  run. A `jq` failure never leaves a partial file: the new content is built first, then written.
- `board consensus show --markdown` writes `@` as `@` + zero-width space, so pasting it into a PR
  or ticket never mentions anyone.
- In the snapshot and on the page, a `pending` item is shown as `owner_back` while the owner is not
  away, or when it was proposed in another away period (`away.since` differs from the current
  record's). Display only; the next `board consensus` command writes it.

**Red lines: never by consensus.** These wait for the owner, whatever every session agrees:
- production deploys
- deleting real data
- irreversible operations
- touching shared production or third-party data
- publishing externally
- loosening permissions or security settings

**Consensus is not a human decision.** It never answers a board `ask`, a gate or a permission
prompt, and the operator never relays it as `[BOARD decision …]`. The page shows it read-only. In
Artifact mode the operator mirrors `owner_away` to `meta/away` and each item to `consensus/{id}`;
when the away state changes it broadcasts it to every live session and reports, per session, who
received and who acknowledged it (`prompts/board-operator.md` section 10).

## Session actions

A session action is one fixed slash command from a short allowlist. The board types it into an
idle session's empty prompt box. It never types free text.

| action | command | what it does |
|---|---|---|
| `compact` | `/compact` | summarizes the conversation to free context |
| `rc_on` | `/remote-control` | Remote Control on (the owner's choice: "Remote Control" below) |
| `rc_off` | `/remote-control` | Remote Control off, through Claude's dialog |

CLI: `board session CODE compact|rc on|rc off|cancel [--by NAME]`, `board session apply
[CODE...|--pending] [--only rc|compact] [--auto]`, `board session status [--json]`. Board:
`POST /api/sessions/<code>/action` (approvers only).

**Every action, the same guards** (in the style of `board press`). A check that fails before the
first key sends nothing:
1. The session must be idle: registry status `idle`, and the hook state, corrected as in the
   snapshot, `done` or `rest`. A `working` or `needs-you` session → `deferred` ("will run when
   idle"), nothing sent.
2. The process must still be the session: it is not a zombie, and the registry's `procStart`
   (Claude Code 2.1.x writes it) equals the start time in `/proc/<pid>/stat`. A registry without
   `procStart` gets only the zombie check: `startedAt` is wall-clock time and drifts against the
   boot clock after the host sleeps, so it is never compared. A reused pid → `offline`. The pane must be this session's: `display-message '#{session_name}\t#{pane_pid}'` must answer,
   name the registry's tmux session when the registry has one, and `#{pane_pid}` must be the
   session process or one of its ancestors (the `/proc/<pid>/stat` parent chain). This is checked
   whether or not the registry has `tmux`. The last `session_actions.lines` (30) lines of
   `capture-pane` must show Claude's input box: the last line that starts with `❯` has a rule
   (`rule_regex`) right above and right below it, and holds nothing else (`prompt_regex`). There
   must be no `esc to interrupt` anywhere, and below the box no dialog footer (`dialog_regex`)
   and no vim mode line (`mode_regex`, `-- NORMAL --`: Enter would not submit). Anything else →
   `refused`, nothing sent. A fresh session sometimes shows a dim placeholder in the box
   (`❯ Try "…"`). It counts as not empty and is refused; it is gone after the first prompt.
3. The registry and hook state are read again. Then `send-keys -l -- <command>`.
4. The box must now hold exactly the command, and the session must still be idle. Else `partial`
   (exit 5): the text is typed, Enter is not pressed, and the owner clears it.
5. Enter, then the action's own confirmation (below).

One action at a time per session: `actions-<code>.lock` (flock). The collector's applier skips a
session whose lock is held. A CLI call (and the board's background apply) waits
`lock_wait_seconds` (20); after that it only reports `busy`, "stored; will apply when the session
is free". The choice or the queued compact stays, and the collector applies it later. Sessions
never wait for each other.

Every attempt appends `{type: "session-action", op: "run", code, action, result, reason, by,
auto, pane}` to `events.jsonl`. Queueing, cancelling and dropping append `op: "queue"|"cancel"|
"drop"|"done"`. The last attempt per session and action is kept in `actions-last.json`:
`{CODE: {ACTION: {sid, result, reason, at, tries, choice_at, done_at}}}`.

Results: `confirmed`, `already`, `deferred`, `offline`, `nothing`, `started`, `dropped`, `busy` (exit 0),
`refused` (3), `partial` (5), `unconfirmed` / `unavailable` / `consent` (6), `failed` (1).

**compact.** These screens were observed in Claude Code 2.1.x:
- Typing `/compact` lists `/compact` and `/autocompact`; Enter runs `/compact`, with no dialog.
- While it runs, the registry status is `busy` and the pane shows `✻ Compacting conversation…
  (Ns · ↓ N tokens)` above an empty input box, with `esc to interrupt` in the footer.
- When it is done, the status is `idle` again. Right under the echoed `❯ /compact`, the pane shows
  `⎿  Compacted (ctrl+o to see full summary)`. The `sessionId` does not change.
- A conversation with nothing to compact prints `⎿  Not enough messages to compact.` at once.
- A tiny conversation took about 4 s; a large one takes longer.

The board confirms the start within `compact_start_seconds` (15; else `unconfirmed`). It then
waits up to `compact_seconds` (600) for `idle` plus the `Compacted` line → `confirmed`. Not enough
messages → `nothing`. Still running at the end → `started`, and it finishes in the session. Idle
again with neither the `Compacting` nor the `Compacted` line for `compact_idle_seconds` (5)
→ `unconfirmed` ("interrupted?", for example Esc pressed in the session).
`done_at` is the board's own record of the last confirmed compact; the hook does not see
compactions.

A compact on a busy session is queued in `session-actions.json` (`{CODE: {action, by, at,
sid}}`). It runs once the session is idle, and is dropped when it is older than `queue_hours` (12),
when it has no `sid` (an offline session is never queued: `offline`, nothing queued), or when the
session restarted (another `sid`). It is also dropped after a final result, or after
`max_tries` retries of `refused` / `failed` (nothing was submitted). Once Enter was pressed it is
never retried, `unconfirmed` included: a compact must not run twice. `board session CODE cancel` drops it.

**Pending work.** The looping collector (`board-collect`, not `--once`) passes `--apply-rc`. When
a session is idle with a pane, has something pending (a Remote Control choice it does not follow
yet, or a queued compact) and no action running on it, it starts `actions.py apply --auto
CODE...` in the background. For the same session process and action (and, for Remote Control,
the same choice), `--auto` never retries a final result (`confirmed`, `already`,
`nothing`, `started`, `unavailable`, `consent`, `partial`). It retries `refused`, `unconfirmed`
(Remote Control only) and `failed` after `retry_minutes` (10), at most `max_tries` (3) times. Turn it off with
`BOARD_ACTIONS_AUTO=0` or `session_actions.auto_apply: false`.

## Remote Control

Remote Control (RC) connects a session to claude.ai/code and the Claude app. The owner turns it
on or off per session, or for all sessions, from the board or with `board rc`.

**What a session wants**, first match wins:
1. its own choice (`board rc on|off CODE`, or `board session CODE rc on|off`),
2. the choice for all sessions (`board rc on|off all`, which also drops every per-session choice),
3. `fleet.yaml`: the session's `remote_control: true|false`, then a top-level `remote_control`,
4. on.

`$BOARD_HOME/rc-prefs.json` (0600, written atomically under `rc-prefs.lock`):
`{version: 1, all: {state, by, at}|null, sessions: {CODE: {state, by, at}}, updated_at,
updated_by}`. `board rc clear CODE|all` forgets a choice. Changes append `{type: "rc", action:
"set"|"clear", code, want, by}` to `events.jsonl`.

**At start.** `fleet-up` asks `lib/rc.py want CODE` and leaves `--remote-control CODE` out when
the answer is `off`.

**On a running session** it is the session action `rc_on` / `rc_off`. Claude Code (2.1.x) has no
flag for a running session; the `/remote-control` slash command (alias `/rc`) toggles it:
- **Off → on:** `/remote-control` + Enter. The first time on a machine, Claude shows a one-time
  consent dialog ("Enable Remote Control" / "Never mind"): the board presses Escape → `consent`.
  After that there is no dialog. The pane shows `/remote-control is active · Continue here, on
  your phone, or at https://claude.ai/code/session_…`, and the registry gets `bridgeSessionId`
  within about a second (`rc_confirm_seconds`, 10) → `confirmed`.
- **On → off:** `/remote-control` + Enter opens a "Remote Control" dialog with the rows "Disconnect
  this session", "Show QR code", "Continue" (focused) and the footer "Enter to select · Esc to
  continue". The board presses Up one row at a time, checking the focused row (`❯ …`) on screen
  after each key (at most 4 moves), until "Disconnect this session" is focused. Then it presses
  Enter: `⎿ Remote Control disconnected.`, and `bridgeSessionId` goes back to `null`. When the
  focus cannot be moved there, Escape closes the dialog ("Continue": Remote Control stays on) →
  `refused`. The disconnect label (`rc_disconnect_label`, anchored:
  `^\s*❯?\s*Disconnect this session\s*$`) is matched against the focused row only.
- **The registry is wrong.** If it says off while Claude is on, `/remote-control` opens the
  Disconnect dialog: the board presses Escape → `refused`. If it says on while Claude is off and
  never consented, the consent dialog opens: Escape → `consent`.
- **When the board presses Escape.** Only into one of Claude's Remote Control dialogs, recognized
  on screen: no input box anywhere, nothing running (`busy_regex`), a dialog footer
  (`dialog_regex`, "Enter to select|confirm") and above it the dialog's own rows, each a whole
  row of its own: "Disconnect this session", "Show QR code" and "Continue", or "Enable Remote
  Control" (`rc_consent_title_regex`) and "Never mind" (`rc_consent_regex`). Only the lines
  between the last echoed command and the footer count; when the echo has scrolled away, only the
  block right above the footer (up to 8 lines, to the first blank line). In those dialogs Escape is "Continue" / "Never
  mind": nothing changes. It never presses Escape into a running turn, text in the box, or any
  other picker (the user's own `/model`, say): the result is then `unconfirmed` and nothing else
  is sent. A consent dialog that does not look as expected is therefore left open, and the owner
  closes it. So is a dialog that opens only after `rc_confirm_seconds`: until someone presses Esc
  in the session, that dialog the board opened holds the pane, and the session's other prompts
  (peer and socket messages included) wait behind it; a later attempt finds no input box and
  sends nothing.
- **Scrollback never decides.** "Unknown command: /remote-control" counts only when it shows on
  more lines of the screen after Enter than right before it (a line already there, or moved up by
  the redraw, never counts; if an old one scrolls off as a new one appears, the result is
  `unconfirmed`, the safe side). The
  consent and Disconnect texts count only inside a recognized dialog. The same words higher up
  in the scrollback change nothing.
- **Not available:** with `DO_NOT_TRACK` set, not signed in to claude.ai, or turned off by
  policy, Claude ignores `--remote-control` and answers `Unknown command: /remote-control` →
  `unavailable`. When the session's process has `DO_NOT_TRACK` set, the board knows this up
  front (`rc.blocked`): it types nothing and the page shows "RC unavailable: DO_NOT_TRACK is set".

**The board choice and a change made by hand.** Making a choice (`board rc on|off`, the board's
toggle, `board session CODE rc on|off`) applies it at once to the idle sessions it covers. The
board then tries each running session at most once per (choice, session process), and only when
the choice is newer than the process: `rc.at` > the registry's `startedAt`. A session started
after the choice got it from fleet-up (or was started by hand, and is left as it is). After that
one try, its result kept in `actions-last.json` with `choice_at`, the board stops. A session that
already follows the choice when it is made (also with `--no-apply`, and for `all`) counts as tried
at once (`already`). From then on a change made
in the session by hand, for example someone disconnecting from the phone, wins until the owner
makes a choice again. A refused try is retried up to `max_tries`; then the board gives up as
well. `pending` (the dashed chip) is true only while the board still means to try. An explicit
action on the page or the CLI always forces the choice. `fleet.yaml` alone never makes the board
type into a pane.

**DO_NOT_TRACK.** In the environment that starts the sessions, `DO_NOT_TRACK` disables Remote
Control in all of them; `board doctor` and `fleet-up` warn about it. `rc.unset_do_not_track: true`
in `fleet.yaml` makes fleet-up always start sessions that want Remote Control with `env -u
DO_NOT_TRACK claude …`, whether or not fleet-up itself sees the variable (the pane's shell may
set it). `env` runs a program, so `CLAUDE_BIN` must then be an executable path or a name on
`PATH`, not a shell alias or function. The default is false: the environment is left as it is.

**Trade-offs.** Typing into a live session is the same risk as `board press`, bounded the same
way: only when idle, only into an empty box, Enter only after a second look, and a fixed command.
There is a window between the last screen check and Enter (the time to type a command and read
the screen once, well under a second): a human typing into the pane at that moment could have
their keystrokes mixed with the command. The board then sees an unexpected box and stops
(`partial`), or the command runs; it never submits text the human typed. A change made by hand
is respected after the board's one try (above); `board rc clear CODE` forgets the board choice
altogether. A session that cannot get Remote Control at runtime gets it at its next start
through fleet-up. The session code `all` is reserved (`fleet.yaml` entries named `all` are
skipped). board-serve checks who is asking before it looks at the request body, so a refused
probe is audited whatever it sent.

**The page.** Each running session card shows the Remote Control state: the card's "Open in Remote
Control" link when it is on (an `RC on` chip only when there is no link), an `RC off` or `RC
unavailable` (DO_NOT_TRACK) chip, or a dashed `RC → on|off` when a choice waits for the session to
be idle. The chip shrinks before it pushes the time under the link. In the
card's details, approvers get Turn on / Turn off and Compact (with a confirm step). There is also
a note: "Queued: runs when the session is idle" or "Last compacted …". The Notifications dialog
has "RC: all on / all off" with a confirm step. Everyone else sees the page read-only, and the
Artifact shows the chip only.
