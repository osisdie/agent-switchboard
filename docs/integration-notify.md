# Integrating alerting (`board-notify`) and the usage meter

This note is for whoever wires alerting into the collector, the `board` CLI and the installer.
The alerting pieces are standalone and already tested (`tests/test_notify.sh`):

| file | what |
|---|---|
| `lib/notify.py` | Alert engine and adapters (slack, desktop, email, line, sns, webhook). Python stdlib + pyyaml. |
| `bin/board-notify` | CLI wrapper: `send`, `ack`, `tick`, `test`, `status`. |
| `hooks/board-statusline-tap.sh` | statusLine wrapper. It writes `$BOARD_HOME/usage.json` and execs the user's previous statusline command. |
| `tests/test_notify.sh`, `tests/fake_smtp.py` | Offline tests: curl, powershell.exe and aws shims, a fake SMTP server and a fake clock. CI runs them as a separate step. |

`send`, `tick` and the `on_*` helpers never raise and never print secrets. Every delivery attempt
is logged to `events.jsonl` as `{ts, type:"alert", key, channel, level, ok, error}`.

## 1. Collector (`lib/collect.py`)

```python
import notify  # lib/ is already on sys.path in collect.py
```

There is one call per event. Each takes the dicts the collector already has, in snapshot shape.

| collector event | call | key / level |
|---|---|---|
| A session transitions into `needs-you` | `notify.on_needs_you(session_row)` | `session:<code>:needs-you`, warn. `kind=permission` when `pending` is set, which escalates to urgent after 10 min. |
| A new `pending` decision (`kind=question`, or anything with `gate:"production"`) | `notify.on_decision(decision)` | `decision:<id>`. warn, or **urgent at once** for `gate:"production"`. |
| A new `pending` permission decision whose session is already alerted | Nothing. As today, the needs-you alert covers it. | |
| A usage-limit episode starts (`snap["limit"]`, new `hit_at`) | `notify.on_limit_hit(lim, pending_n)` | `limit:<hit_at>`, urgent, sent once. |
| **Every collector loop, even when the snapshot did not change** | `notify.tick()` | Escalation, auto-ack, repeat, held quiet-hour alerts, usage thresholds. |

- `tick()` must run on every loop. Escalation is time-based (10/30 min), so it has to run while nothing changes. Skip it only under `--no-notify`. A tick with nothing due reads `snapshot.json`, `usage.json` and `alerts.json` under a flock and does no I/O beyond that, so it is cheap enough to call every `poll_interval`.
- `tick()` is also a safety net. It opens alerts for any needs-you session, pending decision or limit in `snapshot.json` that has no alert yet. A missed transition therefore still alerts on the next loop, and the per-event calls above only make the first alert arrive without waiting for the next loop. Repeated calls for the same key are deduplicated (one send per key per level per `repeat_minutes`).
- **Auto-ack:** `tick()` resolves `session:<code>:needs-you` once that session leaves needs-you (or disappears), `decision:<id>` once the decision leaves `pending`, and `limit:<hit_at>` once the episode ends. The collector does not have to call anything for an ack. If it wants the escalation to stop immediately, for example right after `board answer` or `board press`, it can call `notify.resolve("decision:<id>")`.
- **Replace the old Slack path.** The `send(...)` / `board notify` calls in `collect.notify()` for needs-you, new decisions and the limit message become the three `on_*` calls. Slack is now the `info`/`warn` channel of `board-notify`. Keeping both would double-post. `slack-cooldown.json` is no longer needed for these events, because `alerts.json` dedupe replaces it.
- If you prefer to call from bash, the CLI is equivalent:
  `board-notify send --level warn --key session:API:needs-you --title "API needs you" --body "Bash: rm -rf build" --url URL`
  `board-notify tick`

## 2. `bin/board`

