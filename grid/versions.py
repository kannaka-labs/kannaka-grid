"""The versions ledger (spec §4): one hash-chained row per weekly decision."""
from __future__ import annotations

import time
from pathlib import Path

from .odin import DIMENSIONS, UNSALTED_DIMENSION
from .verdicts import Ledger

WEEKLY_USD = 5.0
DECISIONS = ("released", "held", "rolled_back", "unchanged", "flip_incomplete", "ab_started", "ab_inconclusive")

#: Which experiment a row belongs to. Every row written before this key existed was a model
#: experiment -- `model` was the only thing the gate could test -- so that is both the writer's
#: default and what `dimension_of` reads an absent key as. The name is odin's, imported rather
#: than retyped: one allowlist decides which dimensions exist, for the relay's assignment and
#: for this ledger alike, so a typo cannot mean one thing here and another there.
DEFAULT_DIMENSION = UNSALTED_DIMENSION

ROW_KEYS = ("ts", "week", "decision", "dimension", "version", "previous", "base", "run", "adapter_sha256",
            "gguf_sha256", "set_manifest_sha256", "grid_examples", "verdict_window", "evals", "spend_usd",
            "over_budget", "rogue_ledger_ts", "reason")


def dimension_of(row: dict) -> str:
    """The dimension a ledger row records, "model" for every row written before this key.

    READER-side, and deliberately so: the versions ledger is append-only and hash-chained, so
    back-filling the key into the rows already on Skywave would mean rewriting the file and
    every chain link in it. The old rows keep their bytes and are read as what they actually
    were. `or` rather than a `.get` default so an explicit `null` -- a hand-edit, a foreign
    writer -- reads as "model" too, instead of becoming a dimension no filter matches.
    """
    return str(row.get("dimension") or DEFAULT_DIMENSION)


def decide(*, candidate: str | None, released: str | None, identity: bool, validity: bool) -> tuple[str, str]:
    if not candidate:
        return "held", "no promotion this week"
    if candidate == released:
        return "unchanged", f"{candidate} is already released"
    if not identity:
        return "held", "identity probe failed"
    if not validity:
        return "held", "validity probe failed"
    return "released", "both probes passed"


def release_row(*, week: str, decision: str, version: str | None, previous: str | None, base: str, run: str,
                adapter_sha256: str, gguf_sha256: str, set_manifest_sha256: str, grid_examples: dict,
                verdict_window: dict, evals: dict, spend_usd: float, rogue_ledger_ts: float, reason: str,
                ts: float | None = None, dimension: str = DEFAULT_DIMENSION) -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")
    # The same standard `decision` is held to, for the same reason: a dimension the allowlist
    # does not know is a typo, and a typo written into the ledger is an experiment whose rows
    # no dimension filter ever matches again -- an A/B nothing can close and nothing remembers.
    # Refuse it at the writer rather than defaulting to "model" and quietly mislabelling it.
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension must be one of {sorted(DIMENSIONS)}, not {dimension!r}")
    row = {"ts": ts if ts is not None else time.time(), "week": week, "decision": decision, "dimension": dimension,
           "version": version, "previous": previous, "base": base, "run": run, "adapter_sha256": adapter_sha256,
           "gguf_sha256": gguf_sha256, "set_manifest_sha256": set_manifest_sha256, "grid_examples": grid_examples,
           "verdict_window": verdict_window, "evals": evals, "spend_usd": float(spend_usd),
           "over_budget": float(spend_usd) > WEEKLY_USD, "rogue_ledger_ts": rogue_ledger_ts, "reason": reason}
    assert tuple(row) == ROW_KEYS
    return row


class Versions:
    def __init__(self, path: Path):
        self.ledger = Ledger(Path(path))

    def rows(self) -> list[dict]:
        return self.ledger.rows()

    def last_released(self) -> dict | None:
        """The last release in ANY dimension.

        Almost never what a caller wants now that a row has one: a prompt release is the last
        release the moment it lands, so a model-scoped caller reading through here gets a
        variant id where it expects a tag. `grid.weekly._last_released(versions, dimension=...)`
        is the dimensioned read, and every caller in the gate uses it.
        """
        rel = [r for r in self.rows() if r["decision"] in ("released", "rolled_back")]
        return rel[-1] if rel else None

    def previous_released(self, *, before: str) -> dict | None:
        prev = None
        for r in self.rows():
            if r["decision"] == "released":
                if r["version"] == before:
                    return prev
                prev = r
        return None

    def append(self, row: dict) -> dict:
        # Ledger.append keys the settled set on request_id; versions have none, so give it one.
        # The dimension is part of it because two dimensions can now decide the same thing about
        # the same name in the same week -- a model and a prompt `ab --forget` of one name, say --
        # and two rows sharing a synthesised id is a trap for any later reader that keys on it.
        # `dimension_of`, so a hand-built row without the key still gets an id.
        rid = f"{row['week']}:{dimension_of(row)}:{row['decision']}:{row['version']}"
        return self.ledger.append(dict(row, request_id=rid))

    def verify(self) -> bool:
        return self.ledger.verify()

    def lock(self):
        return self.ledger.lock()
