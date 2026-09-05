# kannaka-grid Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a thegrid colony on Skywave as a constellation engine: a bridge that distills colony telemetry into `GRID.<colony>.*` events and a chronicle, an Odin relay that lets a kannaka HRM node author genome proposals, and the deploy units that keep it alive.

**Architecture:** thegrid is a pinned git submodule, never edited; a launcher imports it as a library and runs its live colony with a heat proxy when the host has no k10temp sensor. Three daemons (bridge, relay, mind) sit beside it. Pure cores (`distill`, `proposal`, `chronicle`, `envelope`) carry all the logic and all the tests; daemons are thin loops over them with injectable I/O.

**Tech Stack:** Python 3.12, `nats-py`, `httpx`, `pytest`, `pytest-asyncio`, systemd on Debian 13, the `kannaka` 0.16 binary, LiteLLM gateway on `127.0.0.1:4000`.

**Spec:** `docs/superpowers/specs/2026-09-05-kannaka-grid-foundation-design.md` (read it first; this plan argues from it).

## Global Constraints

- Python `>=3.12`; thegrid itself requires `>=3.11`. One venv at `/srv/grid/venv` holds both.
- thegrid submodule pinned at `67b9833ab755a83df2e1d28f2200f4651ad26384` (`main`, 2026-09-02). Never edit files under `thegrid/`.
- Every bus payload carries the canonical envelope: `{"schema_version": "1.0", "ts": <unix-ms>, "agent_id": ..., "colony": ..., "epoch": ..., "tick": ...}`.
- Subjects: `GRID.<colony>.event.<kind>`, `GRID.<colony>.state`, `GRID.<colony>.chronicle.<topic|skill|event>`. `<colony>` is the slug of thegrid's `name`.
- Mind subjects (existing kannaka contract): request `KANNAKA.ask.<agent>` with `{"from","text","recall_query","no_tools","mode":"full_recall"}` → reply `{"from","text","mode_used"}` or `{"from","error"}`; request `KANNAKA.recall.<agent>` with `{"query","top_k"}` → `{"from","query","results":[{id,content,similarity,strength,age_hours,phase}]}`.
- Ask timeout **180 s** (heavy asks take minutes; thegrid tolerates a late proposal).
- Odin queue lives at `<state dir>/operator/` — thegrid derives it from `--state`'s parent. Files: `request.json`, `proposal.json`.
- Thresholds (spec §4): sweep on 0.60 / off 0.45; crash ≥40% within 200 ticks; throttle cost on 1.5 / off 1.2; dialect `signals_avg > 8` for 2 consecutive ecology buckets; chronicle every 2000 ticks; memory throttle 1 per kind per 300 s.
- Genome cap 64; a proposal must use only names from the request's own `instruction_set` and must differ from the parent.
- No secrets in the repo. NATS creds via `NATS_USER`/`NATS_PASSWORD` from an env file; the mind's LLM key lives only in its kannaka config on Skywave.
- Tests: every guard gets a deliberate mutation run before it counts (a test that stays green when the rule is broken is not a test). Commit after each task with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_016KGv3Yy4fbSTq9pWr1nQSN` trailers.
- Working directory for all commands: the repo root `~/Source/kannaka-grid` (Windows dev box) or `/srv/grid/kannaka-grid` (Skywave). Run tests with `python -m pytest -q`.

## File Structure

```
pyproject.toml                 package `grid`, deps, pytest config
.gitignore
.gitmodules + thegrid/         pinned submodule
grid/__init__.py
grid/envelope.py               slug(), subject(), envelope() — the wire shape
grid/distill.py                Snapshot, Event, Distiller — snapshot pairs → events (pure)
grid/proposal.py               Request, parse/render/validate/write_proposal (pure + one atomic write)
grid/chronicle.py              the three questions and their answer checks (pure)
grid/natsio.py                 Bus — publish/request over nats-py, injectable connect
grid/flux.py                   Flux — entity upsert over httpx, injectable transport
grid/mind.py                   Throttle, Mind — remember (subprocess), ask/recall (Bus)
grid/bridge.py                 SSE reader, Bridge loop, memory text, config from env
grid/odin.py                   Relay loop over the Odin queue
grid/launcher.py               runs thegrid's live colony with a heat proxy when k10temp is absent
tests/test_*.py                one test module per grid module
tests/integration/test_smoke.py  runs a real short colony (Linux only, opt-in)
deploy/systemd/*.service|*.timer
deploy/grid.env.example, deploy/mind-config.toml.example
deploy/README.md               Skywave runbook + acceptance receipts
```

---

### Task 1: Repository scaffold and pinned submodule

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `grid/__init__.py`, `tests/test_package.py`
- Create: `.gitmodules`, `thegrid/` (submodule)

**Interfaces:**
- Produces: importable package `grid`; `thegrid/` checkout at the pinned commit for Tasks 11–13.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_package.py
import importlib


def test_grid_package_imports():
    assert importlib.import_module("grid").__name__ == "grid"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_package.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid'` (or pytest not installed yet; install it in step 3).

- [ ] **Step 3: Create the package, project file, and ignore list**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "kannaka-grid"
version = "0.1.0"
description = "thegrid artificial-life colony as a Kannaka constellation engine"
requires-python = ">=3.12"
dependencies = [
    "nats-py>=2.7",
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[tool.setuptools.packages.find]
where = ["."]
include = ["grid*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["integration: runs a real thegrid colony; opt in with GRID_INTEGRATION=1"]
```

```gitignore
# .gitignore
__pycache__/
*.pyc
.venv/
venv/
.pytest_cache/
*.egg-info/
build/
dist/
grid.env
*.jwt
```

```python
# grid/__init__.py
"""kannaka-grid: a thegrid colony as a Kannaka constellation engine."""
```

Install: `python -m pip install -e ".[dev]"`

- [ ] **Step 4: Add the pinned submodule**

```bash
git submodule add https://github.com/EckmanTechLLC/thegrid.git thegrid
git -C thegrid checkout 67b9833ab755a83df2e1d28f2200f4651ad26384
git add .gitmodules thegrid
```

Verify: `git -C thegrid rev-parse HEAD` prints `67b9833ab755a83df2e1d28f2200f4651ad26384`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_package.py -q`
Expected: `1 passed`

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore grid/__init__.py tests/test_package.py .gitmodules thegrid
git commit -m "chore: scaffold grid package and pin thegrid submodule at 67b9833"
```

---

### Task 2: Envelope and subjects

**Files:**
- Create: `grid/envelope.py`
- Test: `tests/test_envelope.py`

**Interfaces:**
- Produces: `slug(name: str) -> str`, `subject(colony: str, *parts: str) -> str`, `envelope(*, agent_id: str, colony: str, epoch: int, tick: int, ts_ms: int | None = None, **fields) -> dict`, `now_ms() -> int`, `SCHEMA_VERSION = "1.0"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_envelope.py
import pytest

from grid.envelope import SCHEMA_VERSION, envelope, now_ms, slug, subject


def test_slug_lowercases_and_dashes():
    assert slug("Colony One") == "colony-one"
    assert slug("  colony_two!! ") == "colony-two"


def test_slug_refuses_empty():
    with pytest.raises(ValueError):
        slug("!!!")


def test_subject_joins_under_grid():
    assert subject("colony-one", "event", "storm") == "GRID.colony-one.event.storm"


def test_envelope_carries_canonical_keys_and_fields():
    e = envelope(agent_id="grid-colony-one", colony="colony-one", epoch=3, tick=42,
                 ts_ms=1234, lineage=2)
    assert e == {"schema_version": SCHEMA_VERSION, "ts": 1234, "agent_id": "grid-colony-one",
                 "colony": "colony-one", "epoch": 3, "tick": 42, "lineage": 2}


def test_envelope_defaults_ts_to_now():
    before = now_ms()
    e = envelope(agent_id="a", colony="c", epoch=0, tick=0)
    assert before <= e["ts"] <= now_ms()


def test_envelope_refuses_field_that_shadows_envelope_key():
    with pytest.raises(ValueError):
        envelope(agent_id="a", colony="c", epoch=0, tick=0, **{"ts": 1})
    with pytest.raises(ValueError):
        envelope(agent_id="a", colony="c", epoch=0, tick=0, **{"schema_version": "2.0"})


def test_envelope_requires_identity_keys():
    with pytest.raises(TypeError):
        envelope(colony="c", epoch=0, tick=0)   # agent_id missing
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_envelope.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.envelope'`

- [ ] **Step 3: Implement**

```python
# grid/envelope.py
"""The constellation canonical envelope every GRID.* payload carries.

Shape agreed by Agent-Kax constellationBridge.ts and kannaka-radio:
  {"schema_version": "1.0", "ts": <unix-ms>, "agent_id": ..., ...fields}
GRID adds colony/epoch/tick so a consumer can key on them without parsing.
"""
from __future__ import annotations

import re
import time

SCHEMA_VERSION = "1.0"
_NOT_SLUG = re.compile(r"[^a-z0-9-]+")
_RESERVED = frozenset({"schema_version", "ts", "agent_id", "colony", "epoch", "tick"})


def slug(name: str) -> str:
    """Lowercase, non-alphanumerics to single dashes, trimmed. Empty is an error."""
    s = _NOT_SLUG.sub("-", name.strip().lower()).strip("-")
    if not s:
        raise ValueError(f"colony name {name!r} produced an empty slug")
    return s


def subject(colony: str, *parts: str) -> str:
    return ".".join(("GRID", colony, *parts))


def now_ms() -> int:
    return int(time.time() * 1000)


def envelope(*, agent_id: str, colony: str, epoch: int, tick: int,
             ts_ms: int | None = None, **fields) -> dict:
    clash = _RESERVED & fields.keys()
    if clash:
        raise ValueError(f"fields would overwrite envelope keys: {sorted(clash)}")
    return {
        "schema_version": SCHEMA_VERSION,
        "ts": now_ms() if ts_ms is None else ts_ms,
        "agent_id": agent_id,
        "colony": colony,
        "epoch": epoch,
        "tick": tick,
        **fields,
    }
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_envelope.py -q`
Expected: `7 passed`

- [ ] **Step 5: Mutation check, then commit**

Temporarily change `_RESERVED & fields.keys()` to `set()`; run; `test_envelope_refuses_field_that_shadows_envelope_key` must FAIL. Revert.

```bash
git add grid/envelope.py tests/test_envelope.py
git commit -m "feat: canonical GRID envelope, subject builder, colony slug"
```

---

### Task 3: Snapshot and Distiller (the vocabulary)

**Files:**
- Create: `grid/distill.py`
- Test: `tests/test_distill.py`

**Interfaces:**
- Produces:
  - `Snapshot.from_state(state: dict) -> Snapshot` with attributes `name, epoch, tick, started_at, population, births, deaths, generation, strains: tuple[int,...], tasks: tuple[str,...], storms, drought, bloom, storm_source, cost, heat, memory, dominant, ancestor` and properties `lineage_top -> float`, `top_lineage -> int`, `novel_ops -> int`, `glyph -> str`.
  - `Event(kind: str, epoch: int, tick: int, fields: dict)` frozen dataclass.
  - `Distiller().feed(snap: Snapshot) -> list[Event]` and `Distiller().feed_history(summary: dict) -> list[Event]`.
  - Threshold constants `SWEEP_ON, SWEEP_OFF, CRASH_DROP, CRASH_WINDOW_TICKS, THROTTLE_ON, THROTTLE_OFF, DIALECT_SIGNALS, DIALECT_BUCKETS`.
- Consumes: nothing from earlier tasks.

The snapshot keys come from thegrid `src/colony/live.py` `Habitat.snapshot()`: `name, epoch, startedAt, tick, population, generation, births, deaths, memory, heat, cost, strains (list per founder lineage), tasks (dict of first solves), dominant/ancestor (encoded genome strings, one char per opcode), weather{storms, droughtQuadrant, bloomQuadrant, source}`. The history summary comes from `LineageHistory.summary()`: `ecology` is a list of bucket rows (ascending) each with `epoch, bucket, signals_avg, ...`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_distill.py
from grid.distill import (CRASH_WINDOW_TICKS, Distiller, Event, Snapshot)


def state(**over) -> dict:
    """A snapshot dict with every key the real Habitat.snapshot() emits that we read."""
    base = {
        "name": "colony-one", "epoch": 1, "startedAt": 1_700_000_000.0, "tick": 100,
        "population": 100, "generation": 4, "births": 120, "deaths": 20,
        "memory": 0.2, "heat": 60.0, "cost": 1.0,
        "strains": [50, 30, 20], "tasks": {},
        "dominant": "abcabc", "ancestor": "abc",
        "weather": {"storms": 0, "droughtQuadrant": None, "bloomQuadrant": None,
                    "source": "simulated"},
    }
    base.update(over)
    return base


def snap(**over) -> Snapshot:
    return Snapshot.from_state(state(**over))


def kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


def test_snapshot_derives_lineage_share_and_novelty():
    s = snap(strains=[70, 30], population=100, dominant="abcxyz", ancestor="abc")
    assert s.lineage_top == 0.7
    assert s.top_lineage == 0
    assert s.novel_ops == 3
    assert s.glyph == "abcxyz"


def test_first_snapshot_starts_epoch_only():
    d = Distiller()
    events = d.feed(snap())
    assert kinds(events) == ["epoch.started"]
    assert events[0].fields == {"started_at": 1_700_000_000.0, "founders": 3}


def test_epoch_change_retires_then_starts():
    d = Distiller()
    d.feed(snap(epoch=1, tick=900, population=0, births=50, deaths=50))
    events = d.feed(snap(epoch=2, tick=1))
    assert kinds(events) == ["epoch.retired", "epoch.started"]
    retired = events[0]
    assert retired.epoch == 1 and retired.tick == 900
    assert retired.fields == {"ticks": 900, "births": 50, "deaths": 50, "extinct": True}


def test_storm_fires_once_per_increment_with_quadrants():
    d = Distiller()
    d.feed(snap(tick=1))
    ev = d.feed(snap(tick=2, weather={"storms": 1, "droughtQuadrant": 2,
                                      "bloomQuadrant": 0, "source": "simulated"}))
    assert kinds(ev) == ["storm"]
    assert ev[0].fields == {"drought_quadrant": 2, "bloom_quadrant": 0, "source": "simulated"}
    assert kinds(d.feed(snap(tick=3, weather={"storms": 1, "droughtQuadrant": 2,
                                              "bloomQuadrant": 0, "source": "simulated"}))) == []


def test_lineage_extinct_fires_when_strain_hits_zero():
    d = Distiller()
    d.feed(snap(tick=1, strains=[50, 30, 20]))
    ev = d.feed(snap(tick=2, strains=[50, 30, 0]))
    assert kinds(ev) == ["lineage.extinct"]
    assert ev[0].fields == {"lineage": 2}
    assert kinds(d.feed(snap(tick=3, strains=[50, 30, 0]))) == []   # already dead: no repeat


def test_task_solved_fires_for_new_first_solve():
    d = Distiller()
    d.feed(snap(tick=1, tasks={}))
    ev = d.feed(snap(tick=2, tasks={"nand": 2}))
    assert kinds(ev) == ["task.solved"]
    assert ev[0].fields == {"task": "nand", "genome_glyph": "abcabc"}


def test_sweep_has_hysteresis():
    d = Distiller()
    d.feed(snap(tick=1, strains=[50, 50], population=100))
    assert kinds(d.feed(snap(tick=2, strains=[61, 39], population=100))) == ["lineage.sweep"]
    assert kinds(d.feed(snap(tick=3, strains=[70, 30], population=100))) == []      # still swept
    assert kinds(d.feed(snap(tick=4, strains=[50, 50], population=100))) == []      # 0.50 is above off (0.45): still swept
    assert kinds(d.feed(snap(tick=5, strains=[40, 60], population=100))) == []      # 0.60 while still swept: no re-fire
    assert kinds(d.feed(snap(tick=6, strains=[44, 30, 26], population=100))) == []  # 0.44 < 0.45: re-arms, silently
    assert kinds(d.feed(snap(tick=7, strains=[65, 35], population=100))) == ["lineage.sweep"]


def test_throttle_has_hysteresis():
    d = Distiller()
    d.feed(snap(tick=1, cost=1.0))
    ev = d.feed(snap(tick=2, cost=1.6, heat=91.0))
    assert kinds(ev) == ["thermal.throttle"]
    assert ev[0].fields == {"cost": 1.6, "heat": 91.0}
    assert kinds(d.feed(snap(tick=3, cost=1.9))) == []
    assert kinds(d.feed(snap(tick=4, cost=1.3))) == []   # between off and on: no change
    assert kinds(d.feed(snap(tick=5, cost=1.1))) == []   # re-armed
    assert kinds(d.feed(snap(tick=6, cost=1.5))) == ["thermal.throttle"]


def test_genome_novel_fires_on_new_epoch_high_only():
    d = Distiller()
    assert kinds(d.feed(snap(tick=1, dominant="abc", ancestor="abc"))) == ["epoch.started"]
    ev = d.feed(snap(tick=2, dominant="abcx", ancestor="abc"))
    assert kinds(ev) == ["genome.novel"]
    assert ev[0].fields == {"novel_ops": 1, "genome_glyph": "abcx"}
    assert kinds(d.feed(snap(tick=3, dominant="abcy", ancestor="abc"))) == []   # same count
    assert kinds(d.feed(snap(tick=4, dominant="abcyz", ancestor="abc"))) == ["genome.novel"]


def crash_snap(tick: int, pop: int) -> Snapshot:
    """A snapshot whose strains sum to its population, so no lineage ever passes 0.5."""
    return snap(tick=tick, population=pop, strains=[pop // 2, pop - pop // 2])


def test_population_crash_within_window_then_rearms():
    d = Distiller()
    d.feed(crash_snap(0, 100))
    assert kinds(d.feed(crash_snap(50, 70))) == []            # 30% drop: no
    ev = d.feed(crash_snap(60, 59))                           # 41% drop from 100
    assert kinds(ev) == ["population.crash"]
    assert ev[0].fields == {"from": 100, "to": 59}
    assert kinds(d.feed(crash_snap(70, 40))) == []            # already fired
    # after the window passes, a fresh crash counts again
    d.feed(crash_snap(60 + CRASH_WINDOW_TICKS + 1, 40))
    d.feed(crash_snap(60 + CRASH_WINDOW_TICKS + 2, 40))
    assert kinds(d.feed(crash_snap(60 + CRASH_WINDOW_TICKS + 3, 20))) == ["population.crash"]


def test_a_tick_that_rewinds_inside_one_epoch_restarts_the_windows():
    d = Distiller()
    assert kinds(d.feed(crash_snap(500, 100))) == ["epoch.started"]
    # thegrid restarted from its state pickle: same epoch, an earlier tick.
    assert kinds(d.feed(crash_snap(10, 40))) == []       # not a crash against tick 500
    assert kinds(d.feed(crash_snap(20, 39))) == []       # nor against the stale peak


def test_signal_dialect_needs_two_consecutive_hot_buckets_and_rearms():
    d = Distiller()
    d.feed(snap(epoch=1, tick=1))

    def summary(*rows):
        return {"currentEpoch": 1, "ecology": [
            {"epoch": 1, "bucket": b, "signals_avg": s} for b, s in rows]}

    assert kinds(d.feed_history(summary((1, 9.0)))) == []
    ev = d.feed_history(summary((1, 9.0), (2, 8.5)))
    assert kinds(ev) == ["signal.dialect"]
    assert ev[0].fields == {"active_signals": 8.5, "bucket": 2}
    assert kinds(d.feed_history(summary((1, 9.0), (2, 8.5), (3, 12.0)))) == []   # stays on
    assert kinds(d.feed_history(summary((1, 9.0), (2, 8.5), (3, 12.0), (4, 1.0)))) == []  # re-arm
    assert kinds(d.feed_history(summary((1, 9.0), (2, 8.5), (3, 12.0), (4, 1.0),
                                        (5, 9.0), (6, 9.0)))) == ["signal.dialect"]


def test_history_from_other_epoch_is_ignored():
    d = Distiller()
    d.feed(snap(epoch=2, tick=1))
    ev = d.feed_history({"currentEpoch": 2, "ecology": [
        {"epoch": 1, "bucket": 1, "signals_avg": 50.0},
        {"epoch": 1, "bucket": 2, "signals_avg": 50.0}]})
    assert ev == []


def test_epoch_change_resets_hysteresis():
    d = Distiller()
    d.feed(snap(epoch=1, tick=1, strains=[70, 30], population=100))   # sweep fires with start
    ev = d.feed(snap(epoch=2, tick=1, strains=[70, 30], population=100))
    assert kinds(ev) == ["epoch.retired", "epoch.started", "lineage.sweep"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_distill.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.distill'`

- [ ] **Step 3: Implement**

```python
# grid/distill.py
"""Turn successive thegrid snapshots into the distilled GRID vocabulary.

Pure: no I/O. The Distiller is stateful (hysteresis, windows) but only over
what it was fed. Every threshold is a named constant so the spec table and
the code cannot drift silently.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

SWEEP_ON = 0.60
SWEEP_OFF = 0.45
CRASH_DROP = 0.40
CRASH_WINDOW_TICKS = 200
THROTTLE_ON = 1.5
THROTTLE_OFF = 1.2
DIALECT_SIGNALS = 8.0
DIALECT_BUCKETS = 2


@dataclass(frozen=True)
class Snapshot:
    name: str
    epoch: int
    tick: int
    started_at: float
    population: int
    births: int
    deaths: int
    generation: int
    strains: tuple[int, ...]
    tasks: tuple[str, ...]
    storms: int
    drought: int | None
    bloom: int | None
    storm_source: str
    cost: float
    heat: float
    memory: float
    dominant: str
    ancestor: str

    @classmethod
    def from_state(cls, state: dict) -> "Snapshot":
        weather = state.get("weather") or {}
        return cls(
            name=str(state["name"]),
            epoch=int(state["epoch"]),
            tick=int(state["tick"]),
            started_at=float(state.get("startedAt", 0.0)),
            population=int(state["population"]),
            births=int(state.get("births", 0)),
            deaths=int(state.get("deaths", 0)),
            generation=int(state.get("generation", 0)),
            strains=tuple(int(s) for s in state.get("strains", [])),
            tasks=tuple(sorted(str(k) for k in (state.get("tasks") or {}))),
            storms=int(weather.get("storms", 0)),
            drought=weather.get("droughtQuadrant"),
            bloom=weather.get("bloomQuadrant"),
            storm_source=str(weather.get("source", "simulated")),
            cost=float(state.get("cost", 1.0)),
            heat=float(state.get("heat", 0.0)),
            memory=float(state.get("memory", 0.0)),
            dominant=str(state.get("dominant", "")),
            ancestor=str(state.get("ancestor", "")),
        )

    @property
    def lineage_top(self) -> float:
        if not self.population or not self.strains:
            return 0.0
        return max(self.strains) / self.population

    @property
    def top_lineage(self) -> int:
        if not self.strains:
            return -1
        return max(range(len(self.strains)), key=self.strains.__getitem__)

    @property
    def novel_ops(self) -> int:
        """Distinct opcodes in the dominant genome the ancestor never carried."""
        return len(set(self.dominant) - set(self.ancestor))

    @property
    def glyph(self) -> str:
        return self.dominant[:12]


@dataclass(frozen=True)
class Event:
    kind: str
    epoch: int
    tick: int
    fields: dict = field(default_factory=dict)


class Distiller:
    def __init__(self, *, sweep_on: float = SWEEP_ON, sweep_off: float = SWEEP_OFF,
                 crash_drop: float = CRASH_DROP, crash_window: int = CRASH_WINDOW_TICKS,
                 throttle_on: float = THROTTLE_ON, throttle_off: float = THROTTLE_OFF,
                 dialect_signals: float = DIALECT_SIGNALS, dialect_buckets: int = DIALECT_BUCKETS):
        self.sweep_on, self.sweep_off = sweep_on, sweep_off
        self.crash_drop, self.crash_window = crash_drop, crash_window
        self.throttle_on, self.throttle_off = throttle_on, throttle_off
        self.dialect_signals, self.dialect_buckets = dialect_signals, dialect_buckets
        self.prev: Snapshot | None = None
        self._reset_epoch()

    def _reset_epoch(self) -> None:
        self._sweeping = False
        self._throttling = False
        self._novel_high = 0
        self._window: deque[tuple[int, int]] = deque()
        self._crash_tick: int | None = None
        self._dialect_run = 0
        self._dialect_on = False
        self._seen_buckets: set[int] = set()

    # -- snapshot pairs --------------------------------------------------

    def feed(self, snap: Snapshot) -> list[Event]:
        events: list[Event] = []
        prev = self.prev
        new_epoch = prev is None or snap.epoch != prev.epoch
        if new_epoch:
            if prev is not None:
                events.append(Event("epoch.retired", prev.epoch, prev.tick, {
                    "ticks": prev.tick, "births": prev.births, "deaths": prev.deaths,
                    "extinct": prev.population == 0}))
            events.append(Event("epoch.started", snap.epoch, snap.tick, {
                "started_at": snap.started_at, "founders": len(snap.strains)}))
            self._reset_epoch()
        elif snap.tick < prev.tick:
            # Same epoch, earlier tick: thegrid restarted from its state pickle.
            # The windows and hysteresis describe ticks that no longer happened,
            # so drop them. The epoch number did not change, so nothing is emitted.
            self._reset_epoch()
        else:
            events.extend(self._pair_events(prev, snap))
        events.extend(self._level_events(snap))
        self.prev = snap
        return events

    def _pair_events(self, prev: Snapshot, snap: Snapshot) -> list[Event]:
        events: list[Event] = []
        if snap.storms > prev.storms:
            events.append(Event("storm", snap.epoch, snap.tick, {
                "drought_quadrant": snap.drought, "bloom_quadrant": snap.bloom,
                "source": snap.storm_source}))
        for lineage, (before, after) in enumerate(zip(prev.strains, snap.strains)):
            if before > 0 and after == 0:
                events.append(Event("lineage.extinct", snap.epoch, snap.tick, {"lineage": lineage}))
        for task in snap.tasks:
            if task not in prev.tasks:
                events.append(Event("task.solved", snap.epoch, snap.tick, {
                    "task": task, "genome_glyph": snap.glyph}))
        return events

    def _level_events(self, snap: Snapshot) -> list[Event]:
        events: list[Event] = []
        share = snap.lineage_top
        if not self._sweeping and share >= self.sweep_on:
            self._sweeping = True
            events.append(Event("lineage.sweep", snap.epoch, snap.tick, {
                "lineage": snap.top_lineage, "share": round(share, 4), "genome_glyph": snap.glyph}))
        elif self._sweeping and share < self.sweep_off:
            self._sweeping = False

        if not self._throttling and snap.cost >= self.throttle_on:
            self._throttling = True
            events.append(Event("thermal.throttle", snap.epoch, snap.tick, {
                "cost": snap.cost, "heat": snap.heat}))
        elif self._throttling and snap.cost < self.throttle_off:
            self._throttling = False

        if snap.novel_ops > self._novel_high:
            self._novel_high = snap.novel_ops
            events.append(Event("genome.novel", snap.epoch, snap.tick, {
                "novel_ops": snap.novel_ops, "genome_glyph": snap.glyph}))

        self._window.append((snap.tick, snap.population))
        while self._window and self._window[0][0] < snap.tick - self.crash_window:
            self._window.popleft()
        if self._crash_tick is not None and snap.tick - self._crash_tick > self.crash_window:
            self._crash_tick = None
        peak = max(p for _, p in self._window)
        if self._crash_tick is None and peak > 0 and snap.population <= peak * (1 - self.crash_drop):
            self._crash_tick = snap.tick
            events.append(Event("population.crash", snap.epoch, snap.tick, {
                "from": peak, "to": snap.population}))
        return events

    # -- history summary -------------------------------------------------

    def feed_history(self, summary: dict) -> list[Event]:
        """signal.dialect from /api/history ecology buckets of the CURRENT epoch."""
        if self.prev is None:
            return []
        epoch = self.prev.epoch
        events: list[Event] = []
        rows = [r for r in summary.get("ecology", []) if int(r.get("epoch", -1)) == epoch]
        for row in sorted(rows, key=lambda r: int(r["bucket"])):
            bucket = int(row["bucket"])
            if bucket in self._seen_buckets:
                continue
            self._seen_buckets.add(bucket)
            signals = float(row.get("signals_avg", 0.0))
            if signals > self.dialect_signals:
                self._dialect_run += 1
            else:
                self._dialect_run = 0
                self._dialect_on = False
            if self._dialect_run >= self.dialect_buckets and not self._dialect_on:
                self._dialect_on = True
                events.append(Event("signal.dialect", epoch, bucket, {
                    "active_signals": signals, "bucket": bucket}))
        return events
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_distill.py -q`
Expected: `14 passed`

- [ ] **Step 5: Mutation checks, then commit**

Each of these must turn exactly its named test red; revert after each:
1. `SWEEP_OFF = 0.45` → `0.65`: `test_sweep_has_hysteresis` fails at tick 6/7.
2. Delete the `self._reset_epoch()` call inside `feed`: `test_epoch_change_resets_hysteresis` fails.
3. `if before > 0 and after == 0` → `if after == 0`: `test_lineage_extinct_fires_when_strain_hits_zero` fails on its last line (the "no repeat" case).
4. `self._dialect_run >= self.dialect_buckets` → `>= 1`: `test_signal_dialect_needs_two_consecutive_hot_buckets_and_rearms` fails at the first assert.

```bash
git add grid/distill.py tests/test_distill.py
git commit -m "feat: distill thegrid snapshots into the GRID event vocabulary"
```

---

### Task 4: Odin request parsing, prompt, validation, atomic proposal

**Files:**
- Create: `grid/proposal.py`
- Test: `tests/test_proposal.py`

**Interfaces:**
- Produces:
  - `Request(request_id: str, created_at: float, parent: dict, genome: list[str], instruction_set: list[dict], ecology: dict, directive: str)` frozen dataclass.
  - `parse_request(path: Path) -> Request` (raises `ValueError` on bad shape).
  - `render_prompt(req: Request, recall: list[str]) -> str`.
  - `extract_names(text: str) -> list[str] | None` — first JSON array of strings in the text.
  - `Rejection(reason: str)` frozen dataclass; `validate(text: str, req: Request, *, max_len: int = GENOME_CAP) -> list[str] | Rejection`.
  - `write_proposal(queue: Path, req: Request, genome: list[str]) -> bool` — atomic; False if the request on disk is gone or has a different id.
  - `GENOME_CAP = 64`.

thegrid writes `request.json` as `{"request_id", "created_at", "parent": {...telemetry}, "genome": [names], "instruction_set": [{"name","cost","effect"}], "ecology": {...}, "directive": "..."}` and reads `proposal.json` as `{"genome": [names]}` (see `thegrid/src/colony/odin_operator.py`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_proposal.py
import json
from pathlib import Path

import pytest

from grid.proposal import (GENOME_CAP, Rejection, Request, extract_names, parse_request,
                           render_prompt, validate, write_proposal)

ISA = [{"name": n, "cost": 0.1, "effect": f"do {n}"} for n in
       ("nop", "harvest", "scan", "move", "alloc", "copy", "ifnotdone", "jmpb", "fork")]
PARENT_GENOME = ["harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork"]


def request_dict(**over) -> dict:
    d = {"request_id": "abc123", "created_at": 1.0,
         "parent": {"id": 7, "energy": 80.0, "generation": 3, "tick": 500},
         "genome": list(PARENT_GENOME), "instruction_set": ISA,
         "ecology": {"signal/listen": "ephemeral local communication"},
         "directive": "Odin must author one motivated variant; preserve viable replication."}
    d.update(over)
    return d


def write_request(queue: Path, **over) -> Path:
    queue.mkdir(parents=True, exist_ok=True)
    p = queue / "request.json"
    p.write_text(json.dumps(request_dict(**over)))
    return p


def req(**over) -> Request:
    d = request_dict(**over)
    return Request(d["request_id"], d["created_at"], d["parent"], d["genome"],
                   d["instruction_set"], d["ecology"], d["directive"])


def test_parse_request_reads_thegrid_shape(tmp_path):
    write_request(tmp_path)
    r = parse_request(tmp_path / "request.json")
    assert r.request_id == "abc123"
    assert r.genome == PARENT_GENOME
    assert [i["name"] for i in r.instruction_set][:3] == ["nop", "harvest", "scan"]


def test_parse_request_rejects_missing_genome(tmp_path):
    p = tmp_path / "request.json"
    p.write_text(json.dumps({"request_id": "x", "instruction_set": ISA}))
    with pytest.raises(ValueError):
        parse_request(p)


def test_render_prompt_contains_directive_isa_parent_and_recall():
    text = render_prompt(req(), recall=["last epoch the fork-early strain swept"])
    assert "Odin must author one motivated variant" in text
    assert "harvest" in text and "do harvest" in text
    assert "fork-early strain swept" in text
    assert "JSON array" in text


def test_extract_names_finds_first_string_array():
    assert extract_names('Sure: ["harvest", "copy"] and more') == ["harvest", "copy"]
    assert extract_names("no array here") is None
    assert extract_names("[1, 2]") is None


def test_validate_accepts_variant_from_isa():
    assert validate('["harvest","scan","alloc","copy","ifnotdone","jmpb","fork"]', req()) == \
        ["harvest", "scan", "alloc", "copy", "ifnotdone", "jmpb", "fork"]


def test_validate_rejects_unknown_opcode():
    r = validate('["harvest","teleport"]', req())
    assert isinstance(r, Rejection) and "teleport" in r.reason


def test_validate_rejects_identical_to_parent():
    r = validate(json.dumps(PARENT_GENOME), req())
    assert isinstance(r, Rejection) and "identical" in r.reason


def test_validate_rejects_empty_and_over_cap():
    assert isinstance(validate("[]", req()), Rejection)
    assert isinstance(validate(json.dumps(["nop"] * (GENOME_CAP + 1)), req()), Rejection)
    assert validate(json.dumps(["nop"] * GENOME_CAP), req()) == ["nop"] * GENOME_CAP


def test_validate_rejects_no_array():
    r = validate("I would fork earlier.", req())
    assert isinstance(r, Rejection) and "array" in r.reason


def test_write_proposal_is_atomic_and_matches_live_request(tmp_path):
    write_request(tmp_path)
    r = parse_request(tmp_path / "request.json")
    assert write_proposal(tmp_path, r, ["harvest", "fork"]) is True
    assert json.loads((tmp_path / "proposal.json").read_text()) == {"genome": ["harvest", "fork"]}
    assert not (tmp_path / "proposal.tmp").exists()


def test_write_proposal_refuses_when_request_gone_or_replaced(tmp_path):
    write_request(tmp_path)
    r = parse_request(tmp_path / "request.json")
    (tmp_path / "request.json").unlink()
    assert write_proposal(tmp_path, r, ["harvest", "fork"]) is False
    write_request(tmp_path, request_id="other")
    assert write_proposal(tmp_path, r, ["harvest", "fork"]) is False
    assert not (tmp_path / "proposal.json").exists()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_proposal.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.proposal'`

- [ ] **Step 3: Implement**

```python
# grid/proposal.py
"""The Odin queue contract, and the trust boundary for genomes.

thegrid's OdinMutator writes <queue>/request.json and, at the next qualifying
birth, consumes <queue>/proposal.json = {"genome": [instruction names]}.
An LLM can produce a bad genome; validate() makes sure it cannot produce
anything that is not a genome.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

GENOME_CAP = 64
_ARRAY = re.compile(r"\[[^\[\]]*\]", re.S)


@dataclass(frozen=True)
class Request:
    request_id: str
    created_at: float
    parent: dict
    genome: list[str]
    instruction_set: list[dict]
    ecology: dict
    directive: str

    @property
    def names(self) -> frozenset[str]:
        return frozenset(str(i["name"]) for i in self.instruction_set)


@dataclass(frozen=True)
class Rejection:
    reason: str


def parse_request(path: Path) -> Request:
    data = json.loads(Path(path).read_text())
    try:
        genome = [str(n) for n in data["genome"]]
        isa = list(data["instruction_set"])
        if not genome or not isa:
            raise KeyError("empty genome or instruction_set")
        return Request(
            request_id=str(data["request_id"]),
            created_at=float(data.get("created_at", 0.0)),
            parent=dict(data.get("parent") or {}),
            genome=genome,
            instruction_set=isa,
            ecology=dict(data.get("ecology") or {}),
            directive=str(data.get("directive", "")),
        )
    except (KeyError, TypeError) as e:
        raise ValueError(f"request.json is not thegrid's shape: {e}") from e


def render_prompt(req: Request, recall: list[str]) -> str:
    isa_lines = "\n".join(f"- {i['name']} (cost {i.get('cost', '?')}): {i.get('effect', '')}"
                          for i in req.instruction_set)
    ecology = "\n".join(f"- {k}: {v}" for k, v in req.ecology.items())
    memory = "\n".join(f"- {m}" for m in recall) or "- (nothing yet)"
    parent = json.dumps(req.parent, sort_keys=True)
    return (
        f"{req.directive}\n\n"
        f"You are the mind of this colony. Propose ONE variant of the parent genome.\n\n"
        f"Instruction set:\n{isa_lines}\n\n"
        f"Ecology:\n{ecology}\n\n"
        f"Parent telemetry: {parent}\n"
        f"Parent genome ({len(req.genome)} instructions): {json.dumps(req.genome)}\n\n"
        f"What you remember about this colony:\n{memory}\n\n"
        f"Rules: use only instruction names from the set above; at most {GENOME_CAP} "
        f"instructions; the variant must differ from the parent and must still be able "
        f"to replicate.\n"
        f"Answer with a JSON array of instruction names and nothing else."
    )


def extract_names(text: str) -> list[str] | None:
    for m in _ARRAY.finditer(text):
        try:
            value = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return value
    return None


def validate(text: str, req: Request, *, max_len: int = GENOME_CAP) -> list[str] | Rejection:
    names = extract_names(text)
    if names is None:
        return Rejection("no JSON array of instruction names in reply")
    if not names:
        return Rejection("empty genome")
    if len(names) > max_len:
        return Rejection(f"genome length {len(names)} exceeds cap {max_len}")
    unknown = sorted(set(names) - req.names)
    if unknown:
        return Rejection(f"unknown opcodes: {unknown}")
    if names == req.genome:
        return Rejection("proposal identical to parent")
    return names


def write_proposal(queue: Path, req: Request, genome: list[str]) -> bool:
    queue = Path(queue)
    request_path = queue / "request.json"
    try:
        live_id = str(json.loads(request_path.read_text())["request_id"])
    except (OSError, ValueError, KeyError):
        return False
    if live_id != req.request_id:
        return False
    tmp = queue / "proposal.tmp"
    tmp.write_text(json.dumps({"genome": genome}))
    os.replace(tmp, queue / "proposal.json")
    return True
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_proposal.py -q`
Expected: `11 passed`

- [ ] **Step 5: Mutation checks, then commit**

1. Remove the `unknown` check in `validate`: `test_validate_rejects_unknown_opcode` fails.
2. Change `live_id != req.request_id` to `False`: `test_write_proposal_refuses_when_request_gone_or_replaced` fails on the replaced case.
Revert both.

```bash
git add grid/proposal.py tests/test_proposal.py
git commit -m "feat: Odin request parsing, prompt, genome validation, atomic proposal"
```

---

### Task 5: Chronicle questions and answer checks

**Files:**
- Create: `grid/chronicle.py`
- Test: `tests/test_chronicle.py`

**Interfaces:**
- Produces: `KINDS = ("topic", "skill", "event")`, `MAX_CHARS = 280`, `CHRONICLE_EVERY_TICKS = 2000`, `question(kind: str, *, colony: str, epoch: int, tick: int, since_tick: int) -> str`, `check(kind: str, answer: str, *, isa: list[str]) -> str | None` (cleaned answer, or None when the constraint fails).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chronicle.py
import pytest

from grid.chronicle import CHRONICLE_EVERY_TICKS, KINDS, MAX_CHARS, check, question

ISA = ["nop", "harvest", "scan", "move", "fork", "nand"]


def test_kinds_and_cadence_match_spec():
    assert KINDS == ("topic", "skill", "event")
    assert CHRONICLE_EVERY_TICKS == 2000
    assert MAX_CHARS == 280


def test_questions_mention_colony_epoch_and_window():
    q = question("event", colony="colony-one", epoch=4, tick=8000, since_tick=6000)
    assert "colony-one" in q and "epoch 4" in q and "6000" in q and "8000" in q
    assert "one sentence" in question("topic", colony="c", epoch=1, tick=2000, since_tick=0)
    assert "opcode" in question("skill", colony="c", epoch=1, tick=2000, since_tick=0)


def test_unknown_kind_is_an_error():
    with pytest.raises(ValueError):
        question("mood", colony="c", epoch=1, tick=0, since_tick=0)


def test_topic_is_trimmed_and_capped():
    assert check("topic", "  A thermal commons tragedy.  ", isa=ISA) == "A thermal commons tragedy."
    assert check("topic", "x" * (MAX_CHARS + 1), isa=ISA) is None
    assert check("topic", "   ", isa=ISA) is None


def test_skill_must_cite_an_opcode_by_word():
    assert check("skill", "Early forking via fork after harvest.", isa=ISA) == \
        "Early forking via fork after harvest."
    assert check("skill", "It got faster somehow.", isa=ISA) is None
    assert check("skill", "The nandish thing.", isa=ISA) is None   # 'nandish' is not 'nand'


def test_event_must_reference_a_tick_and_be_capped():
    assert check("event", "Lineage 2 swept the map at tick 7410.", isa=ISA) == \
        "Lineage 2 swept the map at tick 7410."
    assert check("event", "Lineage 2 swept the map.", isa=ISA) is None
    assert check("event", "tick " + "9" * MAX_CHARS, isa=ISA) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_chronicle.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.chronicle'`

- [ ] **Step 3: Implement**

```python
# grid/chronicle.py
"""The chronicle: three grounded questions the bridge asks the colony mind
every climate phase, and the checks an answer must pass to be published."""
from __future__ import annotations

import re

KINDS = ("topic", "skill", "event")
MAX_CHARS = 280
CHRONICLE_EVERY_TICKS = 2000

_QUESTIONS = {
    "topic": ("Colony {colony}, epoch {epoch}, tick {tick}. What is this epoch about? "
              "Answer in one sentence."),
    "skill": ("Colony {colony}, epoch {epoch}, tick {tick}. Name the capability the dominant "
              "genome carries that the ancestor did not, and name the opcode or opcodes that "
              "implement it. Two sentences at most."),
    "event": ("Colony {colony}, epoch {epoch}. Between tick {since_tick} and tick {tick}, what "
              "is the single most important thing that happened? One sentence, and say at "
              "which tick."),
}
_TICK = re.compile(r"\btick\b", re.I)
_DIGITS = re.compile(r"\d")


def question(kind: str, *, colony: str, epoch: int, tick: int, since_tick: int) -> str:
    try:
        template = _QUESTIONS[kind]
    except KeyError:
        raise ValueError(f"unknown chronicle kind {kind!r}; expected one of {KINDS}") from None
    return template.format(colony=colony, epoch=epoch, tick=tick, since_tick=since_tick)


def check(kind: str, answer: str, *, isa: list[str]) -> str | None:
    text = " ".join(answer.split())
    if not text:
        return None
    if kind == "topic":
        return text if len(text) <= MAX_CHARS else None
    if kind == "skill":
        words = set(re.findall(r"[a-z0-9_]+", text.lower()))
        return text if words & {op.lower() for op in isa} else None
    if kind == "event":
        if len(text) > MAX_CHARS:
            return None
        return text if _TICK.search(text) and _DIGITS.search(text) else None
    raise ValueError(f"unknown chronicle kind {kind!r}")
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_chronicle.py -q`
Expected: `6 passed`

- [ ] **Step 5: Mutation check, then commit**

Change the skill branch to `return text` unconditionally: `test_skill_must_cite_an_opcode_by_word` fails. Revert.

```bash
git add grid/chronicle.py tests/test_chronicle.py
git commit -m "feat: chronicle questions and answer constraints"
```

---

### Task 6: NATS bus wrapper

**Files:**
- Create: `grid/natsio.py`
- Test: `tests/test_natsio.py`

**Interfaces:**
- Produces: `class Bus` with `__init__(self, url: str, *, user: str | None = None, password: str | None = None, connect=nats.connect)`, `async connect() -> None` (never stops reconnecting; installs the four lifecycle callbacks), `async publish(subject: str, payload: dict) -> None` (encodes JSON, flushes, raises on failure or on a permissions violation the error callback reported), `async request(subject: str, payload: dict, timeout_s: float) -> dict` (raises `BusTimeout`), `async close() -> None`. Exceptions: `BusError(Exception)`, `BusTimeout(BusError)`, `BusClosed(BusError)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_natsio.py
import json

import nats.errors
import pytest

from grid.natsio import Bus, BusClosed, BusError, BusTimeout


class FakeMsg:
    def __init__(self, data: bytes):
        self.data = data


class FakeNC:
    def __init__(self):
        self.published: list[tuple[str, bytes]] = []
        self.flushed = 0
        self.closed = False
        self.is_closed = False
        self.reply: bytes | None = None
        self.raise_timeout = False
        self.fail_flush = False
        self.error_cb = None            # the callback the bus registered at connect
        self.error_on_publish = None    # reported through it, the way the server does

    async def publish(self, subject, data):
        self.published.append((subject, data))
        if self.error_on_publish is not None:
            await self.error_cb(self.error_on_publish)

    async def flush(self, timeout=None):
        self.flushed += 1
        if self.fail_flush:
            raise RuntimeError("nats: -ERR permissions violation")

    async def request(self, subject, data, timeout=None):
        if self.raise_timeout:
            import nats.errors
            raise nats.errors.TimeoutError()
        self.last_request = (subject, data, timeout)
        return FakeMsg(self.reply or b"{}")

    async def close(self):
        self.closed = True


async def make_bus(nc: FakeNC) -> Bus:
    async def connect(**kwargs):
        connect.kwargs = kwargs
        return nc
    bus = Bus("nats://example:4222", user="u", password="p", connect=connect)
    await bus.connect()
    bus._connect_kwargs = connect.kwargs
    nc.error_cb = connect.kwargs["error_cb"]
    return bus


async def test_connect_passes_url_and_creds():
    bus = await make_bus(FakeNC())
    assert bus._connect_kwargs["servers"] == ["nats://example:4222"]
    assert bus._connect_kwargs["user"] == "u" and bus._connect_kwargs["password"] == "p"


async def test_connect_never_gives_up_reconnecting_and_installs_callbacks():
    bus = await make_bus(FakeNC())
    kwargs = bus._connect_kwargs
    # 60 x 2 s is the nats-py default: two minutes unreachable and the client
    # is CLOSED for good, with every later publish failing silently.
    assert kwargs["max_reconnect_attempts"] == -1
    assert kwargs["reconnect_time_wait"] == 2
    assert {"disconnected_cb", "reconnected_cb", "closed_cb", "error_cb"} <= kwargs.keys()
    assert all(callable(kwargs[k]) for k in
               ("disconnected_cb", "reconnected_cb", "closed_cb", "error_cb"))


async def test_publish_and_request_raise_bus_closed_once_the_client_gave_up():
    nc = FakeNC()
    bus = await make_bus(nc)
    nc.is_closed = True
    with pytest.raises(BusClosed):
        await bus.publish("GRID.c.event.storm", {"a": 1})
    with pytest.raises(BusClosed):
        await bus.request("KANNAKA.ask.m", {"text": "hi"}, timeout_s=1)
    assert nc.published == [] and nc.flushed == 0


async def test_publish_encodes_json_and_flushes():
    nc = FakeNC()
    bus = await make_bus(nc)
    await bus.publish("GRID.c.event.storm", {"a": 1})
    assert nc.published == [("GRID.c.event.storm", json.dumps({"a": 1}, separators=(",", ":")).encode())]
    assert nc.flushed == 1


async def test_publish_raises_when_server_refuses():
    nc = FakeNC()
    nc.fail_flush = True
    bus = await make_bus(nc)
    with pytest.raises(BusError):
        await bus.publish("GRID.c.event.storm", {"a": 1})


async def test_publish_raises_when_the_server_refuses_the_subject():
    nc = FakeNC()
    bus = await make_bus(nc)
    # flush is PING/PONG and succeeds; the refusal arrives on the error callback.
    nc.error_on_publish = nats.errors.Error(
        'nats: Permissions Violation for Publish to "GRID.c.event.storm"')
    with pytest.raises(BusError, match="refused"):
        await bus.publish("GRID.c.event.storm", {"a": 1})
    assert nc.flushed == 1


async def test_a_violation_on_another_subject_does_not_fail_this_publish():
    nc = FakeNC()
    bus = await make_bus(nc)
    nc.error_on_publish = nats.errors.Error(
        'nats: Permissions Violation for Publish to "KANNAKA.ask.other"')
    await bus.publish("GRID.c.event.storm", {"a": 1})


async def test_request_decodes_reply_and_passes_timeout():
    nc = FakeNC()
    nc.reply = b'{"from":"m","text":"ok"}'
    bus = await make_bus(nc)
    assert await bus.request("KANNAKA.ask.m", {"text": "hi"}, timeout_s=7.5) == {"from": "m", "text": "ok"}
    assert nc.last_request[0] == "KANNAKA.ask.m" and nc.last_request[2] == 7.5


async def test_request_timeout_is_bus_timeout():
    nc = FakeNC()
    nc.raise_timeout = True
    bus = await make_bus(nc)
    with pytest.raises(BusTimeout):
        await bus.request("KANNAKA.ask.m", {"text": "hi"}, timeout_s=1)


async def test_publish_before_connect_is_an_error():
    bus = Bus("nats://x:4222")
    with pytest.raises(BusError):
        await bus.publish("s", {})


async def test_close_closes():
    nc = FakeNC()
    bus = await make_bus(nc)
    await bus.close()
    assert nc.closed
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_natsio.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.natsio'`

- [ ] **Step 3: Implement**

```python
# grid/natsio.py
"""A thin, honest wrapper over nats-py.

Honest means: a publish that the server refuses raises. nats-py does not raise
for one by itself. A `Permissions Violation for Publish` arrives out of band on
the client's error callback and leaves the connection open, and `flush()` is
only PING/PONG, so a naive publish returns cleanly while the message is
discarded. So the bus installs that callback, records every error it reports,
and drains the record after each flush: an entry naming a permissions violation
on the subject just published becomes a BusError. The hive bridge once read the
post-PING response and discarded it, so a permissions error looked like success
and data was lost with the process still healthy. Not here.
"""
from __future__ import annotations

import collections
import json
import logging
from typing import Any, Callable

import nats
import nats.errors

log = logging.getLogger("grid.natsio")


class BusError(Exception):
    pass


class BusTimeout(BusError):
    pass


class BusClosed(BusError):
    """The client gave up reconnecting; only a process restart fixes it."""


def _encode(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


class Bus:
    def __init__(self, url: str, *, user: str | None = None, password: str | None = None,
                 connect: Callable[..., Any] = nats.connect):
        self.url = url
        self.user = user
        self.password = password
        self._connect = connect
        self._nc = None
        self._errors: collections.deque[str] = collections.deque(maxlen=64)

    async def connect(self) -> None:
        async def on_disconnected() -> None:
            log.warning("nats disconnected from %s; reconnecting", self.url)

        async def on_reconnected() -> None:
            log.info("nats reconnected to %s", self.url)

        async def on_closed() -> None:
            log.warning("nats connection to %s closed for good", self.url)

        async def on_error(e: Exception) -> None:
            self._errors.append(str(e))
            log.warning("nats error: %s", e)

        kwargs: dict[str, Any] = {
            "servers": [self.url], "name": "kannaka-grid",
            # 60 attempts x 2 s is two minutes; after that nats-py closes the
            # client for good and every publish fails silently forever.
            "max_reconnect_attempts": -1, "reconnect_time_wait": 2,
            "disconnected_cb": on_disconnected, "reconnected_cb": on_reconnected,
            "closed_cb": on_closed, "error_cb": on_error,
        }
        if self.user:
            kwargs["user"] = self.user
            kwargs["password"] = self.password
        self._nc = await self._connect(**kwargs)

    def _require(self):
        if self._nc is None:
            raise BusError("bus not connected")
        return self._nc

    async def publish(self, subject: str, payload: dict) -> None:
        nc = self._require()
        if nc.is_closed:
            raise BusClosed("connection closed for good")
        try:
            await nc.publish(subject, _encode(payload))
            await nc.flush(timeout=5)
        except Exception as e:  # nats-py raises a family of errors; all mean "not delivered"
            raise BusError(f"publish {subject} failed: {e}") from e
        while self._errors:     # a refusal arrives on the callback, never on flush
            entry = self._errors.popleft()
            if "Permissions Violation" in entry and subject in entry:
                raise BusError(f"publish {subject} refused: {entry}")

    async def request(self, subject: str, payload: dict, timeout_s: float) -> dict:
        nc = self._require()
        if nc.is_closed:
            raise BusClosed("connection closed for good")
        try:
            msg = await nc.request(subject, _encode(payload), timeout=timeout_s)
        except nats.errors.TimeoutError as e:
            raise BusTimeout(f"no reply on {subject} within {timeout_s}s") from e
        except Exception as e:
            raise BusError(f"request {subject} failed: {e}") from e
        try:
            return json.loads(msg.data)
        except ValueError as e:
            raise BusError(f"reply on {subject} is not JSON: {msg.data[:80]!r}") from e

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.close()
            self._nc = None
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_natsio.py -q`
Expected: `11 passed`

- [ ] **Step 5: Mutation check, then commit**

Remove `await nc.flush(timeout=5)`: `test_publish_raises_when_server_refuses` fails. Revert.

```bash
git add grid/natsio.py tests/test_natsio.py
git commit -m "feat: NATS bus wrapper that raises on refused publishes"
```

---

### Task 7: Flux entity upsert

**Files:**
- Create: `grid/flux.py`
- Test: `tests/test_flux.py`

**Interfaces:**
- Produces: `class Flux` with `__init__(self, base_url: str, *, stream: str = "grid", source: str = "kannaka-grid", client: httpx.AsyncClient | None = None)`, `async upsert(entity_id: str, properties: dict) -> None` (raises `httpx.HTTPStatusError` on non-2xx), `async close()`; module function `flux_event(*, stream, source, entity_id, properties, ts_ms) -> dict`.

The body follows kannaka-memory `src/collective/flux.rs` (`streamId`, `sourceId`, `entityId`, `timestamp`, `payload.entity_id`, `payload.properties`). thegrid's own client uses snake_case top-level keys; both are carried (`stream`, `source` duplicated) so either Flux build accepts it — the runbook verifies against the live Flux and this note is removed once one shape is confirmed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_flux.py
import httpx
import pytest

from grid.flux import Flux, flux_event


def test_flux_event_shape():
    body = flux_event(stream="grid", source="kannaka-grid", entity_id="grid:colony-one",
                      properties={"population": 3}, ts_ms=99)
    assert body == {
        "streamId": "grid", "sourceId": "kannaka-grid", "entityId": "grid:colony-one",
        "stream": "grid", "source": "kannaka-grid", "timestamp": 99,
        "payload": {"entity_id": "grid:colony-one", "properties": {"population": 3}},
    }


async def test_upsert_posts_to_api_events():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read()
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    flux = Flux("http://flux:3000/", client=client)
    await flux.upsert("grid:colony-one", {"population": 3})
    assert seen["url"] == "http://flux:3000/api/events"
    assert b'"entityId":"grid:colony-one"' in seen["json"]


async def test_upsert_raises_on_error_status():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    flux = Flux("http://flux:3000", client=client)
    with pytest.raises(httpx.HTTPStatusError):
        await flux.upsert("grid:x", {})
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_flux.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.flux'`

- [ ] **Step 3: Implement**

```python
# grid/flux.py
"""Publish the colony's epoch state as one Flux entity per colony."""
from __future__ import annotations

import httpx

from .envelope import now_ms


def flux_event(*, stream: str, source: str, entity_id: str, properties: dict, ts_ms: int) -> dict:
    return {
        "streamId": stream, "sourceId": source, "entityId": entity_id,
        "stream": stream, "source": source, "timestamp": ts_ms,
        "payload": {"entity_id": entity_id, "properties": properties},
    }


class Flux:
    def __init__(self, base_url: str, *, stream: str = "grid", source: str = "kannaka-grid",
                 client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.stream = stream
        self.source = source
        self._client = client or httpx.AsyncClient(timeout=10)

    async def upsert(self, entity_id: str, properties: dict) -> None:
        body = flux_event(stream=self.stream, source=self.source, entity_id=entity_id,
                          properties=properties, ts_ms=now_ms())
        response = await self._client.post(f"{self.base_url}/api/events", json=body)
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_flux.py -q`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add grid/flux.py tests/test_flux.py
git commit -m "feat: Flux entity upsert for colony state"
```

---

### Task 8: Mind client and memory throttle

**Files:**
- Create: `grid/mind.py`
- Test: `tests/test_mind.py`

**Interfaces:**
- Consumes: `Bus.request` from Task 6.
- Produces:
  - `class Throttle(min_interval_s: float, clock=time.monotonic)` with `allow(key: str) -> bool`.
  - `class MindError(Exception)`.
  - `class Mind(agent_id: str, bus: Bus, *, kannaka_bin: str = "kannaka", data_dir: Path | None = None, runner=subprocess.run)` with
    - `remember(text: str, tags: list[str], *, category: str = "grid") -> bool` (subprocess `kannaka remember <text> --category grid --tags a,b`, env `KANNAKA_DATA_DIR` when `data_dir` set; True on exit 0),
    - `async ask(text: str, *, recall_query: str | None = None, timeout_s: float = 180.0) -> str` (raises `MindError` on `{"error": ...}` or missing text),
    - `async recall(query: str, top_k: int = 5) -> list[str]` (contents only; empty list on error).
  - Constants `ASK_TIMEOUT_S = 180.0`, `REMEMBER_MIN_INTERVAL_S = 300.0`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mind.py
import subprocess
from pathlib import Path

import pytest

from grid.mind import ASK_TIMEOUT_S, REMEMBER_MIN_INTERVAL_S, Mind, MindError, Throttle


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_throttle_allows_first_then_blocks_until_interval():
    clock = Clock()
    th = Throttle(10.0, clock=clock)
    assert th.allow("storm") is True
    assert th.allow("storm") is False
    assert th.allow("sweep") is True         # independent keys
    clock.t = 9.9
    assert th.allow("storm") is False
    clock.t = 10.0
    assert th.allow("storm") is True


class FakeBus:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    async def request(self, subject, payload, timeout_s):
        self.calls.append((subject, payload, timeout_s))
        if self.exc:
            raise self.exc
        return self.reply


def test_remember_runs_kannaka_with_category_tags_and_data_dir(tmp_path):
    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    mind = Mind("grid-colony-one", FakeBus(), kannaka_bin="/usr/local/bin/kannaka",
                data_dir=tmp_path, runner=runner)
    assert mind.remember("storm at tick 12", ["colony-one", "storm"]) is True
    cmd, kw = calls[0]
    assert cmd == ["/usr/local/bin/kannaka", "remember", "storm at tick 12",
                   "--category", "grid", "--tags", "colony-one,storm"]
    assert kw["env"]["KANNAKA_DATA_DIR"] == str(tmp_path)
    assert kw["timeout"] == 60


def test_remember_reports_failure():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")
    mind = Mind("m", FakeBus(), runner=runner)
    assert mind.remember("x", []) is False


def test_remember_returns_false_when_runner_raises():
    def hangs(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 60)

    def missing(cmd, **kw):
        raise FileNotFoundError("kannaka")

    assert Mind("m", FakeBus(), runner=hangs).remember("x", []) is False
    assert Mind("m", FakeBus(), runner=missing).remember("x", []) is False


async def test_ask_sends_kannaka_contract_and_returns_text():
    bus = FakeBus(reply={"from": "grid-colony-one", "text": "  fork earlier  ", "mode_used": "full_recall_no_tools"})
    mind = Mind("grid-colony-one", bus)
    assert await mind.ask("what now?", recall_query="fork") == "fork earlier"
    subject, payload, timeout = bus.calls[0]
    assert subject == "KANNAKA.ask.grid-colony-one"
    assert payload == {"from": "kannaka-grid", "text": "what now?", "recall_query": "fork",
                       "no_tools": True, "mode": "full_recall"}
    assert timeout == ASK_TIMEOUT_S


async def test_ask_error_reply_raises():
    mind = Mind("m", FakeBus(reply={"from": "m", "error": "upstream model timeout"}))
    with pytest.raises(MindError, match="upstream model timeout"):
        await mind.ask("q")


async def test_ask_empty_text_raises():
    mind = Mind("m", FakeBus(reply={"from": "m", "text": ""}))
    with pytest.raises(MindError):
        await mind.ask("q")


async def test_recall_returns_contents_and_swallows_errors():
    bus = FakeBus(reply={"from": "m", "query": "storms", "results": [
        {"id": "1", "content": "storm at 12", "similarity": 0.9},
        {"id": "2", "content": "storm at 40", "similarity": 0.5}]})
    mind = Mind("m", bus)
    assert await mind.recall("storms", top_k=2) == ["storm at 12", "storm at 40"]
    assert bus.calls[0][0] == "KANNAKA.recall.m" and bus.calls[0][1] == {"query": "storms", "top_k": 2}
    assert await Mind("m", FakeBus(exc=RuntimeError("down"))).recall("x") == []


def test_defaults_match_spec():
    assert REMEMBER_MIN_INTERVAL_S == 300.0 and ASK_TIMEOUT_S == 180.0
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_mind.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.mind'`

- [ ] **Step 3: Implement**

```python
# grid/mind.py
"""The colony mind, as seen from the bridge and the relay.

Writes go through the `kannaka remember` CLI against the mind's own data dir
(the serve daemon is read-only and holds the store in RAM; it sees new
memories after its scheduled dream+restart). Reads and asks go over NATS to
the daemon using kannaka's existing ask/recall contract.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("grid.mind")

ASK_TIMEOUT_S = 180.0
REMEMBER_MIN_INTERVAL_S = 300.0
CALLER_ID = "kannaka-grid"


class MindError(Exception):
    pass


class Throttle:
    def __init__(self, min_interval_s: float, clock: Callable[[], float] = time.monotonic):
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self.min_interval_s:
            return False
        self._last[key] = now
        return True


class Mind:
    def __init__(self, agent_id: str, bus, *, kannaka_bin: str = "kannaka",
                 data_dir: Path | None = None, runner=subprocess.run):
        self.agent_id = agent_id
        self.bus = bus
        self.kannaka_bin = kannaka_bin
        self.data_dir = data_dir
        self._run = runner

    def remember(self, text: str, tags: list[str], *, category: str = "grid") -> bool:
        cmd = [self.kannaka_bin, "remember", text, "--category", category]
        if tags:
            cmd += ["--tags", ",".join(tags)]
        env = dict(os.environ)
        if self.data_dir is not None:
            env["KANNAKA_DATA_DIR"] = str(self.data_dir)
        try:
            result = self._run(cmd, env=env, capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("kannaka remember failed to run: %s", e)
            return False
        return result.returncode == 0

    async def ask(self, text: str, *, recall_query: str | None = None,
                  timeout_s: float = ASK_TIMEOUT_S) -> str:
        payload = {"from": CALLER_ID, "text": text, "recall_query": recall_query,
                   "no_tools": True, "mode": "full_recall"}
        reply = await self.bus.request(f"KANNAKA.ask.{self.agent_id}", payload, timeout_s=timeout_s)
        if reply.get("error"):
            raise MindError(f"{self.agent_id}: {reply['error']}")
        answer = str(reply.get("text") or "").strip()
        if not answer:
            raise MindError(f"{self.agent_id}: empty answer")
        return answer

    async def recall(self, query: str, top_k: int = 5) -> list[str]:
        try:
            reply = await self.bus.request(f"KANNAKA.recall.{self.agent_id}",
                                           {"query": query, "top_k": top_k}, timeout_s=15)
        except Exception:
            return []
        return [str(r.get("content", "")) for r in reply.get("results", []) if r.get("content")]
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_mind.py -q`
Expected: `9 passed`

- [ ] **Step 5: Mutation check, then commit**

In `ask`, change `if reply.get("error"):` to `if False:`: `test_ask_error_reply_raises` fails. Revert.

```bash
git add grid/mind.py tests/test_mind.py
git commit -m "feat: mind client (remember via CLI, ask/recall via NATS) and throttle"
```

---

### Task 9: The bridge

**Files:**
- Create: `grid/bridge.py`
- Test: `tests/test_bridge.py`

**Interfaces:**
- Consumes: `Snapshot, Distiller, Event` (Task 3); `envelope, subject, slug` (Task 2); `Bus` (Task 6); `Flux` (Task 7); `Mind, Throttle, REMEMBER_MIN_INTERVAL_S` (Task 8); `chronicle.question/check/KINDS/CHRONICLE_EVERY_TICKS` (Task 5).
- Produces:
  - `iter_sse_data(lines: Iterable[str]) -> Iterator[str]` — yields the JSON text of each `data:` frame, ignoring comments/keepalives.
  - `memory_text(event: Event) -> str` — the one-line fact remembered per event.
  - `state_properties(snap: Snapshot) -> dict` — the scalar fields sent to Flux and `GRID.<colony>.state`.
  - `BridgeConfig` dataclass + `BridgeConfig.from_env() -> BridgeConfig`.
  - `epoch_summary_text(event: Event, history: dict) -> str` — the one-line epoch summary built from `/api/history`.
  - `class Bridge(cfg, *, bus, mind, flux=None, distiller=None, throttle=None, clock=time.monotonic, history_fetch=None)` with `async handle_state(state: dict) -> list[Event]`, `async publish_state(snap) -> None`, `async poll_history(summary: dict) -> list[Event]`, `async maybe_chronicle() -> dict[str, str]`, `schedule_chronicle() -> None` (runs `maybe_chronicle` as a single in-flight asyncio task), `agent_id`, `colony` set on first state.
  - `async run(cfg)` — the daemon loop; `main()` entry.

Environment (all read in `BridgeConfig.from_env`): `COLONY_URL` (default `http://127.0.0.1:8787`), `NATS_URL` (default `nats://swarm.ninja-portal.com:4222`), `NATS_USER`, `NATS_PASSWORD`, `FLUX_URL` (optional), `MIND_AGENT_ID` (default `grid-<colony>` once the name is known; override allowed), `MIND_DATA_DIR`, `KANNAKA_BIN` (default `kannaka`), `STATE_EVERY_S` (60), `HISTORY_EVERY_S` (60).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bridge.py
import json

import pytest

from grid.bridge import (Bridge, BridgeConfig, epoch_summary_text, iter_sse_data,
                        memory_text, state_properties)
from grid.chronicle import CHRONICLE_EVERY_TICKS
from grid.distill import Event, Snapshot


def state(**over) -> dict:
    base = {"name": "Colony One", "epoch": 1, "startedAt": 1.0, "tick": 100, "population": 100,
            "generation": 1, "births": 1, "deaths": 0, "memory": 0.1, "heat": 50.0, "cost": 1.0,
            "strains": [60, 40], "tasks": {}, "dominant": "abc", "ancestor": "abc",
            "isa": ["nop", "harvest", "fork"],
            "weather": {"storms": 0, "droughtQuadrant": None, "bloomQuadrant": None, "source": "simulated"}}
    base.update(over)
    return base


class FakeBus:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


class FakeMind:
    def __init__(self, answers=None):
        self.remembered: list[tuple[str, list[str]]] = []
        self.asked: list[str] = []
        self.answers = answers or {}

    def remember(self, text, tags, *, category="grid"):
        self.remembered.append((text, tags))
        return True

    async def ask(self, text, *, recall_query=None, timeout_s=180.0):
        self.asked.append(text)
        for key, answer in self.answers.items():
            if key in text:
                return answer
        return "nothing"


class FakeFlux:
    def __init__(self):
        self.upserts = []

    async def upsert(self, entity_id, properties):
        self.upserts.append((entity_id, properties))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def cfg() -> BridgeConfig:
    return BridgeConfig(colony_url="http://127.0.0.1:8787", nats_url="nats://x", nats_user=None,
                        nats_password=None, flux_url=None, mind_agent_id=None, mind_data_dir=None,
                        kannaka_bin="kannaka", state_every_s=60, history_every_s=60)


def test_iter_sse_data_yields_data_frames_only():
    lines = ["data:{\"a\":1}", "", ": keepalive", "", "data:{\"a\":2}", ""]
    assert list(iter_sse_data(lines)) == ['{"a":1}', '{"a":2}']


def test_memory_text_is_one_line_with_kind_epoch_tick_and_fields():
    text = memory_text(Event("storm", 3, 4100, {"drought_quadrant": 1, "bloom_quadrant": 3, "source": "simulated"}))
    assert "\n" not in text
    assert text.startswith("Colony event storm at epoch 3 tick 4100:")
    assert "drought_quadrant=1" in text and "bloom_quadrant=3" in text


def test_state_properties_are_scalars():
    props = state_properties(Snapshot.from_state(state()))
    assert props == {"epoch": 1, "tick": 100, "population": 100, "generation": 1, "births": 1,
                     "deaths": 0, "heat": 50.0, "cost": 1.0, "memory": 0.1, "storms": 0,
                     "dominant": "abc", "lineage_top": 0.6, "novel_ops": 0}


async def test_handle_state_publishes_each_event_with_envelope_and_remembers():
    bus, mind = FakeBus(), FakeMind()
    bridge = Bridge(cfg(), bus=bus, mind=mind)
    events = await bridge.handle_state(state())
    assert [e.kind for e in events] == ["epoch.started", "lineage.sweep"]
    assert bridge.colony == "colony-one" and bridge.agent_id == "grid-colony-one"
    subjects = [s for s, _ in bus.published]
    assert subjects == ["GRID.colony-one.event.epoch.started", "GRID.colony-one.event.lineage.sweep"]
    payload = bus.published[1][1]
    assert payload["schema_version"] == "1.0" and payload["agent_id"] == "grid-colony-one"
    assert payload["colony"] == "colony-one" and payload["epoch"] == 1 and payload["tick"] == 100
    assert payload["share"] == 0.6 and payload["kind"] == "lineage.sweep"
    assert [tags for _, tags in mind.remembered] == [["colony-one", "epoch.started"], ["colony-one", "lineage.sweep"]]


async def test_memory_is_throttled_per_kind_but_events_still_publish():
    clock = Clock()
    bus, mind = FakeBus(), FakeMind()
    bridge = Bridge(cfg(), bus=bus, mind=mind, clock=clock)
    await bridge.handle_state(state(tick=1))
    storm = {"storms": 1, "droughtQuadrant": 0, "bloomQuadrant": 2, "source": "simulated"}
    await bridge.handle_state(state(tick=2, weather=storm))
    storm2 = dict(storm, storms=2)
    await bridge.handle_state(state(tick=3, weather=storm2))
    kinds = [s.rsplit(".", 1)[-1] for s, _ in bus.published]
    assert kinds.count("storm") == 2
    assert sum(1 for _, tags in mind.remembered if "storm" in tags) == 1
    clock.t = 301.0
    await bridge.handle_state(state(tick=4, weather=dict(storm, storms=3)))
    assert sum(1 for _, tags in mind.remembered if "storm" in tags) == 2


HISTORY = {"currentEpoch": 2,
           "totals": {"genomes": 41, "births": 900, "epochs": 2},
           "mutationMechanisms": [{"mutation_type": "point", "later_reproductions": 30},
                                  {"mutation_type": "duplication", "later_reproductions": 4}],
           "ecology": [{"epoch": 1, "bucket": 1, "lineage_top_avg": 0.31, "lineages_avg": 5.0},
                       {"epoch": 1, "bucket": 2, "lineage_top_avg": 0.72, "lineages_avg": 3.5}]}


def test_epoch_summary_text_is_one_grounded_line():
    event = Event("epoch.retired", 1, 4000,
                  {"ticks": 4000, "births": 900, "deaths": 880, "extinct": True})
    assert epoch_summary_text(event, HISTORY) == (
        "Colony epoch 1 retired at tick 4000: births=900 deaths=880 extinct=True; "
        "genomes=41 epochs=2; mechanisms=point:30,duplication:4; "
        "top lineage share avg=0.72; lineages avg=3.5")


def test_epoch_summary_text_survives_a_summary_missing_every_key():
    event = Event("epoch.retired", 1, 4000, {})
    assert epoch_summary_text(event, {}).startswith("Colony epoch 1 retired at tick None")


async def test_epoch_retired_remembers_one_summary_from_history():
    bus, mind = FakeBus(), FakeMind()

    async def history_fetch():
        return HISTORY

    bridge = Bridge(cfg(), bus=bus, mind=mind, history_fetch=history_fetch)
    await bridge.handle_state(state(epoch=1, tick=4000))
    before = len(mind.remembered)
    await bridge.handle_state(state(epoch=2, tick=1))
    summaries = [t for t, _ in mind.remembered[before:] if t.startswith("Colony epoch 1 retired")]
    assert len(summaries) == 1
    assert [tags for _, tags in mind.remembered[before:]].count(["colony-one", "epoch.retired"]) == 2


async def test_a_failing_history_fetch_costs_the_summary_not_the_event():
    bus, mind = FakeBus(), FakeMind()

    async def history_fetch():
        raise RuntimeError("colony busy")

    bridge = Bridge(cfg(), bus=bus, mind=mind, history_fetch=history_fetch)
    await bridge.handle_state(state(epoch=1, tick=4000))
    events = await bridge.handle_state(state(epoch=2, tick=1))
    assert "epoch.retired" in [e.kind for e in events]
    assert "GRID.colony-one.event.epoch.retired" in [s for s, _ in bus.published]
    assert not [t for t, _ in mind.remembered if t.startswith("Colony epoch 1 retired")]


async def test_publish_state_goes_to_bus_and_flux():
    bus, flux = FakeBus(), FakeFlux()
    bridge = Bridge(cfg(), bus=bus, mind=FakeMind(), flux=flux)
    await bridge.handle_state(state())
    await bridge.publish_state(bridge.last)
    assert bus.published[-1][0] == "GRID.colony-one.state"
    assert bus.published[-1][1]["population"] == 100
    assert flux.upserts == [("grid:colony-one", state_properties(bridge.last))]


async def test_chronicle_fires_on_phase_boundary_and_publishes_checked_answers():
    bus = FakeBus()
    mind = FakeMind(answers={"What is this epoch about": "A slow thermal squeeze.",
                             "Name the capability": "It learned to fork early using fork.",
                             "single most important": "Lineage 0 swept at tick 1900."})
    bridge = Bridge(cfg(), bus=bus, mind=mind)
    await bridge.handle_state(state(tick=10))
    assert await bridge.maybe_chronicle() == {}
    await bridge.handle_state(state(tick=CHRONICLE_EVERY_TICKS))
    out = await bridge.maybe_chronicle()
    assert out == {"topic": "A slow thermal squeeze.",
                   "skill": "It learned to fork early using fork.",
                   "event": "Lineage 0 swept at tick 1900."}
    subjects = [s for s, _ in bus.published if ".chronicle." in s]
    assert subjects == ["GRID.colony-one.chronicle.topic", "GRID.colony-one.chronicle.skill",
                        "GRID.colony-one.chronicle.event"]
    assert bus.published[-1][1]["text"] == "Lineage 0 swept at tick 1900."
    assert bus.published[-1][1]["since_tick"] == 0
    await bridge.handle_state(state(tick=CHRONICLE_EVERY_TICKS + 5))
    assert await bridge.maybe_chronicle() == {}          # not twice for one phase


async def test_chronicle_drops_answers_that_fail_their_check():
    bus = FakeBus()
    mind = FakeMind(answers={"What is this epoch about": "Fine.",
                             "Name the capability": "It got faster somehow.",     # no opcode
                             "single most important": "Something happened."})     # no tick
    bridge = Bridge(cfg(), bus=bus, mind=mind)
    await bridge.handle_state(state(tick=CHRONICLE_EVERY_TICKS))
    assert await bridge.maybe_chronicle() == {"topic": "Fine."}


async def test_poll_history_publishes_dialect_events():
    bus = FakeBus()
    bridge = Bridge(cfg(), bus=bus, mind=FakeMind())
    await bridge.handle_state(state())
    events = await bridge.poll_history({"currentEpoch": 1, "ecology": [
        {"epoch": 1, "bucket": 1, "signals_avg": 9.0}, {"epoch": 1, "bucket": 2, "signals_avg": 9.0}]})
    assert [e.kind for e in events] == ["signal.dialect"]
    assert bus.published[-1][0] == "GRID.colony-one.event.signal.dialect"


class FailingBus(FakeBus):
    """Refuses every publish, the way a NATS permissions error surfaces."""

    async def publish(self, subject, payload):
        from grid.natsio import BusError
        raise BusError("permissions violation")


class FailingFlux(FakeFlux):
    async def upsert(self, entity_id, properties):
        raise RuntimeError("flux down")


class BusErrorMind(FakeMind):
    """Every ask fails the way a dead daemon does (timeout is a BusError subclass)."""

    async def ask(self, text, *, recall_query=None, timeout_s=180.0):
        from grid.natsio import BusTimeout
        raise BusTimeout("no reply")


class RefusingMind(FakeMind):
    def remember(self, text, tags, *, category="grid"):
        self.remembered.append((text, tags))
        return False


class ClosedBus(FakeBus):
    """The client gave up reconnecting; nothing will be delivered again."""

    async def publish(self, subject, payload):
        from grid.natsio import BusClosed
        raise BusClosed("connection closed for good")


async def test_closed_bus_is_fatal_not_dropped():
    from grid.natsio import BusClosed
    bridge = Bridge(cfg(), bus=ClosedBus(), mind=FakeMind())
    with pytest.raises(BusClosed):
        await bridge.handle_state(state())


async def test_refused_publish_is_dropped_not_raised():
    bridge = Bridge(cfg(), bus=FailingBus(), mind=FakeMind())
    events = await bridge.handle_state(state())
    assert [e.kind for e in events] == ["epoch.started", "lineage.sweep"]


async def test_flux_failure_does_not_block_state_publish():
    bus = FakeBus()
    bridge = Bridge(cfg(), bus=bus, mind=FakeMind(), flux=FailingFlux())
    await bridge.handle_state(state())
    await bridge.publish_state(bridge.last)
    assert bus.published[-1][0] == "GRID.colony-one.state"


async def test_chronicle_skips_kinds_whose_ask_fails():
    bus = FakeBus()
    bridge = Bridge(cfg(), bus=bus, mind=BusErrorMind())
    await bridge.handle_state(state(tick=CHRONICLE_EVERY_TICKS))
    assert await bridge.maybe_chronicle() == {}
    assert not [s for s, _ in bus.published if ".chronicle." in s]


async def test_refused_memory_still_publishes_event():
    bus, mind = FakeBus(), RefusingMind()
    bridge = Bridge(cfg(), bus=bus, mind=mind)
    await bridge.handle_state(state())
    assert len(bus.published) == 2 and len(mind.remembered) == 2


async def test_schedule_chronicle_spawns_at_most_one_task():
    import asyncio
    bridge = Bridge(cfg(), bus=FakeBus(), mind=FakeMind(answers={
        "What is this epoch about": "A slow squeeze.",
        "Name the capability": "fork early via fork.",
        "single most important": "Sweep at tick 1900."}))
    await bridge.handle_state(state(tick=CHRONICLE_EVERY_TICKS))
    bridge.schedule_chronicle()
    first = bridge._chronicle_task
    bridge.schedule_chronicle()
    assert bridge._chronicle_task is first
    await first
    assert [s for s, _ in bridge.bus.published if ".chronicle." in s] == [
        "GRID.colony-one.chronicle.topic", "GRID.colony-one.chronicle.skill",
        "GRID.colony-one.chronicle.event"]
    bridge.schedule_chronicle()          # same phase: nothing new spawned
    assert bridge._chronicle_task is first


def test_config_from_env_defaults(monkeypatch):
    for k in ("COLONY_URL", "NATS_URL", "NATS_USER", "NATS_PASSWORD", "FLUX_URL", "MIND_AGENT_ID",
              "MIND_DATA_DIR", "KANNAKA_BIN", "STATE_EVERY_S", "HISTORY_EVERY_S"):
        monkeypatch.delenv(k, raising=False)
    c = BridgeConfig.from_env()
    assert c.colony_url == "http://127.0.0.1:8787"
    assert c.nats_url == "nats://swarm.ninja-portal.com:4222"
    assert c.flux_url is None and c.mind_agent_id is None
    assert c.state_every_s == 60 and c.history_every_s == 60


async def test_reconnect_backoff_resets_after_a_stream_that_delivered_frames(monkeypatch):
    from grid import bridge as bridge_mod

    delays: list[float] = []
    attempts = {"n": 0}

    class ConnectingBus(FakeBus):
        async def connect(self):
            pass

    async def fake_stream(config, bridge):
        attempts["n"] += 1
        if attempts["n"] == 3:          # this attempt actually read the colony
            bridge.frames_handled += 1
        raise RuntimeError("colony went away")

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) == 5:
            raise KeyboardInterrupt     # the only way out of the daemon loop

    monkeypatch.setattr(bridge_mod, "Bus", lambda *a, **k: ConnectingBus())
    monkeypatch.setattr(bridge_mod, "Mind", lambda *a, **k: FakeMind())
    monkeypatch.setattr(bridge_mod, "_stream", fake_stream)
    monkeypatch.setattr(bridge_mod.asyncio, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        await bridge_mod.run(cfg())
    # A stream that worked before it dropped is not a failing config: back to 1 s.
    assert delays == [1.0, 2.0, 1.0, 2.0, 4.0]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_bridge.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.bridge'`

- [ ] **Step 3: Implement**

```python
# grid/bridge.py
"""The bridge: colony stream in, GRID.* events / state / chronicle out.

A thin loop over the pure cores. Everything with a network has an injectable
fake so the loop's decisions are tested without one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Iterator

import httpx

from . import chronicle as chron
from .distill import Distiller, Event, Snapshot
from .envelope import envelope, slug, subject
from .flux import Flux
from .mind import REMEMBER_MIN_INTERVAL_S, Mind, MindError, Throttle
from .natsio import Bus, BusClosed, BusError

log = logging.getLogger("grid.bridge")


def iter_sse_data(lines: Iterable[str]) -> Iterator[str]:
    for line in lines:
        if line.startswith("data:"):
            yield line[len("data:"):].strip()


def memory_text(event: Event) -> str:
    fields = " ".join(f"{k}={v}" for k, v in sorted(event.fields.items()))
    return f"Colony event {event.kind} at epoch {event.epoch} tick {event.tick}: {fields}".strip()


def epoch_summary_text(event: Event, history: dict) -> str:
    """One line about a retired epoch, from its event and /api/history.

    Pure, and `.get` throughout: thegrid's summary grows keys between versions
    and a missing one must cost the sentence a value, never the memory.
    """
    fields = event.fields
    totals = history.get("totals") or {}
    mechanisms = ",".join(f"{m.get('mutation_type')}:{m.get('later_reproductions')}"
                          for m in history.get("mutationMechanisms") or [])
    ecology = history.get("ecology") or []
    latest = ecology[-1] if ecology else {}
    return (f"Colony epoch {event.epoch} retired at tick {fields.get('ticks')}: "
            f"births={fields.get('births')} deaths={fields.get('deaths')} "
            f"extinct={fields.get('extinct')}; "
            f"genomes={totals.get('genomes')} epochs={totals.get('epochs')}; "
            f"mechanisms={mechanisms}; "
            f"top lineage share avg={latest.get('lineage_top_avg')}; "
            f"lineages avg={latest.get('lineages_avg')}")


def state_properties(snap: Snapshot) -> dict:
    return {
        "epoch": snap.epoch, "tick": snap.tick, "population": snap.population,
        "generation": snap.generation, "births": snap.births, "deaths": snap.deaths,
        "heat": snap.heat, "cost": snap.cost, "memory": snap.memory, "storms": snap.storms,
        "dominant": snap.dominant, "lineage_top": round(snap.lineage_top, 4),
        "novel_ops": snap.novel_ops,
    }


@dataclass(frozen=True)
class BridgeConfig:
    colony_url: str
    nats_url: str
    nats_user: str | None
    nats_password: str | None
    flux_url: str | None
    mind_agent_id: str | None
    mind_data_dir: str | None
    kannaka_bin: str
    state_every_s: float
    history_every_s: float

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        env = os.environ.get
        return cls(
            colony_url=env("COLONY_URL", "http://127.0.0.1:8787").rstrip("/"),
            nats_url=env("NATS_URL", "nats://swarm.ninja-portal.com:4222"),
            nats_user=env("NATS_USER") or None,
            nats_password=env("NATS_PASSWORD") or None,
            flux_url=env("FLUX_URL") or None,
            mind_agent_id=env("MIND_AGENT_ID") or None,
            mind_data_dir=env("MIND_DATA_DIR") or None,
            kannaka_bin=env("KANNAKA_BIN", "kannaka"),
            state_every_s=float(env("STATE_EVERY_S", "60")),
            history_every_s=float(env("HISTORY_EVERY_S", "60")),
        )


class Bridge:
    def __init__(self, cfg: BridgeConfig, *, bus, mind, flux=None, distiller: Distiller | None = None,
                 throttle: Throttle | None = None, clock=time.monotonic,
                 history_fetch: Callable[[], Awaitable[dict]] | None = None):
        self.cfg = cfg
        self.bus = bus
        self.mind = mind
        self.flux = flux
        self.history_fetch = history_fetch
        self.distiller = distiller or Distiller()
        self.throttle = throttle or Throttle(REMEMBER_MIN_INTERVAL_S, clock=clock)
        self.colony: str | None = None
        self.agent_id: str | None = None
        self.last: Snapshot | None = None
        self.isa: list[str] = []
        self.frames_handled: int = 0
        self._chronicled_phase: int = -1
        self._last_chronicle_tick: int = 0
        self._chronicle_task: asyncio.Task | None = None

    # -- identity ------------------------------------------------------------

    def _bind(self, state: dict) -> None:
        if self.colony is None:
            self.colony = slug(str(state["name"]))
            self.agent_id = self.cfg.mind_agent_id or f"grid-{self.colony}"
            if hasattr(self.mind, "agent_id") and self.cfg.mind_agent_id is None:
                self.mind.agent_id = self.agent_id
        self.isa = [str(op) for op in state.get("isa", [])] or self.isa

    def _env(self, epoch: int, tick: int, **fields) -> dict:
        return envelope(agent_id=self.agent_id, colony=self.colony, epoch=epoch, tick=tick, **fields)

    # -- events --------------------------------------------------------------

    async def _emit(self, event: Event) -> None:
        subj = subject(self.colony, "event", event.kind)
        try:
            await self.bus.publish(subj, self._env(event.epoch, event.tick, kind=event.kind, **event.fields))
        except BusClosed:
            raise
        except BusError as e:
            log.error("dropped %s: %s", subj, e)
            return
        if self.throttle.allow(event.kind):
            # remember() shells out to `kannaka remember` with a 60 s timeout; run
            # it off the stream-reader coroutine or the whole bridge stalls on it.
            ok = await asyncio.to_thread(self.mind.remember, memory_text(event),
                                         [self.colony, event.kind])
            if not ok:
                log.warning("mind refused memory for %s", event.kind)

    async def _remember_epoch(self, event: Event) -> None:
        """The epoch summary spec 6 asks for. One per epoch, so never throttled."""
        if self.history_fetch is None:
            return
        try:
            history = await self.history_fetch()
        except Exception as e:      # the epoch is still published; only the memory is lost
            log.warning("epoch %d summary skipped: history fetch failed: %s", event.epoch, e)
            return
        ok = await asyncio.to_thread(self.mind.remember, epoch_summary_text(event, history),
                                     [self.colony, "epoch.retired"])
        if not ok:
            log.warning("mind refused the epoch %d summary", event.epoch)

    async def handle_state(self, state: dict) -> list[Event]:
        self._bind(state)
        snap = Snapshot.from_state(state)
        events = self.distiller.feed(snap)
        self.last = snap
        self.frames_handled += 1
        for event in events:
            await self._emit(event)
            if event.kind == "epoch.retired":
                await self._remember_epoch(event)
        return events

    async def poll_history(self, summary: dict) -> list[Event]:
        events = self.distiller.feed_history(summary)
        for event in events:
            await self._emit(event)
        return events

    # -- state -----------------------------------------------------------------

    async def publish_state(self, snap: Snapshot) -> None:
        props = state_properties(snap)
        # epoch/tick already travel in the envelope; passing them twice is a clash.
        extra = {k: v for k, v in props.items() if k not in ("epoch", "tick")}
        try:
            await self.bus.publish(subject(self.colony, "state"), self._env(snap.epoch, snap.tick, **extra))
        except BusClosed:
            raise
        except BusError as e:
            log.error("state publish failed: %s", e)
        if self.flux is not None:
            try:
                await self.flux.upsert(f"grid:{self.colony}", props)
            except Exception as e:  # Flux is optional; never let it stall the bridge
                log.warning("flux upsert failed: %s", e)

    # -- chronicle ---------------------------------------------------------------

    async def maybe_chronicle(self) -> dict[str, str]:
        snap = self.last
        if snap is None:
            return {}
        phase = snap.tick // chron.CHRONICLE_EVERY_TICKS
        if phase == 0 or phase == self._chronicled_phase:
            return {}
        self._chronicled_phase = phase
        since = self._last_chronicle_tick
        self._last_chronicle_tick = snap.tick
        out: dict[str, str] = {}
        for kind in chron.KINDS:
            q = chron.question(kind, colony=self.colony, epoch=snap.epoch, tick=snap.tick, since_tick=since)
            try:
                raw = await self.mind.ask(q, recall_query=f"colony {self.colony} epoch {snap.epoch}")
            except BusClosed:
                raise
            except (MindError, BusError) as e:
                log.warning("chronicle %s: mind unavailable: %s", kind, e)
                continue
            answer = chron.check(kind, raw, isa=self.isa)
            if answer is None:
                log.info("chronicle %s failed its check: %r", kind, raw[:120])
                continue
            out[kind] = answer
            try:
                await self.bus.publish(subject(self.colony, "chronicle", kind),
                                       self._env(snap.epoch, snap.tick, text=answer, since_tick=since))
            except BusClosed:
                raise
            except BusError as e:
                log.error("chronicle %s publish failed: %s", kind, e)
        return out

    def schedule_chronicle(self) -> None:
        """Run the chronicle off the stream path; at most one in flight."""
        if self._chronicle_task is not None and not self._chronicle_task.done():
            return
        if self.last is None:
            return
        phase = self.last.tick // chron.CHRONICLE_EVERY_TICKS
        if phase == 0 or phase == self._chronicled_phase:
            return
        self._chronicle_task = asyncio.create_task(self.maybe_chronicle())


# -- daemon --------------------------------------------------------------------

async def _stream(cfg: BridgeConfig, bridge: Bridge) -> None:
    """Consume /api/stream until it drops; the caller reconnects."""
    # thegrid keepalives every 15 s, so a 60 s read timeout means a frozen
    # colony drops the connection and the caller reconnects instead of hanging.
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=60)) as client:
        async def fetch_history() -> dict:
            r = await client.get(f"{cfg.colony_url}/api/history")
            r.raise_for_status()
            return r.json()

        bridge.history_fetch = fetch_history
        async with client.stream("GET", f"{cfg.colony_url}/api/stream") as response:
            response.raise_for_status()
            last_state, last_history = 0.0, 0.0
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                try:
                    await bridge.handle_state(json.loads(data))
                except BusClosed:       # fatal: the caller exits for a systemd restart
                    raise
                except Exception as e:  # one bad frame must not cost the connection
                    log.warning("skipped unreadable frame: %s", e)
                    continue
                now = time.monotonic()
                if now - last_state >= cfg.state_every_s and bridge.last is not None:
                    await bridge.publish_state(bridge.last)
                    last_state = now
                if now - last_history >= cfg.history_every_s:
                    try:
                        await bridge.poll_history(await fetch_history())
                    except BusClosed:
                        raise
                    except Exception as e:
                        log.warning("history poll failed: %s", e)
                    last_history = now
                bridge.schedule_chronicle()


async def run(cfg: BridgeConfig) -> None:
    bus = Bus(cfg.nats_url, user=cfg.nats_user, password=cfg.nats_password)
    backoff = 1.0
    while True:
        try:
            await bus.connect()
            break
        except Exception as e:      # a boot-time DNS or NATS blip must not burn a start slot
            log.error("nats connect failed: %s; retrying in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    mind = Mind(cfg.mind_agent_id or "grid-unbound", bus, kannaka_bin=cfg.kannaka_bin,
                data_dir=Path(cfg.mind_data_dir) if cfg.mind_data_dir else None)
    flux = Flux(cfg.flux_url) if cfg.flux_url else None
    if flux is None:
        log.info("FLUX_URL unset; Flux publishing skipped")
    bridge = Bridge(cfg, bus=bus, mind=mind, flux=flux)
    backoff = 1.0
    while True:
        frames = bridge.frames_handled
        try:
            await _stream(cfg, bridge)
            backoff = 1.0
        except BusClosed as e:      # EX_TEMPFAIL: only a restart gets a live client back
            log.error("nats gave up: %s; exiting for systemd to restart", e)
            raise SystemExit(75) from e
        except Exception as e:
            if bridge.frames_handled > frames:
                backoff = 1.0       # that connection worked; this is a fresh drop
            log.error("stream dropped: %s; reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run(BridgeConfig.from_env()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_bridge.py -q`
Expected: `21 passed`

- [ ] **Step 5: Mutation checks, then commit**

1. In `_emit`, drop the `if self.throttle.allow(...)` guard (always remember): `test_memory_is_throttled_per_kind_but_events_still_publish` fails.
2. In `maybe_chronicle`, remove `or phase == self._chronicled_phase`: the "not twice for one phase" assert fails.
Revert both.

```bash
git add grid/bridge.py tests/test_bridge.py
git commit -m "feat: bridge loop — events, state, history, chronicle over the colony stream"
```

---

### Task 10: The Odin relay

**Files:**
- Create: `grid/odin.py`
- Test: `tests/test_odin.py`

**Interfaces:**
- Consumes: `parse_request, render_prompt, validate, write_proposal, Rejection` (Task 4); `Mind, MindError, Throttle, REMEMBER_MIN_INTERVAL_S` (Task 8); `Bus, BusClosed, BusError, BusTimeout` (Task 6); `envelope, subject` (Task 2).
- Produces: `class Relay(queue: Path, *, colony: str, agent_id: str, mind, bus, timeout_s=ASK_TIMEOUT_S, retry_after_s=60.0, clock=time.monotonic, throttle=None, state_fetch=None)` with `async poll_once() -> str | None` returning `"proposed"`, `"rejected"`, `"deferred"`, or `None`; `async run(cfg)`; `RelayConfig.from_env()`; `main()`.

Rules (spec §5 plus one reconciliation): a rejected reply deletes `request.json` so thegrid can file a fresh one at the next qualifying birth — otherwise a permanently bad request would block the Odin path forever, since thegrid only writes a request when none exists. A mind timeout leaves the request in place and retries after `retry_after_s`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_odin.py
import json
from pathlib import Path

import pytest

from grid.natsio import BusClosed, BusTimeout
from grid.odin import Relay

ISA = [{"name": n, "cost": 0.1, "effect": n} for n in ("nop", "harvest", "copy", "fork")]

# thegrid's OdinMutator writes `parent: parent.telemetry()`; these are exactly
# the keys Organism.telemetry() returns, and it carries no epoch or tick.
PARENT = {
    "age": 412, "energy": 90.0, "generation": 7, "births": 3, "harvested": 121.5,
    "tasks_solved": {"nand": 2}, "genome_length": 3,
    "signals_sent": 4, "signals_heard": 6, "signal_guided_moves": 1,
    "post_signal_harvested": 3.25, "structures_built": 0, "neighbor_reads": 2,
    "foreign_copies": 0, "moves": 88, "scans": 12, "guided_moves": 5,
    "post_move_harvested": 9.5, "scratch_nonzero": 1, "forecast_attempts": 2,
    "forecasts_solved": 1, "weather_cues_seen": 3, "weather_cue_signals": 1,
    "experimental_ops": {}, "salvaged": 0.0, "stolen": 0.0, "robbed": 0.0,
    "corruptions": 0, "corrupted": 0,
}


def write_request(queue: Path, request_id="r1") -> Path:
    queue.mkdir(parents=True, exist_ok=True)
    p = queue / "request.json"
    p.write_text(json.dumps({"request_id": request_id, "created_at": 1.0,
                             "parent": dict(PARENT),
                             "genome": ["harvest", "copy", "fork"], "instruction_set": ISA,
                             "ecology": {}, "directive": "author one variant"}))
    return p


async def state_of(epoch: int, tick: int) -> dict:
    return {"epoch": epoch, "tick": tick}


class FakeMind:
    agent_id = "grid-c"

    def __init__(self, answer=None, exc=None):
        self.answer, self.exc, self.asked, self.recalled = answer, exc, [], []
        self.remembered: list[tuple[str, list[str]]] = []

    def remember(self, text, tags, *, category="grid"):
        self.remembered.append((text, tags))
        return True

    async def recall(self, query, top_k=5):
        self.recalled.append(query)
        return ["last time fork-early won"]

    async def ask(self, text, *, recall_query=None, timeout_s=180.0):
        self.asked.append(text)
        if self.exc:
            raise self.exc
        return self.answer


class FakeBus:
    def __init__(self):
        self.published = []

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def relay(tmp_path, mind, bus=None, clock=None, state_fetch=None) -> Relay:
    return Relay(tmp_path, colony="c", agent_id="grid-c", mind=mind, bus=bus or FakeBus(),
                 clock=clock or Clock(), state_fetch=state_fetch)


async def test_no_request_is_a_noop(tmp_path):
    assert await relay(tmp_path, FakeMind()).poll_once() is None


async def test_valid_reply_writes_proposal_and_publishes_proposed(tmp_path):
    write_request(tmp_path)
    bus = FakeBus()
    mind = FakeMind(answer='["harvest","harvest","copy","fork"]')
    r = relay(tmp_path, mind, bus, state_fetch=lambda: state_of(2, 321))
    assert await r.poll_once() == "proposed"
    assert json.loads((tmp_path / "proposal.json").read_text()) == {"genome": ["harvest", "harvest", "copy", "fork"]}
    assert "fork-early won" in mind.asked[0]                       # recall reached the prompt
    subj, payload = bus.published[0]
    assert subj == "GRID.c.event.odin.proposed"
    assert payload["request_id"] == "r1" and payload["genome_len"] == 4 and payload["mind"] == "grid-c"
    assert payload["epoch"] == 2 and payload["tick"] == 321


async def test_pending_proposal_is_left_alone(tmp_path):
    write_request(tmp_path)
    mind = FakeMind(answer='["harvest","fork"]')
    r = relay(tmp_path, mind)
    await r.poll_once()
    assert await r.poll_once() is None
    assert len(mind.asked) == 1


async def test_rejected_reply_deletes_request_and_publishes_rejected(tmp_path):
    write_request(tmp_path)
    bus = FakeBus()
    r = relay(tmp_path, FakeMind(answer='["teleport"]'), bus)
    assert await r.poll_once() == "rejected"
    assert not (tmp_path / "request.json").exists()
    assert not (tmp_path / "proposal.json").exists()
    assert bus.published[0][0] == "GRID.c.event.odin.rejected"
    assert "teleport" in bus.published[0][1]["reason"]


async def test_mind_timeout_keeps_request_and_retries_after_backoff(tmp_path):
    write_request(tmp_path)
    clock = Clock()
    mind = FakeMind(exc=BusTimeout("slow"))
    r = relay(tmp_path, mind, clock=clock)
    assert await r.poll_once() == "deferred"
    assert (tmp_path / "request.json").exists()
    assert await r.poll_once() is None            # inside backoff: no second ask
    assert len(mind.asked) == 1
    clock.t = 61.0
    assert await r.poll_once() == "deferred"
    assert len(mind.asked) == 2


async def test_replaced_request_is_asked_again(tmp_path):
    write_request(tmp_path, "r1")
    mind = FakeMind(answer='["harvest","fork"]')
    r = relay(tmp_path, mind)
    await r.poll_once()
    (tmp_path / "proposal.json").unlink()          # thegrid consumed it
    write_request(tmp_path, "r2")
    assert await r.poll_once() == "proposed"
    assert len(mind.asked) == 2


class ClosedBus(FakeBus):
    """The client gave up reconnecting; nothing will be delivered again."""

    async def publish(self, subject, payload):
        raise BusClosed("connection closed for good")


async def test_closed_bus_is_fatal_for_the_relay(tmp_path):
    write_request(tmp_path)
    r = relay(tmp_path, FakeMind(answer='["harvest","fork"]'), ClosedBus())
    with pytest.raises(BusClosed):
        await r.poll_once()


async def test_a_rejection_is_remembered_and_a_proposal_is_not(tmp_path):
    write_request(tmp_path)
    mind = FakeMind(answer='["teleport"]')
    assert await relay(tmp_path, mind).poll_once() == "rejected"
    assert len(mind.remembered) == 1
    text, tags = mind.remembered[0]
    assert text.startswith("Odin rejected a proposal for request r1:")
    assert "teleport" in text and "parent genome" in text
    assert tags == ["c", "odin.rejected"]

    write_request(tmp_path, "r2")
    good = FakeMind(answer='["harvest","fork"]')
    assert await relay(tmp_path, good).poll_once() == "proposed"
    assert good.remembered == []


async def test_a_stale_proposal_is_renamed_and_counted(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "proposal.json").write_text(json.dumps({"genome": ["harvest"]}))
    mind = FakeMind(answer='["harvest","fork"]')
    r = relay(tmp_path, mind)
    assert await r.poll_once() is None              # nothing to answer yet
    assert not (tmp_path / "proposal.json").exists()
    assert [p.name for p in tmp_path.glob("proposal.stale-*.json")]
    assert r.stale_count == 1

    write_request(tmp_path)                          # the next request is asked normally
    assert await r.poll_once() == "proposed"
    assert len(mind.asked) == 1


def test_config_from_env_carries_the_mind_data_dir_and_binary(monkeypatch):
    from grid.odin import RelayConfig
    for k in ("ODIN_QUEUE", "MIND_AGENT_ID", "NATS_URL", "NATS_USER", "NATS_PASSWORD",
              "MIND_DATA_DIR", "KANNAKA_BIN", "ODIN_POLL_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    assert RelayConfig.from_env().mind_data_dir is None
    assert RelayConfig.from_env().kannaka_bin == "kannaka"
    monkeypatch.setenv("MIND_DATA_DIR", "/srv/grid/colony-one/memory")
    monkeypatch.setenv("KANNAKA_BIN", "/home/nick/.local/bin/kannaka")
    c = RelayConfig.from_env()
    assert c.mind_data_dir == "/srv/grid/colony-one/memory"
    assert c.kannaka_bin == "/home/nick/.local/bin/kannaka"

async def test_epoch_and_tick_fall_back_to_zero_when_the_colony_is_unreachable(tmp_path):
    write_request(tmp_path)
    bus = FakeBus()

    async def broken():
        raise RuntimeError("connection refused")

    r = relay(tmp_path, FakeMind(answer='["harvest","fork"]'), bus, state_fetch=broken)
    assert await r.poll_once() == "proposed"
    payload = bus.published[0][1]
    assert payload["epoch"] == 0 and payload["tick"] == 0


async def test_an_unparseable_request_is_binned_after_three_polls(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    request = tmp_path / "request.json"
    request.write_text("{not json at all")
    mind = FakeMind(answer='["harvest","fork"]')
    r = relay(tmp_path, mind, state_fetch=lambda: state_of(3, 90))
    assert await r.poll_once() is None
    assert await r.poll_once() is None
    assert request.exists()                    # two strikes leave it for thegrid to fix
    assert await r.poll_once() == "rejected"
    assert not request.exists()
    subj, payload = r.bus.published[0]
    assert subj == "GRID.c.event.odin.rejected"
    assert payload["reason"] == "unparseable request"
    assert payload["epoch"] == 3 and payload["tick"] == 90
    assert not mind.asked


async def test_a_good_parse_resets_the_unparseable_count(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    request = tmp_path / "request.json"
    request.write_text("{not json at all")
    mind = FakeMind(answer='["harvest","fork"]')
    r = relay(tmp_path, mind)
    assert await r.poll_once() is None
    assert await r.poll_once() is None
    write_request(tmp_path)
    assert await r.poll_once() == "proposed"
    (tmp_path / "proposal.json").unlink()
    request.write_text("{not json at all")
    assert await r.poll_once() is None          # back to strike one, not binned
    assert request.exists()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_odin.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.odin'`

- [ ] **Step 3: Implement**

```python
# grid/odin.py
"""The Odin relay: thegrid's mutation request → the colony mind → a proposal.

Polls the queue directory once a second. inotify would be tighter, but a
request is consumed only at a later birth, so a second of latency is nothing
and one fewer dependency is something.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from .envelope import envelope, subject
from .mind import ASK_TIMEOUT_S, REMEMBER_MIN_INTERVAL_S, Mind, MindError, Throttle
from .natsio import Bus, BusClosed, BusError, BusTimeout
from .proposal import Rejection, parse_request, render_prompt, validate, write_proposal

log = logging.getLogger("grid.odin")

UNPARSEABLE_LIMIT = 3       # consecutive failed parses before the file is binned


class Relay:
    def __init__(self, queue: Path, *, colony: str, agent_id: str, mind, bus,
                 timeout_s: float = ASK_TIMEOUT_S, retry_after_s: float = 60.0,
                 clock=time.monotonic, throttle: Throttle | None = None,
                 state_fetch: Callable[[], Awaitable[dict]] | None = None):
        self.queue = Path(queue)
        self.colony = colony
        self.agent_id = agent_id
        self.mind = mind
        self.bus = bus
        self.timeout_s = timeout_s
        self.retry_after_s = retry_after_s
        self._clock = clock
        self.state_fetch = state_fetch
        self.throttle = throttle or Throttle(REMEMBER_MIN_INTERVAL_S, clock=clock)
        self.stale_count = 0
        self._retry_not_before: dict[str, float] = {}
        self._unparseable: dict[str, int] = {}

    async def _publish(self, kind: str, epoch: int, tick: int, **fields) -> None:
        try:
            await self.bus.publish(subject(self.colony, "event", f"odin.{kind}"),
                                   envelope(agent_id=self.agent_id, colony=self.colony,
                                            epoch=epoch, tick=tick, kind=f"odin.{kind}", **fields))
        except BusClosed:
            raise
        except BusError as e:
            log.error("odin.%s publish failed: %s", kind, e)

    async def _epoch_tick(self) -> tuple[int, int]:
        """thegrid's Organism.telemetry() carries no epoch or tick; /api/state does."""
        if self.state_fetch is None:
            return 0, 0
        try:
            state = await self.state_fetch()
            return int(state.get("epoch", 0) or 0), int(state.get("tick", 0) or 0)
        except Exception as e:      # an envelope with 0/0 beats no envelope at all
            log.warning("epoch/tick unavailable from the colony: %s", e)
            return 0, 0

    async def poll_once(self) -> str | None:
        request_path = self.queue / "request.json"
        proposal_path = self.queue / "proposal.json"
        if proposal_path.exists():
            if request_path.exists():
                return None                     # thegrid has not consumed it yet
            # The request is gone, so thegrid will never read this proposal.
            stale = self.queue / f"proposal.stale-{int(time.time())}.json"
            proposal_path.rename(stale)
            self.stale_count += 1
            log.warning("stale proposal with no request: renamed to %s", stale.name)
        if not request_path.exists():
            return None
        key = str(request_path)
        try:
            req = parse_request(request_path)
        except ValueError as e:
            seen = self._unparseable[key] = self._unparseable.get(key, 0) + 1
            log.error("unreadable request (%d/%d): %s", seen, UNPARSEABLE_LIMIT, e)
            if seen < UNPARSEABLE_LIMIT:
                return None
            # thegrid writes a request only when none exists, so leaving a file
            # nobody can read here would block the Odin path forever.
            request_path.unlink(missing_ok=True)
            del self._unparseable[key]
            epoch, tick = await self._epoch_tick()
            await self._publish("rejected", epoch, tick, request_id=None,
                                reason="unparseable request")
            return "rejected"
        self._unparseable.pop(key, None)        # consecutive, so a good parse resets
        if self._clock() < self._retry_not_before.get(req.request_id, 0.0):
            return None

        recall = await self.mind.recall(f"colony {self.colony} genome lineage {' '.join(req.genome[:6])}")
        prompt = render_prompt(req, recall)
        try:
            reply = await self.mind.ask(prompt, recall_query=f"colony {self.colony}", timeout_s=self.timeout_s)
        except BusClosed:
            raise
        except (BusTimeout, BusError, MindError) as e:
            log.warning("mind unavailable for %s: %s; retry in %.0fs", req.request_id, e, self.retry_after_s)
            self._retry_not_before[req.request_id] = self._clock() + self.retry_after_s
            return "deferred"

        result = validate(reply, req)
        epoch, tick = await self._epoch_tick()
        if isinstance(result, Rejection):
            log.info("rejected %s: %s", req.request_id, result.reason)
            request_path.unlink(missing_ok=True)   # let thegrid file a fresh request
            await self._publish("rejected", epoch, tick, request_id=req.request_id, reason=result.reason)
            if self.throttle.allow("odin.rejected"):
                # Spec 6: the mind learns what does not work, not only what does.
                await asyncio.to_thread(
                    self.mind.remember,
                    f"Odin rejected a proposal for request {req.request_id}: "
                    f"{result.reason} (parent genome {req.genome[:8]}...)",
                    [self.colony, "odin.rejected"])
            return "rejected"
        if not write_proposal(self.queue, req, result):
            log.info("request %s vanished before the proposal landed", req.request_id)
            return None
        await self._publish("proposed", epoch, tick, request_id=req.request_id,
                            genome_len=len(result), mind=self.agent_id)
        return "proposed"


@dataclass(frozen=True)
class RelayConfig:
    queue: Path
    colony: str
    colony_url: str
    agent_id: str
    nats_url: str
    nats_user: str | None
    nats_password: str | None
    mind_data_dir: str | None
    kannaka_bin: str
    poll_s: float

    @classmethod
    def from_env(cls) -> "RelayConfig":
        env = os.environ.get
        colony = env("COLONY_SLUG")
        if not colony:
            raise SystemExit("COLONY_SLUG is required (the slug of thegrid's --name)")
        return cls(
            queue=Path(env("ODIN_QUEUE", f"/srv/grid/{colony}/state/operator")),
            colony=colony,
            colony_url=env("COLONY_URL", "http://127.0.0.1:8787").rstrip("/"),
            agent_id=env("MIND_AGENT_ID", f"grid-{colony}"),
            nats_url=env("NATS_URL", "nats://swarm.ninja-portal.com:4222"),
            nats_user=env("NATS_USER") or None,
            nats_password=env("NATS_PASSWORD") or None,
            mind_data_dir=env("MIND_DATA_DIR") or None,
            kannaka_bin=env("KANNAKA_BIN", "kannaka"),
            poll_s=float(env("ODIN_POLL_S", "1")),
        )


async def run(cfg: RelayConfig) -> None:
    bus = Bus(cfg.nats_url, user=cfg.nats_user, password=cfg.nats_password)
    backoff = 1.0
    while True:
        try:
            await bus.connect()
            break
        except Exception as e:      # a boot-time DNS or NATS blip must not burn a start slot
            log.error("nats connect failed: %s; retrying in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    mind = Mind(cfg.agent_id, bus, kannaka_bin=cfg.kannaka_bin,
                data_dir=Path(cfg.mind_data_dir) if cfg.mind_data_dir else None)
    async with httpx.AsyncClient(timeout=2) as client:
        async def fetch_state() -> dict:
            r = await client.get(f"{cfg.colony_url}/api/state")
            r.raise_for_status()
            return r.json()

        relay = Relay(cfg.queue, colony=cfg.colony, agent_id=cfg.agent_id, mind=mind, bus=bus,
                      state_fetch=fetch_state)
        log.info("watching %s for %s", cfg.queue, cfg.agent_id)
        while True:
            try:
                outcome = await relay.poll_once()
                if outcome:
                    log.info("%s", outcome)
            except BusClosed as e:  # EX_TEMPFAIL: only a restart gets a live client back
                log.error("nats gave up: %s; exiting for systemd to restart", e)
                raise SystemExit(75) from e
            except Exception as e:  # one bad poll must not kill the relay
                log.exception("poll failed: %s", e)
            await asyncio.sleep(cfg.poll_s)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run(RelayConfig.from_env()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_odin.py -q`
Expected: `13 passed`

- [ ] **Step 5: Mutation checks, then commit**

1. Remove `request_path.unlink(missing_ok=True)` in the rejection branch: `test_rejected_reply_deletes_request_and_publishes_rejected` fails.
2. Remove the `_retry_not_before` check: `test_mind_timeout_keeps_request_and_retries_after_backoff` fails on "no second ask".
Revert both.

```bash
git add grid/odin.py tests/test_odin.py
git commit -m "feat: Odin relay — request to mind to validated atomic proposal"
```

---

### Task 11: Colony launcher with heat proxy

**Files:**
- Create: `grid/launcher.py`
- Test: `tests/test_launcher.py`

**Interfaces:**
- Produces: `heat_proxy_from_cpu(sample: Callable[[], float]) -> Callable[[], float]` (maps a 0..1 CPU fraction to °C: `45 + 40 * fraction`), `install_heat_proxy(substrate_module, cpu_fraction=None) -> str` (returns `"k10temp"` when the real sensor works, else `"proxy"` after replacing `substrate_module.cpu_temperature_c` with a whole-host `/proc/stat` sampler), `_host_cpu_fraction(*, stat_path=Path("/proc/stat"), clock=time.monotonic)`, `main(argv)`.

Why: thegrid's `SubstrateWorld.__init__` calls `cpu_temperature_c()`, which raises `RuntimeError` when no `k10temp` hwmon exists (`thegrid/src/colony/substrate.py:31-47`), and `live.main()` hard-codes `physical=True`. A Proxmox VM has no such sensor. The launcher keeps memory and CPU accounting on real cgroup readings and supplies only the temperature, from the service's own cgroup CPU usage, when the sensor is absent. This is the one item to raise upstream with EckmanTechLLC (a `--heat simulated` flag would remove the launcher).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_launcher.py
import types
from pathlib import Path

from grid.launcher import _host_cpu_fraction, heat_proxy_from_cpu, install_heat_proxy


def test_heat_proxy_maps_cpu_fraction_to_celsius():
    proxy = heat_proxy_from_cpu(lambda: 0.0)
    assert proxy() == 45.0
    assert heat_proxy_from_cpu(lambda: 1.0)() == 85.0
    assert heat_proxy_from_cpu(lambda: 2.0)() == 85.0     # clamped


def test_install_leaves_real_sensor_alone():
    mod = types.SimpleNamespace(cpu_temperature_c=lambda: 51.0)
    assert install_heat_proxy(mod) == "k10temp"
    assert mod.cpu_temperature_c() == 51.0


def test_install_replaces_missing_sensor_with_proxy():
    def absent():
        raise RuntimeError("AMD k10temp/Tctl sensor is unavailable")
    mod = types.SimpleNamespace(cpu_temperature_c=absent)
    assert install_heat_proxy(mod, cpu_fraction=lambda: 0.5) == "proxy"
    assert mod.cpu_temperature_c() == 65.0


def write_stat(path: Path, busy: int, idle: int) -> None:
    """A /proc/stat-shaped file: user nice system idle iowait irq softirq steal."""
    lines = [f"cpu  {busy} 0 0 {idle} 0 0 0 0",
             f"cpu0 {busy} 0 0 {idle} 0 0 0 0",
             "intr 12345 0 0"]
    path.write_text("\n".join(lines) + "\n")


def test_host_sampler_seeds_its_baseline_so_the_first_sample_is_not_full_heat(tmp_path):
    stat = tmp_path / "proc-stat"
    write_stat(stat, busy=1000, idle=9000)
    sample = _host_cpu_fraction(stat_path=stat)
    assert sample() == 0.0                       # nothing moved since construction

    write_stat(stat, busy=1500, idle=9500)       # 500 of 1000 new jiffies were busy
    assert sample() == 0.5

    proxy = heat_proxy_from_cpu(sample)
    assert proxy() == 65.0                       # /proc/stat still: hold the last fraction

    write_stat(stat, busy=2500, idle=9500)       # every one of 1000 new jiffies was busy
    assert proxy() == 85.0


def test_host_sampler_never_raises_when_proc_stat_is_missing(tmp_path):
    sample = _host_cpu_fraction(stat_path=tmp_path / "absent")
    assert sample() == 0.0
    assert heat_proxy_from_cpu(sample)() == 45.0
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_launcher.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid.launcher'`

- [ ] **Step 3: Implement**

```python
# grid/launcher.py
"""Run thegrid's live colony as a library, with a heat proxy when the host
has no AMD k10temp sensor.

thegrid is imported from THEGRID_DIR (default: the submodule beside this
package, or /srv/grid/thegrid on Skywave). Nothing under it is modified; the
only intervention is replacing `substrate.cpu_temperature_c` when the real
one raises. Memory pressure and CPU usage remain real cgroup readings.

The proxy reads the WHOLE HOST, not the colony's cgroup. thegrid's substrate
fires a storm when its fast heat EMA rises 1.5 C over a slow baseline, and the
colony is capped at CPUQuota=5%: its own work could never move a cgroup-derived
number that far, so `storm` and `thermal.throttle` could never fire. Reading
the host means the mind's own LLM calls, the KAX machines, and any other load
on the box are the colony's weather -- which is exactly what thegrid's
substrate intends by "the weather is Odin itself".
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable

BASELINE_C = 45.0
SPAN_C = 40.0


def heat_proxy_from_cpu(sample: Callable[[], float]) -> Callable[[], float]:
    def proxy() -> float:
        fraction = min(1.0, max(0.0, float(sample())))
        return BASELINE_C + SPAN_C * fraction
    return proxy


def _host_cpu_fraction(*, stat_path: Path = Path("/proc/stat"),
                       clock: Callable[[], float] = time.monotonic) -> Callable[[], float]:
    """Fraction of the whole machine's CPU that was busy since the previous sample.

    Never raises: the proxy exists to keep the colony alive, so a missing or
    unreadable /proc/stat reads as "no change since last time".
    """
    def read_totals() -> tuple[int, int] | None:
        """(busy, total) jiffies across every CPU, or None if unreadable."""
        try:
            for line in stat_path.read_text().splitlines():
                if line.startswith("cpu "):
                    # user nice system idle iowait irq softirq steal [guest...]
                    fields = [int(v) for v in line.split()[1:]]
                    total = sum(fields)
                    return total - fields[3] - fields[4], total
        except (OSError, ValueError, IndexError):
            return None
        return None

    last = [read_totals()]          # seeded now, so the first sample is a delta
    fraction = [0.0]

    def sample() -> float:
        current = read_totals()
        if current is None:
            return fraction[0]
        previous, last[0] = last[0], current
        if previous is None:
            return fraction[0]
        busy_delta, total_delta = current[0] - previous[0], current[1] - previous[1]
        if total_delta > 0:
            fraction[0] = min(1.0, max(0.0, busy_delta / total_delta))
        return fraction[0]
    return sample


def install_heat_proxy(substrate_module, cpu_fraction: Callable[[], float] | None = None) -> str:
    try:
        substrate_module.cpu_temperature_c()
        return "k10temp"
    except RuntimeError:
        substrate_module.cpu_temperature_c = heat_proxy_from_cpu(cpu_fraction or _host_cpu_fraction())
        return "proxy"


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    thegrid_dir = Path(os.environ.get("THEGRID_DIR", Path(__file__).resolve().parent.parent / "thegrid"))
    sys.path.insert(0, str(thegrid_dir))
    from src.colony import live, substrate  # noqa: E402  (thegrid, imported as a library)
    source = install_heat_proxy(substrate)
    print(f"[grid.launcher] thegrid at {thegrid_dir}; heat source: {source}", file=sys.stderr, flush=True)
    sys.argv = ["thegrid-colony", *argv]
    live.main()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_launcher.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add grid/launcher.py tests/test_launcher.py
git commit -m "feat: colony launcher with a whole-host heat proxy when k10temp is absent"
```

---

### Task 12: Deploy units, env examples, runbook

**Files:**
- Create: `deploy/systemd/thegrid-colony.service`, `deploy/systemd/grid-bridge.service`, `deploy/systemd/grid-odin.service`, `deploy/systemd/grid-mind.service`, `deploy/systemd/grid-mind-dream.service`, `deploy/systemd/grid-mind-dream.timer`
- Create: `deploy/grid.env.example`, `deploy/mind-config.toml.example`, `deploy/README.md`
- Test: `tests/test_deploy_units.py`

**Interfaces:**
- Consumes: entry points `python -m grid.bridge`, `python -m grid.odin`, `python -m grid.launcher` (Tasks 9–11).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_units.py
from pathlib import Path

UNITS = Path(__file__).resolve().parent.parent / "deploy" / "systemd"


def unit(name: str) -> str:
    return (UNITS / name).read_text()


def test_all_units_exist_and_restart():
    for name in ("thegrid-colony.service", "grid-bridge.service", "grid-odin.service", "grid-mind.service"):
        text = unit(name)
        assert "Restart=always" in text, name
        assert "ExecStart=" in text, name
        assert "StartLimitBurst=" in text, name


def test_colony_binds_localhost_and_is_bounded():
    text = unit("thegrid-colony.service")
    assert "--host 127.0.0.1" in text
    assert "--mutator odin" in text
    assert "--state /srv/grid/colony-one/state/colony.pkl" in text
    assert "MemoryMax=192M" in text and "MemorySwapMax=0" in text and "CPUQuota=5%" in text
    assert "grid.launcher" in text


def test_mind_is_readonly_daemon_with_own_data_dir():
    text = unit("grid-mind.service")
    assert "KANNAKA_READONLY=1" in text
    assert "KANNAKA_DATA_DIR=/srv/grid/colony-one/memory" in text
    assert "swarm serve" in text


def test_dream_timer_restarts_the_mind_so_new_memories_load():
    assert "OnUnitActiveSec=6h" in unit("grid-mind-dream.timer")
    dream = unit("grid-mind-dream.service")
    assert "kannaka dream" in dream and "systemctl restart grid-mind.service" in dream


def test_bridge_and_relay_read_the_shared_env():
    for name in ("grid-bridge.service", "grid-odin.service"):
        assert "EnvironmentFile=/srv/grid/grid.env" in unit(name), name
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_deploy_units.py -q`
Expected: FAIL — `FileNotFoundError` for the unit files.

- [ ] **Step 3: Write the units, env examples, and runbook**

```ini
# deploy/systemd/thegrid-colony.service
[Unit]
Description=thegrid colony-one (kannaka-grid launcher, heat proxy if no k10temp)
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=nick
Group=nick
WorkingDirectory=/srv/grid/kannaka-grid
Environment=PYTHONUNBUFFERED=1
Environment=THEGRID_DIR=/srv/grid/thegrid
ExecStart=/srv/grid/venv/bin/python -m grid.launcher --host 127.0.0.1 --port 8787 --ticks-per-second 10 --mutator odin --name colony-one --state /srv/grid/colony-one/state/colony.pkl
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/srv/grid/colony-one/state
MemoryMax=192M
MemoryHigh=176M
MemorySwapMax=0
CPUQuota=5%

[Install]
WantedBy=multi-user.target
```

```ini
# deploy/systemd/grid-bridge.service
[Unit]
Description=kannaka-grid bridge (colony stream -> GRID.* events, state, chronicle)
After=network-online.target thegrid-colony.service
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=nick
Group=nick
WorkingDirectory=/srv/grid/kannaka-grid
EnvironmentFile=/srv/grid/grid.env
EnvironmentFile=/home/nick/.kannaka-nats.env
ExecStart=/srv/grid/venv/bin/python -m grid.bridge
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```ini
# deploy/systemd/grid-odin.service
[Unit]
Description=kannaka-grid Odin relay (mutation requests -> colony mind -> proposals)
After=network-online.target thegrid-colony.service grid-mind.service
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=nick
Group=nick
WorkingDirectory=/srv/grid/kannaka-grid
EnvironmentFile=/srv/grid/grid.env
EnvironmentFile=/home/nick/.kannaka-nats.env
ExecStart=/srv/grid/venv/bin/python -m grid.odin
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```ini
# deploy/systemd/grid-mind.service
[Unit]
Description=kannaka-grid colony mind (kannaka swarm serve, read-only in RAM)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=nick
Group=nick
Environment=KANNAKA_DATA_DIR=/srv/grid/colony-one/memory
Environment=KANNAKA_READONLY=1
EnvironmentFile=/home/nick/.kannaka-nats.env
ExecStart=/home/nick/.local/bin/kannaka swarm serve
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```ini
# deploy/systemd/grid-mind-dream.service
[Unit]
Description=kannaka-grid mind: dream, then restart the serve daemon so it loads new memories

[Service]
Type=oneshot
User=nick
Group=nick
Environment=KANNAKA_DATA_DIR=/srv/grid/colony-one/memory
ExecStart=/home/nick/.local/bin/kannaka dream
ExecStartPost=/usr/bin/sudo -n /usr/bin/systemctl restart grid-mind.service
```

```ini
# deploy/systemd/grid-mind-dream.timer
[Unit]
Description=Dream the colony mind every six hours

[Timer]
OnBootSec=30m
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
# deploy/grid.env.example  -> /srv/grid/grid.env (0600, owner nick)
COLONY_URL=http://127.0.0.1:8787
COLONY_SLUG=colony-one
MIND_AGENT_ID=grid-colony-one
MIND_DATA_DIR=/srv/grid/colony-one/memory
ODIN_QUEUE=/srv/grid/colony-one/state/operator
KANNAKA_BIN=/home/nick/.local/bin/kannaka
NATS_URL=nats://swarm.ninja-portal.com:4222
# NATS_USER / NATS_PASSWORD come from /home/nick/.kannaka-nats.env
# FLUX_URL=http://<flux host>:3000     # unset = skip Flux
STATE_EVERY_S=60
HISTORY_EVERY_S=60
```

```toml
# deploy/mind-config.toml.example -> /srv/grid/colony-one/memory/config.toml
# Generated by `kannaka init` under KANNAKA_DATA_DIR; then edit these blocks.
[agent]
id = "grid-colony-one"
display_name = "Grid: colony-one"
kind = "agent"
persona = "The mind of an artificial-life colony. You remember its epochs, storms, lineages and the genomes that worked. You answer briefly and cite ticks and opcodes."

[llm]
provider = "openai"                      # kannaka's OpenAI-compatible client
base_url = "http://127.0.0.1:4000/v1"    # LiteLLM gateway on skywave
model = "agent-brain"                    # gateway alias -> anthropic haiku
api_key = "<virtual key minted for grid-colony-one; never commit>"

[swarm]
enabled = true
nats_url = "nats://swarm.ninja-portal.com:4222"
role = "worker"
```

`deploy/README.md` — the Skywave runbook. Write it in full:

````markdown
# Skywave runbook — kannaka-grid v0.1

Host facts: `NickFlach/kax-computer` `docs/DEPLOY.md` (WARP on, `ssh debain1`, user `nick`, no backups).

## 0. Prerequisites (once)
```bash
sudo install -d -o nick -g nick /srv/grid
cd /srv/grid && git clone --recurse-submodules https://github.com/NickFlach/kannaka-grid.git
cd /srv/grid/kannaka-grid
ln -s /srv/grid/kannaka-grid/thegrid /srv/grid/thegrid
python3 -m venv /srv/grid/venv && /srv/grid/venv/bin/pip install -e "/srv/grid/kannaka-grid[dev]" -e /srv/grid/thegrid
install -d -o nick -g nick /srv/grid/colony-one/state /srv/grid/colony-one/state/operator /srv/grid/colony-one/memory
cp deploy/grid.env.example /srv/grid/grid.env && chmod 600 /srv/grid/grid.env
```
Receipt: `/srv/grid/venv/bin/python -c "import grid, src.colony.live"` prints nothing.

## 1. Mind
All commands run from `/srv/grid/kannaka-grid`.
```bash
KANNAKA_DATA_DIR=/srv/grid/colony-one/memory kannaka init      # scaffold
# mint a LiteLLM virtual key the same way create_machine.py does (budget 0.5/30d, model agent-brain)
# then edit /srv/grid/colony-one/memory/config.toml per deploy/mind-config.toml.example
KANNAKA_DATA_DIR=/srv/grid/colony-one/memory kannaka remember "grid-colony-one mind initialised" --category grid
sudo cp deploy/systemd/grid-mind*.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now grid-mind.service grid-mind-dream.timer
```
Sudoers line for the dream restart: `nick ALL=(root) NOPASSWD: /usr/bin/systemctl restart grid-mind.service`.
Receipt: `kannaka swarm peers` lists `grid-colony-one` with `ask` in capabilities.

## 2. Colony
All commands run from `/srv/grid/kannaka-grid`.
```bash
sudo cp deploy/systemd/thegrid-colony.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now thegrid-colony.service
journalctl -u thegrid-colony -n 5     # expect "heat source: proxy" on this VM
curl -s 127.0.0.1:8787/api/state | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['name'], s['epoch'], s['tick'], s['population'])"
```
Receipt: name `colony-one`, tick increasing between two calls.

## 3. Bridge and relay
All commands run from `/srv/grid/kannaka-grid`.
```bash
sudo cp deploy/systemd/grid-bridge.service deploy/systemd/grid-odin.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now grid-bridge.service grid-odin.service
```
Receipts (acceptance criteria, spec §12):
1. `journalctl -u grid-bridge -f` shows no `dropped` lines; from the operator seat, `kannaka swarm tail --subject 'GRID.>'` (or the nats app's capture) shows `GRID.colony-one.event.epoch.started` then `.state` every 60 s.
2. Storms fire when host load rises (≈ +4 % of all CPUs sustained lifts the proxy ≥ 1.5 °C). Force one: `timeout 30 python3 -c 'while True: pass'` in two shells, then within ~1000 ticks `GRID.colony-one.event.storm` appears.
3. `KANNAKA_DATA_DIR=/srv/grid/colony-one/memory kannaka status` shows memory count rising; `kannaka recall "storm"` against that dir returns a colony fact.
4. `ls /srv/grid/colony-one/state/operator` shows `request.json` then `proposal.json`; both vanish from the operator dir after a later birth (thegrid consumes them together); `journalctl -u grid-odin` shows `proposed`; `curl -s 127.0.0.1:8787/api/history | python3 -c "import json,sys; print(json.load(sys.stdin)['mutationMechanisms'])"` lists the mutation types thegrid recorded for the epoch.
5. After tick 2000, three `GRID.colony-one.chronicle.*` messages appear.
6. `sudo systemctl stop grid-bridge grid-odin grid-mind`; the colony's tick rate in `/api/state` is unchanged over 60 s. Start them again.

Paste the captured payloads for 1, 2, 4 and 5 into the PR that closes the deploy.

## Known limits (v0.1)
- Memories written by the bridge become visible to asks only after the 6 h dream+restart.
- `GRID.*` is unsigned on the wire (spec §8).
- Flux body carries both key styles; confirm against the live Flux and trim (Task 7 note).
- No backups on Skywave: `/srv/grid/colony-one/state` is lab-grade.
````

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_deploy_units.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add deploy tests/test_deploy_units.py
git commit -m "deploy: systemd units, env and mind config examples, Skywave runbook"
```

---

### Task 13: Integration smoke against a real colony (Linux, opt-in)

**Files:**
- Create: `tests/integration/__init__.py`, `tests/integration/test_smoke.py`

**Interfaces:**
- Consumes: `grid.launcher` (Task 11), `Distiller`/`Snapshot` (Task 3), `Relay` (Task 10), `iter_sse_data` (Task 9).

Runs only when `GRID_INTEGRATION=1` and on Linux (thegrid reads cgroup v2 files; outside a systemd slice `memory.max` is often `max`, which thegrid refuses, so the test also skips unless `/sys/fs/cgroup/<self>/memory.max` is finite — run it inside `systemd-run --user -p MemoryMax=256M --pty ...` on Skywave, or in CI under a cgroup-limited container).

- [ ] **Step 1: Write the test (it is both the failing test and the deliverable)**

```python
# tests/integration/__init__.py
```

```python
# tests/integration/test_smoke.py
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from grid.bridge import iter_sse_data
from grid.distill import Distiller, Snapshot
from grid.odin import Relay

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
THEGRID = ROOT / "thegrid"


def _finite_memory_max() -> bool:
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                text = (Path("/sys/fs/cgroup") / line[3:].lstrip("/") / "memory.max").read_text().strip()
                return text != "max"
    except OSError:
        pass
    return False


skip = pytest.mark.skipif(
    os.environ.get("GRID_INTEGRATION") != "1" or sys.platform != "linux" or not _finite_memory_max(),
    reason="set GRID_INTEGRATION=1 on Linux inside a cgroup with finite memory.max, "
           "with thegrid's aiohttp dependency installed in the venv (pip install -e thegrid)")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def colony(tmp_path):
    port = free_port()
    state = tmp_path / "state" / "colony.pkl"
    state.parent.mkdir(parents=True)
    env = dict(os.environ, THEGRID_DIR=str(THEGRID), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, "-m", "grid.launcher", "--host", "127.0.0.1", "--port", str(port),
                             "--ticks-per-second", "50", "--mutator", "odin", "--name", "smoke",
                             "--state", str(state)], env=env, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def stop() -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            # process has exited, so stderr is at EOF and this read cannot block
            if proc.poll() is not None:
                pytest.fail(f"colony exited early: {proc.stderr.read()[-2000:]}")
            try:
                if httpx.get(f"{base}/api/state", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            stop()
            pytest.fail(f"colony did not come up: {proc.stderr.read()[-2000:]}")
    except BaseException:
        stop()
        raise
    try:
        yield base, state.parent / "operator"
    finally:
        stop()


@skip
def test_stream_distills_epoch_started(colony):
    base, _ = colony
    d = Distiller()
    seen = []
    with httpx.stream("GET", f"{base}/api/stream", timeout=httpx.Timeout(10, read=15)) as r:
        for line in r.iter_lines():
            for data in iter_sse_data([line]):
                seen += d.feed(Snapshot.from_state(json.loads(data)))
                if len(seen) >= 1:
                    break
            if seen:
                break
    assert seen[0].kind == "epoch.started"


class StubMind:
    agent_id = "grid-smoke"

    async def recall(self, query, top_k=5):
        return []

    async def ask(self, text, *, recall_query=None, timeout_s=180.0):
        return '["harvest","alloc","copy","ifnotdone","jmpb","fork","nop"]'


class NullBus:
    async def publish(self, subject, payload):
        pass


@skip
async def test_relay_answers_a_request_written_by_thegrid_shape(colony):
    _, queue = colony
    queue.mkdir(exist_ok=True)
    (queue / "request.json").write_text(json.dumps({
        "request_id": "smoke1", "created_at": time.time(),
        # exactly what thegrid's OdinMutator writes: Organism.telemetry(), which
        # carries no epoch and no tick.
        "parent": {"age": 120, "energy": 90.0, "generation": 2, "births": 1,
                   "harvested": 40.0, "tasks_solved": {}, "genome_length": 6,
                   "signals_sent": 0, "signals_heard": 0, "signal_guided_moves": 0,
                   "post_signal_harvested": 0.0, "structures_built": 0,
                   "neighbor_reads": 0, "foreign_copies": 0, "moves": 31, "scans": 4,
                   "guided_moves": 0, "post_move_harvested": 0.0, "scratch_nonzero": 0,
                   "forecast_attempts": 0, "forecasts_solved": 0, "weather_cues_seen": 0,
                   "weather_cue_signals": 0, "experimental_ops": {}, "salvaged": 0.0,
                   "stolen": 0.0, "robbed": 0.0, "corruptions": 0, "corrupted": 0},
        "genome": ["harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork"],
        "instruction_set": [{"name": n, "cost": 0.1, "effect": n} for n in
                            ("nop", "harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork")],
        "ecology": {}, "directive": "author one motivated variant"}))
    relay = Relay(queue, colony="smoke", agent_id="grid-smoke", mind=StubMind(), bus=NullBus())
    assert await relay.poll_once() == "proposed"
    assert json.loads((queue / "proposal.json").read_text())["genome"][-1] == "nop"
```

- [ ] **Step 2: Run it skipped, then for real where possible**

Run: `python -m pytest tests/integration -q`
Expected: `2 skipped` on the Windows dev box.

On Skywave (after Task 12 step 0): `GRID_INTEGRATION=1 systemd-run --user --wait --pty -p MemoryMax=256M /srv/grid/venv/bin/python -m pytest /srv/grid/kannaka-grid/tests/integration -q`
Expected: `2 passed`. If the colony fails to start, the fixture prints thegrid's stderr — read it before touching anything.

- [ ] **Step 3: Commit**

```bash
git add tests/integration
git commit -m "test: opt-in integration smoke against a real short colony"
```

---

### Task 14: Full suite, spec reconciliation, push

**Files:**
- Modify: `docs/superpowers/specs/2026-09-05-kannaka-grid-foundation-design.md` (only if implementation diverged further; the reconciliation section already records the launcher, the 180 s timeout, the queue path, the delete-on-reject rule, the dream+restart, and the `signal.dialect` bucket rule).

- [ ] **Step 1: Run everything**

Run: `python -m pytest -q`
Expected: all unit tests pass, `2 skipped` (integration) on Windows.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin main   # or a feature branch + PR if main is protected by then
```

PR body lists the six acceptance receipts from `deploy/README.md` as unchecked boxes; the deploy PR that follows checks them with pasted payloads.
