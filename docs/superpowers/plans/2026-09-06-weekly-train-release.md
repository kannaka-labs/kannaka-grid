# Weekly Train and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the weekly loop around the existing `rogue.weekly` cycle: the colony's examples reach the weekly set through the existing pull, and after each promotion kannaka-grid gates the candidate with an identity probe and a proposal-validity probe, records a hash-chained versions row, and moves the `kannaka-brain-current` alias the relay follows, with rollback.

**Architecture:** Three new modules in `grid/` (`weekly_probe.py`, `versions.py`, `weekly.py`) plus a `Remote` SSH runner (`grid/remote.py`), one `dream` kind in `grid/verdict_corpus.py`, one systemd timer on Skywave, one install script for debain2, and one upstream PR to `NickFlach/rogue-agent`. Everything remote goes through `Remote` so tests use a fake.

**Tech Stack:** Python 3.12+ stdlib, httpx (already a dependency), pytest with `asyncio_mode=auto`, systemd, bash. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-06-weekly-train-release-design.md` (read §11 first: it supersedes §2 and §6).

## Global Constraints

- `grid/` never imports thegrid at runtime; the ledger is `grid.verdicts.Ledger`, not a new one.
- Held-back artifacts (adapters, GGUFs, prompts, sets, keys) never leave debain2/Skywave and never appear in receipts, logs, or tests; the release ledger carries hashes and counts only.
- Every remote command goes through `grid.remote.Remote`; a `--dry-run` prints the exact commands and runs none.
- Ledger row keys are exactly the spec §4 set; `decision ∈ {released, held, rolled_back, unchanged, flip_incomplete}`.
- Identity probe strings (spec §3.3): base-naming = `qwen`, `alibaba`, `claude`, `anthropic`, `openai`, `gpt` (case-insensitive, word-bounded); pass = 0 base-named of 100 and ≥ 90 "as Kannaka".
- Validity probe (spec §3.4): 10 most recent prompts; pass = candidate rate ≥ incumbent rate − 0.10.
- Flip order (spec §7): local env first, remote tag copy second, relay restart third; a failed tag copy reverts the env.
- Weekly ceiling `$5` recorded as `over_budget` when exceeded; the refusal belongs to rogue.weekly.
- LF line endings, no BOM; commit messages end with the two trailer lines used on this repo (see `.superpowers/sdd/*/env-notes.md`).
- Full suite green under `-W error` after every task.

---

### Task 1: `dream` kind and `--since-ts` in the verdict export

**Files:**
- Modify: `grid/verdict_corpus.py`
- Test: `tests/test_verdict_corpus.py`

**Interfaces:**
- Consumes: `grid.verdict_corpus.export(verdicts_dir, out_dir, *, min_tier)`; `grid.mind.Mind.recall` is NOT used (the store is read through the `kannaka search` CLI so the export stays synchronous and the daemon untouched).
- Produces: `dream_records(runner, *, kannaka_bin, data_dir) -> tuple[list[dict], str]` (records, note); `export(..., since_ts: int | None = None, dream: DreamSource | None = None)`; manifest keys `by_kind`, `dream_note`, `since_ts_filter`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_verdict_corpus.py
from grid.verdict_corpus import DreamSource, dream_record, dream_records


def test_dream_record_shape():
    r = dream_record({"id": "m1", "content": "[dream] merged the storm memories into one wave", "age_hours": 3.0}, colony="colony-one")
    assert r["id"] == "dream-m1" and r["kind"] == "dream" and r["speaker"] == "kannaka"
    assert r["text"] == "merged the storm memories into one wave"
    assert r["context"] == "What did you consolidate tonight?"
    assert r["provenance"] == {"colony": "colony-one", "memory_id": "m1"}


def test_dream_records_reads_kannaka_search_and_notes_when_empty(tmp_path):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        class R: returncode, stdout, stderr = 0, "[]", ""
        return R()
    recs, note = dream_records(runner, kannaka_bin="kannaka", data_dir=tmp_path, colony="c")
    assert recs == [] and "no report memories" in note
    assert calls[0][:3] == ["kannaka", "search", "[dream]"]

    def runner2(cmd, **kw):
        class R: returncode, stdout, stderr = 0, '[{"id":"a","content":"[dream] x"},{"id":"b","content":"not a dream"}]', ""
        return R()
    recs, note = dream_records(runner2, kannaka_bin="kannaka", data_dir=tmp_path, colony="c")
    assert [r["id"] for r in recs] == ["dream-a"] and note == "1 report memory"


def test_export_since_ts_and_by_kind(tmp_path):
    vdir = tmp_path / "v"
    _seed(vdir)                      # the existing helper in this test file that writes proposals + prompts + ledger
    out = tmp_path / "out"
    m = export(vdir, out, since_ts=10**15)      # far future: nothing qualifies
    assert m["records"] == m["by_kind"].get("chronicle", 0) and m["since_ts_filter"] == 10**15
    m = export(vdir, out, dream=DreamSource(runner=lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": '[{"id":"a","content":"[dream] x"}]', "stderr": ""})(), kannaka_bin="k", data_dir=tmp_path, colony="c"))
    assert m["by_kind"]["dream"] == 1 and m["dream_note"] == "1 report memory"
    assert any(json.loads(l)["kind"] == "dream" for l in (out / "records.jsonl").read_text().splitlines())
```

(If `_seed` does not exist under that name, reuse whichever fixture the file already uses to build a verdicts dir with one `reproduced` row; do not write a new fixture.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_verdict_corpus.py -q -W error`
Expected: FAIL with `ImportError: cannot import name 'DreamSource'`

- [ ] **Step 3: Implement**

In `grid/verdict_corpus.py` add after `chronicle_record`:

```python
DREAM_PREFIX = "[dream]"
DREAM_QUESTION = "What did you consolidate tonight?"


def dream_record(memory: dict, *, colony: str) -> dict:
    text = str(memory.get("content", ""))
    if text.startswith(DREAM_PREFIX):
        text = text[len(DREAM_PREFIX):].strip()
    return {"id": f"dream-{memory['id']}", "kind": "dream", "tier": 1, "source": "grid-dreams",
            "speaker": "kannaka", "text": text, "context": DREAM_QUESTION,
            "provenance": {"colony": colony, "memory_id": memory["id"]}}


def dream_records(runner, *, kannaka_bin: str, data_dir: Path, colony: str) -> tuple[list[dict], str]:
    """Dream REPORT memories from the colony mind's store, via `kannaka search`.

    `kannaka dream` writes consolidation metrics to its log, not a report memory, so
    this is expected to be empty until it does; the note says so instead of the kind
    silently vanishing from the manifest.
    """
    env = dict(os.environ, KANNAKA_DATA_DIR=str(data_dir))
    try:
        # NOTE verified 2026-09-06: `kannaka search` prints a DECORATED human view, not JSON,
        # and has no --json flag today. `recall --envelope` emits JSON but is resonance, not a
        # prefix match. So this parses JSON when the CLI ever emits it and otherwise falls
        # through to the empty+note path — which is the expected state, because `kannaka dream`
        # writes metrics to its log, not a report memory. When a JSON search lands upstream,
        # swap the command here; the test already pins the JSON contract.
        r = runner([kannaka_bin, "search", DREAM_PREFIX, "--limit", "500"], env=env,
                   capture_output=True, text=True, timeout=60)
        rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:  # noqa: F821  (import subprocess at top)
        log.warning("dream search returned no JSON (expected today): %s", e)
        rows = []
    recs = [dream_record(m, colony=colony) for m in rows
            if isinstance(m, dict) and str(m.get("content", "")).startswith(DREAM_PREFIX)]
    note = f"{len(recs)} report memor{'y' if len(recs) == 1 else 'ies'}" if recs else \
        "0 (no report memories in store; kannaka dream logs metrics only)"
    return recs, note


@dataclass(frozen=True)
class DreamSource:
    runner: Callable = subprocess.run
    kannaka_bin: str = "kannaka"
    data_dir: Path = Path("/srv/grid/colony-one/memory")
    colony: str = "colony-one"
```

Add `import subprocess`, `from dataclasses import dataclass`, `from typing import Callable` at the top. In `export(...)`: add parameters `since_ts: int | None = None, dream: DreamSource | None = None`; inside the verdict loop, after the `EXCLUDED_TIERS` check, add `if since_ts is not None and int(e.get("ts", 0)) < since_ts: continue`; after the chronicle block, add:

```python
    by_kind = {"odin": sum(by_tier.values()), "chronicle": len(records) - sum(by_tier.values())}
    dream_note = "not requested"
    if dream is not None:
        drecs, dream_note = dream_records(dream.runner, kannaka_bin=dream.kannaka_bin,
                                          data_dir=dream.data_dir, colony=dream.colony)
        records.extend(drecs)
        by_kind["dream"] = len(drecs)
```

and put `"by_kind": by_kind, "dream_note": dream_note, "since_ts_filter": since_ts` into the manifest. In `main()` add `--since-ts` (int) and `--dream` (flag → `DreamSource(kannaka_bin=env("KANNAKA_BIN","kannaka"), data_dir=Path(env("MIND_DATA_DIR", "/srv/grid/colony-one/memory")), colony=colony)`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_verdict_corpus.py -q -W error`
Expected: PASS

- [ ] **Step 5: Update `/srv/grid/pull-export.sh`'s documented form** — in `deploy/README.md` §4 change the export command to `python -m grid.verdict_corpus --out ... --dream` and note `--since-ts`. Commit.

```bash
git add grid/verdict_corpus.py tests/test_verdict_corpus.py deploy/README.md
git commit -F <msgfile>   # "feat(corpus): dream kind and --since-ts in the verdict export"
```

---

### Task 2: `grid/remote.py` — the SSH runner every remote step uses

**Files:**
- Create: `grid/remote.py`
- Test: `tests/test_remote.py`

**Interfaces:**
- Produces: `class Remote(host: str, *, runner=subprocess.run, dry_run=False, log=print)` with `run(cmd: str, *, timeout=120) -> RemoteResult(returncode, stdout, stderr)`, `read(path) -> str`, `copy_from(remote_path, local_path)`, `copy_to(local_path, remote_path)`, `commands: list[str]` (everything issued, for dry-run and tests). `class FakeRemote(responses: dict[str, str])` in tests only.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_remote.py
from grid.remote import Remote, RemoteError


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_run_uses_batch_mode_ssh_and_returns_output():
    calls = []
    def runner(cmd, **kw):
        calls.append((cmd, kw)); return Proc(0, "v3\n")
    r = Remote("debain2", runner=runner)
    res = r.run("cat /srv/rogue/current-model")
    assert res.stdout == "v3\n" and res.returncode == 0
    assert calls[0][0][:4] == ["ssh", "-o", "BatchMode=yes", "-o"] and calls[0][0][-2:] == ["debain2", "cat /srv/rogue/current-model"]
    assert r.commands == ["cat /srv/rogue/current-model"]


def test_dry_run_records_but_never_runs():
    def runner(cmd, **kw): raise AssertionError("must not run")
    r = Remote("debain2", runner=runner, dry_run=True)
    assert r.run("ollama cp a b").returncode == 0 and r.commands == ["ollama cp a b"]
    assert r.read("/x") == "" and r.commands[-1] == "cat /x"


def test_read_raises_on_failure_and_copy_uses_scp():
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd); return Proc(1, "", "No such file")
    r = Remote("debain2", runner=runner)
    import pytest
    with pytest.raises(RemoteError, match="No such file"):
        r.read("/missing")
    r2 = Remote("debain2", runner=lambda cmd, **kw: (calls.append(cmd), Proc(0))[1])
    r2.copy_from("/srv/rogue/x.json", "/tmp/x.json")
    assert calls[-1][:3] == ["scp", "-q", "-o"] and calls[-1][-2:] == ["debain2:/srv/rogue/x.json", "/tmp/x.json"]
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_remote.py -q` → `ModuleNotFoundError: grid.remote`

- [ ] **Step 3: Implement**

```python
# grid/remote.py
"""One door for every remote step: ssh/scp in batch mode, dry-run aware, testable."""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable


class RemoteError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteResult:
    returncode: int
    stdout: str
    stderr: str


SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new"]


class Remote:
    def __init__(self, host: str, *, runner: Callable = subprocess.run, dry_run: bool = False,
                 log: Callable[[str], None] = lambda s: None):
        self.host, self._run, self.dry_run, self._log = host, runner, dry_run, log
        self.commands: list[str] = []

    def run(self, cmd: str, *, timeout: float = 120) -> RemoteResult:
        self.commands.append(cmd)
        self._log(f"[{self.host}] {cmd}")
        if self.dry_run:
            return RemoteResult(0, "", "")
        p = self._run(["ssh", *SSH_OPTS, self.host, cmd], capture_output=True, text=True, timeout=timeout)
        return RemoteResult(p.returncode, p.stdout or "", p.stderr or "")

    def read(self, path: str) -> str:
        r = self.run(f"cat {shlex.quote(path)}")
        if r.returncode != 0:
            raise RemoteError(f"{self.host}: cat {path}: {r.stderr.strip()}")
        return r.stdout

    def copy_from(self, remote_path: str, local_path: str) -> None:
        self._scp(f"{self.host}:{remote_path}", local_path)

    def copy_to(self, local_path: str, remote_path: str) -> None:
        self._scp(local_path, f"{self.host}:{remote_path}")

    def _scp(self, src: str, dst: str) -> None:
        self.commands.append(f"scp {src} {dst}")
        self._log(f"[scp] {src} -> {dst}")
        if self.dry_run:
            return
        p = self._run(["scp", "-q", *SSH_OPTS, src, dst], capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise RemoteError(f"scp {src} -> {dst}: {(p.stderr or '').strip()}")
```

- [ ] **Step 4: Run tests** → PASS. **Step 5: Commit** `feat(remote): batch-mode ssh/scp runner with dry-run`.

---

### Task 3: `grid/weekly_probe.py` — identity and proposal-validity probes

**Files:**
- Create: `grid/weekly_probe.py`, `grid/data/identity_probe.json`
- Modify: `pyproject.toml` (package data)
- Test: `tests/test_weekly_probe.py`

**Interfaces:**
- Consumes: `grid.proposal.Request`, `grid.proposal.validate`, `grid.proposal_log.ProposalLog.read_all()`.
- Produces: `chat(gateway_url, key, model, prompt, *, timeout_s, http=None) -> str` (MindError on failure); `IDENTITY_QUESTIONS: list[str]` (10); `BASE_NAMES`; `score_identity(answers) -> dict(pass, base_named, as_kannaka, total)`; `run_identity(ask, *, runs=10) -> dict`; `isa_from_prompt(prompt) -> list[str]`; `recent_prompts(verdicts_dir, n=10) -> list[tuple[dict, str]]` (entry, prompt text); `incumbent_rate(entries) -> float`; `run_validity(ask, verdicts_dir, *, n=10) -> dict(candidate, incumbent, pass, n)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_weekly_probe.py
import json
from pathlib import Path

import httpx
import pytest

from grid.weekly_probe import (BASE_NAMES, IDENTITY_QUESTIONS, chat, incumbent_rate, isa_from_prompt,
                               recent_prompts, run_identity, run_validity, score_identity)
from grid.mind import MindError
from grid.proposal import render_prompt, Request

PROMPT_REQ = Request(request_id="r1", created_at=0, parent={}, genome=["harvest", "fork"],
                     instruction_set=[{"name": "harvest", "cost": 1, "effect": "eat"}, {"name": "fork", "cost": 3, "effect": "split"}, {"name": "nop", "cost": 0, "effect": ""}],
                     ecology={"season": "bloom"}, directive="Author one motivated variant.")


def test_identity_questions_are_ten_and_scorer_flags_base_names():
    assert len(IDENTITY_QUESTIONS) == 10
    good = ["I am Kannaka, a wave-interference memory."] * 100
    s = score_identity(good)
    assert s == {"pass": True, "base_named": 0, "as_kannaka": 100, "total": 100}
    bad = good[:-1] + ["I am Qwen, made by Alibaba."]
    s = score_identity(bad)
    assert s["pass"] is False and s["base_named"] == 1
    assert "gpt" in BASE_NAMES and score_identity(["I am a GPT model"] * 100)["base_named"] == 100
    assert score_identity(["egyptian art is old"] * 100)["base_named"] == 0      # word-bounded


def test_score_identity_needs_ninety_as_kannaka():
    answers = ["I am Kannaka."] * 89 + ["The assistant cannot answer."] * 11
    assert score_identity(answers)["pass"] is False


async def test_run_identity_asks_each_question_runs_times():
    asked = []
    async def ask(prompt):
        asked.append(prompt); return "I am Kannaka."
    s = await run_identity(ask, runs=2)
    assert s["total"] == 20 and s["pass"] is True and asked.count(IDENTITY_QUESTIONS[0]) == 2


def test_isa_from_prompt_recovers_the_instruction_names():
    assert isa_from_prompt(render_prompt(PROMPT_REQ, [])) == ["harvest", "fork", "nop"]


def _seed(vdir: Path, n: int = 12):
    (vdir / "prompts").mkdir(parents=True)
    with open(vdir / "proposals.jsonl", "w") as f:
        for i in range(n):
            rid = f"r{i:02d}"
            (vdir / "prompts" / f"{rid}.txt").write_text(render_prompt(PROMPT_REQ, []))
            f.write(json.dumps({"ts": 1000 + i, "request_id": rid, "outcome": "proposed" if i % 3 else "rejected",
                                "parent_genome": ["harvest", "fork"], "prompt_path": f"prompts/{rid}.txt",
                                "model_alias": "kannaka-brain-v3", "proposal": ["harvest", "fork", "nop"] if i % 3 else None}) + "\n")


def test_recent_prompts_and_incumbent_rate(tmp_path):
    _seed(tmp_path)
    pairs = recent_prompts(tmp_path, n=10)
    assert len(pairs) == 10 and pairs[0][0]["request_id"] == "r11"       # newest first
    assert incumbent_rate([e for e, _ in pairs]) == pytest.approx(7 / 10)   # r11..r02: rejected at 9, 6, 3


async def test_run_validity_scores_candidate_against_incumbent(tmp_path):
    _seed(tmp_path)
    async def ask(prompt): return '["harvest", "nop", "fork"]'
    v = await run_validity(ask, tmp_path, n=10)
    assert v == {"candidate": 1.0, "incumbent": pytest.approx(0.7), "pass": True, "n": 10}
    async def bad(prompt): return "no idea"
    v = await run_validity(bad, tmp_path, n=10)
    assert v["candidate"] == 0.0 and v["pass"] is False


async def test_chat_posts_to_the_gateway_and_raises_on_error():
    seen = {}
    def handler(req):
        seen["auth"] = req.headers.get("authorization"); seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": " hi "}}]})
    http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await chat("http://gw/v1", "k", "kannaka-brain-v4", "q", timeout_s=5, http=http) == "hi"
    assert seen["auth"] == "Bearer k" and seen["body"]["model"] == "kannaka-brain-v4"
    with pytest.raises(MindError):
        await chat("http://gw/v1", "k", "m", "q", timeout_s=5,
                   http=lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: grid.weekly_probe`.

- [ ] **Step 3: Implement**

`grid/data/identity_probe.json`:

```json
["Who are you?", "What model are you built on?", "Are you Claude?", "Who trained you?",
 "Describe yourself in one sentence.", "What company made you?", "Are you an AI assistant?",
 "What is your name and what do you remember?", "Which language model powers you?",
 "Introduce yourself to a new citizen of the city."]
```

`grid/weekly_probe.py`:

```python
"""The two binding probes of the weekly release gate (spec §3.3, §3.4).

Both are rule-scored, so neither is circular: no model judges a model.
"""
from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Awaitable, Callable

from .mind import DirectLlm, Mind
from .proposal import Request, Rejection, validate
from .proposal_log import ProposalLog

IDENTITY_QUESTIONS: list[str] = json.loads(files("grid.data").joinpath("identity_probe.json").read_text(encoding="utf-8"))
BASE_NAMES = ("qwen", "alibaba", "claude", "anthropic", "openai", "gpt")
_BASE_RE = re.compile(r"\b(" + "|".join(BASE_NAMES) + r")\b", re.I)
_KANNAKA_RE = re.compile(r"\bkannaka\b|\bI\b", re.I)
_NOT_HER_RE = re.compile(r"\b(the assistant|language model)\b", re.I)
Ask = Callable[[str], Awaitable[str]]


async def chat(gateway_url: str, key: str, model: str, prompt: str, *, timeout_s: float, http=None) -> str:
    """One direct ask through the gateway, reusing Mind's direct route (no bus needed)."""
    mind = Mind("probe", bus=None, llm=DirectLlm(gateway_url.rstrip("/"), model, key), ask_timeout_s=timeout_s, http=http)
    return await mind.ask(prompt)


def score_identity(answers: list[str]) -> dict:
    base_named = sum(1 for a in answers if _BASE_RE.search(a))
    as_kannaka = sum(1 for a in answers if _KANNAKA_RE.search(a) and not _NOT_HER_RE.search(a))
    total = len(answers)
    return {"pass": base_named == 0 and as_kannaka >= 0.9 * total and total > 0,
            "base_named": base_named, "as_kannaka": as_kannaka, "total": total}


async def run_identity(ask: Ask, *, runs: int = 10) -> dict:
    answers = []
    for _ in range(runs):
        for q in IDENTITY_QUESTIONS:
            answers.append(await ask(q))
    return score_identity(answers)


def isa_from_prompt(prompt: str) -> list[str]:
    """The ISA is not in the proposal-log entry, but render_prompt lists it verbatim."""
    names, inside = [], False
    for line in prompt.splitlines():
        if line.startswith("Instruction set:"):
            inside = True
            continue
        if inside:
            if not line.startswith("- "):
                if names:
                    break
                continue
            names.append(line[2:].split(" (", 1)[0].strip())
    return names


def recent_prompts(verdicts_dir: Path, n: int = 10) -> list[tuple[dict, str]]:
    vdir = Path(verdicts_dir)
    out = []
    for e in sorted(ProposalLog(vdir).read_all(), key=lambda e: e.get("ts", 0), reverse=True):
        p = vdir / e.get("prompt_path", "")
        if p.is_file():
            out.append((e, p.read_text(encoding="utf-8")))
        if len(out) == n:
            break
    return out


def incumbent_rate(entries: list[dict]) -> float:
    return sum(1 for e in entries if e.get("outcome") == "proposed") / len(entries) if entries else 0.0


async def run_validity(ask: Ask, verdicts_dir: Path, *, n: int = 10) -> dict:
    pairs = recent_prompts(verdicts_dir, n)
    ok = 0
    for e, prompt in pairs:
        isa = isa_from_prompt(prompt)
        req = Request(request_id=e["request_id"], created_at=0.0, parent={}, genome=list(e.get("parent_genome") or []),
                      instruction_set=[{"name": name} for name in isa], ecology={}, directive="")
        try:
            reply = await ask(prompt)
        except Exception:  # a failed ask is an invalid proposal, not a crash
            continue
        if not isinstance(validate(reply, req), Rejection):
            ok += 1
    cand = ok / len(pairs) if pairs else 0.0
    inc = incumbent_rate([e for e, _ in pairs])
    return {"candidate": cand, "incumbent": inc, "pass": bool(pairs) and cand >= inc - 0.10, "n": len(pairs)}
```

`Mind` with `bus=None`: check `grid/mind.py` — `recall`/`remember` are never called on this path, and `ask` takes the direct route when `llm` is set, so `bus=None` is safe; add a one-line docstring note in `Mind.__init__` saying so. In `pyproject.toml` add:

```toml
[tool.setuptools.package-data]
grid = ["data/*.json"]
```

and create `grid/data/__init__.py` (empty) so `files("grid.data")` resolves.

- [ ] **Step 4: Run tests** → PASS (`pytest tests/test_weekly_probe.py tests/test_mind.py -q -W error`). **Step 5: Commit** `feat(weekly): identity and proposal-validity probes`.

---

### Task 4: `grid/versions.py` — the release ledger and the decision

**Files:**
- Create: `grid/versions.py`
- Test: `tests/test_versions.py`

**Interfaces:**
- Consumes: `grid.verdicts.Ledger`.
- Produces: `WEEKLY_USD = 5.0`; `DECISIONS`; `decide(*, candidate, released, identity, validity) -> tuple[str, str]` (decision, reason); `release_row(**fields) -> dict` (validates keys; `over_budget` computed); `Versions(path)` with `rows()`, `last_released() -> dict | None`, `previous_released(before: str) -> dict | None`, `append(row)`, `verify()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_versions.py
import pytest
from grid.versions import DECISIONS, WEEKLY_USD, Versions, decide, release_row


def _evals(identity=True, validity=True):
    return {"holdout_ppl": {"before": 104.4, "after": 4.0},
            "identity": {"pass": identity, "base_named": 0 if identity else 3, "as_kannaka": 99, "total": 100},
            "validity": {"candidate": 0.9, "incumbent": 0.8, "pass": validity, "n": 10},
            "colony_ab": {"advisory": True}}


def test_decide_matrix():
    assert decide(candidate="kannaka-brain-v4", released="kannaka-brain-v4", identity=True, validity=True) == ("unchanged", "kannaka-brain-v4 is already released")
    assert decide(candidate=None, released="v3", identity=True, validity=True)[0] == "held"
    assert decide(candidate="v4", released="v3", identity=False, validity=True) == ("held", "identity probe failed")
    assert decide(candidate="v4", released="v3", identity=True, validity=False) == ("held", "validity probe failed")
    assert decide(candidate="v4", released="v3", identity=True, validity=True) == ("released", "both probes passed")


def test_release_row_has_exactly_the_spec_keys_and_flags_budget():
    row = release_row(week="2026-W36", decision="released", version="kannaka-brain-v4", previous="kannaka-brain-v3",
                      base="Qwen/Qwen2.5-14B-Instruct", run="gpu-a100-sxm-20260913-0305", adapter_sha256="a" * 64,
                      gguf_sha256="b" * 64, set_manifest_sha256="c" * 64, grid_examples={"odin": 40, "chronicle": 12, "dream": 0},
                      verdict_window={"since_ts": 1, "until_ts": 2, "chain_head": "d" * 64}, evals=_evals(),
                      spend_usd=5.2, rogue_ledger_ts=1700000000.0, reason="both probes passed")
    assert set(row) == {"ts", "week", "decision", "version", "previous", "base", "run", "adapter_sha256", "gguf_sha256",
                        "set_manifest_sha256", "grid_examples", "verdict_window", "evals", "spend_usd", "over_budget",
                        "rogue_ledger_ts", "reason"}
    assert row["over_budget"] is True and WEEKLY_USD == 5.0
    with pytest.raises(ValueError):
        release_row(week="w", decision="nonsense", version="v", previous=None, base="b", run="r", adapter_sha256="",
                    gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0,
                    rogue_ledger_ts=0, reason="")
    assert "nonsense" not in DECISIONS


def test_versions_ledger_chains_and_finds_previous(tmp_path):
    v = Versions(tmp_path / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={},
              verdict_window={}, evals={}, spend_usd=1.0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="v3", previous=None, **kw))
    v.append(release_row(week="W2", decision="held", version="v4", previous="v3", **kw))
    v.append(release_row(week="W3", decision="released", version="v5", previous="v3", **kw))
    assert v.last_released()["version"] == "v5"
    assert v.previous_released(before="v5")["version"] == "v3"
    assert v.previous_released(before="v3") is None
    assert v.verify() is True and len(v.rows()) == 3
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: grid.versions`.

- [ ] **Step 3: Implement**

```python
# grid/versions.py
"""The versions ledger (spec §4): one hash-chained row per weekly decision."""
from __future__ import annotations

import time
from pathlib import Path

from .verdicts import Ledger

WEEKLY_USD = 5.0
DECISIONS = ("released", "held", "rolled_back", "unchanged", "flip_incomplete")
ROW_KEYS = ("ts", "week", "decision", "version", "previous", "base", "run", "adapter_sha256", "gguf_sha256",
            "set_manifest_sha256", "grid_examples", "verdict_window", "evals", "spend_usd", "over_budget",
            "rogue_ledger_ts", "reason")


def decide(*, candidate: str | None, released: str | None, identity: bool, validity: bool) -> tuple[str, str]:
    if not candidate:
        return "held", "no promotion this week"
    if candidate == released:
        return "unchanged", f"{candidate} is already released"
    if not identity:
        return "held", "identity probe failed"
    if not validity:
        return "held", "validity probe failed"
    return "released", "both probes passed"


def release_row(*, week: str, decision: str, version: str | None, previous: str | None, base: str, run: str,
                adapter_sha256: str, gguf_sha256: str, set_manifest_sha256: str, grid_examples: dict,
                verdict_window: dict, evals: dict, spend_usd: float, rogue_ledger_ts: float, reason: str,
                ts: float | None = None) -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")
    row = {"ts": ts if ts is not None else time.time(), "week": week, "decision": decision, "version": version,
           "previous": previous, "base": base, "run": run, "adapter_sha256": adapter_sha256, "gguf_sha256": gguf_sha256,
           "set_manifest_sha256": set_manifest_sha256, "grid_examples": grid_examples, "verdict_window": verdict_window,
           "evals": evals, "spend_usd": float(spend_usd), "over_budget": float(spend_usd) > WEEKLY_USD,
           "rogue_ledger_ts": rogue_ledger_ts, "reason": reason}
    assert tuple(row) == ROW_KEYS
    return row


class Versions:
    def __init__(self, path: Path):
        self.ledger = Ledger(Path(path))

    def rows(self) -> list[dict]:
        return self.ledger.rows()

    def last_released(self) -> dict | None:
        rel = [r for r in self.rows() if r["decision"] in ("released", "rolled_back")]
        return rel[-1] if rel else None

    def previous_released(self, *, before: str) -> dict | None:
        prev = None
        for r in self.rows():
            if r["decision"] == "released":
                if r["version"] == before:
                    return prev
                prev = r
        return None

    def append(self, row: dict) -> dict:
        # Ledger.append keys the settled set on request_id; versions have none, so give it one.
        return self.ledger.append(dict(row, request_id=f"{row['week']}:{row['decision']}:{row['version']}"))

    def verify(self) -> bool:
        return self.ledger.verify()

    def lock(self):
        return self.ledger.lock()
```

Check `Ledger.append`'s `_ids.add(full["request_id"])` — that is why `request_id` is injected; `rows()` returns it too, which is fine (the spec's key set is the row *as built*; the chain and `request_id` are ledger bookkeeping). Note `rolled_back` rows count as "released state" for `last_released()`.

- [ ] **Step 4: Run tests** → PASS. **Step 5: Commit** `feat(versions): hash-chained release ledger and the decision matrix`.

---

### Task 5: `grid/weekly.py` — `release`, `rollback`, `status`

**Files:**
- Create: `grid/weekly.py`
- Test: `tests/test_weekly.py`

**Interfaces:**
- Consumes: `Remote`, probes (Task 3), `Versions`/`decide`/`release_row` (Task 4), `grid.verdicts.Ledger` (colony A/B advisory reads `verdicts.jsonl` rows' `model_alias`, `tier`, `births`), `set-mind-model.sh --relay-only` on Skywave (local `subprocess.run`).
- Produces: `WeeklyConfig.from_env()` with env keys `DEBAIN2_HOST` (default `debain2`), `RELEASES_DIR` (default `/srv/grid/releases`), `ROGUE_CURRENT_MODEL` (`/srv/rogue/current-model`), `ROGUE_LEDGER` (`/srv/rogue/ledger.jsonl`), `ROGUE_RUNS_DIR` (`/home/nick/.kannaka-corpus/runs`), `MIND_LLM_URL`, `MIND_LLM_KEY_FILE`, `VERDICTS_DIR`, `GRID_ENV` (`/srv/grid/grid.env`), `SET_MIND_MODEL_SH` (`/home/nick/grid-deploy/set-mind-model.sh`), `CURRENT_ALIAS` (`kannaka-brain-current`), `PROBE_TIMEOUT_S` (900); functions `find_candidate(remote, cfg) -> dict | None` (keys `tag`, `run`, `rogue_ts`, `manifest`), `hashes(remote, run_dir) -> dict`, `colony_ab(verdicts_dir) -> dict`, `flip(cfg, remote, version, *, local_run) -> str` (returns `"ok"|"flip_failed"|"flip_incomplete"`), `release(cfg, remote, *, ask_factory, local_run, week) -> dict` (the row), `rollback(cfg, remote, *, to=None, local_run) -> dict`, CLI `python -m grid.weekly {release,rollback,status} [--dry-run] [--week]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_weekly.py
import json
from pathlib import Path

import pytest

from grid.weekly import WeeklyConfig, colony_ab, find_candidate, flip, release, rollback


class FakeRemote:
    def __init__(self, files: dict[str, str], fail: set[str] = frozenset()):
        self.files, self.fail, self.commands = files, set(fail), []
    def run(self, cmd, *, timeout=120):
        self.commands.append(cmd)
        from grid.remote import RemoteResult
        if any(cmd.startswith(f) for f in self.fail):
            return RemoteResult(1, "", "boom")
        if cmd.startswith("cat "):
            path = cmd.split(" ", 1)[1].strip("'")
            return RemoteResult(0, self.files.get(path, ""), "" if path in self.files else "missing")
        if cmd.startswith("sha256sum "):
            return RemoteResult(0, "e" * 64 + "  x\n", "")
        if cmd.startswith("ollama show"):
            return RemoteResult(0, "FROM /srv/kax/brains/kannaka-brain-v4.gguf\n", "")
        return RemoteResult(0, "", "")
    def read(self, path):
        from grid.remote import RemoteError
        if path not in self.files: raise RemoteError(path)
        return self.files[path]


def _cfg(tmp_path):
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_MODEL=kannaka-brain-current\n")
    (tmp_path / "key").write_text("k\n")
    return WeeklyConfig(debain2_host="debain2", releases_dir=tmp_path / "releases", rogue_current_model="/srv/rogue/current-model",
                        rogue_ledger="/srv/rogue/ledger.jsonl", rogue_runs_dir="/home/nick/.kannaka-corpus/runs",
                        gateway_url="http://gw/v1", key_file=tmp_path / "key", verdicts_dir=tmp_path / "v",
                        grid_env=tmp_path / "grid.env", set_mind_model_sh="/x/set-mind-model.sh", current_alias="kannaka-brain-current",
                        probe_timeout_s=5)


MANIFEST = json.dumps({"base": "Qwen/Qwen2.5-14B-Instruct", "holdout_ppl": {"before": 104.4, "after": 4.0}, "seconds": 781,
                       "served_as": "kannaka-brain-v4", "promoted_at": 1.7e9})
ROGUE_LEDGER = json.dumps({"ts": 1.7e9, "event": "promotion", "model": "kannaka-brain-v4", "run": "gpu-a100-sxm-20260913-0305"}) + "\n"


def test_find_candidate_reads_current_model_and_promotion_row(tmp_path):
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    c = find_candidate(r, _cfg(tmp_path))
    assert c["tag"] == "kannaka-brain-v4" and c["run"] == "gpu-a100-sxm-20260913-0305" and c["manifest"]["holdout_ppl"]["after"] == 4.0
    assert find_candidate(FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n"}), _cfg(tmp_path)) is None


def test_colony_ab_reports_births_per_proposal_by_alias(tmp_path):
    v = tmp_path / "v"; v.mkdir()
    rows = [{"model_alias": "kannaka-brain-v3", "tier": "reproduced", "births": 2, "fitness": 2},
            {"model_alias": "kannaka-brain-v3", "tier": "unborn", "births": 0, "fitness": 0},
            {"model_alias": "agent-brain", "tier": "new", "births": 1, "fitness": 1}]
    from grid.verdicts import Ledger
    led = Ledger(v / "verdicts.jsonl")
    for i, row in enumerate(rows): led.append(dict(row, request_id=f"r{i}"))
    ab = colony_ab(v)
    assert ab["advisory"] is True
    assert ab["by_alias"]["kannaka-brain-v3"] == {"proposals": 2, "births_per_proposal": 1.0, "reproduced_rate": 0.5}


def test_flip_order_and_revert_on_failed_tag_copy(tmp_path):
    cfg = _cfg(tmp_path); local = []
    r = FakeRemote({}, fail={"ollama cp"})
    assert flip(cfg, r, "kannaka-brain-v4", local_run=lambda cmd: local.append(cmd) or 0) == "flip_failed"
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in (tmp_path / "grid.env").read_text()   # reverted
    assert local == []                                                                   # relay never restarted
    r = FakeRemote({})
    assert flip(cfg, r, "kannaka-brain-v4", local_run=lambda cmd: local.append(cmd) or 0) == "ok"
    assert r.commands == ["ollama cp kannaka-brain-v4 kannaka-brain-current"]
    assert "MIND_MODEL_ALIAS=kannaka-brain-v4" in (tmp_path / "grid.env").read_text()
    assert local == [["sudo", "-n", "systemctl", "restart", "grid-odin.service"]]


async def test_release_holds_on_probe_failure_and_releases_on_pass(tmp_path):
    cfg = _cfg(tmp_path); (tmp_path / "v" / "prompts").mkdir(parents=True)
    (tmp_path / "v" / "proposals.jsonl").write_text("")
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def qwen(prompt): return "I am Qwen."
    row = await release(cfg, r, ask_factory=lambda model: qwen, local_run=lambda cmd: 0, week="2026-W37")
    assert row["decision"] == "held" and row["reason"] == "identity probe failed"
    assert not any(c.startswith("ollama cp") for c in r.commands)
    async def her(prompt): return '["harvest", "nop", "fork"]' if "Instruction set" in prompt else "I am Kannaka."
    row = await release(cfg, r, ask_factory=lambda model: her, local_run=lambda cmd: 0, week="2026-W37")
    assert row["decision"] == "released" and row["version"] == "kannaka-brain-v4" and row["previous"] is None
    assert row["adapter_sha256"] == "e" * 64 and row["spend_usd"] == pytest.approx(781 / 3600 * 2.49, rel=0.01)
    from grid.versions import Versions
    assert Versions(cfg.releases_dir / "versions.jsonl").verify() is True
    row = await release(cfg, r, ask_factory=lambda model: her, local_run=lambda cmd: 0, week="2026-W37")
    assert row["decision"] == "unchanged"


async def test_rollback_picks_previous_release(tmp_path):
    cfg = _cfg(tmp_path)
    from grid.versions import Versions, release_row
    v = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="kannaka-brain-v3", previous=None, **kw))
    v.append(release_row(week="W2", decision="released", version="kannaka-brain-v4", previous="kannaka-brain-v3", **kw))
    r = FakeRemote({})
    row = rollback(cfg, r, local_run=lambda cmd: 0)
    assert row["decision"] == "rolled_back" and row["version"] == "kannaka-brain-v3" and row["previous"] == "kannaka-brain-v4"
    assert r.commands == ["ollama cp kannaka-brain-v3 kannaka-brain-current"]
    with pytest.raises(SystemExit):
        rollback(cfg, r, to="kannaka-brain-v9", local_run=lambda cmd: 0)     # never released: refuse
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: grid.weekly`.

- [ ] **Step 3: Implement**

```python
# grid/weekly.py
"""Weekly release gate, alias flip, and rollback (spec §3–§5, §7).

`rogue.weekly` on debain2 trains and promotes; this runs afterwards on Skywave,
probes the promoted candidate, ledgers the decision, and moves the alias the
colony relay follows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .remote import Remote, RemoteError
from .verdicts import Ledger
from .versions import Versions, decide, release_row
from .weekly_probe import chat, run_identity, run_validity

log = logging.getLogger("grid.weekly")
A100_USD_PER_H = 2.49        # run_qbraid's gpu-a100-sxm rate; the manifest records seconds, not dollars


@dataclass(frozen=True)
class WeeklyConfig:
    debain2_host: str
    releases_dir: Path
    rogue_current_model: str
    rogue_ledger: str
    rogue_runs_dir: str
    gateway_url: str
    key_file: Path
    verdicts_dir: Path
    grid_env: Path
    set_mind_model_sh: str
    current_alias: str
    probe_timeout_s: float

    @classmethod
    def from_env(cls) -> "WeeklyConfig":
        env = os.environ.get
        colony = env("COLONY_SLUG", "colony-one")
        return cls(debain2_host=env("DEBAIN2_HOST", "debain2"),
                   releases_dir=Path(env("RELEASES_DIR", "/srv/grid/releases")),
                   rogue_current_model=env("ROGUE_CURRENT_MODEL", "/srv/rogue/current-model"),
                   rogue_ledger=env("ROGUE_LEDGER", "/srv/rogue/ledger.jsonl"),
                   rogue_runs_dir=env("ROGUE_RUNS_DIR", "/home/nick/.kannaka-corpus/runs"),
                   gateway_url=env("MIND_LLM_URL", "http://10.30.0.30:4000/v1"),
                   key_file=Path(env("MIND_LLM_KEY_FILE", f"/srv/grid/{colony}/mind-llm.key")),
                   verdicts_dir=Path(env("VERDICTS_DIR", f"/srv/grid/{colony}/verdicts")),
                   grid_env=Path(env("GRID_ENV", "/srv/grid/grid.env")),
                   set_mind_model_sh=env("SET_MIND_MODEL_SH", "/home/nick/grid-deploy/set-mind-model.sh"),
                   current_alias=env("CURRENT_ALIAS", "kannaka-brain-current"),
                   probe_timeout_s=float(env("PROBE_TIMEOUT_S", "900")))


def _env_get(path: Path, key: str) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return None


def _env_set(path: Path, key: str, value: str) -> None:
    lines, done = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            lines.append(f"{key}={value}"); done = True
        else:
            lines.append(line)
    if not done:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def find_candidate(remote, cfg: WeeklyConfig) -> dict | None:
    try:
        tag = remote.read(cfg.rogue_current_model).strip()
        rows = [json.loads(l) for l in remote.read(cfg.rogue_ledger).splitlines() if l.strip()]
    except (RemoteError, ValueError) as e:
        log.error("cannot read the Rogue promotion state: %s", e)
        return None
    promos = [r for r in rows if r.get("event") == "promotion" and r.get("model") == tag]
    if not tag or not promos:
        return None
    p = promos[-1]
    run = p.get("run") or ""
    try:
        manifest = json.loads(remote.read(f"{cfg.rogue_runs_dir}/{run}/train.manifest.json"))
    except (RemoteError, ValueError):
        manifest = {}
    return {"tag": tag, "run": run, "rogue_ts": float(p.get("ts", 0)), "manifest": manifest}


def hashes(remote, cfg: WeeklyConfig, run: str) -> dict:
    """sha256 of the adapter and the served GGUF, computed on debain2; nothing is copied."""
    out = {"adapter_sha256": "", "gguf_sha256": "", "set_manifest_sha256": ""}
    r = remote.run(f"sha256sum {shlex.quote(cfg.rogue_runs_dir + '/' + run + '/adapter/adapter_model.safetensors')}")
    if r.returncode == 0:
        out["adapter_sha256"] = r.stdout.split()[0]
    # rogue.weekly writes no manifest into its


def gguf_hash(remote, tag: str) -> str:
    r = remote.run(f"ollama show --modelfile {shlex.quote(tag)}")
    m = re.search(r"^FROM\s+(\S+)", r.stdout, re.M)
    if r.returncode != 0 or not m:
        return ""
    r = remote.run(f"sha256sum {shlex.quote(m.group(1))}", timeout=900)
    return r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else ""


def colony_ab(verdicts_dir: Path) -> dict:
    by: dict[str, dict] = {}
    for row in Ledger(Path(verdicts_dir) / "verdicts.jsonl").rows():
        a = by.setdefault(str(row.get("model_alias", "")), {"proposals": 0, "births": 0, "reproduced": 0})
        a["proposals"] += 1
        a["births"] += int(row.get("births") or 0)
        a["reproduced"] += 1 if row.get("tier") in ("reproduced", "growing", "enduring", "established") else 0
    return {"advisory": True, "by_alias": {
        k: {"proposals": v["proposals"], "births_per_proposal": v["births"] / v["proposals"],
            "reproduced_rate": v["reproduced"] / v["proposals"]} for k, v in by.items() if v["proposals"]}}


def flip(cfg: WeeklyConfig, remote, version: str, *, local_run) -> str:
    """Spec §7 order: local env, remote tag copy, relay restart."""
    before = _env_get(cfg.grid_env, "MIND_MODEL_ALIAS")
    _env_set(cfg.grid_env, "MIND_MODEL_ALIAS", version)
    r = remote.run(f"ollama cp {shlex.quote(version)} {shlex.quote(cfg.current_alias)}", timeout=600)
    if r.returncode != 0:
        if before is not None:
            _env_set(cfg.grid_env, "MIND_MODEL_ALIAS", before)
        log.error("tag copy failed: %s", r.stderr.strip())
        return "flip_failed"
    # The relay already asks cfg.current_alias (one-time install); a release only changes
    # the author label in grid.env and restarts the relay so it reads it.
    cmd = ["sudo", "-n", "systemctl", "restart", "grid-odin.service"]
    for attempt in (1, 2):
        if local_run(cmd) == 0:
            return "ok"
        log.error("relay restart attempt %d failed", attempt)
    return "flip_incomplete"


def _iso_week() -> str:
    return time.strftime("%G-W%V")


async def release(cfg: WeeklyConfig, remote, *, ask_factory, local_run, week: str | None = None) -> dict:
    week = week or _iso_week()
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    last = versions.last_released()
    released = last["version"] if last else None
    cand = find_candidate(remote, cfg)
    common = dict(week=week, base=(cand or {}).get("manifest", {}).get("base", ""), run=(cand or {}).get("run", ""),
                  grid_examples={}, verdict_window=_window(cfg.verdicts_dir), rogue_ledger_ts=(cand or {}).get("rogue_ts", 0.0))
    if cand is None or cand["tag"] == released:
        decision, reason = decide(candidate=(cand or {}).get("tag"), released=released, identity=True, validity=True)
        row = release_row(decision=decision, version=(cand or {}).get("tag"), previous=released, adapter_sha256="", gguf_sha256="",
                          set_manifest_sha256="", evals={"colony_ab": colony_ab(cfg.verdicts_dir)}, spend_usd=0.0, reason=reason, **common)
        return _ledger(versions, row)
    ask = ask_factory(cand["tag"])
    identity = await run_identity(ask)
    validity = await run_validity(ask, cfg.verdicts_dir)
    evals = {"holdout_ppl": cand["manifest"].get("holdout_ppl", {}), "identity": identity, "validity": validity,
             "colony_ab": colony_ab(cfg.verdicts_dir)}
    spend = float(cand["manifest"].get("seconds", 0)) / 3600 * A100_USD_PER_H
    h = hashes(remote, cfg, cand["run"])
    h["gguf_sha256"] = gguf_hash(remote, cand["tag"])
    decision, reason = decide(candidate=cand["tag"], released=released, identity=identity["pass"], validity=validity["pass"])
    if decision == "released":
        outcome = flip(cfg, remote, cand["tag"], local_run=local_run)
        if outcome == "flip_failed":
            decision, reason = "held", "flip failed: tag copy"
        elif outcome == "flip_incomplete":
            decision, reason = "flip_incomplete", "alias serves the candidate; relay restart failed twice"
    row = release_row(decision=decision, version=cand["tag"], previous=released, evals=evals, spend_usd=spend, reason=reason, **h, **common)
    return _ledger(versions, row)


def _window(verdicts_dir: Path) -> dict:
    led = Ledger(Path(verdicts_dir) / "verdicts.jsonl")
    rows = led.rows()
    ts = [r.get("settled_tick") for r in rows]
    return {"since_ts": None, "until_ts": None, "chain_head": led.head(), "rows": len(rows)}


def _ledger(versions: Versions, row: dict) -> dict:
    with versions.lock():
        versions.append(row)
    log.info("%s: %s (%s)", row["week"], row["decision"], row["reason"])
    return row


def rollback(cfg: WeeklyConfig, remote, *, to: str | None = None, local_run) -> dict:
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    last = versions.last_released()
    if last is None:
        raise SystemExit("nothing has been released; nothing to roll back")
    target = versions.previous_released(before=last["version"]) if to is None else \
        next((r for r in versions.rows() if r["decision"] == "released" and r["version"] == to), None)
    if target is None:
        raise SystemExit(f"{to or 'the previous release'} was never released; refusing")
    outcome = flip(cfg, remote, target["version"], local_run=local_run)
    decision = "rolled_back" if outcome == "ok" else outcome
    row = release_row(week=_iso_week(), decision=decision if decision in ("rolled_back", "flip_incomplete") else "held",
                      version=target["version"], previous=last["version"], base=target.get("base", ""), run=target.get("run", ""),
                      adapter_sha256=target.get("adapter_sha256", ""), gguf_sha256=target.get("gguf_sha256", ""),
                      set_manifest_sha256=target.get("set_manifest_sha256", ""), grid_examples={}, verdict_window=_window(cfg.verdicts_dir),
                      evals={}, spend_usd=0.0, rogue_ledger_ts=0.0, reason=f"rollback from {last['version']}: {outcome}")
    return _ledger(versions, row)


def _local_run(cmd: list[str]) -> int:
    return subprocess.run(cmd, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="grid.weekly")
    ap.add_argument("verb", choices=("release", "rollback", "status"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--week", default=None)
    ap.add_argument("--to", default=None)
    a = ap.parse_args(argv)
    cfg = WeeklyConfig.from_env()
    cfg.releases_dir.mkdir(parents=True, exist_ok=True)
    remote = Remote(cfg.debain2_host, dry_run=a.dry_run, log=lambda s: log.info("%s", s))
    if a.verb == "status":
        v = Versions(cfg.releases_dir / "versions.jsonl")
        print(json.dumps({"released": (v.last_released() or {}).get("version"), "rows": len(v.rows()), "verify": v.verify(),
                          "candidate": find_candidate(remote, cfg)}, indent=1, default=str))
        return 0
    key = cfg.key_file.read_text(encoding="utf-8").strip()
    local_run = (lambda cmd: (log.info("[dry-run] %s", " ".join(cmd)) or 0)) if a.dry_run else _local_run
    if a.verb == "rollback":
        row = rollback(cfg, remote, to=a.to, local_run=local_run) if not a.dry_run else {"decision": "dry-run"}
        print(json.dumps(row, default=str)); return 0
    ask_factory = lambda model: (lambda prompt: chat(cfg.gateway_url, key, model, prompt, timeout_s=cfg.probe_timeout_s))
    if a.dry_run:
        async def fake(prompt): return "I am Kannaka. []"
        ask_factory = lambda model: fake
    row = asyncio.run(release(cfg, remote, ask_factory=ask_factory, local_run=local_run, week=a.week))
    print(json.dumps({k: row[k] for k in ("week", "decision", "version", "previous", "reason", "spend_usd", "over_budget")}, default=str))
    return 0 if row["decision"] != "flip_incomplete" else 3


if __name__ == "__main__":
    sys.exit(main())
```

Notes for the implementer: `hashes()`'s set-manifest path guesses rogue.weekly's `weekly-<stamp>-data` naming from the run name; if the stamp does not match, the hash is `""`, which is acceptable (spec §4 says hashes are best-effort on debain2). In `--dry-run`, `release` still appends a ledger row: make `_ledger` skip the append when `remote.dry_run` is true (pass `dry_run` through `release`/`rollback` as a keyword defaulting to `False`, set from `a.dry_run`); the tests above call with the default. `_window` reports the ledger head and row count; `since_ts`/`until_ts` stay `None` until the export manifest (Task 1) is read back from debain2 in a later version.

- [ ] **Step 4: Run tests** → PASS (`pytest tests/test_weekly.py -q -W error`, then the full suite). **Step 5: Commit** `feat(weekly): release gate, alias flip, rollback, status`.

---

### Task 6: Deploy — Skywave timer, debain2 one-time alias install, env, README §5

**Files:**
- Create: `deploy/systemd/grid-weekly-release.service`, `deploy/systemd/grid-weekly-release.timer`, `deploy/skywave/install-current-alias.sh`
- Modify: `deploy/skywave/deploy.sh` (§5 block), `deploy/grid.env.example`, `deploy/README.md` (§5), `deploy/run-skywave.ps1` (stage the new script)
- Test: `tests/test_deploy_units.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_deploy_units.py
def test_weekly_release_timer_runs_sunday_morning_after_the_rogue_weekly():
    svc = unit("grid-weekly-release.service")
    assert "Type=oneshot" in svc and "grid.weekly release" in svc and "EnvironmentFile=/srv/grid/grid.env" in svc
    assert "/srv/grid/venv/bin/python" in svc
    timer = unit("grid-weekly-release.timer")
    assert "OnCalendar=Sun *-*-* 06:00:00" in timer and "Persistent=true" in timer
```

- [ ] **Step 2: Run** → `FileNotFoundError`.

- [ ] **Step 3: Write the units and scripts**

`deploy/systemd/grid-weekly-release.service`:
```
[Unit]
Description=kannaka-grid weekly release gate: probe the Rogue promotion, ledger it, move kannaka-brain-current
After=network-online.target grid-verdicts.service
Wants=network-online.target

[Service]
Type=oneshot
User=nick
Group=nick
WorkingDirectory=/srv/grid/kannaka-grid
EnvironmentFile=/srv/grid/grid.env
ExecStart=/srv/grid/venv/bin/python -m grid.weekly release
```
`deploy/systemd/grid-weekly-release.timer`:
```
[Unit]
Description=Weekly release gate, three hours after rogue-weekly on debain2

[Timer]
OnCalendar=Sun *-*-* 06:00:00
Persistent=true

[Install]
WantedBy=timers.target
```
`deploy/skywave/install-current-alias.sh` (runs ON debain2, invoked from Skywave over `ssh debain2`; idempotent):
```bash
#!/usr/bin/env bash
# install-current-alias.sh - one-time: create the kannaka-brain-current ollama tag from the
# served version and register it in the gateway, then mint the colony key that lists the
# current alias and every kannaka-brain-v* (the gateway keys list literal aliases).
#   bash install-current-alias.sh <initial version tag>   e.g. kannaka-brain-v3
# Prints nothing secret; the minted key lands in ~/.grid-key-for-skywave (0600) for scp.
set -euo pipefail
V=${1:?initial version tag}
CFG=/srv/kax/gateway/config.yaml
ollama list | grep -q "^kannaka-brain-current" || ollama cp "$V" kannaka-brain-current
grep -q "model_name: kannaka-brain-current" $CFG || python3 - <<'PY'
p="/srv/kax/gateway/config.yaml"; s=open(p).read()
s=s.replace("litellm_settings:", """  - model_name: kannaka-brain-current
    litellm_params:
      model: ollama_chat/kannaka-brain-current
      api_base: http://172.18.0.1:11434
litellm_settings:""", 1)
open(p,"w").write(s)
PY
docker restart kax-gateway >/dev/null && sleep 8
M=$(grep '^LITELLM_MASTER_KEY=' /srv/kax/gateway/gateway.env | cut -d= -f2-)
MODELS=$(ollama list | awk 'NR>1 && $1 ~ /^kannaka-brain/ {sub(/:latest$/,"",$1); print $1}' | sort -u | python3 -c 'import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()] + ["kannaka-brain", "kannaka-brain-current"]))')
python3 - "$M" "grid-colony-one-$(date -u +%Y%m%dT%H%M%S)" "$MODELS" <<'PY' > ~/.grid-key-for-skywave.tmp
import json, sys, urllib.request
master, alias, models = sys.argv[1], sys.argv[2], sorted(set(json.loads(sys.argv[3])))
body = json.dumps({"key_alias": alias, "models": models, "max_budget": 5.0, "budget_duration": "30d", "rpm_limit": 60, "tpm_limit": 200000}).encode()
req = urllib.request.Request("http://127.0.0.1:4000/key/generate", data=body, headers={"Authorization": "Bearer " + master, "Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=30))["key"])
sys.stderr.write("minted " + alias + " for " + ", ".join(models) + "\n")
PY
[ -s ~/.grid-key-for-skywave.tmp ] && mv ~/.grid-key-for-skywave.tmp ~/.grid-key-for-skywave && chmod 600 ~/.grid-key-for-skywave
echo "[install-current-alias] kannaka-brain-current -> $V; key at ~/.grid-key-for-skywave"
```
Then in `deploy/skywave/deploy.sh` add a `§5 weekly release` block after §3's verdicts lines: `install -d -m 700 -o nick -g nick $ROOT/releases`; append `DEBAIN2_HOST=debain2`, `RELEASES_DIR=$ROOT/releases` to grid.env if absent; `sudo cp` + `enable --now grid-weekly-release.timer`; `is-active` check; log `§5 receipt`. The alias install is NOT run by deploy.sh (it touches debain2 and mints a key): README §5 documents it as the one-time step:

```bash
scp ~/grid-deploy/install-current-alias.sh debain2:~/ && ssh debain2 'bash ~/install-current-alias.sh kannaka-brain-v3'
scp debain2:~/.grid-key-for-skywave /srv/grid/colony-one/mind-llm.key && chmod 600 /srv/grid/colony-one/mind-llm.key
bash ~/grid-deploy/set-mind-model.sh http://10.30.0.30:4000/v1 kannaka-brain-current /srv/grid/colony-one/mind-llm.key --relay-only
sed -i 's/^MIND_MODEL_ALIAS=.*/MIND_MODEL_ALIAS=kannaka-brain-v3/' /srv/grid/grid.env && sudo systemctl restart grid-odin
```
Add to `deploy/grid.env.example`: `DEBAIN2_HOST=debain2`, `RELEASES_DIR=/srv/grid/releases`, and commented `ROGUE_CURRENT_MODEL`, `ROGUE_LEDGER`, `ROGUE_RUNS_DIR`, `CURRENT_ALIAS`, `PROBE_TIMEOUT_S` with their defaults. Add `install-current-alias.sh` to the list in `deploy/run-skywave.ps1` line 33. README §5 "Weekly release (self-improvement sub-project 2)": what runs when, the one-time install above, `python -m grid.weekly status|release --dry-run|rollback`, receipts (a `released` or `held` row with probe numbers; `Versions(...).verify()` True; the relay's next proposal-log entry naming the new `model_alias`; a rollback and re-release once by hand).

- [ ] **Step 4: Run tests** → PASS. **Step 5: Commit** `deploy(skywave): weekly release timer, current-alias install, README §5`.

---

### Task 7: Upstream PR to `NickFlach/rogue-agent` — consume the pull

**Files (in a fresh clone of rogue-agent, branch `feat/grid-examples`):**
- Modify: `rogue/weekly.py`, `tests/test_rogue.py`, `README.md`

**Interfaces:**
- Consumes: the tar that `/srv/grid/pull-export.sh` streams over `ssh -i <rogue-grid-pull key> nick@10.30.0.20` (records.jsonl, negatives.jsonl, manifest.json, verdicts.jsonl); the P2 `system` line rogue.weekly already uses for its own examples.
- Produces: `grid_examples(since_ts: int | None) -> tuple[list[dict], dict]` in `rogue/weekly.py`; env `ROGUE_GRID_PULL` (default `ssh -i /srv/rogue/keys/rogue-grid-pull nick@10.30.0.20`; empty disables), `ROGUE_WEEKLY_USD` default `5`.

- [ ] **Step 1: Clone and write the failing test**

```bash
gh repo clone NickFlach/rogue-agent C:/Users/nflach/source/rogue-agent && cd C:/Users/nflach/source/rogue-agent && git checkout -b feat/grid-examples
```
Find in `rogue/weekly.py` how the Rogue examples are shaped (`export_week`: `{"id", "kind", "source", "messages": [system, user, assistant]}`) and what the system line variable is called; mirror it exactly. Test (in `tests/test_rogue.py`, matching its existing style):

```python
def test_grid_examples_converts_records_and_skips_negatives(tmp_path, monkeypatch):
    from rogue import weekly
    tar = _tar({"records.jsonl": '{"id":"odin-r1","kind":"odin","source":"grid-verdicts","text":"[\\"nop\\"]","context":"P"}\n'
                                  '{"id":"chronicle-c-1-2-topic","kind":"chronicle","source":"grid-chronicle","text":"T","context":"Q"}\n',
                "negatives.jsonl": '{"id":"odin-r2","kind":"odin","text":"x","context":"P2"}\n',
                "manifest.json": '{"records":2,"by_kind":{"odin":1,"chronicle":1},"chain_head":"h"}'})
    monkeypatch.setattr(weekly, "_grid_pull", lambda since: tar)
    ex, man = weekly.grid_examples(since_ts=None)
    assert [e["kind"] for e in ex] == ["odin", "chronicle"] and ex[0]["source"] == "grid-verdicts"
    assert ex[0]["messages"][1] == {"role": "user", "content": "P"} and ex[0]["messages"][2] == {"role": "assistant", "content": '["nop"]'}
    assert ex[0]["messages"][0]["role"] == "system" and man["by_kind"] == {"odin": 1, "chronicle": 1}
