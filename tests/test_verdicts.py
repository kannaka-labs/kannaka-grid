import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from grid.verdicts import (SETTLE_TICKS, Ledger, StatsReader, canonical, chain_hash, is_settled,
                           settle_pending, tier_of, verdict_row)


def make_db(path: Path, rows: list[tuple]) -> Path:
    """genome_stats exactly as thegrid's history.py creates it."""
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE genome_stats (epoch INTEGER NOT NULL, genome_id TEXT NOT NULL,
        births INTEGER NOT NULL DEFAULT 0, starvation_deaths INTEGER NOT NULL DEFAULT 0,
        senescence_deaths INTEGER NOT NULL DEFAULT 0, first_generation INTEGER,
        max_generation INTEGER NOT NULL DEFAULT 0, first_tick INTEGER NOT NULL, last_tick INTEGER NOT NULL,
        PRIMARY KEY (epoch, genome_id))""")
    db.executemany("INSERT INTO genome_stats VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit(); db.close()
    return path


def entry(request_id="r1", *, epoch=1, tick=100, gid="g1", outcome="proposed", alias="a") -> dict:
    return {"request_id": request_id, "epoch": epoch, "tick": tick, "proposal_genome_id": gid if outcome == "proposed" else None,
            "outcome": outcome, "model_alias": alias, "reason": None if outcome == "proposed" else "bad"}


def test_tiers_match_thegrid():
    assert tier_of(150, 120) == "established"
    assert tier_of(30, 60) == "enduring"
    assert tier_of(5, 10) == "growing"
    assert tier_of(2, 0) == "reproduced"
    assert tier_of(1, 0) == "new"
    assert tier_of(5, 9) == "reproduced"          # births alone do not make 'growing'


def test_settle_rule():
    assert is_settled(1, 100, now_epoch=1, now_tick=100 + SETTLE_TICKS) is True
    assert is_settled(1, 100, now_epoch=1, now_tick=100 + SETTLE_TICKS - 1) is False
    assert is_settled(1, 100, now_epoch=2, now_tick=5) is True                 # epoch retired
    assert is_settled(1, 100, now_epoch=1, now_tick=150, settle_ticks=50) is True


def test_verdict_row_shapes():
    r = verdict_row(entry(), {"births": 40, "first_generation": 3, "max_generation": 13, "first_tick": 120, "last_tick": 3020},
                    settled_tick=3200)
    assert r["tier"] == "growing" and r["fitness"] == 40 and r["generations"] == 10 and r["lifetime_ticks"] == 2900
    assert r["request_id"] == "r1" and r["proposal_genome_id"] == "g1" and r["settled_tick"] == 3200
    assert verdict_row(entry(), None, settled_tick=3200)["tier"] == "unborn"
    rej = verdict_row(entry(outcome="rejected"), None, settled_tick=3200)
    assert rej["tier"] == "rejected" and rej["births"] == 0 and rej["fitness"] == 0


def test_preexisting_genome_is_not_credited():
    """genome_stats is keyed by (epoch, genome_id): a genome already alive carries every carrier's births."""
    r = verdict_row(entry(tick=100), {"births": 90, "first_generation": 2, "max_generation": 40,
                                      "first_tick": 50, "last_tick": 3000}, settled_tick=3200)
    assert r["tier"] == "preexisting" and r["fitness"] == 0
    assert r["births"] == 90 and r["generations"] == 38 and r["lifetime_ticks"] == 2950


def test_child_born_on_or_after_the_proposal_is_credited():
    """thegrid never lowers first_tick, so first_tick == our tick is our own child."""
    r = verdict_row(entry(tick=100), {"births": 40, "first_generation": 3, "max_generation": 13,
                                      "first_tick": 100, "last_tick": 3020}, settled_tick=3200)
    assert r["tier"] == "growing" and r["fitness"] == 40


