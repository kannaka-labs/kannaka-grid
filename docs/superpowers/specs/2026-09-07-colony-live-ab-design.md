# Colony live A/B (self-improving Kannaka, sub-project 3) — design

**Date:** 2026-09-07. **Status:** approved in conversation (two decisions below), awaiting Nick's review of this text.
**Program:** `2026-09-05-self-improving-kannaka-program.md` (sub-project 3). **Depends on:** sub-project 1
(verdict rows carry `model_alias`), sub-project 2 (the release gate, `kannaka-brain-current`, the
versions ledger), sub-project 6 (candidates must first pass the identity gate).

## 1. What this changes

Today the weekly release gate probes a promoted candidate (identity, validity) and, if both pass,
flips `kannaka-brain-current` to it. The colony's own verdict on the candidate — the fossil record's
reproduced-rate per author — is recorded as **advisory** (`colony_ab` in the ledger row). This
sub-project makes that verdict **binding** and gives it a fair sample: for a week the relay splits its
Odin asks between the incumbent and the candidate, verdicts attribute each child to its author, and
only a candidate whose children reproduce no worse than the incumbent's becomes the release. **The
loser is never released.**

## 2. Decisions (settled with Nick, 2026-09-07)

- **Split 80/20 in the incumbent's favour** (program open question 2). The colony's mutation quality
  does not swing week to week; at the live rate (7.2 proposals/h ≈ 1,200/week) the candidate still
  gets ≈ 240 proposals, ≈ 200 of them settled — enough for a reproduced-rate comparison.
- **Win rule: reproduced-rate no worse than the incumbent's.** Candidate wins iff
  `rate_candidate ≥ rate_incumbent × (1 − TOLERANCE)` with `TOLERANCE = 0.10` (relative), **and** both
  arms have at least `MIN_SAMPLE = 100` settled, attributable verdicts. Below the sample the week is
  `inconclusive` and the A/B continues (up to `MAX_WEEKS = 2`). Births-per-proposal stays advisory
  (heavy-tailed).

Live reproduced-or-better rates so far (265 settled): base `kannaka-brain` 18%, `v3` 23%,
`7b-v1` 2%, overall 14% — the metric already separates the brains.

## 3. The relay: two arms, deterministic assignment

`grid/odin.py` gains a second direct route. Config (`grid.env`, written by the gate, never needed by
hand):

- `MIND_LLM_MODEL` — the incumbent alias the relay asks (`kannaka-brain-current`), as today.
- `MIND_MODEL_ALIAS` — the incumbent's **concrete** version label (`kannaka-brain-vN`), as today.
- `MIND_LLM_CANDIDATE` — the candidate's concrete tag (`kannaka-brain-vN+1`); **empty = no A/B**,
  the relay behaves exactly as today.
- `MIND_AB_SPLIT` — the candidate's share, default `0.2`.

Per request the relay picks the arm **deterministically from the request id**:
`candidate iff int(sha256(request_id)[:8], 16) / 2**32 < split`. No RNG state; a request that is
deferred and retried lands on the same arm; the assignment is reproducible from the log alone. The
proposal-log entry's `model_alias` is the **concrete tag of the arm that answered** (incumbent →
`MIND_MODEL_ALIAS`; candidate → `MIND_LLM_CANDIDATE`), so verdict attribution needs no new field.
A candidate ask that fails is **deferred like any other**, never silently re-routed to the
incumbent (re-routing would move the hard cases off the candidate and bias the comparison). The
candidate route uses the same key file; the gate verifies the candidate tag is on the colony key
before starting an A/B (the existing `key_models` check).

## 4. The release gate: a two-stage state machine

`grid.weekly release` (Sunday 06:00) keeps its probes and adds an A/B stage. State lives in
`grid.env` (`MIND_LLM_CANDIDATE`) and in the versions ledger (the last `ab_started` row).

1. **No A/B running, new candidate promoted, probes pass** → do **not** flip. Start the A/B: set
   `MIND_LLM_CANDIDATE=<tag>`, `MIND_AB_SPLIT=0.2`, restart the relay, append a ledger row
   `decision: ab_started` with `colony_ab: {candidate, incumbent, split, since_ts, min_sample,
   tolerance}`. `since_ts` is the row's own `ts`; the window starts here.
