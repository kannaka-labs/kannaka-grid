import json
from pathlib import Path

import pytest

from grid.natsio import BusClosed, BusTimeout
from grid.odin import Relay

ISA = [{"name": n, "cost": 0.1, "effect": n} for n in ("nop", "harvest", "copy", "fork")]


def write_request(queue: Path, request_id="r1") -> Path:
    queue.mkdir(parents=True, exist_ok=True)
    p = queue / "request.json"
    p.write_text(json.dumps({"request_id": request_id, "created_at": 1.0,
                             "parent": {"id": 1, "energy": 90.0, "tick": 321, "epoch": 2},
                             "genome": ["harvest", "copy", "fork"], "instruction_set": ISA,
                             "ecology": {}, "directive": "author one variant"}))
    return p


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


def relay(tmp_path, mind, bus=None, clock=None) -> Relay:
    return Relay(tmp_path, colony="c", agent_id="grid-c", mind=mind, bus=bus or FakeBus(),
                 clock=clock or Clock())


async def test_no_request_is_a_noop(tmp_path):
    assert await relay(tmp_path, FakeMind()).poll_once() is None


async def test_valid_reply_writes_proposal_and_publishes_proposed(tmp_path):
    write_request(tmp_path)
    bus = FakeBus()
    mind = FakeMind(answer='["harvest","harvest","copy","fork"]')
    r = relay(tmp_path, mind, bus)
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
