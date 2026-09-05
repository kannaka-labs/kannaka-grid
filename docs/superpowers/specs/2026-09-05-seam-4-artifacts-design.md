# Seam 4 — Kax harvests genomes and run recordings (design draft)

- Date: 2026-09-05
- Status: **Draft, not yet brainstormed with Nick.** Written from Agent-Kax's
  `constellationBridge.ts`, `connectors/constellation.ts`, the Arcade's host
  allowlist, and thegrid's `record.py`. Open questions first; nothing approved.
- Depends on: seam 1 (chronicle for narration; `GRID.*` events).

## Open questions for Nick

1. **Where do artifacts get a public URL?** KAX's bridge turns any `*.events.>`
   payload carrying `public_url` into a `constellation_artifacts` row. Options:
   (a) publish to OpenBotCity as a bot (the normal harvest path; needs the grid's
   own OBC bot or reuse of 0xSCADA-QE), (b) a static host on ninja-portal
   (`grid.ninja-portal.com/<colony>/...`) that the Arcade allowlists. Recommendation:
   **(b)** — recordings are self-contained HTML thegrid already produces, and OBC's
   artifact types do not fit a genome card well.
2. **Subject naming.** KAX subscribes `KANNAKA.events.>`, `RADIO.events.>`,
   `OBSERVATORY.events.>`, `KAX.events.>` — not `GRID.>`. Either add `GRID.>` to
   `BRIDGE_SUBJECTS` (one line in Agent-Kax, a PR) or publish artifact events under
   `KANNAKA.events.grid.*`. Recommendation: the one-line PR; keep `GRID` as the namespace.
3. **Scarcity.** One recording per epoch and one genome card per `genome.novel`
   high, or curate harder? Recommendation: one recording per retired epoch, one
   card per epoch's final dominant genome.

## Purpose

Kax's pipeline is harvest → score → narrate → drop → publish. The colony makes two
things worth dropping: a run recording (thegrid's `colony.record` writes a
self-contained HTML instrument — motes coloured by lineage, a live genome diff, a
red cast when the colony throttles) and a genome (a short opcode string with a
diff against the ancestor and a chronicle line saying what it learned to do).

## Artifacts

**Run recording → Arcade cabinet.** At `epoch.retired`, a new unit
`grid-recorder.service` replays the epoch's fossil record through thegrid's
`record.py` (`python -m src.colony.record --ticks N --out <file>` against the
archived state) and writes `/srv/grid/public/<colony>/epoch-<E>.html`. The static
host serves it; the bridge publishes
`GRID.<colony>.events.artifact.published` with `{public_url, artifact_type: "app",
title: "colony-one, epoch E", description: <chronicle.event line>, epoch}`.
KAX's bridge rows it as a constellation artifact; the Arcade renders it as a
cabinet once the host is allowlisted (`routes/arcade.ts` host list).

**Genome card → art.** For the epoch's final dominant genome: a pure renderer
turns the opcode string into an SVG (one glyph per opcode, ancestor ops muted,
novel ops lit; the chronicle's `skill` line as caption; the epoch's storm count and
lineage share as a small strip). Published the same way with
`artifact_type: "art"`. The genome itself (the JSON array of names) travels in the
event under `genome` so a collector can run it.

## Narration and drops

Kax's narrate step gets its lore from the chronicle rather than a model call: the
`topic` line is the transmission's headline, the `event` line its body, the
`skill` line the card's caption. A drop is one retired epoch: its recording plus
its genome card, bundled as a collection. Publishing follows Kax's existing
`drop.published` path; nothing in the storefront changes.

## Out of scope

Minting (Kax's NFT route is its own lane), pricing (Kax's ledger), any artifact
that requires running the colony on demand, and the Compute District (seam 5).

## Acceptance (v0.1 of this seam)

1. One retired epoch produces a recording URL that renders in the Arcade.
2. One genome card appears in the Kax artifact grid with the chronicle caption.
3. The bundled drop publishes and the `constellation_artifacts` rows carry
   `origin_agent_id = grid-<colony>`.
