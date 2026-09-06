# Skywave runbook — kannaka-grid v0.1

Host facts: `NickFlach/kax-computer` `docs/DEPLOY.md` (WARP on, `ssh debain1`, user `nick`, no backups).

## 0. Prerequisites (once)
```bash
sudo install -d -o nick -g nick /srv/grid
cd /srv/grid && git clone --recurse-submodules https://github.com/NickFlach/kannaka-grid.git
cd /srv/grid/kannaka-grid
ln -s /srv/grid/kannaka-grid/thegrid /srv/grid/thegrid
python3 -m venv /srv/grid/venv && /srv/grid/venv/bin/pip install -e "/srv/grid/kannaka-grid[dev]" -e /srv/grid/thegrid
install -d -o nick -g nick /srv/grid/colony-one/state /srv/grid/colony-one/state/operator /srv/grid/colony-one/memory
cp deploy/grid.env.example /srv/grid/grid.env && chmod 600 /srv/grid/grid.env
```
Receipt: `/srv/grid/venv/bin/python -c "import grid, src.colony.live"` prints nothing.

## 1. Mind
All commands run from `/srv/grid/kannaka-grid`.
```bash
KANNAKA_DATA_DIR=/srv/grid/colony-one/memory kannaka init      # scaffold
# mint a LiteLLM virtual key the same way create_machine.py does (budget 0.5/30d, model agent-brain)
# then edit /srv/grid/colony-one/memory/config.toml per deploy/mind-config.toml.example
KANNAKA_DATA_DIR=/srv/grid/colony-one/memory kannaka remember "grid-colony-one mind initialised" --category grid
sudo cp deploy/systemd/grid-mind*.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now grid-mind.service grid-mind-dream.timer
```
Sudoers line for the dream restart: `nick ALL=(root) NOPASSWD: /usr/bin/systemctl restart grid-mind.service`.
Receipt: `kannaka swarm peers` lists `grid-colony-one` with `ask` in capabilities.