def test_chain_is_deterministic_and_verifies(tmp_path):
    led = Ledger(tmp_path / "verdicts.jsonl")
    assert led.head() == "0" * 64
    a = led.append({"request_id": "a", "tier": "new"})
    b = led.append({"request_id": "b", "tier": "growing"})
    assert a["chain"] == chain_hash("0" * 64, {"request_id": "a", "tier": "new"})
    assert b["chain"] == chain_hash(a["chain"], {"request_id": "b", "tier": "growing"})
    assert led.verify() is True and led.settled_ids() == {"a", "b"}
    lines = (tmp_path / "verdicts.jsonl").read_text().splitlines()
    tampered = json.loads(lines[0]); tampered["tier"] = "established"
    (tmp_path / "verdicts.jsonl").write_text(json.dumps(tampered) + "\n" + lines[1] + "\n")
    assert Ledger(tmp_path / "verdicts.jsonl").verify() is False


def test_ledger_truncates_a_partial_tail_before_appending(tmp_path):
    """A row is fsynced, so a half-line is a torn write, never a verdict."""
    path = tmp_path / "verdicts.jsonl"
    Ledger(path).append({"request_id": "a", "tier": "new"})
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"request_id": "torn", "tier": "gro')       # the machine lost power here
    led = Ledger(path)
    led.append({"request_id": "b", "tier": "growing"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and [json.loads(l)["request_id"] for l in lines] == ["a", "b"]
    assert Ledger(path).verify() is True


def test_ledger_head_is_cached(tmp_path):
    led = Ledger(tmp_path / "verdicts.jsonl")
    reads = []
    real = led.rows

    def counted():
        reads.append(1)
        return real()

    led.rows = counted
    led.append({"request_id": "a", "tier": "new"})
    led.append({"request_id": "b", "tier": "growing"})
    assert len(reads) == 1                                   # the second append reused the head
    assert led.settled_ids() == {"a", "b"} and len(reads) == 1
    assert Ledger(tmp_path / "verdicts.jsonl").verify() is True


def test_stats_reader_reads_a_copy(tmp_path):
    db = make_db(tmp_path / "history.sqlite3", [(1, "g1", 40, 0, 0, 3, 13, 120, 3020)])
    with StatsReader(db) as reader:
        assert reader.stats(1, "g1") == {"births": 40, "first_generation": 3, "max_generation": 13,
                                         "first_tick": 120, "last_tick": 3020}
        assert reader.stats(1, "missing") is None
        assert reader.stats(2, "g1") is None
        assert reader.copy_path != db and Path(reader.copy_path).exists()
    assert not Path(reader.copy_path).exists()            # temp copy removed


def test_stats_reader_sees_rows_still_in_the_wal(tmp_path):
    """thegrid's DB is WAL-mode on Skywave; a file copy would miss uncheckpointed rows."""
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE genome_stats (epoch INTEGER NOT NULL, genome_id TEXT NOT NULL,
        births INTEGER NOT NULL DEFAULT 0, starvation_deaths INTEGER NOT NULL DEFAULT 0,
        senescence_deaths INTEGER NOT NULL DEFAULT 0, first_generation INTEGER,
        max_generation INTEGER NOT NULL DEFAULT 0, first_tick INTEGER NOT NULL, last_tick INTEGER NOT NULL,
        PRIMARY KEY (epoch, genome_id))""")
    db.commit()
    db.execute("PRAGMA wal_autocheckpoint=0")             # keep the row in the WAL only
    db.execute("INSERT INTO genome_stats VALUES (1,'w1',7,0,0,1,4,10,900)")
    db.commit()
    assert (tmp_path / "history.sqlite3-wal").stat().st_size > 0
    with StatsReader(path) as reader:                    # writer still open, like the colony
        assert reader.stats(1, "w1")["births"] == 7
    db.close()


def test_stats_reader_snapshot_hash_sees_wal_rows(tmp_path):
    """The hash names the snapshot the verdicts were scored against, WAL rows included."""
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (x INTEGER)")
    db.commit()
    with StatsReader(path) as reader:
        before = reader.snapshot_sha256()
        assert reader.snapshot_sha256() is before                  # computed once, cached
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("INSERT INTO t VALUES (1)")
    db.commit()                                                    # the row lives only in the WAL
    with StatsReader(path) as reader:
        after = reader.snapshot_sha256()
    db.close()
    assert len(before) == 64 and before != after


def test_verdict_row_carries_the_settle_time_snapshot_hash():
    r = verdict_row(entry(), None, settled_tick=3200, history_sha256="beef" * 16)
    assert r["history_sha256"] == "beef" * 16
    assert "history_sha256" in canonical(r)                        # inside the chain, not beside it


def test_stats_reader_writes_its_copy_into_the_given_dir(tmp_path):
    db = make_db(tmp_path / "history.sqlite3", [])
    with StatsReader(db, tmp_dir=tmp_path / "verdicts") as reader:
        assert Path(reader.copy_path).parent == tmp_path / "verdicts"
    assert not Path(reader.copy_path).exists()


def test_stats_reader_leaves_no_temp_file_when_the_db_is_missing(tmp_path):
    reader = StatsReader(tmp_path / "absent.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        with reader:
            pass
    assert reader.copy_path is not None and not Path(reader.copy_path).exists()


def test_settle_pending_appends_only_due_and_unsettled(tmp_path):
    db = make_db(tmp_path / "history.sqlite3", [(1, "g1", 40, 0, 0, 3, 13, 120, 3020)])
    led = Ledger(tmp_path / "verdicts.jsonl")
    entries = [entry("r1", tick=100, gid="g1"), entry("r2", tick=200, gid="g2"),
               entry("r3", tick=4000, gid="g3"), entry("r4", tick=100, outcome="rejected")]
    with StatsReader(db) as reader:
        rows = settle_pending(entries, led, reader, now_epoch=1, now_tick=3300)
        snapshot = reader.snapshot_sha256()
    assert [r["request_id"] for r in rows] == ["r1", "r2", "r4"]         # r3 not due yet
    assert [r["tier"] for r in rows] == ["growing", "unborn", "rejected"]
    assert {r["history_sha256"] for r in rows} == {snapshot}             # the state each was scored against
    with StatsReader(db) as reader:
        again = settle_pending(entries, led, reader, now_epoch=1, now_tick=3300)
    assert again == []                                                     # idempotent
    assert led.verify() is True and len(led.rows()) == 3


def test_unknown_epoch_settles_as_unknown_not_unborn(tmp_path):
    """0/0 is the relay's fallback when /api/state was down, not epoch 0 tick 0."""
    db = make_db(tmp_path / "history.sqlite3", [(0, "g1", 40, 0, 0, 3, 13, 0, 3020)])
    led = Ledger(tmp_path / "verdicts.jsonl")
    with StatsReader(db) as reader:
        rows = settle_pending([entry("r0", epoch=0, tick=0, gid="g1")], led, reader,
                              now_epoch=1, now_tick=50)
    assert [r["tier"] for r in rows] == ["unknown"]        # the (0, g1) row is not evidence
    assert rows[0]["births"] == 0 and rows[0]["fitness"] == 0 and rows[0]["epoch_found"] is None
    assert led.settled_ids() == {"r0"}                     # settled: it leaves the pending set


def test_proposal_consumed_in_the_next_epoch_is_found(tmp_path):
    db = make_db(tmp_path / "history.sqlite3", [(2, "g1", 40, 0, 0, 3, 13, 40, 900)])
    led = Ledger(tmp_path / "verdicts.jsonl")
    with StatsReader(db) as reader:
        rows = settle_pending([entry("r1", epoch=1, tick=900, gid="g1")], led, reader,
                              now_epoch=2, now_tick=100)
    assert rows[0]["tier"] == "growing" and rows[0]["fitness"] == 40 and rows[0]["epoch_found"] == 2


def test_next_epoch_row_past_the_grace_window_is_not_credited(tmp_path):
    db = make_db(tmp_path / "history.sqlite3", [(2, "g1", 40, 0, 0, 3, 13, 5000, 9000)])
    led = Ledger(tmp_path / "verdicts.jsonl")
    with StatsReader(db) as reader:
        rows = settle_pending([entry("r1", epoch=1, tick=900, gid="g1")], led, reader,
                              now_epoch=2, now_tick=100)
    assert rows[0]["tier"] == "unborn" and rows[0]["epoch_found"] is None


def test_next_epoch_founder_seen_at_tick_zero_is_not_credited(tmp_path):
    """start_epoch observes the seeded population at observed_tick=0; a born child never is."""
    db = make_db(tmp_path / "history.sqlite3", [(2, "g1", 500, 0, 0, 0, 180, 0, 9000)])
    led = Ledger(tmp_path / "verdicts.jsonl")
    with StatsReader(db) as reader:
        rows = settle_pending([entry("r1", epoch=1, tick=900, gid="g1")], led, reader,
                              now_epoch=2, now_tick=100)
    assert rows[0]["tier"] == "unborn" and rows[0]["fitness"] == 0
    assert rows[0]["epoch_found"] is None


def test_old_epoch_row_is_found_when_now_epoch_is_later(tmp_path):
    db = make_db(tmp_path / "history.sqlite3", [(1, "g1", 40, 0, 0, 3, 13, 950, 3020)])
    led = Ledger(tmp_path / "verdicts.jsonl")
    with StatsReader(db) as reader:
        rows = settle_pending([entry("r1", epoch=1, tick=900, gid="g1")], led, reader,
                              now_epoch=2, now_tick=100)
    assert rows[0]["tier"] == "growing" and rows[0]["epoch_found"] == 1


class FakeBus:
    def __init__(self):
        self.published = []

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


async def test_run_once_settles_publishes_and_is_idempotent(tmp_path):
    from grid.proposal_log import ProposalLog
    from grid.verdicts import run_once
    db = make_db(tmp_path / "history.sqlite3", [(1, "g1", 40, 0, 0, 3, 13, 120, 3020)])
    vdir = tmp_path / "verdicts"
    plog = ProposalLog(vdir)
    plog.record({"request_id": "r1", "epoch": 1, "tick": 100, "proposal_genome_id": "g1", "outcome": "proposed",
                 "model_alias": "a", "reason": None, "prompt_path": "prompts/r1.txt"}, "p")
    bus = FakeBus()
    rows = await run_once(verdicts_dir=vdir, history_db=db, state={"epoch": 1, "tick": 3300}, bus=bus,
                          colony="c", agent_id="grid-c")
    assert [r["tier"] for r in rows] == ["growing"]
    subj, payload = bus.published[0]
    assert subj == "GRID.c.verdict" and payload["request_id"] == "r1" and payload["fitness"] == 40
    assert payload["schema_version"] == "1.0" and payload["epoch"] == 1 and payload["tick"] == 3300
    assert payload["kind"] == "verdict"          # consumers route on kind, not only on subject
    assert await run_once(verdicts_dir=vdir, history_db=db, state={"epoch": 1, "tick": 3400}, bus=bus,
                          colony="c", agent_id="grid-c") == []
    assert len(bus.published) == 1


async def test_run_once_without_bus_still_writes(tmp_path):
    from grid.proposal_log import ProposalLog
    from grid.verdicts import Ledger, run_once
    db = make_db(tmp_path / "history.sqlite3", [])
    vdir = tmp_path / "verdicts"
    ProposalLog(vdir).record({"request_id": "r1", "epoch": 1, "tick": 1, "proposal_genome_id": "gx",
                              "outcome": "proposed", "model_alias": "a", "reason": None,
                              "prompt_path": "prompts/r1.txt"}, "p")
    rows = await run_once(verdicts_dir=vdir, history_db=db, state={"epoch": 2, "tick": 5}, bus=None,
                          colony="c", agent_id="grid-c")
    assert rows[0]["tier"] == "unborn" and Ledger(vdir / "verdicts.jsonl").verify()
