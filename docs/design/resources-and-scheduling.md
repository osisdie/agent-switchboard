# agent-switchboard: system resources + smart scheduling (design)

Status: approved. PR 1 (resources, alerts, operator-stale, resources on the board) is implemented;
see "Implementation notes (PR 1)" at the end for where it differs from this text. PRs 2–4 follow
this design. The binding formats are in `docs/contract.md`.

## 0. Constraints found in the code
- **The operator's Monitor watches `snapshot.json`, and `collect.py` rewrites it whenever any field changes.** Live resource numbers in the snapshot would wake the LLM operator every 3 s and burn tokens. So resources and the schedule live in their own files, and the snapshot only carries a small digest that changes on a *level* change.
- **The incident is a known gap.** No zero-token component alerts when `operator.json.heartbeat_at` goes stale or `serve.py --watch` dies; only the page banner shows it. On a busy development host `/proc/vmstat oom_kill` is often already non-zero, so OOM kills are worth counting. PSI (`/proc/pressure/memory`) is available on recent Linux kernels, WSL2 included.
- **Tasks today hold only `{id,title,owner,stage,artifacts,gates}`.** There is no repo, branch, dependency, size or done-status field, so the scheduler needs task-model additions.
- **`usage.json` holds one reading: whichever session last drew its statusline.** Assumption: one shared Claude account pool. Multi-account support is a later step (`usage/<session_id>.json` plus an optional `accounts:` map).

## 1. Data model ($BOARD_HOME)
```
resources.json      # collector, every resources.interval (15 s); written only when changed after rounding
schedule.json       # collector → lib/plan.py, every schedule.interval (60 s) and on snapshot change; written only on change
schedule-state.json # plan's memory: jam episodes, usage history ring (30 min), advisory delivery {id: sent_at, acked_at, n}
lessons.jsonl       # append-only; each line is a full record version; the last line per id wins
schedule-off        # present = scheduler paused at runtime (like owner-away); `board plan off|on`
```

**resources.json**
```json
{ "at": 0, "platform": "linux|wsl|darwin|other", "level": "ok|warn|high|critical",
  "cpu": {"pct": 37, "ncpu": 16, "load": [1.2,0.9,0.8], "load_per_cpu": 0.08},
  "mem": {"total_mb":0,"available_mb":0,"available_pct":0,"psi_some_avg60":0.0},
  "swap": {"total_mb":0,"used_pct":0}, "oom_kills": 2, "oom_kills_delta": 0,
  "disks": [{"mount":"/","paths":["$BOARD_HOME","~/work/api"],"free_mb":0,"free_pct":0}],
  "sessions": [{"code":"API","pid":123,"rss_mb":0,"cpu_pct":0,
     "children":[{"class":"test|browser|build|container|claude|other","comm":"pytest","pid":0,"rss_mb":0,"cpu_pct":0,"age_s":0}]}],
  "heavy_running": 1, "unavailable": ["psi","swap"] }
```
- **Sources.** Linux and WSL read `/proc/{stat,loadavg,meminfo,vmstat,pressure/memory}` and `/proc/<pid>/{stat,status,cmdline}`. Each session's process tree is walked by ppid from the registry pid. CPU% is the jiffies delta against the previous sample.
- **Elsewhere.** `os.getloadavg`, `shutil.disk_usage` and `ps -axo pid,ppid,rss,%cpu,etime,comm`. Missing metrics go into `unavailable`, never 0.
- **Process classes.** Regexes on comm/cmdline, overridable in config:
  - test: pytest|jest|vitest|go test|cargo test|mvn|gradle
  - browser: chrome|chromium|firefox|headless_shell|playwright
  - build: tsc|webpack|vite build|cargo build|docker build
  - container: docker|containerd
- **Heavy** means class test, browser or build, or any child with RSS > 1 GiB.

**Snapshot additions** (stable fields only, so the Monitor wakes only on real changes):
- `"resources": {"level","worst":"mem|swap|disk:/|load|oom"}`
- `"schedule": {"digest","mode","jams":[{id,rule,severity}],"advisories_n"}`

