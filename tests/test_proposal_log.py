import json
from pathlib import Path

from grid.proposal import Request
from grid.proposal_log import (REPLY_CAP, ProposalLog, proposal_entry, variant_of,
                                variant_sha256_of)

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
    assert e["reply"] is None


def test_reply_is_kept_and_capped():
    """A rejected reply is the training negative; a runaway one is not a transcript."""
    e = proposal_entry(req=req(), proposal=None, outcome="rejected", reason="r", epoch=1, tick=1,
                       colony="c", mind="m", model_alias="a", prompt="p", ts_ms=1,
                       reply='["teleport"]')
    assert e["reply"] == '["teleport"]'
    long = proposal_entry(req=req(), proposal=None, outcome="rejected", reason="r", epoch=1, tick=1,
                          colony="c", mind="m", model_alias="a", prompt="p", ts_ms=1, reply="x" * 5000)
    assert long["reply"] == "x" * REPLY_CAP


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


def test_read_all_stops_at_a_partial_trailing_line(tmp_path):
    log = ProposalLog(tmp_path)
    e = proposal_entry(req=req("a"), proposal=["fork"], outcome="proposed", reason=None, epoch=1, tick=1,
                       colony="c", mind="m", model_alias="a", prompt="p", ts_ms=1)
    assert log.record(e, "p") is True
    with open(tmp_path / "proposals.jsonl", "a", encoding="utf-8") as f:
        f.write('{"request_id": "b", "truncated')          # crash mid-write
    assert [x["request_id"] for x in log.read_all()] == ["a"]


def test_a_legacy_row_without_the_prompt_variant_keys_reads_as_empty():
    # I1, verified against the live corpus: rows written before the prompt registry do not
    # carry these keys as "" -- they do not carry them AT ALL, so entry["prompt_variant"]
    # raises KeyError on real data. Mutation: make either accessor index instead of .get()
    # -> KeyError here. This is the exact shape ProposalLog.read_all yields for such a line.
    legacy = json.loads(json.dumps({
        "ts": 1, "colony": "c", "epoch": 0, "tick": 0, "request_id": "old-1", "mind": "grid-c",
        "model_alias": "agent-brain", "parent_genome": ["harvest"], "parent_genome_id": "abc",
        "proposal": ["harvest", "fork"], "proposal_genome_id": "def", "outcome": "proposed",
        "reason": None, "reply": None, "prompt_sha256": "0" * 64, "prompt_path": "prompts/old-1.txt"}))
    assert "prompt_variant" not in legacy and "prompt_variant_sha256" not in legacy
    assert variant_of(legacy) == "" and variant_sha256_of(legacy) == ""
    # a null, which is what a hand-edited or partially-migrated row can hold, reads the same
    assert variant_of({"prompt_variant": None}) == ""
    assert variant_sha256_of({"prompt_variant_sha256": None}) == ""
    # and a real row round-trips through the same readers
    assert variant_of({"prompt_variant": "v1-sandbox"}) == "v1-sandbox"
    assert variant_sha256_of({"prompt_variant_sha256": "a" * 64}) == "a" * 64


def test_read_all_parses_a_legacy_line_written_before_the_registry(tmp_path):
    # The end-to-end shape of the above: an old JSONL line still loads, and every reader
    # of the new fields copes with what comes back.
    vdir = tmp_path / "verdicts"
    vdir.mkdir()
    (vdir / "proposals.jsonl").write_text(
        json.dumps({"ts": 1, "request_id": "old-1", "outcome": "proposed",
                    "model_alias": "agent-brain", "prompt_path": "prompts/old-1.txt"}) + "\n",
        encoding="utf-8")
    [row] = ProposalLog(vdir).read_all()
    assert "prompt_variant" not in row
    assert variant_of(row) == "" and variant_sha256_of(row) == ""
