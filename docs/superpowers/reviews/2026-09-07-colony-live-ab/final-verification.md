# Final scoped verification — `b99bdcc..d3aeb4e` (branch `feat/ab`)

Adversarial final pass before merge. Scope: the one commit `d3aeb4e` ("close the Critical the last
wave introduced, and reconcile at rest"), which claims to fix N1–N8 of `final-fix-re-review.md`.
Earlier work is re-read only where these fixes depend on it.

**Verification actually run** (nothing in the worktree was modified; `git status --short` empty
before and after):

- Full suite in the worktree: `.venv/Scripts/python.exe -m pytest tests -q -W error`
  → `272 passed, 4 skipped in 21.10s`. The report's line reproduces exactly.
- All mutations and all behavioural probes were run against a **copy of the tree** in the session
  scratchpad (`scratchpad/mut`, with `deploy/` copied so `test_deploy_units.py` is live).
  Baseline there: `107 passed` for `test_weekly.py + test_odin.py + test_verdict_corpus.py +
  test_deploy_units.py`. After every mutation was reverted: `272 passed, 4 skipped`, and
  `diff -r -x __pycache__` between the scratchpad's `grid/` + `deploy/grid.env.example` and the
  worktree's is empty.

---

## 1. Per-finding verdict

| # | Finding | Verdict |
| --- | --- | --- |
| N1 | relabelled-incumbent guard must honour `MAX_WEEKS` and close the A/B | **ADDRESSED** — traced below; mutation RED (2 tests) |
| N2 | one memory rule keyed on `short_arms`, not on wording | **ADDRESSED** — a 48 h both-arms-zero outage no longer blacklists; reproduced end-to-end |
| N3 | `_env_get` comment strip recreated the gate↔relay divergence | **ADDRESSED** — strip reverted, example fixed, shipped-file guard added (residual below, Minor) |
| N4 | resting-state reconciler in `ab_check` | **ADDRESSED for the ledger↔env leg**; the env↔relay leg is still never read (see §4). Blast radius verified in §2 |
| N5 | `verdict_corpus.export` missing the `request_id` guard | **ADDRESSED** — mutation RED |
| N6 | `--abort` / `rollback` exit 0 on `flip_incomplete` | **ADDRESSED** — `_exit_code` shared by all four verbs; mutation RED (one gap: `flip_failed`, §5) |
| N7 | `_env_ab_view` folds unattributed rows into a missing incumbent | **ADDRESSED** — `arms.pop("", None)`, `incumbent: None`; mutation RED |
| N8 | `_defers` leaks on abandoned requests | **ADDRESSED** — pruned to the current request id each poll; mutation RED. `_retry_not_before` deliberately deferred, flagged in the report |

### N1 — walking the failure, and why it can no longer happen

Old failure (`weekly.py`, round 1): `release()`'s I2 guard returned before the
`verdict == "inconclusive" and state["weeks"] + 1 >= MAX_WEEKS` check, and wrote `ab_inconclusive`.
`ab_state` counts `ab_inconclusive` as `weeks += 1` but treats only
`released`/`held`/`rolled_back`/`flip_incomplete` as closing, so the A/B stayed open forever: a row
a week, no probes, no releases, 20 % of live traffic to an unjudgeable candidate, and `ab --check`
exiting 0 every night because its own I2 guard returned `None`.

Now (`weekly.py:452-466`): the same branch computes `base_reason`, then

- `state["weeks"] + 1 >= MAX_WEEKS` → `end_ab(...)`; `"held"` + `; A/B ended after 2 weeks` on
  `"ok"`, `"flip_incomplete"` + `; relay restart failed clearing the A/B` otherwise;
- below the cap → `ab_inconclusive`, unchanged.

`held` and `flip_incomplete` both close `ab_state`, so week 2 is terminal by construction: the
counter that `ab_inconclusive` increments is now actually read on the same branch that increments
it. `end_ab` clears `MIND_LLM_CANDIDATE` and restarts the relay, so env, relay and ledger agree.
The reason starts `incumbent label changed mid-A/B`, which matches none of `lost_tags`' three
prefixes, so the candidate is not blacklisted for an operator's mislabel — and the next Sunday's
`incumbent = _env_get(cfg.grid_env, "MIND_MODEL_ALIAS")` is the *new* label, so the guard does not
re-fire (self-healing). `ab_check`'s guard still returns `None` but now names `MAX_WEEKS` in its
nightly warning, i.e. a bounded, announced two-week window rather than a silent forever.

Mutation applied for real: `if state["weeks"] + 1 >= MAX_WEEKS:` → `if False:` →
`test_a_relabelled_ab_still_ends_at_max_weeks` and
`test_a_relabelled_ab_that_cannot_restart_the_relay_is_flip_incomplete` both **FAILED**. The first
test is not thin: it asserts `held`, the reason, `ab_state(vs) is None` (the gate is unwedged),
`lost_tags(vs) == set()`, `short_arms == []`, the candidate cleared from `grid.env`, and that the
relay was restarted — and it gets there by running two real Sundays through `release()`.

### N4 — walking the failure, and what it does and does not close

Old failure: every `flip_incomplete` row closes `ab_state`, so the divergence it records was
announced exactly once (by the run that created it) and from the next night on `ab --check` took
the `state is None` branch, printed `no A/B running` and exited 0 forever while `grid.env` and the
live relay disagreed.

Now `ab_reconcile(cfg, *, local_run, dry_run)` runs first inside `ab_check` (`weekly.py:646-648`),
and `main()`'s `--check` branch no longer short-circuits on `state is None` (`weekly.py:826-829`) —
which was the actual carrier of the bug, since a standing `flip_incomplete` *is* `state is None`.
I traced all five branches and then ran each against the real code:

| World | Reconciler | Verified |
| --- | --- | --- |
| env candidate the ledger has never seen | `log.warning`, touch nothing | S1/S3 below |
| ledger open **and** env splitting | `None` | S7 below |
| last A/B decision was `flip_incomplete` | `end_ab` + `held` row; second pass is a no-op | S4 below |
| env splits to a tag whose A/B the ledger closed | `end_ab` + `held` row | S2 below |
| ledger open, env carries no candidate | `end_ab` + `held` row, A/B closed | S6 below |
| nothing anywhere | `None` | S3 |

Convergence is real: a successful repair records `held`, which makes the triggering condition false,
so it does not re-fire (asserted in the test, reproduced in S4). A failed repair records
`flip_incomplete`, exits 3, and retries — by design, and documented in the README.

Mutation applied for real: deleting the `ab_reconcile` call at the top of `ab_check` → 4 tests RED;
`if False:` on the never-seen guard → `test_reconciler_leaves_a_hand_started_ab_completely_alone`
RED; restoring the `state is None` short-circuit in `main` →
`test_ab_check_cli_reconciles_even_when_the_ledger_shows_no_ab` RED.

---

## 2. N4's blast radius: does a hand-started env-only A/B survive `ab --check`?

**Yes, for the run you are about to make — with one precondition you must check first, and one
follow-on you must know about.**

The trace (`weekly.py:602-611`):

```python
rows = versions.rows()
state = ab_state(versions)                     # None: no ab_started row is open
env_candidate = _env_get(cfg.grid_env, "MIND_LLM_CANDIDATE")     # "kannaka-brain"
if env_candidate and env_candidate not in {r.get("version") for r in rows
                                           if r["decision"] in _AB_DECISIONS}:
    log.warning(...); return None              # <- returns BEFORE any end_ab / _env_set / restart
```

`ab_reconcile` returns `None` without reaching `end_ab`, so nothing writes `grid.env` and nothing
runs `systemctl restart`. `ab_check` then reads `ab_state` → `None` → returns `None`; `main()`
prints `no A/B running` and exits 0. `MIND_AB_SPLIT` is never read by the reconciler at all.

Run for real against the shipped code (probe in the scratchpad, realistic ledger: a `released` v3
row, an `ab_started` v4 row and a `held` v4 loss row):

```
S1 smoke: candidate=kannaka-brain, never a ledger version
    row = None | env same = True | restarts = 0 | ledger rows = 3 (unchanged)
    log: "kannaka-brain is split in .../grid.env with no ledger row; hand-started, leaving it alone"
S3 empty ledger, hand-started        -> row = None | env same = True | restarts = 0
```

**The precondition.** The guard is not "no ledger row for *this* A/B"; it is "this *tag* has never
appeared as `version` on any row whose decision is in
`("ab_started","ab_inconclusive","released","held","rolled_back","flip_incomplete")`". So before you
run the receipt, confirm the tag you are about to split to has never been a `version`:

```bash
python - <<'EOF'
import json
tags = {"ab_started","ab_inconclusive","released","held","rolled_back","flip_incomplete"}
hits = [r for l in open("/srv/grid/releases/versions.jsonl")
        for r in [json.loads(l)] if r["decision"] in tags and r.get("version") == "kannaka-brain"]
print("SAFE" if not hits else f"NOT SAFE: {len(hits)} row(s) -- the nightly will clear it")
EOF
```

`kannaka-brain` is the base model, never a Rogue promotion tag, so on a clean production ledger this
is `SAFE`. If it prints otherwise, the nightly *will* clear your split at 03:15. Proof of the
negative case, same probe:

```
S2  candidate=kannaka-brain-v4 (a tag the ledger has seen)
    row = ('held', 'colony A/B reconciled: kannaka-brain-v4 is split in grid.env but the ledger closed its A/B')
    env same = False  (MIND_LLM_CANDIDATE emptied) | restarts = 1
S2b candidate=kannaka-brain-v3 (the incumbent's own released tag) -> same, cleared
```

**The follow-on (new finding, Important — see §5 D1).** The protection is *one-shot per tag*. The
receipt ends in `ab --abort`, which appends a `held` row with `version=kannaka-brain`. From that
moment the tag *is* in the ledger, so a **second** run of the same receipt is cleared by the next
nightly. Reproduced end to end:

```
run 1: nightly while hand-started -> None            | env kept: True
abort recorded; lost_tags = []
run 2: nightly while hand-started -> ('held', 'colony A/B reconciled: kannaka-brain is split in grid.env bu...')
       env kept: False | restarts: 1
```

Not a blocker for the first run. It is a landmine for the second, and the README's "an A/B you
started by hand — one the ledger has never seen — is logged at WARNING and left completely alone"
does not say that aborting it spends the exemption.

Two smaller blast-radius notes, both verified:

- **The reconciler is fully disabled while a hand-started A/B is up.** The never-seen guard returns
  before the `flip_incomplete` check, so a standing `flip_incomplete` elsewhere is *not* repaired
  for as long as your hand-started split is in `grid.env` (`S5: row = None, restarts = 0`, with a
  `flip_incomplete` last decision). Conservative and arguably right, but it means the receipt
  suspends N4 for its duration.
- **A hand-started tag is only protected while no ledger A/B is open.** If the gate has an A/B open
  for tag T and you hand-swap `MIND_LLM_CANDIDATE` to a fresh tag U, the reconciler leaves U alone,
  but the *stuck* path 48 h later ends the A/B and `end_ab` clears U anyway — and T is blacklisted
  for producing nothing. Operator-induced, out of the receipt's shape, noted for completeness.

---

## 3. Adjudicating the memory rule

### The rule as it now stands

It is a **two-level** rule, not the one-level rule the docstring describes:

1. **Level 1 — is this row a rejection at all?** Decided purely by the reason's prefix:
   `reason.startswith(("colony A/B lost", "colony A/B stuck", "colony A/B inconclusive"))`.
   Decision is irrelevant (N1 of the earlier wave), so a `flip_incomplete` loss counts.
2. **Level 2 — is the rejection attributable to the candidate?** Decided by
   `evals.colony_ab.short_arms`: remember iff `short is None or not (set(short) - {tag})` — "no arm
   other than the candidate was under sample". Missing field ⇒ remembered (conservative).
3. A tag is blocked iff its latest remembered row's `ts` is later than its latest `unchanged` +
   `operator forgot` row's `ts`.

### Truth table

| # | Outcome | Reason prefix | `short_arms` recorded | Remembered? | Should be? |
| --- | --- | --- | --- | --- | --- |
| 1 | loss | `colony A/B lost` | `[]` (both arms ≥ 100 by construction) | **yes** | yes ✔ |
| 1b | loss, restart failed (`flip_incomplete`) | `colony A/B lost …; relay restart failed…` | `[]` | **yes** | yes ✔ |
| 2 | stuck, classic (incumbent ≥ 50, candidate 0) | `colony A/B stuck: candidate 0 settled…` | `[candidate]` | **yes** | yes ✔ |
| 3 | stuck, both arms zero (colony outage) | `colony A/B stuck: no proposals from either arm…` | `[candidate, incumbent]` | **no** | no ✔ |
| 4 | inconclusive at max, candidate alone short | `colony A/B inconclusive after 2 weeks…` | `[candidate]` | **yes** | yes ✔ (livelock guard) |
| 5 | inconclusive at max, both short | same | `[candidate, incumbent]` | **no** | no ✔ |
| 5b | inconclusive at max, incumbent alone short | same | `[incumbent]` | **no** | no ✔ |
| 6 | inconclusive-relabelled, N1 close | `incumbent label changed mid-A/B…` | `[]` | **no** | no ✔ (but see below) |
| 7 | won-but-flip-failed (C2) | `colony A/B won but the tag copy failed…` | `[]` | **no** | no ✔ |
| 7b | won, restart failed | `alias serves the candidate; relay restart failed twice` | `[]` | **no** | no ✔ |
| 8 | operator abort | `colony A/B aborted by operator` | absent (env view / started block) | **no** | no ✔ |
| 9 | operator forget | `operator forgot the colony A/B loss of X` (`unchanged`) | n/a | unblocks | ✔ |
| 10 | **new:** reconciler repair (3 shapes) | `colony A/B reconciled: …` | `[]` | **no** | no ✔ — the row it repairs already decided memory (verified: a reconciled loss keeps `lost_tags == {tag}`) |
| 11 | non-A/B `held` (probe failed, not on key, no promotion) | — | — | **no** | no ✔ |

**No row in the table gives the wrong answer today.** I ran rows 1, 2, 3, 4, 5, 10 against the real
code rather than reading them off; row 3 in particular (`short_arms in block? True`,
`lost_tags: set()` after a 48 h both-zero outage) is the exact fault N2 exists to close, and it is
closed.

### The relabelled row's `short_arms: []` — correct, fragile, or wrong?

**Fragile, not wrong — and I would change it, one line.** The implementer's own flag is accurate.
Level 1 (the prefix) is doing all the work for row 6; `short_arms: []` would mark it *blockable* at
level 2. And `[]` here is not merely redundant, it is **untruthful**: `[]` is the encoding that
means "both arms were fully sampled and the colony decided on the merits" (it is exactly what a
genuine loss carries). On the relabelled branch nothing was measured at all — `arms` is literally
`{}`. So the row asserts the strongest possible attribution to the candidate, and is saved only by
a string prefix that a future reword ("colony A/B inconclusive: incumbent label changed…" is a very
natural rewording) would silently remove.

Minimal fix, in `release()`'s relabelled block (`weekly.py:452-453`):

```python
"short_arms": [state["candidate"], state["incumbent"]],   # nothing was measurable, not "nothing was short"
```

That is the truthful value, it makes level 2 agree with level 1 instead of contradicting it, and
`lost_tags` then abstains for two independent reasons. Nothing else reads the field. (The previous
reviewer advised keeping `[]`; I think that was the wrong call for exactly the reason he gave —
"the only thing standing between you and a future reword" — since `[]` is the value that *loses*
that bet.)

### A second, larger fragility in the same field (new — see §5 D2)

`short_arms` now carries **two different meanings** in two writers:

- `release()` uses the module's `short_arms()` helper: "settled < `MIN_SAMPLE` (100)";
- `ab_check()` hand-rolls `[candidate] if incumbent_settled else [candidate, incumbent]`:
  "produced literally nothing".

In the classic stuck case the incumbent has ≥ `STUCK_INCUMBENT_MIN` (50) but typically **< 100**, so
the two disagree. Verified:

```
classic stuck, incumbent 60 / candidate 0
  short_arms() would return ['cand','inc']  -> lost_tags would NOT block
  ab_check records          ['cand']        -> lost_tags DOES block   (correct per spec §8)
```

The recorded value is the right one, but the obvious future refactor — replacing the hand-rolled
expression with a call to the same-named helper sitting 300 lines above it — would silently stop
blacklisting every stuck tag and reopen the §8 livelock. The comment in `ab_check` explains the
branch but not that the field means something else here. One sentence in `short_arms()`'s docstring
("`ab_check` writes this field with a different predicate — produced-nothing, not under-sample —
because a stuck incumbent is by definition between 50 and 100") is the whole fix.

---

## 4. Remaining paths where `grid.env`, the live relay and the ledger can disagree

The answer is **not "none"**, but the class is materially narrowed. Precisely: the reconciler closes
the **ledger ↔ `grid.env`** leg at rest. It never reads the **third leg — what the live relay
actually loaded**. It *repairs* that leg blindly (every repair ends in a `systemctl restart`), but
it cannot *detect* a divergence there, because nothing in the module ever asks the relay what it is
serving. The previous review's prescription (an `odin.started` envelope carrying
`model_alias`/`candidate_tag`, or `systemctl show -p ExecMainStartTimestamp grid-odin` vs
`grid.env`'s mtime) is still unimplemented.

| # | Path | Status after this wave |
| --- | --- | --- |
| 1 | standing `flip_incomplete` from any source | **CLOSED** — repaired + re-announced nightly until gone (N4) |
| 2 | relabelled A/B running forever | **CLOSED** — bounded at `MAX_WEEKS` (N1); ≤ 2 weeks of split traffic with a nightly WARNING and exit 0 by deliberate choice |
| 3 | env splits to a tag whose A/B the ledger closed | **CLOSED** (N4) |
| 4 | ledger A/B open, nothing splitting | **CLOSED** (N4) |
| 5 | `--abort` / `rollback` recording `flip_incomplete` and exiting 0 | **CLOSED** — exit 3 (N6), and the nightly reconciler repairs it |
| 6 | `_env_get`'s comment strip | **CLOSED** — gate and relay read the value verbatim, identically, by construction (N3) |
| 7 | **hand-started A/B (never-seen tag) present** | **OPEN by design** — the reconciler returns before every other check, so *all* of paths 1/3/4 go unrepaired while it is up (verified, S5) |
| 8 | **duplicate `KEY=` lines** — `_env_get` returns the **first**, systemd's EnvironmentFile takes the **last** | **OPEN** — verified (S8): with `MIND_LLM_CANDIDATE=` then `MIND_LLM_CANDIDATE=kannaka-brain`, the relay splits and the gate sees no A/B; the reconciler reports nothing. Reachable from a `>>` append, which is how an operator naturally follows the §7 receipt (the example ships the key *commented out*, so there is no line to edit) |
| 9 | leading whitespace on a key (` MIND_LLM_CANDIDATE=x`) — legal for systemd, invisible to `_env_get` | **OPEN**, pre-existing, same shape as 8 |
| 10 | quoted or trailing-whitespace values (`MIND_MODEL_ALIAS="v3"`, `…=v3   `) — systemd unquotes and right-trims, `_env_get` does not | **OPEN**, Minor: `test_env_get_reads_a_value_exactly_as_the_relay_would` over-claims. Not reachable from shipped tooling (`set-mind-model.sh`'s `setenv` and `_env_set` both write bare `KEY=value`), only from a hand-edit |
| 11 | `grid.env` edited without a relay restart | **OPEN** — the third leg. Mitigated for the A/B case (a relabel with no restart now costs 2 weeks and, thanks to N2's `[incumbent]` row, does **not** blacklist the candidate — I traced this), undetected in general |
| 12 | `systemctl restart` returns 0 into a crash-loop | **PARTLY** — reached within 48 h by the both-arms-zero stuck rule *only while a ledger A/B is open*, and now correctly does not blacklist (N2). With no A/B running, nothing notices |

So: paths 7–12 remain, all of them either operator-induced or third-leg. I would file path 8 (it is
one line: make `_env_get` return the **last** match, matching systemd) and the third-leg check as
follow-ups; neither is a merge blocker.

---

## 5. New defects introduced by round 2

### Critical

**None.** I went looking specifically for a fourth Critical — the reconciler is the largest new
surface in the wave and it holds a `systemctl restart` and a `grid.env` write — and I could not
construct one. The dangerous branch (clearing an operator's split) is guarded, the guard is tested,
and the guard's failure mode when it *does* misfire is loud (a printed ledger row plus a WARNING),
not silent.

### Important

**D1 — the hand-started exemption is one-shot per tag, and `ab --abort` spends it.** §2 above,
reproduced. After the §7 receipt's own final step, re-running the receipt on the same tag has its
split cleared by the next nightly, with only a ledger row to show for it. Minimal fix: exempt a tag
whose only ledger rows are non-`ab_started` ones, or better, key the "the ledger closed its A/B"
branch on an actual `ab_started` row for that tag rather than on any decision —
`_AB_DECISIONS` is far broader than the branch needs. Failing that, one sentence in the README:
"the exemption lasts until the tag appears in the ledger; `ab --abort` puts it there."

**D2 — `short_arms` means two different things in its two writers.** §3 above, verified
numerically. Nothing is wrong today; the next refactor toward the obvious helper silently reopens
the §8 livelock. Fix is a docstring sentence.

### Minor

**D3 — the relabelled row's `short_arms: []` is untruthful** and is the only row in the table whose
level-1 and level-2 answers contradict each other. One-line fix in §3.

**D4 — a repair that cannot restart the relay appends a `flip_incomplete` row every night,
forever.** Deliberate and documented ("it retries every night"), and exit 3 keeps the unit failed —
but note that `deploy/skywave/deploy.sh:137-141` writes **only** the `grid-mind.service` sudoers
line, while the `grid-odin.service` line the gate needs for `sudo -n systemctl restart` is
README-manual (`deploy/README.md:29-30`). On a box deployed by the script without that manual step,
the first divergence turns into a permanent nightly row-appending loop rather than a one-off
failure. Worth confirming `/etc/sudoers.d/` has the grid-odin line before you deploy.

**D5 — `_exit_code` covers `flip_incomplete` but not `flip_failed`.** `rollback` whose `ollama cp`
fails records `held` with `reason: "rollback from X: flip_failed"` and exits **0**; likewise a
`release` win whose tag copy fails. N6's stated contract is "one contract for every verb that can
record `flip_incomplete`", so this is inside the letter of the fix but outside its spirit: a script
wrapping `rollback` still cannot see that the rollback did not happen.

**D6 — `test_grid_env_example_has_no_inline_comments` has two small holes.** It reads the shipped
`deploy/grid.env.example` (via `UNITS.parent`), it does cover commented-out template lines, and the
mutation is genuinely RED (I put the `ODIN_RETRY_S=60          # 900 with a CPU model` comment back
→ FAILED). But it matches only `" #"`, so `ODIN_RETRY_S=60# x` and a tab before `#` both pass while
still killing the relay. Widen to `re.search(r"\s#", value) or "#" in value`.

**D7 — `_env_ab_view`'s output schema changed** (`"incumbent": "" → None`) without a README note.
It feeds `_ab_snapshot`, so an `--abort` row for a hand-started A/B now carries
`colony_ab.incumbent: null` and no `short_arms` at all — harmless (its reason matches no rejection
prefix), but worth knowing if anything downstream parses those rows.

**D8 — theoretical concurrency.** `ab_reconcile`'s "ledger open, env carries no candidate" branch
reads `grid.env` between `flip()`'s candidate clear and `release()`'s ledger append — a window that
spans `ollama cp` (timeout 600 s). A concurrent `ab --check` in that window would restart the relay
mid-flip and append a spurious `held`. **Not reachable in the shipped configuration**: the nightly
is `03:15` (`grid-verdicts.timer`), the release is `Sun 06:00` (`grid-weekly-release.timer`), and
`grid-weekly-release.service` carries `After=grid-verdicts.service` so a `Persistent=true`
catch-up runs them in order, not together. Only a hand-run `release` overlapping the nightly hits
it.

**D9 — `ab --check --dry-run` now prints a `held` "reconciled" row it did not write.** Cosmetic.

### Checks requested that came back clean

- **`_env_get` comment-strip removal vs every existing caller.** Nine call sites, three keys
  (`MIND_MODEL_ALIAS`, `MIND_LLM_CANDIDATE`, `MIND_AB_SPLIT`). None of the three ships with an
  inline comment in `grid.env.example`, so **no shipped line parses differently**. On a live
  `grid.env` the change is strictly correcting: I2's guard (`weekly.py:438`) and `ab_arms`'
  attribution now use the same byte string the relay puts in `model_alias`, which is precisely the
  divergence N3 named. `flip`'s and `start_ab`'s revert paths now round-trip a commented line
  verbatim instead of silently deleting the comment. Residual: quoting/trailing whitespace (path 10
  above).
- **`verdict_corpus.py`'s new guard** is the same predicate as `ab_arms`' and is on the identical
  expression; mutation RED.
- **`_defers` bounding.** Keyed on `request_id` (thegrid mints a fresh
  `sha256(time_ns + repr(names))[:16]` per request, so no misattribution), pruned to the current id
  on every successfully-parsed poll, before the retry-window check. Bounded at one entry.
  `_retry_not_before` still leaks — deliberately deferred, and the report says so.

---

## 6. Mutations I applied myself (not the report's table)

All applied for real in `scratchpad/mut`, the named test run, then reverted; the suite is back to
`272 passed, 4 skipped`.

| # | Mutation | Test | Result |
| --- | --- | --- | --- |
| 1 | `ab_reconcile`'s never-seen guard → `if False:` | `test_reconciler_leaves_a_hand_started_ab_completely_alone` | **RED** (AssertionError at :1007 — the operator's env was cleared) |
| 2 | `if state["weeks"] + 1 >= MAX_WEEKS:` → `if False:` | `test_a_relabelled_ab_still_ends_at_max_weeks`, `…_that_cannot_restart_the_relay_is_flip_incomplete` | **RED** (2) |
| 3 | `not (set(short) - {tag})` → the ruling's literal `list(short) == [tag]` | `test_lost_tags_remembers_a_loss_recorded_as_flip_incomplete`, `test_lost_tags_remembers_only_a_candidate_alone_shortfall` | **RED** (2) — confirms the implementer's flagged deviation is load-bearing: the literal rule stops remembering losses |
| 4 | inline comment restored on `ODIN_RETRY_S` in the shipped example | `test_grid_env_example_has_no_inline_comments` | **RED** — the guard reads the real file |
| 5 | drop `arms.pop("", None)` | `test_env_ab_view_does_not_credit_unattributed_rows_to_a_missing_incumbent` | **RED** |
| 6 | drop `if e.get("request_id")` in `verdict_corpus.export` | `test_export_skips_a_proposal_log_line_with_no_request_id` | **RED** |
| 7 | drop the `_defers` prune | `test_the_defer_counter_does_not_outlive_its_request` | **RED** |

Seven for seven. Plus eight behavioural probes (S1–S8, the double-receipt run, and the
`short_arms` numeric check) run against unmutated source.

---

## 7. Verdict

N1–N8 are all addressed, and I walked N1's and N4's failures rather than reading the report's word
for it. The tests are real — seven mutations, seven RED, including the two that matter most (the
hand-started guard and the `MAX_WEEKS` cap). The memory rule is now coherent and every row in the
truth table gives the right answer. The implementer's report is accurate and flags its own
deviation from the ruling, correctly.

**The question you care about:** an operator's hand-started env-only A/B on a tag the ledger has
never carried **survives `ab --check` untouched** — no `end_ab`, no `_env_set`, no restart, no row,
just a WARNING. Verified against the real code, and locked by a test whose mutation is RED. Run the
§7 receipt. Check the one-liner in §2 first, and know that the exemption ends the moment
`ab --abort` puts the tag in the ledger.

No new Critical. Two Importants (D1, D2), both of which are one-line or one-sentence fixes and
neither of which endangers the deploy. Seven Minors.

**Ready to merge? Yes** — with D1 and D2 filed as immediate follow-ups (D3 folded into D2's commit),
and the reconciler's third leg (env ↔ what the relay actually loaded, table row 11) filed as the
standing follow-up this class still needs.
