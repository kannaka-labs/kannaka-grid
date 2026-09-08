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


# --- Fix round 1 -------------------------------------------------------
#
# I1: a template can pass the placeholder check and still fail to render.
# load_registry now smoke-renders every template with dummy values so both
# shapes below are load-time VariantErrors, not a runtime IndexError/KeyError
# swallowed by the relay's poll loop.

def test_load_registry_rejects_bare_positional_placeholder(tmp_path):
    """A bare "{}" is auto-numbered, not a *named* field, so the placeholder
    scan never sees it as unknown -- only a smoke-render catches it."""
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive} {}")
    entry = _valid_entry(template_path, tmp_path)
    registry_path = _write_registry(tmp_path, default="bare-brace", variants={"bare-brace": entry})
    with pytest.raises(VariantError, match="bare-brace"):
        load_registry(registry_path)


def test_load_registry_rejects_nested_format_spec_naming_an_unknown_field(tmp_path):
    """string.Formatter().parse() does not recurse into a nested format spec,
    so "{directive:{width}}" reports only `directive` as used; only a
    smoke-render discovers that `width` is never supplied."""
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive:{width}}")
    entry = _valid_entry(template_path, tmp_path)
    registry_path = _write_registry(tmp_path, default="nested-unknown", variants={"nested-unknown": entry})
    with pytest.raises(VariantError, match="nested-unknown"):
        load_registry(registry_path)


# I3: every structural failure must be a VariantError naming the registry
# file (and the variant, where one is identifiable) -- never a bare KeyError,
# AttributeError, or json.JSONDecodeError.

def test_load_registry_rejects_malformed_json(tmp_path):
    from grid.variants import VariantError, load_registry
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(VariantError, match=r"registry\.json"):
        load_registry(registry_path)


def test_load_registry_rejects_registry_missing_variants_key(tmp_path):
    from grid.variants import VariantError, load_registry
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"default": "v1"}), encoding="utf-8")
    with pytest.raises(VariantError, match=r"registry\.json"):
        load_registry(registry_path)


def test_load_registry_rejects_non_dict_variants(tmp_path):
    from grid.variants import VariantError, load_registry
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"default": "v1", "variants": ["v1"]}), encoding="utf-8")
    with pytest.raises(VariantError, match=r"registry\.json"):
        load_registry(registry_path)


def test_load_registry_rejects_entry_missing_a_required_field(tmp_path):
    """A structural error inside one entry (here: no `temperature` key) must
    still come out as a VariantError naming that variant, not a raw KeyError
    that a caller's `except VariantError` would not catch."""
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive}")
    entry = _valid_entry(template_path, tmp_path)
    del entry["temperature"]
    registry_path = _write_registry(tmp_path, default="no-temp", variants={"no-temp": entry})
    with pytest.raises(VariantError, match="no-temp"):
        load_registry(registry_path)


def test_load_registry_rejects_entry_that_is_not_an_object(tmp_path):
    from grid.variants import VariantError, load_registry
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"default": "v1", "variants": {"v1": "not-an-object"}}), encoding="utf-8")
    with pytest.raises(VariantError, match="v1"):
        load_registry(registry_path)


# I5: template_file must stay inside the registry's own directory.

def test_load_registry_rejects_absolute_template_path(tmp_path):
    from grid.variants import VariantError, load_registry
    outside = tmp_path.parent / "outside-abs.md"
    outside.write_text("hello {directive}", encoding="utf-8", newline="\n")
    entry = _valid_entry(outside, tmp_path.parent, template_file=str(outside))
    registry_path = _write_registry(tmp_path, default="abs-path", variants={"abs-path": entry})
    with pytest.raises(VariantError, match="abs-path"):
        load_registry(registry_path)
    outside.unlink()


def test_load_registry_rejects_path_traversal_template_file(tmp_path):
    from grid.variants import VariantError, load_registry
    outside = tmp_path.parent / "outside-rel.md"
    outside.write_text("hello {directive}", encoding="utf-8", newline="\n")
    entry = _valid_entry(outside, tmp_path.parent)
    entry["template_file"] = f"../{outside.name}"
    registry_path = _write_registry(tmp_path, default="traversal", variants={"traversal": entry})
    with pytest.raises(VariantError, match="traversal"):
        load_registry(registry_path)
    outside.unlink()


