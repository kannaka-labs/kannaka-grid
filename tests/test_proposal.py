import json
from pathlib import Path

import pytest

from grid.proposal import (GENOME_CAP, Rejection, Request, extract_names, parse_request,
                           render_prompt, validate, write_proposal)

ISA = [{"name": n, "cost": 0.1, "effect": f"do {n}"} for n in
       ("nop", "harvest", "scan", "move", "alloc", "copy", "ifnotdone", "jmpb", "fork")]
PARENT_GENOME = ["harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork"]


def request_dict(**over) -> dict:
    d = {"request_id": "abc123", "created_at": 1.0,
         "parent": {"id": 7, "energy": 80.0, "generation": 3, "tick": 500},
         "genome": list(PARENT_GENOME), "instruction_set": ISA,
         "ecology": {"signal/listen": "ephemeral local communication"},
         "directive": "Odin must author one motivated variant; preserve viable replication."}
    d.update(over)
    return d


def write_request(queue: Path, **over) -> Path:
    queue.mkdir(parents=True, exist_ok=True)
    p = queue / "request.json"
    p.write_text(json.dumps(request_dict(**over)))
    return p


def req(**over) -> Request:
    d = request_dict(**over)
    return Request(d["request_id"], d["created_at"], d["parent"], d["genome"],
                   d["instruction_set"], d["ecology"], d["directive"])


def test_parse_request_reads_thegrid_shape(tmp_path):
    write_request(tmp_path)
    r = parse_request(tmp_path / "request.json")
    assert r.request_id == "abc123"
    assert r.genome == PARENT_GENOME
    assert [i["name"] for i in r.instruction_set][:3] == ["nop", "harvest", "scan"]


def test_parse_request_rejects_missing_genome(tmp_path):
    p = tmp_path / "request.json"
    p.write_text(json.dumps({"request_id": "x", "instruction_set": ISA}))
    with pytest.raises(ValueError):
        parse_request(p)


def test_render_prompt_contains_directive_isa_parent_and_recall():
    text = render_prompt(req(), recall=["last epoch the fork-early strain swept"])
    assert "Odin must author one motivated variant" in text
    assert "harvest" in text and "do harvest" in text
    assert "fork-early strain swept" in text
    assert "JSON array" in text


def test_render_prompt_opens_with_the_sandbox_context_before_the_directive():
    from grid.proposal import SANDBOX_CONTEXT
    text = render_prompt(req(), recall=[])
    assert text.startswith(SANDBOX_CONTEXT)
    assert "digital-evolution research sandbox" in SANDBOX_CONTEXT
    assert text.index(SANDBOX_CONTEXT) < text.index("Odin must author one motivated variant")


def test_extract_names_finds_first_string_array():
    assert extract_names('Sure: ["harvest", "copy"] and more') == ["harvest", "copy"]
    assert extract_names("no array here") is None
    assert extract_names("[1, 2]") is None


def test_validate_accepts_variant_from_isa():
    assert validate('["harvest","scan","alloc","copy","ifnotdone","jmpb","fork"]', req()) == \
        ["harvest", "scan", "alloc", "copy", "ifnotdone", "jmpb", "fork"]


def test_validate_rejects_unknown_opcode():
    r = validate('["harvest","teleport"]', req())
    assert isinstance(r, Rejection) and "teleport" in r.reason


def test_validate_rejects_identical_to_parent():
    r = validate(json.dumps(PARENT_GENOME), req())
    assert isinstance(r, Rejection) and "identical" in r.reason


def test_validate_rejects_empty_and_over_cap():
    assert isinstance(validate("[]", req()), Rejection)
    assert isinstance(validate(json.dumps(["nop"] * (GENOME_CAP + 1)), req()), Rejection)
    assert validate(json.dumps(["nop"] * GENOME_CAP), req()) == ["nop"] * GENOME_CAP


def test_validate_rejects_no_array():
    r = validate("I would fork earlier.", req())
    assert isinstance(r, Rejection) and "array" in r.reason


def test_write_proposal_is_atomic_and_matches_live_request(tmp_path):
    write_request(tmp_path)
    r = parse_request(tmp_path / "request.json")
    assert write_proposal(tmp_path, r, ["harvest", "fork"]) is True
    assert json.loads((tmp_path / "proposal.json").read_text()) == {"genome": ["harvest", "fork"]}
    assert not (tmp_path / "proposal.tmp").exists()


