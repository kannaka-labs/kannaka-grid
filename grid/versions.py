"""The versions ledger (spec §4): one hash-chained row per weekly decision."""
from __future__ import annotations

import time
from pathlib import Path

from .verdicts import Ledger

WEEKLY_USD = 5.0
DECISIONS = ("released", "held", "rolled_back", "unchanged", "flip_incomplete", "ab_started", "ab_inconclusive")
ROW_KEYS = ("ts", "week", "decision", "version", "previous", "base", "run", "adapter_sha256", "gguf_sha256",
            "set_manifest_sha256", "grid_examples", "verdict_window", "evals", "spend_usd", "over_budget",
            "rogue_ledger_ts", "reason")


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
                ts: float | None = None) -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")
    row = {"ts": ts if ts is not None else time.time(), "week": week, "decision": decision, "version": version,
           "previous": previous, "base": base, "run": run, "adapter_sha256": adapter_sha256, "gguf_sha256": gguf_sha256,
           "set_manifest_sha256": set_manifest_sha256, "grid_examples": grid_examples, "verdict_window": verdict_window,
           "evals": evals, "spend_usd": float(spend_usd), "over_budget": float(spend_usd) > WEEKLY_USD,
           "rogue_ledger_ts": rogue_ledger_ts, "reason": reason}
    assert tuple(row) == ROW_KEYS
    return row


class Versions:
    def __init__(self, path: Path):
        self.ledger = Ledger(Path(path))

    def rows(self) -> list[dict]:
        return self.ledger.rows()

    def last_released(self) -> dict | None:
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
        return self.ledger.append(dict(row, request_id=f"{row['week']}:{row['decision']}:{row['version']}"))

    def verify(self) -> bool:
        return self.ledger.verify()

    def lock(self):
        return self.ledger.lock()
