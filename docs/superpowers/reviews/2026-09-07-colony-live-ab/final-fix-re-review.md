# Scoped re-review — final-fix wave `2e4eada..b99bdcc` (branch `feat/ab`)

Reviewer: adversarial re-review of the fix wave only. The pre-existing feature is not re-reviewed;
pre-existing code the fixes newly depend on (`flip`, `end_ab`, `lost_tags`, `verdict_row`,
`ProposalLog`, thegrid's `odin_operator`) is.

**Verification actually run** (nothing in the worktree was modified):

- Full suite in the worktree: `.venv/Scripts/python.exe -m pytest tests -q -W error`
  → `258 passed, 4 skipped in 19.37s`. The report's line reproduces.
- All mutation checks below were run against a **copy of the tree** in the session scratchpad
  (`scratchpad/mut`), never against the worktree. Baseline there: `test_odin.py` 28 passed,
  `test_weekly.py` 44 passed. (The 9 failures in that copy are all `test_deploy_units.py` /
  `test_identity.py`, which read `deploy/`, not copied — irrelevant to this review.)

---

## 1. Per-finding verdict

| Finding | Verdict |
| --- | --- |
| C1 empty `MIND_AB_SPLIT=` kills the relay | **ADDRESSED** |
| C2 won A/B whose tag copy fails leaves env+relay splitting | **ADDRESSED** |
| C3 §8 recovery cannot fire when a candidate ask wedges the relay | **ADDRESSED** (with a new side effect, N2 below) |
| I1 `ab --check` loud on `flip_incomplete` | **ADDRESSED** (narrowly — see N4) |
| I2 relabelled-incumbent guard | **ADDRESSED for the misjudgement; NOT for the lifecycle** (R1) |
| I3 only a candidate-alone shortfall is remembered | **ADDRESSED for `inconclusive`; contradicted by C3's new `stuck` branch** (N2) |
| I4 `grid.weekly ab` shows an env-only A/B | **ADDRESSED** |
| Minor: `ab_arms` uses `e.get("request_id")` | **ADDRESSED** (incomplete — N5) |
| Minor: `--abort` records an `ab_arms` snapshot | **ADDRESSED** |
| Minor: README `flip_incomplete` / `--forget` wording / `grid-odin` sudoers line | **ADDRESSED** |
| Minor: `main()` `--check` computes `ab_state` once | **ADDRESSED**, no behaviour change (§4) |
| Minor: `_env_get` strips an inline ` # comment` | **ADDRESSED as written, but the change is wrong** (N3) |
| Minor: `ab_state` docstring names `flip_incomplete` | **ADDRESSED** |
| Minor: tolerance-boundary test exercises the epsilon | **ADDRESSED** |

### C1 — walking the failure

Old failure: `start_ab`'s restart-failure revert wrote `MIND_AB_SPLIT=` when the key was absent
before. systemd's `EnvironmentFile` sets the variable to `""`; `os.environ.get("MIND_AB_SPLIT",
"0.2")` returns `""`, not the default; `float("")` raises `ValueError` inside
`RelayConfig.from_env`, before any handler exists → grid-odin crash-loops into its `StartLimit`
and stays dead.

Both ends are now closed, and either alone would suffice:

1. `grid/odin.py:243-248` — every numeric read the gate can blank is `float(env(X) or default)`:
   `ODIN_POLL_S`, `MIND_ASK_TIMEOUT_S`, `ODIN_RETRY_S`, `MIND_AB_SPLIT`. `"" or 0.2` → `0.2`.
2. `grid/weekly.py:386-391` — `_env_unset` removes the line when `before_split is None`, instead
   of writing an empty one.

Can it still happen? The only remaining way to get a `float("")` at boot is a *hand-edited*
`MIND_AB_SPLIT=` line, and fix (1) absorbs that too. **No.**

Mutation confirmed: restoring `ab_split=float(env("MIND_AB_SPLIT", str(AB_SPLIT)))` →
`test_config_from_env_tolerates_blank_numeric_lines` FAILED with `ValueError` at `odin.py:248`.

### C2 — walking the failure

Old failure: on a `win` whose `ollama cp` failed, `flip()` restored `MIND_LLM_CANDIDATE` to the
candidate tag on its way out (`weekly.py:333-334`) and never restarted the relay. The row was
`held: flip failed: tag copy`, which closes `ab_state`. Result: `grid.env` and the running relay
both still split 20% to the candidate, while the ledger says no A/B. The nightly `ab --check` sees
`ab_state is None` → "no A/B running"; next Sunday `release()` takes the non-A/B branch, re-probes,
and starts a *fresh* A/B — discarding the week that had already won.

Now (`weekly.py:461-473`): `end = end_ab(cfg, ...)` runs after `flip()` returns `flip_failed`.
`end_ab` sets `MIND_LLM_CANDIDATE=""` and restarts grid-odin. Post-state: `MIND_MODEL_ALIAS`
restored to the incumbent by `flip`, `MIND_LLM_CANDIDATE=` empty, relay restarted → `candidate_tag
== ""` → no A/B. Ledger row `held`. Env, relay and ledger all agree. The reason
`colony A/B won but the tag copy failed: …` starts with none of `colony A/B lost` / `colony A/B
stuck` / `colony A/B inconclusive`, so `lost_tags` does not remember it and next Sunday retries the
copy — correct, a win that could not be applied is not a rejection. The added
`end == "ok"` / else `flip_incomplete` branch (beyond the brief's literal wording) is right and is
covered.

Can it still happen? **No** for the `flip_failed` path. The sibling `flip_incomplete` path (both
restart attempts failed after a *successful* copy) still leaves the relay serving the old alias
while `grid.env` names the new one — that is by design, recorded as `flip_incomplete`, and
`main()` exits 3 for it (`weekly.py:742`). See N4 for the part of that which is still quiet.

Mutation confirmed: collapsing the branch to `decision, why = "held", "flip failed: tag copy"` →
`test_ab_win_whose_tag_copy_fails_ends_the_ab_and_is_not_remembered` FAILED.

### C3 — walking the failure

Old failure: thegrid's `OdinOperator` writes `request.json` only `if not request.exists()`
(`thegrid/src/colony/odin_operator.py:71-85`) — one slot. A request that hashes to a wedged
candidate arm deferred forever (`_retry_not_before` only, no cap), so the slot was never freed, the
incumbent never got a turn, `arms[incumbent]["settled"]` stayed 0, and `is_stuck`'s
`>= STUCK_INCUMBENT_MIN (50)` clause could never trip. §8 recovery was unreachable; the A/B ran
until an operator noticed.

Two independent fixes, either of which breaks the deadlock:

1. `odin.py:151-169` — `DEFER_LIMIT = 4` consecutive defers per `request_id`, then
   `request_path.unlink()`, a `rejected` proposal-log row attributed to the failing arm, and an
   `odin.rejected` publish. `_log` before `_publish`, so a dead bus cannot cost the record. The
   slot is freed; thegrid files a fresh request whose id is a fresh
   `sha256(time_ns + repr(names))[:16]`, which re-rolls `arm_for` — ~80% incumbent at `split=0.2`.
2. `weekly.py:334-340` — `is_stuck` also true when 48 h have passed and **both** arms are 0.
   `ab_check` records `colony A/B stuck: no proposals from either arm in 48h`.

Can it still happen? **No.** Even if fix (1) never fired (e.g. the ask hangs longer than the
weekly cadence), fix (2) reaches the same recovery within 48 h.

Mutations confirmed: `if seen < DEFER_LIMIT:` → `if True:` →
`test_a_wedged_candidate_ask_is_binned_after_the_defer_limit` FAILED. Dropping
`self._defers.pop(...)` → `test_a_successful_ask_resets_the_defer_count` FAILED. (Note the first
mutation leaves the reset test green — correctly, since they cover different lines.)

### I2 — partially

The misjudgement the brief names *is* closed: `weekly.py:432` in `release()` and `weekly.py:566`
in `ab_check()` both compare `_env_get(cfg.grid_env, "MIND_MODEL_ALIAS")` against
`state["incumbent"]` and refuse to act on a mismatch. Mutation confirmed:
`if env_incumbent != state["incumbent"]:` → `if False:` →
`test_ab_week_refuses_to_judge_a_relabelled_incumbent` FAILED (`released`, i.e. a losing model
shipped). But the *lifecycle* is broken by the same guard — see R1.

### I3 — addressed, then re-opened by C3

`short_arms()` (`weekly.py:325-330`) + `evals.colony_ab.short_arms` + the split prefix match in
`lost_tags` (`weekly.py:296-302`) do exactly what the brief asks for the `inconclusive` case, and
the missing-field default (`short is None` → treat as candidate-short) is the conservative reading
that keeps the livelock guard. Ordering is `(candidate, incumbent)`, so candidate-alone is
`[tag]` and incumbent-alone is `[incumbent] != [tag]` → not remembered, which is also right.

But `lost_tags` still remembers `colony A/B stuck` **unconditionally**, and C3 just created a
both-arms-zero stuck path. See N2 — the two fixes in this same wave contradict each other.

Mutations confirmed: restoring the bare 3-tuple prefix →
`test_lost_tags_remembers_only_a_candidate_alone_shortfall` FAILED; dropping `short_arms` from the
verdict block → `test_ab_inconclusive_continues_then_holds_at_max_weeks` FAILED.

---

## 2. Are the new tests non-vacuous?

Yes. I did not trust the report's table — I applied 8 mutations for real in the scratchpad copy and
ran the named tests. Every one went RED, and the suite went green again on restore
(`test_odin.py` 28 passed, `test_weekly.py` 44 passed).

The three most load-bearing:

| Mutation applied | Test | Result |
| --- | --- | --- |
| `ab_split=float(env("MIND_AB_SPLIT", str(AB_SPLIT)))` | `test_config_from_env_tolerates_blank_numeric_lines` | RED — `ValueError` at `odin.py:248` |
| win branch → `decision, why = "held", "flip failed: tag copy"` | `test_ab_win_whose_tag_copy_fails_ends_the_ab_and_is_not_remembered` | RED |
| `if seen < DEFER_LIMIT:` → `if True:` | `test_a_wedged_candidate_ask_is_binned_after_the_defer_limit` | RED |

Also verified RED: dropping `_defers.pop` (`..._resets_the_defer_count`); the I2 guard →
`if False:` (`..._refuses_to_judge_a_relabelled_incumbent`); the bare `colony A/B inconclusive`
prefix (`test_lost_tags_remembers_only_a_candidate_alone_shortfall`); dropping `short_arms` from
the block (`..._holds_at_max_weeks`); `- 1e-12` → nothing (`test_ab_decide_boundaries`); the
`--check` `return 0` (`..._exits_three_when_the_relay_restart_failed`).

On the epsilon: the report's own admission is correct and honest — `0.25*(1-0.10)` and `0.225` are
bit-identical, so the original boundary line never touched the epsilon. The added
`42/108 * 0.9` vs `35/100` pair does, and the mutation proves it. Good.

The three assertions I'd call weakest (still not vacuous, just thin):
`test_ab_win_whose_tag_copy_and_restart_both_fail_is_flip_incomplete` asserts only decision +
two substrings and never checks env; `test_ab_cli_shows_an_env_only_ab` asserts `ab["note"]` is
truthy rather than its content; `test_ab_check_skips_a_relabelled_incumbent` is the test that
*locks in* R1's forever-loop without noticing it.

---

## 3. NEW defects introduced by the fixes

### N1 (Critical) — R1: a relabelled A/B never ends. See §5 for the full trace and fix.

### N2 (Important) — C3's both-arms-zero stuck path blacklists the candidate, contradicting I3

`ab_check` now records `colony A/B stuck: no proposals from either arm in 48h`, and `lost_tags`
matches the `colony A/B stuck` prefix **unconditionally** (`weekly.py:299-300`), with no
`short_arms` in that block at all. So a 48 h colony-wide outage — grid-odin down, debain2's
gateway down, the LiteLLM key exhausted, the whole relay wedged — blacklists the candidate tag
until an operator runs `ab --forget`.

That is exactly the fault I3 exists to prevent, stated in `short_arms`' own docstring: *"both arms
short is a colony throughput fault … and must not blacklist a model that never got its turn."*
Before this wave the case was unreachable (`is_stuck` required incumbent ≥ 50), so C3 created it.

Reproduced in the scratchpad against unmodified source:

```
reason: colony A/B stuck: no proposals from either arm in 48h
short_arms in block? False
lost_tags: {'kannaka-brain-v4'}          # blacklisted for a colony outage
```

Minimal fix: in `ab_check`, when `incumbent_settled == 0`, put
`"short_arms": [state["candidate"], state["incumbent"]]` into `block` and give `lost_tags`'
`colony A/B stuck` branch the same `short_arms` consultation the `inconclusive` branch already has.
(The candidate-0/incumbent-≥50 case keeps `short_arms: [candidate]` and stays remembered, which is
correct — that arm demonstrably could not produce.)

### N3 (Important) — the `_env_get` comment strip creates the very gate↔relay divergence I2 detects, and I2 can no longer see it

`_env_get` now returns `line.split("=",1)[1].split(" #",1)[0].strip()`. `RelayConfig.from_env`
reads `os.environ` and does **not** strip. The docstring justifies the change by asserting
*"systemd's EnvironmentFile does not honour trailing comments … the relay then reads it
literally."* If that premise is true, then with a trailing comment on `MIND_MODEL_ALIAS`:

- the gate sees `kannaka-brain-v3`;
- the relay's `model_alias`, and therefore every proposal-log row, is `kannaka-brain-v3 # …`;
- I2's guard compares gate-value to `state["incumbent"]`, which was *also* set from `_env_get`
  (`weekly.py:531`: `incumbent = _env_get(cfg.grid_env, "MIND_MODEL_ALIAS") or released`) — so
  stripped == stripped, the guard passes;
