# Task 6, fix round 4 re-review: the calendar is gone, the ledger refuses, the confounded week forgets

Scope: the fix diff `5e98606..ffdf591` only.

Everything below was measured on clean `git archive` exports at `ffdf591` and `5e98606`
(`scratchpad/h2`, `scratchpad/p2`). The live working tree was never read for code — only the three
review documents, which are gitignored and not in the archive. Nothing tracked was modified;
`git status --short` in the worktree is empty at the end of this review.

**A note on method, because it nearly bit me.** My first pair of exports was extracted into a
scratchpad directory that already held reviewer scratch from an earlier session
(`tests/test_zz_review.py`, `tests/test_zz_residual.py`, `diffcheck_ro.py`, dated Sep 7 22:42).
`git archive | tar -x` overwrites tracked files but does not remove strangers, so the head export
appeared to collect 457 tests where the commit has 449. Every number below is from the second,
freshly-created pair of directories. Anyone re-running this should extract into an empty directory.

The supplied `review-5e98606..ffdf591.diff` was checked against `git diff 5e98606 ffdf591`: 562
`+` lines and 81 `-` lines in both, and the stripped change lines are byte-identical. **Faithful.**

Suite on the clean exports, project `.venv` (Python 3.13.2), `-W error`:

| commit | collected | result |
|---|---|---|
| `5e98606` (FIX_BASE) | 440 | **434 passed, 6 skipped** |
| `ffdf591` (head) | 449 | **443 passed, 6 skipped** |

The report says 436/4 and 445/4 — same totals; the two extra skips are the host-dependent `deploy`
tests, as in every previous round. **+9 collected, 0 removed**, which matches the report's "9 added,
none removed, none rewritten": `git diff -U0 5e98606 ffdf591 -- tests/` has zero deletion lines,
and the collected-test sets differ by exactly the nine new names. No failures, no warnings.

---

## 1. Verdicts at a glance

| finding | verdict |
|---|---|
| **C · N1** the cycle key was the ISO week while the gate's cycle is Sunday-anchored | **ADDRESSED.** The calendar proxy is deleted from the bound entirely, not patched; the bound now keys on each episode's judging count. Critical reproduced on the parent and gone at head, §2 |
| **I · N2** `_overlapped` was ledger-only while the interlock reads env too | **ADDRESSED.** Two sources, the same fallback shape `_blocking_experiments` uses. R12/R13/R14 RED. See N-b for a reachability caveat that does not undo the fix |
| **I · N3** a detected rewrite still produced a binding verdict; the chain was compared, not verified | **ADDRESSED, both halves.** The chain is re-derived row by row with `chain_hash`, exactly as `Ledger.verify` does; a detected rewrite raises `LedgerRewritten` before anything is tallied or written. §4 |
| **I · N4** an abandoned partner was not truncated where it stopped | **ADDRESSED.** `ended_ms` on the episode state, `until_ts` truncated, `factorial_partner_abandoned` on the row, and the test builds the *rollback-A/B* contamination case the review named rather than the benign one |
| **I · N5** involuntary degrades still wrote the permanent rejected list | **ADDRESSED, narrow version as ruled.** `_neighbours` is the predicate; `remember: false` exempts `lost`/`stuck` only; the inconclusive-at-cap memory is untouched. Proved independently, §5 |
| **m · N6** a missing chain head skipped the check | **ADDRESSED.** It is now a fault. R7 RED |
| **m · N7** a torn ledger was reported as rows removed | **ADDRESSED.** `Ledger`'s own corrupt-line number is passed through and named. R23 RED |
| **m · N8** the `rows: 0` boundary was untested | **ADDRESSED.** Pinned; R9 RED (this was my one round-3 survivor, X3) |
| **m · N9** the two-Sunday test stopped at `weeks == 2` | **ADDRESSED.** Four assertions added: both episodes closed, `_blocking_experiments` empty in both dimensions |

