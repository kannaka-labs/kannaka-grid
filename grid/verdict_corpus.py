"""Turn settled verdicts into training records in kannaka-memory's corpus shape.

A proposal is Kannaka's own authored turn (first-party by construction); the
verdict is a FILTER on it, never a target. Rejected and unborn proposals go to
negatives.jsonl for a later preference-pair version.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .proposal_log import ProposalLog
from .verdicts import Ledger

log = logging.getLogger("grid.verdict_corpus")

TIER_RANK = {"established": 4, "enduring": 3, "growing": 2, "reproduced": 1, "new": 0}
NEGATIVE_TIERS = ("unborn", "rejected")
# Neither a win nor a loss: 'preexisting' credited a genome that was already alive,
# 'unknown' never learned the epoch it was proposed in. Training on either teaches noise.
EXCLUDED_TIERS = ("preexisting", "unknown")


def record_for(entry: dict, verdict: dict, prompt: str) -> dict:
    # A rejection never produced a genome, so what she actually wrote is the reply.
    # Anything else — a positive, or an unborn proposal — is the genome she proposed.
    text = entry.get("reply") if verdict.get("tier") == "rejected" else None
    return {
        "id": f"odin-{entry['request_id']}", "kind": "odin", "tier": 1, "source": "grid-verdicts",
        "speaker": "kannaka", "text": json.dumps(entry.get("proposal")) if text is None else text,
        "context": prompt,
        "verdict": {"tier": verdict.get("tier"), "fitness": verdict.get("fitness")},
        "provenance": {"colony": entry.get("colony"), "epoch": entry.get("epoch"), "tick": entry.get("tick"),
                       "chain": verdict.get("chain"),
                       "history_sha256": verdict.get("history_sha256", ""),
                       "model_alias": entry.get("model_alias")},
    }


def chronicle_record(row: dict) -> dict:
    return {
        "id": f"chronicle-{row.get('colony')}-{row.get('epoch')}-{row.get('tick')}-{row.get('kind')}",
        "kind": "chronicle", "tier": 1, "source": "grid-chronicle", "speaker": "kannaka",
        "text": row.get("text", ""), "context": row.get("question", ""),
        "provenance": {"colony": row.get("colony"), "epoch": row.get("epoch"), "tick": row.get("tick"),
                       "since_tick": row.get("since_tick")},
    }


def _seen(verdict: dict, entry: dict, snapshots: set[str], stamps: list[int]) -> None:
    """Remember which colony state a record came from, and when it was proposed."""
    if verdict.get("history_sha256"):
        snapshots.add(verdict["history_sha256"])
    if isinstance(entry.get("ts"), int):
        stamps.append(entry["ts"])


def export(verdicts_dir: Path, out_dir: Path, *, min_tier: str = "reproduced") -> dict:
    vdir, out = Path(verdicts_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    entries = {e["request_id"]: e for e in ProposalLog(vdir).read_all()}
    ledger = Ledger(vdir / "verdicts.jsonl")
    threshold = TIER_RANK[min_tier]
    records, negatives, by_tier = [], [], {}
    skipped = skipped_missing_entry = skipped_below_threshold = 0
    excluded = {t: 0 for t in EXCLUDED_TIERS}
    snapshots: set[str] = set()
    stamps: list[int] = []
    for v in ledger.rows():
        e = entries.get(v["request_id"])
        if e is None:
            skipped_missing_entry += 1
            continue
        if v["tier"] in EXCLUDED_TIERS:
            excluded[v["tier"]] += 1
            continue
        negative = v["tier"] in NEGATIVE_TIERS
        if not negative and TIER_RANK.get(v["tier"], -1) < threshold:
            skipped_below_threshold += 1
            continue
        prompt_path = vdir / e.get("prompt_path", "")
        if not prompt_path.is_file():
            # A negative without its prompt is not a preference pair, only a complaint.
            skipped += 1
            continue
        record = record_for(e, v, prompt_path.read_text(encoding="utf-8"))
        if negative:
            negatives.append(record)
        else:
            records.append(record)
            by_tier[v["tier"]] = by_tier.get(v["tier"], 0) + 1
        _seen(v, e, snapshots, stamps)
    chron = vdir / "chronicle.jsonl"
    if chron.exists():
        # The id is (colony, epoch, tick, kind); a re-published line would otherwise
        # be trained on twice. The first one is the one that was actually published.
        seen_chronicle: set[str] = set()
        for line in chron.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = chronicle_record(json.loads(line))
            if record["id"] not in seen_chronicle:
                seen_chronicle.add(record["id"])
                records.append(record)
    for name, rows in (("records.jsonl", records), ("negatives.jsonl", negatives)):
        with open(out / name, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    manifest = {"records": len(records), "negatives": len(negatives), "min_tier": min_tier,
                "by_tier": by_tier, "skipped_missing_prompt": skipped,
                "skipped_missing_entry": skipped_missing_entry,
                "skipped_below_threshold": skipped_below_threshold,
                "skipped_preexisting": excluded["preexisting"], "skipped_unknown": excluded["unknown"],
                "chain_head": ledger.head(), "history_snapshots": sorted(snapshots),
                "since_ts": min(stamps) if stamps else None, "until_ts": max(stamps) if stamps else None,
                "cumulative": True, "verdicts_dir": str(vdir)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    env = os.environ.get
    colony = env("COLONY_SLUG", "colony-one")
    ap = argparse.ArgumentParser(description="Export settled verdicts as training records.")
    ap.add_argument("--verdicts-dir", default=env("VERDICTS_DIR", f"/srv/grid/{colony}/verdicts"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-tier", default="reproduced", choices=sorted(TIER_RANK))
    a = ap.parse_args(argv)
    print(json.dumps(export(Path(a.verdicts_dir), Path(a.out), min_tier=a.min_tier)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
