# Skywave runbook — kannaka-grid v0.1

Host facts: `kannaka-labs/kax-computer` `docs/DEPLOY.md` (WARP on, `ssh debain1`, user `nick`, no backups).

## 0. Prerequisites (once)
```bash
sudo install -d -o nick -g nick /srv/grid
cd /srv/grid && git clone --recurse-submodules https://github.com/kannaka-labs/kannaka-grid.git
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

### 3a. A relay restart now tells you whether the relay came back
`grid-odin.service` is `Type=simple`, which means systemd calls the start job done the moment
it has forked the process. `systemctl restart grid-odin.service` therefore used to exit 0 even
when the relay died a second later on a `grid.env` it refuses — an unknown `MIND_PROMPT_VARIANT`
or `MIND_PROMPT_CANDIDATE`, a candidate equal to the incumbent, a split outside (0, 1). Those
refusals are deliberate and fail-closed, and each leaves the colony's mind **down**, not merely
mislabelled. The weekly gate reads that exit code and nothing else, so its revert-and-restart
path — the one whose own comments describe exactly this case — never ran for it, and the ledger
recorded `ab_started` over a relay that was not running.

The unit's `ExecStartPost=` closes that: `deploy/skywave/relay-ready.sh` waits for the relay to
log `watching <queue> for <agent>` (emitted only after the env parsed, the variants resolved,
the sampling route was accepted and the bus connected), checks that the `A/B:` and `prompt:`
lines it logged name the values this unit's `EnvironmentFile` just handed it, and then checks
that the relay is *still* alive. It is not a `sleep`: it polls for that evidence, scoped to this
start's `_SYSTEMD_INVOCATION_ID` so a line from the *previous* start can never pass it, and it
gives up at once — without waiting out its 60 s deadline — if the main process is already gone.
Reading the journal is why the unit carries `SupplementaryGroups=systemd-journal`.

Both expected lines are matched whole, prefix (`grid.odin INFO …`) and suffix, so a substring
cannot satisfy them — and a blank `MIND_PROMPT_VARIANT` is resolved through the registry before
the comparison, not left as a bare `prompt: ` prefix. That case is not exotic: the line is blank
on a fresh host and stays blank through every gate-started prompt experiment, because
`start_ab` writes only the candidate and the split. Without the resolution the prompt half of
the check would pass for a relay serving any variant at all, in the default configuration.

**What an operator sees when the check fails.** `systemctl restart` (or `deploy.sh`, or the
gate) exits non-zero with `Job for grid-odin.service failed because the control process exited
with error code.` `systemctl status grid-odin` shows the failed `relay-ready.sh` control
process, and `journalctl -u grid-odin -n 40` carries one `relay-ready:` line saying which of the
following went wrong, above the relay's own last twenty lines:
- `the relay exited during start: it read /srv/grid/grid.env and refused it` — the fail-closed
  case. The refusal itself (`VariantError: unknown variant 'v2-tesre'`, `MIND_PROMPT_AB_SPLIT=1.0
  is out of range`, …) is in the lines printed above it. Fix `grid.env` and restart. When the
  weekly gate provoked it, the gate has already put `grid.env` back and restarted onto the
  restored values, and reported `flip_incomplete` with exit 3.
- `the relay is still alive after 60s but has not logged 'watching'` — it is stuck before that
  point, almost always retrying a NATS connect (the relay's own backoff is 1, 2, 4, 8, 16, 32 s,
  so 60 s allows about six attempts). **What this does to the host during a long bus outage:** an
  `ExecStartPost` failure makes systemd fail and *stop* the unit, so the relay is killed, not left
  waiting. `Restart=always` then starts it again after `RestartSec=10`, and the next start runs
  the probe again — a roughly 70 s kill-and-retry cycle for as long as the bus is down, which
  never trips `StartLimitIntervalSec=300`/`StartLimitBurst=10`, so the relay does come back on its
  own once the bus returns. It is not sitting patiently in its own backoff: each cycle discards
  that backoff and any in-flight ask. To raise the deadline, use a drop-in — it is read from the
  unit's environment, so `export` will not reach it:
  ```bash
  sudo systemctl edit grid-odin.service      # [Service]
                                             # Environment=RELAY_READY_TIMEOUT_S=110
  ```
  `TimeoutStartSec=120` in the unit is the ceiling above whatever is set there; raise it too if
  you go past it. `RELAY_READY_UNIT` and `RELAY_READY_PYTHON` are set the same way.
- `the relay came up on a configuration this unit did not hand it` — the process answering is
  not serving `/srv/grid/grid.env`. The message names the exact line it expected. Expect this
  only after an edit that raced a restart.
- `the relay logged that it was watching the queue and then exited before this check finished` —
  it started cleanly and died immediately afterwards. The reachable case is the bus dropping on
  the first poll (`nats gave up: …; exiting for systemd to restart`, exit 75).
- `MIND_PROMPT_VARIANT is blank and the registry default could not be resolved` — the probe could
  not read the prompt registry, so it has no id to compare against. The relay's own start fails on
  the same registry; the Python error is on the same line. Run the loader one-liner from §6.

Receipts (acceptance criteria, spec §12):
1. `journalctl -u grid-bridge -f` shows no `dropped` lines; from the operator seat, `kannaka swarm tail --subject 'GRID.>'` (or the nats app's capture) shows `GRID.colony-one.event.epoch.started` then `.state` every 60 s.
2. Storms fire when host load rises: the proxy maps whole-host CPU to 45–85 °C and thegrid's substrate needs its fast thermal average (alpha 0.001 per tick ≈ 1000 ticks) to climb ≥ 1.5 °C over the slow baseline. **Under the unit's `CPUQuota=40%` the colony runs at ~9.8 ticks/s on Skywave (measured 2026-09-08: 589 ticks per 60 s), so 1000 ticks is ~100 seconds.** It ran at ~1.5 ticks/s under the old 5% cap, where 1000 ticks was ~11 minutes and a short burst could not move the weather at all. At 40% the colony's own work is about 6.7% of the host, roughly +2.7 C, so it can now cross the +1.5 C storm threshold BY ITSELF -- storms are no longer purely a signal of outside load, and a run of them may be the colony's own metabolism. To force one deliberately anyway: Force one with a full core held for thirty minutes: `timeout 1800 python3 -c 'while True: pass'` (one core ≈ 17 % of Skywave's 6 CPUs ≈ +6.7 °C step). `GRID.colony-one.event.storm` appears during the burst. The first chronicle (receipt 5) likewise lands at tick 2000 ≈ 22 minutes after the colony starts.
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
two-week budget for nothing. It spends at most two: at `MAX_WEEKS` the gate closes the A/B with
`held: incumbent label changed mid-A/B …; A/B ended after 2 weeks`, clears the candidate and restarts
the relay, and the candidate is *not* blacklisted — an operator's mislabel is not the colony's
verdict, so the next Sunday starts a fresh A/B against the new label. Until then the nightly
`ab --check` logs a warning and skips the stuck check. Run `grid.weekly ab --abort` first, or wait
for the verdict Sunday.

**Reconciliation.** `ab --check` first repairs the *resting* state, before any stuck logic: if the
last decision was `flip_incomplete`, or `grid.env` still splits to a candidate whose A/B the ledger
has closed, or the ledger has an A/B open that nothing is splitting to, it clears the candidate,
restarts grid-odin, and records a `colony A/B reconciled: …` row (exit 3, and it retries every night,
if that restart fails). It keys on `ab_started` rows, i.e. on "did the *gate* start this?", so an A/B
you started by hand is logged at WARNING and left completely alone — and stays exempt across as many
runs of the hand-start receipt as you like, because `ab --abort` writes a `held` row, never an
`ab_started` one. While a hand-started split is in `grid.env` the reconciler does nothing else
either, so it suspends the repairs above for its duration.

A release therefore takes two Sundays at minimum: the gate starts the A/B on the Sunday the promotion first passes its probes, and renders the colony's verdict on the following Sunday (continuing up to two weeks if the sample is short); the nightly `ab --check` only aborts a stuck A/B within that window.

To run a hand-started A/B test: set `MIND_LLM_CANDIDATE=<model-tag>` and `MIND_AB_SPLIT=0.2` in
`/srv/grid/grid.env` (the default split is 0.2 = 20% candidate, 80% incumbent), then restart
`grid-odin.service` to begin routing requests. Monitor the relay's proposal log with `journalctl -u grid-odin`
and check that both arms are represented: `grep -o 'model_alias":"[^"]*' <proposal-log>` shows both the
incumbent and candidate labels. Run `python -m grid.weekly ab --dimension model` from
`/srv/grid/kannaka-grid` to print both arms' identities and their verdict counts so far (bare
`ab` prints the same block one level down, under `.ab.model`, alongside every other dimension).
When done, run `python -m grid.weekly ab --abort` to clear the candidate and discard the A/B
test, then restart `grid-odin.service`.

**Dimensions.** The gate can run an A/B on which MODEL answers or on which PROMPT wording is
used, and `--dimension {model,prompt}` selects which. It defaults to `model` on `release`,
`rollback`, `ab --abort` and `ab --forget`; omitting it on `ab` and `ab --check` means *every*
dimension (the display prints one entry per dimension, and the check reconciles each in turn and
exits with the loudest code any of them produced). Only one experiment may run at a time: a
`release` that would start one while another dimension is splitting — whether that split is in
the ledger or hand-started in `grid.env` — records `unchanged: <other> A/B already running (…);
refusing to start a … experiment whose verdict it would confound` and exits 0.

Manual commands from `/srv/grid/kannaka-grid`:
```bash
/srv/grid/venv/bin/python -m grid.weekly ab                      # every dimension, keyed by dimension
/srv/grid/venv/bin/python -m grid.weekly ab --dimension model    # just the model A/B's arms and counts
/srv/grid/venv/bin/python -m grid.weekly ab --check              # nightly reconcile + stuck check, all dimensions
/srv/grid/venv/bin/python -m grid.weekly ab --abort              # discard an in-flight A/B test (model)
/srv/grid/venv/bin/python -m grid.weekly ab --forget <tag>       # append a row lifting the block on a tag
/srv/grid/venv/bin/python -m grid.weekly ab --forget <id> --dimension prompt   # ...the prompt dimension's memory
```
`ab` also reports an A/B that exists only in `grid.env` with no ledger row (a hand-started one, as
above) as `"source": "env"`; its arms then cover all recorded history, since there is no `since_ts`.

Exit codes — `release`, `rollback`, `ab --check` and `ab --abort` share one contract, and `ab --check`
runs as `grid-verdicts.service`'s `ExecStartPost`, so a non-zero exit marks that nightly unit failed:
- `0` — nothing needed doing, or the work was done cleanly (a `held` row printed: the A/B was
  stuck, or reconciled, and the relay restarted). `ab --check` says which "nothing needed doing"
  it means: `no A/B running`, `not stuck`, or `hand-started split in grid.env (<dimension>),
  untracked by the ledger; nothing to reconcile` — that last one is a split someone started by
  editing `grid.env`, which the reconciler deliberately leaves alone (see above) and which
  `ab` reports as `"source": "env"`. It is still taking its share of live traffic.
- `3` — the verb did not do what it says, in one of three ways.
  - The row is `flip_incomplete` (the candidate was cleared in `grid.env` but the relay restart
    failed twice, so the live relay is still splitting to a candidate the ledger has closed): run
    `sudo systemctl restart grid-odin.service` and check `journalctl -u grid-odin` for
    `A/B: none` — the nightly reconciler retries this, so the unit stays failed until it is
    fixed. If the reason also ends `the relay did not come back and may be DOWN -- restart
    grid-odin.service` (two hyphens, as the gate writes it), the revert's own restart failed too:
    `grid.env` is correct and the unit is not running, so the colony has no mind until you
    restart it.
  - The reason ends `: flip_failed` (a `rollback` whose `ollama cp` failed, so the alias was never
    moved): check debain2's ollama and re-run it.
  - The row is `unchanged` and carries `"evals": {"unresolved": true}` — the gate found something
    wrong that it could not put right, and recorded it without deciding anything (no week spent,
    nothing blacklisted). Two shapes: *"a release was applied and never recorded"* means the live
    `MIND_MODEL_ALIAS` is a later version than the ledger's last `released` row, so `status` and
    `rollback` are reading a superseded version — the next `ab --check` records the release for
    real if it can prove the flip happened, otherwise reconcile it by hand; and *"the prompt
    variant registry did not load"* means `grid/data/prompt_variants.json` is broken, which also
    stops `grid-odin` booting — fix the file, then re-run.

Never put a comment after a value in `grid.env` (`ODIN_RETRY_S=60   # 900 on a CPU model`): systemd's
`EnvironmentFile` keeps it *in the value*, `float()` on it kills the relay at boot, and neither the
gate nor the relay strips it — deliberately, so that both always read a value identically. Put the
comment on its own line above.