2. **A/B running** → read the verdict ledger for rows settled with `ts ≥ since_ts` (verdict rows
   carry the settle time), keyed by `model_alias` ∈ {incumbent concrete tag, candidate tag}, excluding
   `preexisting`/`unknown`/`rejected` tiers (not attributable outcomes). Compute per arm
   `settled`, `reproduced` (tier ∈ reproduced/growing/enduring/established), `rate`, and
   `births_per_proposal` (advisory).
   - Either arm `< MIN_SAMPLE` → `decision: ab_inconclusive` (row records both arms; A/B continues).
     If the A/B has already been running `≥ MAX_WEEKS` (2 rows of `ab_inconclusive` since
     `ab_started`) → `decision: held`, reason `colony A/B inconclusive after 2 weeks`, **clear the
     candidate**, relay restart.
   - Both arms sampled and `rate_c ≥ rate_i × (1 − TOLERANCE)` → **win**: the existing three-step
     flip (`MIND_MODEL_ALIAS` ← candidate, `ollama cp candidate current`, relay restart) **plus**
     clearing `MIND_LLM_CANDIDATE` in the same env write; row `decision: released` with
     `colony_ab: {…, binding: true, winner: candidate}`.
   - Otherwise → **loss**: `decision: held`, reason `colony A/B lost: <rate_c> vs <rate_i>`, clear the
     candidate, relay restart; `current` untouched. The candidate stays served under its own tag
     (rogue keeps the newest three) but is never the default.
3. **A newer promotion appears while an A/B is running** → the running A/B finishes first; the gate
   starts a new A/B only when none is running (one candidate at a time). The skipped promotion is
   picked up on the next Sunday after the current A/B ends, if it is still the newest.
4. **Probes fail on a new candidate** → `held` as today; no A/B.

Rollback (`grid.weekly rollback`) is unchanged and also clears any running candidate. A new verb
`grid.weekly ab` prints the running A/B's arms and numbers (`status` shows whether one is running);
`grid.weekly ab --abort` clears the candidate and appends `held: A/B aborted by operator`.

## 5. Ledger

`grid/versions.py` `DECISIONS` gains `ab_started` and `ab_inconclusive`. `last_released()` ignores
them (they are not release-state rows). `colony_ab` in every row becomes:

```
{"binding": bool, "candidate": tag|None, "incumbent": tag, "split": 0.2, "since_ts": ms,
 "min_sample": 100, "tolerance": 0.10, "weeks": n,
 "arms": {tag: {"settled": n, "reproduced": n, "rate": f, "births_per_proposal": f}},
 "winner": tag|None}
```

(`binding: false` with `arms` over the trailing week for rows outside an A/B — today's advisory
shape, kept.)

## 6. Timing, cost, safety

- A release now takes two Sundays minimum: probes on the promotion's first Sunday, the colony's
  verdict the next. Settling lags 3,000 ticks (~33 min at 1.5 t/s), negligible against a week.
- Cost: none. The candidate runs on the same gateway/key; ollama keeps one model loaded, so an A/B
  week reloads models between arms — the relay is serialised anyway and the 7.2/h rate absorbs it.
- The colony is protected: at most 20% of a week's proposals ride an unproven model that already
  passed identity and validity; thegrid's own mutator runs regardless; a losing candidate is dropped.
- Any gate failure (SSH, probe, ledger) records `held` and never flips (sub-project 2 §7 rules).
- Dry-run of `release`/`ab` writes nothing and restarts nothing.

## 7. Testing

- Unit: arm assignment is deterministic and reproducible (same id → same arm; split 0 → never
  candidate; split 1 → always); the relay asks the candidate route and labels the entry with the
  candidate tag on the candidate arm, the incumbent's concrete label otherwise; a failed candidate
  ask defers rather than re-routes; with `MIND_LLM_CANDIDATE` empty the relay is byte-for-byte today's
  behaviour. Gate: the state machine's five transitions (start / inconclusive / inconclusive→held at
  MAX_WEEKS / win→flip+clear / loss→held+clear) against a seeded verdict ledger with settle
  timestamps; the window filter (`ts ≥ since_ts`), the tier exclusions, the tolerance boundary
  (exactly `rate_i × 0.9` wins), the min-sample boundary (99 vs 100); `ab --abort`; dry-run writes
  nothing. `DECISIONS` and `last_released()` over the new rows.
- Receipts (README §5): an A/B started by hand on Skywave with the base `kannaka-brain` as the
  candidate at split 0.2 for a few hours (the base already authored 150+ settled verdicts, so the risk
  is nil), the proposal log showing ≈ 20% `kannaka-brain` labels, `grid.weekly ab` printing both arms,
  then `ab --abort`; the first real A/B starts the Sunday a candidate first clears the identity gate.

## 8. Open questions

1. Should `MAX_WEEKS` be 2 or 3? (2 keeps a stuck candidate from occupying the slot; 3 tolerates a
   slow week on debain2.) Default 2.
2. When a candidate loses, should the gate remember the loss so a re-promotion of the same tag is not
   re-tested? (Default: yes — a `held: colony A/B lost` row for that tag blocks a new A/B on it.)
