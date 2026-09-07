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
    assert e["reply"] == '["teleport"]'          # the raw reply IS the training negative


async def test_a_rejection_is_logged_even_when_the_bus_is_dead(tmp_path):
    """_log runs before _publish: BusClosed must not cost us the training record."""
    from grid.proposal_log import ProposalLog
    write_request(tmp_path)
    plog = ProposalLog(tmp_path / "verdicts")
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=FakeMind(answer='["teleport"]'),
              bus=ClosedBus(), clock=Clock(), proposal_log=plog)
    with pytest.raises(BusClosed):
        await r.poll_once()
    [e] = plog.read_all()
    assert e["outcome"] == "rejected" and e["reply"] == '["teleport"]'


async def test_a_proposal_is_logged_even_when_the_bus_is_dead(tmp_path):
    from grid.proposal_log import ProposalLog
    write_request(tmp_path)
    plog = ProposalLog(tmp_path / "verdicts")
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=FakeMind(answer='["harvest","fork"]'),
              bus=ClosedBus(), clock=Clock(), proposal_log=plog)
    with pytest.raises(BusClosed):
        await r.poll_once()
    [e] = plog.read_all()
    assert e["outcome"] == "proposed" and e["proposal"] == ["harvest", "fork"] and e["reply"] is None


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


def test_config_from_env_ask_timeout(monkeypatch):
    from grid.mind import ASK_TIMEOUT_S
    from grid.odin import RelayConfig
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    monkeypatch.delenv("MIND_ASK_TIMEOUT_S", raising=False)
    assert RelayConfig.from_env().ask_timeout_s == ASK_TIMEOUT_S
    monkeypatch.setenv("MIND_ASK_TIMEOUT_S", "400")
    assert RelayConfig.from_env().ask_timeout_s == 400.0
    monkeypatch.delenv("ODIN_RETRY_S", raising=False)
    assert RelayConfig.from_env().retry_after_s == 60.0
    monkeypatch.setenv("ODIN_RETRY_S", "420")
    assert RelayConfig.from_env().retry_after_s == 420.0


def test_arm_for_is_deterministic_and_respects_the_split():
    from grid.odin import arm_for
    assert arm_for("r1", 0.2) == arm_for("r1", 0.2)
    assert arm_for("anything", 0.0) == "incumbent" and arm_for("anything", 1.0) == "candidate"
    ids = [f"req-{i}" for i in range(2000)]
    share = sum(arm_for(i, 0.2) == "candidate" for i in ids) / len(ids)
    assert 0.15 < share < 0.25                       # ~20%, deterministic per id


async def test_relay_asks_the_candidate_arm_and_labels_it(tmp_path):
    # Build a request whose id hashes to the candidate arm at split 0.2, and one that does not.
    from grid.odin import arm_for
    from grid.proposal_log import ProposalLog
    cand_id = next(i for i in (f"c{i}" for i in range(500)) if arm_for(i, 0.2) == "candidate")
    inc_id = next(i for i in (f"i{i}" for i in range(500)) if arm_for(i, 0.2) == "incumbent")
    incumbent = FakeMind(answer='["nop"]')
    candidate = FakeMind(answer='["nop"]')
    for rid, expect_arm, expect_alias in ((cand_id, "candidate", "kannaka-brain-v4"), (inc_id, "incumbent", "kannaka-brain-v3")):
        queue = tmp_path / rid
        write_request(queue, request_id=rid)
        log = ProposalLog(tmp_path / f"v-{rid}")
        r = Relay(queue, colony="c", agent_id="grid-c", mind=incumbent, bus=FakeBus(), clock=Clock(),
                  state_fetch=lambda: state_of(1, 10), proposal_log=log, model_alias="kannaka-brain-v3",
                  candidate_mind=candidate, candidate_alias="kannaka-brain-v4", split=0.2)
        assert await r.poll_once() == "proposed"
        assert log.read_all()[-1]["model_alias"] == expect_alias
    assert len(candidate.asked) == 1 and len(incumbent.asked) == 1


async def test_relay_without_a_candidate_is_unchanged(tmp_path):
    from grid.proposal_log import ProposalLog
    write_request(tmp_path, "x1")
    log = ProposalLog(tmp_path / "verdicts")
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=FakeMind(answer='["nop"]'), bus=FakeBus(), clock=Clock(),
              state_fetch=lambda: state_of(1, 10), proposal_log=log, model_alias="kannaka-brain-v3")
    assert await r.poll_once() == "proposed" and log.read_all()[-1]["model_alias"] == "kannaka-brain-v3"


async def test_failed_candidate_ask_defers_and_never_reroutes(tmp_path):
    from grid.mind import MindError
    from grid.odin import arm_for
    from grid.proposal_log import ProposalLog
    cand_id = next(i for i in (f"c{i}" for i in range(500)) if arm_for(i, 0.2) == "candidate")
    write_request(tmp_path, request_id=cand_id)
    incumbent = FakeMind(answer='["nop"]')
    log = ProposalLog(tmp_path / "verdicts")
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=incumbent, bus=FakeBus(), clock=Clock(),
              state_fetch=lambda: state_of(1, 10), proposal_log=log, model_alias="v3",
              candidate_mind=FakeMind(exc=MindError("gateway 500")), candidate_alias="v4", split=0.2)
    assert await r.poll_once() == "deferred"
    assert incumbent.asked == []                                             # the incumbent was NOT asked instead


def test_config_from_env_candidate(monkeypatch, tmp_path):
    from grid.mind import DirectLlm
    from grid.odin import RelayConfig
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    for k in ("MIND_LLM_CANDIDATE", "MIND_AB_SPLIT", "MIND_LLM_URL", "MIND_LLM_MODEL", "MIND_LLM_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    c = RelayConfig.from_env()
    assert c.candidate_tag == "" and c.ab_split == 0.2 and c.candidate_llm is None
    kf = tmp_path / "k"; kf.write_text("sk-x\n")
    monkeypatch.setenv("MIND_LLM_URL", "http://gw/v1"); monkeypatch.setenv("MIND_LLM_MODEL", "kannaka-brain-current")
    monkeypatch.setenv("MIND_LLM_KEY_FILE", str(kf)); monkeypatch.setenv("MIND_LLM_CANDIDATE", "kannaka-brain-v4"); monkeypatch.setenv("MIND_AB_SPLIT", "0.3")
    c = RelayConfig.from_env()
    assert c.candidate_tag == "kannaka-brain-v4" and c.ab_split == 0.3
    assert c.candidate_llm == DirectLlm("http://gw/v1", "kannaka-brain-v4", "sk-x")
