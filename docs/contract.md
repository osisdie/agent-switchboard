# agent-switchboard: data contract (v1)

This repo is generic. Do not write any machine-, company-, product- or person-specific names, paths or URLs into tracked files. Machine-specific config lives in `tmp-local/`, which is gitignored.

## Local state (`$BOARD_HOME`, default `~/.agent-switchboard`)

```
$BOARD_HOME/
  state/<session_id>.json   # written by hooks/board-hook.sh, one per Claude Code session
  events.jsonl              # append-only audit log (hook events, presses, relays)
  decisions/<id>.json       # written by `board ask` (kind=question) and by the hook (kind=permission)
  outbox/                   # reserved
  snapshot.json             # written by bin/board-collect (merged view, see below)
  alerts.json               # board-notify alert state {key: {level, first_at, last_at, acked_at}}; dedupe/repeat per
                            # key and level follow the alerting policy (alerting.yaml / fleet.yaml `alerting:`)
```

### state/<session_id>.json
```json
{ "session_id": "...", "pid": 123, "cwd": "...", "tmux_pane": "%7",
  "state": "working|needs-you|done|rest|offline",
  "last_event": "Stop", "notification_type": "permission_prompt",
  "pending": { "tool_use_id": "...", "tool_name": "Bash", "summary": "first 200 chars of input (redacted)",
               "summary_truncated": false, "summary_len": 57, "summary_sha256": "<hex of the full input>",
               "ambiguous": false, "decision_id": "d-..." } | null,
  "updated_at": 1731500000000 }
```

Map hook events to states like this:

| Hook event | State |
|---|---|
| `UserPromptSubmit`, `PreToolUse`, `PostToolUse` | working |
| `PermissionRequest`, Notification `permission_prompt` | needs-you, with `pending` set |
| Notification `idle_prompt`, `Stop` | done |
| `SessionEnd` | offline |
| `SessionStart` | rest |

`pending` is cleared on the next `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit` or `Stop`. The hook writes the state file atomically (write to a temp file, then `mv`). It never blocks Claude, prints nothing to stdout and always exits 0. It does not record prompt text. The only input it captures is a ≤200-char summary of the pending tool's input. Token-like values (bearer tokens, Slack/GitHub/AWS keys, `password=`/`token=`/`secret=`/`api_key=` values, long base64/hex runs) are replaced by `[REDACTED]` before storing. `summary_truncated`, `summary_len` and `summary_sha256` describe the full input string. When a `PermissionRequest` carries no `tool_use_id` and more than one in-flight tool use could match it, the hook does not guess: `pending.ambiguous` is `true` and no permission decision is created. `$BOARD_HOME` is mode 0700 and its files 0600 (umask 077 in every writer).

### snapshot.json (collector output)
```json
{ "generated_at": 1731500000000,
  "sessions": [ { "code": "CP", "aliases": ["CP"], "name": "CP", "pid": 1, "session_id": "...",
                  "cwd": "...", "tmux": "CP:@3.%7", "tmux_pane": "%7",
                  "status": "busy|idle", "state": "working|needs-you|done|rest|offline",
                  "pending": {...}|null, "remote_url": "https://claude.ai/code/<bridgeSessionId>"|null,
                  "updated_at": 0 } ],
  "decisions": [ { ...decision file... } ] }
```

- **Where sessions come from:** live entries in `~/.claude/sessions/<pid>.json`, where `/proc/<pid>` exists. Each is merged with `state/<session_id>.json`.
- **`code`:** the tmux session name (the part before `:` in `tmux`). If there is no tmux, use the registered `name`. `fleet.yaml` may map aliases to codes.
- **Sessions in `fleet.yaml` that aren't running** appear with state `offline`.

### decisions/<id>.json
```json
{ "id": "d-<epoch>-<rand>", "kind": "question|permission", "from": "CP",
  "question": "...", "options": ["A","B"], "context": "optional short text",
  "tool_use_id": "... (permission only)", "tool_name": "...", "summary": "...",
  "summary_truncated": false, "summary_len": 0, "summary_sha256": "... (permission only)",
  "status": "pending|answered|relayed|stale|timeout|unauthorized",
  "answer": null, "answered_by": null, "answered_at": null, "relayed_at": null,
  "created_at": 0 }
```

### Permission decisions: who may press

