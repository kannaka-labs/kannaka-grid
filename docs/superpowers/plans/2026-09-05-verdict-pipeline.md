# Verdict Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Log every genome the relay proposes, score it nightly against thegrid's fossil record, and export the winners as provenance-carrying training records — the self-improving program's non-circular evaluator.

**Architecture:** Three pure modules (`proposal_log`, `verdicts`, `verdict_corpus`) plus two small hooks (the relay logs proposals; the bridge logs passing chronicle lines), a oneshot systemd timer on Skywave, and receipts. Nothing in thegrid changes; the join key is thegrid's own `genome_id`.

**Tech Stack:** Python 3.12+ (3.13 on Skywave), sqlite3 (stdlib), the existing `Bus`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-verdict-pipeline-design.md` (read first). Program context: `docs/superpowers/specs/2026-09-05-self-improving-kannaka-program.md`.

## Global Constraints

- `genome_id_of(names, isa) == sha256(bytes(isa.index(n) for n in names)).hexdigest()[:16]`, identical to thegrid `history.genome_id` over op indices; `isa` is the request's `instruction_set` names in order (thegrid emits it in ISA order).
- thegrid tiers, verbatim from `history.py`: `established` births ≥ 100 and generations ≥ 100; `enduring` births ≥ 25 and gens ≥ 50; `growing` births ≥ 5 and gens ≥ 10; `reproduced` births ≥ 2; else `new`. Generations = `max_generation - first_generation`. Add `unborn` (no `genome_stats` row) and `rejected`.
- Defaults: `SETTLE_TICKS = 3000`; positive threshold for export `reproduced`; full prompt text kept per proposal; `fitness = births`.
- Verdict rows are append-only and hash-chained: `chain = sha256(prev_chain + canonical_json(row_without_chain))`, canonical = `json.dumps(sort_keys=True, separators=(",", ":"))`; genesis `prev_chain = "0"*64`.
- Paths on Skywave: `/srv/grid/<colony>/verdicts/{proposals.jsonl, prompts/<request_id>.txt, verdicts.jsonl, chronicle.jsonl}`, fossil DB `/srv/grid/<colony>/state/history.sqlite3` (read from a copy, never the live file). Exports to `~/.kannaka-corpus/grid/<YYYY-Www>/`.
- Subjects: `GRID.<colony>.verdict` with the canonical envelope plus the row's fields.
- Nothing here ever raises into the relay or bridge loops: every log write is try/except → warning.
- Corpus record contract (from kannaka-memory `export_corpus.py`): keys `id, kind, tier, source, speaker, text` always present; `context` and `provenance` added here.
- Tests are mutation-sensitive; every task names its mutation. Commit trailers as in the foundation plan. Run tests with `.venv\Scripts\python.exe -m pytest -q -W error`; worktree per the SDD process.

## File Structure

```
grid/proposal.py          + genome_id_of(names, isa)
grid/proposal_log.py      ProposalLog (append JSONL + prompt files, never raises), proposal_entry()
grid/odin.py              Relay gains proposal_log + model_alias; RelayConfig gains verdicts_dir, model_alias
grid/bridge.py            Bridge gains chronicle_log: passing lines appended to chronicle.jsonl
grid/verdicts.py          tier_of, is_settled, verdict_row, chain_hash, Ledger, StatsReader, settle_pending, run_once, main
grid/verdict_corpus.py    record_for, export, main
tests/test_proposal.py    + genome_id tests
tests/test_proposal_log.py, tests/test_verdicts.py, tests/test_verdict_corpus.py
tests/test_odin.py        + logging tests;  tests/test_bridge.py + chronicle log test
deploy/systemd/grid-verdicts.service, grid-verdicts.timer; deploy/grid.env.example; deploy/skywave/deploy.sh; deploy/README.md §4; tests/test_deploy_units.py
```

---

### Task 1: `genome_id_of` — thegrid's genome hash from names

**Files:**
- Modify: `grid/proposal.py`
- Test: `tests/test_proposal.py`

**Interfaces:**
- Produces: `genome_id_of(names: list[str], isa: list[str]) -> str` (raises `ValueError` on an unknown name); `Request.isa -> list[str]` property (instruction names in order).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_proposal.py`)

```python
def test_request_isa_is_the_instruction_names_in_order():
    assert req().isa == ["nop", "harvest", "scan", "move", "alloc", "copy", "ifnotdone", "jmpb", "fork"]


def test_genome_id_of_matches_thegrid_algorithm():
    import hashlib
    from grid.proposal import genome_id_of
    isa = ["nop", "harvest", "scan", "move", "alloc", "copy", "ifnotdone", "jmpb", "fork"]
    names = ["harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork"]
    expected = hashlib.sha256(bytes([1, 4, 5, 6, 7, 8])).hexdigest()[:16]
    assert genome_id_of(names, isa) == expected
    assert len(genome_id_of(names, isa)) == 16


def test_genome_id_of_agrees_with_thegrid_when_importable():
    import sys
    from pathlib import Path
    from grid.proposal import genome_id_of
    thegrid = Path(__file__).resolve().parents[1] / "thegrid"
    if not (thegrid / "src" / "colony" / "history.py").exists():
        pytest.skip("thegrid submodule not checked out")
    sys.path.insert(0, str(thegrid))
    try:
        from src.colony.history import genome_id  # type: ignore
    except Exception as e:  # noqa: BLE001 - thegrid may need Linux-only modules
        pytest.skip(f"thegrid not importable here: {e}")
    finally:
        sys.path.remove(str(thegrid))
    isa = ["nop", "harvest", "scan", "move", "alloc", "copy"]
    assert genome_id_of(["harvest", "copy", "nop"], isa) == genome_id([1, 5, 0])


def test_genome_id_of_rejects_unknown_name():
    from grid.proposal import genome_id_of
    with pytest.raises(ValueError):
        genome_id_of(["harvest", "teleport"], ["nop", "harvest"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_proposal.py -q`
Expected: FAIL — `ImportError: cannot import name 'genome_id_of'` / `AttributeError: isa`.

- [ ] **Step 3: Implement** (in `grid/proposal.py`: add `import hashlib` at top; add the property inside `Request` after `names`; add the function after `parse_request`)

```python
    @property
    def isa(self) -> list[str]:
        """Instruction names in ISA order — the index of a name is thegrid's opcode."""
        return [str(i["name"]) for i in self.instruction_set]
```

```python
def genome_id_of(names: list[str], isa: list[str]) -> str:
    """thegrid's genome id (history.genome_id) computed from opcode names.

    thegrid hashes the raw opcode bytes; a name's index in the ISA list is its
    opcode, so the relay can name a child before it is born.
    """
    try:
        ops = bytes(isa.index(n) for n in names)
    except ValueError as e:
        raise ValueError(f"opcode name not in the ISA: {e}") from None
    return hashlib.sha256(ops).hexdigest()[:16]
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_proposal.py -q`
Expected: `16 passed` (12 existing + 4; the thegrid-import test may report as skipped on Windows → `15 passed, 1 skipped`).

- [ ] **Step 5: Mutation, then commit**

Change `.hexdigest()[:16]` to `[:12]`: `test_genome_id_of_matches_thegrid_algorithm` fails. Revert.

```bash
git add grid/proposal.py tests/test_proposal.py
git commit -m "feat: genome_id_of — thegrid's genome hash from opcode names"
```

---

### Task 2: Proposal log

**Files:**
- Create: `grid/proposal_log.py`
- Test: `tests/test_proposal_log.py`

