# kannaka-grid — foundation design

- Date: 2026-09-05
- Status: Draft for review
- Scope: seam 1 of 5 — "Kannaka as the colony's mind". Markets, Kax artifacts,
  Space Child papers, and city embodiment are consumers of this foundation and
  each get their own spec later.

## 1. Purpose

[thegrid](https://github.com/EckmanTechLLC/thegrid) is a contained
artificial-life laboratory: organisms are tiny programs on a ~50-op instruction
set competing for regenerating energy, a hard memory cap, and thermal headroom.
The LLM is kept out of the tick loop by design; it may only propose a genome
variant at reproduction, at a metabolic cost, through a file queue thegrid
calls the **Odin operator**.

kannaka-grid makes a running colony a citizen of the Kannaka constellation and
makes Kannaka its mind. The colony is treated as an **engine**: it generates
raw dynamics (storms, sweeps, extinctions, task solves, signal dialects) and
this project distills them into a vocabulary — events, topics, skills, a
chronicle — that KAX City NPCs, GhostSignals markets, Kax artifact drops, and
Space Child papers can all consume. Mutation authorship carries memory across
epochs because the same HRM that absorbs the events answers the Odin queue.

## 2. Constraints that shaped the design

- **thegrid is not ours and has no license file.** Nothing here modifies it.
  It is pinned as a git submodule and driven from outside through its HTTP API
  and its Odin file queue. Contributing upstream is a separate conversation
  with EckmanTechLLC.
- **The Odin queue is a filesystem queue** under the colony host's state dir,
  and the observer binds LAN ports only. Everything that talks to the colony
  runs on the same host.
- **Host is Skywave** (`nick-debain1`, 10.30.0.20, Debian 13, 6 cores / 16 GiB
  / 120 GB), one of two lab VMs on AE0RM's Proxmox behind Cloudflare Zero
  Trust (WARP required; `ssh debain1`). Groundwave (10.30.0.10) is the control
  plane (Komodo + cloudflared tunnel, the only door in). Skywave already runs
  Docker, `kax-manager`, the LiteLLM gateway (`127.0.0.1:4000`), and a kannaka
  swarm node. **No backups exist on either VM** — colony history is lab-grade
  until that changes. Source: `NickFlach/kax-computer` `docs/DEPLOY.md`.
- **The colony must never depend on the fusion being up.** Every unit here
  degrades to "the colony runs exactly as thegrid runs alone".
- **thegrid ADR-002 containment holds**: organisms get no credentials,
  filesystem, network, or general code execution. Real-resource feedback stays
  opt-in and outside the organism VM.

## 3. Architecture

One repository, `NickFlach/kannaka-grid`, Python 3.12 (matches thegrid and the
kax-computer manager). Deployed to Skywave as systemd units under `/srv/grid`.

```
thegrid colony ──/api/stream──▶ bridge ──distill──▶ NATS  GRID.<colony>.event.<kind>
      │                            │                Flux  entity grid:<colony>
      │                            └──remember──▶ colony mind (kannaka HRM)
      │                                                │ dream (scheduled)
      │                                                └──ask──▶ chronicle ──▶ NATS GRID.<colony>.chronicle.<kind>
      └──odin queue──▶ relay ──ask──▶ colony mind ──reply──▶ relay ──proposal.json──▶ odin queue
```

### 3.1 Units

| Unit | One job | Depends on |
|---|---|---|
| **colony** | thegrid's own colony service, `main` branch pinned at `67b9833ab755a83df2e1d28f2200f4651ad26384` (2026-09-02), run under the cgroup limits its unit already declares (5% CPU, 192 MiB, no swap). thegrid's observer binds a LAN port by default; v0.1 either passes a bind address if `colony.live` accepts one (to be verified in the plan) or firewalls the port to Skywave's LAN, which is already behind Zero Trust. | nothing here |
| **bridge** (`grid-bridge.service`) | Tail `/api/stream`, poll `/api/history`; distill snapshot pairs into events; publish to NATS and Flux; write throttled memories into the mind; every N ticks ask the mind for the chronicle and publish it. | colony (read), NATS, mind (write) |
| **relay** (`grid-odin.service`) | Watch the Odin queue dir; turn `request.json` into a NATS ask to the mind; validate the reply as a genome; write `proposal.json` atomically. | queue dir (rw), NATS, mind |
| **mind** (`grid-mind.service`) | A kannaka swarm node `grid-<colony>` with its own `KANNAKA_DATA_DIR=/srv/grid/<colony>/memory`, `kannaka swarm serve` with `ask` enabled, LLM through the LiteLLM gateway on a scoped virtual key. Dreams on a timer. | NATS, gateway |

The colony name in subjects is the habitat name thegrid reports in
`/api/state.name` (e.g. `colony-one`), lowercased and slug-safe.

### 3.2 Pure cores (no network, fully unit-tested)

- `grid/distill.py` — `distill(prev: Snapshot, curr: Snapshot) -> list[Event]`.
  All thresholds are module constants with names, not literals.
- `grid/proposal.py` — `parse_request(path) -> Request`,
  `validate(reply_text, request) -> Genome | Rejection`,
  `render_prompt(request) -> str`.
- `grid/envelope.py` — the canonical constellation envelope builder.

Daemons are thin loops over these cores, in the ledger-core / tier-core house
style used across the constellation.

## 4. The distilled vocabulary

Every event is derived from fields thegrid already reports in `/api/state`
(`snapshot()` in `src/colony/live.py`) or from the fossil record summary at
`/api/history`. Thresholds below are the v0.1 defaults; they live in one config
block and the spec is updated when they change.

| Event kind | Fires when | Payload (beyond envelope) |
|---|---|---|
| `epoch.started` | `epoch` increases | `epoch, seed, founders` |
| `epoch.retired` | `epoch` increases (fires for the previous epoch) or `/api/history` marks it finished | `epoch, ticks, births, deaths, extinct: bool` |
| `storm` | `weather.storms` increments | `tick, drought_quadrant, bloom_quadrant, source` |
| `lineage.sweep` | `lineage_top` crosses **0.60** upward (re-arms below 0.45) | `tick, lineage, share, genome_glyph` |
| `lineage.extinct` | a strain count goes from >0 to 0 | `tick, lineage` |
| `task.solved` | a new key appears in `tasks` (first solves) | `tick, task, genome_glyph` |
| `population.crash` | population falls by **≥40%** across a window of 200 ticks | `tick, from, to` |
| `thermal.throttle` | `cost` rises above **1.5** (re-arms below 1.2) | `tick, cost, heat` |
| `genome.novel` | max novel-opcode count across the population reaches a new epoch high | `tick, novel_ops, genome_glyph` |
| `signal.dialect` | active signals stay above **8** for 500 consecutive ticks | `tick, active_signals` |
| `odin.proposed` | the relay wrote a proposal | `request_id, genome_len, mind` |
| `odin.rejected` | the relay refused a reply | `request_id, reason` |

Hysteresis (re-arm thresholds) exists so a value hovering at a threshold does
not fire on every snapshot.

### 4.1 Subjects and envelope

- `GRID.<colony>.event.<kind>` — one message per event.
- `GRID.<colony>.state` — the full snapshot every 60 s (what the Compute
  District does for `KAX.machines.status`; not JetStream-retained).
- `GRID.<colony>.chronicle.<topic|skill|event>` — see §6.

Every payload carries the constellation canonical envelope (the shape
`Agent-Kax` `constellationBridge.ts` and `kannaka-radio` agree on):

```json
{"schema_version": "1.0", "ts": 1788000000000, "agent_id": "grid-colony-one",
 "colony": "colony-one", "epoch": 12, "tick": 40213, "...": "event fields"}
```

Because KAX's bridge already subscribes to `*.events.>` style subjects, adding
`GRID.>` to its `BRIDGE_SUBJECTS` is a one-line consumer change and is **out of
scope here**.

### 4.2 Flux

If `FLUX_URL` is set, the bridge upserts one entity `grid:<colony>` on stream
`grid` via `POST /api/events` with the snapshot's scalar fields (population,
tick, epoch, heat, cost, memory, dominant genome glyph, weather). thegrid's own
`src/flux/client.py` already speaks this API; the bridge uses the same shape.
Unset → skipped, logged once at start.

