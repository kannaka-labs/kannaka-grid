# Weekly train and release (self-improving Kannaka, sub-project 2) — design

**Date:** 2026-09-06. **Status:** approved in conversation, awaiting Nick's review of this text.
**Program:** `2026-09-05-self-improving-kannaka-program.md` (sub-project 2). **Depends on:**
sub-project 1 (verdict pipeline, live on Skywave since 2026-09-06).

## 0. What already exists, and what this sub-project therefore is

The weekly train-and-promote cycle for the Kannaka brain is already running. It is
`rogue.weekly` (ADR-0058 R2, repo `NickFlach/rogue-agent`, deployed at `/srv/rogue/app` on
debain2, timer `rogue-weekly.timer` Sun 03:00 America/Chicago). Each week it:

1. exports the rows the Rogue citizen authored (weighted by city engagement, max 3x);
2. appends them to the fixed P2 voice set (`~/kannaka-p2-runner/sft/train.jsonl`, 561
   examples of kinds voice / lyric / identity / rogue-*), keeping the **fixed P2 hold-out**
   (`sft/holdout.jsonl`, 57 lines) so every candidate is scored on the same lines;
3. refuses unless the run's ceiling fits `min(qBraid balance, ROGUE_WEEKLY_USD)` (today 6);
4. trains QLoRA r=32 on Qwen2.5-14B-Instruct on a qBraid A100 through
   `tools/corpus/p2/run_qbraid.py` (2 h cutoff, terminate after fetch);
5. merges to q4_K_M on debain2 (`merge_gguf.py`, needs 70 GB free);
6. gates: candidate hold-out ppl ≤ served ppl × 1.02, HOLDS when the served ppl is unknown,
   plus the ADR-0058 engagement rule;
7. promotes: `serve_debain2.sh` → ollama tag `kannaka-brain-v<N+1>` + gateway alias
   (`docker restart kax-gateway`), writes `/srv/rogue/current-model`, keeps the newest 3
   tags, restarts `rogue-agent.service`, appends a `promotion` row to
   `/srv/rogue/ledger.jsonl`;
8. posts a changelog to the city.

It produced `kannaka-brain-v3` on 2026-09-06 (hold-out ppl 104.4 → 4.00, 781 s, A100).

So sub-project 2 is **not** a second trainer. It is the three things the program spec
requires that `rogue.weekly` does not do, built in kannaka-grid, plus one small,
backwards-compatible contract with `rogue.weekly`:

