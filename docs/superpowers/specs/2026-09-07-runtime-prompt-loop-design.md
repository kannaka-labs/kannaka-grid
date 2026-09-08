# Runtime and prompt loop — design (sub-project 5)

Program spec: `docs/superpowers/specs/2026-09-05-self-improving-kannaka-program.md`, row 5.
Builds on: verdict pipeline (1), colony live A/B (3, `docs/superpowers/specs/2026-09-07-colony-live-ab-design.md`).
Cost: $0. No training run, no new model, no public artifact.

## 1. Why

Sub-project 3 gave the colony a vote on which *model* to serve. It cannot vote on
anything else, and the model is not the only thing we guess at. The prompt the relay
sends is a single hardcoded template with pinned sampling and an always-on memory
block, and no one has ever measured whether any of that helps. Prompt changes cost
nothing and land in a week; a training run costs money and lands in a fortnight. The
cheaper experiment should be the one we can actually run.

So: generalise the machinery that already works. The evaluator does not change. It is
still thegrid's fossil record, still free, still not a model judging itself.

## 2. Scope, in build order

| # | Deliverable | Repo | Evaluator |
|---|---|---|---|
| 5a | Prompt variants competing on live colony traffic | kannaka-grid | fossil record |
| 5b | A turn record and explicit per-machine brain choice in the KAX runtime | kax-computer | **none yet, and labelled so** |
| 5c | Factorial mode: model and prompt tested at once | kannaka-grid | fossil record |

5c is last on purpose. It is the piece most likely to change shape once a real prompt
A/B has run once.

5a and 5c share one implementation plan in kannaka-grid, because 5c is a widening of
the same state machine 5a generalises. 5b gets its own plan in kax-computer: different
repo, different worktree, and another agent commits there regularly, so it should be a
small self-contained change that does not sit on a long-lived branch.

## 3. What a variant is

A variant is a named bundle of everything about the ask that is not the model:

- the **template**, in `grid/data/prompts/<id>.md`, using named placeholders for the
  fields `render_prompt` builds today (sandbox context, directive, instruction set,
  ecology, parent telemetry, parent genome, memory, genome cap);
- the **sampling parameters**, today temperature 0.7 and a 400-token cap, which the
  direct route currently hardcodes;
- whether the **recall block** is included at all.

`grid/data/prompt_variants.json` holds one entry per variant: the template filename,
the parameters, a one-line note, and the sha256 of the template file. A `default` key
names the incumbent. The loader fails closed: an unknown placeholder, a missing
template, or a hash that does not match the file is a startup error, not a warning. A
variant edited in place without a new id would silently invalidate a running A/B, so
the hash is the guard against exactly that.

The relay logs `prompt_variant` and `prompt_variant_sha256` on every proposal entry,
beside the `model_alias` it already records. The log already carries the prompt text,
so the join costs nothing.

**Do not confuse the two hashes.** `prompt_sha256` already exists and hashes the
*rendered* prompt of one request, pairing with `prompt_path`. `prompt_variant_sha256`
hashes the *template* the variant was rendered from. Reusing the older name for the
template hash would have silently changed what an existing field means, and an existing
test asserts its length rather than its meaning, so nothing would have caught it.

**Hazard to write into the registry note.** The shipped template opens with a sandbox
context paragraph because without it the model's safety layer reads thegrid's own
directive plus opcodes named steal and corrupt as a request for self-replicating
malware, and returns an empty completion. A variant that drops it will fail fast. That
is acceptable, and section 5 is what makes it fail in days rather than weeks.

## 4. Arms, generalised

Today the split is two arms hardcoded as incumbent and candidate model aliases. It
becomes a set of named arms over a shared traffic budget, in a named **dimension**.
`model` is one dimension; `prompt` is the second. The existing two-arm model A/B is
the special case, and must stay behaviourally identical:

- **assignment**: a request lands on an arm by bucketing a hash of its id against the
  arms' cumulative weights, so the two-arm case with split *s* reproduces the current
  rule exactly, and a retry still lands where it landed before;
- **tally**: per arm, keyed by whatever label that dimension logs;
- **decision**: the same tolerance rule, per dimension.

A test must show that for the `model` dimension every request id maps to the arm it
maps to today. This is a refactor of code that took six review rounds and five
Criticals to settle, and the reason to do it anyway is that the alternative is a
second copy of that state machine.

## 5. The validity gap (a correction to sub-project 3)

The arm tally walks verdict rows and joins back to proposals. A rejected reply never
becomes a genome, so it produces **no verdict row at all**. An arm that answers badly
therefore does not lose. It accumulates settled verdicts more slowly, reads as
`inconclusive` on sample size, and is only rejected after the two-week cap, with a
reason that says "short sample" when the truth was "this arm could not answer".

