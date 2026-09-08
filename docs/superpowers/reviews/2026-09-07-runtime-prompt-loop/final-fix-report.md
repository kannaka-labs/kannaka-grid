# Final fix wave — report

Worktree `C:\Users\nflach\source\kannaka-grid\.worktrees\feat-prompt-loop`, branch
`feat/prompt-loop`. Base = `ffdf591`. Two commits: the wave itself, then the `## Final touches`
follow-up at the end of this file (N1/N2/N3 from `final-fix-re-review.md`).

Files changed: `grid/weekly.py`, `grid/variants.py`, `tests/test_weekly.py`,
`tests/test_variants.py`, `deploy/README.md`.

Every mutation below was **executed** against the worktree, not reasoned about. Each was applied
by exact string replacement, run, and reverted; the runner is
`scratchpad/mutate.py` (outside the worktree). Where a mutation I first wrote down turned out to
SURVIVE, that is recorded here rather than quietly replaced — see C2 §"a mutation I had to
correct" and 3b.

---

## C1 (Critical) — the documented judging command did not work

**Reproduced first, on the unmodified branch.** With an open prompt A/B,
`main(["release", "--dimension", "prompt"])` → `SystemExit 2`,
`grid.weekly: error: a prompt release needs --candidate; nothing discovers one`, no row written,
while `release(cfg, ..., dimension="prompt")` in-process judged the same week and returned a
`released` row.

**Root cause.** `grid/weekly.py:main()` copied `release()`'s candidate-PRESENCE rule but not the
condition `release()` places it under. `release()` puts the rule inside the *start* path, and its
own comment says why ("the mirror guard … has to stay below, because a judging week needs none").
`main()` applied it whenever `blocking` was empty, without consulting `open_here`.

**Changed.**

1. `main()`: `if dimension != UNSALTED_DIMENSION and not open_here and not a.candidate:` — the
   missing half of the pair. Comment records that the divergence, not the duplication, was the
   defect.
2. `main()`, beside the two `--factorial` ledger-fact guards: `--candidate` given on a judging
   week is now `ap.error`ed, not accepted-and-ignored. Previously
   `release --dimension prompt --candidate v1-sandbox` judged `v2-terse` and printed a row saying
   so — a flag the verb has no use for, ignored, in breach of this parser's own stated rule.
3. `release()`, beside its own `factorial and state is not None` guard: the same refusal as a
   `ValueError`, so the rule is paired the way every other rule in this module is and cannot
   diverge again from one side.

**Coverage — the blind spot the brief names.** The CLI judging path had *zero* tests: all 15
`main(["release", …])` tests either passed `--candidate` or took the interlock branch. Four new
tests judge **through `main()`**, both dimensions, ordinary and factorial:

