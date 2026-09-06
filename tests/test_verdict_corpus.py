import json
from pathlib import Path

from grid.proposal_log import ProposalLog
from grid.verdict_corpus import chronicle_record, export, record_for
from grid.verdicts import Ledger

REQUIRED = {"id", "kind", "tier", "source", "speaker", "text"}       # export_corpus.py's contract


def seed(vdir: Path) -> None:
    plog = ProposalLog(vdir)
    for rid, gid in (("a", "g1"), ("b", "g2"), ("c", "g3")):
        plog.record({"request_id": rid, "epoch": 1, "tick": 10, "colony": "c", "proposal": ["harvest", "fork"],
                     "proposal_genome_id": gid, "outcome": "proposed", "model_alias": "m", "reason": None,
                     "prompt_path": f"prompts/{rid}.txt", "parent_genome": ["harvest"]}, f"prompt {rid}")
    plog.record({"request_id": "d", "epoch": 1, "tick": 11, "colony": "c", "proposal": None,
                 "proposal_genome_id": None, "outcome": "rejected", "model_alias": "m", "reason": "bad",
                 "prompt_path": "prompts/d.txt", "parent_genome": ["harvest"]}, "prompt d")
    led = Ledger(vdir / "verdicts.jsonl")
    led.append({"request_id": "a", "proposal_genome_id": "g1", "epoch": 1, "settled_tick": 3100, "births": 40,
                "generations": 12, "lifetime_ticks": 2000, "tier": "growing", "fitness": 40, "model_alias": "m", "outcome": "proposed"})
    led.append({"request_id": "b", "proposal_genome_id": "g2", "epoch": 1, "settled_tick": 3100, "births": 1,
                "generations": 0, "lifetime_ticks": 5, "tier": "new", "fitness": 1, "model_alias": "m", "outcome": "proposed"})
    led.append({"request_id": "c", "proposal_genome_id": "g3", "epoch": 1, "settled_tick": 3100, "births": 0,
                "generations": 0, "lifetime_ticks": 0, "tier": "unborn", "fitness": 0, "model_alias": "m", "outcome": "proposed"})
    led.append({"request_id": "d", "proposal_genome_id": None, "epoch": 1, "settled_tick": 3100, "births": 0,
                "generations": 0, "lifetime_ticks": 0, "tier": "rejected", "fitness": 0, "model_alias": "m", "outcome": "rejected"})
    (vdir / "chronicle.jsonl").write_text(json.dumps({"ts": 1, "colony": "c", "epoch": 1, "tick": 2000, "kind": "skill",
                                                       "question": "Name the capability", "text": "fork early via fork.",
                                                       "since_tick": 0}) + "\n")


def test_record_for_shape():
    e = {"request_id": "a", "epoch": 1, "tick": 10, "colony": "c", "proposal": ["harvest", "fork"], "model_alias": "m"}
    v = {"tier": "growing", "fitness": 40, "chain": "abc"}
    r = record_for(e, v, "the prompt", history_sha256="deadbeef")
    assert REQUIRED <= set(r)
    assert r["id"] == "odin-a" and r["kind"] == "odin" and r["tier"] == 1 and r["speaker"] == "kannaka"
    assert r["source"] == "grid-verdicts" and r["text"] == '["harvest", "fork"]' and r["context"] == "the prompt"
    assert r["verdict"] == {"tier": "growing", "fitness": 40}
    assert r["provenance"] == {"colony": "c", "epoch": 1, "tick": 10, "chain": "abc", "history_sha256": "deadbeef",
                               "model_alias": "m"}


def test_chronicle_record_shape():
    r = chronicle_record({"ts": 1, "colony": "c", "epoch": 1, "tick": 2000, "kind": "skill",
                          "question": "Q?", "text": "fork early via fork.", "since_tick": 0})
    assert REQUIRED <= set(r) and r["kind"] == "chronicle" and r["tier"] == 1
    assert r["id"] == "chronicle-c-1-2000-skill" and r["context"] == "Q?" and r["text"] == "fork early via fork."


