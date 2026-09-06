#!/usr/bin/env bash
# deploy/skywave/deploy.sh — run the Skywave runbook (deploy/README.md §0–§5)
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
install -d -o nick -g nick $ROOT/$COLONY/state $ROOT/$COLONY/state/operator $MEM $ROOT/$COLONY/verdicts $ROOT/$COLONY/verdicts/prompts
chmod 700 $ROOT/$COLONY/verdicts
if [ ! -f $ROOT/grid.env ]; then
  cp $CHECKOUT/deploy/grid.env.example $ROOT/grid.env && chmod 600 $ROOT/grid.env
  log "wrote $ROOT/grid.env from the example"
fi
grep -q '^VERDICTS_DIR=' $ROOT/grid.env || printf 'VERDICTS_DIR=%s/%s/verdicts\nMIND_MODEL_ALIAS=agent-brain\n' "$ROOT" "$COLONY" >> $ROOT/grid.env
$VENV/bin/python -c "import grid, src.colony.live" && log "§0 receipt: grid and thegrid import" || die "§0 import check failed" 1

# ---- §1 mind -------------------------------------------------------------------
# (Re)derive the mind's config whenever it is missing or not yet wired to the
# gateway, so a partial earlier run is repaired rather than trusted.
if ! grep -q '^provider = "openai"' $MEM/config.toml 2>/dev/null; then
  [ -f "$HOME/.kannaka/config.toml" ] || die "no $HOME/.kannaka/config.toml to derive the mind's config from" 5
  MASTER=$(grep '^LITELLM_MASTER_KEY=' $GATEWAY_ENV | cut -d= -f2-)
  [ -n "$MASTER" ] || die "LITELLM_MASTER_KEY empty in $GATEWAY_ENV" 4
  log "minting a LiteLLM virtual key for grid-$COLONY"
  # key_alias must be unique per gateway, so a re-run mints a fresh, dated key
  # (the earlier one just expires with its 30-day budget).
  # GRID_MIND_BUDGET is USD per 30 days on the gateway. The relay asks the mind
  # at roughly every fifth birth (~50 asks/hour on colony-one); on Sonnet that
  # is ~$0.009 per ask, so 0.5 lasted an hour. Pick the model on the gateway
  # side (agent-brain) with the budget in mind; the default here is a cap, not
  # a plan.
  VKEY=$(python3 - "$MASTER" "grid-colony-one-$(date -u +%Y%m%dT%H%M%S)" "${GRID_MIND_BUDGET:-2.0}" <<'PY'
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
  log "deriving $MEM/config.toml from the host config"
  python3 - "$HOME/.kannaka/config.toml" "$MEM/config.toml" "$MEM" "$VKEY" <<'PY'
import sys
src, dst, mem, vkey = sys.argv[1:5]
WANT = {
    "agent": {"id": '"grid-colony-one"', "display_name": '"Grid: colony-one"', "kind": '"agent"',
              "persona": '"The mind of an artificial-life colony. You remember its epochs, storms, lineages and the genomes that worked. You answer briefly and cite ticks and opcodes."'},
    "llm": {"provider": '"openai"', "base_url": '"http://127.0.0.1:4000/v1"', "model": '"agent-brain"', "api_key": f'"{vkey}"'},
    "swarm": {"enabled": "true", "nats_url": '"nats://swarm.ninja-portal.com:4222"', "role": '"worker"'},
    "hrm": {"path": f"'{mem}/kannaka.hrm'"},
}
out, section, pending = [], None, {}
def flush():
    for k, v in pending.items():
        out.append(f"{k} = {v}\n")
for line in open(src):
    s = line.strip()
    if s.startswith("[") and s.endswith("]"):
        flush()
        section = s[1:-1]
        pending = dict(WANT.get(section, {}))
        out.append(line)
        continue
    if section in WANT and "=" in s and not s.startswith("#"):
        key = s.split("=", 1)[0].strip()
        if key in pending:
            out.append(f"{key} = {pending.pop(key)}\n")
            continue
    out.append(line)
flush()
for sec, keys in WANT.items():
    if not any(l.strip() == f"[{sec}]" for l in out):
        out.append(f"\n[{sec}]\n")
        out.extend(f"{k} = {v}\n" for k, v in keys.items())
open(dst, "w").write("".join(out))
PY
  chmod 600 $MEM/config.toml
fi
grep -q '^id = "grid-colony-one"' $MEM/config.toml || die "mind config does not carry agent id grid-colony-one" 1
grep -q '^provider = "openai"' $MEM/config.toml || die "mind config is not wired to the gateway" 1
# kannaka rewrites config.toml on startup and normalises string quotes to double,
# so accept either quote style here or the guard rejects a correct, running config.
grep -qE "^path = [\"']$MEM/kannaka.hrm[\"']" $MEM/config.toml || die "mind config hrm.path is not $MEM/kannaka.hrm" 1
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
sudo cp $CHECKOUT/deploy/systemd/grid-verdicts.service $CHECKOUT/deploy/systemd/grid-verdicts.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now grid-verdicts.timer
systemctl is-active --quiet grid-verdicts.timer || die "grid-verdicts.timer is not active: $(systemctl status grid-verdicts.timer --no-pager | tail -3)" 6
# The checkout is updated earlier (§0), so grid.identity exists before pull-export.sh
# runs it, and the checkout's copy is the versioned source of truth for what gets
# installed here. Only install when the content differs, so a re-run is a no-op and an
# actual change gets a one-line diff summary in the log instead of a silent overwrite.
if ! cmp -s "$CHECKOUT/deploy/skywave/pull-export.sh" /srv/grid/pull-export.sh 2>/dev/null; then
  log "pull-export.sh changed: $(diff /srv/grid/pull-export.sh "$CHECKOUT/deploy/skywave/pull-export.sh" 2>/dev/null | head -1)"
  install -m 755 -o nick -g nick "$CHECKOUT/deploy/skywave/pull-export.sh" /srv/grid/pull-export.sh
fi
log "§3 receipt: bridge and relay active, verdicts timer enabled"

# ---- §5 weekly release ----------------------------------------------------------
install -d -m 700 -o nick -g nick $ROOT/releases
grep -q '^DEBAIN2_HOST=' $ROOT/grid.env || echo 'DEBAIN2_HOST=debain2' >> $ROOT/grid.env
grep -q '^RELEASES_DIR=' $ROOT/grid.env || echo "RELEASES_DIR=$ROOT/releases" >> $ROOT/grid.env
sudo cp $CHECKOUT/deploy/systemd/grid-weekly-release.service $CHECKOUT/deploy/systemd/grid-weekly-release.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now grid-weekly-release.timer
systemctl is-active --quiet grid-weekly-release.timer || die "grid-weekly-release.timer is not active: $(systemctl status grid-weekly-release.timer --no-pager | tail -3)" 6
log "§5 receipt: weekly release timer enabled"
log "DONE — run deploy/skywave/receipts.sh to collect the six acceptance receipts"
