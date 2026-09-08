import hashlib
import json
from pathlib import Path

import httpx
import pytest

from grid.natsio import BusClosed, BusTimeout
from grid.odin import Relay
from grid.proposal_log import variant_of, variant_sha256_of
from grid.variants import Variant

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
        self.sampling: list[tuple[float | None, int | None]] = []

    def remember(self, text, tags, *, category="grid"):
        self.remembered.append((text, tags))
        return True

    async def recall(self, query, top_k=5):
        self.recalled.append(query)
        return ["last time fork-early won"]

    async def ask(self, text, *, recall_query=None, timeout_s=180.0,
                  temperature=None, max_tokens=None):
        self.asked.append(text)
        self.sampling.append((temperature, max_tokens))
        if self.exc:
            raise self.exc
        return self.answer


def a_variant(vid, *, template="VARIANT {directive} / {genome_json} / {memory}",
              temperature=0.7, max_tokens=400, recall=True) -> Variant:
    """A registry entry built in memory: these tests are about which variant the relay
    picks, not about the shipped templates, which tests/test_variants.py owns."""
    return Variant(id=vid, template=template, sha256=hashlib.sha256(template.encode()).hexdigest(),
                   temperature=temperature, max_tokens=max_tokens, recall=recall, note="test")


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


async def test_a_wedged_candidate_ask_is_binned_after_the_defer_limit(tmp_path):
    # C3: thegrid keeps ONE request slot, so a candidate ask that can never answer defers
    # forever and the incumbent gets no turns either -- is_stuck (incumbent >= 50) can then
    # never trip and §8 recovery cannot fire. Bin the request instead; thegrid files a fresh
    # one that may hash to the other arm. Mutation: drop the `seen >= DEFER_LIMIT` branch in
    # grid/odin.py poll_once -> the fourth poll returns "deferred" and request.json survives.
    from grid.mind import MindError
    from grid.odin import DEFER_LIMIT, arm_for
    from grid.proposal_log import ProposalLog
    cand_id = next(i for i in (f"c{i}" for i in range(500)) if arm_for(i, 0.2) == "candidate")
    request = write_request(tmp_path, request_id=cand_id)
    clock = Clock()
    incumbent = FakeMind(answer='["nop"]')
    plog = ProposalLog(tmp_path / "verdicts")
    bus = FakeBus()
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=incumbent, bus=bus, clock=clock,
              state_fetch=lambda: state_of(1, 10), proposal_log=plog, model_alias="v3",
              candidate_mind=FakeMind(exc=MindError("gateway 500")), candidate_alias="v4", split=0.2)
    for _ in range(DEFER_LIMIT - 1):
        assert await r.poll_once() == "deferred"
        assert request.exists()
        clock.t += 61.0
    assert await r.poll_once() == "rejected"
    assert not request.exists()                      # thegrid's only slot is free again
    [e] = plog.read_all()
    assert e["outcome"] == "rejected" and e["reason"] == f"deferred {DEFER_LIMIT} times"
    assert e["model_alias"] == "v4"                  # the arm that could not answer owns the row
    assert bus.published[-1][1]["reason"] == f"deferred {DEFER_LIMIT} times"
    assert incumbent.asked == []                     # still never re-routed


async def test_a_successful_ask_resets_the_defer_count(tmp_path):
    from grid.odin import DEFER_LIMIT
    request = write_request(tmp_path)
    clock = Clock()
    mind = FakeMind(answer='["harvest","fork"]', exc=BusTimeout("slow"))
    r = relay(tmp_path, mind, clock=clock)
    for _ in range(DEFER_LIMIT - 1):
        assert await r.poll_once() == "deferred"
        clock.t += 61.0
    mind.exc = None
    assert await r.poll_once() == "proposed"
    (tmp_path / "proposal.json").unlink()            # thegrid consumed it
    mind.exc = BusTimeout("slow")
    for _ in range(DEFER_LIMIT - 1):
        assert await r.poll_once() == "deferred"     # back to strike one, not binned
        clock.t += 61.0
    assert request.exists()


