# Contributing

Thanks for helping. Keep changes small and focused, and keep the repo generic.

## Before you commit

- Run `scripts/setup-dev.sh` once per clone. It enables the git hooks (gitleaks + banned terms).
- Create `tmp-local/banned-terms.txt` with the names that must never appear in the repo. See
  [Secrets & sanitization](README.md#secrets--sanitization).
- No company, product or person names, real hostnames, emails or machine paths. Use
  `example.com` and placeholders such as `API`, `WEB`, `<your id>`.
- Commit messages follow the conventional style (`feat:`, `fix:`, `docs:`, `chore:` …).

## Tests

```bash
export BOARD_HOME="$(mktemp -d)"
tests/run.sh -v && tests/test_notify.sh -v && tests/test_notify_controls.sh -v && tests/test_serve.sh -v && tests/test_away.sh -v
python3 tests/test_resources.py -v
python3 tests/test_guard.py -v
python3 tests/test_cards.py -v
python3 tests/test_rc.py -v
python3 tests/test_pomodoro.py -v
python3 tests/test_scheduler.py -v
python3 tests/test_tickets.py -v
python3 tests/test_tickets_take.py -v
python3 scripts/check-readme-sync.py
```

The commands above are the ones CI runs (plus gitleaks and shellcheck). Page and site
checks (`tests/test_page_*.py`, `tests/test_site.py`) need python playwright and skip
without it; they are not run in CI.

Shell scripts must pass `shellcheck -s bash` (CI checks the files listed in
`.github/workflows/ci.yml`; add new scripts to that list).

## Documentation

- When you change README.md, update README.zh-TW.md in the same PR, or say in the PR that it lags.
- If the translation lags, still mirror any added, removed or moved `##` / `###` heading in
  README.zh-TW.md (English text is fine for now). CI runs `scripts/check-readme-sync.py`, which
  fails when the heading count, order or inline code in headings differ.
- README.md is the source of truth.
