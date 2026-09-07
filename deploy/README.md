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
Sudoers lines, one per unit the gate restarts without a password:
```
nick ALL=(root) NOPASSWD: /usr/bin/systemctl restart grid-mind.service
nick ALL=(root) NOPASSWD: /usr/bin/systemctl restart grid-odin.service
```
The first is the dream restart; the second is every relay restart the release gate does
(`flip`, `start_ab`, `end_ab`, `ab --check`, `ab --abort`) — without it the gate's `sudo -n`
fails and an A/B is recorded `flip_incomplete` instead of running.
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
grep -q '^MIND_ASK_TIMEOUT_S=' /srv/grid/grid.env || printf 'MIND_ASK_TIMEOUT_S=900
ODIN_RETRY_S=900
' >> /srv/grid/grid.env
bash ~/grid-deploy/set-mind-model.sh http://10.30.0.30:4000/v1 kannaka-brain ~/.grid-debain2-key
# keep the chronicle (3 asks per phase, rare) on the fast hosted alias so it does not
# queue behind the relay on the same CPU model:
bash ~/grid-deploy/set-mind-model.sh http://127.0.0.1:4000/v1 agent-brain /srv/grid/colony-one/mind-skywave.key --daemon-only
```
Measured 2026-09-06: 121 s per Odin prompt straight to the gateway on an idle debain2, 400 s+
when the box is shared (load 50 on 20 cores), hence the 900 s deadline. `ODIN_RETRY_S` must be
>= the deadline: after a timeout the model is still computing the orphaned answer and an earlier
retry queues behind it.
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
/srv/grid/venv/bin/python -m grid.verdict_corpus --out ~/.kannaka-corpus/grid/$(date -u +%G-W%V) --dream
```
`--dream` adds a `dream` kind pulled from the colony mind's store via `kannaka search`
(empty with a `dream_note` explaining why until `kannaka dream` writes a report memory
instead of only logging consolidation metrics). `--since-ts <epoch-seconds>` narrows the
verdict rows to those proposed at or after that timestamp, for a weekly rather than
cumulative export; the manifest records the cutoff as `since_ts_filter`.
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

### Skywave pull (rogue-grid-pull forced command)
`deploy/skywave/pull-export.sh` is the versioned copy of the live forced command bound to the
`rogue-grid-pull` key on Skywave. It now also runs `python -m grid.identity export --out`
alongside the verdict corpus export and adds `identity_examples.jsonl` and
`identity_manifest.json` to the tar it streams, so the identity example set travels with every
pull. deploy.sh installs `pull-export.sh` from the checkout after updating it; to install by
hand, run deploy.sh first, then, from the checkout, `diff /srv/grid/pull-export.sh
deploy/skywave/pull-export.sh` before copying. Kannaka's `rogue/grid.py`
consumes the new tar member; that consumer change is coordinated separately, so until it lands
the member rides along in the tar unused.

## 5. Weekly release (self-improvement sub-project 2)
`rogue.weekly` trains and promotes a new `kannaka-brain-v*` on debain2 every week; `grid-weekly-release.timer`
runs `python -m grid.weekly release` on Skywave every Sunday at 06:00 (host time, three hours after the
rogue-weekly slot, `Persistent=true` so a missed run fires on the next boot). It probes the promoted
candidate against the fossil identity/validity checks, appends a `released` or `held` row to
`$RELEASES_DIR/versions.jsonl`, and — only on `released` — copies the candidate's ollama tag onto the
`kannaka-brain-current` alias and restarts `grid-odin.service` so the relay's proposals are attributed to
the new version.

One-time, before the first release: create the `kannaka-brain-current` alias on debain2's gateway and mint
the colony key that can see it (this touches debain2 and mints a key; it is not run by `deploy.sh`):
```bash
scp ~/grid-deploy/install-current-alias.sh debain2:~/ && ssh debain2 'bash ~/install-current-alias.sh kannaka-brain-v3'
scp debain2:~/.grid-key-for-skywave /srv/grid/colony-one/mind-llm.key && chmod 600 /srv/grid/colony-one/mind-llm.key
bash ~/grid-deploy/set-mind-model.sh http://10.30.0.30:4000/v1 kannaka-brain-current /srv/grid/colony-one/mind-llm.key --relay-only
sed -i 's/^MIND_MODEL_ALIAS=.*/MIND_MODEL_ALIAS=kannaka-brain-v3/' /srv/grid/grid.env && sudo systemctl restart grid-odin
```
The script never prints the minted key; it lands in `~/.grid-key-for-skywave` (0600) on debain2 for the
`scp` above to carry off.

Manual commands, from `/srv/grid/kannaka-grid`:
```bash
/srv/grid/venv/bin/python -m grid.weekly status                # last release, versions.jsonl row count, chain verify, current candidate
/srv/grid/venv/bin/python -m grid.weekly release --dry-run      # print the planned remote/local commands and the config it would use; does not contact debain2 or flip anything
/srv/grid/venv/bin/python -m grid.weekly rollback                # flip back to the previous released version
```
Receipts:
1. `$RELEASES_DIR/versions.jsonl` gains a `released` or `held` row naming the candidate, with the
   identity/validity probe numbers and (on `released`) the adapter/gguf hashes and spend.