### 1a. Which model answers the mind
The mind is an OpenAI-compatible client; the model behind it is a deployment choice.
`deploy.sh` wires it to Skywave's LiteLLM alias `agent-brain` (a hosted model, ~$0.009 per
ask at ~50 asks/h on colony-one, so a $2 key lasts ~2.5 h). To run on the constellation's own
open-weight brain on debain2 instead (`kannaka-brain` = qwen2.5:14b, or the trained
`kannaka-brain-v1` adapter; CPU-only, ~2 min per Odin prompt, no spend):
```bash
# once: mint a key on debain2's gateway for models kannaka-brain, kannaka-brain-v1 and land it
# on Skywave as ~/.grid-debain2-key (0600) without it ever passing through a terminal
grep -q '^MIND_ASK_TIMEOUT_S=' /srv/grid/grid.env || echo MIND_ASK_TIMEOUT_S=400 >> /srv/grid/grid.env
bash ~/grid-deploy/set-mind-model.sh http://10.30.0.30:4000/v1 kannaka-brain ~/.grid-debain2-key
```
`set-mind-model.sh` rewrites only the `[llm]` lines, sets `MIND_MODEL_ALIAS` (which verdict rows
record as the proposal's author), points the relay's asks straight at the gateway
(`MIND_LLM_URL/MODEL/KEY_FILE`; recall and remember stay on Kannaka's daemon), and restarts
grid-mind/odin/bridge. The direct route exists because `kannaka swarm serve` is single-threaded:
while it waits minutes on a CPU model it misses NATS pings, is dropped as a stale connection, and
the reply is lost (kannaka-memory#904). `--via-daemon` routes asks through the daemon again. Switch back with the same
command and the Skywave gateway URL, alias `agent-brain`, and the key `rotate-mind-key.sh`
minted. A drained hosted key shows in `journalctl -u grid-odin` as `429 budget_exceeded`;
`rotate-mind-key.sh` mints a fresh one.

## 2. Colony
All commands run from `/srv/grid/kannaka-grid`.
```bash
sudo cp deploy/systemd/thegrid-colony.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now thegrid-colony.service
journalctl -u thegrid-colony -n 5     # expect "heat source: proxy" on this VM
curl -s 127.0.0.1:8787/api/state | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['name'], s['epoch'], s['tick'], s['population'])"
```
Receipt: name `colony-one`, tick increasing between two calls.

## 3. Bridge and relay
All commands run from `/srv/grid/kannaka-grid`.
```bash
sudo cp deploy/systemd/grid-bridge.service deploy/systemd/grid-odin.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now grid-bridge.service grid-odin.service
```
Receipts (acceptance criteria, spec §12):
1. `journalctl -u grid-bridge -f` shows no `dropped` lines; from the operator seat, `kannaka swarm tail --subject 'GRID.>'` (or the nats app's capture) shows `GRID.colony-one.event.epoch.started` then `.state` every 60 s.
2. Storms fire when host load rises: the proxy maps whole-host CPU to 45–85 °C and thegrid's substrate needs its fast thermal average (alpha 0.001 per tick ≈ 1000 ticks) to climb ≥ 1.5 °C over the slow baseline. **Under the unit's `CPUQuota=5%` the colony runs at ~1.5 ticks/s on Skywave (measured 2026-09-05: 45 ticks per 30 s), so 1000 ticks is ~11 minutes**, and a short burst cannot do it. Force one with a full core held for thirty minutes: `timeout 1800 python3 -c 'while True: pass'` (one core ≈ 17 % of Skywave's 6 CPUs ≈ +6.7 °C step). `GRID.colony-one.event.storm` appears during the burst. The first chronicle (receipt 5) likewise lands at tick 2000 ≈ 22 minutes after the colony starts.
3. `KANNAKA_DATA_DIR=/srv/grid/colony-one/memory kannaka status` shows memory count rising; `kannaka recall "storm"` against that dir returns a colony fact.
4. `ls /srv/grid/colony-one/state/operator` shows `request.json` then `proposal.json`; both vanish from the operator dir after a later birth (thegrid consumes them together); `journalctl -u grid-odin` shows `proposed`; `curl -s 127.0.0.1:8787/api/history | python3 -c "import json,sys; print(json.load(sys.stdin)['mutationMechanisms'])"` lists the mutation types thegrid recorded for the epoch.
5. After tick 2000, three `GRID.colony-one.chronicle.*` messages appear.
6. `sudo systemctl stop grid-bridge grid-odin grid-mind`; the colony's tick rate in `/api/state` is unchanged over 60 s. Start them again.

Paste the captured payloads for 1, 2, 4 and 5 into the PR that closes the deploy.

## 4. Verdicts (self-improvement sub-project 1)
Verdicts logs live under `/srv/grid/colony-one/verdicts` (mode 700): `proposals.jsonl`,
`prompts/`, `verdicts.jsonl`, and `chronicle.jsonl`.
`grid-verdicts.timer` runs `python -m grid.verdicts` nightly at 03:15 (host time,
`Persistent=true` so a missed run fires on the next boot): it settles every
logged Odin proposal against the fossil record thegrid wrote for that epoch,
appends a hash-chained verdict to `verdicts.jsonl`, and publishes
`GRID.colony-one.verdict` for each one.
```bash
sudo cp deploy/systemd/grid-verdicts.service deploy/systemd/grid-verdicts.timer /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now grid-verdicts.timer
```
A manual run, from `/srv/grid/kannaka-grid`, settles without publishing to the bus:
```bash
/srv/grid/venv/bin/python -m grid.verdicts --no-publish
```
Export the week's verdict corpus (positives, negatives, and a manifest with the chain head) for training:
```bash
/srv/grid/venv/bin/python -m grid.verdict_corpus --out ~/.kannaka-corpus/grid/$(date -u +%G-W%V)
```
Receipts:
1. `verdicts.jsonl` has rows and the ledger's hash chain verifies:
   ```bash
   /srv/grid/venv/bin/python -c "from grid.verdicts import Ledger; print(Ledger('/srv/grid/colony-one/verdicts/verdicts.jsonl').verify())"
   ```
   prints `True`. If it prints `False` and the run exits non-zero with `ledger has a corrupt row at
   line N; refusing to append`, the ledger has a damaged row mid-file: the timer will keep failing
   until an operator repairs or archives `verdicts.jsonl` by hand, which is deliberate — appending
   past it would strand every good row below it.
2. At least one row reaches `reproduced` or better within a day of the proposal it settles.
3. The export manifest shows `records ≥ 1`, and `GRID.colony-one.verdict` is seen in a bus capture (`kannaka swarm tail --subject 'GRID.>'` or the nats app's capture).

## Known limits (v0.1)
- Memories written by the bridge become visible to asks only after the 6 h dream+restart.
- `GRID.*` is unsigned on the wire (spec §8).
- Flux body carries both key styles; confirm against the live Flux and trim (Task 7 note).
- No backups on Skywave: `/srv/grid/colony-one/state` is lab-grade.