## 5. The Odin relay

thegrid's `OdinMutator` writes `request.json` to the queue dir (default
`~/.local/state/thegrid/operator`; here `/srv/grid/<colony>/operator`) and
consumes `proposal.json` at the next qualifying birth. The request carries
`request_id`, the parent's telemetry, the genome as instruction names, the
full `instruction_set` (name, cost, doc), an `ecology` note, and a `directive`.

Relay loop:

1. Watch the queue dir (inotify with a polling fallback).
2. On a new `request.json`: build a prompt from the request — the directive,
   the ecology, the instruction set, the parent genome and telemetry, and the
   mind's own recall — and send a NATS request to `KANNAKA.ask.grid-<colony>`
   with a **30 s** timeout.
3. Validate the reply: extract a JSON array of instruction names; every name
   must be in **the request's own `instruction_set`**; length 1..64 (thegrid's
   genome cap); must be non-identical to the parent (a no-op proposal wastes
   the parent's energy).
4. Write `proposal.tmp` then `os.replace` to `proposal.json`, only if
   `request.json` still exists and its `request_id` matches.
5. Publish `odin.proposed` or `odin.rejected`.

If the mind is unreachable or the reply fails validation, nothing is written.
thegrid then applies its blind-mutation fallback, which is its unmodified
behaviour. The relay is the **trust boundary for genomes**: an LLM can produce
a bad genome but cannot produce anything that is not a genome.

## 6. The colony mind and the chronicle

The mind is a normal kannaka swarm node. What makes it the colony's mind is
what gets written into it and what gets asked of it.

**Writes** (by the bridge, via `kannaka remember --category grid --tags
<colony>,<kind>`): one memory per distilled event, throttled to at most one
memory per event kind per **5 minutes**, plus one epoch summary at
`epoch.retired` built from `/api/history` (dominant genome diff vs ancestor,
task firsts, storm count, lineage count, cause of extinction if any). Rejected
Odin replies are also remembered, so the mind learns what does not work.

**Dreams**: `kannaka dream` on a systemd timer, every **6 hours**, so
consolidation happens off the tick path.

**Chronicle**: every **2000 ticks** (one climate phase in thegrid), the bridge
asks the mind three grounded questions and publishes each answer:

| Subject | Question shape | Constraint |
|---|---|---|
| `chronicle.topic` | "What is this epoch about, in one sentence?" | ≤ 280 chars |
| `chronicle.skill` | "Name the capability the dominant genome carries that the ancestor did not, and the opcodes that implement it." | must cite ≥1 opcode from the ISA |
| `chronicle.event` | "What is the single most important thing that happened since the last chronicle?" | ≤ 280 chars, must reference a tick |

Answers that fail their constraint are not published; the failure is logged.
The chronicle is the engine output the other four seams consume.

**LLM budget**: the mind's `[llm]` points at the LiteLLM gateway with a
per-node virtual key (30-day budget, rpm/tpm, model-scoped) minted the same
way `create_machine.py` mints machine keys. Thinking has a budget like every
other tenant on Skywave.

## 7. Failure handling

- Bridge reconnects to `/api/stream` with exponential backoff (1 s → 60 s).
  Events come from snapshot pairs, so a gap loses that gap's events and no
  more. Epoch changes are read from the `epoch` field, never inferred.
- NATS via `nats-py`, which raises on server `-ERR`. Publishes are awaited;
  a failed publish is logged with the subject and the event is dropped, never
  silently "recorded".
- Flux optional; absent URL → skip, one log line.
- Relay writes only atomically and only against a matching live request.
  Stale `proposal.json` files (request gone) are renamed `.stale-<ts>.json`
  and counted.
- Mind unreachable → relay stops proposing, bridge stops remembering, both
  keep running and retry; the colony is unaffected.
- All three daemons are `Restart=always` with `StartLimitBurst` so a bad
  config backs off rather than hammering the colony or the bus.

## 8. Security

- Colony containment is thegrid's, unchanged. The bridge and relay run as
  `nick`, read colony state over localhost HTTP, and write only to the queue
  dir and `/srv/grid/<colony>/`.
- The relay validates genomes strictly (§5).
- The mind's LLM key is scoped and budgeted; the master key never leaves
  `gateway.env`.
- The swarm bus permits anonymous publish, so `GRID.*` is **unsigned on the
  wire**, like every other constellation event today. Consumers must treat it
  as data. Ed25519-signed envelopes in the `kax-computer` style are a v0.2
  item; this spec does not pretend the bus is trusted.
- No secrets in the repo. NATS creds from `~/.kannaka-nats.env`; gateway key
  from the mind's own kannaka config on Skywave.

## 9. Testing

- `distill.py` and `proposal.py` are tested against fixtures captured from a
  real `/api/state` and a real `request.json`. Every threshold and every
  validation rule has a test that FAILS under a deliberate mutation before it
  is accepted (a guard that survives its own mutation is vacuous).
- One integration smoke: run a short local colony (`--ticks 3000`), run the
  relay against a stub mind that returns a fixed genome, assert
  `proposal.json` lands with the right `request_id` and that `odin.proposed`
  is published to a local `nats-server`.
- Acceptance on Skywave is a receipt: a `request.json` appears, a
  `proposal.json` appears, `odin.proposed` shows in a bus capture, and the
  mind's memory count rises. Screenshots or captured payloads go in the PR.

## 10. Deployment

```
/srv/grid/
  thegrid/                      # submodule checkout, pinned
  <colony>/
    state/                      # thegrid state dir (SQLite fossils, snapshots)
    operator/                   # Odin queue: request.json / proposal.json
    memory/                     # KANNAKA_DATA_DIR for the mind
  grid.env                      # COLONY_URL, NATS creds file ref, FLUX_URL, thresholds
systemd/
  thegrid-colony.service        # thegrid's unit, ports on 127.0.0.1
  grid-bridge.service
  grid-odin.service
  grid-mind.service             # kannaka swarm serve
  grid-mind-dream.timer         # kannaka dream every 6h
```

Bootstrap order: colony → mind → bridge → relay. Each later unit tolerates the
earlier one being absent.

## 11. Deliberately deferred (v0.2+)

- **Energy ↔ KAX credit wallet.** Wire the colony's energy budget to a wallet
  so the colony hibernates at zero balance under the same electricity model as
  the Compute District. A consumer of this foundation, not part of it.
- **Signed `GRID.*` envelopes.**
- **Grid room / lineage NPCs in KAX City**, market proposals from the
  chronicle, Kax harvesting of genomes and run recordings, Space Child papers
  from seeded reproducible runs. Each is its own spec and each consumes the
  subjects defined in §4 and §6.
- **Colony Two / Three** (thegrid's experimental branches). v0.1 runs the
  `main` reference habitat only.

## 12. Acceptance criteria for v0.1

1. A colony runs on Skywave under thegrid's cgroup limits with the observer on
   localhost.
2. `GRID.<colony>.event.*` messages appear on the swarm bus for at least
   `epoch.started`, `storm`, and one of `lineage.sweep` / `task.solved` during
   a real run.
3. The mind's memory count grows from bridge writes; `kannaka recall` on the
   mind returns colony facts.
4. At least one `proposal.json` is written by the relay from a real
   `request.json`, and thegrid's own telemetry shows `accepted ≥ 1` for the
   Odin mutator.
5. Three chronicle messages (topic, skill, event) publish after a climate
   phase.
6. Stopping the mind, bridge, or relay does not change the colony's tick rate
   or behaviour.