async def test_the_defer_counter_does_not_outlive_its_request(tmp_path):
    # N8: thegrid keeps one request slot, so a counter under any other id belongs to a request
    # that was abandoned and will never be seen again. Without the prune the dict grows by one
    # entry per abandoned request for the relay's lifetime. Mutation: drop the prune in
    # poll_once -> r1's counter is still there after r2 is answered.
    write_request(tmp_path, "r1")
    clock = Clock()
    mind = FakeMind(answer='["harvest","fork"]', exc=BusTimeout("slow"))
    r = relay(tmp_path, mind, clock=clock)
    assert await r.poll_once() == "deferred"
    assert set(r._defers) == {"r1"}
    write_request(tmp_path, "r2")                # thegrid abandoned r1 and filed a new one
    mind.exc = None
    assert await r.poll_once() == "proposed"
    assert r._defers == {}


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


def test_config_from_env_tolerates_blank_numeric_lines(monkeypatch):
    # C1: the gate writes AND reverts these lines, and a present-but-empty `MIND_AB_SPLIT=`
    # makes os.environ.get return "" -- not the default -- so float() raises at boot, the
    # relay crash-loops into its StartLimit and stays dead. Mutation: restore
    # `float(env("MIND_AB_SPLIT", str(AB_SPLIT)))` -> ValueError instead of 0.2.
    from grid.mind import ASK_TIMEOUT_S
    from grid.odin import AB_SPLIT, RelayConfig
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    for k in ("MIND_AB_SPLIT", "MIND_ASK_TIMEOUT_S", "ODIN_RETRY_S", "ODIN_POLL_S"):
        monkeypatch.setenv(k, "")
    c = RelayConfig.from_env()
    assert c.ab_split == AB_SPLIT and c.ask_timeout_s == ASK_TIMEOUT_S
    assert c.retry_after_s == 60.0 and c.poll_s == 1.0


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


def test_model_dimension_assignment_is_unchanged_and_prompt_is_independent():
    from grid.odin import arm_for, assign
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


async def test_without_a_prompt_candidate_every_entry_logs_the_default_variant(tmp_path):
    # Includes the deferred-bin rejection: the arm that could not answer is still the arm
    # that was asked, and task 3 counts those rows per arm. Mutation: drop `variant=variant`
    # from any one _log call in poll_once -> that entry's prompt_variant is "".
    from grid.mind import MindError
    from grid.odin import DEFER_LIMIT
    from grid.proposal_log import ProposalLog
    from grid.variants import get_variant
    default = get_variant()
    plog = ProposalLog(tmp_path / "verdicts")
    clock = Clock()
    mind = FakeMind(answer='["harvest","fork"]')
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=mind, bus=FakeBus(), clock=clock,
              state_fetch=lambda: state_of(1, 10), proposal_log=plog, model_alias="v3")
    write_request(tmp_path, "p1")
    assert await r.poll_once() == "proposed"
    (tmp_path / "proposal.json").unlink()               # thegrid consumed it
    write_request(tmp_path, "j1")
    mind.answer = '["teleport"]'
    assert await r.poll_once() == "rejected"
    write_request(tmp_path, "d1")
    mind.exc = MindError("gateway 500")
    for _ in range(DEFER_LIMIT - 1):
        assert await r.poll_once() == "deferred"
        clock.t += 61.0
    assert await r.poll_once() == "rejected"
    entries = plog.read_all()
    assert [e["outcome"] for e in entries] == ["proposed", "rejected", "rejected"]
    assert entries[-1]["reason"] == f"deferred {DEFER_LIMIT} times"
    assert {variant_of(e) for e in entries} == {default.id}
    assert {variant_sha256_of(e) for e in entries} == {default.sha256}
    assert mind.sampling[0] == (default.temperature, default.max_tokens)


