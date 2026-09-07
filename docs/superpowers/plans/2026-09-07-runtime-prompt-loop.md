# Runtime and prompt loop — implementation plan (sub-project 5a + 5c)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the colony's fossil record vote on the prompt, not just the model, by generalising the A/B machinery to named arms in named dimensions.

**Architecture:** A prompt *variant* is a hashed bundle of template, sampling parameters and a recall switch, loaded fail-closed from a registry. The relay assigns each request an arm per *dimension* (`model`, `prompt`) from a dimension-salted hash, so the two assignments are independent. The weekly gate starts, judges and flips an experiment in either dimension using the state machine sub-project 3 already built, plus a validity floor that fails a broken candidate in hours instead of weeks.

**Tech Stack:** Python 3.13/3.14, stdlib plus httpx. pytest with `asyncio_mode = "auto"`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-07-runtime-prompt-loop-design.md` (binding). Sub-project 3's spec, `docs/superpowers/specs/2026-09-07-colony-live-ab-design.md`, still governs the state machine this plan widens.

## Global Constraints

- **Cost $0.** No training run, no model download, no paid API. Nothing in this plan spends money.
- **The `model` dimension's arm assignment must not change.** Every request id maps to the arm it maps to today. `model` keeps the existing unsalted hash; every other dimension salts with its own name.
- **The default variant must render byte-identically** to today's `render_prompt` output. This is a test, not a claim.
- **The registry loader fails closed.** A missing template file, a sha256 that does not match its file, or a placeholder the renderer does not know is an exception at load time, never a warning and never a fallback.
- **`VALIDITY_MIN = 0.5`**, **`VALIDITY_MIN_ANSWERED = 20`**, **`MIN_SAMPLE = 100`**, **`TOLERANCE = 0.10`**, **`MAX_WEEKS = 2`**, **`AB_SPLIT = 0.2`**. Use these names and values verbatim.
- **Unanswered asks are excluded from validity** on both sides and counted separately. A gateway outage is an availability failure, and the stuck rule owns it.
- **Probes do not gate a prompt candidate.** Identity and perplexity measure a model; the model is not what changed.
- **Held-back artifacts.** Prompts, corpora, verdict sets and adapters never enter a public repo. Code and harnesses are open.
- Tests run under `-W error` and the full suite must stay green. Baseline before this plan: 275 passed, 4 skipped.
- Line endings: `.gitattributes` pins `*.py`, `*.sh` and `deploy/systemd/*` to LF. Unit files stay ASCII.

---

## File Structure

**Created**

- `grid/variants.py` — the registry: the `Variant` dataclass, the fail-closed loader, and the known-placeholder set. One responsibility: turn registry files into validated `Variant` objects.
- `grid/data/prompts/v1-sandbox.md` — today's template, extracted verbatim, with named placeholders.
- `grid/data/prompt_variants.json` — the registry: a `default` id and one entry per variant.
- `tests/test_variants.py` — loader validation and the byte-identical render.

**Modified**

- `grid/proposal.py` — `render_prompt` renders a `Variant` instead of a hardcoded f-string.
- `grid/odin.py` — per-dimension arm assignment; the chosen variant drives the render, the sampling and whether recall is called; the log records the variant.
- `grid/mind.py` — `ask` accepts sampling parameters instead of hardcoding them.
- `grid/proposal_log.py` — `proposal_entry` gains `prompt_variant` and `prompt_sha256`. (It lives here, not in `grid/verdicts.py`.)
- `grid/weekly.py` — validity counts and floor; dimensions; exclusivity; factorial; CLI.
- `grid/versions.py` — the ledger row gains `dimension`.
- `deploy/README.md`, `deploy/grid.env.example`, `deploy/skywave/verdict-receipts.sh` — operator documentation and the new environment keys.
- `tests/test_odin.py`, `tests/test_weekly.py`, `tests/test_proposal.py`, `tests/test_deploy_units.py` — extended, not replaced.

---

### Task 1: The prompt variant registry

**Files:**
- Create: `grid/variants.py`
- Create: `grid/data/prompts/v1-sandbox.md`
- Create: `grid/data/prompt_variants.json`
- Create: `tests/test_variants.py`
- Modify: `grid/proposal.py` (`SANDBOX_CONTEXT`, `render_prompt`)
- Modify: `pyproject.toml` (`[tool.setuptools.package-data]`)
- Modify: `tests/test_proposal.py`

**Interfaces:**
- Consumes: `Request` and `GENOME_CAP` from `grid/proposal.py`.
- Produces, for Tasks 2 and 4:
  - `Variant` frozen dataclass with fields `id: str`, `template: str`, `sha256: str`, `temperature: float`, `max_tokens: int`, `recall: bool`, `note: str`
  - `load_registry(path: Path | None = None) -> tuple[str, dict[str, Variant]]` returning `(default_id, variants)`
  - `get_variant(variant_id: str | None = None, *, path: Path | None = None) -> Variant`
  - `PLACEHOLDERS: frozenset[str]`
  - `render_prompt(req: Request, recall: list[str], variant: Variant | None = None) -> str`

- [ ] **Step 1: Write the failing test for a byte-identical render**

The golden string is today's output. Build it from the current implementation before you change anything: run the existing `render_prompt` on the fixture request, and paste the exact result into the test as `EXPECTED`. Do not retype it by hand.

```python
def test_default_variant_renders_byte_identically(fixture_request):
    from grid.proposal import render_prompt
    from grid.variants import get_variant
    out = render_prompt(fixture_request, ["remembered one", "remembered two"], get_variant())
    assert out == EXPECTED          # EXPECTED captured from the pre-change implementation
    assert get_variant().id == "v1-sandbox"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_variants.py -q -W error`
Expected: FAIL with `ModuleNotFoundError: No module named 'grid.variants'`.

- [ ] **Step 3: Extract the template**

Move the whole of today's prompt, including the `SANDBOX_CONTEXT` paragraph, into `grid/data/prompts/v1-sandbox.md` with these placeholders and no others: `{directive}`, `{isa_lines}`, `{ecology}`, `{parent}`, `{genome_count}`, `{genome_json}`, `{memory}`, `{genome_cap}`. Literal braces, if you ever add any, must be doubled. Keep `SANDBOX_CONTEXT` in `grid/proposal.py` as well: `tests/test_proposal.py` imports it and asserts the rendered prompt starts with it, which is a free guard on the byte-identical render. Removing it is out of scope.

- [ ] **Step 4: Write the registry file**

```json
{
  "default": "v1-sandbox",
  "variants": {
    "v1-sandbox": {
      "template_file": "prompts/v1-sandbox.md",
      "sha256": "<sha256 of the file, filled in after writing it>",
      "temperature": 0.7,
      "max_tokens": 400,
      "recall": true,
      "note": "The prompt as shipped 2026-09-05. Opens with the sandbox-context paragraph: without it the safety layer reads thegrid's directive plus opcodes named steal and corrupt as a request for self-replicating malware and returns an empty completion. A variant that drops it will fail the validity floor fast."
    }
  }
}
```

- [ ] **Step 5: Write the loader**

```python
PLACEHOLDERS = frozenset({"directive", "isa_lines", "ecology", "parent",
                          "genome_count", "genome_json", "memory", "genome_cap"})


def load_registry(path=None):
    """Read the registry, fail closed. A bad entry is an exception at load time:
    a variant silently edited in place would invalidate a running A/B, and a
    fallback to the default would hide it."""
```

Validate, per entry: the template file exists; its sha256 matches the recorded value; every placeholder the template uses is in `PLACEHOLDERS` (a template may use fewer, never an unknown one); `temperature` and `max_tokens` are a float and an int; `recall` is a bool. Raise `VariantError` (a new `ValueError` subclass) naming the variant and the specific problem. Also raise if `default` names a variant that does not exist.

Use `string.Formatter().parse()` to enumerate placeholders rather than a regular expression.

- [ ] **Step 6: Point `render_prompt` at the variant**

Keep the signature backwards compatible: `render_prompt(req, recall, variant=None)` where `None` means the registry default. Build the same substitution values it builds today, then `variant.template.format(**values)`. Keep the existing docstring's explanation of why the sandbox context is there.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_variants.py tests/test_proposal.py -q -W error`
Expected: PASS.

- [ ] **Step 8: Add the fail-closed tests**

One test per failure: a hash that does not match the file; a missing template file; a template using `{model}`, which is not in `PLACEHOLDERS`; a `default` naming an absent variant. Each asserts `VariantError` and that the message names the offending variant. Use `tmp_path` registries, never the shipped one.

- [ ] **Step 9: Ship the templates in the package**

`[tool.setuptools.package-data]` reads `grid = ["data/*.json", "data/*.jsonl"]`, which does not match
`data/prompts/*.md`. A non-editable install would then ship without the templates and the fail-closed
loader would raise at startup, which is the correct behaviour for the wrong reason. Add
`"data/prompts/*.md"` to that list.

- [ ] **Step 9: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest tests -q -W error`
Expected: 275 passed plus your new tests, 4 skipped.

- [ ] **Step 11: Commit**

```bash
git add grid/variants.py grid/data tests/test_variants.py grid/proposal.py tests/test_proposal.py pyproject.toml
git commit -F <message-file>
```

---

### Task 2: The relay renders, samples and logs the chosen variant

**Files:**
- Modify: `grid/odin.py` (`RelayConfig`, `Relay.__init__`, `Relay._handle`, `Relay._log`, `arm_for`)
- Modify: `grid/mind.py` (`Mind.ask`, `_ask_direct`)
- Modify: `grid/proposal_log.py` (`proposal_entry`)
- Modify: `tests/test_odin.py`

**Interfaces:**
- Consumes from Task 1: `get_variant`, `Variant`.
- Produces, for Tasks 3-6:
  - `assign(request_id: str, *, dimension: str, arms: list[str], weights: list[float]) -> str` in `grid/odin.py`, returning an arm label
  - proposal log entries carrying `prompt_variant: str` and `prompt_sha256: str`
  - `RelayConfig` fields `prompt_variant: str` (from `MIND_PROMPT_VARIANT`, empty means the registry default) and `prompt_candidate: str` (from `MIND_PROMPT_CANDIDATE`, empty means no prompt A/B)

- [ ] **Step 1: Write the failing test for dimension-salted independence**

```python
def test_model_dimension_assignment_is_unchanged_and_prompt_is_independent():
    ids = [f"req-{i}" for i in range(2000)]
    # The arms list is ORDERED and the candidate leads, so that the cumulative
    # bucket boundary falls at the split exactly where arm_for's `h < split` does.
    for rid in ids:
        assert assign(rid, dimension="model", arms=["cand", "inc"], weights=[0.2, 0.8]) == (
            "cand" if arm_for(rid, 0.2) == "candidate" else "inc")
    # and the prompt dimension must not be correlated with the model one
    both = sum(1 for rid in ids
               if assign(rid, dimension="model", arms=["a", "b"], weights=[0.5, 0.5]) == "a"
               and assign(rid, dimension="prompt", arms=["a", "b"], weights=[0.5, 0.5]) == "a")
    assert 0.20 < both / len(ids) < 0.30      # independent halves meet near 0.25, not 0.5
```

**Arm ordering is load-bearing.** `arm_for` puts the candidate below the split, so a two-arm `assign` must list the candidate first with the split as its weight. Listing the incumbent first silently inverts the A/B: the candidate would get 80% of traffic and every sub-project 3 guarantee would still look satisfied. Assert the ordering in the code, not only in the test.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_odin.py -k assignment -q -W error`
Expected: FAIL, `assign` is not defined.

- [ ] **Step 3: Implement `assign`**

```python
def assign(request_id, *, dimension, arms, weights):
    """Which arm this request lands on, in one dimension.

    The `model` dimension hashes the bare request id, exactly as arm_for has
    since sub-project 3, so deploying this never reshuffles a running A/B.
    Every other dimension salts with its own name, because two dimensions
    drawing on the same hash are perfectly correlated and a factorial run
    could not then say which one caused a win.
    """
    key = request_id if dimension == "model" else f"{dimension}:{request_id}"
    h = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 2**32
    upto = 0.0
    for arm, w in zip(arms, weights):
        upto += w
        if h < upto:
            return arm
    return arms[-1]
```

Keep `arm_for` as it is. It is sub-project 3's reviewed entry point and Task 6 is the only thing that may retire it.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_odin.py -k assignment -q -W error`
Expected: PASS.

- [ ] **Step 5: Thread the variant through `_handle`**

Choose the variant before the recall call, because the variant decides whether recall happens at all:

- with no `prompt_candidate` configured, the variant is `cfg.prompt_variant` or the registry default;
- with one configured, `assign(req.request_id, dimension="prompt", arms=[default, candidate], weights=[1 - split, split])` picks it;
- if `variant.recall` is false, skip `self.mind.recall(...)` entirely and pass `[]`;
- pass `variant` to `render_prompt`, and `variant.temperature` and `variant.max_tokens` to `mind.ask`.

- [ ] **Step 6: Let `ask` carry sampling parameters**

`_ask_direct` hardcodes `"temperature": 0.7, "max_tokens": 400`. Add keyword-only `temperature: float | None = None` and `max_tokens: int | None = None` to `Mind.ask` and `_ask_direct`, defaulting to those same values so every existing caller is unaffected. The bus route ignores them; note that in a comment rather than inventing a payload field.

- [ ] **Step 7: Log the variant**

`proposal_entry` gains `prompt_variant` and `prompt_sha256`, both defaulting to `""` so old log lines still parse. `Relay._log` passes the variant that produced the prompt, including on the deferred-bin rejection path, where the arm that could not answer is still the arm that was asked.

- [ ] **Step 8: Write the behavioural tests**

Three: a relay with no prompt candidate logs the default variant id on every entry; a relay with a candidate at split 0.2 logs both ids and each request's id matches `assign`; a variant with `recall: false` causes zero calls to the fake mind's `recall`. Assert the sampling parameters reached the fake client.

- [ ] **Step 9: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest tests -q -W error`
Expected: green.

- [ ] **Step 10: Commit**

---

### Task 3: Validity counts and the validity floor

**Files:**
- Modify: `grid/weekly.py` (`ab_arms`, `ab_decide`, new constants)
- Modify: `tests/test_weekly.py`

**Interfaces:**
- Consumes: proposal log entries with `outcome` (`"proposed"` or `"rejected"`) and `reason`.
- Produces, for Tasks 4-6: each arm dict gains `answered: int`, `valid: int`, `unanswered: int`, `validity: float`; `ab_decide` may return `("loss", "colony A/B lost: <arm> validity <v> below <floor> over <n> answered")`.

**Why this task exists:** a rejected reply never becomes a genome, so it produces no verdict row, so an arm that answers badly reads as `inconclusive` on sample size rather than losing. Spec section 5.

- [ ] **Step 1: Write the failing test**

```python
def test_a_candidate_that_cannot_answer_loses_instead_of_reading_short(tmp_path):
    # 40 asks on the candidate: 8 valid, 32 rejected. No verdict rows for the rejects.
    arms = _seed_validity(tmp_path, candidate=(8, 32), incumbent=(120, 0))
    verdict, reason = ab_decide(arms, incumbent="inc", candidate="cand")
    assert verdict == "loss"
    assert "validity" in reason and "0.20" in reason
```

- [ ] **Step 2: Run it to verify it fails**

Expected: FAIL with `inconclusive`, because the candidate has too few settled verdicts.

- [ ] **Step 3: Count attempts in `ab_arms`**

`ab_arms` walks verdict rows today. Walk the proposal log as well, in the same pass over `entries`, counting per arm for entries at or after `since_ts`:

- `outcome == "proposed"` increments `answered` and `valid`;
- `outcome == "rejected"` with a reason that is **not** the defer-bin reason increments `answered` only;
- `outcome == "rejected"` with the defer-bin reason increments `unanswered` only.

Match the defer-bin case on `reason.startswith("deferred ")`, the string `grid/odin.py` writes. `validity = valid / answered` when `answered` else `0.0`.

- [ ] **Step 4: Apply the floor in `ab_decide`**

Before the sample check:

```python
VALIDITY_MIN = 0.5
VALIDITY_MIN_ANSWERED = 20

c = arms[candidate]
if c["answered"] >= VALIDITY_MIN_ANSWERED and c["validity"] < VALIDITY_MIN:
    return "loss", (f"colony A/B lost: {candidate} validity {c['validity']:.2f} below "
                    f"{VALIDITY_MIN} over {c['answered']} answered")
```

The floor applies to the candidate only. An incumbent below it is a colony-wide fault, which the stuck rule in `ab_check` already owns; add that as a comment, not a branch.

- [ ] **Step 5: Run the tests**

Expected: PASS, and every existing `ab_decide` and `ab_arms` test still green. Where a fixture now needs `answered`, extend the seed helper rather than adding a second one.

- [ ] **Step 6: Test the exclusions**

Two more: an arm whose rejections are all defer-bins keeps `validity == 1.0` and reports its `unanswered` count, so an outage never reads as a wording failure; an arm with 19 answered and 1 valid does **not** lose, because the floor is silent below `VALIDITY_MIN_ANSWERED`.

- [ ] **Step 7: Run the full suite. Commit.**

---

### Task 4: Dimensions in the gate and the ledger

**Files:**
- Modify: `grid/versions.py` (`release_row`, `DECISIONS`)
- Modify: `grid/weekly.py` (`ab_state`, `lost_tags`, `start_ab`, `end_ab`, `ab_check`, `ab_reconcile`, `release`, `flip`)
- Modify: `tests/test_weekly.py`, `tests/test_versions.py`

**Interfaces:**
- Consumes from Tasks 1-3: variants, `assign`, the validity-aware arms.
- Produces, for Tasks 5-6:
  - every A/B ledger row carries `dimension: str` (`"model"` or `"prompt"`)
  - `ab_state(versions, *, dimension: str) -> dict | None`
  - `lost_tags(rows, *, dimension: str) -> set[str]`
  - `start_ab(cfg, candidate, *, dimension="model", local_run, dry_run=False)` and `end_ab(cfg, *, dimension="model", local_run, dry_run=False)`, i.e. their current signatures with one keyword added
  - a prompt flip writes `MIND_PROMPT_VARIANT` and restarts the relay, with no remote call

- [ ] **Step 1: Write the failing test**

```python
def test_a_prompt_ab_and_a_model_ab_keep_separate_state_and_memory(tmp_path):
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="held",
                 reason="colony A/B lost: 0.10 vs 0.20", short_arms=["kannaka-brain-v4"])
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_started")
    assert ab_state(versions, dimension="model") is None
    assert ab_state(versions, dimension="prompt")["candidate"] == "v2-terse"
    assert lost_tags(rows, dimension="model") == {"kannaka-brain-v4"}
    assert lost_tags(rows, dimension="prompt") == set()
```

- [ ] **Step 2: Run it to verify it fails.** Expected: `TypeError`, unexpected keyword `dimension`.

- [ ] **Step 3: Add `dimension` to the ledger row**

`release_row` gains the key. Default it to `"model"` when absent so every row written before this change reads as a model row, which is what they all were. Add that default in the reader, not by rewriting the file.

`tests/test_versions.py:26` asserts the exact key set of a release row. It will fail, and that is correct: add `dimension` to the expected set rather than loosening the assertion. An exact key set is a deliberate guard against a row quietly growing a field nobody reviewed.

- [ ] **Step 4: Scope the state machine**

Give `ab_state`, `lost_tags`, `start_ab`, `end_ab`, `ab_check` and `ab_reconcile` a keyword-only `dimension`. Inside, filter rows to that dimension. Keep `"model"` as the default value on every signature so no existing call site changes meaning.

- [ ] **Step 5: Make the prompt flip local**

`flip()` today writes `MIND_MODEL_ALIAS`, runs a remote `ollama cp` and restarts the relay. For `dimension="prompt"` it writes `MIND_PROMPT_VARIANT`, clears `MIND_PROMPT_CANDIDATE`, and restarts the relay: no remote call, so no `flip_failed` outcome is reachable. Keep the same three-step revert on a failed restart.

- [ ] **Step 6: Skip probes for a prompt candidate**

In `release()`, the identity and perplexity probes run only for `dimension="model"`. A prompt candidate goes straight to `start_ab`. Comment why: the probes measure a model and the model did not change.

- [ ] **Step 7: Run the full suite**

Every sub-project 3 test must still pass untouched. If one needs a `dimension` argument to keep meaning, that is a signal you changed a default somewhere; fix the default instead.

- [ ] **Step 8: Commit.**

---

### Task 5: The CLI and one experiment at a time

**Files:**
- Modify: `grid/weekly.py` (`main`, `_exit_code`)
- Modify: `tests/test_weekly.py`

**Interfaces:**
- Consumes: everything from Task 4.
- Produces, for Task 6: `--dimension {model,prompt}` on `release`, `rollback` and `ab`; a refusal path that names the blocking dimension.

- [ ] **Step 1: Write the failing tests**

```python
def test_starting_a_prompt_ab_while_a_model_ab_runs_is_refused(tmp_path, capsys):
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="ab_started")
    assert main(["release", "--dimension", "prompt"]) == 0
    row = _last_row(tmp_path)
    assert row["decision"] == "unchanged"
    assert "model A/B already running" in row["reason"] and "kannaka-brain-v4" in row["reason"]


def test_ab_with_no_dimension_reports_every_open_one(tmp_path, capsys):
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="ab_started")
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_started")
    assert main(["ab"]) == 0
    shown = json.loads(capsys.readouterr().out)["ab"]
    assert shown["model"]["candidate"] == "kannaka-brain-v4"
    assert shown["prompt"]["candidate"] == "v2-terse"


def test_ab_check_returns_the_highest_exit_code_any_dimension_produced(tmp_path, monkeypatch):
    # model reconciles cleanly, prompt cannot restart the relay -> the run must be loud
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="flip_incomplete")
    monkeypatch.setattr(weekly_mod, "_local_run", lambda *a, **k: 1)
    assert main(["ab", "--check"]) == 3
```

- [ ] **Step 2: Run to verify they fail. Step 3: Implement.**

`--dimension` defaults to `model` on `release` and `rollback`. On `ab`, omitting it means all dimensions: the display prints a mapping keyed by dimension, and `--check` reconciles each in turn and returns the highest exit code any of them produced.

The refusal is a recorded `unchanged` row, not a bare message, so the ledger shows why a Sunday did nothing.

- [ ] **Step 4: Run the full suite. Step 5: Commit.**

---

### Task 6: Factorial mode

**Files:**
- Modify: `grid/weekly.py` (`ab_arms`, `ab_decide`, `start_ab`, `release`, `main`)
- Modify: `grid/odin.py` (`_handle`)
- Modify: `tests/test_weekly.py`, `tests/test_odin.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `--factorial` on `release`; four-arm tallies keyed by `"<model>|<variant>"`; a marginal decision per dimension plus an advisory interaction.

- [ ] **Step 1: Write the failing test**

```python
def test_a_factorial_run_decides_each_dimension_on_its_marginal(tmp_path):
    # The candidate MODEL is good and the candidate PROMPT is bad. Pooled over the
    # other dimension each marginal is unambiguous, and neither may borrow the
    # other's result. Cells are (settled, reproduced), 60 settled each so every
    # marginal pools to 120, above MIN_SAMPLE.
    arms = _seed_factorial(tmp_path, cells={
        ("m0", "p0"): (60, 18),     # incumbent model, incumbent prompt -> 0.30
        ("m0", "p1"): (60, 6),      # incumbent model, candidate prompt -> 0.10
        ("m1", "p0"): (60, 21),     # candidate model, incumbent prompt -> 0.35
        ("m1", "p1"): (60, 9),      # candidate model, candidate prompt -> 0.15
    })
    result = ab_decide_factorial(arms, model=("m0", "m1"), prompt=("p0", "p1"))
    assert result["model"][0] == "win"      # 0.25 vs 0.20, above the tolerance floor
    assert result["prompt"][0] == "loss"    # 0.125 vs 0.325, far below it
    assert result["interaction"]["advisory"] is True
    assert "sample" in result["interaction"]["reason"]
```

- [ ] **Step 2: Run to verify it fails. Step 3: Implement.**

The relay already assigns both dimensions independently, so the only relay change is logging both labels, which Task 2 did. `ab_arms` gains a mode that keys arms by the pair. `ab_decide_factorial` pools over the other dimension to get each marginal, then applies the same tolerance rule and the same validity floor per marginal. The interaction is computed, reported, and marked `advisory: True` with a one-line reason saying the sample cannot support it.

- [ ] **Step 4: Guard the sample floor**

Four arms need `MIN_SAMPLE` per *marginal*, not per cell, and the marginal pools two cells. State that in a comment and test the boundary.

- [ ] **Step 5: Run the full suite. Step 6: Commit.**

---

### Task 7: Deploy and operator documentation

**Files:**
- Modify: `deploy/README.md`
- Modify: `deploy/grid.env.example`
- Modify: `deploy/skywave/verdict-receipts.sh`
- Modify: `tests/test_deploy_units.py`

- [ ] **Step 1: Add the environment keys**

`#MIND_PROMPT_VARIANT=` and `#MIND_PROMPT_CANDIDATE=`, both commented out, each with its explanation on its own line above. The existing guard test forbids a comment after a value, so keep them above.

- [ ] **Step 2: Extend the guard test**

The inline-comment guard already reads the shipped example. Add an assertion that both new keys appear, so a future edit cannot quietly drop them.

- [ ] **Step 3: Document the operator story**

A new README section covering: what a variant is and where the registry lives; how to add one; that a variant edited in place without a new id is refused at startup by design; how to start a prompt A/B by hand for a smoke test, mirroring the model one; the validity floor and why a broken variant now fails in hours; and that only one dimension runs at a time unless `--factorial` is given.

- [ ] **Step 4: Add the receipts**

`verdict-receipts.sh` gains a section printing the active variant, the registry default, and the per-arm validity from the last window, so a receipt shows the prompt state the way it already shows the model state.

- [ ] **Step 5: Run the full suite. Step 6: Commit.**

---

## After the plan

Deploy is the existing path: merge to `main`, push, run `deploy/skywave/deploy.sh`, which pulls the checkout and restarts the units. The smoke receipt mirrors sub-project 3's: add a second variant, hand-start a prompt A/B at split 0.2, confirm from the proposal log that both variants appear and that each request's variant matches `assign`, then abort and commit the receipts.

**Note for the deploy step:** the repository has moved to `kannaka-labs/kannaka-grid`. `deploy/skywave/deploy.sh` still names the old URL, which works through GitHub's redirect. Updating it is a one-line change and is out of scope here; raise it rather than fixing it silently.
