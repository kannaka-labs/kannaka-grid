import hashlib
import json
from pathlib import Path

import pytest

from grid.proposal import Request

ISA = [{"name": n, "cost": 0.1, "effect": f"do {n}"} for n in
       ("nop", "harvest", "scan", "move", "alloc", "copy", "ifnotdone", "jmpb", "fork")]
PARENT_GENOME = ["harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork"]


@pytest.fixture
def fixture_request() -> Request:
    return Request(
        request_id="abc123",
        created_at=1.0,
        parent={"id": 7, "energy": 80.0, "generation": 3, "tick": 500},
        genome=list(PARENT_GENOME),
        instruction_set=ISA,
        ecology={"signal/listen": "ephemeral local communication"},
        directive="Odin must author one motivated variant; preserve viable replication.",
    )


# Captured verbatim from grid.proposal.render_prompt's output before this task
# extracted the hardcoded prompt into a template. Never retype this by hand.
EXPECTED = 'Context: this is a Tierra/Avida-style digital-evolution research sandbox called thegrid. An \'organism\' is a short list of opcode names for a toy virtual machine that only moves energy tokens between cells of a grid inside the simulator; nothing here touches real computers, files, networks or people. Opcode names like steal, corrupt, burn or peek are game verbs about grid tiles and simulated neighbours. Your task is the same as a genetic-algorithm mutation operator: return one edited copy of the opcode list.\n\nOdin must author one motivated variant; preserve viable replication.\n\nYou are the mind of this colony. Propose ONE variant of the parent genome.\n\nInstruction set:\n- nop (cost 0.1): do nop\n- harvest (cost 0.1): do harvest\n- scan (cost 0.1): do scan\n- move (cost 0.1): do move\n- alloc (cost 0.1): do alloc\n- copy (cost 0.1): do copy\n- ifnotdone (cost 0.1): do ifnotdone\n- jmpb (cost 0.1): do jmpb\n- fork (cost 0.1): do fork\n\nEcology:\n- signal/listen: ephemeral local communication\n\nParent telemetry: {"energy": 80.0, "generation": 3, "id": 7, "tick": 500}\nParent genome (6 instructions): ["harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork"]\n\nWhat you remember about this colony:\n- remembered one\n- remembered two\n\nRules: use only instruction names from the set above; at most 64 instructions; the variant must differ from the parent and must still be able to replicate.\nAnswer with a JSON array of instruction names and nothing else.'


def test_default_variant_renders_byte_identically(fixture_request):
    from grid.proposal import render_prompt
    from grid.variants import get_variant
    out = render_prompt(fixture_request, ["remembered one", "remembered two"], get_variant())
    assert out == EXPECTED
    assert get_variant().id == "v1-sandbox"


def _write_registry(tmp_path: Path, *, default: str, variants: dict) -> Path:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"default": default, "variants": variants}), encoding="utf-8")
    return registry_path


def _write_template(tmp_path: Path, name: str, text: str) -> Path:
    template_path = tmp_path / name
    template_path.write_text(text, encoding="utf-8", newline="\n")
    return template_path


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_entry(template_path: Path, tmp_path: Path, **overrides) -> dict:
    entry = {
        "template_file": template_path.relative_to(tmp_path).as_posix(),
        "sha256": _sha256_of(template_path),
        "temperature": 0.5,
        "max_tokens": 100,
        "recall": True,
        "note": "a test variant",
    }
    entry.update(overrides)
    return entry


def test_load_registry_rejects_mismatched_sha256(tmp_path):
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive}")
    entry = _valid_entry(template_path, tmp_path, sha256="0" * 64)
    registry_path = _write_registry(tmp_path, default="bad-hash", variants={"bad-hash": entry})
    with pytest.raises(VariantError, match="bad-hash"):
        load_registry(registry_path)


def test_load_registry_rejects_missing_template_file(tmp_path):
    from grid.variants import VariantError, load_registry
    entry = {
        "template_file": "does-not-exist.md",
        "sha256": "0" * 64,
        "temperature": 0.5,
        "max_tokens": 100,
        "recall": True,
        "note": "missing template",
    }
    registry_path = _write_registry(tmp_path, default="missing-file", variants={"missing-file": entry})
    with pytest.raises(VariantError, match="missing-file"):
        load_registry(registry_path)


def test_load_registry_rejects_unknown_placeholder(tmp_path):
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive} model={model}")
    entry = _valid_entry(template_path, tmp_path)
    registry_path = _write_registry(tmp_path, default="bad-placeholder", variants={"bad-placeholder": entry})
    with pytest.raises(VariantError, match="bad-placeholder"):
        load_registry(registry_path)


def test_load_registry_rejects_default_naming_absent_variant(tmp_path):
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive}")
    entry = _valid_entry(template_path, tmp_path)
    registry_path = _write_registry(tmp_path, default="nope", variants={"present": entry})
    with pytest.raises(VariantError, match="nope"):
        load_registry(registry_path)


def test_load_registry_accepts_a_template_using_fewer_than_all_placeholders(tmp_path):
    from grid.variants import load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive}, cap {genome_cap}")
    entry = _valid_entry(template_path, tmp_path)
    registry_path = _write_registry(tmp_path, default="fewer", variants={"fewer": entry})
    default_id, variants = load_registry(registry_path)
    assert default_id == "fewer"
    assert variants["fewer"].template.format(
        directive="d", isa_lines="", ecology="", parent="{}",
        genome_count=0, genome_json="[]", memory="", genome_cap=64,
    ) == "hello d, cap 64"


def test_doubled_literal_brace_renders_correctly(tmp_path):
    """.format() treats a single literal brace as a placeholder delimiter and
    raises; a template author must double it ("{{" / "}}") to get one literal
    brace in the output. This is not enforced by the loader (today's shipped
    template has none) -- it is a trap for the next person writing a variant.
    """
    from grid.variants import get_variant, load_registry
    template_path = _write_template(
        tmp_path, "t.md",
        "Note: {{escaped braces}} around directive={directive}",
    )
    entry = _valid_entry(template_path, tmp_path)
    registry_path = _write_registry(tmp_path, default="braces", variants={"braces": entry})
    variant = get_variant(path=registry_path)
    assert variant.id == "braces"
    rendered = variant.template.format(
        directive="GO", isa_lines="", ecology="", parent="{}",
        genome_count=0, genome_json="[]", memory="", genome_cap=64,
    )
    assert rendered == "Note: {escaped braces} around directive=GO"
    # sanity: load_registry validated this template without complaint
    load_registry(registry_path)
