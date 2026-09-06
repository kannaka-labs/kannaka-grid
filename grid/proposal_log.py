"""Every proposal the relay makes, with the prompt it saw — the dataset's input side.

Append-only JSONL beside prompt files. Writing never raises into the relay: a
proposal must still land even if the log cannot be written.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from .proposal import Request, genome_id_of

log = logging.getLogger("grid.proposal_log")


def proposal_entry(*, req: Request, proposal: list[str] | None, outcome: str, reason: str | None,
                   epoch: int, tick: int, colony: str, mind: str, model_alias: str,
                   prompt: str, ts_ms: int) -> dict:
    isa = req.isa
    return {
        "ts": ts_ms, "colony": colony, "epoch": epoch, "tick": tick,
        "request_id": req.request_id, "mind": mind, "model_alias": model_alias,
        "parent_genome": list(req.genome), "parent_genome_id": genome_id_of(req.genome, isa),
        "proposal": list(proposal) if proposal is not None else None,
        "proposal_genome_id": genome_id_of(proposal, isa) if proposal is not None else None,
        "outcome": outcome, "reason": reason,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_path": f"prompts/{req.request_id}.txt",
    }


class ProposalLog:
    def __init__(self, dir: Path):
        self.dir = Path(dir)
        self.path = self.dir / "proposals.jsonl"

    def record(self, entry: dict, prompt: str) -> bool:
        try:
            (self.dir / "prompts").mkdir(parents=True, exist_ok=True)
            with open(self.dir / entry["prompt_path"], "w", encoding="utf-8") as p:
                p.write(prompt)
                p.flush()
                os.fsync(p.fileno())
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as e:      # the proposal still lands; only the record is lost
            log.warning("proposal log write failed for %s: %s", entry.get("request_id"), e)
            return False

    def read_all(self) -> list[dict]:
        out: list[dict] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.warning("proposal log: stopping at unreadable line %d (partial write?)", n)
                        break
        except OSError:
            return []
        return out
