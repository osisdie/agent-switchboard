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
  notify-prefs.json         # notification controls: mutes, min levels, presence policy (see "Notification controls")
  notify-prefs.lock         # flock held by `board mute|unmute|notify-level|notify-presence`
  presence.json             # board-serve: which approver has the board open and visible (presence heartbeats)
  presence.lock             # flock held by board-serve while it writes presence.json
  owner-away                # present = the owner is away (see "Owner away & consensus")
  consensus/<id>.json       # written by `board consensus` while the owner is away
  consensus.lock            # flock held by `board away on|off` and `board consensus propose|vote`
  resources.json            # system resources, sampled by the collector (see "Resources")
  resources-state.json      # the sampler's memory: previous counters, dwell state, teammate activity
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
  "decisions": [ { ...decision file... } ],
  "tasks": [ ... ], "limit": {...}|null,
  "owner_away": { "since": 0, "by": "CP", "said": "..." } | null,
  "resources": { "level": "ok|warn|high|critical", "worst": "mem|swap|disk:<id>|load|oom"|null } | null,
  "consensus": [ { "id": "c-...", "from": "CP", "participants": ["CP","API"], "ref": "...", "proposal": "...",
                   "status": "pending|agreed|objected|owner_back", "votes": [ {"by","vote","at","note"} ],
                   "created_at": 0, "updated_at": 0, "decided_at": 0|null } ] }
```

- **Where sessions come from:** live entries in `~/.claude/sessions/<pid>.json`, where `/proc/<pid>` exists. Each is merged with `state/<session_id>.json`.
- **`code`:** the tmux session name (the part before `:` in `tmux`). If there is no tmux, use the registered `name`. `fleet.yaml` may map aliases to codes.
- **One process, one card.** When several live processes resolve to the same code (teammate panes
  inside a session's tmux, a session started or renamed inside another session's tmux), the one
  whose registered name is the code or an alias and that started first keeps it. Each other one
  takes the fleet code its own registered name maps to, if no live process holds that code;
  otherwise it becomes `CODE/<name>`.
- **Sessions in `fleet.yaml` that aren't running** appear with state `offline`.
- **`owner_away`** is `null` when `$BOARD_HOME/owner-away` does not exist, else its record. A file
  that is not a JSON object (free text, empty) still means away: `{since: <mtime ms>, by: null,
  said: <first 300 chars>, legacy: true}`.
- **`resources`** is only the digest of `resources.json`: it changes when a level changes, never
  for raw numbers, so a Monitor on `snapshot.json` does not wake every sample. `null` when
  `resources.enabled: false`.
- **`consensus`** lists every `pending` item, plus `agreed` / `objected` / `owner_back` items for
  24 h after `decided_at`, oldest first. Text fields are clipped (proposal, ref, said: 300 chars;
  note: 200) and control characters become `?`.

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
| `meta/away` | operator | `{active: true, since, by, said, legacy?, updated_at}` while the owner is away, `{active: false, updated_at}` otherwise. The page shows the away banner only for `active: true`. |
| `consensus/{id}` | operator | a `snapshot.consensus` item as it is. Read-only on the page: no page write path, never relayed as a `[BOARD decision]`. |
| `meta/notify` | operator | the notification controls, read-only on the page: `board-notify status --json` → `.controls` without `presence.present`, plus `channels` (`{slack: true, ...}`, booleans only). See "Notification controls". |
| `meta/resources` | operator | `board resources --mirror`: the `/api/state` `resources` block with percentages bucketed to 5% and sizes to 100 MB. Written only when `snapshot.resources` changes, at most every 10 minutes. Read-only on the page. |
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
  owner-away, consensus/<id>.json    # see "Owner away & consensus"  (bin/board)
```