**Task fields** (all optional; old task files stay valid):
- `status: active|paused|blocked|done|dropped`
- `repo` (git toplevel or remote slug), `branch`, `base`
- `depends_on: [{task, kind: "merge"|"deploy"|"done"}]`
- `size: S|M|L`, `heavy: bool`, `priority: 0-3` (owner-set, default 1)
- `pr: {url, state: open|merged|closed, head_sha, merged_at, method: squash|merge|rebase}`
- `waiting_on: {kind: human|task|ci|reset, ref}|null`, `progress_at`

CLI:
- `board task set ID --repo --branch --base --size --heavy --priority`
- `board task dep ID --on T [--kind merge|deploy|done]`
- `board task pr ID --url U --state open|merged [--head SHA --method squash]`
- `board task done|pause|resume ID`

**schedule.json**
```json
{ "generated_at":0, "enabled":true, "mode":"normal|conserve|wait-reset|degraded|off", "digest":"sha1",
  "queue":[{"rank":1,"task":"t-..","owner":"API","state":"run|ready|blocked|deferred|paused|done",
            "score":71,"p_finish":0.78,"blocked_by":[{"kind":"human|task|repo|resource|reset|redline","ref":"d-.."}],"why":["test stage","unblocks 2"]}],
  "answer_first":[{"decision":"d-..","unblocks_sessions":3,"unblocks_tasks":["t-.."]}],
  "advisories":[{"id":"a-<hash(kind,to,task,ref)>","to":"API|owner","kind":"pause|resume|defer|finish-first|rebase-needed|wait-for-reset|free-resources|stop-own-heavy|answer-first|ask-owner",
                 "task":"t-..","text":"…","severity":"info|warn|high","since":0,"lessons":["l-.."]}],
  "jams":[{"id":"j-<rule>-<scope>","rule":"J1..J8","severity":"warn|high","since":0,"sessions":["API"],"signals":{},"text":"…"}] }
```

## 2. Deterministic scheduler: `lib/plan.py` + `board plan`
- `plan(inputs, cfg, now) -> schedule` is a pure function: no I/O, golden-testable.
- Inputs: snapshot, resources, usage and limit, tasks, schedule-state, active lessons, and git facts. Git facts are cached per HEAD mtime and refreshed at most every 60 s: `rev-parse --show-toplevel --git-common-dir --abbrev-ref HEAD`, `log -1 --format=%ct`, and `merge-base --is-ancestor` for stacked-branch checks.
- CLI: `board plan [--json|--explain [TASK]|--dry-run]`, `board plan off|on|status`, `board plan ack ADV --from CODE`.

**Gates** (hard, applied before scoring; any hit removes the task from `run`):
1. **Red line.** Stage `production`, or `waiting_on` a gate:production, gives `blocked{redline}`. The scheduler never emits resume, run or "go" past it, and no advisory text ever contains approve, deploy or `[BOARD decision`. A test enforces this.
2. **Human.** A pending decision or gate from the owner, or a session in needs-you, gives `blocked{human}`. While the owner is away, the task becomes `deferred{owner-return}` and the session is advised to switch to work that needs no human.
3. **Dependency.** If any `depends_on` is unmet, the task is `blocked{task}`. `deploy` means the dep task reached `maintain` or is done (owner decision 2026-11-15: an approved production gate alone does not count); `merge` means `pr.state=merged`.
4. **Repo.** Another active task in the same `git-common-dir` has an open PR, and `one_pr_per_repo` is on. The lower-ranked task gets `ready` plus a `finish-first` advisory: keep committing locally, don't push or open a PR.
5. **Resource.** If the task is heavy and the level is ≥ high, or `heavy_running ≥ max_heavy_concurrent`, it is `deferred{resource}`. At critical or after an OOM kill, `free-resources` names the largest heavy children, and `stop-own-heavy` asks their owning session to stop its own heavy process.
6. **Reset.** In wait-reset mode, new starts become `deferred{reset}` with the `resets_at` time.
7. **Active lesson rules** (section 4) add gates of the same kinds. They can only tighten, never loosen.