**Interfaces:**
- Produces: `proposal_entry(*, req: Request, proposal: list[str] | None, outcome: str, reason: str | None, epoch: int, tick: int, colony: str, mind: str, model_alias: str, prompt: str, ts_ms: int) -> dict` (pure; computes both genome ids; `prompt_sha256`; `prompt_path = f"prompts/{request_id}.txt"`); `class ProposalLog(dir: Path)` with `record(entry: dict, prompt: str) -> bool` (writes the prompt file then appends the entry line with fsync; returns False and logs on any failure, never raises) and `read_all() -> list[dict]`.
- Consumes: `Request`, `genome_id_of` (Task 1); `now_ms` (envelope).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_proposal_log.py
import json
from pathlib import Path

from grid.proposal import Request
from grid.proposal_log import ProposalLog, proposal_entry

ISA = [{"name": n, "cost": 0.1, "effect": n} for n in ("nop", "harvest", "copy", "fork")]


def req(request_id="r1") -> Request:
    return Request(request_id, 1.0, {"energy": 9.0}, ["harvest", "copy", "fork"], ISA, {}, "author one variant")


def test_entry_carries_ids_hashes_and_paths():
    e = proposal_entry(req=req(), proposal=["harvest", "harvest", "fork"], outcome="proposed", reason=None,
                       epoch=2, tick=321, colony="c", mind="grid-c", model_alias="agent-brain",
                       prompt="PROMPT TEXT", ts_ms=5)
    assert e["request_id"] == "r1" and e["outcome"] == "proposed" and e["reason"] is None
    assert e["parent_genome"] == ["harvest", "copy", "fork"] and len(e["parent_genome_id"]) == 16
    assert e["proposal"] == ["harvest", "harvest", "fork"] and len(e["proposal_genome_id"]) == 16
    assert e["proposal_genome_id"] != e["parent_genome_id"]
    assert e["prompt_path"] == "prompts/r1.txt" and len(e["prompt_sha256"]) == 64
    assert e["epoch"] == 2 and e["tick"] == 321 and e["colony"] == "c" and e["mind"] == "grid-c"
    assert e["model_alias"] == "agent-brain" and e["ts"] == 5


def test_rejected_entry_has_no_proposal():
    e = proposal_entry(req=req(), proposal=None, outcome="rejected", reason="unknown opcodes: ['x']",
                       epoch=0, tick=0, colony="c", mind="m", model_alias="a", prompt="p", ts_ms=1)
    assert e["proposal"] is None and e["proposal_genome_id"] is None and "unknown" in e["reason"]


def test_log_appends_lines_and_prompt_files(tmp_path):
    log = ProposalLog(tmp_path)
    e1 = proposal_entry(req=req("a"), proposal=["fork"], outcome="proposed", reason=None, epoch=1, tick=1,
                        colony="c", mind="m", model_alias="a", prompt="first", ts_ms=1)
    e2 = proposal_entry(req=req("b"), proposal=None, outcome="rejected", reason="r", epoch=1, tick=2,
                        colony="c", mind="m", model_alias="a", prompt="second", ts_ms=2)
    assert log.record(e1, "first") is True and log.record(e2, "second") is True
    lines = (tmp_path / "proposals.jsonl").read_text().splitlines()
    assert [json.loads(l)["request_id"] for l in lines] == ["a", "b"]
    assert (tmp_path / "prompts" / "a.txt").read_text() == "first"
    assert [x["request_id"] for x in log.read_all()] == ["a", "b"]


def test_log_never_raises_when_the_directory_is_unwritable(tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    log = ProposalLog(blocker)                      # a file where a directory is needed
    e = proposal_entry(req=req(), proposal=["fork"], outcome="proposed", reason=None, epoch=1, tick=1,
                       colony="c", mind="m", model_alias="a", prompt="p", ts_ms=1)
    assert log.record(e, "p") is False
    assert log.read_all() == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_proposal_log.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.proposal_log'`

- [ ] **Step 3: Implement**

```python
# grid/proposal_log.py
"""Every proposal the relay makes, with the prompt it saw — the dataset's input side.

Append-only JSONL beside prompt files. Writing never raises into the relay: a
proposal must still land even if the log cannot be written.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from .proposal import Request, genome_id_of

log = logging.getLogger("grid.proposal_log")


def proposal_entry(*, req: Request, proposal: list[str] | None, outcome: str, reason: str | None,
                   epoch: int, tick: int, colony: str, mind: str, model_alias: str,
                   prompt: str, ts_ms: int) -> dict:
    isa = req.isa
    return {
        "ts": ts_ms, "colony": colony, "epoch": epoch, "tick": tick,
        "request_id": req.request_id, "mind": mind, "model_alias": model_alias,
        "parent_genome": list(req.genome), "parent_genome_id": genome_id_of(req.genome, isa),
        "proposal": list(proposal) if proposal is not None else None,
        "proposal_genome_id": genome_id_of(proposal, isa) if proposal is not None else None,
        "outcome": outcome, "reason": reason,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_path": f"prompts/{req.request_id}.txt",
    }


class ProposalLog:
    def __init__(self, dir: Path):
        self.dir = Path(dir)
        self.path = self.dir / "proposals.jsonl"

    def record(self, entry: dict, prompt: str) -> bool:
        try:
            (self.dir / "prompts").mkdir(parents=True, exist_ok=True)
            (self.dir / entry["prompt_path"]).write_text(prompt, encoding="utf-8")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as e:      # the proposal still lands; only the record is lost
            log.warning("proposal log write failed for %s: %s", entry.get("request_id"), e)
            return False

    def read_all(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except OSError:
            return []
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_proposal_log.py -q` → `4 passed`.

- [ ] **Step 5: Mutation, then commit**

Remove the `try/except` in `record` (let it raise): `test_log_never_raises_when_the_directory_is_unwritable` fails. Revert.

```bash
git add grid/proposal_log.py tests/test_proposal_log.py
git commit -m "feat: proposal log — every Odin proposal with its prompt, never raising into the relay"
```

---

### Task 3: The relay logs proposals

**Files:**
- Modify: `grid/odin.py`, `deploy/grid.env.example`
- Test: `tests/test_odin.py`

**Interfaces:**
- Consumes: `ProposalLog`, `proposal_entry` (Task 2); `now_ms` (envelope).
- Produces: `Relay.__init__(..., proposal_log: ProposalLog | None = None, model_alias: str = "")`; `RelayConfig.verdicts_dir: str` (env `VERDICTS_DIR`, default `/srv/grid/<colony>/verdicts`), `RelayConfig.model_alias: str` (env `MIND_MODEL_ALIAS`, default `"agent-brain"`); `run()` wires a `ProposalLog(cfg.verdicts_dir)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_odin.py`)

