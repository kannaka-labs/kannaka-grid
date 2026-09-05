import json

import pytest

from grid.bridge import (Bridge, BridgeConfig, iter_sse_data, memory_text, state_properties)
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