def test_write_proposal_refuses_when_request_gone_or_replaced(tmp_path):
    write_request(tmp_path)
    r = parse_request(tmp_path / "request.json")
    (tmp_path / "request.json").unlink()
    assert write_proposal(tmp_path, r, ["harvest", "fork"]) is False
    write_request(tmp_path, request_id="other")
    assert write_proposal(tmp_path, r, ["harvest", "fork"]) is False
    assert not (tmp_path / "proposal.json").exists()


def test_request_isa_is_the_instruction_names_in_order():
    assert req().isa == ["nop", "harvest", "scan", "move", "alloc", "copy", "ifnotdone", "jmpb", "fork"]


def test_genome_id_of_matches_thegrid_algorithm():
    import hashlib
    from grid.proposal import genome_id_of
    isa = ["nop", "harvest", "scan", "move", "alloc", "copy", "ifnotdone", "jmpb", "fork"]
    names = ["harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork"]
    expected = hashlib.sha256(bytes([1, 4, 5, 6, 7, 8])).hexdigest()[:16]
    assert genome_id_of(names, isa) == expected
    assert len(genome_id_of(names, isa)) == 16


def test_genome_id_of_agrees_with_thegrid_when_importable():
    import sys
    from pathlib import Path
    from grid.proposal import genome_id_of
    thegrid = Path(__file__).resolve().parents[1] / "thegrid"
    if not (thegrid / "src" / "colony" / "history.py").exists():
        pytest.skip("thegrid submodule not checked out")
    sys.path.insert(0, str(thegrid))
    try:
        from src.colony.history import genome_id  # type: ignore
    except Exception as e:  # noqa: BLE001 - thegrid may need Linux-only modules
        # Skywave and CI are Linux and run the colony: there, not importing thegrid
        # means this test silently stopped checking anything.
        if sys.platform == "linux":
            pytest.fail(f"thegrid must be importable on Linux: {e}")
        pytest.skip(f"thegrid not importable here: {e}")
    finally:
        sys.path.remove(str(thegrid))
    isa = ["nop", "harvest", "scan", "move", "alloc", "copy"]
    assert genome_id_of(["harvest", "copy", "nop"], isa) == genome_id([1, 5, 0])


def test_genome_id_of_rejects_unknown_name():
    from grid.proposal import genome_id_of
    with pytest.raises(ValueError):
        genome_id_of(["harvest", "teleport"], ["nop", "harvest"])


def test_a_variant_without_recall_says_so_instead_of_claiming_an_empty_memory():
    # Minor 4: "- (nothing yet)" asserts that recall RAN and came back empty. For a
    # recall: false variant that is a different fact, and it is the one the model reads
    # and the corpus records. Mutation: restore the single unconditional `memory = ...`
    # line -> the recall: false render says "(nothing yet)".
    import hashlib as _h
    from grid.variants import Variant
    tpl = "T {directive}\nMEM:\n{memory}\n"

    def v(recall):
        return Variant(id="v", template=tpl, sha256=_h.sha256(tpl.encode()).hexdigest(),
                       temperature=0.7, max_tokens=400, recall=recall, note="")

    off = render_prompt(req(), [], v(False))
    assert "not consulted" in off and "nothing yet" not in off
    on_empty = render_prompt(req(), [], v(True))
    assert "- (nothing yet)" in on_empty and "not consulted" not in on_empty
    on_full = render_prompt(req(), ["remembered a thing"], v(True))
    assert "- remembered a thing" in on_full and "nothing yet" not in on_full


def test_recall_passed_to_a_variant_that_declines_it_is_refused_not_dropped():
    # N5: the variant wins either way -- it is the thing the A/B measures -- but a caller
    # that went and fetched memories holds a different belief about this render than the
    # variant does, and quietly discarding their argument is how someone later spends an
    # afternoon on a prompt that does not contain what they passed it.
    # Mutation: delete the `if recall: raise` branch -> the first case below renders the
    # "not consulted" line and silently loses the memory.
    import hashlib as _h
    from grid.variants import Variant
    tpl = "T {directive}\nMEM:\n{memory}\n"

    def v(recall):
        return Variant(id="no-recall", template=tpl, sha256=_h.sha256(tpl.encode()).hexdigest(),
                       temperature=0.7, max_tokens=400, recall=recall, note="")

    with pytest.raises(ValueError, match="declares recall: false"):
        render_prompt(req(), ["a remembered thing"], v(False))
    with pytest.raises(ValueError, match="no-recall"):        # the message names the variant
        render_prompt(req(), ["one", "two"], v(False))
    # [] is not a discard: nothing is lost, and this is the path the relay actually takes
    assert "not consulted" in render_prompt(req(), [], v(False))
    # and a variant that wants recall is untouched
    assert "- a remembered thing" in render_prompt(req(), ["a remembered thing"], v(True))
