# Playbook

Lessons from running several long-lived Claude Code sessions on real multi-repo work, written for
any team. Every rule here is a plain `CLAUDE.md` instruction, a hook or a skill. None of it needs the
switchboard, but the parts fit together.

| File | What it covers |
|---|---|
| [`multi-session-collaboration.md`](multi-session-collaboration.md) | Named resident sessions, when and how to notify each other, messages ≠ approval, one MR at a time, lead/teammate split, event-driven progress reports, CI polling, pane hygiene |
| [`artifact-publishing.md`](artifact-publishing.md) | Review-before-publish loop, version and cost table, token ledger, local backups, TOC, diagrams, an account-guard hook |
| [`delivery-harness/`](delivery-harness/SKILL.md) | A skill for an agent-ready SDLC: Plan (`intent.md`) → Design (`spec.md`) → Build → Test (`evals/`) → Deploy (`review-findings.md`) → **production gate** → Maintain. Each stage commits an artifact behind a named human gate |
| [`repo-hygiene.md`](repo-hygiene.md) | gitleaks pre-commit + CI, the banned-terms check, why `tmp*/` is gitignored, what to do after a leak |
| [`ops-tips.md`](ops-tips.md) | Reply-language mirroring, WSL ↔ Windows paths, labeling headed browsers, read-only auto-approve, Slack fallback chain |
| [`claude-md.template.md`](claude-md.template.md) | All of the above as one ready-to-paste `CLAUDE.md` with `<PLACEHOLDER>` markers |

## How to adopt

1. **Pick the scope.**
   - Rules for every session on your machine go in `~/.claude/CLAUDE.md`.
   - Rules for one repo go in that repo's `CLAUDE.md` (committed, so the team shares them).
2. **Start from the template.** Copy `claude-md.template.md`, delete the sections you don't want, and
   replace every `<PLACEHOLDER>`. Check that none are left:
   `grep -n '<[A-Z_]*>' ~/.claude/CLAUDE.md`.
3. **Install the skill** (optional):
   ```bash
   mkdir -p ~/.claude/skills
   cp -r playbook/delivery-harness ~/.claude/skills/
   ```
   Then fill in its "Configure once" table (or put the values in your project `CLAUDE.md`), and point
   `scripts/tickets.sh` at your tracker.
4. **Turn on the commit checks**: `scripts/setup-dev.sh` (gitleaks + banned terms), see
   [`repo-hygiene.md`](repo-hygiene.md).
5. **Enforce what matters with hooks**, not just words. A `CLAUDE.md` rule can be forgotten after a
   long context. A `PreToolUse` hook can't. See the account guard in `artifact-publishing.md`.
6. **Add the switchboard worker protocol** if you run the board:
   [`../snippets/worker-protocol.md`](../snippets/worker-protocol.md).

Start small: one low-risk service, the rules in `CLAUDE.md` and hooks (not only in a wiki), intents
as files, and a named release manager. The checklist is in the
[main README](../README.md#try-it-on-one-low-risk-service-this-week).

## Keep it generic

If you fork this to share it, keep real names (company, product, people, hosts, paths, channels)
in your private `CLAUDE.md` or in the gitignored `tmp-local/`, never in the playbook.