## 6. Prompt variants (self-improvement sub-project 3)
The gate can A/B which **wording** the relay asks the mind with, exactly as §5 A/Bs which
**model** answers. A *variant* is one prompt template plus the sampling it wants: a Markdown
file under `grid/data/prompts/` and an entry in the registry at
`grid/data/prompt_variants.json` naming its file, its sha256, `temperature`, `max_tokens`,
`recall` (whether the ask is preceded by a `kannaka recall`) and a `note` saying what it is for.
The relay resolves its variant **once**, at start-up, and logs the id and hash on every
proposal, so a week's rows can always be attributed to the bytes that produced them.

`python -m grid.weekly ab --dimension prompt` prints what is running; the receipts script's
`6-prompt-state.txt` prints the registry default, the variant the relay is serving, and each
arm's answer quality.

**Adding a variant.** Write the template, hash it, add the entry, commit both:
```bash
cd /srv/grid/kannaka-grid                     # or your checkout
$EDITOR grid/data/prompts/v3-my-idea.md
python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" \
  grid/data/prompts/v3-my-idea.md
$EDITOR grid/data/prompt_variants.json        # new key under "variants"; leave "default" alone
/srv/grid/venv/bin/python -c "from grid.variants import load_registry; d,v=load_registry(); print(d, sorted(v))"
```
That last line is the whole acceptance test: the loader fails closed, so if it prints, the
registry is servable. Two rules the shipped variants exist to illustrate:
1. **Keep the sandbox-context paragraph.** Without it the model's safety layer reads thegrid's
   directive plus opcodes named `steal` and `corrupt` as a request for self-replicating malware
   and returns an empty completion. A variant that drops it does not do badly; it returns
   nothing at all.