2. `python -c "from grid.versions import Versions; print(Versions('$RELEASES_DIR/versions.jsonl').verify())"`
   prints `True`.
3. On `released`, the relay's next `GRID.colony-one` proposal-log entry names the new `model_alias`
   (`journalctl -u grid-odin` or a bus capture on `GRID.>`).
4. `python -m grid.weekly rollback` followed by `python -m grid.weekly release --week <the rolled-back week>`
   once by hand restores the prior version and then re-releases, both landing in `versions.jsonl`.

### Colony A/B testing
The release gate can A/B test a candidate model against the incumbent for up to two weeks. The decision
log (versions.jsonl, column `colony_ab`) records each run's outcome: `ab_started` when the gate begins
a test, `ab_inconclusive` if inconclusive after one week, `released` (A/B win), `held` (stuck, loss,
inconclusive after two weeks, or a win whose `ollama cp` failed), and `flip_incomplete` when the
env was changed but the relay restart failed twice — grid.env then says one thing and the running
relay another, so an operator must `sudo systemctl restart grid-odin.service` by hand.
Nightly, after verdicts settle, the gate's pre-release also runs
`grid.weekly ab --check` to detect if an A/B test has stalled and cannot resolve (same stuck-detection
logic as the release gate itself); other manual A/B sub-commands are `grid.weekly ab --abort` (discard
an in-flight test) and `grid.weekly ab --forget <tag>` (append a row lifting the block on a tag so it
can be retested). An operator's `ab --abort` is not remembered -- the same candidate tag is free to
start a new A/B the next Sunday it is promoted -- whereas a loss, a stuck abort, or an
inconclusive-after-two-weeks hold in which only the candidate arm was under sample blocks that tag
from retesting until an operator runs `ab --forget <tag>`. (Both arms under sample is a colony
throughput fault, not the candidate's, and is not remembered.)

**Do not run `set-mind-model.sh` while an A/B is running**: it rewrites `MIND_MODEL_ALIAS`, and the
incumbent's proposals then log under a label the gate does not count — the gate detects the mismatch
and records `ab_inconclusive` with `incumbent label changed mid-A/B`, spending a week of the A/B's
two-week budget for nothing. Run `grid.weekly ab --abort` first, or wait for the verdict Sunday.

A release therefore takes two Sundays at minimum: the gate starts the A/B on the Sunday the promotion first passes its probes, and renders the colony's verdict on the following Sunday (continuing up to two weeks if the sample is short); the nightly `ab --check` only aborts a stuck A/B within that window.

To run a hand-started A/B test: set `MIND_LLM_CANDIDATE=<model-tag>` and `MIND_AB_SPLIT=0.2` in
`/srv/grid/grid.env` (the default split is 0.2 = 20% candidate, 80% incumbent), then restart
`grid-odin.service` to begin routing requests. Monitor the relay's proposal log with `journalctl -u grid-odin`
and check that both arms are represented: `grep -o 'model_alias":"[^"]*' <proposal-log>` shows both the
incumbent and candidate labels. Run `python -m grid.weekly ab` from `/srv/grid/kannaka-grid` to print both
arms' identities and their verdict counts so far. When done, run `python -m grid.weekly ab --abort` to
clear the candidate and discard the A/B test, then restart `grid-odin.service`.

Manual commands from `/srv/grid/kannaka-grid`:
```bash
/srv/grid/venv/bin/python -m grid.weekly ab               # show both arms' identities and verdict counts
/srv/grid/venv/bin/python -m grid.weekly ab --check       # nightly stuck check; see the exit codes below
/srv/grid/venv/bin/python -m grid.weekly ab --abort       # discard an in-flight A/B test
/srv/grid/venv/bin/python -m grid.weekly ab --forget <tag>  # append a row lifting the block on a tag
```
`ab` also reports an A/B that exists only in `grid.env` with no ledger row (a hand-started one, as
above) as `"source": "env"`; its arms then cover all recorded history, since there is no `since_ts`.

`ab --check` exit codes — it runs as `grid-verdicts.service`'s `ExecStartPost`, so a non-zero exit
marks that nightly unit failed:
- `0` — no A/B running, or one is running and is not stuck.
- `0` with a `held` row printed — the A/B was stuck and has been cleared; the relay restarted cleanly.
- `3` — the A/B was cleared in `grid.env` but the relay restart failed twice. The live relay is still
  splitting traffic to a candidate the ledger has closed: run
  `sudo systemctl restart grid-odin.service` and check `journalctl -u grid-odin` for `A/B: none`.

## Known limits (v0.1)
- Memories written by the bridge become visible to asks only after the 6 h dream+restart.
- `GRID.*` is unsigned on the wire (spec §8).
- Flux body carries both key styles; confirm against the live Flux and trim (Task 7 note).
- No backups on Skywave: `/srv/grid/colony-one/state` is lab-grade.
