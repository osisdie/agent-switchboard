#!/usr/bin/env bash
# expose-cloudflare.sh — publish board-serve's tunnel listener through a DEDICATED Cloudflare
# Tunnel, behind Cloudflare Access.
#
# Usage: scripts/expose-cloudflare.sh --hostname HOST --tunnel NAME [--port PORT] [--out PATH]
#                                     [--dry-run | --apply [--access-checked]]
#
#   --hostname HOST    public name for the board, e.g. board.example.com (a zone on your account)
#   --tunnel NAME      a tunnel used ONLY for the board (create it: cloudflared tunnel create NAME)
#   --port PORT        board-serve's tunnel listener (default: fleet.yaml serve.tunnel.port,
#                      else serve.port + 1, else 39411)
#   --out PATH         the config file this script owns (default:
#                      ~/.cloudflared/agent-switchboard-NAME.yml)
#   --dry-run          (default) print the config, the commands and the Access steps; change nothing
#   --apply            write --out and run `cloudflared tunnel route dns`. Refused unless a Cloudflare
#                      Access application covers HOST (all paths): checked through the API when
#                      CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are set, otherwise you check it
#                      in the dashboard and pass --access-checked.
#
# It never edits another config: not ~/.cloudflared/config.yml, not another tunnel's ingress. It
# refuses a tunnel that an existing cloudflared config already uses, and an --out file it did not
# write. Running the tunnel (cloudflared tunnel --config OUT run NAME) stays manual.
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
MARK="# written by agent-switchboard scripts/expose-cloudflare.sh — do not edit by hand"
host=""; tunnel=""; port=""; out=""; apply=0; access_checked=0
CF_DIR="${CLOUDFLARED_DIR:-$HOME/.cloudflared}"
die() { echo "expose-cloudflare: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --hostname) shift; host="${1:-}" ;;
    --tunnel) shift; tunnel="${1:-}" ;;
    --port) shift; port="${1:-}" ;;
    --out) shift; out="${1:-}" ;;
    --dry-run|-n) apply=0 ;;
    --apply) apply=1 ;;
    --access-checked) access_checked=1 ;;
    -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done
[ -n "$host" ] && [ -n "$tunnel" ] || die "--hostname and --tunnel are required (see --help)"
host="${host,,}"
[[ "$host" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || die "not a host name: $host"
[[ "$tunnel" =~ ^[A-Za-z0-9._-]+$ ]] || die "not a tunnel name: $tunnel"
if [ -z "$port" ]; then
  port="$(python3 "$REPO/lib/fleet.py" get serve.tunnel.port "" 2>/dev/null)"
  if [ -z "$port" ]; then
    port="$(python3 "$REPO/lib/fleet.py" get serve.port 39410 2>/dev/null)"
    case "$port" in ''|*[!0-9]*) port=39410 ;; esac
    port=$((port + 1))
  fi
fi
case "$port" in ''|*[!0-9]*) die "bad port: $port" ;; esac
[ -n "$out" ] || out="$CF_DIR/agent-switchboard-$tunnel.yml"
service="http://127.0.0.1:$port"

emails="$(python3 "$REPO/lib/fleet.py" get approvers 2>/dev/null | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
except ValueError:
    v = []
print("\n".join(sorted({str(x) for x in v if "@" in str(x)})))' 2>/dev/null)"

render_config() {  # render_config TUNNEL_ID CREDENTIALS_FILE
  cat <<EOF
$MARK
tunnel: $1
credentials-file: $2
ingress:
  - hostname: $host
    service: $service
  - service: http_status:404
EOF
}

# ------------------------------------------------------------------ plan
echo "== 1. a config file of its own: $out"
echo "  (your other cloudflared configs and tunnels are not touched)"
render_config "<id of tunnel $tunnel>" "$CF_DIR/<id of tunnel $tunnel>.json" | sed 's/^/    /'
echo
echo "== 2. commands"
echo "  cloudflared tunnel create $tunnel              # once, if the tunnel does not exist yet"
echo "  cloudflared tunnel route dns $tunnel $host"
echo "  cloudflared tunnel --config $out run $tunnel   # keep it running (tmux, a user service, ...)"
cat <<EOF

== 3. Cloudflare Access (Zero Trust dashboard) — BEFORE the tunnel carries traffic
  1. Access > Applications > Add an application > Self-hosted.
     Application domain: $host, path empty or "*" (the whole host). Session duration: e.g. 24h.
  2. Login methods: One-time PIN (email code), or your identity provider.
  3. Policy: action Allow; Include > Emails:
EOF
if [ -n "$emails" ]; then
  while IFS= read -r e; do echo "       - $e"; done <<<"$emails"
else
  echo "       - (the approver emails; none in fleet.yaml approvers yet)"