async def test_a_prompt_candidate_splits_traffic_and_every_entry_matches_assign(tmp_path):
    # The candidate is the MINORITY arm at split 0.2. Mutation: build the pair in
    # Relay.__init__ as two_arm(incumbent.id, candidate.id, split) -> two_arm's own probe
    # raises; delete that probe as well and the candidate silently takes 80% here.
    from grid.odin import assign
    from grid.proposal_log import ProposalLog
    incumbent, candidate = a_variant("v1-inc"), a_variant("v2-cand", template="OTHER {genome_json}")
    plog = ProposalLog(tmp_path / "verdicts")
    mind = FakeMind(answer='["harvest","fork"]')
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=mind, bus=FakeBus(), clock=Clock(),
              state_fetch=lambda: state_of(1, 10), proposal_log=plog, model_alias="v3",
              prompt_variant=incumbent, prompt_candidate=candidate, prompt_split=0.2)
    ids = [f"q{i}" for i in range(40)]
    for rid in ids:
        write_request(tmp_path, rid)
        assert await r.poll_once() == "proposed"
        (tmp_path / "proposal.json").unlink()
    expected = [assign(rid, dimension="prompt", arms=["v2-cand", "v1-inc"], weights=[0.2, 0.8])
                for rid in ids]
    entries = plog.read_all()
    assert [variant_of(e) for e in entries] == expected
    assert set(expected) == {"v1-inc", "v2-cand"}            # both arms really got traffic
    assert 0 < expected.count("v2-cand") < len(ids) // 2     # and the candidate is the minority
    by_id = {"v1-inc": incumbent.sha256, "v2-cand": candidate.sha256}
    assert [variant_sha256_of(e) for e in entries] == [by_id[v] for v in expected]


async def test_a_variant_that_wants_no_recall_never_calls_recall(tmp_path):
    # Mutation: make poll_once recall unconditionally -> the recall=False leg calls it once
    # and "fork-early won" shows up in a prompt whose template has no memory placeholder.
    for recall in (True, False):
        queue = tmp_path / f"q-{recall}"
        write_request(queue, "r1")
        mind = FakeMind(answer='["harvest","fork"]')
        r = Relay(queue, colony="c", agent_id="grid-c", mind=mind, bus=FakeBus(), clock=Clock(),
                  state_fetch=lambda: state_of(1, 10),
                  prompt_variant=a_variant("v", recall=recall))
        assert await r.poll_once() == "proposed"
        assert len(mind.recalled) == (1 if recall else 0)
        assert ("fork-early won" in mind.asked[0]) is recall


async def test_the_variants_sampling_parameters_reach_the_gateway(tmp_path):
    # End to end through the real Mind: relay -> ask -> _ask_direct -> request body.
    # Mutation: restore the hardcoded 0.7/400 in _ask_direct -> 0.7 != 0.11.
    from grid.mind import DirectLlm, Mind
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '["harvest","fork"]'}}]})

    write_request(tmp_path, "s1")
    # recall=False, so the real Mind never reaches for the bus (FakeBus has no request()).
    mind = Mind("grid-c", FakeBus(), llm=DirectLlm("http://gw/v1", "kb"),
                http=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=mind, bus=FakeBus(), clock=Clock(),
              state_fetch=lambda: state_of(1, 10),
              prompt_variant=a_variant("hot", temperature=0.11, max_tokens=123, recall=False))
    assert await r.poll_once() == "proposed"
    assert seen["temperature"] == 0.11 and seen["max_tokens"] == 123


