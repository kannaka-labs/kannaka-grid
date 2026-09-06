#!/usr/bin/env bash
# rotate-mind-key.sh - mint a fresh LiteLLM virtual key for the colony mind and
# restart it. Run when the relay logs "Budget has been exceeded" (HTTP 429):
# deploy.sh only mints a key when the mind's config is not yet wired to the
# gateway, so a drained key is rotated here, never by re-running the deploy.
#
#   GRID_MIND_BUDGET=2.0 bash ~/grid-deploy/rotate-mind-key.sh
#
# Never prints a key. Exit 4 = gateway refused the mint.
set -euo pipefail
ROOT=/srv/grid
COLONY=${COLONY_SLUG:-colony-one}
MEM=$ROOT/$COLONY/memory
GATEWAY_ENV=${GATEWAY_ENV:-/srv/kax/gateway/gateway.env}
log() { echo "[rotate-mind-key] $*"; }
die() { echo "[rotate-mind-key] FAIL: $1" >&2; exit "${2:-1}"; }

[ -f "$MEM/config.toml" ] || die "no $MEM/config.toml; run deploy.sh first" 5
MASTER=$(grep '^LITELLM_MASTER_KEY=' "$GATEWAY_ENV" | cut -d= -f2-)
[ -n "$MASTER" ] || die "LITELLM_MASTER_KEY empty in $GATEWAY_ENV" 4
OLD_ALIAS=$(python3 - "$MEM/config.toml" <<'PY'
import re, sys, urllib.request, json
key = re.search(r'^api_key\s*=\s*"([^"]+)"', open(sys.argv[1]).read(), re.M)
if not key: print(""); sys.exit()
req = urllib.request.Request("http://127.0.0.1:4000/key/info", headers={"Authorization": "Bearer " + key.group(1)})
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        i = json.load(r).get("info", {}); print(f'{i.get("key_alias")} spend={i.get("spend")} max={i.get("max_budget")}')
except Exception as e:
    print(f"(old key info unavailable: {e})")
PY
)
log "old key: $OLD_ALIAS"
ALIAS="grid-$COLONY-$(date -u +%Y%m%dT%H%M%S)"
VKEY=$(python3 - "$MASTER" "$ALIAS" "${GRID_MIND_BUDGET:-2.0}" <<'PY'
import json, sys, urllib.error, urllib.request
master, alias, budget = sys.argv[1:4]
body = json.dumps({"key_alias": alias, "models": ["agent-brain"], "max_budget": float(budget),
                   "budget_duration": "30d", "rpm_limit": 30, "tpm_limit": 40000}).encode()
req = urllib.request.Request("http://127.0.0.1:4000/key/generate", data=body,
                             headers={"Authorization": "Bearer " + master, "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(json.load(r)["key"])
except urllib.error.HTTPError as e:
    sys.stderr.write(f"key mint failed: HTTP {e.code}: {e.read()[:400].decode(errors='replace')}\n")
    sys.exit(4)
PY
)
[ -n "$VKEY" ] || die "key mint returned nothing" 4
python3 - "$MEM/config.toml" "$VKEY" <<'PY'
import re, sys
path, vkey = sys.argv[1:3]
src = open(path, encoding="utf-8").read()
new, n = re.subn(r'^(api_key\s*=\s*)"[^"]*"', lambda m: m.group(1) + '"' + vkey + '"', src, count=1, flags=re.M)
if n != 1:
    sys.stderr.write("no api_key line in the mind config\n"); sys.exit(5)
open(path, "w", encoding="utf-8").write(new)
PY
chmod 600 "$MEM/config.toml"
sudo systemctl restart grid-mind.service
sleep 2
systemctl is-active --quiet grid-mind.service || die "grid-mind did not come back: $(systemctl status grid-mind --no-pager | tail -3)" 6
log "minted $ALIAS (budget \$${GRID_MIND_BUDGET:-2.0}/30d) and restarted grid-mind"
