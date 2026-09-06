#!/usr/bin/env bash
# set-mind-model.sh - point the colony mind at a different OpenAI-compatible
# gateway/model and restart everything that names the model.
#
#   bash ~/grid-deploy/set-mind-model.sh <base_url> <model> <key_file>
#   e.g. bash ~/grid-deploy/set-mind-model.sh http://10.30.0.30:4000/v1 kannaka-brain ~/.grid-debain2-key
#
# Rewrites only the [llm] base_url / model / api_key lines of the mind's
# config.toml (line-oriented, never regenerates the file), sets
# MIND_MODEL_ALIAS in grid.env so verdict rows name the model that authored the
# proposal, then restarts grid-mind, grid-odin and grid-bridge. Never prints a key.
set -euo pipefail
BASE_URL=${1:?base_url}; MODEL=${2:?model}; KEY_FILE=${3:?key_file}
ROOT=/srv/grid
COLONY=${COLONY_SLUG:-colony-one}
CFG=$ROOT/$COLONY/memory/config.toml
ENVF=$ROOT/grid.env
log() { echo "[set-mind-model] $*"; }
die() { echo "[set-mind-model] FAIL: $1" >&2; exit "${2:-1}"; }
[ -f "$CFG" ] || die "no $CFG; run deploy.sh first" 5
[ -s "$KEY_FILE" ] || die "key file $KEY_FILE missing or empty" 5
cp -p "$CFG" "$CFG.bak-$(date -u +%Y%m%dT%H%M%S)"
python3 - "$CFG" "$BASE_URL" "$MODEL" "$(tr -d '\n' < "$KEY_FILE")" <<'PY'
import re, sys
path, base_url, model, key = sys.argv[1:5]
want = {"base_url": base_url, "model": model, "api_key": key}
out, section, seen = [], None, set()
for line in open(path, encoding="utf-8").read().splitlines():
    s = line.strip()
    m = re.match(r"^\[(\w+)\]", s)
    if m:
        if section == "llm":
            for k in want:
                if k not in seen: out.append(f'{k} = "{want[k]}"')
        section = m.group(1)
    elif section == "llm" and "=" in s and not s.startswith("#"):
        k = s.split("=", 1)[0].strip()
        if k in want:
            out.append(f'{k} = "{want[k]}"'); seen.add(k); continue
    out.append(line)
if section == "llm":
    for k in want:
        if k not in seen: out.append(f'{k} = "{want[k]}"')
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
chmod 600 "$CFG"
grep -q '^MIND_MODEL_ALIAS=' "$ENVF" && sed -i "s#^MIND_MODEL_ALIAS=.*#MIND_MODEL_ALIAS=$MODEL#" "$ENVF" || echo "MIND_MODEL_ALIAS=$MODEL" >> "$ENVF"
sudo systemctl restart grid-mind.service grid-odin.service grid-bridge.service
sleep 3
for u in grid-mind grid-odin grid-bridge; do
  systemctl is-active --quiet $u.service || die "$u did not come back: $(systemctl status $u --no-pager | tail -3)" 6
done
log "mind now asks $MODEL at $BASE_URL; MIND_MODEL_ALIAS=$MODEL; grid-mind/odin/bridge restarted"
grep -nE '^(provider|base_url|model) ' "$CFG" | sed 's/^/[set-mind-model] /'