| endpoint | does |
|---|---|
| `GET /` | the board page (nonce CSP, `no-store`). Any query is ignored by the server and never reflected: `/?d=<id>` (alert links, see "Board links in alerts") serves the same page, which reads the query itself. |
| `GET /api/state` | the state below, built from `$BOARD_HOME` |
| `GET /api/me` | who is asking, from the verified identity and `fleet.yaml` only (never from request headers): see "Who am I" below. Same `Host` and identity checks as `/api/state` (no or bad JWT on the tunnel → 401, a forwarded request on the local listener → 403) |
| `GET /icon.svg`, `/favicon.png`, `/favicon.ico` (the PNG), `/apple-touch-icon.png` (+ `-precomposed`), `/icons/icon-192.png`, `/icons/icon-512.png`, `/manifest.webmanifest` | a fixed map to files under `artifact/` (nothing else there is served; the request path is never joined onto a directory), with `image/svg+xml`, `image/png` or `application/manifest+json`, `Cache-Control: private, max-age=86400` and `Content-Security-Policy: default-src 'none'`. Same Host and identity checks as every route, so through the tunnel they need Access too. The page itself gets `manifest-src 'self'` in its CSP |
| `GET /api/events` | `text/event-stream`: `event: state` with the full state at once and after every change (1 s mtime poll; only when the state differs), `: keepalive` every 25 s |
| `POST /api/decisions/<id>/answer` `{answer}` | `<id>` must match `^d-[0-9]+-[a-z0-9]+$`. With empty `approvers` → 403 `no_approvers` (fails closed). The identity is then checked **before anything is written**, with the rule of `board answer` (`approvers`; for a production decision, gate or stage, `release_managers`, falling back to `approvers`): not allowed → 403 `not_approver`, audited, file untouched (so it can never cancel an approver's answer that waits for the relay). Otherwise `board answer <id> <answer> --by <identity>` (argv, no shell; an answer such as `--help` is recorded literally) → 200 `{status: "answered"}`. For `kind: "permission"` (`answer` = `allow`\|`deny`; `allow` with `summary_truncated` → 409 `review_in_session`), it then runs `board press <from> <answer> --expect <tool_use_id>` → 200 `{status: "relayed"}`; exit 3 → 409 `stale`; exit 4 → 403 `press_refused`. A decision already `relayed`/`stale`/`timeout` → 409 `gone`. |
| `POST /api/messages` `{to, body}` | only for `approvers` ∪ `release_managers` (else 403 `not_approver`); writes `messages/<id>.json` (`^m-[0-9]+-[a-z0-9]+$`) with `status: "new"`, `by: <identity>` → 201 `{id}` |
| `POST /api/notify-prefs` `{op, ...}` | Notification controls. `op` is `mute` `{channel, for?\|until?, include_urgent?}`, `unmute` `{channel}`, `level` `{channel, level}` or `presence` `{reduce, level?}`. `channel` is `slack`, `desktop`, `email`, `line`, `sns`, `webhook` or `all`; `for` matches `^[1-9][0-9]{0,4}[mhd]$` (at most 30 days); `until` is `HH:MM` or an ISO time in the next 30 days (a string of at most 64 characters); `include_urgent` and `reduce` are booleans; `level` is `info\|warn\|high\|urgent\|default` (`presence`: `warn\|high\|urgent`). Anything else → 400 `bad_op` / `bad_channel` / `bad_duration` / `bad_level` / `bad_body`, nothing run. Empty `approvers` → 403 `no_approvers`; identity not in `approvers` → 403 `not_approver`, audited. Otherwise `board mute\|unmute\|notify-level\|notify-presence … --by board:<identity>` (argv, no shell) → 200 `{status: "ok", notify}` (the new `notify` state). Failure → 500 `prefs_failed`. |
| `POST /api/presence` `{visible, idle_ms?}` | The page's presence heartbeat. `visible` boolean; `idle_ms` integer ms since the viewer last clicked, tapped, scrolled or typed, at most 86400000 (a day), or null (else 400 `bad_body`). The server records `interaction_at = <server now> − max(0, idle_ms)`, so a page clock that runs fast changes nothing. Any time the page sends (`interaction_at` in the body) is ignored: without `idle_ms` the last interaction is unknown, so the viewer does not count as watching. Same approver rule as `/api/notify-prefs`. Recorded in `presence.json` under the listener's verified identity, never one from the body → 200 `{status: "ok", visible}`. Audited only when the identity's `visible` changes. |
| `POST /api/away` `{state: "on"\|"off", said?}` | `state` other than `on`/`off` → 400 `bad_state`; `said` not a string or over 500 chars → 400 `bad_said`. Empty `approvers` → 403 `no_approvers`; identity not in `approvers` → 403 `not_approver`, audited, nothing written. Otherwise `board away on\|off --by board:<identity> [--said <said>]` (argv, no shell; `on` without `said` records "away (set on the board)") → 200 `{away: true\|false, status: "on"\|"off"\|"unchanged"}`. `on` while already away keeps the existing record (`unchanged`). Failure → 500 `away_failed`. |

Every request: `Host` from the listener's allowlist, else 421. Per listener, at most 64
connections (more get a bare 503) and 32 SSE streams (503 `too_many_streams`); the socket times
out after 60 s. Error bodies are generic `{error, detail}`. Token reasons and `board` output are
logged to stderr only. Identities are matched against `approvers` / `release_managers` /
`allowed_emails` case-insensitively, and `--by` gets the configured spelling. POST: `X-Board: 1` and an `Origin`
from the listener's allowlist (`http://127.0.0.1:<port>`, `http://localhost:<port>` /
`https://<hostname>`; missing or `null` → 403 `csrf`), `Content-Type: application/json` (else
415), at most 16 KiB (else 413). `OPTIONS` and every other method → 405, and no
`Access-Control-*` header is ever sent. Every write appends `{type: "serve", action:
"answer"|"press"|"message"|"away"|"notify-prefs"|"presence", by, via: "loopback"|"cf-jwt", id, result}` to
`events.jsonl` (`presence` only when visibility changes).

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
  "ledger": {...}|null,
  "owner_away": {since, by, said, legacy?}|null,
  "consensus": [ snapshot.consensus items, newest 50 ],
  "notify":    { mutes, min_level, effective_min_level, presence: {reduce, min_level, window_seconds,
                 idle_seconds}, breakthrough, updated_at, updated_by, channels: {slack: bool, ...} } | null,
  "resources": { see "Resources": resources.json without pids, paths or unknown fields }|null }
