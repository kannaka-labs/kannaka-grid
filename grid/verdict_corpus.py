"""Turn settled verdicts into training records in kannaka-memory's corpus shape.

A proposal is Kannaka's own authored turn (first-party by construction); the
verdict is a FILTER on it, never a target. Rejected and unborn proposals go to
negatives.jsonl for a later preference-pair version.
"""
from __future__ import annotations

import argparse
import hashlib
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


def record_for(entry: dict, verdict: dict, prompt: str, *, history_sha256: str) -> dict:
    return {
        "id": f"odin-{entry['request_id']}", "kind": "odin", "tier": 1, "source": "grid-verdicts",
        "speaker": "kannaka", "text": json.dumps(entry.get("proposal")), "context": prompt,
        "verdict": {"tier": verdict.get("tier"), "fitness": verdict.get("fitness")},
        "provenance": {"colony": entry.get("colony"), "epoch": entry.get("epoch"), "tick": entry.get("tick"),
                       "chain": verdict.get("chain"), "history_sha256": history_sha256,
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


def _sha256_file(path: Path | None) -> str:
    if path is None or not Path(path).exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(verdicts_dir: Path, out_dir: Path, *, min_tier: str = "reproduced",
           history_db: Path | None = None) -> dict:
    vdir, out = Path(verdicts_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    entries = {e["request_id"]: e for e in ProposalLog(vdir).read_all()}
    ledger = Ledger(vdir / "verdicts.jsonl")
    hist_sha = _sha256_file(history_db)
    threshold = TIER_RANK[min_tier]
    records, negatives, by_tier, skipped = [], [], {}, 0
    for v in ledger.rows():
        e = entries.get(v["request_id"])
        if e is None:
            continue
        if v["tier"] in NEGATIVE_TIERS:
            negatives.append(record_for(e, v, "", history_sha256=hist_sha))
            continue
        if TIER_RANK.get(v["tier"], -1) < threshold:
            continue
        prompt_path = vdir / e.get("prompt_path", "")
        if not prompt_path.is_file():
            skipped += 1
            continue
        records.append(record_for(e, v, prompt_path.read_text(encoding="utf-8"), history_sha256=hist_sha))
        by_tier[v["tier"]] = by_tier.get(v["tier"], 0) + 1
    chron = vdir / "chronicle.jsonl"
    if chron.exists():
        for line in chron.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(chronicle_record(json.loads(line)))
    for name, rows in (("records.jsonl", records), ("negatives.jsonl", negatives)):
        with open(out / name, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    manifest = {"records": len(records), "negatives": len(negatives), "min_tier": min_tier,
                "by_tier": by_tier, "skipped_missing_prompt": skipped, "chain_head": ledger.head(),
                "history_sha256": hist_sha, "verdicts_dir": str(vdir)}
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
    ap.add_argument("--history-db", default=env("HISTORY_DB", f"/srv/grid/{colony}/state/history.sqlite3"))
    a = ap.parse_args(argv)
    print(json.dumps(export(Path(a.verdicts_dir), Path(a.out), min_tier=a.min_tier, history_db=Path(a.history_db))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
