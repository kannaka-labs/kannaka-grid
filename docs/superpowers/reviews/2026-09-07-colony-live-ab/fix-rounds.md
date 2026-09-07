# Final-review fix wave — colony live A/B: report

Branch `feat/ab`, one commit on top of `2e4eada`. Files touched: `grid/odin.py`, `grid/weekly.py`,
`tests/test_odin.py`, `tests/test_weekly.py`, `deploy/README.md`. Nothing under `thegrid/`; nothing
pushed; `main` untouched.

Every mutation below was applied for real (source patched, the named test run under `-W error`,
source restored) — all 18 went RED. Full suite green before and after.

---

## C1 — an empty `MIND_AB_SPLIT=` line kills the relay

**`grid/odin.py` `RelayConfig.from_env`.** Every numeric env read that the gate can blank now uses
`env(X) or default` before the cast, never `env(X, default)`: `MIND_AB_SPLIT`, `MIND_ASK_TIMEOUT_S`,
`ODIN_RETRY_S`, `ODIN_POLL_S`. `os.environ.get` returns `""` for a present-but-empty line, so the old
form fed `float("")` and the relay crash-looped into its StartLimit.

**`grid/weekly.py` `start_ab` revert.** Added `_env_unset(path, key)` (drops the line entirely). On a
failed restart the candidate line still reverts to the empty idiom (the relay reads empty as "no
A/B"), but an absent-before `MIND_AB_SPLIT` is now **removed**, not blanked — the split has no empty
idiom, an empty value is fatal.

- Test (new): `tests/test_odin.py::test_config_from_env_tolerates_blank_numeric_lines` — all four
  keys set to `""`; asserts the four defaults.
  Mutation: `ab_split=float(env("MIND_AB_SPLIT", str(AB_SPLIT)))` → RED (`ValueError`).
- Test (existing, shape changed):
  `tests/test_weekly.py::test_release_start_ab_records_flip_incomplete_when_relay_restart_fails`.
  **Why it changed:** it asserted `env == before + "MIND_LLM_CANDIDATE=\nMIND_AB_SPLIT=\n"`, i.e. it
  pinned the exact bug. Now asserts `env == before + "MIND_LLM_CANDIDATE=\n"` plus
  `"MIND_AB_SPLIT=" not in env`.
  Mutation: restore `_env_set(..., before_split if before_split is not None else "")` → RED.

## C2 — a WON A/B whose tag copy fails left env + relay splitting

**`grid/weekly.py` `release()` win branch.** On `outcome == "flip_failed"` the gate now calls
`end_ab(...)` (clears the candidate, restarts the relay) and records
`held: colony A/B won but the tag copy failed: <candidate>; A/B ended, current unchanged`.
That reason deliberately starts with none of the lost/stuck/inconclusive prefixes, so `lost_tags`
does not remember it — a win that could not be applied is not a rejection and is retried next Sunday.
If `end_ab`'s own restart fails, the row is `flip_incomplete` with the standard
`; relay restart failed clearing the A/B` suffix (the same idiom as every other end_ab call site) —
a small addition beyond the brief's literal wording, so that a failed restart is never swallowed.

- Test (new): `tests/test_weekly.py::test_ab_win_whose_tag_copy_fails_ends_the_ab_and_is_not_remembered`
  — win verdict + `FakeRemote(fail={"ollama cp"})`; asserts `held`, the exact reason, no candidate in
  `grid.env`, `MIND_MODEL_ALIAS` still v3, **exactly one** relay restart (end_ab's), `ab_state` None,
  and `"kannaka-brain-v4" not in lost_tags(...)`.
  Mutation: replace `end = end_ab(...)` with `end = "ok"` → RED (candidate line survives, no restart).
- Test (new): `..._and_restart_both_fail_is_flip_incomplete` covers the added branch.
  Mutation: collapse to the unconditional `decision, why = "held", base_reason` → RED.

## C3 — §8 recovery could not fire when a candidate ask wedged the relay

**`grid/odin.py`.** New module constant `DEFER_LIMIT = 4` and a per-request `_defers` counter beside
`_retry_not_before`. On the 4th consecutive `deferred` for one request id the relay deletes
`request.json` (the same way the unparseable path does), logs a proposal-log entry with
`outcome="rejected"`, `reason="deferred 4 times"` and `model_alias` = **the arm's alias**, then
publishes `odin.rejected`. `_log` runs before `_publish`, so a dead bus cannot cost the record. A
successful ask pops the counter. thegrid then files a fresh request that may hash to the other arm;
the row settles as tier `rejected`, an `EXCLUDED_TIERS` row, so it is neither a win nor a loss.

**`grid/weekly.py` `is_stuck`.** Also true when 48 h have passed and **both** arms have 0 settled —
a wedged relay starves the incumbent too, so it can never reach `STUCK_INCUMBENT_MIN`. `ab_check`
records the distinct reason `colony A/B stuck: no proposals from either arm in 48h` for that case.

- Test (new): `tests/test_odin.py::test_a_wedged_candidate_ask_is_binned_after_the_defer_limit` — a
  candidate-arm `ask` raising `MindError` every time, clock advanced past retry each poll; asserts 3
  defers keep the file, the 4th returns `rejected`, `request.json` gone, the log entry's reason and
  `model_alias == "v4"`, the published reason, and `incumbent.asked == []`.
  Mutation: `if seen < DEFER_LIMIT:` → `if True:` → RED.
- Test (new): `tests/test_odin.py::test_a_successful_ask_resets_the_defer_count`.
  Mutation: drop the `self._defers.pop(...)` reset → RED.
- Test (extended): `tests/test_weekly.py::test_is_stuck_rule` — `arms(0, 0)` at 49 h is True, at 40 h
  False; the three original cases unchanged.
  Mutation: drop `or arms[incumbent]["settled"] == 0` → RED (both tests).
- Test (new): `tests/test_weekly.py::test_ab_check_aborts_a_wedged_relay_with_both_arms_at_zero` —
  end to end through `ab_check`, asserting the both-arms reason, the restart, the cleared env.
  Mutation: revert `base_reason` to the incumbent-only string → RED.

## I1 — `ab --check` is now loud on `flip_incomplete`

`main()`'s `--check` branch returns **3** when the recorded row's decision is `flip_incomplete`, 0
otherwise. Under `grid-verdicts.service`'s `ExecStartPost` that marks the nightly unit failed, which
is right: `grid.env` says "no A/B" while the live relay is still splitting, and only an operator
restarting grid-odin closes the gap. README's exit-code line was wrong ("exit 1 and log if stuck")
and is replaced with the three documented outcomes.

- Test (new): `tests/test_weekly.py::test_ab_check_cli_exits_three_when_the_relay_restart_failed` —
  seeded stuck A/B, `_local_run` monkeypatched to return 1, `time.time` frozen 49 h in.
  Mutation: `return 0` → RED.

## I2 — guard against a relabelled incumbent mid-A/B

`set-mind-model.sh --relay-only` rewrites `MIND_MODEL_ALIAS`; mid-A/B the incumbent's proposals then
log under a label `ab_arms` does not count, its rate reads 0, and **a losing candidate clears the
tolerance floor against a phantom zero**. Both consumers now check first:

- `release()`'s A/B branch: on mismatch, record `ab_inconclusive` with reason
  `incumbent label changed mid-A/B (<env> != <state>); evaluation skipped` and return. It is an
  `ab_inconclusive` row, so it spends a week of the MAX_WEEKS budget like any other.
- `ab_check`: log a warning and return None (otherwise every A/B looks stuck the moment 48 h pass).
- README §Colony A/B: a bold "do not run `set-mind-model.sh` while an A/B is running" paragraph.

- Test (new): `test_ab_week_refuses_to_judge_a_relabelled_incumbent` — reuses `_run_ab_week` with a
  new `alias=` argument; the seeded week is a **loss** (cand_rep=10), so the mutation is visible.
  Mutation: `if env_incumbent != state["incumbent"]:` → `if False:` → RED (`released`, i.e. a losing
  model shipped).
- Test (new): `test_ab_check_skips_a_relabelled_incumbent` — asserts None, the warning in `caplog`,
  no restart, the A/B still open. Mutation: same guard → `if False:` → RED.

## I3 — only a candidate-arm shortfall is remembered

New `short_arms(arms, *, incumbent, candidate, min_sample)` helper; the A/B verdict row's
`evals.colony_ab` now carries `short_arms: [...]`. `lost_tags` splits the old three-way prefix match:
`colony A/B lost` / `colony A/B stuck` block unconditionally, while a `colony A/B inconclusive` row
blocks only when `short_arms == [that tag]`. A row with no `short_arms` (written before the field
existed, or hand-edited) reads as candidate-short — the conservative default that keeps the livelock
guard in `test_ab_state_and_lost_tags` intact without touching that test.

- Test (new): `test_lost_tags_remembers_only_a_candidate_alone_shortfall` — candidate-short blocked,
  both-short not blocked, missing-field blocked.
  Mutation: restore the bare `"colony A/B inconclusive"` prefix → RED (both-short blocked too).
- Test (existing, one line added): `test_ab_inconclusive_continues_then_holds_at_max_weeks` now also
  asserts `row2["evals"]["colony_ab"]["short_arms"] == ["kannaka-brain-v4"]`.
  **Why it changed:** the brief asks the Task 2 row to carry the new shape; its case is exactly the
  candidate-short one. Mutation: drop `short_arms` from the block → RED.

## I4 — `grid.weekly ab` shows an env-only A/B

New `_env_ab_view(cfg, candidate)`: when `ab_state` is None but `MIND_LLM_CANDIDATE` is set, `ab`
prints `{"source": "env", "candidate", "incumbent": MIND_MODEL_ALIAS, "split": MIND_AB_SPLIT,
"arms": ab_arms(..., since_ts=0), "note": ...}` — arms over all history, because there is no start
time to window on, and the `note` says so. With no candidate at all it still prints `{"ab": null}`.

- Test (new): `test_ab_cli_shows_an_env_only_ab`, captured stdout, both cases.
  Mutation: restore the bare `{"ab": None}` → RED.

## Minors

| Change | Test | Mutation → RED |
| --- | --- | --- |
| `ab_arms` builds `entries` with `e.get("request_id")`, skipping rows without one | extra lines in `test_ab_arms_windows_on_proposal_ts_and_excludes_unattributable_tiers` (a log line with no `request_id` appended) | `e["request_id"]` → `KeyError` |
| `--abort` records an `ab_arms` snapshot in `evals.colony_ab` (`_ab_snapshot`); env-only stray gets the since-0 view with the `note` | `test_ab_abort_records_the_arms_it_threw_away`, `test_ab_abort_of_a_stray_env_candidate_records_the_env_view` | `evals={}` → `KeyError` (both) |
| `_env_get` strips an inline ` # comment` and surrounding blanks | `test_env_get_strips_an_inline_comment` | drop the strip → RED |
| `main()` `--check` path computes `ab_state` once (hoisted above `--abort`/`--check`/display; `--forget` returns before it) | covered by the existing `ab` CLI tests | n/a (refactor) |
| `ab_state` docstring names `flip_incomplete` in the closing set | — | n/a (docstring) |
| Tolerance-boundary test now exercises the epsilon | `test_ab_decide_boundaries` | drop `- 1e-12` in `ab_decide` → RED |
| README: `flip_incomplete` added to the outcomes list; `--forget` reworded to "append a row lifting the block"; the `grid-odin` sudoers line added next to grid-mind (§1); `ab --check` exit codes; the `set-mind-model.sh` warning; the env-only `ab` view | — | n/a (docs) |

On the tolerance test: `0.25 * (1 - 0.10)` and `0.225` are **bit-identical**, so the original
boundary line never touched the epsilon (the test now asserts that with `.hex()` and keeps the line
as an exact-equality case). A pair that does exercise it, from real counts:
`42/108 * 0.9 == 0x1.6666666666667p-2` sits one ulp above `35/100 == 0x1.6666666666666p-2`, so a
candidate exactly on the tolerance line loses without the epsilon. Both facts are asserted.

## Deferred (not in this wave, per the brief)

Advisory Wilson interval in `colony_ab`; the `arm` string style.

## Verification

```
.venv/Scripts/python.exe -m pytest tests -q -W error
258 passed, 4 skipped in 19.55s
```

---

# Round 2 — the re-review of `b99bdcc`

One commit on top of `b99bdcc`. Files touched: `grid/odin.py`, `grid/weekly.py`,
`grid/verdict_corpus.py`, `deploy/grid.env.example`, `deploy/README.md`, `tests/test_odin.py`,
`tests/test_weekly.py`, `tests/test_deploy_units.py`, `tests/test_verdict_corpus.py`.

15 new mutations applied for real (patch source, run the named test, restore) — all RED. The two
round-1 mutations whose anchors this round rewrote (`C2`, `I1`) were re-run against the new text and
are still RED; `I3-lost_tags` and `minor-env_get` are superseded by N2 and N3 below.

## N1 (Critical, my own R1) — a relabelled A/B never ended

`ab_inconclusive` is not a closing decision, so I2's guard on its own wedged the gate permanently:
another `ab_inconclusive` row every Sunday, no candidate discovery, no probes, no releases, the relay
still splitting 20% to a candidate nobody could judge, and `ab --check` exiting 0 every night.

`release()`'s relabelled branch now honours `MAX_WEEKS` exactly as a real inconclusive week does:
below the cap it records `ab_inconclusive` with the same reason as before; at the cap it calls
`end_ab` and closes — `held: <reason>; A/B ended after 2 weeks` when the restart succeeds,
`flip_incomplete` plus `; relay restart failed clearing the A/B` when it does not. The row carries
`short_arms: []` and a reason matching none of `lost_tags`' prefixes, so the candidate is not
remembered as rejected: an operator's mislabel is not the colony's verdict, and the next Sunday
starts a fresh A/B against the new label (self-healing, because the new `incumbent` is read from the
env at start time). `ab_check`'s own relabelled guard keeps its nightly `WARNING`, now naming
`MAX_WEEKS` so the line says when the wedge ends.

- `tests/test_weekly.py::test_a_relabelled_ab_still_ends_at_max_weeks` — two Sundays through
  `_run_ab_week` + `release`; asserts week 1 stays open, week 2 is `held`, `ab_state` is None (the
  gate is unwedged), `lost_tags` empty, `short_arms == []`, the candidate cleared, the relay
  restarted. Mutation: `if state["weeks"] + 1 >= MAX_WEEKS:` to `if False:` — RED.
- `..._that_cannot_restart_the_relay_is_flip_incomplete`. Mutation: collapse to the unconditional
  `held` — RED.

## N2 (Important) — one memory rule, keyed on `short_arms`, not on wording

C3 created a `colony A/B stuck: no proposals from either arm in 48h` row, and `lost_tags` matched the
`colony A/B stuck` prefix unconditionally — so a 48 h colony-wide outage blacklisted the candidate,
the exact fault I3 exists to prevent. Now:

- `ab_check`'s stuck block carries `short_arms`: `[candidate]` for the classic case (incumbent at or
  above `STUCK_INCUMBENT_MIN`, candidate zero), `[candidate, incumbent]` for C3's both-arms-zero
  case. `is_stuck` guarantees the incumbent is either `>= STUCK_INCUMBENT_MIN` or zero, so the
  branch is exact.
- `lost_tags` uses one rule for `lost`, `stuck` and `inconclusive` alike: remember the tag unless
  some arm **other than that tag** was short. A missing field still reads as candidate-short and
  blocks.

**Deviation from the ruling, deliberate and flagged.** The ruling's literal wording is "blocks … only
when `short_arms == [that tag]`". A genuine loss carries `short_arms: []` — both arms were sampled
and the colony decided on the merits — so the literal rule would silently stop remembering losses,
the single most important thing `lost_tags` does (spec §8), and would fail the existing
`test_lost_tags_remembers_a_loss_recorded_as_flip_incomplete`. I implemented
`short is None or not (set(short) - {tag})`, i.e. "no arm other than the candidate was short", which
gives the ruled answer for all three shortfall cases and keeps losses remembered. Both readings are
pinned by tests, including a mutation that applies the literal rule and goes RED.

- `test_lost_tags_remembers_only_a_candidate_alone_shortfall`, extended to six rows: inconclusive
  candidate-short (blocked), inconclusive both-short (not blocked), missing field (blocked), loss
  with `[]` (blocked), stuck both-short (not blocked), stuck candidate-short (blocked).
  Mutations: the unconditional three-prefix match — RED; `list(short) == [tag]` — RED (it stops
  remembering the loss).
- `test_ab_check_aborts_a_wedged_relay_with_both_arms_at_zero` and `test_ab_check_aborts_a_stuck_ab`
  now assert the recorded `short_arms` and the resulting `lost_tags` end to end.
  Mutation: drop `short_arms` from `ab_check`'s block — RED (both).

## N3 (Important) — the relay is the authority; nothing else strips comments

Round 1's `_env_get` comment strip made the gate and the relay disagree about a value's text by
construction, in the one place I2's guard (gate-read vs gate-read) cannot see. Reverted to
`line.split("=", 1)[1]` — verbatim, exactly what `RelayConfig.from_env` gets from `os.environ`. The
premise that justified the strip is also the reason the strip was wrong: systemd keeps the comment in
the value, which is why `deploy/grid.env.example`'s `ODIN_RETRY_S=60          # 900 with a CPU
model …` was a shipped `float("")`-class landmine. That comment (and the one on the commented-out
`FLUX_URL` template) now sits on its own line above the key.

- `tests/test_deploy_units.py::test_grid_env_example_has_no_inline_comments` is the durable guard: it
  parses every `KEY=value` line in the shipped example — commented-out template lines included, since
  they become real lines — and fails on any ` #` in a value. Mutation: put the comment back on
  `ODIN_RETRY_S` — RED.
- `tests/test_weekly.py::test_env_get_reads_a_value_exactly_as_the_relay_would` replaces the deleted
  strip test. Mutation: restore the strip — RED.

## N4 (Important) — a resting-state reconciler

Root-cause fix for the class that has now appeared four times: every guard so far checks a
*transition*, so a divergence is announced once by the run that created it and is then invisible
forever (a `flip_incomplete` row closes `ab_state`, and the next nightly says "no A/B running", exit
0). New `ab_reconcile(cfg, *, local_run, dry_run)` runs at the very top of `ab_check`, before any
stuck logic, and asks only whether the world is currently consistent. It keys on the **ledger**,
never on absence:

| State | Action |
| --- | --- |
| env candidate the ledger has **never** seen | `log.warning`, touch nothing — spec §7's smoke receipt hand-starts an A/B exactly this way |
| ledger open **and** env splitting | consistent, `None` |
| the last A/B decision was `flip_incomplete`, or env splits to a tag whose A/B the ledger closed | `end_ab` + record `colony A/B reconciled: …` (`held`, or `flip_incomplete` which exits 3 and is retried nightly) |
| ledger open, env carries no candidate | `end_ab` + record; the A/B closes so the Sunday gate is not stuck on a verdict it can never reach |
| nothing anywhere | consistent, `None` |

`main()`'s `--check` branch no longer short-circuits on `state is None` — that state *is* the
standing `flip_incomplete` — and still prints `no A/B running` / `not stuck` for the quiet cases, so
the existing CLI test is unchanged. Because a successful reconcile records a `held` row, the
condition stops being true and the reconciler does not re-fire; a failed one records
`flip_incomplete` and so retries, loudly, every night.

Tests (one per branch, plus the CLI): `test_reconciler_repairs_a_standing_flip_incomplete` (and
asserts the second pass is a no-op), `..._reports_a_failed_repair_as_flip_incomplete`,
`..._clears_a_candidate_whose_ab_the_ledger_closed`, `..._closes_an_ab_the_env_is_no_longer_running`,
`test_reconciler_leaves_a_hand_started_ab_completely_alone` (env unchanged, no restart, no row, the
warning logged), `test_ab_check_cli_reconciles_even_when_the_ledger_shows_no_ab` (exit 3).
Mutations: remove the `ab_reconcile` call — RED (4 tests); `if False:` on the never-seen guard — RED
(the operator's A/B is cleared); restore the `state is None` short-circuit in `main` — RED.

## N5-N8

| # | Change | Test | Mutation goes RED |
| --- | --- | --- | --- |
| N5 | `grid/verdict_corpus.py` `export()` gets the same `if e.get("request_id")` guard `ab_arms` got — the identical line, and the same malformed log line would have `KeyError`d the weekly corpus export | `test_verdict_corpus.py::test_export_skips_a_proposal_log_line_with_no_request_id` | drop the guard, `KeyError` |
| N6 | New `_exit_code(row)`; `ab --abort` and `rollback` now exit 3 on `flip_incomplete` like `release` and `ab --check` | `test_ab_abort_cli_exits_three_when_the_relay_restart_failed`, `test_rollback_cli_exits_three_when_the_relay_restart_failed` (fake `Remote`, no ssh) | `return 0` in each branch |
| N7 | `_env_ab_view` drops the `""` arm and reports `incumbent: None` when `MIND_MODEL_ALIAS` is unset, instead of crediting every unattributed verdict row to a phantom incumbent | `test_env_ab_view_does_not_credit_unattributed_rows_to_a_missing_incumbent` | drop `arms.pop("", None)` |
| N8 | `_defers` is pruned to the current request id on every poll — thegrid keeps one slot, so a counter under any other id belongs to an abandoned request | `test_odin.py::test_the_defer_counter_does_not_outlive_its_request` | drop the prune |

README: the `set-mind-model.sh` warning now states the two-week bound and that the candidate is not
blacklisted; a new **Reconciliation** paragraph; the exit-code list rewritten as one contract shared
by `release`, `rollback`, `ab --check` and `ab --abort`; a "never put a comment after a value" note.

## Not done (deliberately)

`_retry_not_before` leaks on abandoned requests the same way `_defers` did (the reviewer's own N8
note). The ruling scoped this round to `_defers`, and pruning the retry map changes deferral timing,
so it is left as a follow-up.

## Verification

```
.venv/Scripts/python.exe -m pytest tests -q -W error
272 passed, 4 skipped in 22.00s
```

---

# Round 3 — the final verification of `d3aeb4e`

One commit on top of `d3aeb4e`, scoped to D1, D2, D3, D5 and D6. D4, D7, D8, D9 and the
env-versus-relay third leg are the coordinator's separate follow-ups and are untouched here.
Files: `grid/weekly.py`, `deploy/README.md`, `tests/test_weekly.py`, `tests/test_deploy_units.py`.

7 new mutations applied for real, all RED. Every earlier-round mutation was re-run: the round-2 set
is still RED except the two anchors this round rewrote (`N2-stuck-short_arms`,
`N4-hand-started-guard`), both re-run against the new text and RED; the round-1 set is still RED
except the four already superseded in round 2 (`C2`, `I1` re-verified against their current text;
`I3-lost_tags`, `minor-env_get` replaced by N2 and N3).

## D1 (Important) — the hand-start exemption was one-shot per tag

The reconciler exempted an env-only candidate only while its tag had never appeared on **any**
lifecycle row. The §7 receipt ends in `ab --abort`, which appends a `held` row for that tag — so the
receipt spent its own exemption and the **second** run of the same receipt had its split cleared by
the next nightly.

The guard now keys on `ab_started` rows only, which is the question it was always asking: *did the
gate start this A/B?* The gate writes `ab_started` whenever it starts one; a hand-started A/B never
has one, before or after an abort. The reconciler therefore clears an env candidate only when the
ledger holds an `ab_started` row for that tag whose A/B was subsequently closed, or when the last
lifecycle decision is a standing `flip_incomplete`. `_AB_DECISIONS` is still what identifies that
last decision — it was simply far broader than this branch needed.

- `tests/test_weekly.py::test_reconciler_exemption_survives_the_receipts_own_abort` runs the receipt
  shape twice over one ledger — hand-set env, nightly (untouched), `ab --abort`, hand-set the same
  tag again, nightly — and asserts the second hand-start survives with `grid.env` **byte-identical**
  and no relay restart. Mutation: key on `_AB_DECISIONS` again — RED.
- `..._leaves_a_hand_started_ab_completely_alone` still guards the branch itself. Mutation:
  `if False:` on the narrowed guard — RED (both tests).

README: the reconciler paragraph now states the rule as "did the *gate* start this?", says the
exemption survives `ab --abort`, and notes that a hand-started split suspends the other repairs while
it is up.

## D2 (Important) — one helper, one meaning

`short_arms` meant "below `MIN_SAMPLE`" in `release()` and "produced nothing" in a hand-rolled
expression in `ab_check`. Both values were right, but the obvious refactor — swapping the expression
for the identically-named helper 300 lines above — would have silently stopped blacklisting every
classic stuck tag (a stuck incumbent sits between `STUCK_INCUMBENT_MIN` 50 and `MIN_SAMPLE` 100, so
the helper at its default bar reports **both** arms short) and reopened the §8 livelock.

Fixed as an overload rather than documented: the hand-rolled expression is deleted and `ab_check`
calls `short_arms(..., min_sample=1)`. The helper's docstring now says `min_sample` is the caller's
bar and why the two bars differ. Classic stuck yields `[candidate]` (blacklisted, correct); the
both-arms-zero outage yields both arms (not blacklisted, correct).

- Both cases asserted end to end in `test_ab_check_aborts_a_stuck_ab` and
  `..._aborts_a_wedged_relay_with_both_arms_at_zero` (each asserts the recorded `short_arms` **and**
  the resulting `lost_tags`). Mutation: drop `min_sample=1` — RED on the classic case exactly as the
  verification predicted (the outage case stays green, since both arms are short at either bar);
  drop the field entirely — RED on both.

## D3 (Minor, folded in) — the relabelled row now says what happened

`short_arms: []` is the encoding a genuine loss carries — "both arms fully sampled, decided on the
merits" — i.e. the strongest possible attribution to the candidate, on a row where `arms` is
literally `{}` and nothing was judged at all. It was kept out of `lost_tags` by a reason prefix that
a natural reword ("colony A/B inconclusive: incumbent label changed…") would have removed. It is now
`[candidate, incumbent]`, so level 2 of the memory rule agrees with level 1 instead of contradicting
it and `lost_tags` abstains for two independent reasons.

- `test_a_relabelled_ab_still_ends_at_max_weeks` keeps its `lost_tags(vs) == set()` assertion, now
  asserts the pair, and adds a reworded-reason ledger whose `lost_tags` must still be empty.
  Mutation: `short_arms: []` — RED on the reworded assertion.

## D5 (Minor) — `_exit_code` covers a failed flip

`flip_failed` is not a decision — `DECISIONS` has no such member. `rollback` records `held` and puts
the flip's own outcome verbatim at the end of the reason (`rollback from X: flip_failed`), which is
the only machine-readable channel it has, so `_exit_code` now also returns 3 for a reason ending
`: flip_failed`. A rollback that did not roll back no longer exits 0.

**Scope judgement, flagged.** `release`'s C2 win-but-copy-failed row stays at 0 on purpose: `end_ab`
ran, env/relay/ledger agree, `held` is that verb's normal terminal state (shared with "no promotion",
"probes failed", "not on the key"), and the gate retries the copy next Sunday. Exit 3 there would
fail the Sunday unit for a condition that needs no operator action, which is the opposite of what the
code means by 3. Say the word if you want it included and it is one more `or` clause.

- `test_rollback_cli_exits_three_when_the_tag_copy_failed` — `FakeRemote(fail={"ollama cp"})`, a
  successful relay restart, asserts exit 3, `held`, the reason suffix, and that `MIND_MODEL_ALIAS`
  never moved. Mutation: drop the `endswith` clause — RED.

## D6 (Minor) — the example guard catches every comment shape

The shipped-file guard matched only `" #"`, so `ODIN_RETRY_S=60# x` and a tab before the `#` both
passed while killing the relay identically. The predicate is now any `#` in a value and lives in
`inline_comment_keys(text)` so it can be tested directly.

- `test_grid_env_example_has_no_inline_comments` (over the real file) plus
  `test_the_inline_comment_guard_catches_every_shape` (spaced, tight, tabbed, commented-out template,
  and two negatives: a prose line containing `key=value` and a `#`, and clean lines).
  Mutations: narrow the predicate back to `" #"` — RED; put a *tight* comment on `ODIN_RETRY_S` in
  the shipped example — RED (the old guard would have passed it).

## Verification

```
.venv/Scripts/python.exe -m pytest tests -q -W error
275 passed, 4 skipped in 21.65s
```
