import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from grid.verdicts import (SETTLE_TICKS, Ledger, StatsReader, chain_hash, is_settled, settle_pending,
                           tier_of, verdict_row)


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
    assert [r["request_id"] for r in rows] == ["r1", "r2", "r4"]         # r3 not due yet
    assert [r["tier"] for r in rows] == ["growing", "unborn", "rejected"]
    with StatsReader(db) as reader:
        again = settle_pending(entries, led, reader, now_epoch=1, now_tick=3300)
    assert again == []                                                     # idempotent
    assert led.verify() is True and len(led.rows()) == 3
