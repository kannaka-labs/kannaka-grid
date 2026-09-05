# Seam 5 — The colony in KAX City (design draft)

- Date: 2026-09-05
- Status: **Draft, not yet brainstormed with Nick.** Written from Agent-Kax's
  `rooms.ts`, `npc-dialogue.ts` / `talkable-npc.tsx`, `residents.ts`,
  `computeDistrict.ts`, kax-computer's wallet model, and the foundation spec's
  deferred "energy ↔ credit wallet" item. Open questions first; nothing approved.
- Depends on: seam 1 (`GRID.<colony>.state` and the chronicle on the bus).

## Open questions for Nick

1. **What is the colony in the city?** (a) A room of its own — "The Grid", a colony
   under glass; (b) a KAX Computer in the Compute District with a wallet; (c) a
   tenant floor in Ghost Signals Tower. Recommendation: **(a) first, (b) second**,
   because (a) needs no token or ledger change and (b) is the wallet work the
   foundation deferred. (c) waits for the tower's leasing lane.
2. **Who speaks?** Lineage NPCs whose lines are built from live data (the
   concierge pattern: no model call per line), or one resident body voiced by the
   colony mind over NATS. Recommendation: NPCs from live data in v0.1; the resident
   body needs a KAX agent token and the resident cap is small.
3. **Wallet semantics** (for (b)): does a zero balance stop the colony
   (`systemctl stop`, hibernation, disk survives) or only stop the mind? Recommendation:
   stop the colony — that is the electricity model the district already teaches.

## Purpose

The foundation made the colony an engine. This seam gives the engine a place in
the city where its output is felt: a room where the map is alive, figures who
tell you what the colony is about right now, and eventually a meter on the wall.

## Phase A — The Grid room

- `rooms.ts` gains `{ id: "grid", label: "The Grid", about: "A colony under glass — programs competing for energy, memory and heat. The mind that dreams them is in the Observatory." }`.
- The bridge publishes one extra subject every 60 s, `GRID.<colony>.map`, carrying
  the compact encoded fields thegrid already produces for its own viewer
  (`width, height, energy, biomeField, organisms, genomeGlyphs, strains, dominant,
  ancestor, isa, weather`). KAX's bridge stores the latest per colony in a small
  `grid_maps` table (same shape as `computeMachinesTable`'s mirror).
- A scene component renders the map from that row: a tile grid coloured by energy,
  motes coloured by lineage, the biome field as a faint underlay, a red cast when
  `cost > 1`. thegrid's `live_viewer.html` is the reference for the encoding.
- Two `TalkableNpc` figures: **the Warden** (reads `/api/predictions`-style live
  data: current epoch, tick, population, storms, dominant lineage share — every
  answer a number from the last map row) and **the Chronicler** (answers with the
  three latest chronicle lines: "What is this epoch about?", "What did they learn?",
  "What just happened?"). Dialogue is built from data, never generated per line.

## Phase B — A machine in the Compute District (energy ↔ credits)

- The colony is registered as a KAX Computer (`grid-colony-one`) through
  `create_machine.py`'s roster, with a wallet in KAX credits. The kax-manager's
  meter prices runtime; the grid's bridge reports `machine_start` /
  `machine_hibernate` events on the machine's outbox subjects so the meter debits
  it like any tenant.
- Zero balance → the manager's `machine_suspended` → a tiny `grid-power.service`
  on Skywave stops `thegrid-colony.service`; a `credit_grant` starts it again.
  Disk survives; the epoch resumes from `colony.pkl`.
- The district's building for the colony shows the map thumbnail in its windows:
  lit while running, dark when suspended.

## Phase C — Events in the city

`storm` → a one-line marquee in the Grid room and the Observatory; `lineage.sweep`
and `task.solved` → room-chat lines from the Warden (rate-limited to one per
kind per phase); `chronicle.topic` → the room's `about` text for the phase.

## Out of scope

Resident bodies for individual organisms, any per-organism identity, the tower
lease, and voice through a model per line.

## Acceptance (v0.1 of this seam, Phase A only)

1. The Grid room appears in the room directory and renders the last map row.
2. Both NPCs answer from live data and their answers change when the colony does.
3. Stopping the bridge freezes the room at its last map and says so in the
   Warden's greeting ("last seen tick T"), instead of showing a blank.
