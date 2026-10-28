"""The genome dimension (kannaka-dna E-001): eight genomes, each a mind, interleaved per request."""
import json
import sys
from pathlib import Path

import pytest

from grid.odin import DIMENSIONS, GenomeArm, Relay, assign, load_genome_arms
from grid.proposal_log import ProposalLog, proposal_entry
from grid.verdicts import verdict_row

sys.path.insert(0, str(Path(__file__).parent))
from test_odin import FakeBus, FakeMind, Clock, a_variant, state_of, write_request  # noqa: E402


def arm(i, alias="kannaka-brain-v1", t=0.7, k=5):
    return GenomeArm(individual=f"kannaka-g1-{i:06x}", genome_hash=f"{i:064x}", model_alias=alias,
                     temperature=t, recall_top_k=k, max_tokens=400)


def phenotype(path: Path, a: GenomeArm):
    path.write_text(json.dumps({"genome": a.genome_hash, "individual": a.individual, "role": "role-swarm-mind",
                                "params": {"model_alias": a.model_alias, "temperature": a.temperature,
                                           "recall_top_k": a.recall_top_k, "max_tokens": a.max_tokens}}))


def test_genome_is_a_dimension_and_salted():
    assert "genome" in DIMENSIONS
    arms = [f"{i:064x}" for i in range(8)]
    w = [1 / 8] * 8
    ids = [f"req-{i}" for i in range(4000)]
    counts = {}
    for rid in ids:
        counts[assign(rid, dimension="genome", arms=arms, weights=w)] = counts.get(
            assign(rid, dimension="genome", arms=arms, weights=w), 0) + 1
    assert len(counts) == 8 and min(counts.values()) > 350        # every arm reachable, roughly even
    # independent of the prompt dimension: the two assignments are not the same partition
    same = sum(assign(rid, dimension="genome", arms=arms[:2], weights=[0.5, 0.5]) == arms[0]
               and assign(rid, dimension="prompt", arms=arms[:2], weights=[0.5, 0.5]) == arms[0] for rid in ids)
    assert 800 < same < 1200


def test_loader_reads_kdna_phenotypes_in_a_fixed_order(tmp_path):
    a, b = arm(2, "kannaka-brain-v2", 0.58, 3), arm(1, "kannaka-brain-v1", 0.88, 9)
    phenotype(tmp_path / "z.json", a)
    phenotype(tmp_path / "a.json", b)
    arms = load_genome_arms(tmp_path)
    assert [x.individual for x in arms] == [b.individual, a.individual]   # file order, not insertion order
    assert arms[0].temperature == 0.88 and arms[0].recall_top_k == 9


def test_loader_refuses_duplicates_singletons_and_non_phenotypes(tmp_path):
    phenotype(tmp_path / "a.json", arm(1))
    with pytest.raises(ValueError, match="at least two"):
        load_genome_arms(tmp_path)
    phenotype(tmp_path / "b.json", arm(1))
    with pytest.raises(ValueError, match="share a genome hash"):
        load_genome_arms(tmp_path)
    (tmp_path / "b.json").write_text(json.dumps({"individual": "x"}))
    with pytest.raises(ValueError, match="not a kdna phenotype"):
        load_genome_arms(tmp_path)


def test_genome_dimension_cannot_run_beside_a_model_ab(tmp_path):
    with pytest.raises(ValueError, match="cannot run together"):
        Relay(tmp_path, colony="c", agent_id="grid-c", mind=FakeMind(), bus=FakeBus(),
              candidate_mind=FakeMind(), genome_arms=[arm(1), arm(2)],
              genome_minds={"kannaka-brain-v1": FakeMind()})
    with pytest.raises(ValueError, match="no mind"):
        Relay(tmp_path, colony="c", agent_id="grid-c", mind=FakeMind(), bus=FakeBus(),
              genome_arms=[arm(1, "kannaka-brain-v3")], genome_minds={})


async def test_each_request_is_answered_by_its_genome_and_logged(tmp_path):
    arms = [arm(1, "kannaka-brain-v1", 0.58, 3), arm(2, "kannaka-brain-v2", 0.88, 9)]
    minds = {"kannaka-brain-v1": FakeMind(answer='["harvest", "harvest", "copy", "fork"]'),
             "kannaka-brain-v2": FakeMind(answer='["harvest", "fork"]')}
    shared = FakeMind()                 # recall and remember go here, whatever the genome
    log_dir = tmp_path / "verdicts"
    r = Relay(tmp_path / "q", colony="c", agent_id="grid-c", mind=shared, bus=FakeBus(), clock=Clock(),
              state_fetch=lambda: state_of(3, 500), proposal_log=ProposalLog(log_dir),
              model_alias="unused", prompt_variant=a_variant("v1"), genome_arms=arms, genome_minds=minds)
    seen = {}
    for i in range(40):
        rid = f"req-{i}"
        write_request(tmp_path / "q", rid)
        assert await r.poll_once() == "proposed"
        (tmp_path / "q" / "proposal.json").unlink()
        (tmp_path / "q" / "request.json").unlink(missing_ok=True)
        seen[rid] = r.genome_for(rid)
    assert {g.genome_hash for g in seen.values()} == {a.genome_hash for a in arms}
    # the genome's mind answered, at the genome's temperature, with the genome's recall depth
    asked = {alias: len(m.asked) for alias, m in minds.items()}
    assert asked["kannaka-brain-v1"] + asked["kannaka-brain-v2"] == 40 and min(asked.values()) > 5
    assert set(minds["kannaka-brain-v1"].sampling) == {(0.58, 400)}
    assert set(minds["kannaka-brain-v2"].sampling) == {(0.88, 400)}
    assert len(shared.recalled) == 40 and not shared.asked          # recall shared, asks not
    rows = [json.loads(l) for l in (log_dir / "proposals.jsonl").read_text().splitlines()]
    assert len(rows) == 40
    for row in rows:
        g = seen[row["request_id"]]
        assert row["genome"] == g.individual and row["genome_hash"] == g.genome_hash
        assert row["model_alias"] == g.model_alias
    # ...and the verdict carries the arm through without a join
    v = verdict_row(rows[0], None, settled_tick=9000)
    assert v["genome"] == rows[0]["genome"] and v["genome_hash"] == rows[0]["genome_hash"]


def test_rows_without_a_genome_arm_read_as_blank():
    from test_verdicts import entry
    assert verdict_row(entry(), None, settled_tick=9000)["genome"] == ""
    assert "genome" in proposal_entry.__kwdefaults__
