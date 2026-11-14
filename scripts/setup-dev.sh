#!/usr/bin/env bash
# setup-dev.sh — one-time (idempotent) developer setup for this clone.
#
#   - git config core.hooksPath .githooks   (versioned pre-commit: gitleaks + banned terms)
#   - checks that gitleaks is installed
#   - warns when tmp-local/banned-terms.txt is missing
set -u

case "${1:-}" in
  -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
esac

root="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "setup-dev: not inside a git clone" >&2; exit 1; }
cd "$root" || exit 1
rc=0

git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null
echo "ok    core.hooksPath = $(git config core.hooksPath)"

if command -v gitleaks >/dev/null 2>&1; then
  echo "ok    gitleaks $(gitleaks version 2>/dev/null)"
else
  rc=1
  cat >&2 <<'MSG'
FAIL  gitleaks is not installed; the pre-commit hook will block commits until it is.
      Install a release binary from https://github.com/gitleaks/gitleaks/releases
      (verify it against the release's checksums file), e.g. into ~/.local/bin,
      or use your package manager (brew install gitleaks, ...).
MSG
fi

if [ -f tmp-local/banned-terms.txt ]; then
  n="$(grep -cvE '^[[:space:]]*(#|$)' tmp-local/banned-terms.txt)"
  echo "ok    tmp-local/banned-terms.txt ($n terms)"
else
  cat >&2 <<'MSG'
warn  tmp-local/banned-terms.txt is missing; the banned-terms check will be skipped.
      Create it (it is gitignored) with one term or extended regex per line, e.g.:
        # company, product, hosts, machine paths
        example-corp
        internal\.example\.com
        /home/alice
MSG
fi
exit "$rc"