fi
cat <<EOF
     Access decides who reaches the board; board-serve still checks fleet.yaml approvers /
     release_managers on every answer, with the email from the verified Access token.
  4. Copy the application's "Application Audience (AUD) Tag" (Overview). The team domain
     (<team>.cloudflareaccess.com) is in the Zero Trust settings.

== 4. fleet.yaml (your config dir)
  approvers: [local, you@example.com]   # emails come from Access; "local" only if you list it
  serve:
    enabled: true
    port: $((port - 1))                       # local listener (browser on this machine)
    tunnel:
      enabled: true
      port: $port                       # the tunnel listener = the ingress target above
      hostname: $host
      team_domain: <team>.cloudflareaccess.com
      aud: <Application Audience (AUD) Tag>
  Then: bin/board-serve --check   (the tunnel listener refuses to start if anything is missing,
  including the python3 "cryptography" module)
EOF

if [ "$apply" = 0 ]; then
  echo
  echo "(dry run: nothing was changed. --apply writes $out and routes DNS, after the Access check.)"
  exit 0
fi

# ------------------------------------------------------------------ apply
echo
echo "== applying"
command -v cloudflared >/dev/null 2>&1 || die "cloudflared not found"

# 1. Access must already protect the hostname.
if [ -n "${CLOUDFLARE_API_TOKEN:-}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
  [[ "$CLOUDFLARE_ACCOUNT_ID" =~ ^[A-Za-z0-9]+$ ]] || die "CLOUDFLARE_ACCOUNT_ID looks wrong"
  apps="$(curl -sS -m 20 -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps?per_page=1000")" \
    || die "Access API request failed"
  match="$(python3 -c '
import json, sys
host = sys.argv[1]
try:
    data = json.load(sys.stdin)
except ValueError:
    print("error"); sys.exit()
if not data.get("success"):
    print("error"); sys.exit()
ok = {host, host + "/*", host + "/"}
for app in data.get("result") or []:
    doms = {str(app.get("domain") or "")} | {str(d) for d in app.get("self_hosted_domains") or []}
    doms |= {str((d or {}).get("uri") or "") for d in app.get("destinations") or [] if isinstance(d, dict)}
    if {d.lower() for d in doms} & ok:
        print("ok " + str(app.get("aud") or "")); sys.exit()
print("none")' "$host" <<<"$apps")"
  case "$match" in
    ok*) echo "  Access application found for $host (AUD ${match#ok })" ;;
    none) die "no Access application covers $host (all paths): create it first (step 3)" ;;
    *) die "could not read the Access applications (check the token's Access: Apps read permission)" ;;
  esac
elif [ "$access_checked" = 1 ]; then
  echo "  Access check: confirmed by you (--access-checked)"
else
  die "refusing: first make sure a Cloudflare Access application covers $host (all paths, step 3),
  then re-run with --access-checked. (Or set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID to check
  it through the API.)"
fi

# 2. The tunnel: must exist, and must not be used by any other cloudflared config.
tid="$(cloudflared tunnel list --output json 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    rows = []
for r in rows or []:
    if r.get("name") == sys.argv[1] and not str(r.get("deleted_at") or "").startswith(("1", "2")):
        print(r.get("id", "")); break' "$tunnel")"
[ -n "$tid" ] || die "tunnel '$tunnel' not found: create it with 'cloudflared tunnel create $tunnel' (a tunnel just for the board)"
[[ "$tid" =~ ^[A-Za-z0-9-]+$ ]] || die "unexpected tunnel id: $tid"
creds="$CF_DIR/$tid.json"
[ -f "$creds" ] || die "credentials file $creds not found (created by 'cloudflared tunnel create')"
for f in "$CF_DIR"/*.yml "$CF_DIR"/*.yaml; do
  [ -f "$f" ] || continue
  [ "$f" = "$out" ] && continue
  if grep -qE "^[[:space:]]*tunnel:[[:space:]]*[\"']?($tunnel|$tid)[\"']?[[:space:]]*$" "$f"; then
    die "tunnel '$tunnel' is already used by $f — use a tunnel of its own for the board"
  fi
done

# 3. Our own config file only.
if [ -e "$out" ] && ! head -n 1 "$out" | grep -qxF "$MARK"; then
  die "$out exists and was not written by this script — not touching it (choose --out)"
fi
mkdir -p "$(dirname "$out")"
tmp="$out.tmp.$$"
{ render_config "$tid" "$creds" > "$tmp" && mv -f "$tmp" "$out"; } || die "could not write $out"
echo "  wrote $out"
cloudflared tunnel --config "$out" ingress validate >/dev/null 2>&1 \
  || echo "  WARNING: 'cloudflared tunnel --config $out ingress validate' failed — check the file"

# 4. DNS.
cloudflared tunnel route dns "$tunnel" "$host" || echo "  route dns failed (it may already exist)"
echo "  Next: run 'cloudflared tunnel --config $out run $tunnel' and set fleet.yaml serve.tunnel (step 4)."