def test_config_from_env_prompt_variant_and_candidate(monkeypatch):
    from grid.odin import RelayConfig
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    for k in ("MIND_PROMPT_VARIANT", "MIND_PROMPT_CANDIDATE"):
        monkeypatch.delenv(k, raising=False)
    c = RelayConfig.from_env()
    assert c.prompt_variant == "" and c.prompt_candidate == ""
    for k in ("MIND_PROMPT_VARIANT", "MIND_PROMPT_CANDIDATE"):
        monkeypatch.setenv(k, "")          # the gate reverts by blanking, never by deleting
    c = RelayConfig.from_env()
    assert c.prompt_variant == "" and c.prompt_candidate == ""
    monkeypatch.setenv("MIND_PROMPT_VARIANT", "v1-sandbox")
    monkeypatch.setenv("MIND_PROMPT_CANDIDATE", "v2-terse")
    c = RelayConfig.from_env()
    assert c.prompt_variant == "v1-sandbox" and c.prompt_candidate == "v2-terse"


def test_a_prompt_ab_against_itself_is_refused(tmp_path):
    # Both arms would render the same template, so the run could never separate them --
    # but every log row and ledger row would claim an A/B was running.
    with pytest.raises(ValueError, match="both"):
        Relay(tmp_path, colony="c", agent_id="grid-c", mind=FakeMind(), bus=FakeBus(),
              prompt_variant=a_variant("same"), prompt_candidate=a_variant("same"))


def test_assign_rejects_an_unknown_dimension():
    # I3: the model dimension's whole guarantee is one string compare, so `dimension="modle"`
    # would silently salt it and re-randomise a RUNNING model A/B with no error anywhere.
    # Mutation: drop the `dimension not in DIMENSIONS` check -> "modle" quietly returns an arm.
    from grid.odin import DIMENSIONS, UNSALTED_DIMENSION, assign
    assert UNSALTED_DIMENSION in DIMENSIONS and "prompt" in DIMENSIONS
    for bad in ("modle", "Model", "", "models"):
        with pytest.raises(ValueError, match="unknown dimension"):
            assign("r1", dimension=bad, arms=["a", "b"], weights=[0.5, 0.5])


def test_assign_rejects_mismatched_arms_and_weights():
    # Minor 1: zip() truncates silently, so a missing weight would make the last arm
    # unreachable while the A/B still looked like it was running.
    # Mutation: drop the length check -> "c" never wins and no error is raised.
    from grid.odin import assign
    with pytest.raises(ValueError, match="3 arms but 2 weights"):
        assign("r1", dimension="prompt", arms=["a", "b", "c"], weights=[0.4, 0.6])


def test_the_two_arm_probe_catches_an_inversion_at_an_extreme_split():
    # Minor 2: the plain 64-id probe spans h in [0.00459, 0.99944], so an inverted pair was
    # invisible for split in (0, 0.000564) or (0.999436, 1). The splits below sit inside
    # that old blind window, which is what makes this test non-vacuous.
    # Mutation: drop the two appended extreme ids from _ORDER_PROBE_IDS -> `caught` goes
    # False and both cases fail. (Choosing 0.001 / 0.999 instead would pass either way:
    # those are already covered by the plain range, so they prove nothing.)
    from grid.odin import _ORDER_PROBE_IDS, arm_for, assign, two_arm
    plain = tuple(f"order-probe-{i}" for i in range(64))
    for split in (0.0001, 0.9999):
        assert two_arm("cand", "inc", split) == (["cand", "inc"], [split, 1.0 - split])
        inverted, inv_w = ["inc", "cand"], [1.0 - split, split]

        def disagrees(ids):
            return any(assign(rid, dimension="model", arms=inverted, weights=inv_w)
                       != ("cand" if arm_for(rid, split) == "candidate" else "inc")
                       for rid in ids)

        assert not disagrees(plain), f"split {split} was meant to be blind to the plain range"
        assert disagrees(_ORDER_PROBE_IDS), f"the probe is blind to an inversion at {split}"