2. **The reply must still parse as a bare JSON array of instruction names.** A variant that
   invites reasoning, or a preamble, loses on answer quality rather than on its wording, and the
   experiment measures the wrong thing.

**A variant is immutable once named.** Editing a template in place and re-pinning its hash is
refused: not by the loader, which would accept the new bytes, but by the design around it — the
ledger row that opened an A/B names an id, the proposal log rows name an id and a hash, and
silently changing what that id says mid-experiment invalidates every row already collected. So
the loader raises `VariantError` at **start-up** on any drift it can see (a hash that does not
match its file, a missing or empty template, an unknown placeholder, a template that will not
render, a path escaping the registry directory, a duplicate id, a `default` naming an absent
variant) and grid-odin refuses to boot rather than serving something the ledger cannot name.
There is no warning path and no fallback to the default. **To change a prompt, add a new id.**

**Hand-starting a prompt A/B for a smoke test** — the mirror of §5's model one. In
`/srv/grid/grid.env` set `MIND_PROMPT_CANDIDATE=<variant id>` and `MIND_PROMPT_AB_SPLIT=0.2`
(20% candidate, 80% incumbent; its own knob, deliberately not `MIND_AB_SPLIT`, so widening a
model rollout cannot re-randomise which wording each request gets mid-experiment). Leave
`MIND_PROMPT_VARIANT` alone unless you mean to change the incumbent: blank means the registry's
`default`, which is the id the relay logs and the gate counts the incumbent arm under. Then:
```bash
sudo systemctl restart grid-odin.service      # non-zero here means it did NOT come back: see §3a
journalctl -u grid-odin -n 20 | grep 'prompt A/B:'          # both ids and the split
/srv/grid/venv/bin/python -m grid.weekly ab --dimension prompt     # arms and counts so far
# each request's variant matches odin's own assignment, from the log alone:
grep -o '"prompt_variant":"[^"]*"' /srv/grid/colony-one/verdicts/proposals.jsonl | sort | uniq -c
/srv/grid/venv/bin/python -m grid.weekly ab --abort --dimension prompt   # done: clear and restart
```
The nightly reconciler leaves a hand-started split completely alone (it keys on `ab_started`
rows, i.e. on "did the *gate* start this?"), and stays exempt across as many runs of this
receipt as you like, because `ab --abort` writes a `held` row and never an `ab_started` one.

