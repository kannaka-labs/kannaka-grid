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
                        kannaka_bin="kannaka", state_every_s=60, history_every_s=60, verdicts_dir=None)


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
              "MIND_DATA_DIR", "KANNAKA_BIN", "STATE_EVERY_S", "HISTORY_EVERY_S", "VERDICTS_DIR"):
        monkeypatch.delenv(k, raising=False)
    c = BridgeConfig.from_env()
    assert c.colony_url == "http://127.0.0.1:8787"
    assert c.nats_url == "nats://swarm.ninja-portal.com:4222"
    assert c.flux_url is None and c.mind_agent_id is None
    assert c.state_every_s == 60 and c.history_every_s == 60
    assert c.verdicts_dir is None
    assert c.ask_timeout_s == 180.0
    monkeypatch.setenv("MIND_ASK_TIMEOUT_S", "400")
    assert BridgeConfig.from_env().ask_timeout_s == 400.0


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


# -- issue #1: the chronicle skill line needs opcode NAMES, not glyphs ---------------

def test_genome_alphabet_matches_thegrid_record_py():
    """thegrid encodes one opcode per char; our copy of its alphabet must not drift."""
    import re
    from pathlib import Path
    from grid.bridge import GENOME_ALPHABET
    src = Path(__file__).resolve().parents[1] / "thegrid" / "src" / "colony" / "record.py"
    if not src.exists():
        pytest.skip("thegrid submodule not checked out")
    text = src.read_text()
    base = re.search(r'^ALPHABET = "([^"]+)"', text, re.M).group(1)
    ext = re.search(r'^GENOME_ALPHABET = ALPHABET \+ "([^"]+)"', text, re.M).group(1)
    assert GENOME_ALPHABET == base + ext


def test_decode_genome_maps_chars_to_isa_names():
    from grid.bridge import decode_genome
    isa = ["nop", "harvest", "fork"]
    assert decode_genome("012", isa) == ["nop", "harvest", "fork"]
    assert decode_genome("0z", isa) == ["nop", "?z"]          # index beyond the ISA
    assert decode_genome("", isa) == []


def test_phase_facts_names_dominant_and_novel_opcodes():
    from grid.bridge import phase_facts
    snap = Snapshot.from_state(state(tick=2000, dominant="0122", ancestor="01", population=77,
                                     strains=[50, 27]))
    facts = phase_facts(snap, ["nop", "harvest", "fork"])
    assert facts.startswith("Observed now at epoch 1 tick 2000:")
    assert "population 77" in facts and "storms so far 0" in facts
    assert "dominant genome (4 opcodes): nop harvest fork fork" in facts
    assert "opcodes the ancestor lacks: fork" in facts
    assert "\n" not in facts


def test_memory_text_carries_opcode_names_when_given():
    text = memory_text(Event("genome.novel", 1, 61, {"novel_ops": 1, "genome_glyph": "012"}),
                       names={"dominant": ["nop", "harvest", "fork"], "novel_opcodes": ["fork"]})
    assert "novel_opcodes=[fork]" in text and "dominant=[nop harvest fork]" in text
    assert memory_text(Event("storm", 1, 2, {})) == "Colony event storm at epoch 1 tick 2:"


async def test_handle_state_remembers_names_for_a_novel_genome():
    bus, mind = FakeBus(), FakeMind()
    bridge = Bridge(cfg(), bus=bus, mind=mind)
    await bridge.handle_state(state(tick=1, dominant="01", ancestor="01", strains=[50, 50]))
    await bridge.handle_state(state(tick=2, dominant="012", ancestor="01", strains=[50, 50]))
    novel = [t for t, tags in mind.remembered if "genome.novel" in tags]
    assert len(novel) == 1
    assert "novel_opcodes=[fork]" in novel[0] and "dominant=[nop harvest fork]" in novel[0]


async def test_chronicle_questions_carry_facts_and_remember_the_phase_line():
    bus = FakeBus()
    mind = FakeMind(answers={"What is this epoch about": "A slow squeeze.",
                             "Name the capability": "It learned to fork early using fork.",
                             "single most important": "Sweep at tick 1900."})
    bridge = Bridge(cfg(), bus=bus, mind=mind)
    await bridge.handle_state(state(tick=CHRONICLE_EVERY_TICKS, dominant="012", ancestor="01"))
    out = await bridge.maybe_chronicle()
    assert set(out) == {"topic", "skill", "event"}
    assert len(mind.asked) == 3
    for q in mind.asked:
        assert "Observed now at epoch 1 tick 2000" in q and "opcodes the ancestor lacks: fork" in q
    phase_lines = [t for t, tags in mind.remembered if tags == ["colony-one", "phase"]]
    assert len(phase_lines) == 1 and "dominant genome (3 opcodes): nop harvest fork" in phase_lines[0]
    assert mind.remembered.index((phase_lines[0], ["colony-one", "phase"])) >= 0


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


class ChronicleClosedBus(FakeBus):
    """State and events go through; the chronicle publish finds the client closed."""

    async def publish(self, subject, payload):
        if ".chronicle." in subject:
            from grid.natsio import BusClosed
            raise BusClosed("connection closed for good")
        await super().publish(subject, payload)


async def test_a_closed_bus_in_the_chronicle_task_is_re_raised_on_the_stream_path():
    # #2 item 4: nothing awaits the chronicle task, so its BusClosed must not vanish.
    import asyncio
    from grid.natsio import BusClosed
    bridge = Bridge(cfg(), bus=ChronicleClosedBus(), mind=FakeMind(answers={
        "What is this epoch about": "A slow squeeze."}))
    await bridge.handle_state(state(tick=CHRONICLE_EVERY_TICKS))
    bridge.schedule_chronicle()
    task = bridge._chronicle_task
    await asyncio.wait([task])
    with pytest.raises(BusClosed):
        bridge.schedule_chronicle()
