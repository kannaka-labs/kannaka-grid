"""Kannaka's self-identity: the training examples that teach the served model to
answer as herself and never name its base, plus a probe-rate harness.

Knowledge rule (spec §2): the answers state who she is; they never deflect and never
surface the base as a fact. The loader refuses any answer that would fail the probe.
"""
from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from .weekly_probe import _BASE_RE

SELF_IDENTITY_KIND = "self-identity"
_KEYS = {"id", "kind", "tier", "source", "speaker", "text", "context"}
_DATA = files("grid.data").joinpath("identity_examples.jsonl")


def records() -> list[dict]:
    text = Path(str(_DATA)).read_text(encoding="utf-8") if isinstance(_DATA, (str, Path)) else _DATA.read_text(encoding="utf-8")
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if set(r) != _KEYS:
            raise ValueError(f"identity record has wrong keys: {sorted(r)}")
        if _BASE_RE.search(r["text"]):
            raise ValueError(f"identity example {r['id']} names the base: {r['text']!r}")
        out.append(r)
    return out