**The answer-quality floor, and why a bad variant costs hours instead of weeks.** Each arm's
proposals are tallied two ways: the verdict rows say what the colony did with the genomes it
produced, and the proposal log says how many asks it could answer *at all*. `validity` is
`valid / answered` — asks binned after repeated defers (a gateway outage) sit outside both sides
of the ratio, because that is the colony's availability failing, not the arm's wording. Once a
candidate has `answered` at least 20 asks, a `validity` below 0.5 is an immediate **loss**,
decided before the settled-sample check. That ordering is the point: a variant whose replies do
not parse produces no genomes, so it settles nothing, so on verdict rows alone it merely looks
*slow* — it would read `inconclusive` week after week and be held at the two-week cap with a
reason blaming sample size instead of the wording. The floor is the candidate's alone; an
incumbent below it is a colony-wide fault, and the nightly stuck check owns that case.

**One experiment at a time.** A `release` that would start an A/B while another dimension is
already splitting — whether that split is in the ledger or hand-started in `grid.env` — records
`unchanged: <other> A/B already running (…); refusing to start a … experiment whose verdict it
would confound` and exits 0. A model verdict collected while a prompt A/B runs was measured
across two wordings, and a prompt verdict during a model A/B across two sets of weights; both
are recorded as *binding*, so a confounded week is not a slow week but a confident wrong answer
written into an append-only file.