# I4: the three type checks below had zero coverage; deleting any one of them
# left the whole suite green. Also: render_prompt's substitution keys must
# never drift from PLACEHOLDERS, or a new placeholder silently KeyErrors at
# render time inside the relay's poll loop.

def test_load_registry_rejects_temperature_of_wrong_type(tmp_path):
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive}")
    entry = _valid_entry(template_path, tmp_path, temperature=1)  # int, not float
    registry_path = _write_registry(tmp_path, default="bad-temp", variants={"bad-temp": entry})
    with pytest.raises(VariantError, match="bad-temp"):
        load_registry(registry_path)


def test_load_registry_rejects_max_tokens_of_wrong_type(tmp_path):
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive}")
    entry = _valid_entry(template_path, tmp_path, max_tokens=100.0)  # float, not int
    registry_path = _write_registry(tmp_path, default="bad-tokens", variants={"bad-tokens": entry})
    with pytest.raises(VariantError, match="bad-tokens"):
        load_registry(registry_path)


def test_load_registry_rejects_recall_of_wrong_type(tmp_path):
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive}")
    entry = _valid_entry(template_path, tmp_path, recall="true")  # string, not bool
    registry_path = _write_registry(tmp_path, default="bad-recall", variants={"bad-recall": entry})
    with pytest.raises(VariantError, match="bad-recall"):
        load_registry(registry_path)


def test_render_prompt_substitution_keys_match_placeholders(fixture_request):
    """Ties render_prompt's .format() kwargs to PLACEHOLDERS so the two
    cannot silently drift apart: add a ninth placeholder without a matching
    kwarg (or vice versa) and this fails instead of surfacing as a render-time
    KeyError in production."""
    from grid.proposal import render_prompt
    from grid.variants import PLACEHOLDERS, Variant

    captured: dict = {}

    class _RecordingTemplate:
        def format(self, **kwargs):
            captured.update(kwargs)
            return ""

    variant = Variant(
        id="probe", template=_RecordingTemplate(), sha256="0" * 64,
        temperature=0.1, max_tokens=1, recall=True, note="probe",
    )
    render_prompt(fixture_request, ["x"], variant)
    assert set(captured) == PLACEHOLDERS


# Minors folded in.

def test_load_registry_rejects_duplicate_variant_keys(tmp_path):
    """M1: json.loads keeps the last of two duplicate object keys silently;
    the registry must reject that instead of serving one template while an
    operator reads another entry's note."""
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive}")
    good_entry_json = json.dumps(_valid_entry(template_path, tmp_path, note="first"))
    other_entry_json = json.dumps(_valid_entry(template_path, tmp_path, note="second"))
    registry_path = tmp_path / "registry.json"
    # Hand-build the JSON text so the same key "dup" appears twice.
    text = '{"default": "dup", "variants": {"dup": %s, "dup": %s}}' % (good_entry_json, other_entry_json)
    registry_path.write_text(text, encoding="utf-8")
    with pytest.raises(VariantError, match="dup"):
        load_registry(registry_path)


def test_load_registry_rejects_empty_template(tmp_path):
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "")
    entry = _valid_entry(template_path, tmp_path)
    registry_path = _write_registry(tmp_path, default="empty", variants={"empty": entry})
    with pytest.raises(VariantError, match="empty"):
        load_registry(registry_path)


def test_load_registry_rejects_non_string_note(tmp_path):
    from grid.variants import VariantError, load_registry
    template_path = _write_template(tmp_path, "t.md", "hello {directive}")
    entry = _valid_entry(template_path, tmp_path, note={"not": "a string"})
    registry_path = _write_registry(tmp_path, default="bad-note", variants={"bad-note": entry})
    with pytest.raises(VariantError, match="bad-note"):
        load_registry(registry_path)
