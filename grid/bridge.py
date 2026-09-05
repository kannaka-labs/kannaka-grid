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


# thegrid's `record.py` encodes a genome as one character per opcode index
# (`GENOME_ALPHABET[op % 64]`); pinned by a test against the submodule's source.
GENOME_ALPHABET = "0123456789abcdefghijklmnopqrstuv" + "wxyzABCDEFGHIJKLMNOPQRSTUVWXYZ@#"
FACTS_MAX_OPCODES = 32


def decode_genome(encoded: str, isa: list[str]) -> list[str]:
    """Glyph string -> opcode names, `?<char>` for anything outside the ISA."""
    names = []
    for ch in encoded:
        index = GENOME_ALPHABET.find(ch)
        names.append(isa[index] if 0 <= index < len(isa) else f"?{ch}")
    return names


def novel_opcodes(snap: Snapshot, isa: list[str]) -> list[str]:
    """Opcode names in the dominant genome that the ancestor never carried, in order."""
    ancestor = set(decode_genome(snap.ancestor, isa))
    return [n for n in dict.fromkeys(decode_genome(snap.dominant, isa)) if n not in ancestor]


def phase_facts(snap: Snapshot, isa: list[str]) -> str:
    """One line of what the colony looks like right now, in opcode names.

    Goes into every chronicle question (see chronicle.question) and into the
    mind as the phase memory. Names, not glyphs: the `skill` check needs an
    opcode the model can cite.
    """
    dominant = decode_genome(snap.dominant, isa)
    novel = novel_opcodes(snap, isa)
    return (f"Observed now at epoch {snap.epoch} tick {snap.tick}: population {snap.population}; "
            f"top lineage share {snap.lineage_top:.2f}; storms so far {snap.storms}; "
            f"heat {snap.heat:.1f}; dominant genome ({len(dominant)} opcodes): "
            f"{' '.join(dominant[:FACTS_MAX_OPCODES])}; "
            f"opcodes the ancestor lacks: {' '.join(novel) or 'none'}")


def memory_text(event: Event, *, names: dict[str, list[str]] | None = None) -> str:
    fields = " ".join(f"{k}={v}" for k, v in sorted(event.fields.items()))
    text = f"Colony event {event.kind} at epoch {event.epoch} tick {event.tick}: {fields}".strip()
    if names:
        text += " " + " ".join(f"{k}=[{' '.join(v)}]" for k, v in names.items())
    return text


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
            # Genome-shaped events carry opcode NAMES into memory, not glyphs, so
            # the chronicle's skill question has something to cite (issue #1).
            names = None
            if event.kind in ("genome.novel", "lineage.sweep") and self.last is not None and self.isa:
                names = {"dominant": decode_genome(self.last.dominant, self.isa)[:FACTS_MAX_OPCODES],
                         "novel_opcodes": novel_opcodes(self.last, self.isa)}
            # remember() shells out to `kannaka remember` with a 60 s timeout; run
            # it off the stream-reader coroutine or the whole bridge stalls on it.
            ok = await asyncio.to_thread(self.mind.remember, memory_text(event, names=names),
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
        # The observation rides in each question (the daemon cannot see a memory
        # written now) AND is remembered once per phase for later asks and dreams.
        facts = phase_facts(snap, self.isa) if self.isa else ""
        if facts:
            ok = await asyncio.to_thread(self.mind.remember, f"Colony phase {phase} summary. {facts}",
                                         [self.colony, "phase"])
            if not ok:
                log.warning("mind refused the phase %d summary", phase)
        out: dict[str, str] = {}
        for kind in chron.KINDS:
            q = chron.question(kind, colony=self.colony, epoch=snap.epoch, tick=snap.tick,
                               since_tick=since, facts=facts)
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