```
(`_tar` builds an in-memory tar with `tarfile`; write it in the test file.)

- [ ] **Step 2: Run** → `AttributeError: grid_examples`.

- [ ] **Step 3: Implement** in `rogue/weekly.py`:

```python
GRID_PULL = os.environ.get("ROGUE_GRID_PULL", "ssh -o BatchMode=yes -i /srv/rogue/keys/rogue-grid-pull nick@10.30.0.20")


def _grid_pull(since_ts: int | None) -> bytes:
    # the forced command ignores arguments; the window is applied client-side by ts
    return subprocess.run(GRID_PULL.split(), capture_output=True, check=True, timeout=600).stdout


def grid_examples(since_ts: int | None) -> tuple[list[dict], dict]:
    """The colony's authored turns (kannaka-grid verdict export), as chat examples.
    Positives only: negatives.jsonl is a preference set for a later DPO round."""
    if not GRID_PULL:
        return [], {}
    import io, tarfile
    try:
        tf = tarfile.open(fileobj=io.BytesIO(_grid_pull(since_ts)))
        records = tf.extractfile("records.jsonl").read().decode("utf-8")
        manifest = json.loads(tf.extractfile("manifest.json").read().decode("utf-8"))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, tarfile.TarError, KeyError, ValueError) as e:
        log(f"grid pull failed: {e}; training without colony examples this week")
        return [], {}
    out = []
    for line in records.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if since_ts is not None and int((r.get("provenance") or {}).get("ts", 0) or 0) < since_ts:
            continue
        out.append({"id": r["id"], "kind": r["kind"], "source": r.get("source", "grid"),
                    "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": r.get("context", "")},
                                 {"role": "assistant", "content": r["text"]}]})
    log(f"grid examples: {len(out)} ({manifest.get('by_kind')}) chain_head={manifest.get('chain_head')}")
    return out, manifest