- `board press` sends keys only after `board answer <id> allow|deny --by <id>` recorded an
  approver's answer with that same action (exit 4 otherwise), and only while the hook state still
  shows that `tool_use_id` (and `summary_sha256`) pending, the pane shows a permission prompt, and
  the start of the summary is visible in the pane (exit 3 otherwise, decision `stale`).
- **No approvers configured fails closed** for every decision, not only production. A decision is a
  production decision when `gate` or `stage`, trimmed and lowercased, is `"production"`.
- **Truncated summaries:** when `summary_truncated` is true the human cannot see the whole input on
  the board. The page must **not offer one-click allow** for such a card (show "answer in the
  session's pane" instead); `board press` refuses unless `--allow-truncated` is passed by a human
  who read the full input in the pane.

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
| `sessions/{code}` | operator | `code, aliases[], state, status, task` (short summary, never raw prompt), `tmux, remote_url, pending{tool_name,summary,tool_use_id}, updated_at` |
| `decisions/{id}` | operator | fields as in `decisions/<id>.json` above. Only the operator writes `answer, answered_by` (= the path id of the answer it relayed), `answered_at`, `status`, `relayed_at`. An Editor's page may set `status: "answered"` as a display hint. The operator ignores it, and ignores who set it. |
| `answers/{userId}/d/{decisionId}` | that user (page) | `{answer, answered_at, note}`. For permission cards, `answer` is `"allow"` or `"deny"`; the page never offers `"allow"` when the decision has `summary_truncated: true`. The operator lists `answers/<id>/d` for each id in `approver_ids` ∪ `release_manager_ids`, takes the identity **from the path**, and runs `board answer <decisionId> <answer> --by <id>`. If several approvers answered, the earliest `answered_at` wins. |
| `messages/{id}` | page (Editors) creates; operator updates | `to` (code or `"all"`), `body`, `by` (user id), `created_at`, `status: "new"\|"relayed"\|"replied"`. The operator adds `relayed_to[]` and `relayed_at`. |
| `messages/{id}/replies/{rid}` | operator | `from` (code), `body`, `created_at` |
| `meta/operator` | operator | `heartbeat_at, version, owner_id`. The page shows "operator offline" if the heartbeat is older than 10 minutes. |
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
the emails in `approvers` ∪ `release_managers`), or every route, `/api/state` included, answers
403 `not_allowed`. Listing a viewer there lets them read the board; only approvers decide.

The cloudflared ingress points at the **tunnel** listener only. The tunnel listener refuses to start
(exit 2, with the reason) without `hostname`, `team_domain`, `aud` and the python `cryptography`
module. There is no unverified-header mode.

Extra files under `$BOARD_HOME`:

```
$BOARD_HOME/
  messages/<id>.json                 # {id, to: CODE|"all", body, by, created_at, status: new|relayed|replied,
                                     #  relayed_to[]?, relayed_at?}  (board-serve creates; operator updates)
  messages/<id>/replies/<rid>.json   # {id, from: CODE, body, created_at}  (operator)
  operator.json                      # {heartbeat_at, version, operator_session, account_hint}  (operator)
  ledger.json                        # optional, same shape as meta/ledger  (operator)
```

