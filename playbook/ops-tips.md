# Ops tips

Small rules that save friction every day. Each is ready to paste into a `CLAUDE.md`.

## Reply language mirroring

- Reply in the language the user writes in. Mixed-language messages follow the dominant language.
- Decide by the **recent dominant language of the conversation**, not just the last line. A short
  English aside in a conversation held in another language ("merged?", "what's this?") still gets a
  reply in that language. Switch only when the user writes full paragraphs, or several messages in a
  row, in the new language.
- This applies to conversation only. Commit messages, MRs, tickets and code comments follow each
  project's conventions.

## WSL ↔ Windows paths

- When the workspace is in WSL and the user pastes a Windows path (`C:\Users\…`,
  `\\wsl.localhost\<distro>\…`, `file:///C:/…`), convert it with `wslpath -u '<path>'` and read it.
  Don't ask.
- When you hand the user a file to open in Windows (Explorer, browser, editor), also give the
  `wslpath -w '<path>'` form.
- Quote the whole path in single quotes when it contains spaces or backslashes. A
  `\\wsl.localhost\<distro>\home\…` UNC path maps directly to `/home/…`.

## Headed browser with manual steps

When a script opens a visible browser and the user has to log in or click something, make it obvious
which window and tab they need:

- **Tab title:** prefix each tab's `document.title` with a tag such as `【A】` or `【B】`. Navigation
  resets the title, so reapply it on `page.on('load')` or `framenavigated`.
- **Window:** with several windows, give each a fixed position (`--window-position`) and a fixed
  colored label in a page corner showing the window name.
- **Tab count:** keep one active tab per window. The script closes popups and leftover pages after
  logout, so tabs don't pile up.
- **Instructions to the user** name the window, the tab title (or URL), what to do and where they
  will end up. Never write "tab4" or other ids only the script understands.
- **Detect completion.** In sync Playwright, poll with `page.wait_for_timeout()`, not `time.sleep()`.
  With `time.sleep()`, `page.url` doesn't update and the script hangs.
- Save screenshots to a project-relative folder (for example `.playwright-mcp/`) unless told
  otherwise.

## Auto-approve read-only commands

Read-only inspection commands run without asking: `ls`, `cat`, `find`, `grep`, `head`, `tail`, `wc`,
`ps aux`, `git status`, `git log`, `git diff`. So do pipelines that end in `head`, `tail` or `grep`
(for example `some_command 2>&1 | tail -20`).

Put the instruction in `CLAUDE.md`, and also make it real with an allowlist in `settings.json`:

```json
{ "permissions": { "allow": [
  "Bash(ls:*)", "Bash(cat:*)", "Bash(find:*)", "Bash(grep:*)", "Bash(head:*)", "Bash(tail:*)",
  "Bash(wc:*)", "Bash(ps aux:*)", "Bash(git status:*)", "Bash(git log:*)", "Bash(git diff:*)" ] } }
```

## Slack notifications with a fallback chain

When a task completes or needs approval, notify, and degrade gracefully:

1. **Bot token** (two-way): send, ask, wait for a reply.
2. **Incoming webhook** (send-only): `SLACK_WEBHOOK_URL` from `.env`.
3. **Neither configured**: skip silently. Never fail the task because a notification failed.

```bash
WEBHOOK=$(grep -E '^SLACK_WEBHOOK_URL=' .env 2>/dev/null | cut -d= -f2- | tr -d '"')
[ -n "$WEBHOOK" ] && curl -s -X POST -H 'Content-type: application/json' \
  -d "$(jq -n --arg t "[<project>] Task completed: <description>" '{text:$t}')" "$WEBHOOK" >/dev/null
```

- Webhook notifications are sent without asking for confirmation.
- Build the JSON with `jq`, so quotes in the message don't break it.
- With the switchboard installed, `board notify "<text>"` goes through its alert adapters (Slack,
  desktop, email, LINE, SNS, webhook), with escalation levels, quiet hours and dedupe. See the README's
  "Alerting & escalation" section.
- Never print the webhook URL. It is a secret.

## Commit and PR attribution

If your team doesn't want AI attribution in history, say so explicitly: no `Co-Authored-By` trailer
for the assistant, no "Generated with …" footer in PR bodies, and no model names. Tools add these by
default, so the rule has to say to remove them.