def test_export_filters_by_tier_and_splits_negatives(tmp_path):
    vdir, out = tmp_path / "v", tmp_path / "out"
    seed(vdir)
    manifest = export(vdir, out, min_tier="reproduced")
    records = [json.loads(l) for l in (out / "records.jsonl").read_text().splitlines()]
    negatives = [json.loads(l) for l in (out / "negatives.jsonl").read_text().splitlines()]
    assert [r["id"] for r in records] == ["odin-a", "chronicle-c-1-2000-skill"]
    assert sorted(n["id"] for n in negatives) == ["odin-c", "odin-d"]          # unborn + rejected; 'new' is neither
    assert records[0]["context"] == "prompt a"
    assert manifest["records"] == 2 and manifest["negatives"] == 2 and manifest["min_tier"] == "reproduced"
    assert manifest["by_tier"] == {"growing": 1} and manifest["chain_head"] == Ledger(vdir / "verdicts.jsonl").head()
    assert manifest["skipped_below_threshold"] == 1                            # 'b' is only 'new'
    assert json.loads((out / "manifest.json").read_text()) == manifest


def test_preexisting_verdicts_are_neither_positive_nor_negative(tmp_path):
    vdir, out = tmp_path / "v", tmp_path / "out"
    seed(vdir)
    ProposalLog(vdir).record({"request_id": "p", "epoch": 1, "tick": 12, "colony": "c",
                              "proposal": ["harvest", "fork"], "proposal_genome_id": "g4",
                              "outcome": "proposed", "model_alias": "m", "reason": None,
                              "prompt_path": "prompts/p.txt", "parent_genome": ["harvest"]}, "prompt p")
    Ledger(vdir / "verdicts.jsonl").append({"request_id": "p", "proposal_genome_id": "g4", "epoch": 1,
        "settled_tick": 3100, "births": 90, "generations": 38, "lifetime_ticks": 2950,
        "tier": "preexisting", "fitness": 0, "model_alias": "m", "outcome": "proposed"})
    manifest = export(vdir, out, min_tier="reproduced")
    ids = {json.loads(l)["id"] for l in (out / "records.jsonl").read_text().splitlines()}
    ids |= {json.loads(l)["id"] for l in (out / "negatives.jsonl").read_text().splitlines()}
    assert "odin-p" not in ids
    assert manifest["skipped_preexisting"] == 1 and manifest["skipped_unknown"] == 0


def test_export_growing_threshold_and_missing_prompt(tmp_path):
    vdir, out = tmp_path / "v", tmp_path / "out"
    seed(vdir)
    (vdir / "prompts" / "a.txt").unlink()
    manifest = export(vdir, out, min_tier="growing")
    records = [json.loads(l) for l in (out / "records.jsonl").read_text().splitlines()]
    assert [r["id"] for r in records] == ["chronicle-c-1-2000-skill"]      # 'a' skipped: prompt missing
    assert manifest["skipped_missing_prompt"] == 1


def test_history_hash_sees_rows_still_in_the_wal(tmp_path):
    import sqlite3
    from grid.verdict_corpus import _sha256_db
    db = tmp_path / "history.sqlite3"
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (x INTEGER)")
    c.commit()
    before = _sha256_db(db)
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()                                    # row lives only in the WAL
    after = _sha256_db(db)
    c.close()
    assert before and after and before != after
    assert _sha256_db(tmp_path / "absent.sqlite3") == ""


def test_manifest_counts_verdicts_without_a_proposal_entry(tmp_path):
    vdir, out = tmp_path / "v", tmp_path / "out"
    seed(vdir)
    Ledger(vdir / "verdicts.jsonl").append({"request_id": "ghost", "proposal_genome_id": "gz", "epoch": 1,
        "settled_tick": 3100, "births": 9, "generations": 20, "lifetime_ticks": 100, "tier": "growing",
        "fitness": 9, "model_alias": "m", "outcome": "proposed"})
    manifest = export(vdir, out, min_tier="reproduced")
    assert manifest["skipped_missing_entry"] == 1 and manifest["records"] == 2