```python
async def test_proposed_is_logged_with_prompt_and_ids(tmp_path):
    from grid.proposal_log import ProposalLog
    write_request(tmp_path)
    plog = ProposalLog(tmp_path / "verdicts")
    mind = FakeMind(answer='["harvest","harvest","copy","fork"]')
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=mind, bus=FakeBus(), clock=Clock(),
              state_fetch=lambda: state_of(2, 321), proposal_log=plog, model_alias="agent-brain")
    assert await r.poll_once() == "proposed"
    [e] = plog.read_all()
    assert e["outcome"] == "proposed" and e["proposal"] == ["harvest", "harvest", "copy", "fork"]
    assert e["epoch"] == 2 and e["tick"] == 321 and e["model_alias"] == "agent-brain" and e["mind"] == "grid-c"
    assert (tmp_path / "verdicts" / "prompts" / "r1.txt").read_text() == mind.asked[0]


async def test_rejected_is_logged_without_a_proposal(tmp_path):
    from grid.proposal_log import ProposalLog
    write_request(tmp_path)
    plog = ProposalLog(tmp_path / "verdicts")
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=FakeMind(answer='["teleport"]'), bus=FakeBus(),
              clock=Clock(), proposal_log=plog)
    assert await r.poll_once() == "rejected"
    [e] = plog.read_all()
    assert e["outcome"] == "rejected" and e["proposal"] is None and "teleport" in e["reason"]


async def test_relay_without_a_log_still_proposes(tmp_path):
    write_request(tmp_path)
    r = relay(tmp_path, FakeMind(answer='["harvest","fork"]'))
    assert await r.poll_once() == "proposed"


def test_config_from_env_carries_verdicts_dir_and_model_alias(monkeypatch):
    from grid.odin import RelayConfig
    for k in ("VERDICTS_DIR", "MIND_MODEL_ALIAS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    c = RelayConfig.from_env()
    assert c.verdicts_dir == "/srv/grid/colony-one/verdicts" and c.model_alias == "agent-brain"
    monkeypatch.setenv("VERDICTS_DIR", "/tmp/v")
    monkeypatch.setenv("MIND_MODEL_ALIAS", "kannaka-brain-v1")
    c = RelayConfig.from_env()
    assert c.verdicts_dir == "/tmp/v" and c.model_alias == "kannaka-brain-v1"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_odin.py -q` → FAIL (`unexpected keyword argument 'proposal_log'`, `verdicts_dir`).

- [ ] **Step 3: Implement**

In `grid/odin.py`:
- imports: `from .envelope import envelope, now_ms, subject` and `from .proposal_log import ProposalLog, proposal_entry`.
- `Relay.__init__` gains `proposal_log: ProposalLog | None = None, model_alias: str = ""`; store both.
- Add a method:

```python
    def _log(self, req, prompt: str, proposal: list[str] | None, outcome: str, reason: str | None,
             epoch: int, tick: int) -> None:
        if self.proposal_log is None:
            return
        entry = proposal_entry(req=req, proposal=proposal, outcome=outcome, reason=reason,
                               epoch=epoch, tick=tick, colony=self.colony, mind=self.agent_id,
                               model_alias=self.model_alias, prompt=prompt, ts_ms=now_ms())
        self.proposal_log.record(entry, prompt)
```

- In `poll_once`: after `await self._publish("rejected", ...)` in the rejection branch add `self._log(req, prompt, None, "rejected", result.reason, epoch, tick)`; after the `proposed` publish add `self._log(req, prompt, result, "proposed", None, epoch, tick)`.
- `RelayConfig`: fields `verdicts_dir: str`, `model_alias: str`; in `from_env`: `verdicts_dir=env("VERDICTS_DIR", f"/srv/grid/{colony}/verdicts")`, `model_alias=env("MIND_MODEL_ALIAS", "agent-brain")`.
- `run()`: `relay = Relay(..., state_fetch=fetch_state, proposal_log=ProposalLog(Path(cfg.verdicts_dir)), model_alias=cfg.model_alias)`.
- `deploy/grid.env.example`: add after `ODIN_POLL_S=1`:

```
VERDICTS_DIR=/srv/grid/colony-one/verdicts
MIND_MODEL_ALIAS=agent-brain
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_odin.py -q` → all pass (13 existing + 4).

- [ ] **Step 5: Mutation, then commit**

Delete the `self._log(...)` call in the proposed branch: `test_proposed_is_logged_with_prompt_and_ids` fails. Revert.

```bash
git add grid/odin.py deploy/grid.env.example tests/test_odin.py
git commit -m "feat: relay logs every proposal and rejection with its prompt"
```

---

### Task 4: The bridge logs passing chronicle lines

**Files:**
- Modify: `grid/bridge.py`
- Test: `tests/test_bridge.py`

**Interfaces:**
- Produces: `Bridge.__init__(..., chronicle_log: Path | None = None)`; `BridgeConfig.verdicts_dir: str | None` (env `VERDICTS_DIR`); each published chronicle line is appended to `<verdicts_dir>/chronicle.jsonl` as `{"ts","colony","epoch","tick","kind","question","text","since_tick"}`; failures log and continue.

- [ ] **Step 1: Write the failing test** (append to `tests/test_bridge.py`)

```python
async def test_published_chronicle_lines_are_logged(tmp_path):
    import json as _json
    bus = FakeBus()
    mind = FakeMind(answers={"What is this epoch about": "A slow squeeze.",
                             "Name the capability": "fork early via fork.",
                             "single most important": "Sweep at tick 1900."})
    bridge = Bridge(cfg(), bus=bus, mind=mind, chronicle_log=tmp_path / "chronicle.jsonl")
    await bridge.handle_state(state(tick=CHRONICLE_EVERY_TICKS))
    out = await bridge.maybe_chronicle()
    assert set(out) == {"topic", "skill", "event"}
    rows = [_json.loads(l) for l in (tmp_path / "chronicle.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["topic", "skill", "event"]
    assert rows[1]["text"] == "fork early via fork." and "Name the capability" in rows[1]["question"]
    assert rows[0]["epoch"] == 1 and rows[0]["tick"] == CHRONICLE_EVERY_TICKS and rows[0]["colony"] == "colony-one"


async def test_chronicle_log_failure_does_not_block_publishing(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    bus = FakeBus()
    mind = FakeMind(answers={"What is this epoch about": "Fine."})
    bridge = Bridge(cfg(), bus=bus, mind=mind, chronicle_log=blocker / "chronicle.jsonl")
    await bridge.handle_state(state(tick=CHRONICLE_EVERY_TICKS))
    out = await bridge.maybe_chronicle()
    assert out["topic"] == "Fine."
    assert [s for s, _ in bus.published if s.endswith(".chronicle.topic")]
```

- [ ] **Step 2: Run to verify they fail** → `unexpected keyword argument 'chronicle_log'`.

- [ ] **Step 3: Implement**

In `grid/bridge.py`: `Bridge.__init__` gains `chronicle_log: Path | None = None` (store as `self.chronicle_log`). In `maybe_chronicle`, after the successful `bus.publish` of a kind (inside the `try`, after the await):

```python
                self._log_chronicle(kind, q, answer, snap, since)
```

Add the method:

```python
    def _log_chronicle(self, kind: str, question: str, text: str, snap: Snapshot, since: int) -> None:
        if self.chronicle_log is None:
            return
        row = {"ts": now_ms(), "colony": self.colony, "epoch": snap.epoch, "tick": snap.tick,
               "kind": kind, "question": question, "text": text, "since_tick": since}
        try:
            self.chronicle_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.chronicle_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        except Exception as e:      # the line is already published; only the log is lost
            log.warning("chronicle log write failed: %s", e)
```

Import `now_ms` from `.envelope`. `BridgeConfig` gains `verdicts_dir: str | None` (`env("VERDICTS_DIR") or None`); `run()` passes `chronicle_log=Path(cfg.verdicts_dir) / "chronicle.jsonl" if cfg.verdicts_dir else None`. Update `tests/test_bridge.py::cfg()` to pass `verdicts_dir=None` and `test_config_from_env_defaults` to assert `c.verdicts_dir is None`.

- [ ] **Step 4: Run** `python -m pytest tests/test_bridge.py -q` → all pass.

- [ ] **Step 5: Mutation, then commit**

Remove the `self._log_chronicle(...)` call: the first new test fails. Revert.

```bash
git add grid/bridge.py tests/test_bridge.py
git commit -m "feat: bridge logs every published chronicle line for the verdict corpus"
```

---

### Task 5: Verdicts — tiers, settle rule, hash chain, stats reader

**Files:**
- Create: `grid/verdicts.py`
- Test: `tests/test_verdicts.py`