That is a real weakness in what shipped, and prompt variants make it acute: wording
moves parse success far more than model choice does.

Each arm therefore gains three counts taken straight from the proposal log, which
already records an outcome per attempt:

- **answered**: asks the model actually returned a reply for;
- **valid**: those replies that survived validation and became genomes;
- **unanswered**: asks binned after repeated defers, where the model never replied at
  all.

`validity = valid / answered`. Unanswered asks are excluded from both sides and
reported on their own, because a gateway outage is an availability failure and must
not read as a wording failure. They are the signal that the *relay* is unwell, and the
stuck rule already owns that case.

An arm whose validity falls below a floor (**VALIDITY_MIN = 0.5**, against an
incumbent that runs near 1.0) **loses outright**, without waiting for the settled
sample floor, with a reason that says so. A candidate that cannot answer is a
rejection, not a mystery, and it should cost one Sunday rather than two.

The floor cannot fire on noise: it applies only once the arm has at least
**VALIDITY_MIN_ANSWERED = 20** answered asks. Below that it is silent. Twenty asks is
roughly three hours of colony traffic on one arm of a two-way split, so a genuinely
broken variant is still caught in hours rather than weeks, while a good variant that
happens to open with two bad replies is not executed for it.

The floor applies to a candidate arm only. An incumbent below it is a colony-wide
fault, not a verdict: that case ends the experiment as stuck, the path section 8 of
the A/B spec already defines.

## 6. Independent assignment

Model and prompt assignments must not come from the same hash. If they do they are
perfectly correlated and a four-arm run cannot say which dimension caused a win.
Each dimension therefore salts the hash with its own name.

The `model` dimension keeps the existing unsalted hash, so deploying this never
reshuffles an experiment that is already running. Every dimension after it is salted.
The inelegance is deliberate and is worth one comment in the code.

## 7. Starting, deciding, and flipping a prompt A/B

The gate treats a prompt A/B exactly like a model A/B, with one simplification: the
flip is local. There is no remote tag copy and therefore none of its failure modes.
A won prompt A/B writes the new default variant id into `grid.env` and restarts the
relay; a rollback writes the previous id. The ledger row gains a `dimension` field so
that state, memory of rejected candidates, and the nightly reconciler all scope per
dimension rather than assuming one experiment exists.

Probes do not gate a prompt candidate. The identity and perplexity probes measure a
model, and the model is not what changed.

## 8. One experiment at a time, unless asked

By default the gate refuses to start an experiment in one dimension while another is
open, and names the one in the way. A deliberate factorial flag crosses the dimensions
instead, giving four arms at equal weight.

Four arms are affordable. The colony produces about 7.75 proposals an hour, so a week
yields roughly 1300 attempts, comfortably over a hundred settled per arm even split
four ways. Settlement lag, not throughput, is the binding constraint, and it is the
same constraint the two-arm design already lives with.

A factorial run decides **each dimension on its marginal**, pooling over the other.
The interaction is reported and is advisory only: this design does not have the
samples to call it, and saying so is cheaper than pretending otherwise.

## 9. The KAX turn record

`machine/runtime.py` runs recall, think, remember. It gains a fourth step that writes
one line per turn to a local `turns.jsonl`: timestamp, agent and machine id, sandbox
kind, model, prompt variant, job id, prompt and reply hashes, reply length, latency,
outcome, and an explicit `evaluator: "none"`.

Per-machine brain choice is recorded rather than merely configured. Today the model is
an environment variable that nothing tracks; the machine record and every turn record
name it.

**The record is honest about having no evaluator, and it is shaped so that it will not
need reshaping when one exists.** A KAX machine wired up as the colony's mind is scored
by the pipeline above with no new machinery, because the fields it writes are the
fields the arm tally already joins on. That is the unification this sub-project is
reaching for: per-machine brain and prompt choice become the things selection tests.

Collection is out of scope here. The records sit on the machine, like the grid export
did before it had a pull.

## 10. Non-goals

- No evaluator for KAX machine jobs. There is not an honest one yet.
- No training, no adapter, no HRM work, no spend.
- No changes to the verdict pipeline, the ledger format, or the release gate's
  decision set beyond the `dimension` field and the validity counts.
- No public artifacts. Adapters, corpora, verdict sets and prompts stay held back.

## 11. Open questions for Nick

1. Where should KAX turn records end up: pulled per machine the way the grid export is
   pulled, or published to NATS as they happen?
2. Should the prompt registry be shared with kax-computer eventually, so a machine and
   the colony relay can run the same named variant, or stay grid-local?
3. VALIDITY_MIN is set at 0.5 by judgement, not evidence. The first prompt A/B will
   produce the evidence to set it properly.