**Score** (ranks the tasks that pass the gates):
```
p_finish = stage_w × size_w × recency
  stage_w: plan .15 design .25 build .45 test .70 deploy .85
  size_w:  S 1.0 · M .8 · L .6 (unknown .8)
  recency: 0.5^(minutes_since_progress / 90)
score = 60·p_finish + 15·min(unblocks,3) + 10·priority + 2·hours_ready (aging, cap 10)
        − 15·heavy·[level ≥ warn]
```
- In conserve mode the p_finish weight becomes 90 and plan/design tasks get −20, so nearly finished work wins.
- `progress_at` = max(task.updated_at, last commit on the task's branch, `board task` events). Tool activity alone is not progress, which is how churn shows up.

**Modes**
- `wait-reset`: the 5h window is ≥ 95%, or a limit episode is active.
- `conserve`: the projected 5h exhaustion, from the burn rate over 30 min, comes before `resets_at` − 20 min; or 5h ≥ 85%; or weekly pct > elapsed-week-fraction + 15 points.
- `degraded`: resources are at critical.
- Otherwise `normal`.

**Jam rules** (an episode opens after 2 consecutive evaluations and closes after 2 clear ones):
| rule | condition (defaults) | severity → alert |
|---|---|---|
| J1 all-wait-human | every live session with an active task is blocked{human} directly or through deps for ≥ 10 min | warn (info while away). Output: `answer_first` ranked by sessions unblocked |
| J2 dep-cycle | a cycle in `depends_on` (DFS) | high; `ask-owner` naming the cycle |
| J3a same-branch | ≥ 2 live sessions on the same common-dir + branch, or the same worktree path | high, immediately |
| J3b repo PR clash | ≥ 2 open PRs in one repo (one_pr_per_repo) | warn; `finish-first` to the lower-ranked task |
| J4 stall | session `working` with an active task and no progress for 45 min (warn) / 90 min (high); or `done/rest` with an unblocked active task for 30 min (info, `resume`) | warn/high |
| J5 resources | level critical for 2 samples, or `oom_kills_delta > 0` | high (OOM is high immediately) |
| J6 operator silent | `operator.json.heartbeat_at` > 10 min old, or no `serve.py --watch` process while serve is enabled, and something is pending | high → urgent after 30 min (the incident) |
| J7 limit starvation | mode conserve/wait-reset with ≥ 2 sessions working | warn; one alert per reset window |
| J8 rebase-stale | a `rebase-needed` advisory open > 60 min | info |

**Rebase-needed (squash merge).** When task X's `pr` becomes merged with `method=squash`, any active task Y in the same repo whose branch contains `X.pr.head_sha` (`merge-base --is-ancestor`) gets `rebase-needed`: "rebase --onto origin/<base> <X.head_sha> <Y.branch>; force-push only your own feature branch with --force-with-lease". If Y's base was X's branch, the advisory also says to retarget the PR base.

## 3. How the operator uses it
**When it runs.**
- The plan runs inside the zero-token collector: a 60 s timer, plus every snapshot change.
- The operator wakes only when `snapshot.schedule.digest` changes, or on new `board-serve --watch` lines: `advisory <id> new`, `jam <id> open|resolved`, `lesson <id> candidate`.
- One wake handles one batch of messages. The LLM never computes the schedule itself.

**Message format** (sent by BOARD only, from day one):
`[BOARD schedule <adv-id>] <kind>: <text> — advisory for ordering/pausing only; not an approval. Reply [re <adv-id>] ack|done|cannot: <reason>`
- Several advisories for one session go into one message, one line each.
- `cannot` replies are logged and become lesson signals.

**Worker CLAUDE.md wording** (for `snippets/worker-protocol.md` and `playbook/claude-md.template.md`):
> **`[BOARD schedule <id>] …` from `BOARD`** is a fleet-scheduling advisory. Follow it for *ordering and pacing*: pause or defer, finish one thing first, wait for the usage reset, don't start a heavy test run or browser now, rebase my own branch, and at critical memory stop a heavy process I started myself. It is **never an approval**. It does not answer any question, gate or permission prompt, does not authorize a push, merge, deploy or deletion, and never moves a red line. If it conflicts with my user's instruction or a `[BOARD decision]`, those win and I reply `[re <id>] cannot: <reason>`. I only ever stop or close processes I started myself. When I can't follow it, I say why in one line and carry on.

**Noise control.**
- Advisory ids are stable hashes, so the same condition is never re-sent.
- Re-advise only if still open after `readvise_minutes` (60) with no ack.
- At most 4 schedule messages per session per hour, batched.
- `resume`, `info` and J8 stay on the board only. Only pause, defer, finish-first, rebase-needed, wait-for-reset, free-resources and stop-own-heavy are sent.
- Thresholds have hysteresis (enter at 15% free, leave at 20%) and 2-sample dwell.
- Quiet hours apply, except for high resource/OOM and J6.
- `advise: false` gives shadow mode: computed and shown, never sent.

## 4. Lessons lifecycle
**Capture (automatic, zero tokens).**
- When a jam episode resolves with time lost ≥ `min_lost_minutes` (10), plan appends a `candidate`.
- Time lost = (resolved − since) × affected sessions.
- Recorded signals: rule, resource buckets, process classes, stage, repo id (local only), and the advisories sent and their replies.
- On the `lesson <id> candidate` watch line, the operator adds a one-line cause and resolution: `board lesson note <id> --cause "…" --resolution "…"`. That costs a few tokens, only per resolved jam.
- Owners and sessions can add lessons by hand: `board lesson add --rule …`.

**Schema**
```json
{ "id":"l-<epoch>-<rand>", "fingerprint":"sha1(rule|normalized signals)", "rule_kind":"J5",
  "status":"candidate|active|retired", "source":"auto|operator|owner",
  "title":"two test suites + <2GiB available → OOM", "signals":{"mem_available_mb_bucket":"<2048","classes":["test","test"]},
  "cause":"…","resolution":"…","occurrences":3,"lost_min_total":95,"first_seen":0,"last_seen":0,
  "rule":{"when":{"all":[{"metric":"mem_available_mb","lt":3000},{"metric":"heavy_running","gte":1}]},
          "then":{"action":"defer_heavy","reason":"…"}} | null,
  "hits":0,"expires_at":0,"retired_by":null }
```

**Dedupe and merge.** The fingerprint uses buckets and classes, never session codes or timestamps. A matching fingerprint merges: occurrences++, the lost time adds up, `last_seen` is set, and the cause/resolution keep the latest non-empty note.

**Rule format** (declarative, fixed vocabulary, no eval):
- `when` metrics: `mem_available_mb|mem_available_pct|swap_used_pct|psi_mem|load_per_cpu|disk_free_pct|heavy_running|five_hour_pct|five_hour_min_to_reset|weekly_pct|open_prs_in_repo|stage`, plus events `after_squash_merge_in_repo`.
- `then` actions: `defer_heavy|max_heavy:N|pause_new_starts|serialize_repo|advise:<text>|raise_severity`.
- The schema validator only accepts actions that tighten. No action can approve, resume past a gate, push, merge or deploy.
- Example text lesson: "after a squash merge the stacked branch needs rebase --onto". It rides along as a hint in matching `rebase-needed` advisories.

**Activation.** A candidate becomes active automatically at occurrences ≥ 2 (`auto_activate_after`). The owner can activate or retire any lesson on the board (approver identity) or with `board lesson activate|retire`.

**Expiry.** A lesson is retired automatically after `ttl_days` (30) since `last_seen` with no hits. Hits extend it. At most 100 active lessons. Retired lessons are kept.

**Sharing.** `board lesson export --generic` strips repo and session names, so a lesson can be proposed for `playbook/`.

## 5. Board UI and alerts
- **Header tile "System 系統".** Mem and swap bars, load/CPU, worst disk, colored by level, plus an OOM counter badge.
- **Section "Resources 資源".**
  - Per-session rows: claude RSS/CPU, plus children grouped by class with the top 3 processes.
  - A disks table.
  - An "unavailable on this platform" note when metrics are missing.
- **Section "Schedule 排程".**
  - A mode chip.
  - A jams strip on top: red/amber cards with signals and "since".
  - An "Answer first" list that links `?d=` cards, with "unblocks N".
  - The ranked queue table: state chip, p_finish, blocked_by, why.
  - Advisories with sent/acked/cannot status.
- **Section "Lessons 經驗".** Candidate / active / retired tabs with occurrences, time lost and a readable rule. Activate/Retire buttons are approver-only (`POST /api/lessons/<id>` → `board lesson … --by`; same CSRF/Host/identity checks). The Artifact board is read-only.
- **Banners.** Critical resources and J6 show a red banner; J1 shows amber "everyone is waiting on you: answer d-… first".
- **Plumbing.**
  - serve: `resources`, `schedule` and `lessons` (newest 50) go into `/api/state`, and their files go into `signature()`.
  - Artifact mode: the operator mirrors `meta/resources` (bucketed to 5%, written only when the level changes, at most every 10 min), `meta/schedule` (on digest change) and `lessons/{id}`.
- **Alert keys** (via `notify.send`, auto-resolved in `tick()` the way `limit:` is):
  - `res:mem` / `res:swap` / `res:disk:<mount>`: warn → high, escalate=false, repeat 60 min.
  - `res:oom:<count>`: high.
  - `operator:stale`: high → urgent after 30 min.
  - `jam:<rule>:<scope>`: from the table above.
  - J1 stays at warn/info, because the per-decision alerts already escalate.
  - Advisories never alert.

## 6. Config (fleet.yaml defaults)
```yaml
resources:
  enabled: true
  interval: 15
  disks: auto                 # BOARD_HOME + each session cwd's filesystem
  thresholds:                 # warn / high (critical = high for 2 samples, or OOM)
    mem_available_pct: [15, 8]
    mem_available_mb:  [2048, 1024]
    swap_used_pct:     [50, 80]
    psi_mem_some_avg60: [10, 25]
    load_per_cpu:      [1.5, 3.0]
    disk_free_pct:     [10, 3]
    disk_free_mb:      [5120, 1024]
  # process_classes: {test: "regex", browser: "regex", ...}
schedule:
  enabled: true
  advise: true                # sent from day one; false = shadow mode (board only)
  stop_own_heavy: true        # at critical/OOM, may advise a session to stop its own heavy process
  interval: 60
  max_heavy_concurrent: auto  # 1 if MemTotal < 16 GiB, else 2
  one_pr_per_repo: true
  stall_minutes: [45, 90]
  idle_with_work_minutes: 30
  human_wait_minutes: 10
  operator_stale_minutes: 10
  conserve: {five_hour_pct: 85, margin_minutes: 20, weekly_pace_slack_pct: 15}
  readvise_minutes: 60
  max_messages_per_session_hour: 4
  forge: none                 # PR state from workers (`board task pr`); gh | glab = opt-in poll, every 5 min, cached
lessons:
  enabled: true
  min_lost_minutes: 10
  auto_activate_after: 2
  ttl_days: 30
```
- **Turning it off.** `schedule.enabled: false` or `resources.enabled: false` in fleet.yaml; `board plan off` at runtime (the `schedule-off` file is audited in events.jsonl); `advise: false` keeps it visible but silent; `stop_own_heavy: false` limits it to "don't start new".
- **Health checks.** `board doctor` reports which metrics are available and whether git, gh and glab can be found.

## 7. PRs (one at a time, in this order)
1. **Resources sampler + alerts + operator-stale (incident fix) + resources on the board.**
   - Scope: `lib/resources.py`, collector cadence, `resources.json`, the snapshot `resources` digest, `res:*`, `res:oom` and `operator:stale` in notify, `board resources [--json]`, doctor. Also: serve state and signature, the board.html System tile and Resources section, the contract, and the operator-prompt mirror rule for `meta/resources`.
   - Tests: a fixture `/proc` tree via `BOARD_PROC_ROOT` (meminfo, vmstat, pressure, a pid tree with pytest and chromium children); statvfs monkeypatched; hysteresis/dwell tables; the macOS path through a fake `ps` on PATH with no proc root. Notify keys/levels through the `test_notify.sh` harness. A check that the snapshot does **not** change when only the raw numbers change. `/api/state` shape in `test_serve.sh`; a demo-data render with the optional playwright test.
2. **Task model + scheduler core.**
   - Scope: the new task fields; `board task set|dep|pr|done|pause|resume`; `lib/plan.py`; `schedule.json` and schedule-state; jams J1–J8; modes; `board plan`; jam alerts; contract.
   - Tests: bash CLI tests (field validation, back-compat with old task files). Table-driven golden fixtures (inputs → expected queue, advisories and jams) for each gate, rule and mode. Temp git repos for the same-branch and squash/stacked `--onto` cases. Usage burn-rate fixtures. Invariant tests: no `run` at production, advisory kinds only from the whitelist, no "approve" / `[BOARD` in any text, deterministic output (same input → same digest).
3. **Operator/worker integration + lessons engine.**
   - Scope: `--watch` lines; operator prompt §11; the `[BOARD schedule]` format in the contract; the worker snippet and claude-md template wording; `board plan ack`; noise limits. Plus `lib/lessons.py`, capture on jam resolve, `board lesson ls|show|note|add|activate|retire|export`, the rule engine feeding plan gates, auto-activation and expiry.
   - Tests: watch output; dedupe/readvise/rate-limit units; defang of `[BOARD` in advisory text. Fingerprint merge, the tighten-only schema rejection, auto-activate at 2, TTL. A golden test where an active rule defers a heavy task. The export scrub.
4. **Schedule, jams and lessons UI.**
   - Scope: the board.html Schedule and Lessons sections and jam banners; `POST /api/lessons/<id>` (approver-only); the Artifact `meta/schedule` and `lessons/*` mirror.
   - Tests: the test_serve auth matrix (non-approver → 403, CSRF, Host); demo render.

PR 1 ships the incident fix even if the rest slips.

## 8. Owner decisions (approved 2026-11-15)
1. **Advisories go to workers from day one** (`schedule.advise: true`), capped at 4 per session per hour, batched, and only for pause, defer, finish-first, rebase-needed, wait-for-reset, free-resources and stop-own-heavy.
2. **PR and merge state comes from workers** via `board task pr` (zero network). Forge polling (`schedule.forge: gh|glab`) is opt-in; the default is `none`.
3. **Lessons auto-activate after 2 occurrences** (`lessons.auto_activate_after: 2`). This is safe because rules can only tighten; the owner can retire a lesson with one click or `board lesson retire`.
4. **At critical memory or after an OOM kill, an advisory may ask a session to stop its own heavy process** (`stop-own-heavy`, `schedule.stop_own_heavy: true`). The session only ever stops processes it started itself, and the board never kills anything.

## Implementation notes (PR 1)
- **Teammates** (added at the owner's request). Teammates are separate `claude` processes started
  with `--agent-id <name>@<team>` / `--agent-name`, usually in extra tmux panes, so they are not in
  the session's process tree. They belong to the session whose `session_id` equals their
  `--parent-session-id`. Failing that, they belong to the session whose tree holds them, or whose
  code or alias equals `@<team>`; the rest are `orphan_teammates`. Each has `name, pid, rss_mb,
  cpu_pct, age_s, idle_s, idle` (class `teammate`). `idle_s` is the time since its subtree last used
  ≥ 1% CPU or started a process. Alert `res:idle-teammates:<code>` (and `res:orphan-teammates`) is
  info, or warn while memory (PSI included) or OOM is at warn or worse. Memory alerts at ≥ warn name the idle
  teammates to reclaim first. The board never kills anything.
- **resources.json** adds `levels` (per metric), `oom_recent`/`last_oom_at`, `operator.watch` (the
  J6 input, checked even with `resources.enabled: false`), `teammate_idle_minutes` and per-session
  `classes`, `tree_rss_mb`, `heavy_n`, and `config_errors`. Disks are opaque ids (`disk1`, ...)
  with `labels` (`$BOARD_HOME`, session codes) instead of `paths`; levels, `worst` and alert keys
  (`res:disk:<id>`) use the id, so no path reaches the board or an alert. Command lines are matched,
  never stored. resources.json is also rewritten at least every 60 s, so `at` stays fresh when
  nothing changes (resources disabled included).
- **Disks** are probed in threads with a 2 s budget; a path that hangs is `unresponsive` and left
  alone for 10 minutes. nfs/cifs/9p/FUSE mounts are skipped (from mountinfo) unless configured.
- **ps fallback** reads `pid=,ppid=,rss=,%cpu=,time=,etime=,args=`: CPU% (and teammate idle time)
  comes from `time` deltas between samples, as on Linux; `%cpu` only fills the first sample. `args`
  gives the teammate flags.
- **Thresholds.** `mem` is the worst of available %, available MB and PSI; a disk is the worst of
  free % and free MB. Swap counts only while `mem` is at warn or worse. Hysteresis: lower-is-worse
  values leave a level at threshold × 4/3, higher-is-worse ones at × 3/4. Dwell counts a streak
  per direction and moves to the closest step the streak sustained. An OOM kill keeps `oom`
  critical for 10 minutes. Browsers are classed by `comm` alone. Load affects the
  level but raises no alert of its own (as in section 5).
- **Heavy count.** `heavy_running` counts heavy processes whose parent is not heavy, so one browser
  (or one test runner and its workers) counts once. A shell running `pytest` is class `other`.
- **operator:stale** uses the existing escalation (high, urgent after `escalate_minutes.urgent`,
  default 30). Like every non-urgent alert it is held during `alerting.quiet_hours` until it turns
  urgent; section 3's quiet-hours exception is for schedule messages and lands with PR 3. It is
  judged only with `serve.enabled` (local mode; it also fires when no `operator.json` exists yet).
  In Artifact mode the heartbeat is in `meta/operator`, which the collector cannot read.
- **"Something is pending"** for J6 means what `board-serve --watch` hands the operator: an
  answered question or gate (not a permission prompt, which board-serve presses) or a message with
  `status: "new"`. A decision still pending waits on the owner.

## Implementation notes (scheduler core)
The scheduler ships in three steps instead of section 7's PRs 2–4: (1) task model + core,
(2) operator/worker integration + lessons, (3) UI. Step 1 differs from the text above:
- **Names.** `lib/scheduler.py` and `$BOARD_HOME/scheduler.json` (not `lib/plan.py` /
  `schedule.json`); the read-only CLI is `board schedule [--json]` (`plan` is already a lifecycle
  stage and an artifact). Task changes go through `lib/tasks.py`.
- **Not yet:** usage modes (`conserve`, `wait-reset`), jams J1–J8 and their alerts,
  `schedule-state.json`, git facts, `answer_first`, `board plan off|on|ack`, the snapshot
  `schedule` digest (it would wake the operator before it acts on anything), and delivery. The
  mode is `normal`, `degraded` (critical) or `off`.
- **Score.** No recency factor in `p_finish`: the output changes only when an input or an hour
  boundary of the aging term changes, so `scheduler.json` stays heartbeat-free.
- **Added:** task `status` verbs `drop` and `block`, `board task progress` (resets the stall clock;
  there are no git facts yet), cycle and self-dependency refusal at `board task dep`, a per-session
  "one current task" rule, and heavy-slot fairness (sessions holding fewer heavy processes first).
- **Stuck** is a queue flag plus a `stuck` list (J4's input), not an advisory.

## Implementation notes (scheduler 2a: delivery, jams, usage modes)
- **Files.** Jam episodes and delivery live in `schedule-state.json` (`lib/advisories.py`), next to
  `scheduler.json`; both are written under `schedule.lock`. The pause file is `schedule-off`
  (`board schedule off|on --by WHO`, not `board plan off|on`).
- **Delivery.** The operator wakes on `schedule <CODE> due` (`--watch`) or `snapshot.schedule.due`,
  sends `board schedule outbox <CODE>` as one message and records it with `board schedule sent`;
  replies go to `board schedule reply` (not `board plan ack`). `to` must be a fleet.yaml or live
  session code. `advise: false` and `board schedule off` mark advisories `shadow`.
- **Snapshot.** `schedule: {mode, due, jams}` instead of `{digest, mode, jams[], advisories_n}`:
  it changes only on what the operator acts on.
- **Jams.** J3a uses the collector's per-session repo basename and branch (no git calls), so two
  different repos with the same basename count as one. J5 and J6 raise no `jam:*` alert (`res:*` and
  `operator:stale` already do); J4's idle case and J8 stay on the board. No `--watch` jam lines:
  nothing for the operator to do until lessons (2b).
- **Modes** follow section 2. While the owner is away J1 is info; tasks are not moved to
  `deferred{owner-return}`. A limit episode stops holding starts 10 minutes after its `resets_at`.
- **Review fixes.** J3a is board-only (the collector's branch is the start cwd's, not the
  worktree's) and skips default, detached and commit-less branches. J5 needs 2 samples at critical
  (an OOM opens it at once). Jam alerts never escalate; J4 alerts at the level it opens with. J1
  does not count a session that has another ready task. Advice to an offline session is held; an
  unanswered advisory is sent at most 3 times. `board schedule off` stops the outbox, `sent` and
  the `--watch` line at once.
- **Deploy dependencies** (owner decision): met only when the task reached `maintain` or is
  `done`; an approved production gate alone no longer counts (section 2's gate 3 said it did).
- **Not yet:** lessons (2b), git facts for stacked branches (rebase-needed still follows `base` ==
  the merged task's `branch`).