| endpoint | does |
|---|---|
| `GET /` | the board page (nonce CSP, `no-store`) |
| `GET /api/state` | the state below, built from `$BOARD_HOME` |
| `GET /api/events` | `text/event-stream`: `event: state` with the full state at once and after every change (1 s mtime poll; only when the state differs), `: keepalive` every 25 s |
| `POST /api/decisions/<id>/answer` `{answer}` | `<id>` must match `^d-[0-9]+-[a-z0-9]+$`. With empty `approvers` → 403 `no_approvers` (fails closed). The identity is then checked **before anything is written**, with the rule of `board answer` (`approvers`; for a production decision, gate or stage, `release_managers`, falling back to `approvers`): not allowed → 403 `not_approver`, audited, file untouched (so it can never cancel an approver's answer that waits for the relay). Otherwise `board answer <id> <answer> --by <identity>` (argv, no shell; an answer such as `--help` is recorded literally) → 200 `{status: "answered"}`. For `kind: "permission"` (`answer` = `allow`\|`deny`; `allow` with `summary_truncated` → 409 `review_in_session`), it then runs `board press <from> <answer> --expect <tool_use_id>` → 200 `{status: "relayed"}`; exit 3 → 409 `stale`; exit 4 → 403 `press_refused`. A decision already `relayed`/`stale`/`timeout` → 409 `gone`. |
| `POST /api/messages` `{to, body}` | only for `approvers` ∪ `release_managers` (else 403 `not_approver`); writes `messages/<id>.json` (`^m-[0-9]+-[a-z0-9]+$`) with `status: "new"`, `by: <identity>` → 201 `{id}` |

Every request: `Host` from the listener's allowlist, else 421. POST: `X-Board: 1` and an `Origin`
from the listener's allowlist (`http://127.0.0.1:<port>`, `http://localhost:<port>` /
`https://<hostname>`; missing or `null` → 403 `csrf`), `Content-Type: application/json` (else
415), at most 16 KiB (else 413). `OPTIONS` and every other method → 405, and no
`Access-Control-*` header is ever sent. Every write appends `{type: "serve", action:
"answer"|"press"|"message", by, via: "loopback"|"cf-jwt", id, result}` to `events.jsonl`.

State (`/api/state` and every SSE frame):

```json
{ "mode": "local", "me": "local|<email>", "writable": true, "can_message": true|false, "generated_at": 0,
  "sessions":  [ {code, aliases, state, status, task, tmux, remote_url, pending, updated_at} ],
  "decisions": [ decision files, newest 200, without session_id/tmux_pane ],
  "tasks":     [ task files, newest 50 ],
  "approvals": [ events.jsonl type:"approval" rows (+ relayed_at from the decision), newest 100 ],
  "messages":  [ newest 20 ], "replies": { "<msgId>": [ ... ] },
  "operator":  { heartbeat_at, version, operator_session, account_hint, approver_ids,
                 release_manager_ids, usage: {five_hour, seven_day, updated_at}, limit|null },
  "ledger": {...}|null }
```

The shape matches what the page reads from the Artifact db (`sessions/*`, `decisions/*`, `tasks/*`,
`approvals/*`, `messages/*` + replies, `meta/operator`, `meta/ledger`). `task` is the title of the
session's current task. `approver_ids` is `[]` when nobody may answer.

**Operator in local mode** (`prompts/board-operator.md` section 9): `board-serve --watch` prints
`decision <id> answered`, `message <id> new` (once each, oldest first) and `tick` every 8 minutes.
The operator relays questions and gates (then `board mark <id> relayed`) and messages, writes
replies, and keeps `operator.json.heartbeat_at` fresh. Permission decisions are already pressed by
`board-serve`.

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
  `local` (a browser on this machine), which counts only when it is listed. An id from one
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
- **Worker reply:** `[re <msgId>] <text>`.

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
- **State:** kept in `$BOARD_HOME/alerts.json` as `{key: {level, first_at, last_at, acked_at}}`. The key is `decision:<id>`, `session:<code>:needs-you` or `usage:<window>:<threshold>`.
- **Quiet hours:** `alerting.quiet_hours: "23:00-08:00"`. Only `urgent` passes during them.
- **Dedupe:** at most one send per key per level. A re-alert at the same level happens only after `alerting.repeat_minutes` (default 60).

## Usage meter (approaching 5h / weekly limits)

Claude Code's statusline input JSON carries `rate_limits.five_hour` and `rate_limits.seven_day`, each `{used_percentage, resets_at}`.

- `hooks/board-statusline-tap.sh` wraps the user's existing statusline command (`BOARD_STATUSLINE_NEXT`). It writes `$BOARD_HOME/usage.json` as `{five_hour:{pct,resets_at}, seven_day:{pct,resets_at}, updated_at, session_id}` atomically, then execs the original command with the same stdin. It never changes the statusline output.
- The collector crosses thresholds 80/90/95 (configurable) per window and raises the levels above. Message examples:
  - "5h usage 90% — resets ~14:20 — prepare to switch account"
  - "weekly usage 95% — resets Mon 09:00 — switch account now"
- The operator mirrors usage into `meta/operator.usage`. The page shows two meters (5h and 7d) with reset countdowns. At ≥90% it shows an amber "Prepare to switch account" banner, and at ≥95% or on limit-hit a red one, with a link to the failover section in Info.
