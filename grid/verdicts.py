"""Score every logged proposal against thegrid's fossil record.

thegrid never tags Odin births; the join is its own genome_id, which the relay
computed when it wrote the proposal. Verdict rows are append-only and hash-
chained so a training example can always be traced to a colony state.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
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
# A proposal written near the end of an epoch is often consumed by the first births
# of the next one. Only the very start of that epoch can be our child; later rows
# are somebody else's strain that happens to share the id.
NEXT_EPOCH_GRACE_TICKS = 1000


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


def verdict_row(entry: dict, stats: dict | None, *, settled_tick: int,
                epoch_found: int | None = None, history_sha256: str = "") -> dict:
    entry_epoch, entry_tick = int(entry.get("epoch", 0)), int(entry.get("tick", 0))
    births = gens = life = 0
    if entry.get("outcome") != "proposed":
        tier, epoch_found = "rejected", None
    elif entry_epoch == 0 and entry_tick == 0:
        # The relay's _epoch_tick fallback: /api/state was unreachable, so the
        # (epoch, genome_id) key is a guess. A missing row is then no evidence the
        # child was never born, and a present one is no evidence it was ours.
        tier, epoch_found = "unknown", None
    elif stats is None:
        tier = "unborn"
    else:
        births = int(stats.get("births") or 0)
        gens = int(stats.get("max_generation") or 0) - int(stats.get("first_generation") or 0)
        life = int(stats.get("last_tick") or 0) - int(stats.get("first_tick") or 0)
        # genome_stats is keyed by (epoch, genome_id), so a proposal that recreates a
        # genome already alive would inherit every carrier's births. thegrid's
        # _observe_genome never lowers first_tick and the relay reads its tick before
        # writing the proposal, so a child of THIS proposal has first_tick >= our tick.
        # Epoch ticks restart at 0, so the guard only means anything within our epoch.
        stale = (epoch_found is None or epoch_found == entry_epoch) and \
            int(stats.get("first_tick", 0)) < entry_tick
        tier = "preexisting" if stale else tier_of(births, gens)
    return {"request_id": entry["request_id"], "proposal_genome_id": entry.get("proposal_genome_id"),
            "epoch": entry_epoch, "epoch_found": epoch_found, "settled_tick": settled_tick,
            "births": births, "generations": gens, "lifetime_ticks": life, "tier": tier,
            "fitness": 0 if tier == "preexisting" else births,
            "model_alias": entry.get("model_alias", ""), "outcome": entry.get("outcome"),
            "history_sha256": history_sha256}


class Ledger:
    """The append-only hash chain of verdict rows.

    The head and the settled ids are cached after the first read: a nightly run
    settles thousands of proposals, and re-reading the whole file per append is
    quadratic. `lock()` is what keeps the cache honest against a second writer.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._head: str | None = None
        self._ids: set[str] | None = None
        self._partial_tail_offset: int | None = None
        self._corrupt_line: int | None = None

    def rows(self) -> list[dict]:
        out: list[dict] = []
        self._partial_tail_offset = self._corrupt_line = None
        try:
            with open(self.path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                offset = 0
                for n, raw in enumerate(f, 1):
                    if raw.strip():
                        try:
                            out.append(json.loads(raw))
                        except ValueError:      # includes a bad UTF-8 decode
                            # A row is fsynced with its newline, so only an
                            # unterminated final line can be a torn write. Anything
                            # else — mid-file, or newline-terminated garbage — has
                            # complete rows after it that must not be destroyed.
                            if not raw.endswith(b"\n") and offset + len(raw) == size:
                                log.warning("verdict ledger: torn last line at byte %d (line %d) "
                                            "of %s; it will be dropped on the next append",
                                            offset, n, self.path)
                                self._partial_tail_offset = offset
                            else:
                                log.error("verdict ledger: corrupt row at line %d of %s; "
                                          "refusing to read past it", n, self.path)
                                self._corrupt_line = n
                            break
                    offset += len(raw)
        except OSError:
            out = []
        self._head = out[-1]["chain"] if out else GENESIS
        self._ids = {r["request_id"] for r in out}
        return out

    def head(self) -> str:
        if self._head is None:
            self.rows()
        return self._head

    def _truncate_partial_tail(self) -> None:
        """Drop a half-written last line: rows are fsynced, so it was never a row."""
        if self._head is None:
            self.rows()
        if self._partial_tail_offset is None:
            return
        log.warning("verdict ledger: truncating a partial tail at byte %d of %s",
                    self._partial_tail_offset, self.path)
        with open(self.path, "r+b") as f:
            f.truncate(self._partial_tail_offset)
            f.flush()
            os.fsync(f.fileno())
        self._partial_tail_offset = None

    def append(self, row: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._head is None:
            self.rows()
        if self._corrupt_line is not None:
            # Appending would chain onto a prefix and strand every good row below it.
            raise RuntimeError(f"ledger has a corrupt row at line {self._corrupt_line}; "
                               "refusing to append")
        self._truncate_partial_tail()
        full = dict(row, chain=chain_hash(self.head(), row))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(full, sort_keys=True, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._head = full["chain"]
        self._ids.add(full["request_id"])
        return full

    @contextlib.contextmanager
    def lock(self):
        """Exclusive across processes on Linux (the nightly timer plus a manual run).

        Windows has no flock and is a dev box only, so there it is a no-op.
        """
        if sys.platform != "linux":
            yield
            return
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(f"{self.path}.lock", "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def verify(self) -> bool:
        prev = GENESIS
        rows = self.rows()
        if self._corrupt_line is not None:
            return False        # a prefix that verifies is not a ledger that verifies
        for row in rows:
            if row.get("chain") != chain_hash(prev, row):
                return False
            prev = row["chain"]
        return True

    def settled_ids(self) -> set[str]:
        if self._ids is None:
            self.rows()
        return set(self._ids)


class StatsReader:
    """Reads genome_stats from a SNAPSHOT of thegrid's fossil DB, never the live file.

    thegrid keeps the DB in WAL mode (history.sqlite3-wal beside it on Skywave), so
    a plain file copy would miss every row still in the write-ahead log. The
    snapshot is taken through SQLite's backup API on a read-only connection,
    which reads consistently through the WAL.
    """

    def __init__(self, db_path: Path, tmp_dir: Path | None = None):
        self.db_path = Path(db_path)
        self.tmp_dir = Path(tmp_dir) if tmp_dir is not None else None
        self.copy_path: str | None = None
        self._db: sqlite3.Connection | None = None
        self._sha256: str | None = None

    def __enter__(self) -> "StatsReader":
        if self.tmp_dir is not None:        # /tmp may be a small tmpfs; the fossil DB is not
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, self.copy_path = tempfile.mkstemp(
            suffix=".sqlite3", dir=str(self.tmp_dir) if self.tmp_dir is not None else None)
        os.close(fd)
        try:
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
        except Exception:
            if os.path.exists(self.copy_path):
                os.unlink(self.copy_path)
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self._db is not None:
            self._db.close()
        if self.copy_path and os.path.exists(self.copy_path):
            os.unlink(self.copy_path)

    def snapshot_sha256(self) -> str:
        """Hash of the snapshot this reader scored against — the state to re-derive from.

        Hashing the live file would name a DB the verdicts were never read from;
        the colony writes to it continuously.
        """
        if self._sha256 is None:
            h = hashlib.sha256()
            with open(self.copy_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            self._sha256 = h.hexdigest()
        return self._sha256

    def stats(self, epoch: int, genome_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT births, first_generation, max_generation, first_tick, last_tick "
            "FROM genome_stats WHERE epoch=? AND genome_id=?", (epoch, genome_id)).fetchone()
        return dict(row) if row else None


def _lookup(reader: StatsReader, entry: dict, *, now_epoch: int) -> tuple[dict | None, int | None]:
    """The genome's stats and the epoch they came from, or (None, None).

    thegrid consumes proposal.json at the next qualifying birth, which for a
    proposal written late in an epoch can fall after the epoch rolled over. Only
    the first NEXT_EPOCH_GRACE_TICKS of that epoch could be our child.
    """
    gid = entry.get("proposal_genome_id")
    if not gid:
        return None, None
    entry_epoch = int(entry.get("epoch", 0))
    stats = reader.stats(entry_epoch, gid)
    if stats is not None:
        return stats, entry_epoch
    if now_epoch > entry_epoch:
        nxt = reader.stats(entry_epoch + 1, gid)
        # first_tick 0 is a founder or immigrant: start_epoch observes the seeded
        # population at observed_tick=0, and those genomes are absent from the old
        # epoch by construction, which is exactly what got us here. A born child is
        # observed at its birth tick, which is at least 1.
        if nxt is not None and 0 < int(nxt.get("first_tick", 0)) <= NEXT_EPOCH_GRACE_TICKS:
            return nxt, entry_epoch + 1
    return None, None


def settle_pending(entries: list[dict], ledger: Ledger, reader: StatsReader, *, now_epoch: int,
                   now_tick: int, settle_ticks: int = SETTLE_TICKS) -> list[dict]:
    done = ledger.settled_ids()
    hist = reader.snapshot_sha256()
    out = []
    for e in entries:
        if e["request_id"] in done:
            continue
        if not is_settled(int(e.get("epoch", 0)), int(e.get("tick", 0)), now_epoch=now_epoch,
                          now_tick=now_tick, settle_ticks=settle_ticks):
            continue
        stats, found = _lookup(reader, e, now_epoch=now_epoch)
        out.append(ledger.append(verdict_row(e, stats, settled_tick=now_tick, epoch_found=found,
                                             history_sha256=hist)))
        done.add(e["request_id"])
    return out


async def run_once(*, verdicts_dir: Path, history_db: Path, state: dict, bus=None, colony: str,
                   agent_id: str, settle_ticks: int = SETTLE_TICKS) -> list[dict]:
    from .envelope import envelope, subject
    from .proposal_log import ProposalLog

    entries = ProposalLog(Path(verdicts_dir)).read_all()
    ledger = Ledger(Path(verdicts_dir) / "verdicts.jsonl")
    now_epoch, now_tick = int(state.get("epoch", 0)), int(state.get("tick", 0))
    # One writer at a time: the nightly timer and a manual run must not interleave
    # appends, and the cached head is only safe inside the lock.
    with ledger.lock():
        with StatsReader(Path(history_db), tmp_dir=Path(verdicts_dir)) as reader:
            rows = settle_pending(entries, ledger, reader, now_epoch=now_epoch, now_tick=now_tick,
                                  settle_ticks=settle_ticks)
        if bus is not None:
            for row in rows:
                fields = {k: v for k, v in row.items() if k not in ("epoch",)}
                try:
                    await bus.publish(subject(colony, "verdict"),
                                      envelope(agent_id=agent_id, colony=colony, epoch=row["epoch"],
                                               tick=now_tick, kind="verdict", **fields))
                except Exception as e:  # the row is already in the ledger; the bus is best-effort
                    log.error("verdict publish failed for %s: %s", row["request_id"], e)
    log.info("settled %d proposals (epoch %d tick %d)", len(rows), now_epoch, now_tick)
    return rows


async def _main_async(a: argparse.Namespace) -> int:
    import httpx
    from .natsio import Bus

    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{a.colony_url}/api/state")
        r.raise_for_status()
        state = r.json()
    bus = None
    if not a.no_publish:
        bus = Bus(os.environ.get("NATS_URL", "nats://swarm.ninja-portal.com:4222"),
                  user=os.environ.get("NATS_USER") or None, password=os.environ.get("NATS_PASSWORD") or None)
        try:
            await bus.connect()
        except Exception as e:
            log.error("nats unavailable, verdicts will be written but not published: %s", e)
            bus = None
    try:
        rows = await run_once(verdicts_dir=Path(a.verdicts_dir), history_db=Path(a.history_db), state=state,
                              bus=bus, colony=a.colony, agent_id=a.agent_id, settle_ticks=a.settle_ticks)
    finally:
        if bus is not None:
            await bus.close()
    print(json.dumps({"settled": len(rows), "by_tier": {t: sum(1 for r in rows if r["tier"] == t)
                                                        for t in set(r["tier"] for r in rows)}}))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    env = os.environ.get
    colony = env("COLONY_SLUG", "colony-one")
    ap = argparse.ArgumentParser(description="Settle logged Odin proposals against thegrid's fossil record.")
    ap.add_argument("--verdicts-dir", default=env("VERDICTS_DIR", f"/srv/grid/{colony}/verdicts"))
    ap.add_argument("--history-db", default=env("HISTORY_DB", f"/srv/grid/{colony}/state/history.sqlite3"))
    ap.add_argument("--colony-url", default=env("COLONY_URL", "http://127.0.0.1:8787").rstrip("/"))
    ap.add_argument("--colony", default=colony)
    ap.add_argument("--agent-id", default=env("MIND_AGENT_ID", f"grid-{colony}"))
    ap.add_argument("--settle-ticks", type=int, default=int(env("SETTLE_TICKS", str(SETTLE_TICKS))))
    ap.add_argument("--no-publish", action="store_true")
    a = ap.parse_args(argv)
    return asyncio.run(_main_async(a))


if __name__ == "__main__":
    sys.exit(main())