def test_config_from_env_prompt_split_is_its_own_knob(monkeypatch):
    # I5: MIND_AB_SPLIT widens a MODEL rollout; if it also moved the prompt boundary the
    # prompt arms would re-randomise mid-experiment across the restart.
    # Mutation: read prompt_ab_split from MIND_AB_SPLIT -> the 0.2 assertion below fails.
    from grid.odin import AB_SPLIT, RelayConfig
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    for k in ("MIND_AB_SPLIT", "MIND_PROMPT_AB_SPLIT"):
        monkeypatch.delenv(k, raising=False)
    assert RelayConfig.from_env().prompt_ab_split == AB_SPLIT
    monkeypatch.setenv("MIND_AB_SPLIT", "0.6")
    assert RelayConfig.from_env().prompt_ab_split == AB_SPLIT   # untouched by the model knob
    monkeypatch.setenv("MIND_PROMPT_AB_SPLIT", "0.35")
    c = RelayConfig.from_env()
    assert c.prompt_ab_split == 0.35 and c.ab_split == 0.6      # and the two are independent
    monkeypatch.setenv("MIND_PROMPT_AB_SPLIT", "")              # the gate reverts by blanking
    assert RelayConfig.from_env().prompt_ab_split == AB_SPLIT


def test_the_bus_route_refuses_to_start_a_variant_it_cannot_sample(tmp_path):
    # I4: with no DirectLlm the NATS route drops temperature/max_tokens, so two arms that
    # differ ONLY in sampling would be identical on the wire while every log and ledger row
    # claimed an A/B was running. Mutation: make check_sampling_route return immediately
    # -> no SystemExit, and the run boots into two indistinguishable arms.
    from grid.mind import DirectLlm
    from grid.odin import check_sampling_route
    default = a_variant("v1")
    hot = a_variant("v2", temperature=0.9)
    longer = a_variant("v3", max_tokens=999)
    check_sampling_route(None, default, None)                        # defaults: fine
    check_sampling_route(DirectLlm("http://gw/v1", "kb"), default, hot)   # direct route: fine
    for bad in (hot, longer):
        with pytest.raises(SystemExit, match="cannot carry them"):
            check_sampling_route(None, default, bad)
        with pytest.raises(SystemExit, match=bad.id):
            check_sampling_route(None, bad)


async def test_editing_the_registry_after_the_relay_starts_changes_nothing(tmp_path):
    # Carried over from task 1's review: load_registry re-reads and re-hashes on every
    # call, so a template edited in place with its sha256 re-pinned would be adopted
    # mid-experiment -- silently changing what one arm says while the ledger row that
    # opened the A/B still names the old bytes. The relay resolves ONCE and holds the
    # objects. Mutation: re-resolve inside variant_for with get_variant(self.prompt_variant.id,
    # path=...) -> the second prompt carries "EDITED" and the sha256 changes.
    import json as _json
    from grid.proposal_log import ProposalLog, variant_sha256_of
    from grid.variants import get_variant
    data = tmp_path / "reg"
    (data / "prompts").mkdir(parents=True)
    template = "ORIGINAL {genome_json}"

    def write_registry(text):
        (data / "prompts" / "v.md").write_bytes(text.encode())
        (data / "prompt_variants.json").write_text(_json.dumps({
            "default": "v", "variants": {"v": {
                "template_file": "prompts/v.md",
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "temperature": 0.7, "max_tokens": 400, "recall": False, "note": "t"}}}))

    write_registry(template)
    resolved = get_variant(path=data / "prompt_variants.json")       # resolved once, as run() does
    plog = ProposalLog(tmp_path / "verdicts")
    mind = FakeMind(answer='["harvest","fork"]')
    r = Relay(tmp_path / "q", colony="c", agent_id="grid-c", mind=mind, bus=FakeBus(), clock=Clock(),
              state_fetch=lambda: state_of(1, 10), proposal_log=plog, prompt_variant=resolved)
    write_request(tmp_path / "q", "a1")
    assert await r.poll_once() == "proposed"
    (tmp_path / "q" / "proposal.json").unlink()

    write_registry("EDITED {genome_json}")      # re-pinned hash: the loader would accept it
    write_request(tmp_path / "q", "a2")
    assert await r.poll_once() == "proposed"

    assert [m.startswith("ORIGINAL") for m in mind.asked] == [True, True]
    assert all(not m.startswith("EDITED") for m in mind.asked)
    sha = {variant_sha256_of(e) for e in plog.read_all()}
    assert sha == {hashlib.sha256(template.encode()).hexdigest()}