**`--factorial` is the deliberate override**, and only on `release`:
```bash
/srv/grid/venv/bin/python -m grid.weekly release --dimension prompt --candidate v3-my-idea --factorial
```
It refuses when nothing is blocking (it starts a factorial run; it cannot retro-fit one onto an
A/B already open, and it must not stamp `factorial: True` on an ordinary single-dimension week).
It does not merely silence the interlock: it changes how the week is judged. The tally becomes
the four `model|variant` cells, and each dimension is decided on its **marginal** — the two
cells sharing that level, pooled, with ratios re-derived from the summed counts rather than
averaged. odin salts the two dimensions' hashes separately, so wording is balanced across both
model arms by construction and pooling cancels it out. The sample floor stays per marginal, not
per cell: at `MIN_SAMPLE=100` a 2x2 run needs 50 settled per cell. `factorial` is written onto
the `ab_started` row either way, true or false, so a later reader can tell a week that
deliberately crossed two dimensions from one that merely got confounded.

**Judging a factorial week takes two commands, and neither their order nor the delay between
them changes the verdict.** `release` decides one dimension per call, and
`grid-weekly-release.service` runs the bare `grid.weekly release`, i.e. the model leg only — so
the other leg is a hand-typed command:
```bash
/srv/grid/venv/bin/python -m grid.weekly release --dimension prompt
```
Whichever runs first ends or freezes its own leg, and the second is still judged on its marginal:
the partner lookup asks the ledger *which episode was splitting during this leg's window, and
when was it last judged*, not *what is splitting right now*. Both legs are tallied over `[the
later of the two starts, the moment the first leg was judged)`, and over the verdicts that had
landed at that same instant. Rows answered before the second split began, or after the first leg
was judged, are not part of the design and are excluded, as is any row that names only one of the
two labels. Note *judged*, not *closed*: an `ab_inconclusive` week freezes a leg's numbers
without ending its split, and at `MIN_SAMPLE=100` that is the normal first result.

**The verdict bound is a recorded prefix, not a clock.** Proposals already inside the window go
on settling while you are between the two commands — a verdict row carries no timestamp, so no
bound on *ask* time can see that — and the leg judged second would otherwise count more
settlements than the first could. It does not, because every judging row already records
`verdict_window`: the verdict ledger's own `rows: N` and `chain_head` at the instant that leg was
judged. The ledger is append-only and hash-chained, so those two fields name an exact, immutable
set, and the second leg is held to it. Both legs therefore count the same verdicts whether the
commands are a minute or a week apart. If that prefix cannot be honoured — a row from an older
writer that recorded none, or a verdict ledger that no longer extends it — the week is judged
two-arm and the row says so, rather than measuring through it.

**Judge both legs the same Sunday anyway**, for reasons that have nothing to do with the bound.
The second leg's split keeps taking its share of live traffic in the meantime, and every one of
those rows falls after the first leg was judged, so it is outside the factorial window and
decides nothing: you are spending the colony's traffic on an experiment that has stopped counting
it. Then, in the order you will meet them:
1. The nightly `ab --check` applies the stuck rule `STUCK_AFTER_S` (48 h) after the start. A leg
   still open at that point can be closed as stuck — a `held` row, and a **stuck abort is
   remembered**, so the candidate goes into `lost_tags` and cannot be retested until someone runs
   `ab --forget <id> --dimension prompt`.
