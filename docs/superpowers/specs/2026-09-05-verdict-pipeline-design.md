# Sub-project 1 — the verdict pipeline (design)

- Date: 2026-09-05
- Status: **Draft for Nick's approval.** First sub-project of the self-improving
  program (`2026-09-05-self-improving-kannaka-program.md`).
- Repo: kannaka-grid. Runs on Skywave beside the colony. No thegrid changes.

## Purpose

Turn thegrid's selection record into a scored dataset of Kannaka's own decisions:
for every genome the relay proposed, what she was asked, what she answered, and what
happened to the child. The output feeds the weekly trainer (sub-project 2), the live
A/B (3), and her memory (4). It is the program's non-circular evaluator.

## Facts this design rests on (verified 2026-09-05)

- thegrid identifies a genome by `genome_id = sha256(bytes(op_indices))[:16]`
  (`history.py:15`), where op indices are positions in the ISA list the snapshot
  ships as `isa`. The relay knows the proposal as names, so it can compute the same
  id before the child is even born.
- `genome_stats(epoch, genome_id)` records `births`, `first_generation`,
  `max_generation`, `first_tick`, `last_tick`, `starvation_deaths`,
  `senescence_deaths`. thegrid's own tiers: `reproduced` = births ≥ 2; `growing` =
  generations ≥ 10 and births ≥ 5; `enduring` = 50/25; `established` = 100/100.
- `mutation_origins` does not tag Odin births specially; the join is on
  `child_genome_id`, so no thegrid change is needed.
- The fossil DB is SQLite at `/srv/grid/colony-one/state/history.sqlite3`, written
  by the colony process under its own lock; readers copy the file first.
- The relay already publishes `odin.proposed{request_id, genome_len, mind}` and
  `odin.rejected{request_id, reason}`, and can read `/api/state` for epoch/tick.
- P2's `prep_sft.py` consumes corpus records `{id, kind, tier, source, speaker,
  text, ...}` and only ever targets tier-1 records; `export_corpus.py` writes them
  as JSONL with a manifest.

## Components

### 1. Proposal log (relay, `grid/odin.py`)

On every `proposed` and `rejected`, append one JSON line to
`/srv/grid/<colony>/verdicts/proposals.jsonl`:

```json
{"ts": 1788634000123, "colony": "colony-one", "epoch": 1, "tick": 5322,
 "request_id": "01fb6f4d5ed2b2e3", "mind": "grid-colony-one", "model_alias": "agent-brain",
 "parent_genome": ["harvest", "..."], "parent_genome_id": "…16hex",
 "proposal": ["harvest", "..."] | null, "proposal_genome_id": "…16hex" | null,
 "outcome": "proposed" | "rejected", "reason": null | "unknown opcodes: [...]",
 "prompt_sha256": "…", "prompt_path": "prompts/<request_id>.txt"}
```

The full prompt is written once to `prompts/<request_id>.txt` (it is ~4 KB and is
the training input later). `model_alias` comes from the mind's config so the live
A/B (sub-project 3) can attribute verdicts to a brain. The writer is append-only
and never raises into the relay loop (log and continue).

`genome_id_of(names, isa)` is a pure function in `grid/proposal.py`:
`sha256(bytes(isa.index(n) for n in names)).hexdigest()[:16]`, tested against
thegrid's `history.genome_id` on a fixture (the submodule is importable in tests
on Linux; on Windows the test pins the algorithm literal).

### 2. Nightly verdicts (`grid/verdicts.py`, `grid-verdicts.timer`)

Once a day (and on demand):

1. Copy `history.sqlite3` to a temp file; open read-only.
2. For each proposal line not yet settled: look up `genome_stats(epoch,
   proposal_genome_id)`. A proposal **settles** when the colony's current tick is at
   least `SETTLE_TICKS` (default 3000 ≈ 33 min at 1.5 t/s) past the proposal's tick
   or the epoch retired, whichever first.
3. Write one verdict row per settled proposal to `verdicts.jsonl`, hash-chained
   (`chain = sha256(prev + canonical(row))`):

```json
{"request_id": "...", "proposal_genome_id": "...", "settled_tick": 8322,
 "births": 40, "generations": 7, "lifetime_ticks": 2900,
 "tier": "growing" | "reproduced" | "new" | "unborn" | "rejected",
 "fitness": 40, "chain": "…"}
