#!/usr/bin/env bash
# Ticket adapter for the delivery-harness skill. The default backend is GitHub Issues via `gh`.
# To use Jira, Linear or your own tracker, replace the function bodies and keep the verbs.
#
#   tickets.sh get     <key>
#   tickets.sh list    '<search>'
#   tickets.sh create  @create.json      # {"title": "...", "body": "...", "labels": ["..."], "parent": <key>?}
#   tickets.sh status  <key> backlog|todo|in_progress|awaiting_decision|done|cancelled
#   tickets.sh comment <key> @comment.md # or a literal string
#
# Repo: TICKETS_REPO=<owner>/<repo>, or the current directory's GitHub remote.
# Auth: whatever `gh` is logged in as. It acts AS that person. Tokens are never printed.
# Statuses map to labels named `status:<status>`. done and cancelled also close the issue.
set -euo pipefail

REPO_ARGS=()
[ -n "${TICKETS_REPO:-}" ] && REPO_ARGS=(--repo "$TICKETS_REPO")
STATUSES="backlog todo in_progress awaiting_decision done cancelled"

die() { echo "tickets.sh: $*" >&2; exit 2; }
body_arg() { # @file → file contents, otherwise the literal string
  case "$1" in @*) cat -- "${1#@}" ;; *) printf '%s' "$1" ;; esac
}

cmd=${1:-}; shift || true
case "$cmd" in
  get)
    [ $# -ge 1 ] || die "usage: get <key>"
    gh issue view "$1" "${REPO_ARGS[@]}" --comments
    ;;
  list)
    gh issue list "${REPO_ARGS[@]}" --search "${1:-}" --limit 50 \
      --json number,title,state,labels --jq '.[] | "\(.number)\t\(.state)\t\(.title)"'
    ;;
  create)
    [ $# -ge 1 ] || die "usage: create @create.json"
    json=$(body_arg "$1")
    title=$(jq -r '.title' <<<"$json"); body=$(jq -r '.body // ""' <<<"$json")
    parent=$(jq -r '.parent // empty' <<<"$json")
    [ -n "$parent" ] && body=$(printf '%s\n\nParent: #%s' "$body" "$parent")
    label_args=()
    while IFS= read -r l; do [ -n "$l" ] && label_args+=(--label "$l"); done < <(jq -r '.labels[]? // empty' <<<"$json")
    gh issue create "${REPO_ARGS[@]}" --title "$title" --body "$body" "${label_args[@]}"
    ;;
  status)
    [ $# -ge 2 ] || die "usage: status <key> <$STATUSES>"
    key=$1 new=$2
    [[ " $STATUSES " == *" $new "* ]] || die "unknown status '$new' (use: $STATUSES)"
    rm_args=()
    for s in $STATUSES; do [ "$s" = "$new" ] || rm_args+=(--remove-label "status:$s"); done
    gh label create "status:$new" "${REPO_ARGS[@]}" --force >/dev/null 2>&1 || true
    gh issue edit "$key" "${REPO_ARGS[@]}" --add-label "status:$new" "${rm_args[@]}" >/dev/null
    case "$new" in
      done)      gh issue close "$key" "${REPO_ARGS[@]}" --reason completed >/dev/null ;;
      cancelled) gh issue close "$key" "${REPO_ARGS[@]}" --reason "not planned" >/dev/null ;;
      *)         gh issue reopen "$key" "${REPO_ARGS[@]}" >/dev/null 2>&1 || true ;;
    esac
    echo "#$key → $new"
    ;;
  comment)
    [ $# -ge 2 ] || die "usage: comment <key> @file|text"
    gh issue comment "$1" "${REPO_ARGS[@]}" --body "$(body_arg "$2")"
    ;;
  *)
    die "usage: get | list | create | status | comment  (see the header of this file)"
    ;;
esac