**Interfaces:**
- Produces:
  - `SETTLE_TICKS = 3000`; `TIERS = ("established", "enduring", "growing", "reproduced", "new")`.
  - `tier_of(births: int, generations: int) -> str`.
  - `is_settled(entry_epoch: int, entry_tick: int, *, now_epoch: int, now_tick: int, settle_ticks: int = SETTLE_TICKS) -> bool` — True if `now_epoch > entry_epoch` or `now_tick - entry_tick >= settle_ticks`.
  - `canonical(row: dict) -> str`; `chain_hash(prev: str, row: dict) -> str` (row without `chain`).
  - `verdict_row(entry: dict, stats: dict | None, *, settled_tick: int) -> dict` with keys `request_id, proposal_genome_id, epoch, settled_tick, births, generations, lifetime_ticks, tier, fitness, model_alias, outcome`; rejected entries → tier `rejected`, births 0; no stats → `unborn`.
  - `class Ledger(path: Path)` with `rows() -> list[dict]`, `head() -> str` (genesis `"0"*64`), `append(row: dict) -> dict` (adds `chain`, fsyncs), `verify() -> bool`, `settled_ids() -> set[str]`.
  - `class StatsReader(db_path: Path)` context manager: copies the DB to a temp file, opens read-only; `stats(epoch: int, genome_id: str) -> dict | None` returning `{births, first_generation, max_generation, first_tick, last_tick}`.
  - `settle_pending(entries: list[dict], ledger: Ledger, reader: StatsReader, *, now_epoch: int, now_tick: int, settle_ticks: int = SETTLE_TICKS) -> list[dict]` — appends and returns the new rows.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_verdicts.py
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from grid.verdicts import (SETTLE_TICKS, Ledger, StatsReader, chain_hash, is_settled, settle_pending,
                           tier_of, verdict_row)


def make_db(path: Path, rows: list[tuple]) -> Path:
    """genome_stats exactly as thegrid's history.py creates it."""
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE genome_stats (epoch INTEGER NOT NULL, genome_id TEXT NOT NULL,
        births INTEGER NOT NULL DEFAULT 0, starvation_deaths INTEGER NOT NULL DEFAULT 0,
        senescence_deaths INTEGER NOT NULL DEFAULT 0, first_generation INTEGER,
        max_generation INTEGER NOT NULL DEFAULT 0, first_tick INTEGER NOT NULL, last_tick INTEGER NOT NULL,
        PRIMARY KEY (epoch, genome_id))""")
    db.executemany("INSERT INTO genome_stats VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit(); db.close()
    return path


def entry(request_id="r1", *, epoch=1, tick=100, gid="g1", outcome="proposed", alias="a") -> dict:
    return {"request_id": request_id, "epoch": epoch, "tick": tick, "proposal_genome_id": gid if outcome == "proposed" else None,
            "outcome": outcome, "model_alias": alias, "reason": None if outcome == "proposed" else "bad"}


def test_tiers_match_thegrid():
    assert tier_of(150, 120) == "established"
    assert tier_of(30, 60) == "enduring"
    assert tier_of(5, 10) == "growing"
    assert tier_of(2, 0) == "reproduced"
    assert tier_of(1, 0) == "new"
    assert tier_of(5, 9) == "reproduced"          # births alone do not make 'growing'


def test_settle_rule():
    assert is_settled(1, 100, now_epoch=1, now_tick=100 + SETTLE_TICKS) is True
    assert is_settled(1, 100, now_epoch=1, now_tick=100 + SETTLE_TICKS - 1) is False
    assert is_settled(1, 100, now_epoch=2, now_tick=5) is True                 # epoch retired
    assert is_settled(1, 100, now_epoch=1, now_tick=150, settle_ticks=50) is True


def test_verdict_row_shapes():
    r = verdict_row(entry(), {"births": 40, "first_generation": 3, "max_generation": 10, "first_tick": 120, "last_tick": 3020},
                    settled_tick=3200)
    assert r["tier"] == "growing" and r["fitness"] == 40 and r["generations"] == 7 and r["lifetime_ticks"] == 2900
    assert r["request_id"] == "r1" and r["proposal_genome_id"] == "g1" and r["settled_tick"] == 3200
    assert verdict_row(entry(), None, settled_tick=3200)["tier"] == "unborn"
    rej = verdict_row(entry(outcome="rejected"), None, settled_tick=3200)
    assert rej["tier"] == "rejected" and rej["births"] == 0 and rej["fitness"] == 0


def test_chain_is_deterministic_and_verifies(tmp_path):
    led = Ledger(tmp_path / "verdicts.jsonl")
    assert led.head() == "0" * 64
    a = led.append({"request_id": "a", "tier": "new"})
    b = led.append({"request_id": "b", "tier": "growing"})
    assert a["chain"] == chain_hash("0" * 64, {"request_id": "a", "tier": "new"})
    assert b["chain"] == chain_hash(a["chain"], {"request_id": "b", "tier": "growing"})
    assert led.verify() is True and led.settled_ids() == {"a", "b"}
    lines = (tmp_path / "verdicts.jsonl").read_text().splitlines()
    tampered = json.loads(lines[0]); tampered["tier"] = "established"
    (tmp_path / "verdicts.jsonl").write_text(json.dumps(tampered) + "\n" + lines[1] + "\n")
    assert Ledger(tmp_path / "verdicts.jsonl").verify() is False


def test_stats_reader_reads_a_copy(tmp_path):
    db = make_db(tmp_path / "history.sqlite3", [(1, "g1", 40, 0, 0, 3, 10, 120, 3020)])
    with StatsReader(db) as reader:
        assert reader.stats(1, "g1") == {"births": 40, "first_generation": 3, "max_generation": 10,
                                         "first_tick": 120, "last_tick": 3020}
        assert reader.stats(1, "missing") is None
        assert reader.stats(2, "g1") is None
        assert reader.copy_path != db and Path(reader.copy_path).exists()
    assert not Path(reader.copy_path).exists()            # temp copy removed


def test_stats_reader_sees_rows_still_in_the_wal(tmp_path):
    """thegrid's DB is WAL-mode on Skywave; a file copy would miss uncheckpointed rows."""
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE genome_stats (epoch INTEGER NOT NULL, genome_id TEXT NOT NULL,
        births INTEGER NOT NULL DEFAULT 0, starvation_deaths INTEGER NOT NULL DEFAULT 0,
        senescence_deaths INTEGER NOT NULL DEFAULT 0, first_generation INTEGER,
        max_generation INTEGER NOT NULL DEFAULT 0, first_tick INTEGER NOT NULL, last_tick INTEGER NOT NULL,
        PRIMARY KEY (epoch, genome_id))""")
    db.commit()
    db.execute("PRAGMA wal_autocheckpoint=0")             # keep the row in the WAL only
    db.execute("INSERT INTO genome_stats VALUES (1,'w1',7,0,0,1,4,10,900)")
    db.commit()
    assert (tmp_path / "history.sqlite3-wal").stat().st_size > 0
    with StatsReader(path) as reader:                    # writer still open, like the colony
        assert reader.stats(1, "w1")["births"] == 7
    db.close()


def test_settle_pending_appends_only_due_and_unsettled(tmp_path):
    db = make_db(tmp_path / "history.sqlite3", [(1, "g1", 40, 0, 0, 3, 10, 120, 3020)])
    led = Ledger(tmp_path / "verdicts.jsonl")
    entries = [entry("r1", tick=100, gid="g1"), entry("r2", tick=200, gid="g2"),
               entry("r3", tick=4000, gid="g3"), entry("r4", tick=100, outcome="rejected")]
    with StatsReader(db) as reader:
        rows = settle_pending(entries, led, reader, now_epoch=1, now_tick=3300)
    assert [r["request_id"] for r in rows] == ["r1", "r2", "r4"]         # r3 not due yet
    assert [r["tier"] for r in rows] == ["growing", "unborn", "rejected"]
    with StatsReader(db) as reader:
        again = settle_pending(entries, led, reader, now_epoch=1, now_tick=3300)
    assert again == []                                                     # idempotent
    assert led.verify() is True and len(led.rows()) == 3
```

- [ ] **Step 2: Run to verify they fail** → `ModuleNotFoundError: No module named 'grid.verdicts'`.

- [ ] **Step 3: Implement**

```python
# grid/verdicts.py
"""Score every logged proposal against thegrid's fossil record.

