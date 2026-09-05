#!/usr/bin/env bash
# deploy/skywave/deploy.sh — run the Skywave runbook (deploy/README.md §0–§3)
# unattended, as user `nick`, on the host itself. Idempotent: every step checks
# before it acts, so re-running after a partial failure is safe.
#
# Exit codes: 0 done · 3 needs passwordless sudo · 4 gateway/master key missing
#             5 kannaka binary missing · 6 a unit failed to start
set -euo pipefail

REPO_URL="${GRID_REPO_URL:-https://github.com/NickFlach/kannaka-grid.git}"
BRANCH="${GRID_BRANCH:-main}"
ROOT=/srv/grid
CHECKOUT=$ROOT/kannaka-grid
VENV=$ROOT/venv
COLONY=colony-one
MEM=$ROOT/$COLONY/memory
KANNAKA="$HOME/.local/bin/kannaka"
GATEWAY_ENV=/srv/kax/gateway/gateway.env
GATEWAY=http://127.0.0.1:4000

log() { printf '[deploy] %s\n' "$*" >&2; }
die() { printf '[deploy] FAIL: %s\n' "$*" >&2; exit "${2:-1}"; }

# ---- preflight ---------------------------------------------------------------
sudo -n true 2>/dev/null || die "passwordless sudo is required for systemd unit install (add: nick ALL=(root) NOPASSWD: ALL, or at least /usr/bin/cp, /usr/bin/systemctl, /usr/bin/install)" 3
[ -x "$KANNAKA" ] || die "kannaka binary not at $KANNAKA" 5
[ -r "$GATEWAY_ENV" ] || die "cannot read $GATEWAY_ENV (need the LiteLLM master key to mint the mind's virtual key)" 4
command -v python3 >/dev/null || die "python3 missing" 1
command -v git >/dev/null || die "git missing" 1
log "host $(hostname) · $(python3 --version) · kannaka $($KANNAKA --version | head -1)"

# ---- §0 prerequisites ----------------------------------------------------------
sudo install -d -o nick -g nick $ROOT
if [ ! -d $CHECKOUT/.git ]; then
  log "cloning $REPO_URL ($BRANCH)"
  git clone --recurse-submodules -b "$BRANCH" "$REPO_URL" $CHECKOUT
else
  log "updating checkout"
  git -C $CHECKOUT fetch -q origin && git -C $CHECKOUT checkout -q "$BRANCH" && git -C $CHECKOUT pull -q --ff-only
  git -C $CHECKOUT submodule update --init -q
fi
[ -e $ROOT/thegrid ] || ln -s $CHECKOUT/thegrid $ROOT/thegrid
if [ ! -x $VENV/bin/python ]; then
  log "creating venv"
  python3 -m venv $VENV
fi
$VENV/bin/pip install -q --upgrade pip
$VENV/bin/pip install -q -e "$CHECKOUT[dev]" -e $ROOT/thegrid
install -d -o nick -g nick $ROOT/$COLONY/state $ROOT/$COLONY/state/operator $MEM
if [ ! -f $ROOT/grid.env ]; then
  cp $CHECKOUT/deploy/grid.env.example $ROOT/grid.env && chmod 600 $ROOT/grid.env
  log "wrote $ROOT/grid.env from the example"
fi
$VENV/bin/python -c "import grid, src.colony.live" && log "§0 receipt: grid and thegrid import" || die "§0 import check failed" 1