The diagnosis is right and the level is right. Deriving the bound from the episodes rather than
from any label is the correct shape — it needs no clock, no `--week` discipline and no assumption
about when an operator types the second command — and it is strictly stronger than anchoring the
label to Sunday, which is what I asked for. Taking the harder option was the right call.

---

## 2. Monday morning

### The Critical, reproduced on the parent

`scratchpad/probe_monday2.py`. Both legs go through the real `release()`. One `ab_started` per
dimension at `since`, the prompt leg carrying `--factorial`; 55 settled per cell before the first
command (110 per marginal arm, above `MIN_SAMPLE`); then a second batch of proposals **already
inside the window** whose verdicts are held back and landed with `_settle` between the two
commands — overnight settlement, **no new proposals at all**. The late batch is skewed so that
counting it turns both candidates from winners into losers.

```
                first leg (Sunday)                second leg
PARENT 5e98606, second leg labelled W2 (same ISO week)
  model-first   model released 110/44             prompt released 110/44   bound   lost {}
  prompt-first  prompt released 110/44            model  released 110/44   bound   lost {}

PARENT 5e98606, second leg labelled W3 (Monday morning)  and W9 (a week later)
  model-first   model released 110/44             prompt HELD    230/44   UNBOUND  lost {prompt: [p1]}
  prompt-first  prompt released 110/44            model  HELD    230/44   UNBOUND  lost {model: [m1]}
```

One order releases a model and permanently blacklists a prompt variant; the other does the mirror.
Same data, same colony, no new traffic. That is C1'' alive, exactly as the round-3 review found it.

### The same three scenarios at `ffdf591`

```
HEAD ffdf591, second leg labelled W2, W3 and W9 — all three identical
  model-first   model released 110/44             prompt released 110/44   BOUND   lost {}
  prompt-first  prompt released 110/44            model  released 110/44   BOUND   lost {}
```

**The label decides nothing.** The second leg is bounded at its partner's recorded prefix whether
it is run in the same hour, the next morning, or a week later, and the two orders agree on the
decision, the arms, the reason and `lost_tags`. The committed
`test_the_bound_survives_the_calendar_rolling_over_between_the_two_legs` pins the same property
with a different fixture, and `test_the_first_leg_of_a_new_round_is_unbounded_however_it_is_labelled`
pins the other direction by giving week 2 the *same* label as week 1.

### The replacement, attacked

`scratchpad/probe_attacks.py`, `probe_F.py`, `probe_I.py`. All through the real `release()`.

| attack | result |
|---|---|
| **one leg judged twice before the other is judged at all** (model×2 then prompt, and the mirror) | Sound and symmetric. The leg with the lower count is bounded at the partner's *latest* judging row; the leg that ran ahead is unbounded, correctly, because nothing has happened after it. Both orders end with the same `lost_tags` |
| **a partner never judged, still open** | Unbounded, `factorial_judged: true`, no truncation. That is the first leg of the first cycle and it is right |
| **two episodes of the same dimension in flight** (M1 superseded unjudged by a marked M2 while the prompt leg runs) | `_ab_episodes` records `judgings: [0, 0]`; `_partner_bound` takes the marked successor, the window opens at *its* start, and the design's own rows fall outside it. Inconclusive, nothing blacklisted. No crash, no double-count |
| **the partner judged after this leg, then this leg re-judged in the same cycle** | The re-judged leg is unbounded (its partner is no longer ahead) and counts the new traffic. This is the pre-existing double-judge hazard — running `release` twice on one dimension in one week also double-increments `weeks` and fires `MAX_WEEKS` early. Unchanged from the parent; not introduced here |
| **a partner that was JUDGED and then closed, once the counts catch up** | See N-a below. Reproduces **identically on the parent**, so it is a residual, not a regression |

### Why it holds, as a property