2. Next Sunday's `release` sees it still splitting and refuses to start anything in either
   dimension (the one-experiment interlock), so a forgotten leg costs the following week too.
3. At `MAX_WEEKS` it is held on the two-week cap, with a reason blaming sample size rather than
   the fact that nobody judged it.

**A second Sunday is judged normally, not bounded to the first.** The bound applies when the
partner has been *judged more times than this leg has* — a fact about the two experiments, not
about the calendar. Within one round the leg judged first has an equal count and is unbounded;
the leg judged second sees a partner one ahead and is held to what it saw. By the following
Sunday the counts have caught up, so a factorial run that read inconclusive in week 1 is free to
count week 2's traffic in full. Nothing here needs `--week`, a clock, or the two commands landing
on the same date: run the second leg on Monday morning if you like. (Two earlier builds keyed
this on elapsed hours and then on the ISO week. The first refused the second Sunday outright and
livelocked the gate; the second silently unbounded a leg run after midnight, because ISO weeks
roll over on Monday while the gate's cycle starts Sunday 06:00.)

If for some reason a leg cannot be judged factorially — its partner had already closed before it
started, a third dimension was splitting alongside, an `ab_started` row is missing an arm label,
or the partner's judging row records no verdict prefix — the week is judged two-arm and **says
so**: the row carries `factorial_judged: false`, its `reason` gains `[factorial run, judged
two-arm: …]`, and a WARNING is logged. A row never claims a marginal it did not compute.

**A degraded week does not blacklist if anything else was splitting.** A two-arm number taken
while another dimension was live was measured across two uncontrolled conditions, which is the
thing the interlock exists to refuse; such a row carries `remember: false` and the
rejected-candidate memory skips it, so the candidate is retested once the confound clears. With
no neighbour the leg really was an ordinary single-dimension A/B, its tally is complete, and a
loss is remembered as always. `colony A/B inconclusive after 2 weeks` is remembered either way —
that is the guard against retesting a candidate that can never reach sample size, and a
confounded week is still a week in which it did not reach it.

**One fault refuses the week rather than degrading it: a verdict ledger that no longer matches
what was already measured over it.** The prefix check re-derives the hash chain over the recorded
rows, so an in-place edit is caught as well as an insertion or a deletion. When it fails, the gate
writes no row, moves nothing, and exits non-zero:
```
grid.weekly: error: verdict row 3 of the recorded prefix no longer hashes to its own chain value;
the file has been rewritten below the prefix. The prompt A/B cannot be judged over a verdict
ledger that no longer matches what its partner measured … Restore the ledger from the receipts,
or abort both legs with `grid.weekly ab --abort --dimension <d>`
```
Every other fault degrades, because "we cannot cross these two dimensions" still leaves one
honest measurement. This one says the colony's own fossil record changed underneath a measurement
already recorded, and a two-arm tally over the same file repairs nothing.

The keys a factorial week adds under `.evals.colony_ab`: `factorial` (on every `ab_started` row,
true or false), `factorial_judged`, `factorial_with`, `factorial_since_ts`, `factorial_until_ts`
and `factorial_until_verdicts` (both null on the leg judged first, whose partner has not been
judged this cycle and which therefore needs no bound), `cells`, and `interaction`.
`factorial_partner_abandoned` says the partner stopped splitting mid-window
without ever being judged, in which case the window is truncated there.
`factorial_judged` is on **every** A/B row — including the nightly `ab --check` stuck row and the
`ab --abort` snapshot, both of which are two-arm over the leg's own window and say so. Every A/B
row in either mode also carries `other_dimensions` — an advisory record of what else was
splitting when the row was written, including a hand-started split with no ledger row of its own.

The row also reports an **interaction** — whether the candidate wording helps the candidate
model more than it helps the incumbent. It is `advisory: True`, it carries its own reason
saying so, and **nothing reads it, and nothing may**: an interaction is estimated per cell, and
a cell is a quarter of the traffic a marginal pools. Deciding on it would be deciding on a
quarter-sample dressed up as the most sophisticated number in the file.

## Known limits (v0.1)
- Memories written by the bridge become visible to asks only after the 6 h dream+restart.
- `GRID.*` is unsigned on the wire (spec §8).
- Flux body carries both key styles; confirm against the live Flux and trim (Task 7 note).
- No backups on Skywave: `/srv/grid/colony-one/state` is lab-grade.
