# agent-switchboard: tickets on the board + open API / MCP / developer portal (design)

Status: agreed, with the decisions in "Decisions" below. T1 (tickets core), T3 (writes, take,
relay) and T4 (the board panel) are implemented; see the implementation notes at the end for where they differ from this
text. The binding formats
are in `docs/contract.md`.

## 1. Data model and files

Everything under `$BOARD_HOME`, directories 0700, files 0600, atomic writes.

```
tickets/cache/<prov>__<key>.json   {provider,key,id,title,status,status_category:todo|doing|done|other,
                                    assignee{id,name}|null,excerpt(≤500 plain),url,updated_at,fetched_at,etag?}
                                    TTL 60 s; purged after 7 d unless linked
tickets/links/<prov>__<key>.json   {provider,key,task_id,session,taken_by,taken_at,status:active|released|done,
                                    msg_id,upstream:[{op,result,ref}]}
tasks/<id>.json                    + "ticket": {provider,key,url,title_at_take}|null   (board task new --ticket prov:key)
apikeys/<kid>.json                 {id,name,kind:live,scopes[],hash,created_by,created_at,expires_at,
                                    revoked_at|null,last_used_at,rate_per_min}
secrets/apikey.pepper              32 random bytes, 0600, made on the first `board apikey create`
messages/<id>.json                 + kind:"ticket"|"api" (new optional field), via:"board"|"apikey:<name>"
events.jsonl                       new types: ticket {action:get|comment|transition|assign|take|release,
                                    provider,key,by,via,result,task_id?,body_sha256},
                                    apikey {action:create|revoke|auth_fail}, api {action,key_id,route,result}.
                                    Never a body, a token or a secret.
```

Config lives in the config dir (`tmp-local/` or `$BOARD_CONFIG_DIR`); secrets only in its `.env`.
Mock home: `$BOARD_MOCK_HOME` (default `~/.agent-switchboard-mock`), seeded from `tests/fixtures/mock/`.

## 2. Provider interface and config

`lib/tickets/`:

- `Provider`: `name`, `key_re`, `capabilities` ⊆ {read, comment, transition, assign};
  `resolve(key) -> ctx`, `get(key) -> Ticket`, `comment(key, text) -> ref`, `transitions(key) -> [str]`,
  `transition(key, to) -> Ticket`, `assign(key, who) -> Ticket`.
- Built-ins: `jira`, `http-json`, `fake` (fixtures; the only one mock mode loads).

**Routing.** `prov:KEY` explicit wins; otherwise every provider whose `key_pattern` matches. One
match → it; none → 400 `unknown_ticket_ref`; several → 409 `ticket_ambiguous {candidates}`, and the
page asks which. This matters: Jira keys and another tracker's ids can both be `PREFIX-N`, so
`key_pattern` should list project prefixes, not `[A-Z]+-\d+`. A numeric id matches only providers
whose pattern accepts digits.

**HTTP client** (stdlib urllib): HTTPS only (http only for 127.0.0.1/localhost); the host is pinned
to `base_url`; templates fill only path segments (percent-encoded) and query/JSON values (typed
substitution into the parsed body template, never string-built JSON); redirects not followed;
10 s timeout; body ≤ 1 MiB.

**Rate limits.** A token bucket per provider (`rate_per_min`, default 30), 429 `Retry-After`
honoured, a circuit breaker (5 failures → 60 s open). Errors are generic `{error:
ticket_not_found|ticket_auth|ticket_rate_limited|ticket_upstream|ticket_unconfigured, detail}`;
upstream bodies are clipped, run through the redaction, also redacted by the literal token values,
and sent to stderr only.

**Caching.** Reads use the cache within the TTL; a 404 is cached 30 s; writes and take always
refetch and invalidate.

**Secrets.** Config names env vars only; values come from the process env, then
`$BOARD_CONFIG_DIR/.env`. `board ticket doctor` prints set/unset only.

Jira example:

```yaml
- name: jira
  type: jira
  flavor: cloud              # cloud: /rest/api/3, Basic email:token, ADF; server: /rest/api/2, Bearer PAT, assignee {name}
  base_url: https://example.atlassian.net
  auth: {email_env: JIRA_EMAIL, token_env: JIRA_API_TOKEN}     # server: {token_env: JIRA_PAT}
  key_pattern: '^(PROJ|OPS)-[0-9]+$'
  rate_per_min: 60
  take: {transition: "In Progress", assign: self, comment: "Picked up as {task_id} ({session})."}
```

