# Final fix re-review — `ffdf591..f7f0373` (one commit)

Reviewer: final scoped re-reviewer. Date: 2026-09-08.
Scope: this fix diff only. Verified against a clean `git archive f7f0373` export and, for every
before/after claim, a clean `git archive ffdf591` export, both in a scratch directory outside the
repo. The worktree was clean before and after; no tracked file was modified, nothing was
committed, no subagent was dispatched.

Suite on the clean export: **459 passed, 6 skipped in 144.6 s** under `-W error`.
(465 collected = the report's 461 + 4; the two extra skips are `thegrid`-submodule tests that a
`git archive` export cannot run. Consistent with the report's worktree figure.)

Everything below marked "ran", "reproduced" or "proved" was executed. The probe scripts live in
the session scratchpad; each finding names the shape that produced it.

---

## 0. Verdict

C1 is fixed and now proved through the real command line. I1, I2, 3b, the test gap and the three
minors are all genuinely closed, and I re-ran **twenty-five** of the implementer's mutations
myself: every KILL claim is RED and every SURVIVOR claim is GREEN, with zero mismatches. The
mutation story in the report is honest.

Two things stop it merging today, both inside the C2 fix — the half the brief cared most about:

1. **The nightly can record a `released` row for a version that was never applied** (new on this
   commit; the parent writes a harmless `held`). `ab_reconcile` proves the release happened about
   one tag and then writes the row about a *different* one.
2. **The report's N-g claim is false.** Two of the three docstrings were fixed; the third — the
   one the brief named in `tests/test_weekly.py` — is byte-identical to the parent and still
   states the deleted ISO-week rule. The report says all three were fixed and that a re-sweep
   found no others; the sweep's own grep terms hit the line it missed.

Both are small. A third finding (the repair's recording is lost for good if the reconciler's own
restart fails) is Important and should go in the same touch.

---

## 1. Each finding in the brief

| Finding | Verdict | Evidence |
|---|---|---|
| **C1** — the documented judging command | **ADDRESSED** | `python -m grid.weekly release --dimension prompt` exits 2 on `ffdf591`, exits 0 and writes a binding `released` row on `f7f0373`; both dimensions, ordinary and factorial; four new CLI-level tests, all mutation-confirmed. §2. |
| **C1b** — `--candidate` accepted-and-ignored on a judging week | **ADDRESSED** | Parent judged `v2-terse` while the operator named `v1-sandbox` (exit 0). Fix refuses at both layers; `c1-refuse-cli` and `c1-refuse-release` both RED when I ran them. |
| **C2a** — record what was actually applied | **ADDRESSED, WITH A NEW DEFECT** | Full lifecycle reproduced: nightly now writes `released kannaka-brain-v4 (previous v3)`, `_last_released` catches up, `rollback` works and names v3. But see N1 (false recording) and N3 (the repair is not retried). §3. |
| **C2b** — the gate can no longer be wrong and quiet | **ADDRESSED** for the state the brief named | Parent: `unchanged … is already the live model incumbent`, exit 0. Fix: exit 3, `evals.unresolved = true`, a reason naming both versions. The benign `released is None` case stays exit 0 (mutation-confirmed both ways). |
| **I1** — registry failure kills the Sunday and the nightly | **ADDRESSED** (one documented gap, N4) | Judging Sunday: exit 3, `unchanged`, `version: null`, A/B still open, `weeks == 0`, `lost_tags` empty. Nightly: parent raises `VariantError` out of the loop and loses the model repair; fix prints both dimensions' rows and exits 3. §5. |
| **I2** — false "no A/B running" | **ADDRESSED** | All three exit-0 lines the README now documents print correctly, including the combined case. §5. |
| **3b** — the ledger cannot tell a revert's restart apart | **ADDRESSED** | Both callers stamp `relay_after_revert`; `RELAY_DOWN_NOTE` is appended, never prepended; both the flip and the start path are mutation-covered (`3b-discard-flip`, `3b-discard-start`, both RED). |
| **N-g** — three docstrings describing a deleted rule | **NOT ADDRESSED (2 of 3)** | `_episode_span` and `_partner_bound` fixed. `tests/test_weekly.py:2873` `_judge` is unchanged from the parent and still says "`week` is load-bearing: … belongs to the SAME judging cycle". See N2. |
| **Test gap** — the interaction guard half-pinned | **ADDRESSED** | I ran all three sign mutations against both tests: the OLD test survives the positive sign (the exact gap filed); the NEW structural test kills all three. |
| **Minor** — `\|` in a variant id | **ADDRESSED** | Refused at load, message names the character; `var-cellsep` and `var-cellsep-drift` both RED. |
| **Minor** — non-string registry `default` | **ADDRESSED** | `VariantError` over `["a"]`, `{"a":1}`, `7`, `None`; `var-default` RED. |
| **Minor** — row contradicting its own `remember` marker | **ADDRESSED** | One `_neighbours` read feeds both; `nc-independent` RED. |

---

## 2. The documented command, proved

Run as a **real subprocess** — `python -m grid.weekly …` with `RELEASES_DIR`/`VERDICTS_DIR`/
`GRID_ENV`/`MIND_LLM_KEY_FILE` from the environment, real argparse, real hash-chained ledger
writes, real `grid.env` rewrites. The only things stubbed are the two OS calls a Windows box
cannot make (`sudo systemctl restart`, `ssh`), intercepted at `subprocess.run` by a
`sitecustomize.py` on `PYTHONPATH`.

| Command | `ffdf591` | `f7f0373` |
|---|---|---|
| `release --dimension prompt --week W2` (prompt A/B open) | **exit 2**, `a prompt release needs --candidate; nothing discovers one`, no row | **exit 0**, `released v2-minimal-edit`, `candidate rate 0.545 >= 0.090`, `binding: true`, `winner: v2-minimal-edit`; `grid.env` now `MIND_PROMPT_VARIANT=v2-minimal-edit`, `MIND_PROMPT_CANDIDATE=` |
| `release --week W2` (model A/B open) | exit 0, `held`, `colony A/B lost: 0.109 vs 0.500` | identical |
| factorial leg 1: `release --week W2` | exit 0, `released m1`, `[factorial marginal, pooled over prompt]`, four cells | identical |
| factorial leg 2: `release --dimension prompt --week W2` | **exit 2**, no row — the second leg of every factorial run unreachable | **exit 0**, `held p1`, `[factorial marginal, pooled over model, up to the verdicts it was judged on]`, four cells |

**Agreement with the in-process path.** I re-ran each of those four judgings by calling
`release()` directly on an identical fixture and compared the ledger rows key by key, excluding
only `chain`, `request_id`, `ts` and the wall-clock `factorial_until_ts`:

```
prompt:    1 row  CLI vs 1 in-process -> IDENTICAL
model:     1 row  CLI vs 1 in-process -> IDENTICAL
factorial: 2 rows CLI vs 2 in-process -> IDENTICAL
```

Every field agrees: decision, version, previous, reason, arms, cells, marginals, interaction,
`short_arms`, `winner`, `verdict_window`.

### The other six duplicated rule pairs

I exercised each pair through **both** layers on the same fixture, on both commits.

| # | Rule | `ffdf591` | `f7f0373` |
|---|---|---|---|
| P1 | model dimension + `--candidate` (`_refuse_given_model_candidate`) | agree | agree |
| P2 | unknown `--candidate` (`_validate_candidate`) | agree | agree |
| P3 | `--factorial` with this dimension's A/B open | agree | agree |
| P4 | `--candidate` on a judging week | **DIVERGED** (CLI accepted and ignored; `release()` also ignored) | agree — both refuse |
| P5 | `--factorial` with nothing blocking (`_factorial_pair_fault`) | agree | agree |
| P6 | the interlock (`blocking and not factorial`) | agree on decision/reason/evals/exit | same, plus one residual (below) |
| P7 | the candidate-presence rule | **DIVERGED** (C1) | agree |

P1, P2, P3 and P5 route through one shared helper each, so they cannot diverge by construction.
P4 and P7 were the two hand-written pairs that had diverged; both now agree. **One residual in
P6**, pre-existing and unchanged by this diff: under the interlock on the **model** dimension the
CLI records `version: null` while an in-process `release()` records the Rogue-discovered tag
(`kannaka-brain-v9` in my probe). Same *class* as C1 — a duplicated rule whose two copies do not
produce the same result — but the decision, reason, `evals` and exit code are identical, nothing
reads `version` on an `unchanged` interlock row, and it reproduces byte-for-byte on the parent.
Filed as N5, not blocking.

---

## 3. THE QUIET GATE

### 3a. Half (a): the recorder. Is the proof sufficient?

`_unrecorded_release` demands three facts. I enumerated **all eight corners**:

```
winner=True  live=True  not-yet-recorded=True  -> True     <- the only True
winner=True  live=True  not-yet-recorded=False -> False
winner=True  live=False not-yet-recorded=True  -> False
winner=True  live=False not-yet-recorded=False -> False
winner=False live=True  not-yet-recorded=True  -> False
winner=False live=True  not-yet-recorded=False -> False
winner=False live=False not-yet-recorded=True  -> False
winner=False live=False not-yet-recorded=False -> False
```

**Two agreeing and the third not always yields False**, and each degrades correctly:

- *winner + live, already recorded* → `held`. Right: the ledger is not behind.
- *winner + not-recorded, live is the incumbent* → `held`. Right: `flip()`'s tag-copy failure and
  `_flip_local` both revert the live line, so nothing was applied. Verified: the prompt-flip case
  writes `held`, clears the split, and does not claim a release.
- *live + not-recorded, no winner* → `held`. This is the deliberate rollback exclusion. Verified:
  a `rollback` whose restart failed writes `evals: {}`, so `winner` is absent and no `released`
  row is manufactured for a rollback target.

The proof is sufficient *for the fact it states*. What it does not cover is which tag the row is
then written about — see N1.

The `_env_get` (raw) read rather than `_live_arm` is correct and load-bearing: I ran the
`_live_arm` mutation and it is GREEN against the model-dimension test (as the report says) and
**RED** against `test_a_broken_registry_does_not_block_the_prompt_reconcilers_repair`, which is
the case where the two readings differ. Reading through `_live_arm` would let a broken prompt
registry stop a model repair, which is I1 reintroduced through C2's door. Correctly avoided, and
correctly tested.

### 3b. Half (b): the loud `unchanged`

Reproduced end to end on both commits.

```
                                    ffdf591                       f7f0373
Sunday   won A/B, cp ok, restart x2  flip_incomplete   exit 3      same
nightly  reconcile                   held              exit 0      released v4 (prev v3)  exit 0
Sunday+1                             unchanged "…is already       unchanged "…is already released"
                                     the live model incumbent"    exit 0  -- because it is RIGHT
                                     exit 0  ...forever
LAST_RELEASED                        kannaka-brain-v3             kannaka-brain-v4
rollback                             SystemExit "the previous     rolled_back kannaka-brain-v3
                                     release was never released"  env: MIND_MODEL_ALIAS=…-v3
```

And with the nightly not yet run (the backstop):

```
ffdf591  exit 0  "kannaka-brain-v4 is already the live model incumbent"
f7f0373  exit 3  "kannaka-brain-v4 is the live model incumbent but the ledger's last model
                  release is kannaka-brain-v3: a release was applied and never recorded…"
                 evals: {"colony_ab": {...}, "unresolved": true}
```

**Rollback works again after the sequence that previously bricked it.** Confirmed above:
`rolled_back kannaka-brain-v3`, `previous kannaka-brain-v4`, `grid.env` back to v3, exit 0.

**Cannot fire spuriously.** The `released is None` case stays benign and exit 0
(mutation-confirmed in both directions). After a `rolled_back`, `_last_released` counts the
rollback row, so live == released and the branch returns above it — no false alarm. For `prompt`,
`_live_arm` only consults the registry when `MIND_PROMPT_VARIANT` is blank, which is only true
before the first prompt release, i.e. exactly when `released is None` — so a changed registry
`default` cannot trigger the loud branch either. The one remaining trigger without a gate-applied
release is a hand-set `MIND_MODEL_ALIAS`, and *something* did apply it, so exit 3 is defensible.

### 3c. Can the gate still be wrong AND silent? — Yes, in one place, and it is not new

A `rollback` whose `ollama cp` succeeded and whose relay restart failed leaves
`MIND_MODEL_ALIAS=v3` while the last `released` row is v4. Every subsequent Sunday returns
through `if cand is None or cand["tag"] == released:` — *above* the C2 check — as
`unchanged / "kannaka-brain-v4 is already released"` at **exit 0**, forever, while `status`
reports `released: kannaka-brain-v4` and the relay serves v3.

Reproduced identically on `ffdf591` and `f7f0373`, so it is **pre-existing and not a regression**,
the rollback path is untouched by this diff, and it is out of the brief's scope. It is also
milder than C2: the live version is one that *was* released, and `rollback` still resolves. Filed
as N6, follow-up.

### 3d. Can the nightly record a release that was NOT applied? — **Yes.** See N1.

---

## 4. The new exit-code channel

`UNRESOLVED = "unresolved"` has exactly **two writers** (the C2 `unchanged` branch and
`_registry_fault_row`) and **one reader** (`_exit_code`). Grepped across `grid/`, `tests/`,
`deploy/`.

- **Cannot mask another dimension's failure.** `ab --check` keeps `code = max(code, _exit_code(row))`
  and the fault row is appended to `rows`, so the "nothing needed doing" early return cannot
  swallow it. `max` only ever raises the code. Verified: model repair (`held`, 0) beside a prompt
  registry fault (3) prints **two** JSON rows and returns **3**; and with the model repair being
  the C2 `released` recording, likewise.
- **Cannot fire spuriously.** No other code path writes an `evals` key named `unresolved`
  (`_advisory_evals` nests everything under `colony_ab`; the other keys are `holdout_ppl`,
  `identity`, `validity`, `variant_registry_error`), so no historical row can carry it.
  `(row.get("evals") or {}).get(UNRESOLVED)` is None-safe.
- **Every caller still gets a defensible answer.** Four callers: `release`, `rollback`,
  `ab --abort`, `ab --check`. Rows from `rollback` and `ab --abort` cannot carry the marker
  (neither writer is on those paths), so their contract is byte-identical to before. The new
  branch returns 3, the same value the `flip_incomplete`/`: flip_failed` rules already return, so
  the exit-code alphabet is unchanged (0 or 3) and `grid-verdicts.service`'s `ExecStartPost`
  reading is unchanged.
- The marker is read as an **explicit key**, never sniffed out of the reason prose, which was the
  right call: the reason is already load-bearing twice (`lost_tags`' prefix match, `_exit_code`'s
  `": flip_failed"` suffix), and 3b's `RELAY_DOWN_NOTE` is correctly *appended* so it collides
  with neither. Verified: `down["reason"].startswith(up["reason"])` and `lost_tags` stays empty on
  a `flip_incomplete` win.

No defect found in this channel.

---

## 5. The registry failure

**The Sunday row decides nothing.** Broken registry on a prompt judging Sunday, run through the
CLI (`MIND_PROMPT_VARIANT` absent so `_live_arm` really reaches `get_variant`):

```
exit 3
decision  unchanged        version  null        previous  null
reason    the prompt variant registry did not load, so nothing could be decided in this
          dimension: prompt_variants.json: not valid JSON: Expecting ',' delimiter
evals     {"unresolved": true, "variant_registry_error": "…"}
ab_state(prompt) still open = True      weeks = 0      lost_tags(prompt) = set()
```

Parent: exit 2 with C1's usage error (C1 masked I1 on this path entirely).

- **Cannot count toward the week cap**: `unchanged` is not in `_AB_DECISIONS`, so it enters no
  episode; `weeks` stays 0. Mutations `held` (closes the episode) and `ab_inconclusive` (spends a
  week) are both RED.
- **Cannot blacklist**: `version` is `None` and the reason begins with none of `lost_tags`'
  three rejection prefixes. Defended twice — which is why the reason-wording-alone mutation
  **survives** (I ran it: GREEN) and the combined `version` + rejection-wording mutation **kills**
  (I ran it: RED). That is the correct reading, and the test says so in its own comment.

**The nightly reconciles the other dimensions regardless, and no dimension's failure hides
another's result.** Parent: `VariantError` escapes `ab --check`'s loop — the prompt dimension is
never reconciled and the model dimension's repair is written but never printed, exit replaced by
an uncaught 1. Fix: two JSON rows printed (model `held`/`released`, prompt the fault row), exit 3.
`i1-nightly-outside` (moving the handler out of the loop) is RED.

`except VariantError` only — every other exception still reaches the operator as a traceback,
which is right.

**One gap, documented here rather than found by the report** — see N4: on a *start* Sunday
(`--candidate` given), `main`'s own `_validate_candidate` runs inside `except ValueError`, and
`VariantError` **is** a `ValueError`, so a broken registry becomes `ap.error(...)` → exit 2 with
no row. Identical on both commits, so not a regression, and it is loud rather than a traceback —
but `deploy/README.md` now tells the operator to look for an `unchanged` row carrying
`"unresolved": true`, and on that path there is none.

---

## 6. The three surviving mutations

I re-ran **twenty-five** mutations myself, each applied by exact string replacement to a fresh
copy of the clean export and run under `-W error`. **Zero mismatches with the report.**

Kills (all RED): `c1-presence`, `c1-refuse-cli`, `c1-refuse-release`, `c2-no-record`, `c2-quiet`,
`c2-loud-always`, `i1-release`, `i1-nightly-outside`, `i2-wording`, `3b-discard-flip`,
`3b-discard-start`, `nc-independent`, `var-cellsep`, `var-cellsep-drift`, `var-default`.

The three declared survivors, checked rather than accepted:

| Survivor | Ran | Genuinely a weak spot? | The replacement that kills |
|---|---|---|---|
| `_live_arm` instead of raw `_env_get` in `_unrecorded_release`, vs `test_one_dimensions_registry_fault_does_not_silence_the_others_nightly` | **GREEN — survives** | **Yes, and correctly so.** `_live_arm` only consults the registry for non-model dimensions, so on the model path the two reads are the same expression. The test cannot see the difference because there is none to see. | `test_a_broken_registry_does_not_block_the_prompt_reconcilers_repair` → **RED**, verified. The added test is the right one: `MIND_PROMPT_VARIANT` absent is the *only* shape in which the two readings differ, and `_live_arm`'s own docstring says that is the ordinary state before the first prompt release. |
| the fault row's reason worded `colony A/B stuck: …` **alone**, vs `test_a_registry_that_will_not_load_records_a_row_instead_of_a_traceback` | **GREEN — survives** | **Yes.** `lost_tags` skips a row with no `version`, so the reason alone decides nothing. The assertion `lost_tags == set()` is genuinely defended twice over, and one defence alone cannot reach it. | reason-as-rejection **plus** naming the open candidate as `version` → **RED**, verified: `lost_tags` returns `{"v2-terse"}` and a variant is blacklisted for good over a JSON file. |
| main's presence rule applied to every dimension, vs `test_the_cli_judges_a_model_ab_too_and_never_reads_rogue_on_a_judging_week` | **GREEN — survives** | **Yes.** That week has `open_here` true, so `not open_here` short-circuits and the dimension test decides nothing. The `model` path was never broken by C1; the test is coverage of the seam, not of C1's fix. | `dimension = a.dimension or "prompt"` → **RED**; and `if False and state is not None:` (a judging Sunday becomes a probe day) → **RED**. Both verified. |

**No deleted assertion is being passed off as an equivalent mutation.** The whole test diff
contains exactly three deletion lines: `assert "no A/B running" in …` (the falsehood I2 replaced,
and the replacement asserts both the true line *and* the absence of the old one), one `_seed_ab_row`
signature line (a parameter was added), and one `main([...])` argv line (the meaningless
`--candidate p1` workaround, replaced by the documented command). Every other change is an
addition. Two existing tests changed; both gained assertions and lost none.

The interaction test, which is where a vacuous assertion would hurt most, I checked separately:

```
interaction-neg  vs the NEW structural test -> RED   | vs the OLD test -> RED
interaction-pos  vs the NEW structural test -> RED   | vs the OLD test -> GREEN (the filed gap)
interaction-abs  vs the NEW structural test -> RED   | vs the OLD test -> RED
```

The new test is non-vacuous: it asserts each fixture's interaction really is -0.8 / 0.0 / +0.8 and
that all four marginals are identical (rate 0.300, 120 settled) before asserting the three
verdicts and reason strings are equal, and it pins the literal expected verdict. The gap is closed.

---

## 7. Scope

Files changed: exactly the five the report names (`grid/weekly.py`, `grid/variants.py`,
`tests/test_weekly.py`, `tests/test_variants.py`, `deploy/README.md`). `git diff --name-status`
confirms no others.

**Nothing on the out-list was touched.**

- *rest-state relay-versus-configuration (I4)*: no new comparison of the running relay to
  `grid.env` anywhere in the diff.
- *readiness gate against a commented-out `MIND_LLM_URL` (I3)*: `deploy/relay-ready.sh`,
  `deploy/systemd/*` and `deploy/grid.env.example` are all unchanged — `deploy/README.md` is the
  only file under `deploy/` in the diff.
- *the remote alias never re-read (#3)*: no new `remote.run`/`remote.read` call.
- The three dropped items are untouched: the defer-reason exclusion assertion, the `remember`
  marker test, and the judged-then-closed-partner behaviour all read identically to the parent.

**The documentation edit was necessary, not opportunistic.** One hunk, at the exit-code table.
Every line of it describes a behaviour this wave *introduced or changed*: the third exit-0 line
(I2), the appended relay-down note (3b), and the two shapes of `"evals": {"unresolved": true}`
(C2 and I1). The bullet it replaces — "`0` — nothing needed doing (no A/B running…)" — is the
false statement the whole-branch review flagged as endorsing the wrong reading. I verified all
three exit-0 lines print exactly as documented, including the combined case:

```
no A/B running
not stuck
hand-started split in grid.env (prompt), untracked by the ledger; nothing to reconcile
not stuck; hand-started split in grid.env (prompt), untracked by the ledger; nothing to reconcile
hand-started split in grid.env (model, prompt), untracked by the ledger; nothing to reconcile
```

§6's judging commands were already correct and are untouched; they now work as written (§2).
One byte is wrong in the new text — see N7.

---

## 8. Regression

Twenty-two realistic scenarios, both dimensions, run identically against `ffdf591` and `f7f0373`
and diffed field by field (exit code, decision, version, previous, reason, `winner`, `short_arms`,
`remember`, `relay_after_revert`, `unresolved`, `lost_tags`, `grid.env`, printed lines).

**Identical on both commits** (13): model judge win, model judge loss (incl. `lost_tags`), model
already-released, model live-incumbent-never-released (the benign `unchanged` the C2 fix must not
make loud), rollback, prompt start, `ab --check` with nothing running, `ab --check` with a tracked
A/B open, `ab --check` reconciling a stale `flip_incomplete` with no winner, registry fault on a
start Sunday, and the shipped-registry load.

**Different, and every difference is one the brief asked for** (9):

| Scenario | Difference | Asked for by |
|---|---|---|
| prompt judge win / loss / inconclusive | exit 2 + no row → a real binding row | C1 |
| factorial second leg | exit 2 + no row → `held p1` on its marginal | C1 |
| prompt judge with `--candidate` | exit 0, judged the *other* variant → exit 2, refused | C1b |
| prompt win, flip's restart fails | exit 2 → `flip_incomplete` carrying `relay_after_revert: "ok"` | C1 + 3b |
| prompt start, relay never comes back | reason gains `; the relay did not come back and may be DOWN…`, `relay_after_revert: "down"` | 3b |
| `ab --check` with a hand-started split | `no A/B running` → the truthful line | I2 |
| nightly, one dimension's registry broken | `VariantError` escapes → two rows printed, exit 3 | I1 |
| degraded factorial row | prose derived from `_neighbours` (mutation-verified RED) | N-c |

No unrequested behaviour change found.

---

## 9. New defects, ranked

### N1 — the nightly can record a `released` row for a version that was never applied. **MERGE-BLOCKING.**

`ab_reconcile` computes `applied = _unrecorded_release(cfg, dimension, last, last_rel)` from
`last["version"]` — the standing `flip_incomplete` — but then writes the row with
`version=candidate`, where `candidate = env_candidate or last.get("version")`. When those two
differ, **the proof is about one tag and the recording is about another.**

Reproduced (`f7f0373`; the parent writes a harmless `held`):

```
ledger:  released v3 | ab_started v2 | held v2 | ab_started v4 | flip_incomplete v4 (winner v4)
grid.env: MIND_MODEL_ALIAS=kannaka-brain-v4   MIND_LLM_CANDIDATE=kannaka-brain-v2

$ grid.weekly ab --check --dimension model
  exit 0
  decision released   version kannaka-brain-v2   previous kannaka-brain-v3
  winner   kannaka-brain-v2
  reason   colony A/B reconciled: kannaka-brain-v2 was flipped live but the relay restart
           failed, so the release was applied and never recorded; recording it now (was v3)
  LAST_RELEASED(model) -> kannaka-brain-v2      LIVE -> kannaka-brain-v4
```

kannaka-brain-v2 was never live, never won, and was never flipped. The gate has written a false
`released` row into an append-only, hash-chained file, at exit 0, and `_last_released` now names
it — so `status` lies, and `rollback`'s default target becomes v2's `previous`, **skipping the
version actually in service**, which is precisely the harm the whole-branch review named under
C2. The v4 release remains unrecorded.

Preconditions: a standing C2-shaped `flip_incomplete`, plus `MIND_LLM_CANDIDATE` naming a tag that
has an `ab_started` row (so the hand-start exemption does not fire). Hand-starting a split by
editing `grid.env` is a documented, exercised flow with its own receipt, and re-testing a tag the
gate has A/B'd before is the natural way an operator reaches this. The conjunction is not the
mainline, but the failure is unrecoverable and it is the exact question the brief asked.

Fix: when `applied`, record `last["version"]` (the tag the proof is about), or refuse to record
when `env_candidate` names a different tag. Roughly one line, plus a test that a hand-set
candidate beside an unrecorded flip does not record a release for it.

### N2 — the third N-g docstring was not fixed, and the report says it was. **MERGE-BLOCKING (trivial to fix).**

`tests/test_weekly.py:2873`, `_judge`, is **byte-identical to the parent**:

> `"""Judge one leg. `week` is load-bearing: the partner's judging row bounds this leg only when
> that row belongs to the SAME judging cycle, so a fixture that judges two legs must give them
> one week, and a two-Sunday fixture must give them different ones."""`

That is the deleted ISO-week rule verbatim, in the helper every factorial test calls, and it is
false: no code in `grid/` reads `row["week"]` except one `log.info` format argument; the bound is
`judgings` counts and the partner's `verdict_window`. The report claims all three were fixed and
that a follow-up `grep` for `ISO week`, `calendar`, `judging cycle`, `belongs to` and `` `week` ``
found no others — the terms "judging cycle" and "belongs to" both occur in the line it missed, so
the re-sweep did not happen as described. Blocking not for the docstring's size but because the
brief named it, the report asserts it done, and N-g exists precisely because a docstring
describing a deleted rule is how the rule comes back.

### N3 — a repair whose own restart fails loses the recording permanently, and the row contradicts itself. **IMPORTANT; take it in the same touch.**

If `end_ab`'s restart fails on the night the reconciler first proves the unrecorded release, it
writes `flip_incomplete` with `winner: None` — and the next night `_unrecorded_release` reads
*that* row as `last`, finds no winner, and returns False for ever after. Reproduced:

```
night 1 (relay still down)   exit 3  flip_incomplete  winner None
        reason: "…the release was applied and never recorded; recording it now (was v3);
                 relay restart failed clearing the A/B"        <- records nothing
night 2 (relay back)         exit 0  held  "…the last decision for v4 was flip_incomplete;
                                            candidate cleared and the relay restarted"
night 3                      exit 0  LAST_RELEASED still kannaka-brain-v3, LIVE v4
Sunday                       exit 3  (the backstop fires, correctly)
rollback                     SystemExit "the previous release was never released; refusing"
```

The C2 state exists *because* the relay would not restart, so a second failure on the first
reconcile attempt is the likely continuation, not an exotic one. Two problems: the report's claim
"the next night retries" is false, and the row says "recording it now" while recording nothing —
the same self-contradicting-row defect this wave fixed as N-c/I5, reintroduced three functions
away. Not silent (the Sunday backstop still exits 3 with the marker), so the brief's minimum bar
holds — which is why this is Important and not blocking.

### N4 — a registry fault on a *start* Sunday still writes no row. **MINOR, non-blocking, not a regression.**

`main`'s own `_validate_candidate` sits inside `except ValueError → ap.error`, and `VariantError`
is a `ValueError`, so `release --dimension prompt --candidate v2-terse` with a broken registry
exits **2** with a usage error and no ledger row, on both commits. Loud, so no traceback and no
silence — but `deploy/README.md` now directs the operator to an `unchanged` row carrying
`"unresolved": true` that does not exist on this path. Either widen the wrap or say so in the
README.

### N5 — the interlock pair records a different `version` in each layer. **MINOR, pre-existing.**

Under the interlock on the model dimension the CLI records `version: null` while `release()`
records the Rogue-discovered tag. Same class as C1; consequence is only a less informative row.
Reproduces on the parent.

### N6 — a rollback whose relay restart failed leaves the gate wrong and quiet. **MINOR, pre-existing, out of scope.**

`LAST_RELEASED=v4` while `LIVE=v3`, and every subsequent Sunday returns through
`cand["tag"] == released` — above the new C2 check — at exit 0 with `"…is already released"`.
Reproduces identically on the parent. Worth a board row beside I4 and #3.

### N7 — the README quotes the relay-down note with the wrong dash. **TRIVIAL.**

`deploy/README.md:324` writes ``the relay did not come back and may be DOWN — restart
grid-odin.service`` with U+2014, while `RELAY_DOWN_NOTE` emits `--`. An operator grepping the
ledger for the documented literal will not find it.

### N8 — two top-level test functions with no blank line between them. **TRIVIAL.**

`tests/test_weekly.py:4580-4581`: `test_one_dimensions_registry_fault_does_not_silence_the_others_nightly`
ends and `test_a_broken_registry_does_not_block_the_prompt_reconcilers_repair` begins on the next
line. No linter is configured, so nothing fails; it is the only formatting slip in 581 added lines.

---

## 10. Ready to merge?

**No** — but only just. N1 and N2 are both small and both sit inside what this wave changed:

1. **N1** — record `last["version"]`, not `candidate`, when the release is proven applied (or
   refuse to record when the two disagree), plus one test. ~1 line.
2. **N2** — fix the third N-g docstring in `tests/test_weekly.py:_judge`. Doc-only.
3. **N3** — worth taking in the same touch: stop the reason claiming "recording it now" on a row
   that records nothing, and keep the winner marker alive so the next night can retry.

Everything else is verified. C1 is closed and proved through the real command line in both
dimensions, ordinary and factorial, with the CLI and in-process rows identical field for field;
all seven duplicated rule pairs now agree; the loud-`unchanged` backstop works and cannot fire
spuriously; rollback works again after the sequence that bricked it; the registry fault records a
row that decides nothing in either verb; the exit-code channel has two writers, one reader, and
cannot mask or invent a failure; and twenty-five mutations plus three interaction-sign probes
reproduce the implementer's results exactly, survivors included.

---

## Confirmation pass

Scope: N1, N2, N3 only, plus the two risks the implementer raised and the mutation accounting.
FIX = `2b8b0b2`, parent `f7f0373`. Verified against `git archive 2b8b0b2` extracted to a scratch
tree (byte-identical to the worktree bar `__pycache__`), run with the worktree venv and
`PYTHONPATH` shadowing the editable install. No tracked source or test file was modified.

**Suite.** `461 passed, 6 skipped in 145.26s`. The report says `463 passed, 4 skipped`. The delta
is exactly the two `thegrid submodule not checked out` skips — `git archive` does not carry
submodule content — so 461+2 = 463 and 6-2 = 4. The report's number is right for the worktree.

### N1 — CONFIRMED FIXED

*Proof and row now concern the same version.* `recorded = last["version"] if applied else None`,
and every version-naming field on the `applied` path is `recorded`: `version=recorded if applied
else candidate`, `evals.colony_ab.winner = recorded`, and the reason's subject. `recorded` is the
tag `_unrecorded_release` proved its three facts of. `candidate` no longer reaches any field that
names the release.

*Original reproduction re-run.* The reviewer's hand-set-candidate fixture, executed against
`f7f0373`, still fabricates:

```
{"decision": "released", "version": "kannaka-brain-v2", "previous": "kannaka-brain-v3",
 "evals": {"colony_ab": {"candidate": "kannaka-brain-v2", "winner": "kannaka-brain-v2", ...}},
 "reason": "colony A/B reconciled: kannaka-brain-v2 was flipped live but the relay restart
            failed, so the release was applied and never recorded; recording it now (was
            kannaka-brain-v3)"}
```

exit 0, `_last_released` -> v2, relay serving v4. On `2b8b0b2` the same fixture records
`released kannaka-brain-v4`, `winner v4`, `previous v3`, `_last_released` -> v4, and no `released`
row anywhere names v2. It no longer fabricates.

*Pushed further — any remaining input that records `released` for a version not applied?* I found
none. `released` is written only under `applied and outcome == "ok"`; `applied` is
`_unrecorded_release`, whose fact 2 is `_env_get(grid.env, keys["live"]) == version`, read raw.
The paths that could break the tie were checked and hold:

- `end_ab` runs between the proof and the write, but only does `_env_set(candidate, "")` plus
  `_restart_relay`. It never touches the live line, so `recorded` cannot go stale in between.
- `flip()` sets `MIND_MODEL_ALIAS` *before* `ollama cp` but reverts both lines on cp failure, and
  `_flip_local` reverts both lines on restart failure — so a live line equal to a winner implies
  the apply step succeeded. A kill between the env set and the cp leaves no `flip_incomplete` row
  at all (the row is written after `flip` returns), so fact 1 fails and nothing is recorded.
- The N3 carry-forward does not open a new route: an operator who hand-reverts the alias between
  nights fails fact 2 on the next run; a later real `released`/`rolled_back` row fails fact 3; a
  new `ab_started` makes `state` non-None and the reconciler returns early.
- Verified empirically that the marker cannot be laundered into a wrong recording across five
  consecutive failed nights (probe under N3).

One un-guarded shape exists on this path, and it is **not** a fabrication: when the dimension has
no prior `released`/`rolled_back` row at all, `last_rel` is `None` and the reason f-string
dereferences `last_rel['version']` -> `TypeError: 'NoneType' object is not subscriptable`
(`grid/weekly.py:2111`). Reproduced. It is **pre-existing** — `f7f0373` crashes identically at its
line 2098 — and the row two lines below already guards it (`previous=last_rel["version"] if
last_rel else None`), so the omission is inconsistent rather than intended. It is reachable on a
dimension's first-ever release whose relay restart failed. It fails loudly with a non-zero exit
and writes nothing, so it is the opposite of the N1 failure mode. Not a blocker for this touch;
worth a `(last_rel or {}).get('version')` when someone is next in the function.

### N2 — CONFIRMED FIXED

`tests/test_weekly.py:_judge` no longer states the deleted rule. It now says `week` "is only the
row's label here, and NOTHING reads it", quotes the old sentence explicitly to disown it, names
the real mechanism (the two episodes' `judgings` counts via `_judge_ab`, bound by the partner's
`ts` and `verdict_window`), and points at
`test_the_bound_survives_the_calendar_rolling_over_between_the_two_legs`.

The report now quotes search output instead of asserting a sweep. I re-ran its command verbatim
and got **39 hits**, the count it claims. Every prose hit describes the rule as deleted or wrong:
`grid/weekly.py:926-927` ("that proxy was deleted"), `:1069` ("it was the deleted ISO-week proxy,
and it was wrong"), `:1566,1573-1578` ("An earlier build keyed this on the ISO week instead. That
was a proxy... and a wrong one"), `tests/test_weekly.py:2876-2882` (the fixed docstring),
`:3913-3954` (the test that proves the calendar is gone), `deploy/README.md:482-488` (the history
of both wrong attempts). The remainder are mechanical `_iso_week()` call sites — the row's `week`
*label*, a separate and still-live use — plus `grid/versions.py:100` and
`tests/test_versions.py:111` about `request_id` dedup. **I agree with the reviewer: the remaining
mentions explain why the rule was removed rather than asserting it.**

Two nits on the report's own presentation, neither substantive: the 18 quoted lines are
transcribed and annotated (wrapped, elided with `...`, one line carrying an inline gloss) rather
than verbatim grep output; and "exactly these 18 lines" is two short — `grid/weekly.py:1069` and
`tests/test_weekly.py:3914` are also about the bound and are absent from the list. Both omitted
lines disown the rule, so the substantive claim survives intact.

### N3 — CONFIRMED FIXED

On `f7f0373` the failed-restart repair wrote `"winner": null` while its reason said "recording it
now" — reproduced, exact row captured. On `2b8b0b2` the row is `flip_incomplete`, says `; the
release is still NOT recorded, and is retried on the next reconcile`, does not say "recording it
now", and carries `version: kannaka-brain-v4` and `winner: kannaka-brain-v4` forward.

The retry genuinely happens on a later run, and not only the next one. Probe (scratch only, not
committed): five consecutive nights with `_local_run -> 1`, then one with `-> 0`. Every failed
night returns exit 3, writes `flip_incomplete` with the marker intact, and leaves `_last_released`
at v3; the sixth night records `released kannaka-brain-v4` and `_last_released` catches up.
Passed. The marker survives repeated self-appending because each night's row is itself a valid
`flip_incomplete` + `winner` pair, and fact 2 is re-proved against the live env line every time.

### The two-facts row — right shape, will not be misread

The divergent-tag row carries the release facts (`version`, `evals.colony_ab.winner`, `previous`,
and the reason's subject) all as v4, and the cleared split appears in exactly two places: the
audit-only `evals.colony_ab.candidate` field, and prose in `reason`. That is the correct split,
because **no machine reader ever interprets a closing row's `candidate` field**: `_episode_state`
takes `candidate` from `episode["started"]` — the `ab_started` row — only, and `ab_state`,
`_partner_bound`, `_overlapped` and the staleness bound all derive from `_ab_episodes`. Grepped
the tree: `colony_ab` has no reader outside `grid/weekly.py` (only two prose mentions in
`deploy/README.md`). Additionally the divergent row can only be written when no episode is open —
the `state is not None` branch leaves `last = None`, so `applied` is False there and `recorded` is
`None`. So the second fact is inert to every consumer and legible to a human. Merging the two
facts would have been worse; splitting them across two rows would have put an un-decided row into
a hash chain. This is the right shape. Nit only: a human skimming raw JSON sees `"candidate":
"kannaka-brain-v2"` beside `"decision": "released"`, which reads oddly until the adjacent
`"winner": "kannaka-brain-v4"` and the reason resolve it; a distinct key (`cleared_split`) would
read better, but renaming a ledger key is a schema change and does not belong in a fix wave.

### The N1/N3 coupling — genuinely defended, mutation dies

The claimed guard is real. I ran the coupling directly rather than trusting the report: revert N1
entirely to the parent's shape (`version=candidate` **and** `"winner": candidate if applied and
outcome == "ok" else None`) while keeping N3's decision/reason branch. Result: **2 failed, 167
passed** — and the two failures are
`test_the_repair_records_the_version_it_proved_not_the_one_grid_env_is_splitting_to` **and**
`test_a_repair_that_could_not_restart_the_relay_says_so_and_is_retried`. Reverting N1 alone does
not silently re-break N3; it breaks N3's own test loudly. The narrower `n3-burn-marker` mutation
(`"winner": recorded if outcome == "ok" else None`) independently kills the N3 test on its own.
The coupling is not undefended.

### Mutations — all real kills, re-runs genuine

All five new mutations re-run by me against `2b8b0b2`, full `tests/test_weekly.py` each time:

| Mutation | Result | Test killed |
|---|---|---|
| `n1-row-about-candidate` (`version=candidate`) | RED, 1 failed / 168 passed | `..._records_the_version_it_proved...` |
| `n1-winner-about-candidate` | RED, 1 failed / 168 passed | `..._records_the_version_it_proved...` |
| `n1-no-split-note` (drop `candidate != recorded`) | RED, 1 failed / 168 passed | `..._records_the_version_it_proved...` |
| `n3-burn-marker` | RED, 1 failed / 168 passed | `..._could_not_restart_the_relay_says_so...` |
| `n3-claims-recording` | RED, 1 failed / 168 passed | `..._could_not_restart_the_relay_says_so...` |
| `coupling-revert-n1` (mine, not claimed) | RED, 2 failed / 167 passed | both of the above |

Each kills a *specific, correct* test rather than collapsing the suite. Real kills.

On the six earlier mutations being re-run rather than carried forward: the strongest available
evidence is that `c2-no-record`'s result **changed** — the previous report recorded `2 failed`,
this one records `4 failed`, which is only possible against the new code. I re-ran it myself:
**4 failed, 165 passed**, and the four are
`test_the_nightly_records_a_release_that_was_applied_and_never_written_down`,
`test_one_dimensions_registry_fault_does_not_silence_the_others_nightly` and the two new tests —
exactly the report's explanation. I also re-ran `c2-live-arm` (the other mutation whose target
this touch sits on): **1 failed, 168 passed**, killing
`test_a_broken_registry_does_not_block_the_prompt_reconcilers_repair`, as claimed. Both match. The
remaining four (`c2-quiet`, `c2-loud-always`, `3b-discard`, `3b-discard-start`) target functions
this commit did not modify; their reported counts moved from `passed` to `deselected` totals
consistent with a `-k` run, and I did not re-derive them.

### N7 / N8

N7 confirmed: `deploy/README.md:324` now quotes `the relay did not come back and may be DOWN --
restart grid-odin.service`, byte-matching `RELAY_DOWN_NOTE` at `grid/weekly.py:1366`, with "(two
hyphens, as the gate writes it)" beside it. N8 confirmed in the diff.

### New finding (1) — stale uniqueness claim in `_unrecorded_release`'s docstring

**`grid/weekly.py:2155-2156`**, made stale by this commit:

```
Nothing else in the module sets `winner`: a reconciler row, an abort
snapshot, a stuck row and every start-path failure all carry `winner: None`;
```

This is now false, and false *because of N3*. On the parent, a reconciler row carrying a winner
was always `released` (`candidate if applied and outcome == "ok" else None`), so it could never
satisfy fact 1 (`decision == "flip_incomplete"`). The N3 fix deliberately makes the reconciler
write a `flip_incomplete` row **carrying a winner** — the first row shape in the module that is
both reconciler-authored and accepted by this very predicate as fact 1 + fact 3 evidence.

The behaviour is correct: the carry-forward only re-states a proof this same function already
established, and fact 2 (the live `grid.env` line) is re-proved independently on every run, so the
marker alone cannot authorise a recording. The write site's own inline comment says so. But this
docstring is the audit trail for the one write in the module that a hash chain makes
un-correctable, and it now tells a future auditor that the `flip_incomplete` + `winner` shape can
only come from a real flip. That is the same defect class the wave just blocked N2 for — prose
stating a rule the code no longer follows — reintroduced one commit later, on more dangerous
prose.

**Merge call: not blocking.** No behavioural risk, fully tested, mutation-defended. But it is a
two-line docstring amendment with zero risk, and the wave's own N2 standard argues for making it
in this commit rather than deferring: add the reconciler's retry row as a fourth, deliberate
`winner` source and note that fact 2 is what keeps it honest. No re-review needed afterwards.

### Verdict

N1, N2 and N3 are fixed. The two risks the implementer raised are both resolved in its favour: the
two-facts row is the right shape and is inert to every machine reader, and the N1/N3 coupling is
defended by mutations that genuinely die. The mutation accounting is honest, including the one
number that changed. One new non-blocking documentation finding above.

**Ready to merge: Yes**, with the one-line docstring fix at `grid/weekly.py:2155` recommended
first.
