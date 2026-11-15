# Publishing Artifacts (plans, architecture, reports)

claude.ai Artifacts are a good way to share a plan or report as a page. These rules keep them
accurate, keep their cost visible and keep a copy you control.

## 1. Review before publish, until LGTM

- Before publishing or updating an Artifact, have an independent teammate (a `Plan` agent or a
  separate reviewer) check it **against the code and the facts**.
- On `REVISE` / `CHANGES REQUESTED`, fix it and send it for review again. Publish only on
  **LGTM / APPROVED**.
- Show the number of review rounds and the verdict on the page ("Reviewed: 2 rounds, LGTM").
- Give the reviewer the sources (code paths, docs), not your conclusion. Otherwise it tends to
  confirm what you told it.

## 2. Version and cost table on the page

Every Artifact carries a **versions and cost** table, in the footer or behind a small "ⓘ Info"
button:

| Version | Date | What changed | Review | Lead tokens (input / cache read / cache write / output / subtotal) | Teammate tokens |
|---|---|---|---|---|---|
| v1 | 2025-01-10 | New | 2 rounds, LGTM | 120k / 1.4M / 90k / 18k / 1.6M | 310k |
| v2 | 2025-01-12 | Added rollout section | 1 round, LGTM | … | not available |
| **Total** | | | | … | … |

- One row per version, plus a cumulative total.
- If a range also covered other work and the numbers look high, say so in that row.
- A version's own numbers exist only after it is published. So the table runs up to the **previous**
  version, and the current row says "filled in after this publish". Fill it in on the next update,
  or republish right away with only the table changed.

**Where the numbers come from**

- **Lead tokens**: sum the `usage` of every assistant message in the session transcript
  (`~/.claude/projects/<project>/<session>.jsonl`) between the previous publish and this one: input,
  cache read, cache write, output.
- **Teammate tokens**: the `subagent_tokens` in each teammate's completion notice, covering research,
  writing and every review round.

## 3. Token ledger

Keep a local ledger file (for example `<docs-repo>/artifacts/token-ledger.md`) with one row per
publish: date, artifact title and URL, version, lead tokens, teammate tokens, what changed. Add a
subtotal per artifact and a grand total. Write the file, but don't commit it automatically.

After each publish, reply with one line: tokens for this version, and the artifact's running total.
If the cost is clearly high, say why (the lead did work a teammate should have done, many review
rounds), so the cost can be judged.

## 4. Local backup knowledge base

Published pages may be private or get lost. After every publish or update, save a copy to a docs repo
you control:

- Path: `<docs-repo>/artifacts/<source-repo>/<YYYY-MM-DD>-<slug>.html`. Updates overwrite the same
  file.
- At the top, an HTML comment with the original URL, the publish date, the **publishing account**,
  and a note that the original may be private or inaccessible, so this copy is the reference.
  Only the publishing account can later update or delete the page, so record the account the CLI
  was *actually* logged into at publish time (`~/.claude.json` → `oauthAccount.emailAddress`). Don't
  rely on the email in the session context: it isn't refreshed after you switch accounts with
  `/login`, so it can name an account that no longer owns anything.
- Right after the comment, `<meta charset="utf-8">`. The published page gets a charset from the
  platform, but a local file opened directly is decoded with the browser's default, which garbles
  non-ASCII text.
- Add or update one line in `<docs-repo>/artifacts/README.md`: date, title, source repo, URL,
  publishing account, a one-sentence description.
- Write the files only. Don't commit or push unless asked.

## 5. Table of contents

If an Artifact has sections, it needs a TOC:

- Every section has an `id`. TOC entries are `#id` anchor links.
- On wide screens the TOC is fixed at the side or sticky. On phone width it becomes a collapsible list
  at the top.
- Highlight the current section while scrolling.
- The TOC must not cover content or cause horizontal scrolling.

## 6. Diagrams

Draw them yourself with inline SVG (or whatever the page natively supports, such as Mermaid). The
approach follows [archify](https://github.com/tt-a1i/archify), but don't install or call its tooling.

- **Pick the right diagram type first:** architecture, workflow, sequence, data flow, lifecycle, ERD.
- **Architecture:** 8–12 core components, one highlighted main path, external dependencies and
  **trust boundaries** drawn explicitly.
- Label nodes with their source where useful (`file:line`).
- For design comparisons, use **Before / Delta / After**.

## 7. Account guard (generic pattern)

If only certain accounts may publish (for example only your work domain, not a personal account),
enforce it with a `PreToolUse` hook rather than a rule that depends on memory. Read-only actions
always pass. Write actions fail closed when the account can't be determined. The hook reads the
account the CLI is actually logged into (`oauthAccount.emailAddress`), not the email in the session
context, which goes stale after a `/login` account switch. A rule-only setup should check the same
file, and refuse when it disagrees with the session context.

`~/.claude/hooks/artifact-publish-guard.sh`:

```bash
#!/usr/bin/env bash
# PreToolUse guard: only accounts in ALLOWED_DOMAIN may publish Artifacts or write docs.
# Read-only actions always pass. Fails closed if the account email can't be determined.
ALLOWED_DOMAIN="${ARTIFACT_ALLOWED_DOMAIN:-@example.com}"

input=$(cat)
tool=$(jq -r '.tool_name // empty' <<<"$input")

is_write=0
case "$tool" in
  Artifact)
    action=$(jq -r '.tool_input.action // "publish"' <<<"$input")
    case "$action" in
      read|list|read_db|list_assets|read_asset|list_files|read_file|status|watch|unwatch|list_types|describe_type|quickstart) ;;
      *) is_write=1 ;;
    esac ;;
  mcp__*Docs__*)
    case "${tool##*__}" in guide|query|read|export) ;; *) is_write=1 ;; esac ;;
esac
[ "$is_write" = 1 ] || exit 0

cfg="${CLAUDE_CONFIG_DIR:+$CLAUDE_CONFIG_DIR/.claude.json}"
[ -n "$cfg" ] && [ -f "$cfg" ] || cfg="$HOME/.claude.json"
email=$(jq -r '.oauthAccount.emailAddress // empty' "$cfg" 2>/dev/null)
[ -n "$email" ] || email=$(timeout 15 claude auth status 2>/dev/null | jq -r '.email // empty' 2>/dev/null)

shopt -s nocasematch
[[ -n "$email" && "$email" == *"$ALLOWED_DOMAIN" ]] && exit 0

reason="Blocked: this account is not in ${ALLOWED_DOMAIN}. Write the content to a local file and give the user its path."
jq -n --arg r "$reason" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
```

`~/.claude/settings.json`, merged with `jq` after a backup:

```json
{ "hooks": { "PreToolUse": [ {
  "matcher": "Artifact|mcp__.*Docs__.*",
  "hooks": [ { "type": "command", "command": "$HOME/.claude/hooks/artifact-publish-guard.sh",
               "timeout": 20, "onFailure": "block" } ] } ] } }
```

Pair it with a `CLAUDE.md` rule, so the model knows what to do when it is blocked: write the content
to a local file, give the full path, and say that publishing was blocked by the account rule.

**Switchboard note:** the board operator's `write_db` calls are writes too. Run the operator on an
allowed account, or let `read_db`/`write_db` through the guard on purpose.