Calls: `GET /issue/{key}?fields=summary,status,assignee,description,updated` (description ADF →
text); `POST /issue/{key}/comment` (ADF paragraph); `GET`+`POST /issue/{key}/transitions
{transition:{id}}` (resolved by name); `PUT /issue/{key}/assignee {accountId}` (self = `GET /myself`).

Generic `http-json` example:

```yaml
- name: tracker
  type: http-json
  base_url: https://tracker.example.com
  auth: {header: Authorization, scheme: Bearer, env: EXAMPLE_TRACKER_TOKEN}
  key_pattern: '^(WEB|API)-[0-9]+$'
  resolve:                                  # optional: display key → internal id
    {method: GET, path: /api/v1/issues/resolve, query: {displayId: "{key}"},
     set: {id: "$.data.id", project: "$.data.project"}}
  get:
    {method: GET, path: "/api/v1/projects/{project}/issues/{id}",
     map: {title: $.data.title, status: $.data.status, assignee: $.data.assigneeName,
           description: $.data.description, updated_at: $.data.updatedAt},
     link: "https://tracker.example.com/projects/{project}/issues/{key}",
     status_category: {todo: [backlog, todo], doing: [in_progress], done: [done, cancelled]}}
  comment:    {method: POST,  path: "/api/v1/projects/{project}/issues/{id}/comments", body: {body: "{text}"}}
  transition: {method: PATCH, path: "/api/v1/projects/{project}/issues/{id}", body: {status: "{to}"},
               states: [backlog, todo, in_progress, done, cancelled]}
  assign:     {method: PATCH, path: "/api/v1/projects/{project}/issues/{id}", body: {assigneeName: "{who}"}}
```

Path language (tiny, no eval): `$ . name [n] [?field=={var}]`, e.g. `"$.data[?displayId=={key}].id"`
for a search-based resolve.

**Permissions** (board identities): get = approvers ∪ release_managers (decision Q3);
comment/transition/assign/take/release = approvers only; empty approvers → 403, failing closed like
answers. The identity is checked before any upstream call, and refusals are audited. The tracker
token acts as its owner, so every comment carries the configurable footer "(via agent-switchboard)".

**Lifecycle.** `POST /api/tickets/take {ref, session, comment?, transition?, assign?}` (approver):

1. fresh fetch;
2. an active link exists → 409 `already_taken {task_id}`;
3. `board task new --owner CODE --ticket prov:key "KEY: title"`;
4. write the link file;
5. write `messages/<id>.json` with `kind: "ticket"`;
6. optional upstream writes, each audited; a failure → 200 `{status: "partial", failed: [...]}` with
   no rollback (the task is the source of truth).

The operator's `--watch` prints `ticket <key> taken` and relays:

```
[BOARD ticket <KEY>] taken for <CODE> as task <t-id> by <identity> on board: <title> <url>. Authorizes: <see Q4>. Reply to BOARD with [re <msgId>].
```

Release or reassign → `[BOARD ticket <KEY>] released|reassigned …`. Once the task reaches maintain
or done, the link becomes done. board-serve refreshes linked tickets every 5 min while a page is
open; when the upstream ticket is closed but the task is still active, the page shows a drift badge.

**Authority of `[BOARD ticket]`**: the same trust path as `[BOARD decision]`. Only the exact format,
only from BOARD, and only when the local file confirms it: the worker checks with
`board ticket show <KEY>` that the link is active, names this session, and was taken by an
approver. Any other sender, or a session that forwards it, is information only. What it authorizes
is decided in Q4 below. Consensus can never take a ticket, and neither can an API key (section 3).
The contract, `snippets/worker-protocol.md` and `prompts/board-operator.md` get these rules.
Artifact mode is out of scope in v1, because the page can't call trackers.

## 3. API surface and MCP

All routes are `/api/v1`, JSON, bearer `Authorization: Bearer asb_…`. API keys are the only
identity there: `local` and the Access email never count on `/api/v1`.