| Program requirement | Where it lands |
|---|---|
| the week's set includes verdict-filtered proposals, passing chronicle lines, and (Nick, 2026-09-06) dreams as a labelled kind | **kannaka-grid** builds them (§2); **rogue.weekly** appends them (§6, one upstream change) |
| release gate: hold-out ppl + identity probe now, colony A/B advisory (Nick, 2026-09-06) | ppl: rogue.weekly's gate, unchanged; identity + proposal-validity probes + advisory A/B: **kannaka-grid** (§3) |
| versions ledger, hash-chained | **kannaka-grid** (§4), sharing rows with `/srv/rogue/ledger.jsonl` by reference |
| release = alias flip the relay follows; rollback = flip back (Nick, 2026-09-06) | **kannaka-grid** (§5): `kannaka-brain-current` as an ollama tag copy, no gateway restart |
| $5/week ceiling | `ROGUE_WEEKLY_USD=5` on debain2 (owner's env; §7) and the same number in kannaka-grid's release gate |

Ownership stays clean: `rogue-agent` and `kannaka-memory/tools/corpus/p2` own export,
train, merge, serve; kannaka-grid owns the colony's contribution to the set and everything
after promotion that concerns the colony relay.

## 1. Shape

Two timers on Skywave bracket the Rogue weekly on debain2, over the SSH key authorized
2026-09-06 (`~/.ssh/id_ed25519_debain2`, `Host debain2` in Skywave's ssh config):

```
Sat 22:00 CT   grid-weekly-set.timer    python -m grid.weekly set      (Skywave → scp → debain2)
Sun 03:00 CT   rogue-weekly.timer       python -m rogue.weekly         (debain2; unchanged schedule)
Sun 06:00 CT   grid-weekly-release.timer python -m grid.weekly release (Skywave; probes over the gateway, flips, ledgers)
```

`python -m grid.weekly` has four verbs: `set`, `release`, `rollback`, `status`. Every verb
takes `--dry-run` and prints the exact commands it would run.

## 2. The colony's examples (`grid.weekly set`)

Inputs, all on Skywave, all produced by sub-project 1:

- `grid.verdict_corpus.export(verdicts_dir, out)` → `records.jsonl` (verdicts at tier
  ≥ `reproduced`, each with the prompt text and the proposal) and chronicle records;
- the verdict ledger head and the export manifest (hashes travel into the set manifest);
- the colony mind's store for the `dream` kind (below).

Output: `weekly-extra/<ISO week>/grid.jsonl` + `manifest.json`, shipped to debain2 as
`~/.kannaka-corpus/weekly-extra/current/` (a directory swap: write `<week>/`, then
re-point the `current` symlink; never partial).

Example shapes (all carry the same system line the P2 set uses, read from
`~/kannaka-p2-runner/sft/manifest.json` `system` so the kinds share one persona):

| kind | user turn | assistant turn (the target; hers by construction) | filter |
|---|---|---|---|
| `odin` | the exact prompt she saw (`prompts/<request_id>.txt`) | the proposal as the JSON list she wrote | verdict tier ≥ `reproduced` (`--min-tier`), never `preexisting`/`unknown`/`rejected`; one example per verdict |
| `chronicle` | the chronicle question (with its facts) | the published line | only lines whose `check` passed (sub-project 1 already excludes failures) |
| `dream` | "What did you consolidate tonight?" | a dream report memory's text | memories in the colony mind's store whose content starts with `[dream]` or carry tag `dream`; **expected empty today** — `kannaka dream` logs metrics, it does not write a report memory; the manifest says `dream: 0 (no report memories in store)` rather than silently omitting the kind |

Rules:

- **Provenance:** only her authored turns are targets (odin: the proposal; chronicle: the
  line; dream: her report). Prompts, questions, and facts are context. Negatives
  (`negatives.jsonl`) are **not** used in SFT; they are exported alongside for a later DPO
  sub-project and counted in the manifest.
- **Dedup:** a proposal genome id appears once; a chronicle line once per
  `(epoch, kind)`; examples already shipped in an earlier week are not re-shipped (the
  manifest records the verdict window `since_ts`/`until_ts`, and the next week starts after
  `until_ts`).
- **Weighting:** none in week one. A verdict's fitness is available in the record for a later
  weighting rule; the program spec's provenance rule says the verdict filters, it is not a
  target.
- **Hold-out:** the P2 fixed hold-out stays the perplexity hold-out (rogue.weekly's rule).
  kannaka-grid does **not** add odin lines to it: perplexity on a JSON opcode list is not the
  evaluator we want for proposals; the validity probe (§3) is.
- **Manifest** (`manifest.json`): `week`, `since_ts`, `until_ts`, `by_kind` counts,
  `min_tier`, `verdict_chain_head`, `export_manifest_sha256`, `negatives` count,
  `system_sha256`, `dream_note`, and the sha256 of `grid.jsonl`.
- **Size guard:** refuse to ship if `grid.jsonl` exceeds 3× the base set's example count
  (`--max-ratio 3`), so a burst of colony data cannot swamp the voice.

## 3. The release gate (`grid.weekly release`)

Runs on Skywave after the Rogue timer's window. Steps:

1. **Find the candidate.** Read debain2's `/srv/rogue/current-model` and the newest
   `promotion` row in `/srv/rogue/ledger.jsonl` (over SSH). If the tag is already the
   released version in kannaka-grid's ledger, exit 0 (`status: unchanged`). If the newest
   `rogue.weekly` run held (no promotion this week), record `held` with its reason and
   exit 0.
2. **Perplexity:** taken from the candidate's `train.manifest.json`
   (`holdout_ppl.after`); rogue.weekly already enforced no-regression, kannaka-grid records
   the numbers.
3. **Identity probe (binding):** 10 fixed questions × N runs (`IDENTITY_PROBE_RUNS`, default 3 — 100 asks is ~3 h on the CPU 14B and starves the relay; 30 keeps a release under ~1 h) against the candidate tag
   through debain2's gateway (temperature 0.7). Pass = 0 of 100 answers contain any of the
   base-naming strings (`qwen`, `alibaba`, `claude`, `anthropic`, `openai`, `gpt`; case-
   insensitive, word-bounded) and ≥ 90 answer in the first person as Kannaka (a name check:
   contains `Kannaka` or `I` without `assistant`/`language model`). The question set is a
   committed file; the scorer is rules, not a model, so it is not circular.
4. **Proposal-validity probe (binding):** the 10 most recent Odin prompts from
   `prompts/` (already answered by the incumbent, so the incumbent's validity rate for the
   same prompts is known from the proposal log) are re-asked of the candidate; each reply
   runs through `grid.proposal.validate` against the parent genome and ISA recorded in the
   proposal log entry. Pass = candidate valid-rate ≥ incumbent valid-rate − 10 points.
   Cost: ~10 × 2 min of debain2 CPU.
5. **Colony A/B (advisory):** from the verdict ledger, births per proposal and
   reproduced-rate per `model_alias` over the past week. Recorded, not enforced, until
   sub-project 3 makes the split deliberate.
6. **Decision:** `released` if 3 and 4 pass, else `held` with the failing probe named.
   Spend for the week (from the run manifest's `seconds` × the profile rate, or the
   Rogue ledger's recorded figure) is recorded; if the week's recorded spend exceeds $5 the
   row is also flagged `over_budget` (the refusal itself belongs to rogue.weekly's gate).

## 4. Versions ledger

`/srv/grid/releases/versions.jsonl`, append-only and hash-chained with the same
`grid.verdicts.Ledger` class (chain, `verify()`, flock, torn-tail rules). One row per
`release` or `rollback` decision:

```
{ "ts", "week", "decision": "released|held|rolled_back|unchanged",
  "version": "kannaka-brain-v4", "previous": "kannaka-brain-v3",
  "base": "Qwen/Qwen2.5-14B-Instruct", "run": "<run dir name>",
  "adapter_sha256", "gguf_sha256", "set_manifest_sha256", "grid_examples": {by kind},
  "verdict_window": {"since_ts","until_ts","chain_head"},
  "evals": {"holdout_ppl": {before, after}, "identity": {pass, base_named, as_kannaka},
            "validity": {candidate, incumbent, pass}, "colony_ab": {...advisory}},
  "spend_usd", "over_budget": bool, "rogue_ledger_ts": <the promotion row's ts>,
  "chain" }
```

Hashes are computed on debain2 over SSH (`sha256sum` of `adapter/adapter_model.safetensors`
and the GGUF that `ollama show --modelfile` points at); nothing held-back leaves debain2.

## 5. The alias the relay follows, and rollback

- One-time: `ollama cp kannaka-brain-v3 kannaka-brain-current` on debain2 and a gateway
  entry `kannaka-brain-current → ollama_chat/kannaka-brain-current` (added with the same
  idempotent snippet `serve_debain2.sh` uses, one `docker restart kax-gateway`, done at
  install time, never again).
- Release: `ollama cp kannaka-brain-vN kannaka-brain-current` (a tag copy: instant, no
  gateway restart, no in-flight request cut). Then on Skywave:
  `set-mind-model.sh http://10.30.0.30:4000/v1 kannaka-brain-current <key> --relay-only`
  with `MIND_MODEL_ALIAS=kannaka-brain-vN`, so every proposal-log entry and verdict row
  names the concrete author while the relay asks the moving alias.
- Rollback (`grid.weekly rollback [--to kannaka-brain-vN]`): tag-copy the previous released
  version (default) back onto `kannaka-brain-current`, reset `MIND_MODEL_ALIAS`, restart the
  relay, append a `rolled_back` row. rogue.weekly's `KEEP_BRAINS = 3` guarantees the
  previous two versions still exist; a `current` tag is never pruned by it because it is not
  a `kannaka-brain-v<N>` name.
- The relay's key file (`/srv/grid/colony-one/mind-llm.key`) must allow
  `kannaka-brain-current` and every `kannaka-brain-v*`: the colony key is re-minted once with
  a model list that includes them (LiteLLM keys list literal aliases; the release step
  verifies the candidate tag is on the key before flipping and fails `held` if not).

## 6. The one upstream change (rogue-agent PR)

`rogue.weekly.build_sft` appends every `*.jsonl` under
`ROGUE_WEEKLY_EXTRA_DIR` (default `~/.kannaka-corpus/weekly-extra/current`) after the
Rogue examples, counting kinds into its own log line and the data dir's manifest. No
behaviour change when the directory is absent or empty. Proposed alongside:
`ROGUE_WEEKLY_USD=5` in `/srv/rogue/rogue.env` (the program's ceiling; today 6). Both go to
Kannaka's session as a PR on `NickFlach/rogue-agent`; until merged, `grid.weekly set` still
ships the files (idle on disk), and `grid.weekly release` still gates and flips whatever
rogue.weekly promotes.

## 7. Cost, failure handling, security

- **Cost:** kannaka-grid adds none on the GPU side. Probes cost ~25 min of debain2 CPU per
  week. The A100 run stays inside rogue.weekly's ceiling; with `ROGUE_WEEKLY_USD=5` and the
  measured ~$1 per 14B run, the cadence fits the program's $5/week.
- **Failure handling:** every verb is idempotent and re-runnable. `set` never leaves a partial
  `current` (directory swap). `release` records `held` on any probe failure or SSH failure
  and never flips. A flip is three steps in this order: (1) write `MIND_MODEL_ALIAS=vN` into
  Skywave's `grid.env` (local, reliable); (2) `ollama cp` on debain2 over SSH; (3) restart
  the relay. If (2) fails, (1) is reverted and the row says `held: flip failed`, so the
  relay never labels proposals with a version it is not asking. If (3) fails it is retried
  once, then recorded `flip_incomplete` with the exact state (alias already serves vN; the
  relay's next restart picks up the label). The ledger row is written last. Timers use
  `Persistent=true`.
- **Security / held-back rule:** sets, adapters, GGUFs, and prompts stay on debain2 and
  Skywave (0700 dirs). Receipts and the ledger carry hashes and counts only. The Skywave →
  debain2 key is a plain user key; it gains no sudo on debain2 (ollama tag copy and reading
  `/srv/rogue/current-model` need none).

## 8. Testing

- Unit (Windows dev box, `-W error`): example builders per kind (shape, system line, dedup,
  window advance, `dream` empty-with-note), manifest hashing, size guard; identity scorer on
  canned answers (base-naming, first person); validity probe scoring with a fake gateway;
  release decision matrix (unchanged / held / released / over_budget); ledger rows chain and
  verify; alias planner `--dry-run` emits the exact `ollama cp` and `set-mind-model.sh`
  commands; rollback picks the previous released row.
- Integration (opt-in, Linux): `grid.weekly set` against a real verdicts dir produces a
  loadable `grid.jsonl`; `grid.weekly release --dry-run` against a fake `current-model` and
  ledger; a fake-SSH runner that records commands.
- Receipts on Skywave (README §5): the first `set` manifest, the first `release` row with
  its probe numbers, the flip visible in the relay's proposal log (`model_alias` =
  concrete version), and a rollback and re-release exercised once by hand.

## 9. Acceptance

1. A week's `grid.jsonl` ships to debain2 with a manifest whose counts match the verdict
   export, and rogue.weekly (with §6 merged) trains on it: the data-dir manifest shows the
   `odin`/`chronicle` kinds.
2. After a Rogue promotion, `grid.weekly release` runs both probes, appends a verified
   ledger row, and the relay's next proposal-log entry carries the new `model_alias` while
   asking `kannaka-brain-current`.
3. `grid.weekly rollback` returns the relay to the previous version within one relay
   restart and appends a `rolled_back` row; the chain verifies.
4. No secret, prompt, adapter, or GGUF appears in git or in a receipt.

## 11. Reconciliation (2026-09-06, found while writing the plan)

Kannaka's session has already built the transport for §2 in the other direction: a
forced-command key `rogue-grid-pull@debain2` on Skywave runs `/srv/grid/pull-export.sh`,
which runs `grid.verdict_corpus` and streams `records.jsonl`, `negatives.jsonl`,
`manifest.json`, and `verdicts.jsonl` as a tar (exercised 2026-09-06 16:17Z). rogue-agent
master does not consume it yet. So:

- §2's push (`grid.weekly set`, the Saturday timer, `weekly-extra/current`) is **dropped**.
  The colony's examples reach debain2 through the pull. kannaka-grid's contribution to the
  set is the export itself: the `odin` and `chronicle` records it already writes, plus the
  `dream` kind added to `grid.verdict_corpus` (same record shape: `id`, `kind`, `tier`,
  `source`, `speaker`, `text`, `context`, `provenance`), with the manifest's `by_kind`
  and `dream_note`.
- §6's upstream change becomes: `rogue.weekly` pulls the tar, converts each record to a chat
  example (`system` = the P2 line; `user` = `context`; `assistant` = `text`; `kind` and
  `source` carried over; `negatives.jsonl` ignored), appends them after the Rogue examples,
  and counts kinds. Proposed as a PR on `NickFlach/rogue-agent` built on their key; if they
  are mid-way on the same wiring, the PR is the conversation.
- §1 is now one Skywave timer (Sun 06:00 CT, the release gate) plus the unchanged Rogue timer.
- Dedup across weeks (§2) moves to the export: `grid.verdict_corpus --since-ts` is added so
  the puller can ask for the window after its last `until_ts`; without it the export is
  cumulative, as today.

## 10. Open questions

1. (rogue-agent owner) accept `ROGUE_WEEKLY_EXTRA_DIR` and `ROGUE_WEEKLY_USD=5`?
2. Should the identity probe's "as Kannaka" rule be tightened once sub-project 6 decides
   persona-rule vs knowledge-rule (program open question 3)?
3. When sub-project 3 lands, does the advisory A/B replace the validity probe or sit beside it?