```

The shape matches what the page reads from the Artifact db (`sessions/*`, `decisions/*`, `tasks/*`,
`approvals/*`, `messages/*` + replies, `meta/operator`, `meta/ledger`, `meta/resources`). `task` is the title of the
session's current task. `approver_ids` is `[]` when nobody may answer.

### Who am I (`GET /api/me`) and API keys

```json
{ "identity": "local|<email>", "via": "local|access",
  "roles": ["approver", "release_manager"] | ["viewer"],
  "is_approver": true, "is_release_manager": false,
  "api_keys": [ ... ] }
```

- `via` is `local` for the local listener and `access` for the tunnel (a verified Cloudflare Access
  JWT). The audit log keeps its own names (`loopback` / `cf-jwt`).
- `is_approver`: the identity is in `approvers`. `is_release_manager` is the effective rule for a
  production decision: in `release_managers`, or in `approvers` when `release_managers` is empty.
  `roles` lists what is true, or `["viewer"]` when neither is. All matches are case-insensitive.
- `api_keys` is **reserved** for the key backend (a later change) and absent until it exists. The page
  shows "API keys are not enabled on this board" without it. Its shape, one entry per key issued to
  this identity (an owner email, or `local`):

  ```json
  { "id": "key_…", "name": "Phone shortcut", "scopes": ["state:read"],
    "created_at": 0, "expires_at": 0|null, "last_used_at": 0|null,
    "status": "active|revoked|expired" }
  ```

  Times are epoch ms; `null` means no expiry / never used. `scopes` are opaque strings defined by
  the key backend. **The key secret is never returned** by this or any later read: it is shown once,
  when the key is created. Create and revoke endpoints come with the backend.

The page's profile menu (header, top right) reads this route in local mode. In Artifact mode it
uses the `user` capability instead (`me()`: `canEdit`, `isOwner`; the name stays empty because the
page does not declare the `profile` scope) and takes roles from `meta/operator.approver_ids` /
`release_manager_ids`. Sign-out links to `/cdn-cgi/access/logout` and is shown only when
`via` is `access`.

**Operator in local mode** (`prompts/board-operator.md` section 9): `board-serve --watch` prints
`decision <id> answered`, `message <id> new`, `away on|off` and `consensus <id> <status>` (once
each, oldest first) and `tick` every 8 minutes.
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
- The collector crosses thresholds 80/90/95 (configurable) per window and raises the levels above. Message examples:
  - "5h usage 90% — resets ~14:20 — prepare to switch account"
  - "weekly usage 95% — resets Mon 09:00 — switch account now"
- The operator mirrors usage into `meta/operator.usage`. The page shows two meters (5h and 7d) with reset countdowns. At ≥90% it shows an amber "Prepare to switch account" banner, and at ≥95% or on limit-hit a red one, with a link to the failover section in Info.

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
  "unavailable": ["psi", "swap", "disk"], "interval": 15, "config_errors": [] }
```

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
- **Classes** match `comm` plus the base names of the first argv words (a shell is never a test):
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
owner's words: `board away on --by <CODE> --said "<owner's words>"`. When the owner says they are
back (or cancels) in any session, that session runs `board away off --by <CODE> [--said "..."]`.
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

- `board consensus propose --from <CODE> --to A,B[,..] [--ref <PR/ticket>] "<proposal>"` prints
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