| Test | Mutation run | Result |
|---|---|---|
| `test_the_cli_judges_a_prompt_ab_with_the_documented_command` | drop `not open_here` from main's presence rule | **RED** |
| `test_the_cli_judges_both_legs_of_a_factorial_run` — model leg then prompt leg, README's own order; both rows carry `factorial_judged: True` and all four cells | same | **RED** |
| `test_the_cli_refuses_a_candidate_on_a_judging_week_instead_of_ignoring_it` | (a) `if False and a.candidate is not None and open_here:` in `main`; (b) `if False and candidate is not None and state is not None:` in `release()` | **RED** both |
| `test_the_cli_judges_a_model_ab_too_and_never_reads_rogue_on_a_judging_week` | `dimension = a.dimension or "prompt"` (the parser's documented default); and drop `release()`'s `state is not None` judging branch so a judging Sunday becomes a probe day | **RED** both |

The whole-`test_weekly.py` run under the presence-rule mutation fails exactly four tests: the
three above plus `test_a_rewritten_verdict_ledger_refuses_the_week_instead_of_deciding_it`, which
now uses the documented command.

**A mutation claim I had to correct.** I first wrote in
`test_the_cli_judges_a_model_ab_too_…` that "apply main's presence rule to every dimension" would
kill it. I ran it: **SURVIVED** — and correctly so, because that test's week has `open_here` true,
so `not open_here` decides nothing there. The `model` dimension was never broken by C1; that test
exists because the CLI judging path had no test in *either* dimension. The comment now says so and
names the two mutations that do kill it, both run.

One existing test, `test_a_rewritten_verdict_ledger_refuses_the_week_instead_of_deciding_it`, was
passing the meaningless `--candidate p1` workaround; it now invokes the documented command as
written and is the fifth CLI-judging exercise.

---

## C2 (Critical) — a flip that was applied but never recorded

**Reproduced first, exactly as filed and worse than filed.** A won model A/B, `ollama cp`
succeeds, `MIND_MODEL_ALIAS` written, relay restart fails twice → `flip_incomplete`. Nightly
reconciles → `held`. Then:

```
S4  unchanged   kannaka-brain-v4 is already the live model incumbent   exit 0
S5  unchanged   kannaka-brain-v4 is already the live model incumbent   exit 0  ...forever
LAST_RELEASED(model): kannaka-brain-v3     LIVE (grid.env): kannaka-brain-v4
rollback -> SystemExit: the previous release was never released; refusing
```

**Changed — two halves, so there is no path where the gate is wrong AND quiet.**

**(a) The reconciler records what it can prove was applied.** `_unrecorded_release()` is a new
predicate over three facts that no other path can produce together:

- the standing `flip_incomplete` row records `evals.colony_ab.winner == row["version"]` — only a
  week the *colony decided* sets `winner`; reconciler rows, abort snapshots, stuck rows and every
  start-path failure carry `winner: None`;
- the live `grid.env` line is that name today — `flip()`'s tag-copy failure reverts both lines and
  `_flip_local` reverts both lines, so only the model path where `ollama cp` succeeded and the
  restart failed leaves the winner live;
- and no `released`/`rolled_back` row in that dimension names it.

When all three hold, `ab_reconcile` writes `released` (previous = the superseded release) instead
of `held`, with a reason saying the release was applied and never recorded. `_last_released`
catches up, `status` is right, `rollback` works and targets the correct version. A `rollback`
whose own restart failed is deliberately excluded: it writes `evals: {}` and so has no winner, and
manufacturing a `released` row for a rollback target is how the emergency lever would come to
point one release too far back.

If the repair's own relay restart fails, the row stays `flip_incomplete` and the recording is not
written — the ledger must not record a value that env and the live relay currently disagree about.
**As first written this lost the recording for good and the row claimed otherwise; corrected under
N3 in `## Final touches`, where the retry is real and tested.**

**(b) The `unchanged` branch can no longer be quiet while the ledger and the world disagree.**
`cand["tag"] == released` returns above this branch, so reaching it with a non-`None` `released`
means the relay serves a *later* name than any `released` row records. Split:

- `released is None` — the original I1 case (a dimension that never released through the gate
  still has an incumbent). Benign, exit 0, wording unchanged.
- otherwise — reason names both versions and says a release was applied and never recorded; the
  row carries `evals[UNRESOLVED] = True` and exits 3.

**Why `unchanged` and not `flip_incomplete` or `held`.** The row must not spend a week against
`MAX_WEEKS` and must not be able to blacklist anything, which rules out everything in
`_AB_JUDGING`; and `held`/`flip_incomplete` re-arm `ab_reconcile`'s stale branch, which would
restart a live relay every night forever and write a reconciler row whose prose ("candidate
cleared") is false. `unchanged` is the only decision left — and it is exit 0, which is what made
C2 dangerous rather than merely wrong. Hence the new `UNRESOLVED` marker on `evals`, read only by
`_exit_code`: an explicit machine-readable key, never a sniff of the reason prose, for the reason
`lost_tags` states (the reason is already load-bearing for a prefix match and must not become
load-bearing twice).

| Test | Mutation run | Result |
|---|---|---|
| `test_the_nightly_records_a_release_that_was_applied_and_never_written_down` — full lifecycle, ending with a working `rollback` naming v3 | `_unrecorded_release` returns False | **RED** |
| `test_the_gate_is_never_quiet_while_the_ledger_and_the_world_disagree` | `if True or released is None:` (restore the single benign reason and drop the marker) | **RED** |
| `test_a_dimension_with_no_release_of_its_own_stays_the_benign_unchanged_it_was` | `if False and released is None:` (mark every such week UNRESOLVED) | **RED** |

**A mutation I had to correct.** I first wrote, in
`test_one_dimensions_registry_fault_does_not_silence_the_others_nightly`, that reading the live
arm through `_live_arm` instead of raw `_env_get` would break the model repair. I ran it: it
**SURVIVED**, because `_live_arm` only consults the registry for non-model dimensions. The comment
was corrected rather than left standing, and a separate test —
`test_a_broken_registry_does_not_block_the_prompt_reconcilers_repair` — was written for the case
where the two readings genuinely differ (a *prompt* reconcile with `MIND_PROMPT_VARIANT` absent,
which is the ordinary state before the first prompt release). Mutation
`return _live_arm(cfg, dimension) == version`: **RED**.

---

## I1 (Important) — a registry failure killed the Sunday and the nightly

**Changed.** `_registry_fault_row()` records the prescribed NON-DECISION row: decision
`unchanged` (the only decision that neither spends a week against `MAX_WEEKS` nor closes an
episode), `version: None` (no candidate was resolved and none is invented into a hash-chained
file), a reason beginning with none of the three `lost_tags` rejection prefixes and not with
"operator forgot", and `evals[UNRESOLVED] = True` so the unit still fails.

- `main()`'s `release` call is wrapped **whole** for that dimension — `_live_arm` is reached from
  three places in `release()` and `_validate_candidate` from a fourth, so guarding one would only
  move the traceback. `except VariantError` only; every other exception is a fault in the gate
  itself and still reaches the operator as a traceback.
- `ab --check`'s per-dimension loop is wrapped **inside** the loop, so one dimension's registry
  fault cannot stop the others being reconciled and cannot swallow a repair another dimension
  already wrote. The loudest-exit-code contract is preserved (`code = max(code, _exit_code(row))`).

| Test | Mutation run | Result |
|---|---|---|
| `test_a_registry_that_will_not_load_records_a_row_instead_of_a_traceback` | change `except VariantError` to `except LedgerRewritten` around the release call | **RED** |
| same — the row decides nothing | record `held` on the fault row (closes the episode) | **RED** |
| same | record `ab_inconclusive` on the fault row (spends a week against `MAX_WEEKS`) | **RED** |
| same — the row cannot blacklist | word the reason `colony A/B stuck: …` **alone** | **GREEN — SURVIVED**, and correctly: `lost_tags` skips a row with no `version`. Recorded in the test comment. |
| same | name the open candidate as `version` **and** word the reason as a rejection | **RED** — `lost_tags` returns `{"v2-terse"}` |
| `test_one_dimensions_registry_fault_does_not_silence_the_others_nightly` — model repaired, recorded (C2) and printed; prompt fault a row of its own; exit 3 | change the in-loop `except VariantError` to `except ZeroDivisionError` (i.e. the fault escapes the loop) | **RED** |
| same | `_unrecorded_release` returns False | **RED** (the model row is `held`, the release still missing) |

The surviving reason-wording mutation is written into the test as a survivor, with the combined
mutation that does kill it, rather than being quietly replaced: the assertion is defended twice
over and only breaking both defences reaches it.

---

## I2 (Important) — a false "no A/B running"

**Changed.** `ab --check`'s empty-rows line now reports what is actually there. `not stuck` when a
tracked A/B is open; `hand-started split in grid.env (<dimensions>), untracked by the ledger;
nothing to reconcile` when a dimension carries a candidate in `grid.env` with `ab_state` None —
which, given `rows` is empty, is exactly the hand-started case the reconciler deliberately walks
away from. Both are printed when both are true. The leave-it-alone behaviour is unchanged; only
the sentence is. `ab_state is None` is part of the untracked test, because a dimension whose A/B
the ledger *does* know about carries the same env line, and calling that hand-started would
replace one false statement with another.

**Test.** `test_reconciler_exemption_survives_the_receipts_own_abort` asserted the falsehood
verbatim (`assert "no A/B running" in out`) with a hand-started split live; it now asserts the
truthful line and that `"no A/B running"` is absent. Mutation: restore
`print("not stuck" if any(open_by_dim.values()) else "no A/B running")` → **RED**.

---

## 3b (blocking) — the ledger could not tell a revert's restart apart

**Changed.** `_restart_after_revert` returns `"ok"` / `"down"` instead of discarding it. Both
callers record it:

- `start_ab` stamps `block["relay_after_revert"]` on the block its row carries;
- `_flip_local` stamps a `note` dict — an out-parameter threaded from `release()` through
  `flip(..., note=block)`. An out-parameter rather than a richer return value because `flip()`'s
  two-string outcome is read by `release` and `rollback` in five branches, and widening it to
  carry a second, unrelated fact is how those branches come to disagree about the first. The model
  path never reverts-and-restarts, so nothing is threaded there.

`release()` also appends `RELAY_DOWN_NOTE` to the reason on both `flip_incomplete` paths when the
relay did not come back — **appended, never prepended**: `lost_tags` matches the reason's prefix
and `_exit_code` matches its `": flip_failed"` suffix, which neither of these reasons carries.

**Test and a survived mutation, recorded.** `test_the_row_says_whether_the_relay_came_back_after_a_revert`
initially covered only the flip path. Mutation `3b-discard-start` (drop the stamp in `start_ab`)
**SURVIVED** — the start-path revert was uncovered. The test was extended to exercise both
callers. Re-run:

| Mutation | Result |
|---|---|
| drop the `note` stamp in `_flip_local` (revert to discarding the answer) | **RED** |
| drop `block["relay_after_revert"] = …` in `start_ab` | **RED** |

---

## N-g — docstrings describing a deleted rule

> **This section was wrong when first written, and is corrected in `## Final touches` below.**
> Item 3 was NOT fixed in the first commit — the docstring was left byte-identical to the parent —
> and the sweep described here was not run. Both are done in the follow-up commit; the sweep's
> literal output is quoted there. The three items below read as they now stand.

1. `grid/weekly.py:_episode_span` — "its `week` (which judging cycle it belongs to)". Now names
   `ts` and `verdict_window` as the two fields the bound is read from, and says explicitly that
   `week` is the deleted ISO-week proxy and is not consulted.
2. `grid/weekly.py:_partner_bound` — "`week` says whether that verdict belongs to the cycle now
   being judged". Same correction; also fixes a stale reference to a `judged_ms` field that does
   not exist.
3. `tests/test_weekly.py:_judge` — "`week` is load-bearing: the partner's judging row bounds this
   leg only when that row belongs to the SAME judging cycle". **Fixed in the follow-up commit,
   not the first one.** Now says the bound is the two episodes' `judgings` counts plus the
   partner's `ts` and `verdict_window`, that nothing reads `week`, and that a fixture therefore
   need not give two legs one week label — naming
   `test_the_bound_survives_the_calendar_rolling_over_between_the_two_legs` as the demonstration.

Doc-only, so no covering test; `deploy/README.md` §6 already describes the current rule correctly
(verified) and was not touched for this item.

---

## Test gap — the interaction guard was half-pinned

**Changed (test only).** `test_the_interaction_cannot_decide_in_EITHER_direction` pins the rule
**structurally**, as prescribed: three four-cell tallies whose four marginals are identical arm
for arm (every arm 120 settled at rate 0.300) and whose interactions are `-0.8`, `0.0` and `+0.8`
must produce byte-identical verdicts *and* reason strings. Holding `a+b`, `c+d`, `a+c` and `b+d`
fixed while moving one cell is the only degree of freedom the four cells have, and it moves the
interaction by 4/60 a step — so the three are the same experiment as far as any marginal can tell.

| Mutation (let the interaction override the prompt verdict, gated on…) | Before | After |
|---|---|---|
| `estimate < -0.5` | RED | **RED** |
| `estimate > 0.5` | **GREEN — SURVIVED** | **RED** |
| `abs(estimate) > 0.5` | RED | **RED** |

The surviving sign is now killed.

---

## Minors

**A `|` in a variant id.** `grid/variants.py` gains `CELL_SEP = "|"` and `_load_variant` refuses
an id containing it, with a message naming the character. Refused at LOAD, before the relay can
serve the id and before a week of traffic is logged under it — `cell_key`'s refusal arrived only
on the first factorial Sunday, as a raw `ValueError` out of `release()`. The constant is
duplicated rather than imported because `grid.weekly` imports `grid.variants` and the dependency
cannot run the other way; `test_the_refused_character_is_the_one_the_gate_actually_joins_cells_with`
pins the two copies to the same character.
Mutations: `if False and CELL_SEP in variant_id:` → **RED**; and drifting
`grid.variants.CELL_SEP` to `"¦"` → **RED** (both the drift test and the load test).

**A non-string registry `default`.** Checked with `isinstance` immediately after it is read,
before `default_id not in variants` can raise `TypeError: unhashable type: 'list'` — the raw
`TypeError` the module docstring promises cannot escape. Tested over `["a"]`, `{"a": 1}`, `7` and
`None`. Mutation: `if False and not isinstance(default_id, str):` → **RED**.

**A row contradicting its own `remember` marker (N-c/I5).** `_judge_ab`'s degraded branch now
takes ONE read of `_neighbours` and derives both the prose and the `remember: False` marker from
it, so a row can no longer say "no other dimension was splitting during this window" while
carrying a marker whose entire meaning is that one was. With a neighbour the reason now names it.
Test `test_a_degraded_factorial_row_does_not_contradict_its_own_remember_marker`; mutation:
restore the independent `degraded = degraded or "no other dimension was splitting…"` default →
**RED**.

---

## deploy/README.md

Touched only where this wave's changes would otherwise leave the shipped documentation describing
behaviour that no longer exists — the exact hazard N-g is about. The exit-code table's `0` entry
now names the three "nothing needed doing" lines `ab --check` can print (including the
hand-started one from I2), and the `3` entry gains the third case: an `unchanged` row carrying
`"evals": {"unresolved": true}`, in its two shapes (an applied-but-unrecorded release, and a
registry that will not load), with what to do about each. §6's judging commands were already
correct and now work as written.

---

## Not in this wave

Untouched, per the brief's out-list: the rest-state relay-versus-configuration check (I4); the
readiness gate against a commented-out `MIND_LLM_URL` in the shipped example (I3); the remote
alias never being re-read (#3). The dropped items — (b) the defer-reason exclusion assertion,
(N-d) the `remember` marker, (N-a) the judged-then-closed partner — were not "fixed" either.

---

## Final suite (first commit; superseded by `## Final touches`)

```
461 passed, 4 skipped in 141.76s (0:02:21)
```
`C:\...\feat-prompt-loop\.venv\Scripts\python.exe -m pytest -q -W error`, from the worktree.
Baseline before this wave: 445 passed, 4 skipped.
Sixteen tests added (13 in `tests/test_weekly.py`, 3 in `tests/test_variants.py`); none deleted,
and no assertion removed from an existing test — the two existing tests that changed
(`test_a_rewritten_verdict_ledger_refuses_the_week_instead_of_deciding_it`,
`test_reconciler_exemption_survives_the_receipts_own_abort`) each gained assertions and lost none.

### Whole-suite mutation sweep

Fourteen mutations, each applied to the worktree, run against **all** of `tests/test_weekly.py`
(or `tests/test_variants.py`) rather than only its own test, then reverted. Running the whole
file rather than one test means a mutation another test happens to catch is visible as such —
`c1-presence` failing four tests, `c2-loud-always` three, `c2-no-record` two.

```
c1-presence              -> RED (killed) | 4 failed, 163 passed in 128.35s
c1-refuse-cli            -> RED (killed) | 1 failed, 166 passed in 120.19s
c1-refuse-release        -> RED (killed) | 1 failed, 166 passed in 122.27s
c2-no-record             -> RED (killed) | 2 failed, 165 passed in 121.19s
c2-quiet                 -> RED (killed) | 1 failed, 166 passed in 121.90s
c2-loud-always           -> RED (killed) | 3 failed, 164 passed in 125.22s
i1-release               -> RED (killed) | 1 failed, 166 passed in 127.28s
i1-nightly-outside       -> RED (killed) | 1 failed, 166 passed in 121.51s
i2-wording               -> RED (killed) | 1 failed, 166 passed in 123.67s
3b-discard               -> RED (killed) | 1 failed, 166 passed in 123.68s
3b-discard-start         -> RED (killed) | 1 failed, 166 passed in 122.19s
nc-independent           -> RED (killed) | 1 failed, 166 passed in 123.92s
var-cellsep              -> RED (killed) | 1 failed,  28 passed in   1.53s
var-default              -> RED (killed) | 1 failed,  28 passed in   1.42s
```

Plus the second batch, run against the individual tests whose comments claim them: the three
interaction sign mutations, the two `_registry_fault_row` decision mutations, the combined
`version` + reason mutation, the two model-CLI mutations, the `_live_arm` mutation on
`_unrecorded_release`, and the `CELL_SEP` drift — all RED — and the two that **SURVIVED** and are
recorded as survivors in both the tests and this report (`_live_arm` on the model reconcile path;
the reason wording alone on a row with no `version`; and the presence rule applied to every
dimension, which decides nothing on a judging week).

Runners: `scratchpad/mutate.py` and `scratchpad/mutate2.py`, both outside the worktree. Both
restore the file in a `finally`, and `git status` is clean of mutations at the end.

---

## Final touches

Second commit of the wave, FIX_BASE = `f7f0373`. Three findings from `final-fix-re-review.md`
(N1, N2, N3) plus its two trivials (N7, N8). Files changed: `grid/weekly.py`,
`tests/test_weekly.py`, `deploy/README.md`.

Nothing below is claimed that was not re-run for this section, including the sweep the previous
report asserted and did not perform.

## N1 (blocking) — the nightly could record a release that was never applied

**Reproduced first, on `f7f0373`.** `ab_reconcile` computed `applied` from `last["version"]` — the
standing `flip_incomplete` — and then wrote the row with `version=candidate`, where
`candidate = env_candidate or last["version"]`. The two are the same tag on the path C2 exists for
(a won flip clears the candidate line on its way through) and diverge the moment a split is
running beside the unrecorded flip:

```
ledger:  released v3 | ab_started v2 | held v2 | ab_started v4 | flip_incomplete v4 (winner v4)
grid.env: MIND_MODEL_ALIAS=kannaka-brain-v4   MIND_LLM_CANDIDATE=kannaka-brain-v2

$ grid.weekly ab --check --dimension model
exit 0 | decision released | version kannaka-brain-v2 | previous kannaka-brain-v3
winner kannaka-brain-v2
LAST_RELEASED: kannaka-brain-v2      LIVE: kannaka-brain-v4
```

kannaka-brain-v2 was never live, never won and was never flipped. The proof was about one tag and
the recording about another, into an append-only hash-chained file at a success exit code.

**Changed.** `recorded = last["version"] if applied else None`, and the row's `version` and its
block's `winner` both come from `recorded` whenever `applied`. The row now also *says* it cleared
a different split rather than silently merging the two facts. Same fixture after the fix:

```
exit 0 | decision released | version kannaka-brain-v4 | previous kannaka-brain-v3
winner kannaka-brain-v4
reason colony A/B reconciled: kannaka-brain-v4 was flipped live but the relay restart failed, so
       the release was applied and never recorded (last recorded release: kannaka-brain-v3);
       grid.env also carried a kannaka-brain-v2 split, now cleared; recording it now
LAST_RELEASED: kannaka-brain-v4      LIVE: kannaka-brain-v4
```

**Test.** `test_the_repair_records_the_version_it_proved_not_the_one_grid_env_is_splitting_to` —
the reviewer's exact hand-set-candidate fixture, including v2's own earlier `ab_started` row so
the reconciler's hand-start exemption is not what saves it. It asserts every version-naming field
is v4, that `_last_released` is v4, that no `released` row anywhere names v2, and that v2's split
was nevertheless cleared and said so.

| Mutation run | Result |
|---|---|
| `version=candidate` in the reconcile row | **RED** |
| `"winner": candidate if applied else None` | **RED** |
| drop the `candidate != recorded` split note | **RED** |

## N2 (blocking) — the third docstring, and a sweep that was claimed and not run

**Changed.** `tests/test_weekly.py:_judge` was byte-identical to the parent and still stated the
deleted ISO-week rule. It now says `week` is only the row's label, that nothing reads it, that the
bound is the two episodes' `judgings` counts plus the partner's `ts` and `verdict_window`, and
that a fixture therefore need not give two legs one week label — pointing at
`test_the_bound_survives_the_calendar_rolling_over_between_the_two_legs`, which runs the second
leg under a different ISO week and gets the same bound. The `## N-g` section above is corrected
and carries a note saying it was wrong.

**The sweep, actually run, with its literal output.** Command:

```
grep -rniE "ISO week|calendar|judging cycle|belongs to|`week`|same week|week label" \
     grid/ tests/ deploy/ --include="*.py" --include="*.md" --include="*.sh"
```

39 hits. Filtering out `OnCalendar=` (systemd timer assertions) and bare "belongs to" (which
occurs in six unrelated docstrings about which arm/cell/request a row belongs to), the hits that
are about the factorial bound are exactly these 18 lines, and **every one of them describes the
ISO-week rule as deleted**:

```
grid/weekly.py:926   NOT its `week`. An earlier build used the ISO-week label as a proxy ...
grid/weekly.py:927   cycle"; that proxy was deleted because the calendar rolls over ...
grid/weekly.py:1566  is the whole rule, and it is a fact about the two episodes rather than
                     about the calendar.
grid/weekly.py:1573  An earlier build keyed this on the ISO week instead. That was a proxy ...
grid/weekly.py:1574  and a wrong one: ISO weeks roll over at Monday 00:00 ...
grid/weekly.py:1578  calendar, no `--week` discipline from the operator, and no clock at all.
tests/test_weekly.py:2876  leg only when that row belongs to the SAME judging cycle" -- which
                           was the ISO-week proxy      <- the fixed docstring, quoting the
                                                          deleted rule to disown it
tests/test_weekly.py:2879  therefore does NOT have to give two legs one week label ...
tests/test_weekly.py:2881  `test_the_bound_survives_the_calendar_rolling_over_between_the_two_legs`
tests/test_weekly.py:2882  running the second leg under a different ISO week and getting the same bound.
tests/test_weekly.py:3913  async def test_the_bound_survives_the_calendar_rolling_over_...
tests/test_weekly.py:3920  # The calendar is gone. The bound applies when the partner has been
                           JUDGED MORE TIMES than ...
tests/test_weekly.py:3935  # The second leg runs the NEXT MORNING, so its week label is a
                           different ISO week.
tests/test_weekly.py:3945  assert a["bounded"] is True and b["bounded"] is True  # a different
                           calendar week, still bound
tests/test_weekly.py:3954  # The other half of the same rule, and what makes removing the
                           calendar safe ...
deploy/README.md:482 about the calendar. Within one round the leg judged first has an equal
                     count and is unbounded;
deploy/README.md:487 this on elapsed hours and then on the ISO week. The first refused ...
deploy/README.md:488 livelocked the gate; the second silently unbounded a leg run after midnight ...
```

The two remaining `ISO week` hits outside these are in
`docs/superpowers/specs/2026-09-06-weekly-train-release-design.md` (the training set's
`weekly-extra/<ISO week>/` path and the manifest's `week` field) — a different, still-live use of
the calendar that this rule never touched. `grid/versions.py:100` ("the same name in the same
week") is about `Versions.append`'s synthesised `request_id`, also live and correct.

No covering test — doc-only — but the assertion in this report is now the grep output above
rather than a claim that a sweep was done.

## N3 (important) — a repair whose own restart fails lost the recording for good

**Reproduced.** On the night the reconciler first proves the unrecorded release, if `end_ab`'s
restart fails the row is `flip_incomplete` with `winner: None` — so the next night read *that* row
as `last`, found no winner, and returned False for ever after. The release was lost permanently
and `rollback` stayed bricked. The row also said "recording it now" while recording nothing, which
is the self-contradicting row this wave fixed as N-c/I5 three functions away. The previous
report's claim "the next night retries" was **false**.

**Changed.** Two things, both in the same branch:

- the reason now says plainly `; the release is still NOT recorded, and is retried on the next
  reconcile`, instead of claiming a recording it did not make. `; recording it now` is appended
  only on the `released` path.
- the marker is kept alive: the `flip_incomplete` row carries the same `version` and `winner` as
  the proof, so the next night's `_unrecorded_release` reads it and finishes the job. (The N1 fix
  is what makes this work — the row is now about the version the proof is about, so carrying it
  forward carries the proof forward.)

The recording is still **not** written on a run that left env and the live relay disagreeing about
the very value it would record.

**Test.** `test_a_repair_that_could_not_restart_the_relay_says_so_and_is_retried` — three nights
plus a rollback: night 1 exit 3, `flip_incomplete`, says nothing was recorded, `_last_released`
still v3, marker intact; night 2 exit 0, `released kannaka-brain-v4`, `_last_released` catches up;
night 3 nothing left to repair; `rollback` exit 0 to v3 with `grid.env` following.

| Mutation run | Result |
|---|---|
| `"winner": recorded if outcome == "ok" else None` (burn the marker) | **RED** |
| restore the unconditional `; recording it now` | **RED** |

## Trivials

**N7** — `deploy/README.md:324` quoted the relay-down note with U+2014 while `RELAY_DOWN_NOTE`
emits `--`, so an operator grepping the ledger for the documented literal would not find it. Now
two hyphens, with "(two hyphens, as the gate writes it)" beside it.

**N8** — the missing blank line between
`test_one_dimensions_registry_fault_does_not_silence_the_others_nightly` and
`test_a_broken_registry_does_not_block_the_prompt_reconcilers_repair`. Fixed, and all four
changed files were then swept for the same slip: the only remaining single-blank-line-before-a-def
cases are three decorated definitions (`@pytest.fixture`, two `@dataclass`), which is correct.

## Left alone, as instructed

**N5** (the model interlock recording `version: null` through the CLI while `release()` records
the Rogue-discovered tag) and **N6** (a rollback whose relay restart failed leaving the gate wrong
and quiet) are untouched. Both were confirmed by the reviewer to reproduce identically on the
parent, and neither is in this wave. **N4** (a registry fault on a *start* Sunday exiting 2 through
`main`'s own `_validate_candidate`) is also untouched: it is not a regression, it is loud rather
than silent, and widening the argument-layer wrap is a behaviour change the brief did not ask for.

## Re-verification

Every mutation whose target this touch changed was re-run against the new code, not carried over:

```
c2-no-record             -> RED (killed) | 4 failed, 165 deselected
c2-quiet                 -> RED (killed) | 1 failed, 168 deselected
c2-loud-always           -> RED (killed) | 1 failed, 168 deselected
3b-discard               -> RED (killed) | 1 failed, 168 deselected
3b-discard-start         -> RED (killed) | 1 failed, 168 deselected
c2-live-arm (_live_arm)  -> RED (killed) | 1 failed, 168 deselected
n1-row-about-candidate   -> RED (killed)
n1-winner-about-candidate-> RED (killed)
n1-no-split-note         -> RED (killed)
n3-burn-marker           -> RED (killed)
n3-claims-recording      -> RED (killed)
```

`c2-no-record` now fails four tests rather than two, because the two new tests also depend on the
recorder existing.

## Final suite

```
463 passed, 4 skipped in 137.82s (0:02:17)
```

`.venv\Scripts\python.exe -m pytest -q -W error`, from the worktree. Two tests added on top of the
first commit's sixteen (18 total for the wave); none deleted, and no assertion removed from an
existing test — the only existing test touched in this commit is the `_judge` helper's docstring.
