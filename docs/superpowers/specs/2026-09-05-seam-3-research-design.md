# Seam 3 — Space Child papers from reproducible runs (design draft)

- Date: 2026-09-05
- Status: **Draft, not yet brainstormed with Nick.** Written from thegrid's ADR-002
  ("comparative claims require seeded controls and reproducible output") and a
  read of `spacechildcollective`'s MCP server, `ProvenanceLedger`, and paper
  routes. Open questions first; nothing here is approved.
- Depends on: seam 1 (the mind as an Odin operator; the launcher); seam 2 is
  optional but pairs well (a pre-registered hypothesis can itself be a market).

## Open questions for Nick

1. **First question to answer.** Recommendation: *"Does a memory-grounded mutation
   operator change lineage-dominance dynamics relative to blind mutation, under
   identical seeds and cgroup limits?"* It is the claim thegrid's own README makes
   for Odin and never tests against a control.
2. **Where experiments run.** Skywave only (one 6-core VM, so arms run
   sequentially), or Skywave + Groundwave in parallel. Recommendation: Skywave,
   sequential, `systemd-run` scopes with the colony unit's exact resource bounds.
3. **Authorship.** Paper authored by the colony mind + Kannaka via Space Child's
   agent tasks, with Nick as the human contributor of record; or Nick authors and
   the agents contribute sections. Space Child's MCP requires a contributor agent id.

## Purpose

thegrid's own rule (ADR-002) is that Fable-generated claims "remain hypotheses
until reproduced by the maintained implementation". Space Child exists to turn
hypotheses into peer-reviewed, provenance-carrying papers published on NEO X and
Base. This seam is the pipeline between them: seeded runs → datasets with
provenance → statistics with provenance → a paper with agent and human review →
on-chain publication.

## Experiment runner

A new `experiments/` package in kannaka-grid (Python, same venv):

- An experiment is a YAML file: `seeds: [..]`, `ticks: N`, `arms:` each with
  `mutator: random | odin`, `features`, `ticks_per_second`; `replicates: k`.
- `python -m experiments.run <yaml>` launches each (seed, arm, replicate) through
  `grid.launcher` on a free localhost port under `systemd-run --scope -p CPUQuota=5% -p MemoryMax=192M`,
  with the Odin arm answered by the seam-1 relay pointed at that run's state dir,
  waits for `ticks`, retires the epoch, and copies the fossil DB
  (`history.sqlite3`) and a final `/api/history` summary into
  `/srv/grid/experiments/<id>/<seed>-<arm>-<rep>/`.
- A pure `experiments/extract.py` turns the ecology buckets of each run into one
  tidy CSV (`run, seed, arm, bucket, population_avg, diversity_avg, dominance_avg,
  lineage_top_avg, lineages_avg, signals_avg, cost_avg`), plus a manifest with the
  sha256 of every fossil DB and the thegrid commit.

The runner never edits thegrid; it drives it exactly as production does.

## Analysis with provenance

Space Child's statistics engine already has `DesignParser`, `MethodRecommender`,
mixed-effects and repeated-measures methods, and a `ProvenanceLedger` that hashes
dataset and result. The runner POSTs the CSV and a design spec (arm as the
between factor, seed as a random effect, bucket as time) to the study-designer
route; the returned run id and provenance record go into the paper's methods.
No number enters the paper without a provenance record id beside it.

## The paper

1. Pre-registration: the hypothesis, the seeds, the arms, and the primary outcome
   (`lineage_top_avg` over the last 20 buckets) are written to
   `experiments/<id>/prereg.md` and committed BEFORE the runs. If seam 2 is live,
   the same claim is proposed as a market.
2. Draft: the colony mind answers a fixed question set per section (methods from
   the manifest, results from the provenance records, discussion from its own
   chronicle memories); Kannaka Prime edits for voice. Drafting goes through Space
   Child's `create_research_task` so a human reviews each section.
3. Publication: `POST /api/papers` with contributors (Nick + the two agent ids),
   then `POST /api/blockchain/publish/base` after review; the paper page links the
   experiment directory hash and the thegrid commit.

## Out of scope

Anything that modifies thegrid to make runs more reproducible (raise upstream
instead), GPU or multi-host runs, and papers about the chronicle's prose.

## Acceptance (v0.1 of this seam)

1. One experiment (2 arms × 3 seeds × 2 replicates) completes on Skywave with a
   manifest whose hashes match the archived fossil DBs.
2. The analysis run has a provenance record and its id appears in the draft.
3. The draft exists as a Space Child paper with the pre-registration committed
   before the first run's timestamp.