| route | scope |
|---|---|
| `GET /whoami` → `{key_id, name, scopes, mode: live\|mock}` | any |
| `GET /state`, `/events` (SSE) | `read:state` |
| `GET /sessions[/code]` | `read:state` |
| `GET /decisions[?status]`, `/decisions/{id}` | `read:decisions` |
| `GET /tasks[/id]` | `read:tasks` |
| `POST /tasks`, `/tasks/{id}/stage` | `tasks:write` |
| `POST /messages` → relayed as `[BOARD api <msgId> from <keyname>]`, never `[BOARD ask]` | `write:messages` |
| `GET /messages/{id}` (+ replies) | `read:messages` |
| `GET /tickets/{ref}` | `tickets:read` |
| `POST /tickets/{ref}/comments\|transition\|assign` | `tickets:write` |
| `POST /tickets/{ref}/take` → a PENDING board decision ("Start KEY in CODE?" approve/decline); the take runs only after an approver answers | `tickets:take` |
| `GET /openapi.json` | none beyond reaching `/dev` |

No scope ever exists for away on/off, pressing or permission prompts, consensus, API key admin, or
production gates. `decisions:answer` is not issued (Q1). Rule: an API key never carries owner
authority; anything that would becomes a pending decision for an approver.

POST rules: `Content-Type: application/json`, ≤ 16 KiB, no `Access-Control-*` headers (cross-origin
bearer calls fail preflight), the Host allowlist as today.

Single source: `lib/api_spec.py` holds a route table `{method, path, scope, handler, schema}`. The
router dispatches from it and `/openapi.json` is generated from it; a test asserts they are equal.

MCP: `bin/board-mcp` (stdlib, JSON-RPC, stdio) is a thin client of `/api/v1` (`BOARD_API_URL`,
`BOARD_API_KEY` from env, required even locally, because any worker session can start an MCP
server). Streamable HTTP: `POST /mcp` on board-serve with the same bearer key. One enforcement and
audit path. `tools/list` is filtered by the key's scopes.

- Read-only (`readOnlyHint`): board_state, list_sessions, list_decisions, get_decision, list_tasks,
  get_task, get_message, ticket_get.
- Write: create_task, set_task_stage (`tasks:write`); send_message (`write:messages`);
  ticket_comment, ticket_transition, ticket_assign (`tickets:write`, `destructiveHint` for
  transition); ticket_take_request (`tickets:take`, returns the decision id).
- Resources: `board://state`, `board://decisions/{id}`, `board://tasks/{id}`, `board://tickets/{ref}`,
  `board://openapi`.

## 4. API key security

- Format: `asb_live_<kid 12 [a-z0-9]>_<secret 43 base64url = 256 bit>`; `asb_test_` for mock.
- Store: `hash = HMAC-SHA256(pepper, full key)`; lookup by kid; `hmac.compare_digest`. A per-key
  salt adds nothing for 256-bit random secrets, while the pepper makes a stolen `apikeys/` directory
  useless.
- Shown once on stdout or in the UI modal, never logged.
- CLI: `board apikey create --name N --scopes a,b --expires 90d|YYYY-MM-DD [--rate 60] [--test]`
  (default 90 d, max 365 d, no "never", unknown scope → exit 2); `board apikey ls [--json]` (id,
  name, scopes, created, expires, last used, status); `board apikey revoke ID` (immediate; the
  server reads `apikeys/` with an mtime cache); rotate = create + revoke.
- `last_used` is kept in memory and flushed at most once a minute.
- Rate limit: a token bucket per key (default 60/min, burst 20), 429 + `Retry-After`. Failed auth
  is limited per source (tunnel: `Cf-Connecting-IP`; local: per listener) to 10/min and audited by
  kid only.
- Redaction: `asb_(live|test)_…` in the hook's token redaction and in notify's redaction.
- `.gitleaks.toml` rule:

```toml
[[rules]]
id = "agent-switchboard-api-key"
description = "agent-switchboard API key"
regex = '''asb_(live|test)_[0-9a-z]{12}_[A-Za-z0-9_-]{43}'''
keywords = ["asb_live_", "asb_test_"]
```

The only fixture key, the mock demo key, lives in `tests/fixtures/` (already allowlisted).

## 5. Exposure

- **C. Local only (default).** `/api/v1` and `/mcp` on the local listener only;
  `serve.api.tunnel: off`. MCP over stdio. Enough for local services and sessions.
