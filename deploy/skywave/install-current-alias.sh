#!/usr/bin/env bash
# install-current-alias.sh - one-time: create the kannaka-brain-current ollama tag from the
# served version and register it in the gateway, then mint the colony key that lists the
# current alias and every kannaka-brain-v* (the gateway keys list literal aliases).
#   bash install-current-alias.sh <initial version tag>   e.g. kannaka-brain-v3
# Prints nothing secret; the minted key lands in ~/.grid-key-for-skywave (0600) for scp.
set -euo pipefail
V=${1:?initial version tag}
CFG=/srv/kax/gateway/config.yaml
ollama list | grep -q "^kannaka-brain-current" || ollama cp "$V" kannaka-brain-current
grep -q "model_name: kannaka-brain-current" $CFG || python3 - <<'PY'
p="/srv/kax/gateway/config.yaml"; s=open(p).read()
s=s.replace("litellm_settings:", """  - model_name: kannaka-brain-current
    litellm_params:
      model: ollama_chat/kannaka-brain-current
      api_base: http://172.18.0.1:11434
litellm_settings:""", 1)
open(p,"w").write(s)
PY
if [ -s ~/.grid-key-for-skywave ] && grep -q "model_name: kannaka-brain-current" $CFG; then
  echo "[install-current-alias] already installed: kannaka-brain-current -> $V; key at ~/.grid-key-for-skywave"
else
  docker restart kax-gateway >/dev/null && sleep 8
  M=$(grep '^LITELLM_MASTER_KEY=' /srv/kax/gateway/gateway.env | cut -d= -f2-)
  MODELS=$(ollama list | awk 'NR>1 && $1 ~ /^kannaka-brain/ {sub(/:latest$/,"",$1); print $1}' | sort -u | python3 -c 'import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()] + ["kannaka-brain", "kannaka-brain-current"]))')
  (umask 077; python3 - "$M" "grid-colony-one-$(date -u +%Y%m%dT%H%M%S)" "$MODELS" <<'PY' > ~/.grid-key-for-skywave.tmp
import json, sys, urllib.request
master, alias, models = sys.argv[1], sys.argv[2], sorted(set(json.loads(sys.argv[3])))
body = json.dumps({"key_alias": alias, "models": models, "max_budget": 5.0, "budget_duration": "30d", "rpm_limit": 60, "tpm_limit": 200000}).encode()
req = urllib.request.Request("http://127.0.0.1:4000/key/generate", data=body, headers={"Authorization": "Bearer " + master, "Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=30))["key"])
sys.stderr.write("minted " + alias + " for " + ", ".join(models) + "\n")
PY
  )
  [ -s ~/.grid-key-for-skywave.tmp ] && mv ~/.grid-key-for-skywave.tmp ~/.grid-key-for-skywave && chmod 600 ~/.grid-key-for-skywave
  echo "[install-current-alias] kannaka-brain-current -> $V; key at ~/.grid-key-for-skywave"
fi
