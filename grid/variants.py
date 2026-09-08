"""The prompt variant registry.

The relay used to have one hardcoded prompt. This module loads a named,
validated set of prompt-variant templates instead, so later work can A/B two
prompts against each other on live traffic (see grid/data/prompt_variants.json
and grid/data/prompts/*.md).

The loader fails closed: a missing template file, a sha256 that does not
match its file, an unknown placeholder, a template that cannot render, a
template path that escapes the registry directory, or a `default` naming an
absent variant is a VariantError at load time. There is no warning path and
no fallback to the default — a variant silently edited in place would
invalidate a running A/B, and a fallback would hide that it happened. Every
failure to load is a VariantError naming the registry file and, where one is
identifiable, the offending variant; nothing escapes as a raw KeyError,
AttributeError, or json.JSONDecodeError that a caller's `except VariantError`
would miss.
"""
from __future__ import annotations

import hashlib
import json
import string
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

DATA_DIR = Path(str(files("grid.data")))
DEFAULT_REGISTRY_PATH = DATA_DIR / "prompt_variants.json"

# The only placeholders render_prompt knows how to fill. A template may use
# fewer of these (a variant that omits the memory block is legitimate); using
# one that is not in this set is a load-time error.
PLACEHOLDERS = frozenset({
    "directive", "isa_lines", "ecology", "parent",
    "genome_count", "genome_json", "memory", "genome_cap",
})

# Dummy values used to smoke-render a template at load time, one per known
# placeholder. Enumerating {placeholder} names with string.Formatter().parse()
# only sees the top level of a replacement field: it misses a bare "{}"
# (auto-numbered, so it never becomes a *named* field at all) and it does not
# recurse into a nested format spec like "{directive:{width}}" (the inner
# "width" lookup only happens inside .format() itself). Both load cleanly
# under the placeholder check and then raise IndexError/KeyError at render
# time. Actually rendering once, here, with every name bound, converts those
# into a VariantError before the relay ever sees the template.
_SMOKE_VALUES: dict[str, object] = {
    "directive": "smoke-directive",
    "isa_lines": "- smoke (cost 0): smoke",
    "ecology": "- smoke: smoke",
    "parent": "{}",
    "genome_count": 0,
    "genome_json": "[]",
    "memory": "- smoke",
    "genome_cap": 64,
}


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
    """The set of {placeholder} names a template references at its top level.

    Uses string.Formatter().parse() rather than a regex so that doubled
    literal braces ("{{" / "}}") are correctly ignored: parse() yields
    field_name=None for the literal text between them. This does NOT see a
    bare "{}" (auto-numbered fields report field_name == "", not a name) or
    a name buried in a nested format spec; load_registry's smoke-render
    catches both of those separately.
    """
    names = set()
    for _literal_text, field_name, _format_spec, _conversion in string.Formatter().parse(template):
        if field_name:
            names.add(field_name)
    return names


def _dup_key_hook(registry_path: Path):
    """An object_pairs_hook that fails closed on a repeated JSON object key.

    Plain json.loads silently keeps the last of two duplicate keys, so a
    registry with the same variant id pasted twice would serve one template
    while an operator reading the file sees the other entry's note.
    """
    def hook(pairs: list[tuple[str, object]]) -> dict:
        seen: dict = {}
        for key, value in pairs:
            if key in seen:
                raise VariantError(f"{registry_path}: duplicate key {key!r} in registry JSON")
            seen[key] = value
        return seen
    return hook


def _load_variant(variant_id: str, entry: object, registry_dir: Path) -> Variant:
    if not isinstance(entry, dict):
        raise VariantError(f"variant {variant_id!r}: entry must be a JSON object, got {type(entry).__name__}")

    template_file = entry["template_file"]
    if not isinstance(template_file, str):
        raise VariantError(f"variant {variant_id!r}: template_file must be a string, got {template_file!r}")

    raw_path = Path(template_file)
    if raw_path.is_absolute():
        raise VariantError(f"variant {variant_id!r}: template_file must be a relative path, got {template_file!r}")

    registry_dir_resolved = registry_dir.resolve()
    template_path = (registry_dir / raw_path).resolve()
    try:
        template_path.relative_to(registry_dir_resolved)
    except ValueError:
        raise VariantError(
            f"variant {variant_id!r}: template_file escapes the registry directory: {template_file!r}"
        ) from None

    if not template_path.is_file():
        raise VariantError(f"variant {variant_id!r}: template file not found: {template_path}")

    template_bytes = template_path.read_bytes()
    if not template_bytes:
        raise VariantError(f"variant {variant_id!r}: template file is empty: {template_path}")

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

    try:
        template.format(**_SMOKE_VALUES)
    except Exception as e:
        raise VariantError(
            f"variant {variant_id!r}: template {template_path} does not render: {type(e).__name__}: {e}"
        ) from e

    temperature = entry["temperature"]
    if not isinstance(temperature, float):
        raise VariantError(f"variant {variant_id!r}: temperature must be a float, got {temperature!r}")

    max_tokens = entry["max_tokens"]
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
        raise VariantError(f"variant {variant_id!r}: max_tokens must be an int, got {max_tokens!r}")

    recall = entry["recall"]
    if not isinstance(recall, bool):
        raise VariantError(f"variant {variant_id!r}: recall must be a bool, got {recall!r}")

    note = entry["note"]
    if not isinstance(note, str):
        raise VariantError(f"variant {variant_id!r}: note must be a string, got {note!r}")

    return Variant(
        id=variant_id,
        template=template,
        sha256=actual_sha256,
        temperature=temperature,
        max_tokens=max_tokens,
        recall=recall,
        note=note,
    )


def load_registry(path: Path | None = None) -> tuple[str, dict[str, Variant]]:
    """Read the registry, fail closed.

    A bad entry is an exception at load time: a variant silently edited in
    place would invalidate a running A/B, and a fallback to the default
    would hide it. Every failure -- malformed JSON, a missing or wrong-typed
    top-level key, a non-object `variants`, a non-object entry, a missing or
    wrong-typed field inside one, a path that escapes the registry directory,
    a template that fails to render -- is a VariantError naming the registry
    file and, where one is identifiable, the offending variant.
    """
    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    registry_dir = registry_path.parent

    try:
        text = registry_path.read_text(encoding="utf-8")
    except OSError as e:
        raise VariantError(f"{registry_path}: cannot read registry: {e}") from e

    try:
        raw = json.loads(text, object_pairs_hook=_dup_key_hook(registry_path))
    except json.JSONDecodeError as e:
        raise VariantError(f"{registry_path}: not valid JSON: {e}") from e

    if not isinstance(raw, dict):
        raise VariantError(f"{registry_path}: registry must be a JSON object, got {type(raw).__name__}")

    try:
        default_id = raw["default"]
    except KeyError:
        raise VariantError(f"{registry_path}: missing 'default'") from None

    try:
        raw_variants = raw["variants"]
    except KeyError:
        raise VariantError(f"{registry_path}: missing 'variants'") from None
    if not isinstance(raw_variants, dict):
        raise VariantError(f"{registry_path}: 'variants' must be a JSON object, got {type(raw_variants).__name__}")

    variants: dict[str, Variant] = {}
    for variant_id, entry in raw_variants.items():
        try:
            variants[variant_id] = _load_variant(variant_id, entry, registry_dir)
        except VariantError:
            raise
        except Exception as e:
            raise VariantError(f"variant {variant_id!r}: {type(e).__name__}: {e}") from e

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