- **A. Same hostname, Cloudflare Access kept in front, plus the API key** (recommended when remote
  access is needed). An Access "Service Auth" policy for named service tokens; clients send
  `CF-Access-Client-Id`/`Secret` plus the bearer key. board-serve already verifies the Access JWT;
  on `/api/v1` it accepts only a JWT whose `common_name` is in `serve.api.service_tokens` (or an
  allowed email, for the `/dev` console), AND a valid key. Two independent secrets, and the
  internet never reaches our parser unauthenticated. Cost: two header pairs per client; service
  tokens expire (Access manages that).
- **B. Separate hostname with an Access bypass, key only.** Simplest for SaaS webhooks that can't
  add headers, but it puts board-serve's stdlib HTTP server and auth code directly on the internet
  (DoS, parser bugs, no Access logs or geo rules). Not recommended. If ever needed: only a narrow
  inbound-webhook path with HMAC-signed payloads, as a later design.

## 6. Mock mode

`board-serve --mock` is a separate process on `serve.port + 2`.

- Home: `BOARD_HOME=$BOARD_MOCK_HOME`, seeded from `tests/fixtures/mock/` (sessions, decisions,
  tasks, fake tickets, the demo `asb_test_` key). `--reset` reseeds it.
- Refuses to start when the mock home resolves to the real home or inside it.
- Sets `BOARD_MOCK=1`. With it: notify.py and board-notify become no-ops (they raise in tests);
  `board press` and `board away` refuse; the tracker config is ignored and only the fake provider
  loads; there is no tunnel listener; `board-serve --watch` refuses a mock home, so the operator
  never sees mock items and nothing reaches a session.
- Keys: the mock accepts only `asb_test_` and the real API only `asb_live_`; any other key gets 403
  `key_wrong_env`. They are stored in different homes, so lookups can't cross.
- `/dev/mock/api/v1/*` on the real board-serve is a dumb loopback proxy to the mock port. It strips
  `Cf-*` and forwarded headers and copies no identity. That is how the console reaches the mock
  through the tunnel.

## 7. Portal

`/dev` is `artifact/dev.html`, served like the board: nonce CSP, `no-store`, `script-src` nonce
only, `connect-src 'self'`.

- Renderer: our own minimal one, about 300 lines of vanilla JS (tags, operations, params, schemas,
  examples, a curl snippet). Vendored Redoc or Swagger UI is about 1 MB, and those want a CDN or
  eval-like CSP relaxations.
- Try-it console: defaults to mock with the demo key prefilled; "live" needs a pasted live key,
  shows a red banner and asks for confirmation on each write; the key stays in memory only, never
  localStorage.
- Who sees what: `/dev` is visible to whoever can see the board (local, or `allowed_emails` on the
  tunnel); `/dev/keys`: approvers create and revoke live keys (session-authenticated
  `POST /api/apikeys` with the `X-Board` CSRF header; an API key can never mint keys; the key is
  shown once in a modal); test keys: any viewer, created in the mock home.

## 8. PRs

One open at a time. `lib/serve.py` is split into `lib/serve_api.py`, `lib/apikeys.py`, `lib/tickets/`.

| PR | scope | tests |
|---|---|---|
| T1 | tickets core: interface, routing, cache, limits, http-json + fake providers, `board ticket get\|doctor`, `GET /api/tickets/{ref}` | a stdlib fake tracker server; ambiguity and 409; SSRF (host pin, redirect, http refused); a sentinel token never in stdout, stderr, events or cache; 429 backoff |
| T2 | Jira provider (cloud + server) | recorded JSON fixtures served locally, ADF to text, transition by name |
| T3 | ticket writes + take + task link + relay: contract, operator prompt, worker protocol | a temp-home end-to-end take; non-approver 403 with nothing written; partial upstream failure; already_taken; `--watch` prints `ticket KEY taken`; fixed relay format |
| T4 | board.html ticket panel | `test_page_links.py` style; buttons absent for non-approvers |
| A1 | API keys + CLI + pepper + gitleaks rule + redaction | shown once, hash only, 0600, expiry and revoke, a gitleaks run catches a fixture-shaped key outside fixtures |
| A2 | `/api/v1` read routes + spec table + openapi.json, local listener | a scope matrix per route; 401, 403, 429; spec equals router; no CORS headers; `local` refused on v1 |
| A3 | v1 writes (tasks, messages, ticket writes, take → pending decision) | audit rows; an API take never runs without an approver's answer; message relay label |
| A4 | mock mode + `/dev/mock` proxy | real-home tree hash unchanged after a mock session; zero notify calls; cross-env key refused; refuses to start on the real home |
| A5 | `/dev` portal + key UI | CSP header, approver-only creation, the key in exactly one response |
| A6 | MCP stdio + `/mcp` | initialize, tools/list filtered by scope, tools/call against mock |
| A7 | tunnel exposure (option A) | JWT fixtures with a test RSA key; service-token common_name allowlist; an email JWT without a key gets 401 |
| A8 | static developer docs site (see the addendum) | |