Within one round the leg judged first has an equal judging count and is unbounded — it cannot
count what has not happened yet. The leg judged second sees a partner exactly one ahead and is held
to the verdict prefix that partner recorded. On the next Sunday the counts have caught up, so the
first leg of the new round is free again. The comparison is over two integers derived from the
versions ledger, so it is the same in both orders and under any label; `MAX_WEEKS = 2` bounds the
counts to `{0, 1, 2}`, which keeps the skew cases small and enumerable.

---

## 3. The three concerns the implementer raised

### Concern 1 — `remember: False` is set only on the degraded path

**Judged: correct today, and the assumption underneath it is even stronger than stated — but there
is a live gap of the same shape one step to the side.**

The implementer rests this on "a third split refuses the factorial tally". In fact
`DIMENSIONS = frozenset({"model", "prompt"})` and `FACTORIAL_DIMENSIONS = ("model", "prompt")`, so
in `_factorial_partner` the `uncrossable` comprehension is `[d for d in others if {dimension, d} !=
set(FACTORIAL_DIMENSIONS) and ...]` over a single-element `others` that always forms the factorial
pair. **`uncrossable` is empty by construction in production**; the third-split refusal is reachable
only through tests that monkeypatch a third dimension in. So a factorially-judged leg cannot have a
neighbour outside its own design, and not stamping `remember` on that path is right. The note is a
good forward-looking one and should stay.

The live gap is elsewhere: `remember` is stamped inside `if _is_factorial(state)`, so an **ordinary,
unmarked two-arm week** with a live neighbour still blacklists. The interlock normally prevents that
state at *start* time, but nothing refuses a hand-started split that appears *after* the A/B opened,
and `_neighbours` would see it. Minor follow-up (N-d).

### Concern 2 — a hand-started split has no start instant, so it overlaps any window

**Judged: yes, it is a state the system can enter and not leave — but it is not a new one, and it
costs far less than the implementer feared. Not a blocker.**

Entering it is easy (a stale `MIND_PROMPT_CANDIDATE=` / `MIND_LLM_CANDIDATE=` line left after an
experiment ends) and nothing clears it automatically, because the nightly reconciler exempts
hand-started splits by design. What it actually costs, today:

1. **No refused tallies.** The "refuses every factorial tally until a human clears it" outcome runs
   through `uncrossable`, which as shown above is unreachable with two dimensions. I reproduced the
   stale-line case end to end (`probe_F.py` scenario G): `_overlapped(prompt) = True`,
   `_neighbours(model) = ['prompt']`, and the week is still **judged and written**, degraded to
   two-arm.
2. **`_blocking_experiments` refuses to start a new A/B in the other dimension.** This is
   pre-existing — `_blocking_experiments` has always read `grid.env` — and it already required a
   human to clear the line or run `ab --abort`. Round 4 changes nothing here.
3. **A degraded two-arm week stops blacklisting** (`remember: false`). That is the fail-safe
   direction: a candidate gets retried rather than permanently rejected.

So the escape hatch is the one the interlock already demanded, and the round adds no new
unescapable state. **The concern becomes real only if a third dimension is ever added to
`DIMENSIONS`** — then a stale env line for it would silently refuse every factorial tally forever,
with no age on the line and no override. That is worth a sentence in `deploy/README.md` beside the
existing hand-start note; it is not worth blocking on.

One live wart falls out of this and is filed as N-c: on that stale-line week the row's reason says
`[factorial run, judged two-arm: no other dimension was splitting during this window]` while the
same block carries `remember: false`, which is stamped precisely *because* another dimension was
splitting. The row contradicts itself.

### Concern 3 — `LedgerRewritten` writes no row: is the refusal LOUD?

**Judged: yes. Non-zero exit and a message an operator will see. Not a silent weekly no-op.**

- `main` catches `LedgerRewritten` and calls `ap.error(str(e))`. `argparse.ArgumentParser.error`
  prints `usage: …` and `grid.weekly: error: <message>` **to stderr** and raises `SystemExit(2)`.
  Confirmed directly on this interpreter.
