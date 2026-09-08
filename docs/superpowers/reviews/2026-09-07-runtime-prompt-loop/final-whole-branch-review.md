# Final whole-branch review — `feat/prompt-loop` (67a464e..ffdf591, 21 commits)

Reviewer: final whole-branch reviewer. Date: 2026-09-08.
Verified against a clean `git archive ffdf591` export in a scratch directory; the worktree
was clean before and after and no tracked file was modified.
Suite re-run by me on the worktree: **445 passed, 4 skipped in 121.7 s** under `-W error`.
`tests/test_deploy_units.py`: 27 passed, **0 skipped** — the readiness-probe tests genuinely
execute on this box.

Everything below that says "reproduced" was executed, not reasoned. The probe scripts live in
the session scratchpad; each finding names the command shape that produced it.

---

## 0. Verdict

The machinery is sound and the statistics are honest. The refactor that generalised a two-arm
model A/B into a dimensioned, optionally-factorial one did not break the model path, the
fossil record is still the only evaluator, and the four review rounds Task 6 spent on
order-independence bought something real: I judged the same two-Sunday factorial data in both
command orders and got identical binding verdicts with nothing blacklisted.

It does not merge today. **The single documented command for judging a prompt A/B does not
work** — `grid.weekly release --dimension prompt` exits 2 with a usage error — which makes the
whole new feature undeliverable through its own documentation, and makes the second leg of
every factorial run unreachable. That is roughly a two-line fix and an untested code path, and
it is the strongest possible argument that the suite's 445 tests are shaped around the
functions rather than around the operator.

The second blocker is the recurring class, one more time: after a model A/B *wins* and the
relay restart fails, the release is applied to the world and never recorded in the ledger, and
the gate then reports `unchanged` at exit 0 **forever** while `status` disagrees and `rollback`
is bricked.

---

## 1. Does the whole thing hold together? — one life cycle, traced in code

### 1a. A prompt variant, end to end (executed)

| Step | What I ran | What happened |
|---|---|---|
| variant added | `load_registry()` on the shipped registry | `v1-sandbox` (default) and `v2-minimal-edit` load; hashes match; templates are LF-pinned by `.gitattributes` |
| incumbent resolved | `_live_arm(cfg, "prompt")` with a blank `MIND_PROMPT_VARIANT` | `v1-sandbox` — the same id odin logs, so the gate's incumbent and the log's label are one string |
| experiment starts | `release(..., dimension="prompt", candidate="v2-minimal-edit")` | `ab_started`; `grid.env` gains `MIND_PROMPT_CANDIDATE=v2-minimal-edit` and `MIND_PROMPT_AB_SPLIT=0.2`; relay restarted |
| relay splits traffic | `assign(..., dimension="prompt", two_arm(...))` over 2000 ids | 387 candidate / 1613 incumbent ≈ 0.194 — candidate leads the arms list, salted independently of the model hash |
| proposals logged | `proposal_entry` | `prompt_variant` + `prompt_variant_sha256` beside `model_alias`; the two hashes are named apart and `variant_of()` is the only reader |
| nightly settles | `ab_check` → `ab_reconcile` → `is_stuck` | resting state repaired first, then liveness; per dimension |
| Sunday judges | `release(..., dimension="prompt")` | `released`, `candidate rate 0.250 >= 0.116 (incumbent 0.129 x 0.9)` |
| winner flips | `_flip_local` | `MIND_PROMPT_VARIANT=v2-minimal-edit`, candidate cleared, relay restarted; `_live_arm` now returns the winner |
| loser remembered | a losing run | `held`, `colony A/B lost: 0.050 vs 0.250`, `short_arms: []`, `lost_tags(prompt) == {v2-minimal-edit}`, `lost_tags(model) == set()`; next Sunday `held` with retest advice naming `--dimension prompt` |
| answer-quality floor | 25 rejects + 3 valid on the candidate | `colony A/B lost: v2-minimal-edit validity 0.11 below 0.5 over 28 answered` — decided **before** the settled floor, with `short_arms: []` so the loser is blacklisted. The spec's "hours, not weeks" claim is real. |

The cycle holds. **Except that the "Sunday judges" step above was driven by calling
`release()` directly.** Through the CLI, which is the only route an operator has, it fails —
see C1.

### 1b. A factorial run across two Sundays (executed, both orders)

Started the model leg, then the prompt leg with `--factorial`, seeded four cells at 40 settled
each, judged; then added 120 more per cell and judged again.