# Both split knobs, so the range check cannot be added to one and forgotten on the other.
SPLIT_KEYS = ("MIND_AB_SPLIT", "MIND_PROMPT_AB_SPLIT")


def _split_of(key, cfg):
    return cfg.ab_split if key == "MIND_AB_SPLIT" else cfg.prompt_ab_split


@pytest.mark.parametrize("key", SPLIT_KEYS)
def test_a_split_outside_zero_to_one_is_refused_at_load(monkeypatch, key):
    # N1: 0, 1, 2 and -1 all parse and then run a ONE-ARMED experiment that every log row
    # and every ledger row still calls an A/B, until the weekly gate declares a winner from
    # a comparison that never happened. MIND_AB_SPLIT was unchecked from the previous
    # sub-project and is deployed that way today.
    # Mutation: drop the `0.0 < value < 1.0` check in _split -> every rejected case below
    # loads happily. Clamping instead of raising fails here too, which is the point.
    from grid.odin import RelayConfig
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    for other in SPLIT_KEYS:
        monkeypatch.delenv(other, raising=False)
    for bad in ("0", "1", "0.0", "1.0", "2", "-1", "-0.2", "1.0001"):
        monkeypatch.setenv(key, bad)
        with pytest.raises(SystemExit) as e:
            RelayConfig.from_env()
        assert key in str(e.value) and bad in str(e.value)      # names the key AND the value
        assert "strictly between 0 and 1" in str(e.value)
    for bad in ("abc", "0.2x", "nan-ish"):
        monkeypatch.setenv(key, bad)
        with pytest.raises(SystemExit) as e:
            RelayConfig.from_env()
        assert key in str(e.value) and "not a number" in str(e.value)


@pytest.mark.parametrize("key", SPLIT_KEYS)
def test_a_split_just_inside_the_boundaries_is_accepted(monkeypatch, key):
    # The complement of the test above: the check must reject the boundaries without
    # rejecting the usable range next to them. Mutation: tighten _split to `0.01 < v < 0.99`
    # -> the 0.001 and 0.999 cases raise.
    from grid.odin import AB_SPLIT, RelayConfig
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    for other in SPLIT_KEYS:
        monkeypatch.delenv(other, raising=False)
    for good in ("0.001", "0.2", "0.5", "0.999"):
        monkeypatch.setenv(key, good)
        assert _split_of(key, RelayConfig.from_env()) == float(good)
    monkeypatch.setenv(key, "")             # the gate reverts by blanking, never deleting
    assert _split_of(key, RelayConfig.from_env()) == AB_SPLIT
    monkeypatch.setenv(key, "  0.35  ")     # a stray space in a unit file is not a crash
    assert _split_of(key, RelayConfig.from_env()) == 0.35


@pytest.mark.parametrize("key", SPLIT_KEYS)
def test_one_bad_split_does_not_take_the_other_knob_with_it(monkeypatch, key):
    # The two dimensions stay independent through validation too: a bad value on one knob
    # must be reported as that knob, not silently attributed to the other.
    from grid.odin import RelayConfig
    other = next(k for k in SPLIT_KEYS if k != key)
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    monkeypatch.setenv(other, "0.4")
    monkeypatch.setenv(key, "0")
    with pytest.raises(SystemExit) as e:
        RelayConfig.from_env()
    assert key in str(e.value) and other not in str(e.value)
    monkeypatch.setenv(key, "0.4")
    assert _split_of(key, RelayConfig.from_env()) == 0.4


