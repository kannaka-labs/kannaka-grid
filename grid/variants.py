"""The prompt variant registry.

The relay used to have one hardcoded prompt. This module loads a named,
validated set of prompt-variant templates instead, so later work can A/B two
prompts against each other on live traffic (see grid/data/prompt_variants.json
and grid/data/prompts/*.md).

The loader fails closed: a missing template file, a sha256 that does not
match its file, an unknown placeholder, or a `default` naming an absent
variant is a VariantError at load time. There is no warning path and no
fallback to the default — a variant silently edited in place would invalidate
a running A/B, and a fallback would hide that it happened.
"""
from __future__ import annotations

import hashlib
import json
import string
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_REGISTRY_PATH = DATA_DIR / "prompt_variants.json"

# The only placeholders render_prompt knows how to fill. A template may use
# fewer of these (a variant that omits the memory block is legitimate); using
# one that is not in this set is a load-time error.
PLACEHOLDERS = frozenset({
    "directive", "isa_lines", "ecology", "parent",
    "genome_count", "genome_json", "memory", "genome_cap",
})


class VariantError(ValueError):
    """A prompt-variant registry entry is malformed. Raised at load time."""


@dataclass(frozen=True)
class Variant:
    id: str
    template: str
    sha256: str
    temperature: float
    max_tokens: int
    recall: bool
    note: str


def _template_field_names(template: str) -> set[str]:
    """The set of {placeholder} names a template references.

    Uses string.Formatter().parse() rather than a regex so that doubled
    literal braces ("{{" / "}}") are correctly ignored: parse() yields
    field_name=None for the literal text between them.
    """
    names = set()
    for _literal_text, field_name, _format_spec, _conversion in string.Formatter().parse(template):
        if field_name:
            names.add(field_name)
    return names


def load_registry(path: Path | None = None) -> tuple[str, dict[str, Variant]]:
    """Read the registry, fail closed.

    A bad entry is an exception at load time: a variant silently edited in
    place would invalidate a running A/B, and a fallback to the default
    would hide it.
    """
    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    default_id = raw["default"]

    variants: dict[str, Variant] = {}
    for variant_id, entry in raw["variants"].items():
        template_path = registry_path.parent / entry["template_file"]
        if not template_path.is_file():
            raise VariantError(f"variant {variant_id!r}: template file not found: {template_path}")

        template_bytes = template_path.read_bytes()
        actual_sha256 = hashlib.sha256(template_bytes).hexdigest()
        expected_sha256 = entry["sha256"]
        if actual_sha256 != expected_sha256:
            raise VariantError(
                f"variant {variant_id!r}: sha256 mismatch for {template_path} "
                f"(registry says {expected_sha256}, file hashes to {actual_sha256})"
            )

        template = template_bytes.decode("utf-8")
        unknown = sorted(_template_field_names(template) - PLACEHOLDERS)
        if unknown:
            raise VariantError(f"variant {variant_id!r}: unknown placeholder(s) {unknown} in {template_path}")

        temperature = entry["temperature"]
        if not isinstance(temperature, float):
            raise VariantError(f"variant {variant_id!r}: temperature must be a float, got {temperature!r}")

        max_tokens = entry["max_tokens"]
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
            raise VariantError(f"variant {variant_id!r}: max_tokens must be an int, got {max_tokens!r}")

        recall = entry["recall"]
        if not isinstance(recall, bool):
            raise VariantError(f"variant {variant_id!r}: recall must be a bool, got {recall!r}")

        variants[variant_id] = Variant(
            id=variant_id,
            template=template,
            sha256=actual_sha256,
            temperature=temperature,
            max_tokens=max_tokens,
            recall=recall,
            note=str(entry.get("note", "")),
        )

    if default_id not in variants:
        raise VariantError(f"default variant {default_id!r} is not in the registry")

    return default_id, variants


def get_variant(variant_id: str | None = None, *, path: Path | None = None) -> Variant:
    """Look up one variant by id, or the registry default when variant_id is None."""
    default_id, variants = load_registry(path)
    if variant_id is None:
        variant_id = default_id
    try:
        return variants[variant_id]
    except KeyError:
        raise VariantError(f"unknown variant {variant_id!r}") from None
