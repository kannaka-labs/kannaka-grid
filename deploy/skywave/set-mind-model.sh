#!/usr/bin/env bash
# set-mind-model.sh - point the colony mind at a different OpenAI-compatible
# gateway/model and restart everything that names the model.
#
#   bash ~/grid-deploy/set-mind-model.sh <base_url> <model> <key_file> [--via-daemon|--daemon-only|--relay-only]
#   e.g. bash ~/grid-deploy/set-mind-model.sh http://10.30.0.30:4000/v1 kannaka-brain ~/.grid-debain2-key
#
# Rewrites only the [llm] base_url / model / api_key lines of the mind's
# config.toml (line-oriented, never regenerates the file), sets
# MIND_MODEL_ALIAS in grid.env so verdict rows name the model that authored the
# proposal, and points the relay's asks straight at the gateway (MIND_LLM_*):
# the serve daemon is single-threaded and a model that needs minutes per answer
# makes it miss NATS pings and lose the reply. Pass --via-daemon to route the
# relay's asks through `kannaka swarm serve` instead (fine for a hosted model).
# --daemon-only rewrites just the daemon's config (the chronicle's model) and
# leaves the relay's MIND_LLM_* route and MIND_MODEL_ALIAS alone, so the relay
# can run on the local brain while the chronicle keeps a fast hosted model.
# --relay-only is the mirror image: writes the relay's direct route and
# MIND_MODEL_ALIAS, leaves the daemon config alone, restarts only grid-odin.
# Then restarts grid-mind, grid-odin and grid-bridge (not grid-odin for
# --daemon-only). Never prints a key.
set -euo pipefail
BASE_URL=${1:?base_url}; MODEL=${2:?model}; KEY_FILE=${3:?key_file}; VIA=${4:-}
ROOT=/srv/grid
COLONY=${COLONY_SLUG:-colony-one}
CFG=$ROOT/$COLONY/memory/config.toml
ENVF=$ROOT/grid.env
log() { echo "[set-mind-model] $*"; }
die() { echo "[set-mind-model] FAIL: $1" >&2; exit "${2:-1}"; }
[ -f "$CFG" ] || die "no $CFG; run deploy.sh first" 5
[ -s "$KEY_FILE" ] || die "key file $KEY_FILE missing or empty" 5
setenv() { grep -q "^$1=" "$ENVF" && sed -i "s#^$1=.*#$1=$2#" "$ENVF" || echo "$1=$2" >> "$ENVF"; }
if [ "$VIA" = "--relay-only" ]; then
  cp -p "$KEY_FILE" "$ROOT/$COLONY/mind-llm.key" && chmod 600 "$ROOT/$COLONY/mind-llm.key"
  setenv MIND_MODEL_ALIAS "$MODEL"
  setenv MIND_LLM_URL "$BASE_URL"
  setenv MIND_LLM_MODEL "$MODEL"
  setenv MIND_LLM_KEY_FILE "$ROOT/$COLONY/mind-llm.key"
  sudo systemctl restart grid-odin.service
  sleep 2
  systemctl is-active --quiet grid-odin.service || die "grid-odin did not come back: $(systemctl status grid-odin --no-pager | tail -3)" 6
  log "relay now asks $MODEL at $BASE_URL directly; daemon untouched; grid-odin restarted"
  grep -E '^MIND_(MODEL_ALIAS|LLM_URL|LLM_MODEL|LLM_KEY_FILE)=' "$ENVF" | sed 's/^/[set-mind-model] /'
  exit 0
fi
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
if [ "$VIA" = "--daemon-only" ]; then
  sudo systemctl restart grid-mind.service grid-bridge.service
  sleep 3
  for u in grid-mind grid-bridge; do
    systemctl is-active --quiet $u.service || die "$u did not come back: $(systemctl status $u --no-pager | tail -3)" 6
  done
  log "daemon (chronicle) now asks $MODEL at $BASE_URL; relay route untouched; grid-mind/bridge restarted"
  grep -nE '^(provider|base_url|model) ' "$CFG" | sed 's/^/[set-mind-model] /'
  exit 0
fi
setenv MIND_MODEL_ALIAS "$MODEL"
if [ "$VIA" = "--via-daemon" ]; then
  sed -i '/^MIND_LLM_\(URL\|MODEL\|KEY_FILE\)=/d' "$ENVF"
  ROUTE="via kannaka swarm serve"
else
  cp -p "$KEY_FILE" "$ROOT/$COLONY/mind-llm.key" && chmod 600 "$ROOT/$COLONY/mind-llm.key"
  setenv MIND_LLM_URL "$BASE_URL"
  setenv MIND_LLM_MODEL "$MODEL"
  setenv MIND_LLM_KEY_FILE "$ROOT/$COLONY/mind-llm.key"
  ROUTE="direct from the relay (recall/remember stay on the daemon)"
fi
sudo systemctl restart grid-mind.service grid-odin.service grid-bridge.service
sleep 3
for u in grid-mind grid-odin grid-bridge; do
  systemctl is-active --quiet $u.service || die "$u did not come back: $(systemctl status $u --no-pager | tail -3)" 6
done
log "mind now asks $MODEL at $BASE_URL ($ROUTE); MIND_MODEL_ALIAS=$MODEL; grid-mind/odin/bridge restarted"
grep -E '^MIND_(MODEL_ALIAS|LLM_URL|LLM_MODEL|LLM_KEY_FILE)=' "$ENVF" | sed 's/^/[set-mind-model] /'
grep -nE '^(provider|base_url|model) ' "$CFG" | sed 's/^/[set-mind-model] /'