```
ORDER (model, prompt)                      ORDER (prompt, model)
Sun1 model  ab_inconclusive  factorial=T   Sun1 prompt ab_inconclusive  factorial=T
Sun1 prompt ab_inconclusive  factorial=T   Sun1 model  ab_inconclusive  factorial=T
Sun2 model  released         factorial=T   Sun2 prompt released         factorial=T
Sun2 prompt released         factorial=T   Sun2 model  released         factorial=T
lost model: {}  lost prompt: {}            lost model: {}  lost prompt: {}
live: kannaka-brain-v4 / v2-minimal-edit   live: kannaka-brain-v4 / v2-minimal-edit
```

Identical binding outcomes in both orders, both legs genuinely judged on their marginal on the
second Sunday, the week counter reaching 2, both episodes closing, the interlock clearing, and
the interaction reported `advisory: True, estimate 0.0` and read by nothing. The deleted
12-hour and ISO-week proxies really are gone: the bound is the partner's recorded
`verdict_window` prefix, and it is re-derived over the hash chain rather than string-compared.

**Where the seams are weakest, in order:**

1. **The CLI is the seam.** Every dimensioning rule was implemented twice on purpose — once in
   `release()` for "every other caller" and once in `main()` for "the argument layer". Six of
   those pairs agree. The seventh, the candidate-presence rule, does not (C1), and it is the
   one an operator meets first. The programme's own discipline — "the CLI is not the only
   caller" — produced the duplication that then diverged.
2. **`_restart_relay`'s boolean.** The readiness gate made `systemctl restart`'s exit code
   truthful, which is a genuine advance. But every path still collapses that into one of two
   strings, and `_restart_after_revert` throws its answer away entirely (see 3b).
3. **The leg judged second is measured over strictly less data.** By design and correctly, but
   in my fixture that produced a candidate arm at `0/80` on Sunday 1 in one order and `80/80`
   in the other. Both read inconclusive so nothing binding differed, and real interleaved
   traffic will not look like my fixture — but the README's "neither their order nor the delay
   between them changes the verdict" is true of the *verdict*, not of the *numbers on the row*,
   and a reader comparing two rows will notice.
4. **The registry is a fourth place state lives.** `_live_arm` resolves a blank
   `MIND_PROMPT_VARIANT` through `get_variant()` on every call, so editing the registry's
   `default` key silently redefines what the gate believes the incumbent is. This is handled
   safely (the "incumbent label changed mid-A/B" branch fires, no blacklisting, nightly
   warning) — but it means an experiment's identity depends on a file nobody thinks of as
   configuration.

---

## 2. The recurring bug class: every remaining way the three can disagree

State lives in four places, not three: **`grid.env` (A)**, **the running relay process (B)**,
**the versions ledger (C)**, and — for `model` only — **the `kannaka-brain-current` alias on
debain2 (D)**.

| # | Disagreement | Would an operator find out? |
|---|---|---|
| 1 | **B vs A at rest.** Nothing ever compares the running relay to `grid.env` except at the instant of a restart. Edit `grid.env` and don't restart, and the relay serves the old values indefinitely; `ab`, `ab --check` and `status` all read the file, never the process. | **No.** Only by eye, via `journalctl -u grid-odin \| grep 'prompt A/B:'`. The readiness check runs on `ExecStartPost` only. This is the last open leg of the class and the ledger already files the right fix (have the relay publish what it loaded). |
| 2 | **C vs the world after a `flip_incomplete` that applied.** Model A/B wins, `ollama cp` succeeds, `MIND_MODEL_ALIAS` is written, restart fails twice → `flip_incomplete`. The reconciler then writes `held (reconciled)`. No `released` row is ever written. | **No.** Exit 0 every subsequent Sunday, with the reason `<tag> is already the live model incumbent`, which reads as "nothing to do". `status` reports the superseded version. `rollback` refuses or targets the wrong tag. **See C2 — reproduced.** |
| 3 | **D vs A and C.** Nothing re-reads `kannaka-brain-current` after `flip` sets it. A hand `ollama cp` on debain2, or a copy that landed on the wrong tag, is invisible. `find_candidate` reads `/srv/rogue/current-model`, a different file. | **No.** Pre-existing, out of scope, but it is the fourth leg and worth a beads row. |
| 4 | **A splitting, C silent (hand-started).** The reconciler deliberately leaves it alone and logs one WARNING. | **Half.** `ab` reports it honestly as `"source": "env"`. `ab --check` prints **`no A/B running`** and exits 0 while 20 % of live traffic goes to an untracked candidate — reproduced — and the README's exit-code table endorses that reading. See I2. |
| 5 | **The split *value*.** `start_ab` writes `0.2` into `grid.env` and records `split: 0.2` on the row. Edit `MIND_AB_SPLIT` to 0.5 and restart, and the relay splits 50/50 while the ledger row still claims 0.2. | **No.** The *verdict* stays honest — assignment is per-request and logged — so only the recorded design is wrong. Minor. |
| 6 | **The registry `default` vs a blank `MIND_PROMPT_VARIANT`.** Changing `default` mid-experiment redefines the incumbent's label. | **Yes, and safely.** `release()` takes the "incumbent label changed mid-A/B" branch, records `short_arms: [candidate, incumbent]` so nothing is blacklisted, and the nightly warns every night until `MAX_WEEKS` closes it. This leg is genuinely closed. |
| 7 | **A duplicated key in `grid.env`.** `_env_get` returns the first match; systemd's `EnvironmentFile` gives the relay the last. | **No.** Very minor; `_env_set` rewrites every match, so it self-heals on the next write. |

