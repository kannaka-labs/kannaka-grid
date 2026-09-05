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
