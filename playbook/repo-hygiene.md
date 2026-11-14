# Repo hygiene: secrets and sanitization

Agents commit often and fast, and they copy whatever is in front of them: a token from `.env`, a
webhook URL from a log, your company's hostname from a config file. Rules in `CLAUDE.md` help, but
the commit is the last chance to catch a leak. So check at the commit, automatically.

Two independent checks:

| Check | Protects against | Runs |
|---|---|---|
| **gitleaks** | secrets: API keys, tokens, private keys, webhook URLs, passwords | pre-commit hook and CI |
| **banned terms** | identity leaks in a public or shared repo: company, product, customer and people names, internal hosts and domains, machine paths, channel names | pre-commit hook (the list never leaves your machine) |

## Setup

```bash
scripts/setup-dev.sh
```

It does the following:

- sets `git config core.hooksPath .githooks`, so the versioned hooks in `.githooks/` run for this
  clone (hooks in `.git/hooks/` aren't versioned and are easy to forget)
- checks that `gitleaks` is installed (CI uses **v8.30.1**). If it isn't, it fails and points to the
  release binaries (verify them against the checksums file) or your package manager
- warns if `tmp-local/banned-terms.txt` is missing, and counts its terms if it exists

Run it once per clone. Ask agents to run it at the start of a task too. It is idempotent.

## `.githooks/pre-commit`

1. **gitleaks on the staged changes**, with `.gitleaks.toml`: `gitleaks git --staged` (or
   `gitleaks protect --staged` on older versions), with findings redacted. Only what you are about to
   commit is scanned, so it is fast. **If gitleaks isn't installed, the hook blocks the commit**
   until it is. A secret scan that silently doesn't run is worse than none.
2. **Banned-terms check.** Each pattern in `tmp-local/banned-terms.txt` is matched,
   case-insensitively, against the staged **added lines and staged file paths**, excluding `tmp*/`
   paths. Each hit prints `file:line` (or `file:(path)`) and the text. Invalid patterns are skipped
   with a warning.
3. Any hit blocks the commit. Fix the content. Don't bypass the hook with `--no-verify`, and tell
   agents that explicitly (see below).

If the banned-terms file doesn't exist, that check is skipped with a warning. gitleaks still runs.

`.gitleaks.toml` extends the default rule set. It has one narrow allowlist, for synthetic values
under `tests/fixtures/`. Don't widen the allowlist to get a commit through. Fix the content instead.

## CI

`.github/workflows/ci.yml` runs three jobs on every push and pull request:

| Job | What it does |
|---|---|
| `gitleaks` | **gitleaks-action v3.0.0** running gitleaks **v8.30.1** with `.gitleaks.toml`, on a full-history checkout |
| `shellcheck` | lints every shell script, including the hooks |
| `tests` | runs `tests/run.sh` |

- It catches commits made in a clone where `setup-dev.sh` was never run, or made with `--no-verify`.
- **Every action is pinned by commit SHA**, with the version in a comment, never by tag. A tag can
  be moved to point at different code, and a SHA can't. That matters for a step that sees your
  whole history. When you upgrade, bump the SHA and the comment together, and keep the local gitleaks
  version the same as `GITLEAKS_VERSION` in CI, so the hook and CI agree.
- gitleaks-action needs a free `GITLEAKS_LICENSE` secret when the repository belongs to a GitHub
  **organization**. Personal repos don't need it.

### Scanning by hand: scan what is tracked, not the directory

`gitleaks dir .` scans **every file in the folder, including ignored ones**: `tmp-local/.env`, your
real webhook URLs, local tokens. It reports secrets that will never be committed. Worse, its report
(and any baseline you save from it) now holds those secrets, and that report is easy to paste into an
issue. To check what a commit or push would actually publish, scan the tracked set instead:

```bash
gitleaks git --redact .                         # every commit in the history

# only the tracked files, as staged in the index (ignored files are never copied)
tmp=$(mktemp -d) && git checkout-index -a --prefix="$tmp/" \
  && gitleaks dir --redact -c .gitleaks.toml "$tmp"; rm -rf "$tmp"
```

Or just rely on the pre-commit hook (staged changes) and CI (pushed commits), which only ever see
tracked content. Always pass `--redact` when you scan by hand.

The banned-terms list is **not** available in CI by design (see below). If you want CI to check it
too, store the list as a CI secret and write it to `tmp-local/banned-terms.txt` in the job. Never
commit it.

## `tmp-local/banned-terms.txt`

```text
# one term or extended regex per line; '#' starts a comment
example-corp
internal\.example\.com
/home/alice
team-alerts-channel
```

- **Why it is gitignored:** the list says exactly what you are trying to hide. Committing it would
  leak every name on it at once.
- Keep it short and specific. Overly broad terms (`api`, `prod`) make the hook noisy, and people
  start bypassing it.
- When a hit is legitimate (for example a public URL of the repo itself), narrow the pattern rather
  than deleting the term.

## Why `tmp*/` folders are gitignored

`.gitignore` contains `tmp*/`, `.env` and `state/`. That gives you one rule that is easy to remember:

- **Anything specific to your machine or organization goes in `tmp-local/`** (or any `tmp`-prefixed
  folder): real `fleet.yaml`, `.env` with webhook URLs, the banned-terms list, scratch notes with real
  names.
- Agents need a place to put reference material. If there is a safe place, they don't drop it in
  `docs/`. Tell them it is `tmp-local/`.
- Because the pattern is a prefix, `tmp-notes/`, `tmp-scratch/` and so on are all covered. Nobody
  has to remember to update `.gitignore`.

## Rules to put in CLAUDE.md

```markdown
## Secrets and sanitization
- Never commit secrets. If gitleaks or the banned-terms hook blocks a commit, fix the content.
  Never use `--no-verify`, and never edit the hook or `.gitleaks.toml` to get past it, without the
  user's approval.
- In this repo, never write company, product, customer or people names, internal hosts, emails,
  machine paths or channel names into tracked files. Use neutral placeholders (`service-a`,
  `example.com`, `API`). Anything specific goes in `tmp-local/`.
- Never print tokens or webhook URLs in output, notes, tickets or stage artifacts.
```

## If something leaked anyway

1. **Rotate the secret first.** Assume anything that reached a remote has been copied. Rewriting
   history does not un-leak it.
2. Then remove it from history (for example with `git filter-repo`) and force-push, coordinated with
   everyone who has a clone.
3. Add a pattern (gitleaks rule or banned term) so the same thing is caught next time.