## Decisions

The owner's answers to the open questions.

- **Q1. API keys never answer decisions.** Anything that needs authority, a take included, becomes
  a pending board decision for an approver. There is no `decisions:answer` scope. (A later option
  was considered: a key bound to one named approver, `kind=question` only, plus a confirm step on
  the board; never permission prompts or production gates.)
- **Q2. Remote exposure now, using option A:** the same hostname, Cloudflare Access kept in front,
  an Access service token (common_name allowlist) AND a board API key. Option B is never used.
  Local-only stays possible via config. A7 ships together with the first `/api/v1` PR, not last.
- **Q3. Only approvers and release_managers read tickets on the board**, because the tracker token
  can read whatever its owner can, so plain viewers would get it too. Writes and take are approvers
  only.
- **Q4. Taking a ticket on the board authorizes the assigned session to follow the delivery
  process up to and including opening the PR/MR:** plan, design, build, tests, local review, push,
  open the PR, progress comments and in-progress on that ticket. It does NOT authorize merge,
  production deploy, closing or cancelling the ticket, touching other tickets or repos outside the
  ticket's scope, or any red line. Merge and production still need a `[BOARD decision]` or the
  owner in the pane. Gates before PR open (plan sign-off and so on) are covered by the take itself.
  The contract and the worker protocol spell this out.
- **Q5. Tracker comments posted from the board carry the footer "(via agent-switchboard)"** with no
  email. Including the identity is opt-in.

### Addendum: static developer docs site (A8)

Publish the developer portal as a static site on GitHub Pages: a GitHub Actions workflow builds
`site/` from `lib/api_spec.py`'s openapi.json, `artifact/dev.html` in static mode, and the board
page with its demo adapter. In static mode the try-it console runs against an in-browser mock (a JS
fake backend seeded from the same fixtures as `board-serve --mock`), so it needs no server and no
keys, and it never calls a live board. Cloudflare Pages can host the same build output (documented
as an alternative). Generic and free for the community: no account-specific setup in the repo. The
actions are pinned by SHA, like the CI.

### Addendum: API keys are issued to a person

Every live API key has an `owner` (an email, or `local`), set at creation:
`board apikey create --owner EMAIL ...`.

- Approvers may issue keys to any listed person, or to themselves. A non-approver viewer can see
  the keys issued to them (name, scopes, created, expires, last used, status) and can revoke their
  own key, but can't create one unless an approver allows it later.
- The profile menu on the board (top right) lists "my API keys" for the signed-in identity.
- The audit trail shows the owner plus the key name for every API call.
- An API key still never carries owner authority (Q1). Its owner field is attribution, not approval.

### Addendum: key administration by owners

- A new role, `owners` in fleet.yaml, sits above approvers for administration. An owner can manage
  everyone's API keys from the board's profile menu ("Manage API keys"): issue a key to a person;
  approve a key request (a non-owner may request a key, and it stays pending until an owner
  approves it); revoke; rotate (create a successor and revoke the old key after an optional grace
  window); set or extend the expiration; change scopes (which revokes and reissues the key).
- Every admin action goes through the CLI (`board apikey ... --by board:<owner>`) and is audited.
- An approver who isn't an owner sees only their own keys.
- An API key itself can never manage keys.

## Implementation notes (T1)

Where T1 differs from the text above (`docs/contract.md`, "Tickets", is binding):

- **Config is in `fleet.yaml` under `tickets:`** (`tickets.trackers`, `cache_ttl_seconds`,
  `not_found_ttl_seconds`), not a separate `trackers.yaml`. Tokens still never go there.
- **Tokens:** `auth.env` (process env, then `<config dir>/.env`) or `auth.token_file` (a file that
  must not be readable by group or others), or `auth: none`.