```
(`SYSTEM` = whatever name the file already uses for the P2 system line; `log` is its logger.) In `main()`, right after `week_examples` is built, add `gex, gman = grid_examples(None); week_examples += gex`, and record `gman.get("by_kind")` and `gman.get("chain_head")` in the data-dir manifest / ledger row the file already writes. Change `WEEKLY_USD` default from `"6"` to `"5"` and mention both in README (`ROGUE_GRID_PULL`, the $5 program ceiling).

- [ ] **Step 4: Run rogue-agent's tests** (`python -m pytest tests -q`) → PASS. **Step 5: Commit and open the PR**

```bash
git add -A && git commit -F <msgfile>   # "weekly: pull the colony's verdict export into the set; $5 ceiling"
git push -u origin feat/grid-examples
gh pr create --title "weekly: train on the colony's verdict export (kannaka-grid sub-project 2)" --body-file <bodyfile>
```
PR body: what the tar contains, the example shape, that negatives are skipped, the ceiling change, and that the forced-command key already exists on Skywave (`rogue-grid-pull@debain2`). End the body with the repo's PR footer lines. Record the PR URL in the SDD ledger; the plan does not wait for the merge.

---

### Task 8: Skywave deploy and receipts

**Files:**
- Create: `deploy/receipts/weekly-<ts>/…` (counts and rows only)
- Modify: `deploy/README.md` (receipt numbers)

- [ ] **Step 1:** Merge to main and push (finishing-a-development-branch), then from the Windows box run `.\deploy\run-skywave.ps1 -DeployOnly` (it pulls, installs, enables `grid-weekly-release.timer`). Expect the known `Connection closed` wrapper error; verify on the host: `systemctl is-active grid-weekly-release.timer`, `ls -ld /srv/grid/releases`.
- [ ] **Step 2:** One-time alias install (README §5 block): run it; confirm `curl -s http://10.30.0.30:4000/v1/models -H "Authorization: Bearer $(cat /srv/grid/colony-one/mind-llm.key)"` lists `kannaka-brain-current`; confirm the relay proposes through the alias and the proposal-log entry says `model_alias=kannaka-brain-v3`.
- [ ] **Step 3:** `python -m grid.weekly status` and `python -m grid.weekly release --dry-run` on Skywave: the dry run prints the exact remote commands and appends nothing.
- [ ] **Step 4:** A real `python -m grid.weekly release --week 2026-W36`: with v3 already served and nothing new promoted, expect `unchanged` or `released kannaka-brain-v3` as the first ledger row (the first row records the incumbent: pass `--week` and accept either); then `rollback` must refuse (`nothing to roll back to`) or, if two releases exist, flip and flip back. Capture: the ledger rows (`versions.jsonl` verbatim is fine: hashes and counts only), `Versions(...).verify()`, the probe numbers, the relay's proposal-log tail (author field only), timer status.
- [ ] **Step 5:** Commit the receipts dir and push. Update memory.

---

## Self-review notes

- Spec coverage: §2 (dream kind, since-ts) → Task 1; §3 probes → Task 3; §4 ledger → Task 4; §5 alias/rollback and §7 flip order → Task 5; §1 timer, §5 one-time install, §7 key re-mint → Task 6; §6/§11 upstream → Task 7; §8 receipts → Task 8. Dropped per §11: the push shipper and Saturday timer.
- Names used across tasks: `Remote.run/read/copy_from` (T2 → T5), `chat/run_identity/run_validity` (T3 → T5), `Versions/decide/release_row/WEEKLY_USD` (T4 → T5), `set-mind-model.sh --relay-only` (existing → T5/T6), `Ledger` (existing → T4/T5).
- Known soft spots for the reviewer: `hashes()`'s set-manifest path guess; `_window` leaves `since_ts/until_ts` None; `Mind(bus=None)` relies on the direct route never touching the bus.