**Summary:** the reconciler closed A↔C and the readiness check closed A↔B *at a transition*.
What remains open is A↔B **at rest** (#1), C↔world after an applied-but-unrecorded flip (#2),
and D entirely (#3). Of those, #2 is new on this branch and is a blocker; #1 and #3 are honest
follow-ups that should be written down as such rather than left to be rediscovered a sixth
time.

---

## 3. The parked queue, ranked and adjudicated

### 3.1 The two you named

**(g) After a `flip_incomplete` the gate settles into permanent `unchanged` at exit 0 while
`status` disagrees. — MUST FIX BEFORE MERGE.**

Reproduced end to end, and it is worse than filed:

```
S1  ab_started            probes passed; colony A/B started
S2  flip_incomplete       alias serves the candidate; relay restart failed twice
      -> remote ran `ollama cp kannaka-brain-v4 kannaka-brain-current`
      -> grid.env: MIND_MODEL_ALIAS=kannaka-brain-v4, MIND_LLM_CANDIDATE=
S3  held                  colony A/B reconciled: ... candidate cleared and the relay restarted
S4  unchanged             kannaka-brain-v4 is already the live model incumbent      exit 0
S5  unchanged             kannaka-brain-v4 is already the live model incumbent      exit 0   ...forever
LAST_RELEASED(model): kannaka-brain-v3        LIVE (grid.env): kannaka-brain-v4
rollback -> SystemExit: "the previous release was never released; refusing"
```

Three consequences, only the first of which was filed:

1. Every subsequent Sunday is a silent no-op at exit 0 with a reason that reads as benign.
2. `status` permanently reports the superseded version as released.
3. **`rollback` — the emergency lever — is bricked.** `_last_released` returns the *old*
   release row, so the default target is that row's `previous`. In my ledger that name was
   never released and `rollback` refuses outright; in a longer ledger it would roll back two
   steps, skipping the version actually in service, and stamp `rollback from kannaka-brain-v3`
   on a row while the live alias says v4.

I ran the same scenario on the base commit `67a464e`: S4 there is **`ab_started`**, not
`unchanged`. So the permanent-quiet half is **introduced by this branch**, by Task 4's I1 guard
(`cand["tag"] == _live_arm(...)` → `unchanged`). That guard is correct in itself — it stops the
gate starting an A/B against itself and bricking the relay — but it converted a loop that
eventually recorded a release into one that never does. The rollback breakage is pre-existing
and reproduces on base; this branch makes it permanent instead of eventually self-repairing.

Minimum acceptable fix: that `unchanged` branch must distinguish "already released, nothing to
do" from "live but never recorded as released". When `cand["tag"] == _live_arm(...)` and
`cand["tag"] != released`, the ledger and the world disagree: say so in the reason and exit
non-zero. Better: have the reconciler close the gap by recording the release it can prove
happened. Either way it must not stay exit 0 with a benign sentence.

**(e) A variant-registry failure during a Sunday run raises rather than recording a row. —
MUST FIX BEFORE MERGE, and the prescribed shape is too narrow.**

Reproduced. `_live_arm` calls `get_variant()` (weekly.py:216), so a registry that will not load
raises `VariantError` out of a prompt judging Sunday. `main()` catches only `LedgerRewritten`,
so it is an uncaught traceback: no ledger row, exit 1, unit failed with a Python stack.

The part not filed: **the nightly breaks too.** `ab --check` loops `sorted(DIMENSIONS)`; `model`
reconciles, then `prompt` raises out of the loop. So:

- the prompt dimension is never reconciled — precisely when the relay is down and `grid.env`
  is most likely to have diverged;
- any repair the model dimension made is written to the ledger but never printed, and the
  "loudest exit code" contract is replaced by exit 1.

Adopt the prescribed shape (catch `VariantError` only, record a non-decision row that cannot
count toward the week cap or blacklist anything, exit 3) **and apply it to `ab --check`'s
per-dimension loop as well**, so one dimension's registry fault cannot silence the other's.

### 3.2 (3b) The carried item — checked, not assumed

**(f) Can the ledger now distinguish a relay that came back from one that did not, after a
revert? — PARTIALLY. The ledger leg is NOT closed.**

What the readiness check *did* close: `grid-odin.service` now carries
`ExecStartPost=relay-ready.sh`, which waits for the `watching` line (emitted only after every
fail-closed gate), checks the logged `A/B:` and `prompt:` lines against the values this unit's
`EnvironmentFile` handed it, anchored at both ends, scoped to `_SYSTEMD_INVOCATION_ID`, and
re-checks liveness afterwards. So `sudo -n systemctl restart grid-odin.service` — and therefore
`_restart_relay`'s return value — is now *truthful*. That is real and it is the load-bearing
half.

What is still open: `_restart_after_revert` computes the answer and **throws it away**. It logs
`log.info("relay restarted on the values restored after %s")` or
`log.error("... the unit may be DOWN")` and returns `None`; both callers (`_flip_local`,
`start_ab`) ignore it, and the row is byte-identical either way — `flip_incomplete`, with no key
recording whether the relay came back.

So a reader of `versions.jsonl` alone still cannot tell the two apart. An operator *can*, from
the journal, which is a strict improvement on the filed state. **Ruling: not closed, but now
trivial precisely because the readiness check exists.** Three lines: return the outcome from
`_restart_after_revert` and put it on the row (`relay_after_revert: "ok" | "down"`, or appended
to the reason — appended, never prepended, per `_exit_code`'s `": flip_failed"` suffix rule).
This can merge as a documented follow-up **only** if it is written down; it is the difference
between "a human must restart grid-odin" and "the colony's mind is off".

### 3.3 The rest of the queue

**Merge-blocking (must fix):**

- **(g)** and **(e)** above.

**Merge as documented follow-ups:**

| Item | Why it can wait |
|---|---|
| **(f)** relay-up/down not on the row | The journal carries it today; the fix is 3 lines. Must be *written down*, not dropped. |
| **N-g** two docstrings still describe the deleted week rule | `_episode_span` ("its `week` (which judging cycle it belongs to)") and `_partner_bound` ("`week` says whether that verdict belongs to the cycle now being judged") both describe the ISO-week proxy that was *deleted* in favour of judging counts. This is a doc-only edit but I agree with the controller: a docstring describing a rule that no longer exists is how the next person reintroduces a Critical that took two rounds to kill. Cheap enough that I would just do it. |
| **(h)** `status`'s scalars are model-only while `ab` is per-dimension | Real inconsistency, visible in my Part-1 run (after a prompt release, bare `status` still shows only model facts). Cosmetic; the per-dimension answer is one flag away. |
| **(c)** a split is validated with no candidate configured | Boot-brick *class*, but unreachable in practice: the gate only ever writes `0.2`, Skywave's `grid.env` reads `0.2`, and `MIND_PROMPT_AB_SPLIT` has no writers. It needs a human to have typed `0` or `2`, which is already fatal while an A/B runs. Fix it, but it is not a blocker. |
| **(d)** the revert-path docstring | Now largely *true* given `_restart_after_revert` and the readiness gate; fold into the (f) fix. |
| **(a)** `ab_decide` doc overclaims reading eight arm keys | Pure doc. |
| **(i)/D8** post-ready-death diagnostic reuses a pre-death journal snapshot | The check still fails correctly; only the printed excerpt omits the crash. Matters more than its severity because diagnosability is the whole point of the probe — but it is one `journalctl` re-read. |
| **N-e** the relabelled-incumbent branch skips `_refuse_untrustworthy_ledger` | A row is appended while the fossil record is known-corrupt — but that row carries `arms: {}`, so no verdict is computed from bad data, and at `MAX_WEEKS` it records `short_arms: [both]` so nothing is blacklisted. Bounded. |
| **N-f** `_overlapped` / `cell_key` can raise as a traceback | Reachable only via a missing `grid.env` (relay already dead) or a label containing `\|` (see below). |
| **N-c** a reason that contradicts the `remember` marker on the same row | See I5 — I rate this higher than "minor". It is an honesty defect, and honesty is this programme's premise. |
| *(new)* a variant id containing `\|` loads and then raises out of the first factorial Sunday | Reproduced: `load_registry` accepts `"v2\|weird"`, the relay serves it, and `cell_key` raises `ValueError` out of `release()`. One line in `_load_variant`. |
| *(new)* a non-string `default` escapes as a raw `TypeError` | Reproduced: `{"default": ["a"], ...}` → `TypeError: unhashable type: 'list'`, not `VariantError`. Directly breaches the module docstring's "nothing escapes as a raw … that a caller's `except VariantError` would miss". One line. |
| *(new)* the interaction-inertness test is single-signed | See §7. |

**Drop:**

| Item | Why |
|---|---|
| **(b)** the defer-reason pin test does not assert the pattern excludes the other five rejection reasons | The pattern is `deferred \d+ times\Z`, anchored at both ends; none of the five `Rejection` literals in `grid/proposal.py` (`no JSON array…`, `empty genome`, `genome length…`, `unknown opcodes…`, `proposal identical to parent`) begins with "deferred", so collision is impossible by construction. The near-miss that *could* collide — `"deferred to human review"` — is already in the test at `test_weekly.py:386` with its mutation stated. Adding four more assertions pins nothing new. |
| **N-d** the `remember: False` marker is undefended by any test | **Contradicted by measurement.** I deleted the `if _neighbours(...)` guard and the suite went RED (`test_weekly.py:3958`, with the mutation named in the comment). The item was filed before Task 6's round-4 fix landed and is stale. |
| **N-a** a judged-then-closed partner stops bounding once the counts catch up | The controller already established it reproduces identically on the parent. Pre-existing, and correct behaviour besides — the counts catching up is exactly what makes the next round unbounded. Not this branch's to carry. |

---

## 4. Is the honesty intact? — yes, with one blemish

**The evaluator is not a model judging itself.** Verified by reading every decision path:

- `ab_decide` reads only `settled`, `reproduced`, `rate`, `answered`, `valid`, `validity` —
  all derived from verdict rows (the colony's fossil record) and the proposal log's own
  `outcome`/`reason`. No model output reaches a verdict.
- The two probes are rule-scored (`score_identity` is regexes; `run_validity` is
  `grid.proposal.validate`), and they gate a **model** candidate only. A prompt candidate runs
  none of them, correctly, and the code says why (the weights did not change).
- `decide()` consumes two booleans. `holdout_ppl` is carried on the row and read by nothing.

**Advisory numbers are labelled and inert.** `colony_ab` (`advisory: True`),
`other_dimensions` (`advisory: True`), and `interaction` (`advisory: True` plus a reason
naming the sample problem) are all recorded and none is read by any decision. The interaction
is built *after* both verdicts are fixed, so there is no branch it could reach by accident. I
tried to make it decide: a mutation letting it override the prompt marginal is killed by
`test_the_interaction_is_reported_and_never_decides_anything` — **for a negative estimate
only**; see §7.

**No operator preference decides what the fossil record decides.** `--factorial` changes the
*method* (raw rate → marginal), never the direction; `--candidate` selects a challenger, not a
winner; `ab --forget` makes a candidate retestable and releases nothing; `ab --abort` records
the arms as they stood and decides nothing about the candidate. Blacklisting is gated on
`short_arms` and the `remember` marker, both computed, neither typed.

**Every ledger row describes what was computed — with one exception.** `factorial_judged` is on
every A/B row, including the nightly stuck row and the abort snapshot, both of which are
two-arm and say so; a degraded leg appends `[factorial run, judged two-arm: <why>]` and logs a
WARNING. That is genuinely good discipline. The exception (parked **N-c**, and I rate it
Important):

In `_judge_ab`'s degraded branch, `degraded = degraded or "no other dimension was splitting
during this window"`, and then `remember: False` is set independently from `_neighbours(...)`.
When `_factorial_partner` returns `(None, "")` but a neighbour *was* live, the row says **"no
other dimension was splitting during this window"** while carrying **`remember: False`**, whose
entire meaning is "another dimension was splitting". One row, two contradictory statements, one
for humans and one for machines. Derive the prose from `_neighbours` and they cannot diverge.

One more honesty point, from C1: on a prompt judging week the CLI *requires* `--candidate` and
then **silently ignores its value** — including a value naming the incumbent. I confirmed
`release --dimension prompt --candidate v1-sandbox` judges `v2-minimal-edit` and prints a row
saying so. "A flag a verb has no use for is REFUSED, not ignored" is this parser's own stated
rule, in a comment, twelve lines above the place it is broken.

---

## 5. The operator story — read as if it were Sunday and I had never seen this

`deploy/README.md` gained 283 lines and they are unusually good: §3a explains the readiness
gate by naming the five failure messages and what each one means, §6 explains variants, the
validity floor, the interlock, `--factorial`, the two-command judging protocol, the verdict
prefix bound, and — creditably — the two *deleted* designs and why they were wrong.

**What it tells you that is untrue of the code:**

1. **The judging command does not work.** §6 gives, twice,
   `/srv/grid/venv/bin/python -m grid.weekly release --dimension prompt`
   — once as the way to judge a prompt A/B and once as the second leg of a factorial run. It
   exits 2: `grid.weekly: error: a prompt release needs --candidate; nothing discovers one`.
   This is C1. On Sunday, following the documentation, I would be stuck at the first step of
   the feature and would have no way to know that adding a meaningless `--candidate` unblocks
   it.
2. **The exit-code table blesses a false statement.** "`0` — nothing needed doing (no A/B
   running…)". With a hand-started split live, `ab --check` prints `no A/B running` and exits 0
   while 20 % of traffic goes to an untracked candidate (reproduced). The WARNING is one line
   above in the journal; the *conclusion* is wrong.
3. **`grid.env.example` ships with `MIND_LLM_URL` commented out**, i.e. the documented default
   is the NATS route — on which odin logs `A/B: none` even with `MIND_LLM_CANDIDATE` set, so
   `relay-ready.sh` fails the start and a model A/B can never begin. The message the operator
   gets ("the relay came up on a configuration this unit did not hand it") points at the wrong
   cause. See I3.

**What is essential and missing:**

- Nothing tells you what to do when the *registry* fails to load during a Sunday or a nightly.
  Today you get a Python traceback out of a systemd timer (see (e)). §3a covers the relay's
  refusal; nothing covers the gate's.
- Nothing says a bus outage longer than `RELAY_READY_TIMEOUT_S` at flip time will discard a won
  A/B: `_restart_relay` reports failure, the gate reverts, and the week's verdict is thrown
  away even though the relay comes back on its own moments later. §3a documents the kill-retry
  cycle honestly; it does not connect it to the gate's decision.
- Nothing says `MIND_PROMPT_AB_SPLIT=0.2` is left behind in `grid.env` after a prompt win.
  (Harmless — the relay ignores a split with no candidate — but it looks like a live A/B to a
  reader.)
- No single "is this healthy?" command is named.

**Could I tell from the outside whether an experiment was healthy?** Mostly yes, and this is a
real improvement: `grid.weekly ab --dimension prompt` prints, per arm,
`answered / valid / unanswered / validity / settled / reproduced / rate` — which is exactly the
set you need to distinguish "the wording is bad" from "the gateway is down" from "it is just
early". The receipts script's `6-prompt-state.txt` adds the registry state and the live variant.
The gap is the contradiction in #2 above: the display is honest about a hand-started split and
the check is not, and the check is the one wired to a timer.

---

## 6. Anything genuinely dangerous on a host serving live traffic

| | Path | Assessment |
|---|---|---|
| **Bricks the relay** | The gate writing a value odin refuses at boot | **Closed, and well.** `_validate_candidate` (twice — CLI and `start_ab`), the equal-arms refusal (twice — `Relay.__init__` and `start_ab`), `_split`'s range check, `_env_restore`'s absent-vs-blank rule, and now the readiness gate making a failed start visible. I could not construct a gate-written brick. |
| | `_flip_local` writes the winning variant without re-validating it | If the variant were removed from the registry between the start Sunday and the judging Sunday, the flip writes a name the loader refuses. But the revert restores both lines and `_restart_after_revert` brings the relay back on values known to work. Self-healing. **Minor**, one `_validate_candidate` call from a clean refusal. |
| | The readiness gate itself, during a long NATS outage | The relay is now *killed and restarted* roughly every 70 s instead of sitting in its own backoff. Documented honestly in §3a, never trips `StartLimitBurst`, recovers when the bus returns. Accepted, but it is a real behaviour change to a live daemon and it discards in-flight asks. |
| **Corrupts the ledger** | — | **No path found.** Both ledgers are append-only and hash-chained; `_ledger` takes the lock; `release_row` asserts the exact key tuple and refuses an unknown `decision` or `dimension`; `dimension_of` is reader-side so no historical row is rewritten. `_refuse_untrustworthy_ledger` re-derives the verdict chain from genesis before any append and refuses loudly (exit 2) rather than degrading — I confirmed the mutation that string-compares the stored chain instead goes RED. |
| **Blacklists a candidate that did nothing wrong** | | **Well defended, three ways.** `short_arms` abstains when the incumbent is short too; the validity-loss path deliberately records `[]` (killing the sub-project-3 livelock); `remember: False` exempts a two-arm week taken beside a live neighbour; and a `flip_failed` win is worded so `lost_tags`' prefix match cannot see it. I could not construct an unjust blacklisting. The one residual is C2's cousin: a *forgotten* factorial leg can be closed as stuck at 48 h and blacklisted — which the README states plainly. |
| **A confident verdict from a measurement that did not happen** | | **Closed at the counter.** `_row_arm` returns `""` for any row missing a label in *any* dimension and such rows enter no cell and therefore no marginal — mutation-confirmed RED. `ab_arms` refuses `cells=` mixed with `dimension=`, refuses a wrong-arity cell, and `ab_decide_factorial` refuses equal arms and a missing cell rather than zero-filling. `_pool` re-derives ratios from summed counts — mutation-confirmed RED. `check_sampling_route` refuses at boot a variant whose sampling the NATS route cannot carry, so two arms can never be silently identical. |
| **Bricks the emergency lever** | `rollback` after a `flip_incomplete` that applied | **Yes — see C2.** This is the most dangerous single consequence on the branch, and it is the one that is *pre-existing* but made permanent here. |

---

## 7. Test suite health at 445

Not a count problem. The load-bearing properties are pinned, and I verified that by mutation
rather than by reading — 8 targeted mutations of `grid/weekly.py`, each run against the full
`tests/test_weekly.py`:

| Mutation | Result |
|---|---|
| delete the `remember: False` marker | **RED** (retires parked N-d) |
| bucket unattributed rows instead of excluding them (phantom arm) | **RED** |
| trust the stored chain string instead of re-deriving the prefix | **RED** |
| move the validity floor *after* the sample check | **RED** |
| drop the second leg's partner bound (order-dependence returns) | **RED** |
| `_pool` averages the ratios instead of re-deriving from sums | **RED** |
| the interlock reads the ledger only, ignoring `grid.env` | **RED** |
| let the interaction decide, gated on a **positive** estimate | **GREEN — SURVIVED** |

The suite is in good shape. Two gaps:

**1. The interaction-inertness property is pinned in one direction only.** This is the
"vacuous-if-you-squint" case you asked about. `test_the_interaction_is_reported_and_never_
decides_anything` uses a single fixture whose estimate is exactly `-0.8`, and the whole suite
contains no fixture producing a positive interaction on a decision path. Proven by a
three-way sign probe:

```
RED (killed)   | negative interaction flips the prompt verdict to loss
GREEN SURVIVED | POSITIVE interaction flips the prompt verdict to loss
RED (killed)   | any non-zero interaction flips the prompt verdict to loss
```

The code genuinely does not read the interaction — this is a test-strength gap, not a live
bug — but "nothing reads it, and no caller may" is stated in the module as a hard rule, and a
guard that only fires for one sign is not a guard on the rule. Fix: assert *structurally* —
two tallies with identical marginals and interactions of `-0.8`, `0.0` and `+0.8` must return
byte-identical verdicts and reason strings. (The Task 6 review did exactly this for `-0.8` and
`0.0`; it needs one more value.)

**2. The CLI's judging path is not tested at all.** All 15 `main(["release", …])` tests either
pass `--candidate` or take the interlock branch. There is **no** test of
`main(["release", "--dimension", "prompt"])` against an open prompt A/B — which is the single
most-used command the feature will ever see, and it is broken (C1). The suite tests `release()`
thoroughly and `main()` selectively, and the seam between them is exactly where the defect sits.
Any fix for C1 must come with a `main()`-level test of the judging week, and a test that a
supplied `--candidate` on a judging week is *refused*, not ignored.

Everything else I sampled was non-vacuous and often better than required: the malformed-log-row
test states its own mutation for each of five row shapes; the deploy-probe tests *execute* the
shipped shell script against stubs rather than grepping its text (27 passed, 0 skipped here);
the tests diff for Task 6's last two rounds contains zero deletion lines.

---

## 8. Findings, ranked, with a merge call on each

### CRITICAL

**C1 — `grid.weekly release --dimension prompt` exits 2; the prompt dimension cannot be judged
through its own documented command. MERGE-BLOCKING.**
`main()` applies the candidate-presence rule whenever `blocking` is empty, without consulting
`open_here`; `release()` correctly places the same rule inside the start path and its own
comment says why ("*the mirror guard … has to stay below, because a judging week needs none*").
Reproduced: with an open prompt A/B, `main(["release","--dimension","prompt"])` → SystemExit 2,
no row written. The workaround, `--candidate <any registry name>`, is undocumented and its value
is silently ignored — I judged `v2-minimal-edit` while passing `--candidate v1-sandbox`. This
also makes the second leg of every factorial run unreachable, which is the deliverable Task 6
spent four fix rounds on. Fix ≈ 2 lines (`… and not open_here`), plus refuse a `--candidate`
given on a judging week, plus the two `main()`-level tests §7 names.

**C2 — a won model A/B whose restart fails is applied to the world and never recorded, leaving
the gate permanently quiet and `rollback` bricked. MERGE-BLOCKING.**
Reproduced above and contrasted against `67a464e`. The permanent-`unchanged`-at-exit-0 half is
introduced by this branch; the stale `_last_released` and the dead `rollback` are pre-existing
but this branch removes the path that used to repair them. At minimum the `unchanged` branch
must distinguish "already released" from "live but unrecorded", say so, and exit non-zero.

### IMPORTANT

**I1 — a `VariantError` takes down both the Sunday run and the nightly, with no row and no exit
code. MERGE-BLOCKING (this is parked item (e), whose blast radius is larger than filed).**
Apply the prescribed non-decision row, and extend it to `ab --check`'s per-dimension loop so one
dimension's registry fault cannot silence the other's repair.

**I2 — `ab --check` prints `no A/B running` and exits 0 with a hand-started split taking 20 % of
live traffic. Merge with a follow-up, but fix the wording now.**
Reproduced. The display (`ab`) is honest; the timer-wired check is not, and the README's
exit-code table endorses the wrong reading. A one-line change to the printed string
("hand-started split in grid.env, untracked; nothing to reconcile") removes the falsehood
without changing the deliberate leave-it-alone behaviour.

**I3 — the readiness gate fails closed on the shipped default configuration. Merge with a
follow-up.** With `MIND_LLM_CANDIDATE` set and `MIND_LLM_URL` unset (the state
`grid.env.example` ships), odin logs `A/B: none` while `relay-ready.sh` expects
`A/B: candidate …`, so a model A/B can never start on a bus-route host and the diagnostic blames
the wrong thing. Failing closed is the right direction — the alternative was a silently
one-armed A/B — but the probe should recognise the case and say "`MIND_LLM_CANDIDATE` is set but
`MIND_LLM_URL` is not, so the relay is not running an A/B".

**I4 — nothing compares the running relay to `grid.env` at rest. Merge as a written-down
follow-up.** The last open leg of the recurring class. The ledger already names the right fix
(the relay publishes what it loaded, which also retires D2, D3, D5 and the journald grant). It
must be on the board before merge, not carried in prose.

**I5 — a degraded factorial row can assert "no other dimension was splitting" and carry
`remember: false` simultaneously. Merge with a follow-up.** Parked N-c. Small code, but it is a
ledger row contradicting itself in a programme whose premise is that rows describe what was
computed.

### MINOR — all mergeable as documented follow-ups

- **(f)** the revert's restart outcome is computed and discarded; the row cannot distinguish
  relay-up from relay-down. 3 lines. *Must be recorded, not dropped.*
- **N-g** two docstrings (`_episode_span`, `_partner_bound`) still describe the deleted ISO-week
  rule while the code uses judging counts. Doc-only; I would just do it.
- the interaction-inertness test is single-signed (§7).
- a variant id containing `|` loads and raises out of the first factorial Sunday.
- a non-string registry `default` escapes as a raw `TypeError`, breaching the loader's own
  contract.
- **(h)** `status`'s scalars are model-only while `ab` is per-dimension.
- **(c)** a split is validated with no candidate configured (boot-brick class, no live
  exposure).
- **(a)**, **(d)**, **(i)/D8**, **N-e**, **N-f** as adjudicated in §3.3.
- `_flip_local` does not re-validate the winner before writing it (self-healing today).
- `MIND_PROMPT_AB_SPLIT` is left behind in `grid.env` after a prompt win; undocumented.
- a duplicated key in `grid.env`: the gate reads the first, the relay the last.

### DROPPED

**(b)** the defer-reason exclusion assertion (impossible by construction, near-miss already
tested); **N-d** the `remember` marker being undefended (measured RED — stale item); **N-a** the
judged-then-closed partner (pre-existing, and correct).

---

## 9. Ready to merge?

**No.** Two Criticals and one Important are blocking, and all three are small:

1. **C1** — `main()`'s candidate rule must consult `open_here`; refuse a `--candidate` given on
   a judging week; two `main()`-level tests. (~2 lines + tests)
2. **C2** — the `already the live incumbent` branch must distinguish "released" from "live but
   unrecorded", say so, and not exit 0. (~5 lines + a test)
3. **I1/(e)** — catch `VariantError`, record the prescribed non-decision row, exit 3, and cover
   `ab --check`'s loop as well. (~15 lines + tests)

I would also take **N-g** (two stale docstrings) and **I2** (the false `no A/B running` line) in
the same wave: both are one-line edits, and the first is the exact mechanism by which a Critical
that cost two fix rounds gets reintroduced.

Everything else on the parked queue can merge as follow-ups provided **(f)** and **I4** are
written onto the board rather than left in this file. Once those five land, this is a strong
branch: the statistics are honest, the fossil record is still the only judge, the advisory
numbers are labelled and inert, the factorial verdict is order-independent by construction, and
an arm that cannot answer now loses in hours.
