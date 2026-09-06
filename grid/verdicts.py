"""Score every logged proposal against thegrid's fossil record.

thegrid never tags Odin births; the join is its own genome_id, which the relay
computed when it wrote the proposal. Verdict rows are append-only and hash-
chained so a training example can always be traced to a colony state.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("grid.verdicts")

SETTLE_TICKS = 3000
TIERS = ("established", "enduring", "growing", "reproduced", "new")
GENESIS = "0" * 64


def tier_of(births: int, generations: int) -> str:
    if generations >= 100 and births >= 100:
        return "established"
    if generations >= 50 and births >= 25:
        return "enduring"
    if generations >= 10 and births >= 5:
        return "growing"
    if births >= 2:
        return "reproduced"
    return "new"


def is_settled(entry_epoch: int, entry_tick: int, *, now_epoch: int, now_tick: int,
               settle_ticks: int = SETTLE_TICKS) -> bool:
    return now_epoch > entry_epoch or (now_epoch == entry_epoch and now_tick - entry_tick >= settle_ticks)


def canonical(row: dict) -> str:
    return json.dumps({k: v for k, v in row.items() if k != "chain"}, sort_keys=True, separators=(",", ":"))


def chain_hash(prev: str, row: dict) -> str:
    return hashlib.sha256((prev + canonical(row)).encode()).hexdigest()


def verdict_row(entry: dict, stats: dict | None, *, settled_tick: int) -> dict:
    if entry.get("outcome") != "proposed":
        tier, births, gens, life = "rejected", 0, 0, 0
    elif stats is None:
        tier, births, gens, life = "unborn", 0, 0, 0
    else:
        births = int(stats.get("births") or 0)
        max_gen = int(stats.get("max_generation") or 0)
        first_gen = int(stats.get("first_generation") or 0)
        gens = max_gen - first_gen
        life = int(stats.get("last_tick") or 0) - int(stats.get("first_tick") or 0)
        tier = tier_of(births, max_gen)
    return {"request_id": entry["request_id"], "proposal_genome_id": entry.get("proposal_genome_id"),
            "epoch": int(entry.get("epoch", 0)), "settled_tick": settled_tick,
            "births": births, "generations": gens, "lifetime_ticks": life, "tier": tier,
            "fitness": births, "model_alias": entry.get("model_alias", ""), "outcome": entry.get("outcome")}


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def rows(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def head(self) -> str:
        rows = self.rows()
        return rows[-1]["chain"] if rows else GENESIS

    def append(self, row: dict) -> dict:
        full = dict(row, chain=chain_hash(self.head(), row))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(full, sort_keys=True, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return full

    def verify(self) -> bool:
        prev = GENESIS
        for row in self.rows():
            if row.get("chain") != chain_hash(prev, row):
                return False
            prev = row["chain"]
        return True

    def settled_ids(self) -> set[str]:
        return {r["request_id"] for r in self.rows()}


class StatsReader:
    """Reads genome_stats from a SNAPSHOT of thegrid's fossil DB, never the live file.

    thegrid keeps the DB in WAL mode (history.sqlite3-wal beside it on Skywave), so
    a plain file copy would miss every row still in the write-ahead log. The
    snapshot is taken through SQLite's backup API on a read-only connection,
    which reads consistently through the WAL.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.copy_path: str | None = None
        self._db: sqlite3.Connection | None = None

    def __enter__(self) -> "StatsReader":
        fd, self.copy_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        src = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(self.copy_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        self._db = sqlite3.connect(f"file:{self.copy_path}?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        return self

    def __exit__(self, *exc) -> None:
        if self._db is not None:
            self._db.close()
        if self.copy_path and os.path.exists(self.copy_path):
            os.unlink(self.copy_path)

    def stats(self, epoch: int, genome_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT births, first_generation, max_generation, first_tick, last_tick "
            "FROM genome_stats WHERE epoch=? AND genome_id=?", (epoch, genome_id)).fetchone()
        return dict(row) if row else None


def settle_pending(entries: list[dict], ledger: Ledger, reader: StatsReader, *, now_epoch: int,
                   now_tick: int, settle_ticks: int = SETTLE_TICKS) -> list[dict]:
    done = ledger.settled_ids()
    out = []
    for e in entries:
        if e["request_id"] in done:
            continue
        if not is_settled(int(e.get("epoch", 0)), int(e.get("tick", 0)), now_epoch=now_epoch,
                          now_tick=now_tick, settle_ticks=settle_ticks):
            continue
        stats = reader.stats(int(e.get("epoch", 0)), e["proposal_genome_id"]) if e.get("proposal_genome_id") else None
        out.append(ledger.append(verdict_row(e, stats, settled_tick=now_tick)))
        done.add(e["request_id"])
    return out