- `board notify "text"` can stay as it is: plain Slack and nothing else. Optionally, forward it to `board-notify send --level info --key adhoc:<epoch> --title "text"` so it uses the same channels and log.
- Add `board ack KEY` → `exec "$REPO/bin/board-notify" ack "$@"`. `KEY` may be a glob, for example `'session:API:*'`.
- `board doctor` can print `board-notify status`. It shows `channels: slack=on desktop=on email=on line=off ...` and never shows values.

## 3. `scripts/install.sh`: statusline tap

Install (idempotent: skip if the current command already contains `board-statusline-tap.sh`):

```bash
settings="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
tap="$REPO/hooks/board-statusline-tap.sh"
cur="$(jq -r '.statusLine.command // empty' "$settings")"
case "$cur" in *board-statusline-tap.sh*) ;; *)
  # Keep the whole previous statusLine object (or null) for uninstall.
  jq '.statusLine // null' "$settings" > "$BOARD_HOME/statusline.prev.json"
  if [ -n "$cur" ]; then
    q="$(printf '%s' "$cur" | sed "s/'/'\\\\''/g")"          # single-quote for sh
    new="BOARD_STATUSLINE_NEXT='$q' $tap"
  else
    new="$tap"                                                # no previous statusline: tap prints nothing
  fi
  jq --arg c "$new" '.statusLine = ((.statusLine // {}) + {type: "command", command: $c})' \
    "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
esac
```

- Other keys of the previous `statusLine` object, such as `refreshInterval` and `padding`, are kept.
- Only `command` is replaced.

Uninstall (only when the current command is the tap):

```bash
case "$(jq -r '.statusLine.command // empty' "$settings")" in *board-statusline-tap.sh*)
  if [ -f "$BOARD_HOME/statusline.prev.json" ]; then
    jq --slurpfile p "$BOARD_HOME/statusline.prev.json" \
      'if $p[0] == null then del(.statusLine) else .statusLine = $p[0] end' \
      "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
    rm -f "$BOARD_HOME/statusline.prev.json"
  fi ;;
esac
```

The tap's contract:

- It reads stdin byte-exact.
- It writes `usage.json` atomically, in a detached background job (temp file + `mv`), only when the input has `rate_limits.five_hour` or `rate_limits.seven_day`.
- It then `exec`s `/bin/sh -c "$BOARD_STATUSLINE_NEXT"` with the same stdin. Output and exit status are the next command's.
- It measured 6–12 ms of overhead in the tests (the limit is 30 ms).
- Without jq it is a pure passthrough.

`usage.json` schema: `{five_hour:{pct,resets_at}, seven_day:{pct,resets_at}, updated_at, session_id}`. `resets_at` is in epoch ms; the tap converts epoch seconds.

Also symlink `bin/board-notify` into `~/.local/bin` next to the other commands.

## 4. Configuration

Channel credentials go in `$BOARD_CONFIG_DIR/.env`, which is resolved like `lib/fleet.py`. The process environment overrides the file. All variables are listed in `.env.example`:

- `SLACK_WEBHOOK_URL`, `SLACK_MENTION`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `ALERT_EMAIL_TO`, and optionally `SMTP_SECURITY`
- `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_TO`
- `AWS_SNS_TOPIC_ARN`
- `ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_AUTH`
- `ALERT_DESKTOP`

Policy comes from three places, merged in this order:

1. Built-in defaults.
2. `$BOARD_CONFIG_DIR/alerting.yaml`, if present. It may hold an `alerting:` key or the bare block.
3. The `alerting:` block of `fleet.yaml`, which wins.

So a machine can keep its policy in `alerting.yaml` without touching `fleet.yaml`. Suggested addition to `fleet.example.yaml`:

```yaml
# Alerting (bin/board-notify). Channels are enabled by their variables in .env.
# alerting:
#   routes:                     # level -> channels (defaults shown)
#     info:   [slack]
#     warn:   [slack, desktop]
#     high:   [slack, desktop, email]
#     urgent: [slack, desktop, email, line, sns, webhook]
#   quiet_hours: "23:00-08:00"  # local time; only urgent is delivered, the rest is held until it ends
#   repeat_minutes: 60          # re-alert an unacked key at the same level after this long (0 = never)
#   escalate_minutes: {high: 10, urgent: 30, permission_urgent: 10}
#   thresholds: [80, 90, 95]    # usage % -> warn / high / urgent, once per threshold per reset window
```