- **Reads only.** The http-json provider accepts `GET` for `resolve` and `get`; write calls come in
  T3. `fill_body` (typed substitution into a parsed JSON template) is in place for them.
- **429:** a `Retry-After` up to `retry_wait_max_seconds` (default 2) is waited out once; a longer
  one is stored in `tickets/state/<tracker>.json` and refuses requests until it passes, across
  processes. The breaker counts consecutive upstream failures (5xx and network errors) and is
  stored there too; the token bucket is per process.
- **Errors:** also `bad_ticket_ref` (400) for a ref that is not `KEY` or `tracker:KEY`.
  `ticket_auth` is 502 (the tracker refused our token, not the caller), `ticket_unconfigured` 503.
- **The route** answers 403 `no_approvers` when approvers and release_managers are both empty, and
  403 `not_approver` otherwise; the refusal is audited as a `ticket` event.
- **Mock:** `BOARD_MOCK=1` already loads only `fake` trackers; the rest of mock mode is A4.

## Implementation notes (T3)

- **Writes** are configured per tracker (`comment`, `transition` with its `states`, `assign`), with
  POST/PUT/PATCH and a JSON body template filled by type; `ref` optionally picks the new comment's
  id. `fake` trackers support all three and log them to `tickets/fake-writes.jsonl`.
- **Take** follows the lifecycle in section 2, with the tracker's `take:` config (`transition`,
  `assign`, `comment` with `{task_id}`, `{session}`, `{key}`) as the default upstream writes; the
  request can skip each (`comment: false`, `transition: false`, `assign: false`) or give its own
  comment. `board task new` gained `--ticket TRACKER:KEY [--ticket-url URL]`.
- **The relay** text follows decision Q4 (not the earlier "plan stage" wording) and names the
  check: `Verify with board ticket show TRACKER:KEY`. Tracker text in it is defanged (one line,
  no brackets or double quotes). `--watch` prints `ticket KEY taken <msgId>` (and `released`): the
  message id is added so the operator can run `board message show`.
- **Release** is implemented (a `released` relay ends the authority); reassign is release + take.
  A link becomes `done` lazily, when it is read after its task reached `maintain` or ended. The
  5-minute refresh and the drift badge come with the board panel (T4).
- `board ticket show` is the worker's check: exit 0 only for one active take, and it reports
  whether `taken_by` is an approver now.

## Implementation notes (A6)

- **Scope discovery:** `bin/board-mcp` asks `GET /api/v1/whoami` (any valid key, no board data)
  and caches the scopes for 60 s; `POST /mcp` has the key record. Listing is a convenience only:
  `tools/call` goes to the route, which checks the scope itself. No probing of data routes.
- **One enforcement path:** `/mcp` authenticates the key once per request (route `/mcp`), then runs
  each tool call through `serve_api.grant` (scope, rate, audit with `"mcp": true`) and the route's
  own `Api` handler in process, so the viewer filter (Q3) and the secret guard apply unchanged.
- **Read-only for now:** `ticket_get` and the write tools wait for their `/api/v1` routes (no v1
  ticket route exists yet; writes are A3). `lib/mcp_tools.py` marks the hook (`WRITE_TOOLS`).
- **Transport:** Streamable HTTP with JSON responses only (no SSE stream, no session id); stdio is
  newline-delimited JSON-RPC. The stdio client refuses redirects and plain http to a non-loopback
  host, so the key never leaves for another host or in clear text.

## Implementation notes (T4)

- The panel is a section of `artifact/board.html` ("Tickets"), present only for approvers ∪
  release managers (Q3); approvers get the actions. Take and release have a confirm step that
  shows the Q4 text (the same words as the relay, `lib/tickets/writes.py` `AUTHORIZES`).
- `GET /api/tickets/<ref>` also returns `link` (the newest take) and `tracker` (capabilities,
  states, what a take does upstream), so the panel needs no other route.
- The 5-minute refresh runs in the page, only while it is visible and a ticket is shown or taken;
  the drift badge compares the tracker's `done` category with a task that is still going. Session
  cards get a `KEY taken` / `KEY closed upstream` chip from `task.ticket` (approvers and release
  managers only).
- Demo mode has three sample tickets; Artifact mode shows a note (the page cannot reach trackers).