thegrid never tags Odin births; the join is its own genome_id, which the relay
computed when it wrote the proposal. Verdict rows are append-only and hash-
chained so a training example can always be traced to a colony state.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("grid.verdicts")

SETTLE_TICKS = 3000
TIERS = ("established", "enduring", "growing", "reproduced", "new")
GENESIS = "0" * 64


def tier_of(births: int, generations: int) -> str:
    if generations >= 100 and births >= 100:
        return "established"
    if generations >= 50 and births >= 25:
        return "enduring"
    if generations >= 10 and births >= 5:
        return "growing"
    if births >= 2:
        return "reproduced"
    return "new"


def is_settled(entry_epoch: int, entry_tick: int, *, now_epoch: int, now_tick: int,
               settle_ticks: int = SETTLE_TICKS) -> bool:
    return now_epoch > entry_epoch or (now_epoch == entry_epoch and now_tick - entry_tick >= settle_ticks)


def canonical(row: dict) -> str:
    return json.dumps({k: v for k, v in row.items() if k != "chain"}, sort_keys=True, separators=(",", ":"))


def chain_hash(prev: str, row: dict) -> str:
    return hashlib.sha256((prev + canonical(row)).encode()).hexdigest()


def verdict_row(entry: dict, stats: dict | None, *, settled_tick: int) -> dict:
    if entry.get("outcome") != "proposed":
        tier, births, gens, life = "rejected", 0, 0, 0
    elif stats is None:
        tier, births, gens, life = "unborn", 0, 0, 0
    else:
        births = int(stats.get("births") or 0)
        gens = int(stats.get("max_generation") or 0) - int(stats.get("first_generation") or 0)
        life = int(stats.get("last_tick") or 0) - int(stats.get("first_tick") or 0)
        tier = tier_of(births, gens)
    return {"request_id": entry["request_id"], "proposal_genome_id": entry.get("proposal_genome_id"),
            "epoch": int(entry.get("epoch", 0)), "settled_tick": settled_tick,
            "births": births, "generations": gens, "lifetime_ticks": life, "tier": tier,
            "fitness": births, "model_alias": entry.get("model_alias", ""), "outcome": entry.get("outcome")}


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def rows(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def head(self) -> str:
        rows = self.rows()
        return rows[-1]["chain"] if rows else GENESIS

    def append(self, row: dict) -> dict:
        full = dict(row, chain=chain_hash(self.head(), row))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(full, sort_keys=True, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return full

    def verify(self) -> bool:
        prev = GENESIS
        for row in self.rows():
            if row.get("chain") != chain_hash(prev, row):
                return False
            prev = row["chain"]
        return True

    def settled_ids(self) -> set[str]:
        return {r["request_id"] for r in self.rows()}


class StatsReader:
    """Reads genome_stats from a SNAPSHOT of thegrid's fossil DB, never the live file.

    thegrid keeps the DB in WAL mode (history.sqlite3-wal beside it on Skywave), so
    a plain file copy would miss every row still in the write-ahead log. The
    snapshot is taken through SQLite's backup API on a read-only connection,
    which reads consistently through the WAL.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.copy_path: str | None = None
        self._db: sqlite3.Connection | None = None

    def __enter__(self) -> "StatsReader":
        fd, self.copy_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        src = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(self.copy_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        self._db = sqlite3.connect(f"file:{self.copy_path}?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        return self

    def __exit__(self, *exc) -> None:
        if self._db is not None:
            self._db.close()
        if self.copy_path and os.path.exists(self.copy_path):
            os.unlink(self.copy_path)

    def stats(self, epoch: int, genome_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT births, first_generation, max_generation, first_tick, last_tick "
            "FROM genome_stats WHERE epoch=? AND genome_id=?", (epoch, genome_id)).fetchone()
        return dict(row) if row else None


def settle_pending(entries: list[dict], ledger: Ledger, reader: StatsReader, *, now_epoch: int,
                   now_tick: int, settle_ticks: int = SETTLE_TICKS) -> list[dict]:
    done = ledger.settled_ids()
    out = []
    for e in entries:
        if e["request_id"] in done:
            continue
        if not is_settled(int(e.get("epoch", 0)), int(e.get("tick", 0)), now_epoch=now_epoch,
                          now_tick=now_tick, settle_ticks=settle_ticks):
            continue
        stats = reader.stats(int(e.get("epoch", 0)), e["proposal_genome_id"]) if e.get("proposal_genome_id") else None
        out.append(ledger.append(verdict_row(e, stats, settled_tick=now_tick)))
        done.add(e["request_id"])
    return out
```

- [ ] **Step 4: Run** `python -m pytest tests/test_verdicts.py -q` → `6 passed`.

- [ ] **Step 5: Mutations, then commit**

1. `tier_of`: change `births >= 2` to `births >= 1` → `test_tiers_match_thegrid` fails.
2. `settle_pending`: remove the `if e["request_id"] in done: continue` → the idempotent assertion fails.
Revert both.

```bash
git add grid/verdicts.py tests/test_verdicts.py
git commit -m "feat: verdicts — thegrid tiers, settle rule, hash-chained ledger, fossil-DB reader"
```

---

### Task 6: Verdicts daemon — run once, publish, CLI

**Files:**
- Modify: `grid/verdicts.py`
- Test: `tests/test_verdicts.py`

**Interfaces:**
- Consumes: `ProposalLog.read_all` (Task 2), `Bus`, `envelope`, `subject`.
- Produces: `async run_once(*, verdicts_dir: Path, history_db: Path, state: dict, bus=None, colony: str, agent_id: str, settle_ticks: int = SETTLE_TICKS) -> list[dict]` (reads proposals, settles, publishes each new row on `GRID.<colony>.verdict`, publish failures logged); `main(argv)` with `--verdicts-dir`, `--history-db`, `--colony-url`, `--colony`, `--settle-ticks`, `--no-publish`; env fallbacks `VERDICTS_DIR`, `HISTORY_DB` (default `/srv/grid/<colony>/state/history.sqlite3`), `COLONY_URL`, `COLONY_SLUG`, `MIND_AGENT_ID`, `SETTLE_TICKS`, NATS vars.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_verdicts.py`)

```python
class FakeBus:
    def __init__(self):
        self.published = []

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


async def test_run_once_settles_publishes_and_is_idempotent(tmp_path):
    from grid.proposal_log import ProposalLog
    from grid.verdicts import run_once
    db = make_db(tmp_path / "history.sqlite3", [(1, "g1", 40, 0, 0, 3, 10, 120, 3020)])
    vdir = tmp_path / "verdicts"
    plog = ProposalLog(vdir)
    plog.record({"request_id": "r1", "epoch": 1, "tick": 100, "proposal_genome_id": "g1", "outcome": "proposed",
                 "model_alias": "a", "reason": None, "prompt_path": "prompts/r1.txt"}, "p")
    bus = FakeBus()
    rows = await run_once(verdicts_dir=vdir, history_db=db, state={"epoch": 1, "tick": 3300}, bus=bus,
                          colony="c", agent_id="grid-c")
    assert [r["tier"] for r in rows] == ["growing"]
    subj, payload = bus.published[0]
    assert subj == "GRID.c.verdict" and payload["request_id"] == "r1" and payload["fitness"] == 40
    assert payload["schema_version"] == "1.0" and payload["epoch"] == 1 and payload["tick"] == 3300
    assert await run_once(verdicts_dir=vdir, history_db=db, state={"epoch": 1, "tick": 3400}, bus=bus,
                          colony="c", agent_id="grid-c") == []
    assert len(bus.published) == 1


async def test_run_once_without_bus_still_writes(tmp_path):
    from grid.proposal_log import ProposalLog
    from grid.verdicts import Ledger, run_once
    db = make_db(tmp_path / "history.sqlite3", [])
    vdir = tmp_path / "verdicts"
    ProposalLog(vdir).record({"request_id": "r1", "epoch": 1, "tick": 1, "proposal_genome_id": "gx",
                              "outcome": "proposed", "model_alias": "a", "reason": None,
                              "prompt_path": "prompts/r1.txt"}, "p")
    rows = await run_once(verdicts_dir=vdir, history_db=db, state={"epoch": 2, "tick": 5}, bus=None,
                          colony="c", agent_id="grid-c")
    assert rows[0]["tier"] == "unborn" and Ledger(vdir / "verdicts.jsonl").verify()
```

- [ ] **Step 2: Run** → `ImportError: cannot import name 'run_once'`.

- [ ] **Step 3: Implement** (append to `grid/verdicts.py`)

```python
async def run_once(*, verdicts_dir: Path, history_db: Path, state: dict, bus=None, colony: str,
                   agent_id: str, settle_ticks: int = SETTLE_TICKS) -> list[dict]:
    from .envelope import envelope, subject
    from .proposal_log import ProposalLog

    entries = ProposalLog(Path(verdicts_dir)).read_all()
    ledger = Ledger(Path(verdicts_dir) / "verdicts.jsonl")
    now_epoch, now_tick = int(state.get("epoch", 0)), int(state.get("tick", 0))
    with StatsReader(Path(history_db)) as reader:
        rows = settle_pending(entries, ledger, reader, now_epoch=now_epoch, now_tick=now_tick,
                              settle_ticks=settle_ticks)
    if bus is not None:
        for row in rows:
            fields = {k: v for k, v in row.items() if k not in ("epoch",)}
            try:
                await bus.publish(subject(colony, "verdict"),
                                  envelope(agent_id=agent_id, colony=colony, epoch=row["epoch"],
                                           tick=now_tick, **fields))
            except Exception as e:      # the row is already in the ledger; the bus is best-effort
                log.error("verdict publish failed for %s: %s", row["request_id"], e)
    log.info("settled %d proposals (epoch %d tick %d)", len(rows), now_epoch, now_tick)
    return rows


async def _main_async(a: argparse.Namespace) -> int:
    import httpx
    from .natsio import Bus

    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{a.colony_url}/api/state")
        r.raise_for_status()
        state = r.json()
    bus = None
    if not a.no_publish:
        bus = Bus(os.environ.get("NATS_URL", "nats://swarm.ninja-portal.com:4222"),
                  user=os.environ.get("NATS_USER") or None, password=os.environ.get("NATS_PASSWORD") or None)
        try:
            await bus.connect()
        except Exception as e:
            log.error("nats unavailable, verdicts will be written but not published: %s", e)
            bus = None
    try:
        rows = await run_once(verdicts_dir=Path(a.verdicts_dir), history_db=Path(a.history_db), state=state,
                              bus=bus, colony=a.colony, agent_id=a.agent_id, settle_ticks=a.settle_ticks)
    finally:
        if bus is not None:
            await bus.close()
    print(json.dumps({"settled": len(rows), "by_tier": {t: sum(1 for r in rows if r["tier"] == t)
                                                        for t in set(r["tier"] for r in rows)}}))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    env = os.environ.get
    colony = env("COLONY_SLUG", "colony-one")
    ap = argparse.ArgumentParser(description="Settle logged Odin proposals against thegrid's fossil record.")
    ap.add_argument("--verdicts-dir", default=env("VERDICTS_DIR", f"/srv/grid/{colony}/verdicts"))
    ap.add_argument("--history-db", default=env("HISTORY_DB", f"/srv/grid/{colony}/state/history.sqlite3"))
    ap.add_argument("--colony-url", default=env("COLONY_URL", "http://127.0.0.1:8787").rstrip("/"))
    ap.add_argument("--colony", default=colony)
    ap.add_argument("--agent-id", default=env("MIND_AGENT_ID", f"grid-{colony}"))
    ap.add_argument("--settle-ticks", type=int, default=int(env("SETTLE_TICKS", str(SETTLE_TICKS))))
    ap.add_argument("--no-publish", action="store_true")
    a = ap.parse_args(argv)
    return asyncio.run(_main_async(a))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run** `python -m pytest tests/test_verdicts.py -q` → `8 passed`.

- [ ] **Step 5: Mutation, then commit**

In `run_once`, publish rows from `ledger.rows()` instead of `rows` → the idempotent test's `len(bus.published) == 1` fails. Revert.

```bash
git add grid/verdicts.py tests/test_verdicts.py
git commit -m "feat: verdicts run_once + CLI — settle, chain, publish GRID.<colony>.verdict"
```

---

### Task 7: Corpus export

**Files:**
- Create: `grid/verdict_corpus.py`
- Test: `tests/test_verdict_corpus.py`

**Interfaces:**
- Consumes: `Ledger`, `ProposalLog`, `TIERS` (Tasks 2, 5); chronicle log rows (Task 4).
- Produces: `TIER_RANK = {"established": 4, "enduring": 3, "growing": 2, "reproduced": 1, "new": 0}`; `record_for(entry: dict, verdict: dict, prompt: str, *, history_sha256: str) -> dict`; `chronicle_record(row: dict) -> dict`; `export(verdicts_dir: Path, out_dir: Path, *, min_tier: str = "reproduced", history_db: Path | None = None) -> dict` (writes `records.jsonl`, `negatives.jsonl`, `manifest.json`; returns the manifest); `main(argv)` with `--verdicts-dir`, `--out`, `--min-tier`, `--history-db`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_verdict_corpus.py
import json
from pathlib import Path

from grid.proposal_log import ProposalLog
from grid.verdict_corpus import chronicle_record, export, record_for
from grid.verdicts import Ledger

REQUIRED = {"id", "kind", "tier", "source", "speaker", "text"}       # export_corpus.py's contract


def seed(vdir: Path) -> None:
    plog = ProposalLog(vdir)
    for rid, gid in (("a", "g1"), ("b", "g2"), ("c", "g3")):
        plog.record({"request_id": rid, "epoch": 1, "tick": 10, "colony": "c", "proposal": ["harvest", "fork"],
                     "proposal_genome_id": gid, "outcome": "proposed", "model_alias": "m", "reason": None,
                     "prompt_path": f"prompts/{rid}.txt", "parent_genome": ["harvest"]}, f"prompt {rid}")
    plog.record({"request_id": "d", "epoch": 1, "tick": 11, "colony": "c", "proposal": None,
                 "proposal_genome_id": None, "outcome": "rejected", "model_alias": "m", "reason": "bad",
                 "prompt_path": "prompts/d.txt", "parent_genome": ["harvest"]}, "prompt d")
    led = Ledger(vdir / "verdicts.jsonl")
    led.append({"request_id": "a", "proposal_genome_id": "g1", "epoch": 1, "settled_tick": 3100, "births": 40,
                "generations": 12, "lifetime_ticks": 2000, "tier": "growing", "fitness": 40, "model_alias": "m", "outcome": "proposed"})
    led.append({"request_id": "b", "proposal_genome_id": "g2", "epoch": 1, "settled_tick": 3100, "births": 1,
                "generations": 0, "lifetime_ticks": 5, "tier": "new", "fitness": 1, "model_alias": "m", "outcome": "proposed"})
    led.append({"request_id": "c", "proposal_genome_id": "g3", "epoch": 1, "settled_tick": 3100, "births": 0,
                "generations": 0, "lifetime_ticks": 0, "tier": "unborn", "fitness": 0, "model_alias": "m", "outcome": "proposed"})
    led.append({"request_id": "d", "proposal_genome_id": None, "epoch": 1, "settled_tick": 3100, "births": 0,
                "generations": 0, "lifetime_ticks": 0, "tier": "rejected", "fitness": 0, "model_alias": "m", "outcome": "rejected"})
    (vdir / "chronicle.jsonl").write_text(json.dumps({"ts": 1, "colony": "c", "epoch": 1, "tick": 2000, "kind": "skill",
                                                       "question": "Name the capability", "text": "fork early via fork.",
                                                       "since_tick": 0}) + "\n")


def test_record_for_shape():
    e = {"request_id": "a", "epoch": 1, "tick": 10, "colony": "c", "proposal": ["harvest", "fork"], "model_alias": "m"}
    v = {"tier": "growing", "fitness": 40, "chain": "abc"}
    r = record_for(e, v, "the prompt", history_sha256="deadbeef")
    assert REQUIRED <= set(r)
    assert r["id"] == "odin-a" and r["kind"] == "odin" and r["tier"] == 1 and r["speaker"] == "kannaka"
    assert r["source"] == "grid-verdicts" and r["text"] == '["harvest", "fork"]' and r["context"] == "the prompt"
    assert r["verdict"] == {"tier": "growing", "fitness": 40}
    assert r["provenance"] == {"colony": "c", "epoch": 1, "tick": 10, "chain": "abc", "history_sha256": "deadbeef",
                               "model_alias": "m"}


def test_chronicle_record_shape():
    r = chronicle_record({"ts": 1, "colony": "c", "epoch": 1, "tick": 2000, "kind": "skill",
                          "question": "Q?", "text": "fork early via fork.", "since_tick": 0})
    assert REQUIRED <= set(r) and r["kind"] == "chronicle" and r["tier"] == 1
    assert r["id"] == "chronicle-c-1-2000-skill" and r["context"] == "Q?" and r["text"] == "fork early via fork."


def test_export_filters_by_tier_and_splits_negatives(tmp_path):
    vdir, out = tmp_path / "v", tmp_path / "out"
    seed(vdir)
    manifest = export(vdir, out, min_tier="reproduced")
    records = [json.loads(l) for l in (out / "records.jsonl").read_text().splitlines()]
    negatives = [json.loads(l) for l in (out / "negatives.jsonl").read_text().splitlines()]
    assert [r["id"] for r in records] == ["odin-a", "chronicle-c-1-2000-skill"]
    assert sorted(n["id"] for n in negatives) == ["odin-c", "odin-d"]          # unborn + rejected; 'new' is neither
    assert records[0]["context"] == "prompt a"
    assert manifest["records"] == 2 and manifest["negatives"] == 2 and manifest["min_tier"] == "reproduced"
    assert manifest["by_tier"] == {"growing": 1} and manifest["chain_head"] == Ledger(vdir / "verdicts.jsonl").head()
    assert json.loads((out / "manifest.json").read_text()) == manifest


def test_export_growing_threshold_and_missing_prompt(tmp_path):
    vdir, out = tmp_path / "v", tmp_path / "out"
    seed(vdir)
    (vdir / "prompts" / "a.txt").unlink()
    manifest = export(vdir, out, min_tier="growing")
    records = [json.loads(l) for l in (out / "records.jsonl").read_text().splitlines()]
    assert [r["id"] for r in records] == ["chronicle-c-1-2000-skill"]      # 'a' skipped: prompt missing
    assert manifest["skipped_missing_prompt"] == 1
```

- [ ] **Step 2: Run** → `ModuleNotFoundError: No module named 'grid.verdict_corpus'`.

- [ ] **Step 3: Implement**

```python
# grid/verdict_corpus.py
"""Turn settled verdicts into training records in kannaka-memory's corpus shape.

A proposal is Kannaka's own authored turn (first-party by construction); the
verdict is a FILTER on it, never a target. Rejected and unborn proposals go to
negatives.jsonl for a later preference-pair version.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from .proposal_log import ProposalLog
from .verdicts import Ledger

log = logging.getLogger("grid.verdict_corpus")

TIER_RANK = {"established": 4, "enduring": 3, "growing": 2, "reproduced": 1, "new": 0}
NEGATIVE_TIERS = ("unborn", "rejected")


def record_for(entry: dict, verdict: dict, prompt: str, *, history_sha256: str) -> dict:
    return {
        "id": f"odin-{entry['request_id']}", "kind": "odin", "tier": 1, "source": "grid-verdicts",
        "speaker": "kannaka", "text": json.dumps(entry.get("proposal")), "context": prompt,
        "verdict": {"tier": verdict.get("tier"), "fitness": verdict.get("fitness")},
        "provenance": {"colony": entry.get("colony"), "epoch": entry.get("epoch"), "tick": entry.get("tick"),
                       "chain": verdict.get("chain"), "history_sha256": history_sha256,
                       "model_alias": entry.get("model_alias")},
    }


def chronicle_record(row: dict) -> dict:
    return {
        "id": f"chronicle-{row.get('colony')}-{row.get('epoch')}-{row.get('tick')}-{row.get('kind')}",
        "kind": "chronicle", "tier": 1, "source": "grid-chronicle", "speaker": "kannaka",
        "text": row.get("text", ""), "context": row.get("question", ""),
        "provenance": {"colony": row.get("colony"), "epoch": row.get("epoch"), "tick": row.get("tick"),
                       "since_tick": row.get("since_tick")},
    }


def _sha256_file(path: Path | None) -> str:
    if path is None or not Path(path).exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(verdicts_dir: Path, out_dir: Path, *, min_tier: str = "reproduced",
           history_db: Path | None = None) -> dict:
    vdir, out = Path(verdicts_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    entries = {e["request_id"]: e for e in ProposalLog(vdir).read_all()}
    ledger = Ledger(vdir / "verdicts.jsonl")
    hist_sha = _sha256_file(history_db)
    threshold = TIER_RANK[min_tier]
    records, negatives, by_tier, skipped = [], [], {}, 0
    for v in ledger.rows():
        e = entries.get(v["request_id"])
        if e is None:
            continue
        if v["tier"] in NEGATIVE_TIERS:
            negatives.append(record_for(e, v, "", history_sha256=hist_sha))
            continue
        if TIER_RANK.get(v["tier"], -1) < threshold:
            continue
        prompt_path = vdir / e.get("prompt_path", "")
        if not prompt_path.is_file():
            skipped += 1
            continue
        records.append(record_for(e, v, prompt_path.read_text(encoding="utf-8"), history_sha256=hist_sha))
        by_tier[v["tier"]] = by_tier.get(v["tier"], 0) + 1
    chron = vdir / "chronicle.jsonl"
    if chron.exists():
        for line in chron.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(chronicle_record(json.loads(line)))
    for name, rows in (("records.jsonl", records), ("negatives.jsonl", negatives)):
        with open(out / name, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    manifest = {"records": len(records), "negatives": len(negatives), "min_tier": min_tier,
                "by_tier": by_tier, "skipped_missing_prompt": skipped, "chain_head": ledger.head(),
                "history_sha256": hist_sha, "verdicts_dir": str(vdir)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    env = os.environ.get
    colony = env("COLONY_SLUG", "colony-one")
    ap = argparse.ArgumentParser(description="Export settled verdicts as training records.")
    ap.add_argument("--verdicts-dir", default=env("VERDICTS_DIR", f"/srv/grid/{colony}/verdicts"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-tier", default="reproduced", choices=sorted(TIER_RANK))
    ap.add_argument("--history-db", default=env("HISTORY_DB", f"/srv/grid/{colony}/state/history.sqlite3"))
    a = ap.parse_args(argv)
    print(json.dumps(export(Path(a.verdicts_dir), Path(a.out), min_tier=a.min_tier, history_db=Path(a.history_db))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run** `python -m pytest tests/test_verdict_corpus.py -q` → `4 passed`.

- [ ] **Step 5: Mutation, then commit**

In `TIER_RANK` change `"new": 0` to `"new": 1` → `test_export_filters_by_tier_and_splits_negatives` fails because the `new` verdict (`odin-b`) would be exported. Revert.

```bash
git add grid/verdict_corpus.py tests/test_verdict_corpus.py
git commit -m "feat: verdict corpus export — positives, negatives, manifest with chain head"
```

---

### Task 8: Deploy — timer, env, deploy.sh, runbook §4

**Files:**
- Create: `deploy/systemd/grid-verdicts.service`, `deploy/systemd/grid-verdicts.timer`
- Modify: `deploy/skywave/deploy.sh`, `deploy/README.md`, `tests/test_deploy_units.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_deploy_units.py`)

```python
def test_verdicts_timer_runs_nightly_and_reads_the_shared_env():
    svc = unit("grid-verdicts.service")
    assert "Type=oneshot" in svc and "grid.verdicts" in svc and "EnvironmentFile=/srv/grid/grid.env" in svc
    assert "EnvironmentFile=/home/nick/.kannaka-nats.env" in svc
    timer = unit("grid-verdicts.timer")
    assert "OnCalendar=" in timer and "Persistent=true" in timer
```

- [ ] **Step 2: Run** → `FileNotFoundError`.

- [ ] **Step 3: Write the files**

```ini
# deploy/systemd/grid-verdicts.service
[Unit]
Description=kannaka-grid verdicts: settle logged Odin proposals against the fossil record
After=network-online.target thegrid-colony.service
Wants=network-online.target

[Service]
Type=oneshot
User=nick
Group=nick
WorkingDirectory=/srv/grid/kannaka-grid
EnvironmentFile=/srv/grid/grid.env
EnvironmentFile=/home/nick/.kannaka-nats.env
ExecStart=/srv/grid/venv/bin/python -m grid.verdicts
```

```ini
# deploy/systemd/grid-verdicts.timer
[Unit]
Description=Settle Odin verdicts nightly

[Timer]
OnCalendar=*-*-* 03:15:00
OnBootSec=20m
Persistent=true

[Install]
WantedBy=timers.target
```

`deploy/skywave/deploy.sh`: in §0 add `$ROOT/$COLONY/verdicts $ROOT/$COLONY/verdicts/prompts` to the `install -d` line and `chmod 700 $ROOT/$COLONY/verdicts`; in §3 also `sudo cp $CHECKOUT/deploy/systemd/grid-verdicts.service $CHECKOUT/deploy/systemd/grid-verdicts.timer /etc/systemd/system/` and `sudo systemctl enable --now grid-verdicts.timer`, and log `§3 receipt: ... verdicts timer enabled`. Ensure `grid.env` on the host carries `VERDICTS_DIR` and `MIND_MODEL_ALIAS`: after the `cp grid.env.example` block add

```bash
grep -q '^VERDICTS_DIR=' $ROOT/grid.env || printf 'VERDICTS_DIR=%s/%s/verdicts\nMIND_MODEL_ALIAS=agent-brain\n' "$ROOT" "$COLONY" >> $ROOT/grid.env
```

`deploy/README.md`: add "## 4. Verdicts (self-improvement sub-project 1)" after §3 with: what the timer does, `python -m grid.verdicts --no-publish` for a manual run, `python -m grid.verdict_corpus --out ~/.kannaka-corpus/grid/$(date -u +%G-W%V)`, and three receipts: (a) `verdicts.jsonl` has rows and `python -c "from grid.verdicts import Ledger; print(Ledger('/srv/grid/colony-one/verdicts/verdicts.jsonl').verify())"` prints True; (b) at least one row `reproduced` or better within a day; (c) the export manifest shows `records ≥ 1`; plus `GRID.colony-one.verdict` seen in a bus capture.

- [ ] **Step 4: Run** `python -m pytest tests/test_deploy_units.py -q` → `6 passed`; `bash -n deploy/skywave/deploy.sh`.

- [ ] **Step 5: Commit**

```bash
git add deploy tests/test_deploy_units.py
git commit -m "deploy: nightly verdicts timer, env, runbook section 4"
```

---

### Task 9: Integration smoke — verdicts against a real fossil DB (Linux, opt-in)

**Files:**
- Modify: `tests/integration/test_smoke.py`

- [ ] **Step 1: Add the test** (same `skip` marker and `colony` fixture as the existing tests)

```python
@skip
async def test_verdicts_settle_against_the_real_fossil_db(colony, tmp_path):
    from grid.proposal_log import ProposalLog
    from grid.verdicts import Ledger, run_once
    base, queue = colony
    state_dir = queue.parent
    vdir = tmp_path / "verdicts"
    # a proposal for a genome the colony will never birth -> settles 'unborn' once the epoch retires or 60 ticks pass
    ProposalLog(vdir).record({"request_id": "smoke-v", "epoch": 1, "tick": 1, "proposal_genome_id": "0" * 16,
                              "outcome": "proposed", "model_alias": "stub", "reason": None,
                              "prompt_path": "prompts/smoke-v.txt", "colony": "smoke"}, "p")
    deadline = time.time() + 60
    while time.time() < deadline:
        state = httpx.get(f"{base}/api/state", timeout=2).json()
        if state["tick"] >= 200:
            break
        time.sleep(1)
    db = state_dir / "history.sqlite3"
    assert db.exists(), "thegrid did not write history.sqlite3"
    rows = await run_once(verdicts_dir=vdir, history_db=db, state=state, bus=None, colony="smoke",
                          agent_id="grid-smoke", settle_ticks=100)
    assert [r["tier"] for r in rows] == ["unborn"]
    assert Ledger(vdir / "verdicts.jsonl").verify()
```

The filename is confirmed: `live.py:76` opens `LineageHistory(state_path.with_name("history.sqlite3"))`, and Skywave shows `history.sqlite3` with `-wal`/`-shm` siblings (WAL mode, hence the backup-API reader in Task 5).

- [ ] **Step 2: Run** `python -m pytest tests/integration -q` → `3 skipped` on Windows.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_smoke.py
git commit -m "test: integration smoke settles a verdict against thegrid's real fossil DB"
```

---

### Task 10: Full suite, then Skywave

- [ ] **Step 1:** `python -m pytest -q -W error` → all green, integration skipped.
- [ ] **Step 2:** The controller deploys: pull on Skywave (deploy key), rerun `deploy.sh` (idempotent; installs the timer and env keys), restart `grid-odin` and `grid-bridge`, run `python -m grid.verdicts --no-publish` once after `SETTLE_TICKS` have elapsed, run the export, capture `GRID.colony-one.verdict`, commit receipts under `deploy/receipts/verdicts-<ts>/`.
