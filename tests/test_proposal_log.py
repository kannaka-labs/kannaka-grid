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