```

`unborn` means thegrid never birthed that genome (the proposal was consumed but the
child died at birth or the id never appeared). `fitness` = births (v0.1; later
versions may weight generations). Verdict rows are immutable; a re-run appends
nothing for already-settled ids.

4. Publish `GRID.<colony>.verdict` per row (envelope + the row) so sub-project 4
   can remember it and the observatory can show it.

### 3. Corpus export (`grid/verdict_corpus.py`)

Emit training records in `export_corpus.py`'s shape, one per verdict at or above
a tier threshold (default `reproduced`):

```json
{"id": "odin-<request_id>", "kind": "odin", "tier": 1, "source": "grid-verdicts",
 "speaker": "kannaka", "text": "[\"harvest\", ...]",
 "context": "<the prompt text>", "verdict": {"tier": "growing", "fitness": 40},
 "provenance": {"colony": "colony-one", "epoch": 1, "tick": 5322,
                "chain": "…", "history_sha256": "…"}}
```

- `text` (the target) is her proposal; `context` is the prompt. Both are hers by
  construction: the prompt was rendered by kannaka-grid, the answer authored by
  her brain. Nothing inbound is a target (ADR-0057 provenance rule).
- Rejected and `unborn` proposals are exported to a separate `negatives.jsonl`
  for a later preference-pair (DPO) version; v0.1 trains on positives only
  (rejection-sampling SFT).
- Passing chronicle lines are exported the same way with `kind: "chronicle"`
  (target = the published line, context = the question with its facts).
- A manifest with counts by tier and the verdict chain head accompanies every
  export. The exporter never uploads; the weekly trainer (sub-project 2) pulls.

## Data flow

```
relay (proposed/rejected) ──▶ proposals.jsonl + prompts/<id>.txt
                                     │ nightly
history.sqlite3 (copy) ──▶ verdicts.py ──▶ verdicts.jsonl (hash-chained) ──▶ GRID.<colony>.verdict
                                     │ on demand
                          verdict_corpus.py ──▶ ~/.kannaka-corpus/grid/<week>/{records,negatives}.jsonl + manifest
```

## Failure handling

- The proposal log never blocks a proposal: a write failure is logged and the
  proposal still lands.
- The nightly job works on a copy of the DB and is idempotent; a crash mid-run
  loses nothing (the chain is appended row by row, fsync per row).
- A proposal whose epoch retired before settling settles at retirement with
  whatever `genome_stats` says.

## Security and provenance

- Everything stays on Skywave under `/srv/grid/<colony>/verdicts/` (nick, 0700);
  exports go to the operator's corpus dir, never a public repo (held-back rule).
- The verdict chain and the fossil DB hash make every training example traceable
  to a colony state anyone with the DB can re-derive.

## Testing

- Pure: `genome_id_of` vs a fixture of thegrid ids; verdict tiering; chain
  verification; settle rule; corpus record shape against `prep_sft.py`'s
  expectations (a test imports its `build()` on Linux).
- Integration (opt-in, Linux): run the smoke colony for 3000 ticks with the stub
  mind, then `verdicts.py` against its fossil DB, assert at least one settled row.
- Mutation-sensitive per the standing rule.

## Acceptance

1. After one night on Skywave, `verdicts.jsonl` has rows for every proposal older
   than `SETTLE_TICKS`, the chain verifies, and at least one row is `reproduced`
   or better.
2. `verdict_corpus.py` produces a records file in `export_corpus.py`'s record
   contract (`id, kind, tier, source, speaker, text`, plus `context`). Note:
   P2's `prep_sft.py` today builds examples only from `kind == "voice"`; teaching
   it `odin` and `chronicle` (context as the user turn, text as the target) is
   the first task of sub-project 2, not this one.
3. `GRID.colony-one.verdict` messages appear on the bus.

## Open questions for Nick

1. `SETTLE_TICKS` 3000 — settle faster (noisier) or slower (fewer, surer rows)?
2. Positive threshold `reproduced` (births ≥ 2) or `growing` (gens ≥ 10, births ≥ 5)?
   Recommendation: `reproduced` for v0.1 to get volume; raise once weekly sets
   exceed a few hundred rows.
3. Keep the full prompt text per proposal (~4 KB × ~1200/day ≈ 5 MB/day) or only
   its hash plus the parent genome, regenerating the prompt at export? Recommendation:
   keep the text; disk is cheap and the render function may change.