- The nesting is safe: the only other `except ValueError` in `main` wraps the argument checks far
  above, so the subclass cannot be swallowed on its way out.
- `deploy/systemd/grid-weekly-release.service` is `Type=oneshot` with no `Restart=`, so exit 2 puts
  the unit into `failed` and the stderr line lands in the journal. It fails once per Sunday, loudly,
  rather than quietly producing nothing.
- Pinned by `test_a_rewritten_verdict_ledger_refuses_the_week_instead_of_deciding_it`, which asserts
  `SystemExit.code == 2`, the two message fragments, that no versions row was appended, that
  `grid.env` still carries the candidate, and that the ledger really is broken. My own mutations
  R10 (refusal never fires) and R11 (plain `ValueError`, so it escapes as a traceback) are both RED.

The livelock trade is real and I agree with the implementer's ruling: `weeks` does not advance
while a rewritten fossil record goes unrepaired, and the interlock keeps blocking. The difference
from round 2 is that this one announces itself every week and the message names the two remedies
("Restore the ledger from the receipts, or abort both legs with `grid.weekly ab --abort
--dimension <d>`"). A silent refusal would have been the round-2 failure; this is not that.

---

## 4. The ledger-rewrite refusal

**The chain is genuinely verified, not compared as a stored string.** `_verdict_prefix` walks the
prefix from `GENESIS` and recomputes `chain_hash(prev, row)` for each row, comparing it to the row's
own stored `chain` before advancing — the identical method `Ledger.verify` uses over the whole file
— and only then compares the final value to the recorded head. It names the first row that fails.
My mutation **R6** (restore the round-3 behaviour: take `prefix[-1]["chain"]` and compare it to the
head) is **RED**, killed by `test_the_prefix_check_recomputes_the_chain_rather_than_trusting_it`,
whose fixture edits a row's `births` field in place and leaves every stored hash untouched.

**No binding verdict is written into a ledger just judged untrustworthy.**
`_refuse_untrustworthy_ledger` is called in `release()` *before* `_judge_ab` and before any
`_ledger()` append on that path; it raises, so `ab_arms` is never called, no row is constructed, and
`flip`/`end_ab` are never reached. The committed test proves it from the outside (row count
unchanged, `grid.env` unchanged, exit 2).

Two residuals, both Minor, both filed below:

- The `env_incumbent != state["incumbent"]` branch **returns before** the refusal, and the row it
  writes carries `verdict_window=_window(cfg.verdicts_dir)` — a fresh prefix and stored head taken
  over the rewritten file. Narrow (both faults at once), but it stamps a "trusted" baseline onto a
  file the gate would otherwise refuse.
- The refusal only fires for a factorial leg whose partner is one judging ahead. An **ordinary
  two-arm week** over a rewritten verdict ledger is still measured silently: nothing in
  `grid/weekly.py` ever calls `Ledger.verify()` on `verdicts.jsonl` (the only `.verify()` call in
  the gate is `status`'s check of the *versions* ledger). Pre-existing, and outside what N3 asked
  for, but the fossil-record argument the new docstring makes applies just as well to that week.

---

## 5. The neighbour predicate

Three cases, run end to end through `release()` (`scratchpad/probe_F.py`):

| case | result |
|---|---|
| **ordinary two-arm week, no neighbour** — a marked model leg, no prompt episode, `grid.env` carrying no prompt candidate | `_neighbours(model) == []`; the block carries **no** `remember` key; `reason` starts `colony A/B lost`; `lost_tags(model) == {'m1'}`. **Blacklists, as it must.** |
| **confounded week** — same leg, same numbers, a hand-started `MIND_PROMPT_CANDIDATE=p1` line live | `_overlapped(prompt) == True`, `_neighbours(model) == ['prompt']`; block carries `remember: false`; `lost_tags(model) == set()`. **Does not blacklist.** |
| **inconclusive at the two-week cap, confounded** | The committed `test_an_inconclusive_at_the_cap_is_remembered_even_when_confounded` drives all three reason prefixes with `remember: false` and asserts the tag is remembered for `colony A/B inconclusive after 2 weeks` and forgotten for `lost`/`stuck`. My mutation **R19** (widen the exemption to every matched prefix) is **RED**, and **R18** (ignore the flag entirely) and **R16/R17** (always/never stamp) are RED too. **The livelock guard still works.** |

The `lost_tags` change reads an explicit `remember` key, never the reason prose, as ruled.

---

## 6. Are the 9 new tests non-vacuous?

**Yes. 25 of my own mutations, all RED** (`scratchpad/mutate.py`), written from the properties
rather than from the implementer's table, applied one at a time to the clean export and reverted
between runs. Each is first run against a 19-test subset; a survivor would be escalated to the full
`tests/test_weekly.py`. None survived.

| # | mutation | result |
|---|---|---|
| R1 | the bound applies on `>=` (so the first leg of a new round is bound too) | RED |
| R2 | the bound applies whenever the partner has any judging row | RED |
| R3 | the bound never applies | RED |
| R4 | episodes stop counting judgings | RED |
| R5 | the two judging counts are compared the wrong way round | RED |
| R6 | the chain is trusted, not recomputed (round 3's behaviour) | RED |
| R7 | a missing chain head skips the check | RED |
| R8 | a prefix LONGER than the ledger is accepted | RED |
| R9 | `rows: 0` is read as "no bound" | RED |
| R10 | the ledger-rewrite refusal never fires | RED |
| R11 | the refusal escapes as a plain `ValueError` (traceback, not a usage error) | RED |
| R12 | `_overlapped` is ledger-only again | RED |
| R13 | `_overlapped` reads env only, ignoring the ledger | RED |
| R14 | `_overlapped` asks "open NOW" again | RED |
| R15 | `_neighbours` looks at this dimension too | RED |
| R16 | every degrade is exempted from `lost_tags` | RED |
| R17 | no degrade is ever exempted | RED |
| R18 | `lost_tags` ignores the `remember` flag | RED |
| R19 | the exemption swallows the inconclusive-at-max memory | RED |
| R20 | an abandoned partner is not truncated where it stopped | RED |
| R21 | an episode state carries no end instant | RED |
| R22 | a closing row does not count as a judging | RED |
| R23 | a torn ledger is reported as rows removed | RED |
| R24 | the row stops saying the partner was abandoned | RED |
| R25 | the abandoned clause is dropped from the reason | RED |

### The seven dropped round-3 mutations, checked individually

The report drops H5, H6, H10, H11, H12, H13 and H17 as "superseded by a round-4 mutation that
removes the same property at its new home". I did not take that on trust; I re-aimed each at the
code as it now stands and ran it.

| dropped | claimed successor | my re-aim | result |
|---|---|---|---|
| H5 the recorded chain head is not checked | J11 / J12 | R6 (trust the stored chain) and R7 (skip on a falsy head) | **both RED** |
| H6 a prefix longer than the ledger is accepted | J13 / J14 | R8 — the `n > len(rows)` guard still exists, so H6 is *not* actually superseded; I ran it verbatim | **RED** — still covered, so the drop is harmless even though the stated mapping is wrong |
| H10 the bound ignores which cycle | J2 | R2 | **RED** |
| H11 the bound is never applied | J3 | R3 | **RED** |
| H12 `release` passes a fixed week | J1 | genuinely inapplicable — `week` is gone from `_judge_ab` and from `_factorial_partner`; the nearest live property is R1/R4/R5 | **RED** |
| H13 the third-dimension check asks "open now" | J6 / J7 | R14 (verbatim, at `_overlapped`'s new body) | **RED** |
| H17 `_episode_span` reports the closing row as the judging row | J5 / J19 | R22 (a closing row does not count as a judging) and R21 (no end instant) | **both RED** |

**Nothing was quietly abandoned.** The one inaccuracy is bookkeeping: H6's stated successor
(J13/J14, the `rows: 0` and non-integer-count cases) is not the mutation that covers it — the
torn-ledger pair J15/J16 and the surviving length guard are — but the property is still pinned, so
this is a wrong label in a table, not a hole in the suite.

### The re-aim, checked specifically

Round 4 claims one new re-aim, **F10** ("the degraded row stops saying it was not judged
factorially"). At head the degraded block is constructed as `{"factorial_judged": False}` before the
`_is_factorial` branch rather than returned as a literal at the end, so the original mutation no
longer has its old site. I ran both plausible re-aims — drop the key entirely, and set it to `True`
— plus a scope probe and the `until_verdicts` seam. (Round 3's F5 re-aim was checked and accepted
in the previous review and is unchanged here.)

| # | mutation | result |
|---|---|---|
| F10a | the degraded row drops `factorial_judged` entirely | RED |
| F10b | the degraded row CLAIMS it was judged factorially | RED |
| S5 | the CLI prints the rewrite and exits 0 instead of `ap.error` | RED |
| S6 | `remember` is stamped `True` rather than `False` | RED |
| S7 | the neighbour exemption is widened to a NON-factorial two-arm week | **GREEN — by design, see below** |
| S8 | `until_verdicts` is not passed to the factorial tally | RED |

**F10's re-aim is genuine**: both readings of it are RED, so the property F10 protected — a degraded
row saying what it actually computed — is still pinned at its new site, and the re-aim was not a
reclassification of a dead mutation.

**S7 is not a fault I injected; it is the scope probe for N-d**, and its survival is the finding.
Widening `remember: false` to cover an ordinary unmarked two-arm week with a live neighbour passes
the whole of `tests/test_weekly.py`. Nothing in the suite decides whether the exemption should be
marker-scoped or window-scoped, so if that scope is deliberate it is not defended anywhere, and a
future widening (or narrowing) would go unnoticed.

---

## 7. Regression

**Differential, parent vs head, both dimensions** (`scratchpad/diffreg.py`). 60 seeded random
scenarios × 2 dimensions over the public two-arm surface, plus 10 end-to-end **ordinary,
non-factorial** A/B weeks driven through the real `release()`.

Each random scenario draws 0–70 rows per arm from all seven `tier` values including every
`EXCLUDED_TIERS` member, ~10 % placed outside the window, ~8 % with no `model_alias` and ~6 % with
no `prompt_variant`, rejects with reasons drawn from `unknown opcode` / `deferred 5 times` / `None`
/ `bad json`, and ~2 % with an unrecognised `outcome`. For each it dumps, for both dimensions:

`ab_arms` (full per-arm tally) · `ab_decide` · `short_arms` at `MIN_SAMPLE` **and** at 1 ·
`is_stuck` at two clocks · `is_validity_loss` · `ab_state` · `lost_tags` in both dimensions

over a hand-built `versions.jsonl` holding an `ab_inconclusive` week, a closing `held` row whose
reason is `colony A/B lost` (the `lost_tags` path), and two further `ab_started` rows opened while
one was still open. The end-to-end weeks cover a clean loss, a clean win, a short week, a
near-tolerance week and a dead-candidate week, and compare the decision, the full reason string, the
whole `evals` block, `lost_tags`, and `grid.env` after the flip.

```
diff reg_par.json reg_head.json  ->  480 lines, ALL OF ONE KIND:
     120 x  >    "judgings": N,
     120 x  >    "ended_ms": null,
```

**No two-arm behaviour changed in either dimension.** The only difference anywhere is the two new
keys `ab_state` now returns (60 seeds × 2 dimensions × 2 keys = 240 added lines, 480 diff lines).
Every tally, decision, `short_arms`, `is_stuck`, `is_validity_loss`, `lost_tags`, reason string and
post-flip `grid.env` is byte-identical. `until_verdicts` still defaults to `None`, which
`_verdict_prefix` returns unchanged, so every two-arm caller is untouched by the new chain
recomputation as well.

One consequence worth naming: `_ab_view` returns `{**state, "arms": arms}`, so `judgings` and
`ended_ms` now appear in `grid.weekly ab` and `grid.weekly status` output. Cosmetic, and no test or
deploy file reads them, but it is a change to a printed interface that the README does not mention.

---

## 8. New defects

### Important (carry as documented follow-ups — none blocks this merge)

**N-a. A partner that was JUDGED and then closed stops bounding once the judging counts catch up,
so a later leg is tallied over a window in which the named partner had stopped splitting.**
`_judge_ab` truncates `until_ts` at `ended_ms` only on the `judging_row is None` branch — the
abandoned-unjudged case N4 fixed. A partner that was judged, decided and closed leaves
`judging_row` set, so once this leg's own judging count catches up the bound simply stops applying
and the window runs to now. Reproduced (`scratchpad/probe_I.py`):

```
W1 prompt  released        p0=125 p1=125   -> the prompt A/B closes, p1 goes live
W1 model   ab_inconclusive m0=240 m1=10    bounded at the prompt row  (model stays open)
W2 model   RELEASED        m0=340 m1=110   until_ts None, factorial_with {prompt: p1}
           reason: candidate rate 0.491 >= 0.249 [factorial marginal, pooled over prompt]
```

Week 2's model verdict is binding, claims a marginal pooled over a prompt split that ended a week
earlier, and crosses `MIN_SAMPLE` only because it counted 200 rows of post-partner traffic. The
marginal itself is not biased — odin salts the two dimensions independently, so the surviving cells
stay balanced — so this is a wrong *claim* and an inflated *sample*, not a wrong rate. **The output
is byte-identical on the parent**, so it is a pre-existing residual that round 4 did not widen, and
it is the same shape N4 fixed one branch of. The fix is small and the machinery is already there:
truncate `until_ts` at `other_state["ended_ms"]` whenever the partner's split ended inside this
leg's window, judged or not, and say so on the row.
*Follow-up. Does not block: pre-existing, no order-dependence, no biased marginal.*

### Minor

**N-b. The third-dimension refusal that N2 fixed is unreachable in production.** With
`DIMENSIONS == {"model", "prompt"}` and `FACTORIAL_DIMENSIONS == ("model", "prompt")`, the
`uncrossable` comprehension in `_factorial_partner` can never be non-empty; it is exercised only by
tests that monkeypatch a third dimension in. The N2 fix is still worth having — `_neighbours` is
built on the same env-aware `_overlapped` and *is* live — but the docstring and the README read as
though the refusal is a running guard, and it is currently latent. Worth one sentence saying so.
*Follow-up.*

**N-c. On a stale-env-line week the row contradicts itself.** `reason` says `[factorial run, judged
two-arm: no other dimension was splitting during this window]` while the same block carries
`remember: false`, which is stamped precisely because `_neighbours` found one splitting. The
degrade reason comes from `_factorial_partner`, which is ledger-only, while `remember` comes from
the env-aware `_neighbours`. Given this round's own rule — "a row never claims what it did not
compute" — the degrade reason should name the env-only neighbour the way `_blocking_experiments`
does ("`p1`, hand-started: no ledger row"). One line.
*Follow-up.*

**N-d. `remember` is stamped only inside `if _is_factorial(state)`.** An ordinary unmarked two-arm
week with a live neighbour still blacklists. The interlock prevents this at start time, but nothing
refuses a split that appears after the A/B opened. The predicate the round settled on — "was any
other dimension actually splitting live traffic during this leg's window" — does not depend on the
marker, so the stamp arguably belongs outside the branch. Whichever scope is intended, it is
undefended: my mutation S7, which widens the exemption to unmarked weeks, passes the entire suite.
*Follow-up.*

**N-e. The relabelled-incumbent branch escapes the rewrite refusal and records a prefix over the
rewritten file.** `release()` returns from the `env_incumbent != state["incumbent"]` branch before
`_refuse_untrustworthy_ledger` runs, and the row it writes carries `verdict_window=_window(...)` —
a row count and a *stored* chain head taken over a ledger the gate would otherwise refuse. Narrow
(both faults at once), and no tally is taken, but it writes the baseline a future prefix check will
trust. Moving the refusal above the branch is a one-line change.
*Follow-up.*

**N-f. `_overlapped` can now raise where it used to be total.** It calls
`_dimension_env(dimension)["candidate"]`, which raises `ValueError` for a dimension present in
`DIMENSIONS` but absent from `_DIMENSION_ENV`. Before this round `_overlapped` read the ledger only
and could not fail. `main` catches only `LedgerRewritten`, so such a dimension would take the Sunday
down with a traceback rather than a usage error or a degrade. Failing loud is defensible; failing
with a traceback is not the house style, and `_dimension_env`'s own docstring argues for a loud
refusal, not an uncaught one.
*Follow-up.*

**N-g. Three docstrings still describe the deleted calendar rule.** `grid/weekly.py:911`
(`_episode_span`: "its `week` (which judging cycle it belongs to)"), `grid/weekly.py:1047`
(`_partner_bound`: "`week` says whether that verdict belongs to the cycle now being judged (a
partner judged LAST Sunday bounds nothing …)"), and `tests/test_weekly.py:2830` (the `_judge`
helper: "`week` is load-bearing: the partner's judging row bounds this leg only when that row
belongs to the SAME judging cycle"). All three are now false, and the third will actively mislead
the next person writing a factorial fixture — the round's own new tests deliberately pass mismatched
labels to prove the opposite. Three edits.
*Follow-up.*

**N-h. `ab_state` gained two keys that surface in `grid.weekly ab` / `status` JSON.** Via
`_ab_view`'s `{**state, "arms": arms}`. Harmless, undocumented.
*Follow-up.*

**N-i. An ordinary two-arm week over a rewritten verdict ledger is still measured silently.** The
gate never calls `Ledger.verify()` on `verdicts.jsonl`; the new check fires only where a partner's
recorded prefix exists to compare against. The argument the new `LedgerRewritten` docstring makes
("the colony's own evidence has changed underneath a measurement already recorded") applies to any
week that reads the file. Out of scope for N3 as written, but the natural next step.
*Follow-up.*

---

## Recommendation

**Merge.** No Critical, no Important that must block.

The Critical is genuinely dead and the fix is better than the one I specified: deleting the
calendar rather than re-anchoring it removes the whole class, needs no operator discipline, and I
could not construct a labelling — same, adjacent or a week apart — that separates the two legs. The
four Importants are each addressed at the right level, with the chain now recomputed the way
`Ledger.verify` does it, the rewrite refused before anything is written and refused *loudly*, the
abandoned partner truncated where it stopped, and the confounded degrade forgetting exactly the two
prefixes it should while the livelock guard stays intact. All four minors are done. Two-arm
behaviour is byte-identical over 60 randomised scenarios in both dimensions plus 10 end-to-end
ordinary weeks; the only difference anywhere is two new keys on `ab_state`. Twenty-five independent
mutations are RED, including every one of the seven dropped round-3 entries re-aimed at its new
home, so the drops are real supersessions rather than quiet abandonment.

What is left is a residual (N-a) that reproduces identically on the parent, a latent branch (N-b),
and a handful of one-line tidyups — the self-contradicting reason (N-c), the marker-scoped
`remember` (N-d), the relabelled branch that skips the refusal (N-e), a raise that escapes as a
traceback (N-f), and three docstrings that still talk about a week key that no longer exists (N-g).
None of them changes a verdict, none is order-dependent, and none touches the permanent rejected
list in the unsafe direction. Carry them.

If only one is taken before merge, take **N-g** — a test helper whose docstring tells the next
maintainer the opposite of what the code now does is the cheapest way to lose this fix again.