## 5. Tests

- `tests/test_notify.sh -v` runs on its own, outside `tests/run.sh`. 96 checks: routing per level, Slack mention, non-blocking desktop, email via the fake SMTP server, the LINE payload and its 5000-character cap, `aws sns publish`, webhook, no secrets on argv or in logs, dedupe/repeat, failures that never raise, quiet hours (held, then delivered), escalation timing with a fake clock, auto-ack, the production gate, limit hit, usage thresholds per window, and the tap (passthrough, exit status, byte-exact stdin, overhead).
- `.github/workflows/ci.yml` runs it as a separate step and shellchecks the three new scripts.

---

## README draft (for the docs teammate to lift)

### Alerting & escalation

`board-notify` escalates the way PagerDuty does. The collector runs it, so it costs zero tokens and keeps working while the operator session is rate-limited.

| level | raised when | channels (default) |
|---|---|---|
| `info` | FYI | Slack |
| `warn` | a session needs you, a new decision, usage ≥ 80% | Slack with @-mention + desktop toast |
| `high` | still unacknowledged after 10 min, usage ≥ 90% | + email |
| `urgent` | unacknowledged 30 min, a permission prompt unacknowledged 10 min, a production gate, usage ≥ 95%, usage limit hit | + LINE (+ SNS, webhook) |

- **Acknowledging** stops the escalation. An alert is acked automatically when the decision is answered or the session leaves needs-you. To ack by hand, run `board-notify ack 'session:API:*'`.
- **Dedupe:** each alert is sent at most once per level. It is re-sent only after `repeat_minutes` (default 60) while still unacknowledged.
- **Quiet hours** (`alerting.quiet_hours: "23:00-08:00"`) hold everything except `urgent` until they end.
- **Channels** turn on when their variables are set in `<config dir>/.env`. See `.env.example`.
- Check a channel with `board-notify test --channel email`. See what is enabled and open with `board-notify status`.

| channel | needs |
|---|---|
| Slack | `SLACK_WEBHOOK_URL`, plus `SLACK_MENTION=<@your member id>` so your phone buzzes |
| Desktop | WSL (`powershell.exe`), `notify-send` or macOS |
| Email | `SMTP_HOST/PORT/USER/PASSWORD/FROM`, `ALERT_EMAIL_TO`. Port 465 uses SSL, 587 uses STARTTLS. |
| LINE | `LINE_CHANNEL_ACCESS_TOKEN` (the bot's long-lived token), `LINE_TO` (your user id). The free plan allows 500 pushes a month. |
| SNS | `AWS_SNS_TOPIC_ARN` and a configured `aws` CLI |
| Webhook | `ALERT_WEBHOOK_URL` (generic JSON POST), and optionally `ALERT_WEBHOOK_AUTH` |

### Usage meter

Claude Code passes `rate_limits.five_hour` and `rate_limits.seven_day` to the statusline. The installer wraps your existing statusline command with `hooks/board-statusline-tap.sh`:

- The tap records `$BOARD_HOME/usage.json` and then runs your command unchanged.
- The statusline you see is exactly the same.
- `scripts/install.sh --uninstall` restores the original `statusLine` setting.

The collector turns usage into alerts once per threshold per reset window:

- 80% → warn: `5h usage 82% — resets ~14:20 — heads up`
- 90% → high: `5h usage 90% — resets ~14:20 — prepare to switch account`
- 95% → urgent: `weekly usage 95% — resets Mon 09:00 — switch account now`

The board page shows the same numbers as two meters. At ≥ 90% it shows an amber "prepare to switch account" banner, and at ≥ 95% or when the limit is hit it shows a red one.
