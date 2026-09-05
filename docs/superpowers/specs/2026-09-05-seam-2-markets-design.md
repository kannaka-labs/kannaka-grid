# Seam 2 — GhostSignals markets on the colony (design draft)

- Date: 2026-09-05
- Status: **Draft, not yet brainstormed with Nick.** Written from the foundation
  spec and a read of `ghostsignals`, Agent-Kax `routes/predictions.ts`,
  KAX-ADR-0004, and the command-center `propose_prediction` tool. The open
  questions at the top decide the shape; nothing here is approved.
- Depends on: seam 1 (`GRID.<colony>.*` on the bus; `/api/state` and `/api/history`
  on Skywave; the chronicle).

## Open questions for Nick

1. **Which venue?** (a) The Kannaka Labs registry via `propose_prediction`
   (falsifiable claim + settle-by, escrow-funded after review, the proposer cannot
   trade its own market), or (b) KAX's own `/predictions` (LMSR, Kannaka Labs
   settlement, floor ledger). KAX-ADR-0004 federates the two read-side; stakes stay
   native. Recommendation: **(a)** for v0.1 — it already exists as a tool, needs no
   KAX token, and the review step is a safety net against garbage claims.
2. **Who proposes?** The colony mind as its own principal (`grid-colony-one`, needs
   a KAX agent identity the moment the venue is KAX) or Kannaka Prime on its behalf.
3. **Cadence and cap.** How many open colony markets at once? Recommendation: at
   most 3 per colony, one per claim family, new ones only when an old one settles.

## Purpose

thegrid produces falsifiable, machine-settleable claims for free: "lineage 2 still
holds ≥ 60 % at tick 40 000", "`nand` is first-solved before tick 25 000", "epoch 7
ends in extinction". Markets on those claims give the constellation something to
predict that has ground truth, a settlement source nobody can argue with, and a
Brier record that says which minds actually understand the colony.

## Claim families (v0.1)

Each family is a pure function from a `GRID.*` event stream plus the colony's
own endpoints to a claim text and a settlement rule. All settle from the fossil
record or the state snapshot, never from the chronicle (which is LLM output).

| Family | Trigger to propose | Claim text | Settles by | Settled from |
|---|---|---|---|---|
| `sweep-holds` | `lineage.sweep` for lineage L at share s | "Lineage L holds ≥ 0.50 of colony-one at tick T+5000" | tick T+5000 | `/api/state.strains` at or after the tick |
| `task-first` | epoch start, per unsolved task | "colony-one first-solves `nand` before tick 25 000 in epoch E" | tick 25 000 | `/api/state.tasks` keys |
| `epoch-survives` | `epoch.started` | "Epoch E of colony-one reaches tick 50 000 without extinction" | tick 50 000 | `/api/history.epochs` row for E |
| `storm-count` | first storm of an epoch | "colony-one records ≥ N storms by tick T" | tick T | `/api/state.weather.storms` |

Claim text is templated and carries the colony slug, the epoch, and the exact
tick, so it is falsifiable on its face.

## The oracle

The bus does not retain `GRID.<colony>.state`, so settlement needs its own durable
record. A new unit on Skywave, `grid-oracle.service`, appends every
`GRID.<colony>.state` payload it receives (or polls `/api/state` at the same
60 s cadence) to `/srv/grid/<colony>/oracle/state.jsonl`, hash-chained the way
kax-computer's ledger is (`chain = sha256(prev + canonical(row))`). A settlement
is a signed statement `{claim_id, verdict, evidence_row_hash, chain_head}` so
anyone can verify the verdict against the log. The oracle publishes
`GRID.<colony>.market.settled` with that statement.

## Proposal flow

1. The bridge's chronicle phase boundary is the proposer's clock. Every phase the
   proposer evaluates the claim families against the current state and the open
   set; if a family has no open market and its trigger fired, it proposes one via
   `propose_prediction` (venue (a)) with the claim text, the settle-by date derived
   from the tick and the colony's ticks-per-second, and a link to the oracle log.
2. The registry's review gate accepts or refuses. Accepted markets are recorded in
   `/srv/grid/<colony>/oracle/open.json`.
3. At settle-by, the oracle reads its own log, decides, publishes the verdict, and
   files it back to the venue (registry settlement call, or the KAX floor ledger
   under venue (b)).

## Out of scope

Trading (existing tools), price display (the Trading Floor already renders the
hub), federation of leaderboards (ADR-0004 phase 1), and any market whose truth
depends on the chronicle text.

## Acceptance (v0.1 of this seam)

1. One market per family proposed and accepted over one epoch.
2. Every settlement verifiable from the oracle log by hash.
3. A refused proposal leaves no state behind and is not re-proposed until the
   family's trigger fires again.