- `ab_arms` counts the incumbent arm under the stripped label → **0 settled** → the phantom-zero
  I2 was written to catch, now silently un-caught. A losing candidate clears the tolerance floor
  against it, or the week reads inconclusive/stuck.

Verified in the scratchpad: `_env_get` → `'kannaka-brain-v3'`, relay view
`'kannaka-brain-v3 # the incumbent'`.

Worse, the premise and the repo contradict each other: `deploy/grid.env.example:16` ships

```
ODIN_RETRY_S=60          # 900 with a CPU model on the direct route (see README §1a)
```

and `float("60          # 900 with a CPU model …")` raises `ValueError` — the exact C1
crash-loop, from the shipped example file. Either systemd strips (and the `_env_get` change is
unnecessary and only introduces the divergence above and silent comment-destruction on every
revert), or it does not (and `grid.env.example` is a fatal landmine and `RelayConfig.from_env`
needs the same strip). Both cannot be true, and the wave asserts one while shipping the other.

Also: because `_env_set` rewrites the whole line, every revert path (`flip`'s restore, `start_ab`'s
revert) now silently *deletes* the operator's inline comment rather than round-tripping it —
previously it round-tripped faithfully.

Minimal fix: pick one and make both readers agree. Either (a) drop the strip from `_env_get` and
let I2's guard catch a commented line loudly — the safest, since gate and relay then read
identically by construction — or (b) apply the same strip in `RelayConfig.from_env` and delete the
comment from `grid.env.example`. (a) is smaller and is what I'd do.

