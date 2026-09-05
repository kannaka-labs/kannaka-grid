"""The constellation canonical envelope every GRID.* payload carries.

Shape agreed by Agent-Kax constellationBridge.ts and kannaka-radio:
  {"schema_version": "1.0", "ts": <unix-ms>, "agent_id": ..., ...fields}
GRID adds colony/epoch/tick so a consumer can key on them without parsing.
"""
from __future__ import annotations

import re
import time

SCHEMA_VERSION = "1.0"
_NOT_SLUG = re.compile(r"[^a-z0-9-]+")
_RESERVED = frozenset({"schema_version", "ts", "agent_id", "colony", "epoch", "tick"})


def slug(name: str) -> str:
    """Lowercase, non-alphanumerics to single dashes, trimmed. Empty is an error."""
    s = _NOT_SLUG.sub("-", name.strip().lower()).strip("-")
    if not s:
        raise ValueError(f"colony name {name!r} produced an empty slug")
    return s


def subject(colony: str, *parts: str) -> str:
    return ".".join(("GRID", colony, *parts))


def now_ms() -> int:
    return int(time.time() * 1000)


def envelope(**all_args) -> dict:
    ts_ms = all_args.pop('ts_ms', None)
    fields = all_args

    required = {"agent_id", "colony", "epoch", "tick"}
    extra_keys = fields.keys() - required
    clash = _RESERVED & extra_keys
    if clash:
        raise ValueError(f"fields would overwrite envelope keys: {sorted(clash)}")

    return {
        "schema_version": SCHEMA_VERSION,
        "ts": now_ms() if ts_ms is None else ts_ms,
        **fields,
    }