# ---- §1 mind -------------------------------------------------------------------
if [ ! -f $MEM/config.toml ]; then
  [ -f "$HOME/.kannaka/config.toml" ] || die "no $HOME/.kannaka/config.toml to derive the mind's config from" 5
  MASTER=$(grep '^LITELLM_MASTER_KEY=' $GATEWAY_ENV | cut -d= -f2-)
  [ -n "$MASTER" ] || die "LITELLM_MASTER_KEY empty in $GATEWAY_ENV" 4
  log "minting a LiteLLM virtual key for grid-$COLONY"
  VKEY=$(python3 - "$MASTER" <<'PY'
import json, sys, urllib.request
master = sys.argv[1]
body = json.dumps({"key_alias": "grid-colony-one", "models": ["agent-brain"], "max_budget": 0.5,
                   "budget_duration": "30d", "rpm_limit": 30, "tpm_limit": 20000}).encode()
req = urllib.request.Request("http://127.0.0.1:4000/key/generate", data=body,
                             headers={"Authorization": "Bearer " + master, "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=30) as r:
    print(json.load(r)["key"])
PY
)
  [ -n "$VKEY" ] || die "key mint returned nothing" 4
  log "deriving $MEM/config.toml from the host config"
  python3 - "$HOME/.kannaka/config.toml" "$MEM/config.toml" "$MEM" "$VKEY" <<'PY'
import re, sys
src, dst, mem, vkey = sys.argv[1:5]
text = open(src).read()
def set_key(section, key, value):
    global text
    pat = re.compile(r'(\[' + re.escape(section) + r'\][^\[]*?^' + re.escape(key) + r'\s*=\s*)(.*)$', re.M | re.S)
    if pat.search(text):
        text = pat.sub(lambda m: m.group(1) + value, text, count=1)
    else:
        text = re.sub(r'(\[' + re.escape(section) + r'\]\n)', lambda m: m.group(1) + f'{key} = {value}\n', text, count=1)
set_key("agent", "id", '"grid-colony-one"')
set_key("agent", "display_name", '"Grid: colony-one"')
set_key("agent", "kind", '"agent"')
set_key("agent", "persona", '"The mind of an artificial-life colony. You remember its epochs, storms, lineages and the genomes that worked. You answer briefly and cite ticks and opcodes."')
set_key("llm", "provider", '"openai"')
set_key("llm", "base_url", '"http://127.0.0.1:4000/v1"')
set_key("llm", "model", '"agent-brain"')
set_key("llm", "api_key", f'"{vkey}"')
set_key("hrm", "path", f"'{mem}/kannaka.hrm'")
set_key("swarm", "role", '"worker"')
open(dst, "w").write(text)
PY
  chmod 600 $MEM/config.toml
fi
grep -q 'id = "grid-colony-one"' $MEM/config.toml || die "mind config does not carry agent id grid-colony-one" 1
if [ ! -f $MEM/kannaka.hrm ]; then
  log "seeding the mind's store"
  KANNAKA_DATA_DIR=$MEM $KANNAKA remember "grid-colony-one mind initialised on $(hostname) $(date -u +%FT%TZ)" --category grid --tags colony-one,init >/dev/null
fi
sudo cp $CHECKOUT/deploy/systemd/grid-mind.service $CHECKOUT/deploy/systemd/grid-mind-dream.service $CHECKOUT/deploy/systemd/grid-mind-dream.timer /etc/systemd/system/
SUDOERS=/etc/sudoers.d/grid-mind-restart
if ! sudo test -f $SUDOERS; then
  echo 'nick ALL=(root) NOPASSWD: /usr/bin/systemctl restart grid-mind.service' | sudo tee $SUDOERS >/dev/null
  sudo chmod 440 $SUDOERS
fi
sudo systemctl daemon-reload
sudo systemctl enable --now grid-mind.service grid-mind-dream.timer
sleep 3
systemctl is-active --quiet grid-mind.service || die "grid-mind.service is not active: $(systemctl status grid-mind.service --no-pager | tail -5)" 6
log "§1 receipt: grid-mind active"

# ---- §2 colony -----------------------------------------------------------------
sudo cp $CHECKOUT/deploy/systemd/thegrid-colony.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now thegrid-colony.service
for i in $(seq 1 30); do
  if curl -fs 127.0.0.1:8787/api/state >/dev/null 2>&1; then break; fi; sleep 1
done
curl -fs 127.0.0.1:8787/api/state >/dev/null || die "colony observer not answering on 127.0.0.1:8787: $(journalctl -u thegrid-colony -n 8 --no-pager)" 6
HEAT=$(journalctl -u thegrid-colony --no-pager | grep -o 'heat source: [a-z0-9]*' | tail -1)
log "§2 receipt: colony up ($HEAT) · $(curl -fs 127.0.0.1:8787/api/state | python3 -c 'import json,sys; s=json.load(sys.stdin); print(s["name"], "epoch", s["epoch"], "tick", s["tick"], "pop", s["population"])')"

# ---- §3 bridge + relay ---------------------------------------------------------
sudo cp $CHECKOUT/deploy/systemd/grid-bridge.service $CHECKOUT/deploy/systemd/grid-odin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now grid-bridge.service grid-odin.service
sleep 5
for u in grid-bridge grid-odin; do
  systemctl is-active --quiet $u.service || die "$u.service not active: $(journalctl -u $u -n 8 --no-pager)" 6
done
log "§3 receipt: bridge and relay active"
log "DONE — run deploy/skywave/receipts.sh to collect the six acceptance receipts"
