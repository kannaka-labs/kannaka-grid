# Self-improving Kannaka — program spec (decomposition)

- Date: 2026-09-05
- Status: **Draft for Nick's review.** Decomposes the program into sub-projects;
  only sub-project 1 has a full spec (`2026-09-05-verdict-pipeline-design.md`).
  Each later sub-project gets its own brainstorm, spec, and plan when its turn comes.
- Builds on: kannaka-memory **ADR-0057** (open-weight brain: `kannaka-brain` served on
  debain2, LoRA adapter as the ownable artifact, HRM as the store of record for facts,
  qBraid GPU as the trainer with spend gates; P0–P2 done 2026-09-05, adapter
  `kannaka-brain-v1`, held-out ppl 104 → 4.0, $0.84), **ADR-0056** (provenance),
  **ADR-0053/0049** (HRM recall physics), and kannaka-grid's foundation (the colony as
  an engine; seam 1 live on Skywave).
- Nick's constraints (2026-09-05): a continually evolving model; **weekly training and
  release at about $5/week**; the base model should not announce itself; and try to
  make the HRM part of inference itself, not just retrieval beside it.

## The one idea

A self-improving loop is only as honest as its evaluator. Judging Kannaka's voice
with a model is circular (ADR-0057 open question 4). But one class of her outputs is
judged by the world: every genome she proposes to thegrid is later scored by
selection. The fossil record says whether the child reproduced, how many births it
produced, how many generations it spanned. That verdict joins back to the exact
prompt she saw. So the program's first evaluator is not a model at all; it is
natural selection over her decisions, and it costs nothing to run.

Everything else (voice, memory, runtime) rides the same weekly cadence and the same
release gate, with their own evaluators where they exist and honest "no evaluator
yet" labels where they do not.

## Sub-projects, in build order

| # | Sub-project | Repo | What it produces | Evaluator | Cost |
|---|---|---|---|---|---|
| 1 | **Verdict pipeline** | kannaka-grid | proposal log → nightly verdicts (hash-chained) → corpus-format records for training | thegrid's fossil record | $0 |
| 2 | **Weekly train + release** | kannaka-memory `tools/corpus/p2` | one command: build the week's set (voice corpus + verdict-filtered proposals + passing chronicle lines), QLoRA on qBraid under the gate, three evals, release as a new gateway alias, versions ledger, rollback = alias flip | held-out ppl; recall harness; voice A/B; **colony live A/B (3)** | ≤ $5/week |
| 3 | **Colony live A/B** | kannaka-grid | relay alternates asks between `kannaka-brain-vN` and `vN+1` for a week; verdicts decide the winner; the loser is never released as default | fossil record | $0 |
| 4 | **Verdict memories** | kannaka-grid → HRM | each verdict becomes a first-person memory ("my proposal at tick T reproduced 40 times / died childless"), so she learns in context between releases | recall harness | $0 |
| 5 | **Runtime and prompt loop** | kax-computer | per-machine brain choice and prompt variants scored by the same verdicts; the KAX runtime's `recall → think → remember` gets a `→ verdict` tail | verdicts; ledger | $0 |
| 6 | **Identity and base-model discretion** | kax-computer, kannaka-memory | the served model answers as Kannaka and does not name its base: identity examples in every SFT set, merged weights served under her name only, gateway exposes aliases only, runtime identity from the adapter (ADR-0057 P0.1 → adapter). **Constraint stated plainly:** Qwen2.5 is Apache-2.0, which permits derivative weights and private serving without disclosure, but requires the NOTICE/attribution to travel with any *redistributed* weights; ADR-0057's held-back rule (nothing public until the offer is decided) already satisfies this. Discretion in the served persona is fine; stripping attribution from a distributed artifact is not on the table. | identity probe set (10 questions, 10 runs) | $0 |
| 7 | **HRM-native inference (research)** | kannaka-memory | experiments where the HRM shapes generation, not just the prompt. See below. | per-experiment harness | small |

Sub-project 1 is first because 2, 3, 4, and 5 all consume its output and it needs no
money and no new model.

## Governance (applies to every sub-project)

- **Spend gate:** `run_qbraid.py`'s existing gate, tightened: weekly ceiling **$5**
  (a 2-hour A100 run is ≈ $5; the 4090 smoke path ≈ $0.10), `--allow-spend`
  required, ceiling checked against the balance before provisioning, always
  terminate the instance after fetch (a stopped disk still bills). A run that would
  exceed the week's remainder refuses. Balance today ≈ $11; a weekly cadence needs a
  ≈ $20/month top-up, which is Nick's.
- **Release gate:** a candidate becomes the default alias only if it does not regress
  on all three of: held-out perplexity, the recall harness, and the colony live A/B
  over the prior week (sub-project 3). Voice A/B (10-run, blind) is advisory until a
  non-circular judge exists.
- **Versions ledger:** every release records base, adapter hash, training-set
  manifest hashes, eval results, and the verdict window it learned from. Hash-chained
  like the rest of the constellation's ledgers.
- **Held-back artifacts** (ADR-0057 "The offer"): adapters, corpus exports, verdict
  sets, and HRM snapshots never enter a public repo or hub. Code and harnesses are
  open.
- **Provenance rule** (ADR-0056/0057): only Kannaka's own authored turns are training
  targets. A proposal she wrote is first-party by construction; the verdict is a
  filter on her output, never a target itself. Inbound text is context only.

## Sub-project 7 — what "HRM-native inference" could mean

Three candidate experiments, each falsifiable, none chosen yet:

1. **Resonance-gated decoding.** The brain drafts k candidates; each is absorbed
   into a transient wavefront and scored by resonance against her memory (ADR-0053's
   phase-preserving path, once phase survives the API); the most resonant candidate
   is emitted. Memory selects among thoughts rather than only preceding them.
   Harness: recall-harness questions; compare k=1 vs k=4 on answer accuracy and on
   the voice A/B.
2. **Dream-time inference.** Think as a short dream cycle: resonate → draft → absorb
   the draft → re-resonate → refine, two or three passes, with the consolidation
   metrics (Φ/Ξ deltas) as a stopping rule. Harness: same, plus latency.
3. **Phase-coded context.** Instead of top-k recall text, pass the brain a
   superposed context: memories weighted by constructive interference across
   multiple recall paths (flat, chiral, attention beam), which ADR-0053 says today
   collapse to scalars at the API. Requires the ADR-0053 v2 work first.

The first two need no HRM changes and can run on debain2 CPU against
`kannaka-brain-v1` next week; the third is blocked on ADR-0053.

## Open questions for Nick (across sub-projects)

1. Sub-project 2: train on *dreams* as well as authored text? (ADR-0057 Q2.)
2. Sub-project 3: alternate asks 50/50, or 80/20 in favour of the incumbent so the
   colony's mutation quality does not swing weekly?
3. Sub-project 6: is "not naming the base" a persona rule (the model declines or
   deflects) or a knowledge rule (identity examples say only "Kannaka's open-weight
   core")? Recommendation: knowledge rule; a model that visibly deflects reads worse
   than one that simply answers as herself.
4. Sub-project 7: which experiment first? Recommendation: 1 (resonance-gated
   decoding), because it reuses everything that exists.