def test_a_zero_split_is_refused_before_the_sampling_guard_is_ever_consulted(monkeypatch):
    # N2: the report of a candidate "parked at split 0" crash-looping through
    # check_sampling_route assumed a zero split could be configured. After N1 it cannot,
    # so rather than adding dead defensive code inside the guard -- which takes no split
    # and should not -- this pins the reason it is unreachable: from_env raises first, so
    # no RelayConfig carrying a zero split can ever exist to hand to the guard.
    # Mutation: drop the range check -> from_env returns a cfg with prompt_ab_split 0.0
    # and the assertion that no such cfg exists fails.
    import inspect
    from grid.odin import RelayConfig, check_sampling_route, run
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    monkeypatch.setenv("MIND_PROMPT_AB_SPLIT", "0")
    with pytest.raises(SystemExit, match="MIND_PROMPT_AB_SPLIT"):
        RelayConfig.from_env()
    # and the guard genuinely has no split to consult, so there is nothing to defend
    assert "split" not in inspect.signature(check_sampling_route).parameters
    # run() consults the guard only after cfg exists, i.e. after the check above has fired
    body = inspect.getsource(run)
    assert "check_sampling_route" in body and "def run(cfg: RelayConfig)" in body


async def test_a_factorial_relay_logs_both_labels_from_two_independent_draws(tmp_path):
    # Requirement A for the factorial gate, pinned: the relay ALREADY does everything a 2x2 run
    # needs, and there must be no second assignment path added for it. With both A/Bs running at
    # once each request is drawn TWICE -- the model arm from the bare request id (unchanged since
    # sub-project 3) and the prompt arm from a "prompt:"-salted hash -- and every log row carries
    # both labels, which is the only thing that lets grid.weekly key a tally by the pair.
    #
    # Mutation: salt the prompt draw with the model arm, or drop either salt so both dimensions
    # hash the same key -> the two draws become perfectly correlated, only two of the four cells
    # are ever populated, and neither marginal is balanced over the other dimension. Mutation:
    # drop `model_alias=alias` or `variant=variant` from any _log call -> a row attributable in
    # one dimension only, which grid.weekly must then exclude from every cell.
    from grid.odin import arm_for, assign
    from grid.proposal_log import ProposalLog
    incumbent, candidate = a_variant("p0"), a_variant("p1", template="OTHER {genome_json}")
    plog = ProposalLog(tmp_path / "verdicts")
    r = Relay(tmp_path, colony="c", agent_id="grid-c", mind=FakeMind(answer='["harvest","fork"]'),
              bus=FakeBus(), clock=Clock(), state_fetch=lambda: state_of(1, 10), proposal_log=plog,
              model_alias="m0", candidate_mind=FakeMind(answer='["harvest","fork"]'),
              candidate_alias="m1", split=0.5,
              prompt_variant=incumbent, prompt_candidate=candidate, prompt_split=0.5)
    ids = [f"f{i}" for i in range(60)]
    for rid in ids:
        write_request(tmp_path, rid)
        assert await r.poll_once() == "proposed"
        (tmp_path / "proposal.json").unlink()
    cells = [(e["model_alias"], variant_of(e)) for e in plog.read_all()]
    assert len(cells) == len(ids)
    assert all(m and p for m, p in cells)          # not one row attributable in only one dimension
    assert cells == [("m1" if arm_for(rid, 0.5) == "candidate" else "m0",
                      assign(rid, dimension="prompt", arms=["p1", "p0"], weights=[0.5, 0.5]))
                     for rid in ids]
    # All four cells populated, and neither draw predicts the other: two independent halves meet
    # near a quarter, not a half. That independence is what makes each marginal balanced over the
    # other dimension, which is the entire basis on which grid.weekly is allowed to pool.
    assert len(set(cells)) == 4
    assert 0.15 < cells.count(("m1", "p1")) / len(cells) < 0.35