### N4 (Important) — a standing `flip_incomplete` is announced once and never again

I1 makes `ab --check` exit 3 only when `ab_check` *itself* records `flip_incomplete` this run.
Every `flip_incomplete` row closes `ab_state` (`weekly.py:259`), so on the **next** night
`ab --check` takes the `state is None` branch → prints `no A/B running` → exits 0, forever — while
`grid.env` still says one thing and the live relay another. That includes the loudest case: a
Sunday `release()` win where both restart attempts failed after a successful `ollama cp`
(`alias serves the candidate; relay restart failed twice`). `main()` exits 3 for that release run
(`weekly.py:742`, good), but if that one failed unit is missed or reset, nothing re-detects it.

This is the structural reason this bug class has now appeared three times: every fix makes one more
*transition* report the divergence; nothing ever detects an *existing* one. The class closes only
with a reconciliation check — `ab --check` comparing `grid.env` against what the live relay
actually loaded (an `odin.started` envelope carrying `model_alias`/`candidate_tag`, or
`systemctl show -p ExecMainStartTimestamp grid-odin` vs `grid.env`'s mtime) and exiting non-zero on
a mismatch regardless of ledger state.

### N5 (Minor) — the `e.get("request_id")` minor fix was applied to one of two identical lines

`weekly.py:201` is guarded; `grid/verdict_corpus.py:118` is the same
`{e["request_id"]: e for e in ProposalLog(vdir).read_all()}` and is **not**. The same malformed
proposal-log line that the new test appends would `KeyError` the corpus export.

### N6 (Minor) — `ab --abort` and `rollback` still exit 0 on `flip_incomplete`

`main()` returns 3 for `release` and for `ab --check`, but the `--abort` branch (`weekly.py:709`)
and the `rollback` branch (`weekly.py:739`) `return 0` even when the row is `flip_incomplete`.
Both are hand-run so the operator sees the JSON, but the inconsistency will bite whoever wires
either into a script.

### N7 (Minor) — `_env_ab_view` with no `MIND_MODEL_ALIAS` folds unattributed rows into the incumbent

`incumbent = _env_get(...) or ""` (`weekly.py:753`), then `ab_arms(..., incumbent="")`. `ab_arms`
does `acc.get(str(row.get("model_alias", "")))`, so every verdict row with a missing/empty
`model_alias` lands in the `""` arm and is reported as the incumbent's. Display and `--abort`
snapshot only — no decision reads it — but it is a misleading number in the one view an operator
consults when they cannot tell what is running.

### N8 (Minor) — `_defers` leaks like `_retry_not_before`

`_defers` is keyed by `request_id`, which thegrid generates as
`sha256(time_ns + repr(names))[:16]` — unique per request, so **no misattribution across
requests** and no cross-request leak of the counter's meaning. It is popped on success and on the
bin path. But a request that defers 1–3 times and is then abandoned (thegrid replaced it, an
operator deleted it, the relay restarted the sequence) leaves its entry forever. Same unbounded
growth as the pre-existing `_retry_not_before`, which this wave does not fix either (it is popped
only on the bin path, never on success). Tiny per entry; a long-lived relay accumulates one per
abandoned request. Worth a `dict` prune keyed on the current request id, not urgent.

---

## 4. The specific interactions I was asked to check

**`DEFER_LIMIT` — does deleting `request.json` race the colony?** No. `OdinOperator` writes the
request atomically (`temporary.write_text` → `os.replace`) and only `if not request.exists()`; it
never re-reads the request's content, and its only other touch is
`request.unlink(missing_ok=True)` after consuming a proposal. Worst interleaving is the colony's
existence check passing just before the relay's unlink — the colony then does nothing that tick and
files a fresh request on the next mutation event. The relay uses exactly the same deletion the
pre-existing unparseable and `Rejection` paths use. **No new race.**

**Is the counter keyed and cleaned so it cannot leak or misattribute?** Keyed on `request_id`
(unique per request, see N8) — cannot misattribute. Cleaned on both exits. Leaks on abandonment
(N8, minor). Note the inconsistency with `_unparseable`, which is keyed on `str(request_path)` — a
constant — so the two counters have different lifetimes for the same file. Not a defect, but if
anyone later assumes they behave alike they will be wrong.

**Can a transient outage now bin a request that would have succeeded, and is that acceptable at
`ODIN_RETRY_S=900` / `MIND_ASK_TIMEOUT_S=900`?** Yes, and yes. Time to bin: 4 failing asks plus 3
retry waits. Fast failures (gateway 500) → 3 × 900 s ≈ **45 min**; slow failures (ask timeouts) →
4 × 900 + 3 × 900 ≈ **1 h 45 min**. So any gateway/debain2 outage longer than ~45 min bins the
in-flight request. The cost of binning is genuinely small, and I verified the whole chain rather
than taking the report's word:

- the colony loses one Odin variant for that birth and falls back to
  `base.mutate_at_birth` — no colony damage;
- `verdict_row` (`verdicts.py:61`) turns any entry with `outcome != "proposed"` into tier
  `"rejected"`, which is in `EXCLUDED_TIERS`, so `ab_arms` skips it — **neither a win nor a loss
  for the arm**. The report's claim here is correct;
- `verdict_corpus.export` skips it at `verdict_corpus.py:140`
  (`v["tier"] == "rejected" and not e.get("reply")`) — no training-corpus contamination from a
  reply-less rejection;
- only `colony_ab()` (explicitly `"advisory": True`) counts it in `proposals`, mildly depressing
  that arm's advisory `births_per_proposal`. Pre-existing behaviour for all rejections.

Acceptable. The one real consequence is N2: a long enough outage bins every request, both arms
reach 48 h at zero, and the candidate gets blacklisted.

**`end_ab` in the win branch vs `flip()`'s env restore.** Correct and ordered correctly. `flip()`
on `flip_failed` restores `MIND_MODEL_ALIAS=before` and `MIND_LLM_CANDIDATE=before_cand` (the
candidate tag, since an A/B is running) and does **not** restart. `end_ab` then overwrites
`MIND_LLM_CANDIDATE=""` and restarts. Net env: incumbent alias restored, candidate cleared, split
line left at `0.2` (harmless — the relay treats an empty candidate as no A/B, and `ab_split` is
only read when `candidate_llm` exists). Exactly one restart, asserted by the test. No double
restart, no ordering hazard. The one latent edge is `flip()`'s `if before is not None` — a
`MIND_MODEL_ALIAS` absent before the flip is never restored — but that is pre-existing and
unreachable in practice (`deploy.sh` always seeds it).

**`_env_unset` vs `_env_set` on real file shapes.** Checked by construction against
`deploy/grid.env.example`:

- *comments*: a full-line `#MIND_AB_SPLIT=0.2` (which the example actually ships, line 48) does
  not `startswith("MIND_AB_SPLIT=")`, so `_env_unset` preserves it and `_env_get` correctly reports
  the key as absent. Correct.
- *no trailing newline*: `splitlines()` keeps the last partial line; both writers always terminate
  with `\n`, so the file is normalised. Correct.
- *duplicate keys*: `_env_unset` removes **all** matching lines (right for an unset).
  `_env_set` rewrites **every** matching line to the same new value, leaving the duplicates in
  place — harmless for systemd (last wins) but it does not deduplicate. Pre-existing.
- *empty file / trailing blank line*: `_env_unset` writes `""` on an empty list where `_env_set`
  would write a bare `"\n"`; a trailing blank line is preserved by both. Cosmetic.
- *leading whitespace* (` MIND_AB_SPLIT=0.2`, legal in an EnvironmentFile): neither function
  matches it. `_env_set` appends a second line, which systemd resolves last-wins, and `_env_get`
  reports absent so `_env_unset` removes only the appended one and the indented original survives.
  Recoverable, low risk, not introduced here.
- the real problem is not shape but the strip — N3.

**`short_arms` defaults on rows written before the field existed.** Correct.
`short is None or list(short) == [tag]` → a pre-field row reads as candidate-short and stays
blocked, which is the conservative direction and keeps `test_ab_state_and_lost_tags` honest
without editing it. `short_arms == []` cannot arise from `release()` (an `inconclusive` verdict
means at least one arm is under sample), and if it were hand-written it would read as
not-blocked — also the safe direction. `[incumbent]` alone → not blocked, correct. The only gap is
that `ab_check`'s stuck row carries no `short_arms` at all and is not consulted (N2).

**Did `main()`'s hoisted `ab_state` change any CLI path?** No. `--forget` returns before the hoist;
`--abort`, `--check` and the display branch each read the same `versions` object with no
intervening mutation, so the single read is value-identical to the three it replaced. The one
ordering change inside `--abort` is deliberate and right: `_ab_snapshot` runs *before* `end_ab`, so
the row records the arms as they were when the operator pulled the plug. (`--check` still causes a
second `ab_state` read inside `ab_check` — redundant, not wrong.)

---

## 5. R1 — verdict: **CONFIRMED, and it is worse than "runs forever"**

Your reading is right, and I proved it rather than reasoning about it. `release()`'s I2 guard
(`weekly.py:432-443`) returns *before* the `verdict == "inconclusive" and state["weeks"] + 1 >=
MAX_WEEKS` check at `weekly.py:449`. It writes `ab_inconclusive`, which `ab_state` counts as
`weeks += 1` but does **not** treat as a closing decision (only
`released`/`held`/`rolled_back`/`flip_incomplete` close). So every Sunday adds another
`ab_inconclusive` row and the A/B stays open with no cap. Run against unmodified source in the
scratchpad copy:

```
W2: decision=ab_inconclusive weeks=1 ab_open=True
W3: decision=ab_inconclusive weeks=2 ab_open=True      <- MAX_WEEKS = 2, and nothing happens
W4: decision=ab_inconclusive weeks=3 ab_open=True
W5: decision=ab_inconclusive weeks=4 ab_open=True
W6: decision=ab_inconclusive weeks=5 ab_open=True
relay still splitting; ledger still open after 5 weeks (MAX_WEEKS=2)
ab_check returns None -> `ab --check` exits 0, nightly is quiet
```

The consequences compound, and the wave's other two fixes actively hide them:

1. The candidate keeps 20% of live traffic indefinitely.
2. **The entire release gate is wedged.** `release()` takes the A/B branch every Sunday — no
   candidate discovery, no probes, no releases, ever again — until a human notices.
3. **The nightly is silent.** `ab_check`'s own I2 guard returns `None`, so `ab --check` prints
   `not stuck` and exits **0** every night. I1's exit-3 never fires. The only signal is one
   `log.warning` line in `journalctl -u grid-verdicts` and a `versions.jsonl` growing by a row a
   week.
4. `versions.jsonl` accumulates unbounded rows for one A/B, and `ab_state`'s `weeks` counter grows
   past any value the schema anticipates.

The trigger is an operator running `set-mind-model.sh --relay-only` mid-A/B — exactly the action
the wave added a README warning about, which means the README now documents a foot-gun whose
handling makes it permanent instead of self-limiting. It also violates the brief's own I2 ruling
("counts toward MAX_WEEKS like any inconclusive week"): the row counts, but nothing ever reads the
count.

**Your proposed fix is correct.** I implemented it in the scratchpad and it behaves exactly as
specified:

```
W2: decision=ab_inconclusive weeks=1 ab_open=True
W3: decision=held            weeks=None ab_open=False   <- closes at MAX_WEEKS
```

with `test_weekly.py` + `test_odin.py` still 72 passed. Confirmations of each part of your
proposal:

- `held` is a closing decision, so `ab_state` returns `None` and the gate resumes normal Sundays.
- `end_ab` clears `MIND_LLM_CANDIDATE` and restarts, so env, relay and ledger agree.
- The candidate is **not** remembered: the reason starts with `incumbent label changed mid-A/B`,
  which matches none of `lost_tags`' prefixes. Verified — `lost_tags` stays empty.
- `short_arms: []` is therefore belt-and-braces rather than load-bearing, but keep it: it is the
  correct value, and it is the only thing standing between you and a future reword of that reason
  to start with `colony A/B inconclusive`, which would silently blacklist the tag.
- Self-healing: because the tag is retestable, the next Sunday starts a fresh A/B whose
  `incumbent = _env_get(cfg.grid_env, "MIND_MODEL_ALIAS")` is the *new* label, so the guard does
  not fire again and the new A/B evaluates correctly.

Two refinements I'd add, both small:

1. **Honour `end_ab`'s outcome.** Your sketch says "call `end_ab` and record `held`". Use the same
   idiom as every other `end_ab` call site in the file — `held` on `"ok"`, `flip_incomplete` with
   the `; relay restart failed clearing the A/B` suffix otherwise — so a failed restart here is not
   the one place in the module that swallows it. (My scratchpad implementation did this; it is what
   made the run above clean.)
2. **Make `ab_check`'s guard loud, not silent.** With (1) applied the wedge is bounded at two
   weeks, but for up to two weeks the nightly still exits 0 with only a warning. Either return a
   sentinel from `ab_check` and exit 3 on it, or leave `ab_check` as-is and accept a bounded
   two-week window. I'd take the bounded window — exit 3 for an operator's own mistake, every night
   for two weeks, is noise — so (2) is optional and (1) is not.

I looked for a better fix than yours and did not find one. Aborting immediately on the first
relabelled Sunday is tempting (the data is unusable from the relabel onward either way) but it
contradicts the controller's explicit I2 ruling that the week is spent like any other inconclusive
one, and it would make an operator's momentary `set-mind-model.sh` destroy a week-old A/B with no
grace. Honouring `MAX_WEEKS` in the guard is the minimal change that restores the invariant the
rest of the module already assumes: *every A/B ends within `MAX_WEEKS`.*

---

## 6. Remaining paths where `grid.env` / the live relay and the ledger can disagree

Enumerated in full, since this class has now recurred three times.

| # | Path | Detected? |
| --- | --- | --- |
| 1 | **A standing `flip_incomplete` from any source** — env cleared/updated, relay never restarted, ledger closed. Announced once (release exits 3, or `ab --check` exits 3 that night) and then invisible forever, because `flip_incomplete` closes `ab_state` and the next `ab --check` says "no A/B running". | Once, then **no** (N4) |
| 2 | **R1** — relay splitting and ledger open, unbounded, with `ab --check` exiting 0 every night. | **No** (N1) |
| 3 | **`ab_check`'s I2 guard** returning `None` for any other reason the labels drift — silent, exit 0. | **No** (N1/N4) |
| 4 | **`ab --abort` / `rollback` recording `flip_incomplete` but exiting 0.** | Printed, exit **0** (N6) |
| 5 | **`_env_get`'s comment strip** — gate and relay disagree about a value's text by construction, and I2's guard compares gate-to-gate so it cannot see it. | **No** (N3) |
| 6 | **`systemctl restart` returning 0 for a unit that then crash-loops** (C1's own scenario, or any bad env line). The gate records `ok`; the relay is dead; the ledger says the A/B is running and no arm ever settles. `is_stuck`'s new both-arms-zero rule reaches this in 48 h — but then blacklists the candidate for it (N2). Pre-existing; C3 half-covers it and N2 spoils the coverage. | Partly, wrongly |
| 7 | **`start_ab`'s revert restoring a pre-existing stray `before_cand`** — env carries a candidate with no ledger row. Faithful restore of a prior divergence rather than a new one, and `--abort`'s N2 branch can clear it. | Yes, by `--abort` |
| 8 | Anyone editing `grid.env` by hand between a gate run and a relay restart. Out of scope, but the reason a reconciliation check beats another transition-time guard. | **No** |

Paths 1, 2, 3, 5 and 8 all share one root cause: **every fix so far checks a transition, and
nothing checks the resting state.** Until `ab --check` compares what the relay actually loaded
against `grid.env` — the relay already knows both `model_alias` and `candidate_tag` and already
publishes envelopes — this class will produce a fourth instance. I'd file that as the follow-up
regardless of what happens to N1–N3.

---

## 7. Verdict

The three Criticals are genuinely closed, and I walked each failure to confirm it can no longer
happen. The tests are real: eight mutations applied for real, eight RED. The implementer's report
is accurate and unusually honest (it flags its own epsilon finding against itself).

But the wave introduces one Critical of its own (R1/N1 — the release gate wedges permanently and
silently on an operator action the wave itself documents) and two Importants (N2 — C3 and I3
contradict each other and a colony outage now blacklists an innocent candidate; N3 — the `_env_get`
strip recreates the gate↔relay divergence that I2 exists to catch, in the one place I2 cannot see).

**Ready to merge? No.** Fix N1 (your R1 proposal plus the `end_ab`-outcome refinement), N2 and N3;
N4 as a follow-up issue; N5–N8 at the maintainer's discretion.
