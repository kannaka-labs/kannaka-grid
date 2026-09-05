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
2. Storms fire when host load rises: the proxy maps whole-host CPU to 45–85 °C and thegrid's substrate needs its fast thermal average (alpha 0.001 per tick ≈ 1000 ticks ≈ 100 s at 10 ticks/s) to climb ≥ 1.5 °C over the slow baseline. A short burst cannot do that. Force one with a full core held for three minutes: `timeout 180 python3 -c 'while True: pass'` (one core ≈ 17 % of Skywave's 6 CPUs ≈ +6.7 °C step). `GRID.colony-one.event.storm` appears during the burst or within ~1000 ticks after it.
3. `KANNAKA_DATA_DIR=/srv/grid/colony-one/memory kannaka status` shows memory count rising; `kannaka recall "storm"` against that dir returns a colony fact.
4. `ls /srv/grid/colony-one/state/operator` shows `request.json` then `proposal.json`; both vanish from the operator dir after a later birth (thegrid consumes them together); `journalctl -u grid-odin` shows `proposed`; `curl -s 127.0.0.1:8787/api/history | python3 -c "import json,sys; print(json.load(sys.stdin)['mutationMechanisms'])"` lists the mutation types thegrid recorded for the epoch.
5. After tick 2000, three `GRID.colony-one.chronicle.*` messages appear.
6. `sudo systemctl stop grid-bridge grid-odin grid-mind`; the colony's tick rate in `/api/state` is unchanged over 60 s. Start them again.

Paste the captured payloads for 1, 2, 4 and 5 into the PR that closes the deploy.

## Known limits (v0.1)
- Memories written by the bridge become visible to asks only after the 6 h dream+restart.
- `GRID.*` is unsigned on the wire (spec §8).
- Flux body carries both key styles; confirm against the live Flux and trim (Task 7 note).
- No backups on Skywave: `/srv/grid/colony-one/state` is lab-grade.
