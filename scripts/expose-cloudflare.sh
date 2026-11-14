#!/usr/bin/env bash
# expose-cloudflare.sh — publish the self-hosted board (bin/board-serve) through an existing
# Cloudflare Tunnel, behind Cloudflare Access.
#
# Usage: scripts/expose-cloudflare.sh --hostname HOST --tunnel NAME [--port PORT]
#                                     [--config PATH] [--dry-run | --apply]
#
#   --hostname HOST  public name for the board, e.g. board.example.com (a zone on your account)
#   --tunnel NAME    name (or id) of an existing cloudflared tunnel
#   --port PORT      board-serve port (default: fleet.yaml serve.port, else 39410)
#   --config PATH    cloudflared config (default: ~/.cloudflared/config.yml)
#   --dry-run        (default) print the ingress rule, the DNS command and the Access steps;
#                    change nothing
#   --apply          add the ingress rule to --config (after a timestamped backup; never
#                    touches other rules) and run `cloudflared tunnel route dns`
#
# Without --apply nothing is written and no cloudflared command that changes state is run.
# Restarting cloudflared and creating the Access application stay manual (printed steps).
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
host=""; tunnel=""; port=""; config="$HOME/.cloudflared/config.yml"; apply=0
die() { echo "expose-cloudflare: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --hostname) shift; host="${1:-}" ;;
    --tunnel) shift; tunnel="${1:-}" ;;
    --port) shift; port="${1:-}" ;;
    --config) shift; config="${1:-}" ;;
    --dry-run|-n) apply=0 ;;
    --apply) apply=1 ;;
    -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done
[ -n "$host" ] && [ -n "$tunnel" ] || die "--hostname and --tunnel are required (see --help)"
[[ "$host" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] \
  || die "not a host name: $host"
[[ "$tunnel" =~ ^[A-Za-z0-9._-]+$ ]] || die "not a tunnel name: $tunnel"
[ -n "$port" ] || port="$(python3 "$REPO/lib/fleet.py" get serve.port 39410 2>/dev/null)"
case "$port" in ''|*[!0-9]*) die "bad port: $port" ;; esac
host="${host,,}"
service="http://127.0.0.1:$port"

# Approver emails from fleet.yaml, for the Access policy.
emails="$(python3 "$REPO/lib/fleet.py" get approvers 2>/dev/null | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
except ValueError:
    v = []
print("\n".join(sorted({str(x) for x in v if "@" in str(x)})))' 2>/dev/null)"

# ------------------------------------------------------------------ 1. ingress rule
echo "== 1. cloudflared ingress (tunnel: $tunnel, config: $config)"
cat <<EOF
  Add this rule above the catch-all "- service: http_status:404":

    ingress:
      - hostname: $host
        service: $service
      # ... your other rules stay as they are ...
      - service: http_status:404

EOF

plan="$(python3 - "$config" "$host" "$service" <<'PY'
import sys
path, host, service = sys.argv[1:4]
try:
    import yaml
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
except FileNotFoundError:
    print("missing"); sys.exit()
except Exception as exc:
    print("error " + str(exc).replace("\n", " ")); sys.exit()
rules = cfg.get("ingress") if isinstance(cfg, dict) else None
if not isinstance(rules, list) or not rules:
    print("noingress"); sys.exit()
for r in rules:
    if isinstance(r, dict) and str(r.get("hostname", "")).lower() == host:
        print("same" if r.get("service") == service else "other " + str(r.get("service")))
        sys.exit()
print("add")
PY
)"
case "$plan" in
  missing) echo "  $config does not exist: write it (with tunnel: and credentials-file:) or pass --config." ;;
  error*) echo "  cannot read $config: ${plan#error }" ;;
  noingress) echo "  $config has no ingress list: add the rule above by hand." ;;
  same) echo "  $config already routes $host to $service — nothing to add." ;;
  other*) echo "  $config already routes $host to ${plan#other } — left alone; fix it by hand." ;;
  add) echo "  $config: the rule for $host is not there yet." ;;
esac

# ------------------------------------------------------------------ 2. DNS
echo
echo "== 2. DNS"
echo "  cloudflared tunnel route dns $tunnel $host"

if [ "$apply" = 1 ]; then
  echo
  echo "== applying"
  if [ "$plan" = add ]; then
    bak="$config.bak.$(date +%Y%m%d%H%M%S)"
    cp -p -- "$config" "$bak" || die "backup failed"
    python3 - "$config" "$host" "$service" <<'PY' || { cp -p -- "$bak" "$config"; die "could not update $config (restored)"; }
import os, sys, yaml
path, host, service = sys.argv[1:4]
with open(path, encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh)
rules = cfg["ingress"]
# Insert before the trailing catch-all (a rule without hostname/path), else at the end.
at = len(rules)
if rules and isinstance(rules[-1], dict) and not rules[-1].get("hostname") and not rules[-1].get("path"):
    at = len(rules) - 1
rules.insert(at, {"hostname": host, "service": service})
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    yaml.safe_dump(cfg, fh, sort_keys=False)
os.replace(tmp, path)
PY
    echo "  $config updated (backup: $bak)"
    if command -v cloudflared >/dev/null 2>&1; then
      cloudflared tunnel --config "$config" ingress validate || echo "  WARNING: ingress validate failed — check $config (backup: $bak)"
    fi
  else
    echo "  ingress: nothing written ($plan)"
  fi
  command -v cloudflared >/dev/null 2>&1 || die "cloudflared not found"
  cloudflared tunnel route dns "$tunnel" "$host" || echo "  route dns failed (it may already exist)"
  echo "  Restart cloudflared yourself (however it runs here) so it reads the new rule."
else
  echo
  echo "  (dry run: nothing was changed; re-run with --apply to add the rule and the DNS route)"
fi

# ------------------------------------------------------------------ 3. Access
cat <<EOF

== 3. Cloudflare Access (Zero Trust dashboard, manual)
  1. Access > Applications > Add an application > Self-hosted.
     Application domain: $host   Session duration: e.g. 24h.
  2. Login methods: One-time PIN (email code), or your identity provider.
  3. Policy: action Allow; Include > Emails:
EOF
if [ -n "$emails" ]; then
  while IFS= read -r e; do echo "       - $e"; done <<<"$emails"
else
  echo "       - (the approver emails; none found in fleet.yaml approvers yet)"
fi
cat <<EOF
     Only these people get through Access. board-serve still checks fleet.yaml approvers /
     release_managers on every answer.
  4. Save, then open the application's Overview and copy the "Application Audience (AUD) Tag".
     The team domain (<team>.cloudflareaccess.com) is shown in the Zero Trust settings.

== 4. fleet.yaml (in your config dir)
  approvers: [local, you@example.com]      # "local" = a browser on this machine
  serve:
    enabled: true
    port: $port
    hostnames: [$host]
    cf_access:
      team_domain: <team>.cloudflareaccess.com
      aud: <Application Audience (AUD) Tag>

  JWT verification needs the python3 "cryptography" module. Check with: bin/board-serve --check
  (look for "cf jwt   verify"). Without it, remote requests are refused unless you set
  serve.trust_cf_headers: true (see README, "Self-hosted board").
EOF
