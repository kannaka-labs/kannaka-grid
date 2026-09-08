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

REPLY_CAP = 2000        # a rejected reply is a training negative, not a transcript


def variant_of(entry: dict) -> str:
    """The prompt arm a log entry belongs to, "" when the row predates the registry.

    Every reader must come through here. Rows written before the prompt registry have
    no `prompt_variant` key at all (not an empty one), so `entry["prompt_variant"]`
    raises KeyError on the existing corpus. `or ""` additionally normalises a null,
    which json.loads yields for a JSON `null`.
    """
    return str(entry.get("prompt_variant") or "")


def variant_sha256_of(entry: dict) -> str:
    """The sha256 of that arm's TEMPLATE, "" when the row predates the registry.

    Not to be confused with the entry's `prompt_sha256`, which hashes the rendered
    prompt of one request and pairs with `prompt_path`.
    """
    return str(entry.get("prompt_variant_sha256") or "")


def proposal_entry(*, req: Request, proposal: list[str] | None, outcome: str, reason: str | None,
                   epoch: int, tick: int, colony: str, mind: str, model_alias: str,
                   prompt: str, ts_ms: int, reply: str | None = None,
                   prompt_variant: str = "", prompt_variant_sha256: str = "") -> dict:
    """The `prompt_variant*` defaults make this callable without them; they say NOTHING
    about older log lines. A row written before the prompt registry existed does not
    carry these keys as "" -- it does not carry them AT ALL, across the whole existing
    Skywave corpus. Read them with variant_of()/variant_sha256_of() below, never by
    indexing, or the first real corpus a reader touches raises KeyError."""
    isa = req.isa
    return {
        "ts": ts_ms, "colony": colony, "epoch": epoch, "tick": tick,
        "request_id": req.request_id, "mind": mind, "model_alias": model_alias,
        "parent_genome": list(req.genome), "parent_genome_id": genome_id_of(req.genome, isa),
        "proposal": list(proposal) if proposal is not None else None,
        "proposal_genome_id": genome_id_of(proposal, isa) if proposal is not None else None,
        "outcome": outcome, "reason": reason,
        # What she actually said. A rejected reply has no genome, so the reply IS the
        # negative example; without it the corpus can only say that something failed.
        "reply": reply[:REPLY_CAP] if reply is not None else None,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_path": f"prompts/{req.request_id}.txt",
        # Which prompt variant was asked. NOTE the two hashes are different things and
        # are named apart on purpose: `prompt_sha256` above hashes the RENDERED prompt of
        # this one request and pairs with `prompt_path`, while this one hashes the
        # variant's TEMPLATE, so every row from the same arm shares it. Overloading the
        # first name would have destroyed the input-side integrity check the corpus uses.
        "prompt_variant": prompt_variant,
        "prompt_variant_sha256": prompt_variant_sha256,
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
