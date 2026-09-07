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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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


DREAM_PREFIX = "[dream]"
DREAM_QUESTION = "What did you consolidate tonight?"


def dream_record(memory: dict, *, colony: str) -> dict:
    text = str(memory.get("content", ""))
    if text.startswith(DREAM_PREFIX):
        text = text[len(DREAM_PREFIX):].strip()
    return {"id": f"dream-{memory['id']}", "kind": "dream", "tier": 1, "source": "grid-dreams",
            "speaker": "kannaka", "text": text, "context": DREAM_QUESTION,
            "provenance": {"colony": colony, "memory_id": memory["id"]}}


def dream_records(runner, *, kannaka_bin: str, data_dir: Path, colony: str) -> tuple[list[dict], str]:
    """Dream REPORT memories from the colony mind's store, via `kannaka search`.

    `kannaka dream` writes consolidation metrics to its log, not a report memory, so
    this is expected to be empty until it does; the note says so instead of the kind
    silently vanishing from the manifest.
    """
    env = dict(os.environ, KANNAKA_DATA_DIR=str(data_dir))
    try:
        # NOTE verified 2026-09-06: `kannaka search` prints a DECORATED human view, not JSON,
        # and has no --json flag today. `recall --envelope` emits JSON but is resonance, not a
        # prefix match. So this parses JSON when the CLI ever emits it and otherwise falls
        # through to the empty+note path — which is the expected state, because `kannaka dream`
        # writes metrics to its log, not a report memory. When a JSON search lands upstream,
        # swap the command here; the test already pins the JSON contract.
        r = runner([kannaka_bin, "search", DREAM_PREFIX, "--limit", "500"], env=env,
                   capture_output=True, text=True, timeout=60)
        rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log.warning("dream search returned no JSON (expected today): %s", e)
        rows = []
    recs = [dream_record(m, colony=colony) for m in rows
            if isinstance(m, dict) and str(m.get("content", "")).startswith(DREAM_PREFIX)]
    note = f"{len(recs)} report memor{'y' if len(recs) == 1 else 'ies'}" if recs else \
        "0 (no report memories in store; kannaka dream logs metrics only)"
    return recs, note


@dataclass(frozen=True)
class DreamSource:
    runner: Callable = subprocess.run
    kannaka_bin: str = "kannaka"
    data_dir: Path = Path("/srv/grid/colony-one/memory")
    colony: str = "colony-one"


def _seen(verdict: dict, entry: dict, snapshots: set[str], stamps: list[int]) -> None:
    """Remember which colony state a record came from, and when it was proposed."""
    if verdict.get("history_sha256"):
        snapshots.add(verdict["history_sha256"])
    if isinstance(entry.get("ts"), int):
        stamps.append(entry["ts"])


def export(verdicts_dir: Path, out_dir: Path, *, min_tier: str = "reproduced",
           since_ts: int | None = None, dream: DreamSource | None = None) -> dict:
    vdir, out = Path(verdicts_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # N5: the same guard ab_arms has -- a proposal-log line without a request_id (a future
    # writer, a hand-edit) must be skipped, not KeyError the whole export.
    entries = {e["request_id"]: e for e in ProposalLog(vdir).read_all() if e.get("request_id")}
    ledger = Ledger(vdir / "verdicts.jsonl")
    threshold = TIER_RANK[min_tier]
    records, negatives, by_tier = [], [], {}
    skipped = skipped_missing_entry = skipped_below_threshold = skipped_no_reply = 0
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
        if since_ts is not None and int(e.get("ts", 0)) < since_ts:
            continue
        negative = v["tier"] in NEGATIVE_TIERS
        if not negative and TIER_RANK.get(v["tier"], -1) < threshold:
            skipped_below_threshold += 1
            continue
        if v["tier"] == "rejected" and not e.get("reply"):
            # Logged before the relay kept replies: a rejection has no proposal either,
            # so the negative would be the literal string "null" against a real prompt.
            skipped_no_reply += 1
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
    by_kind = {"odin": sum(by_tier.values()), "chronicle": len(records) - sum(by_tier.values())}
    dream_note = "not requested"
    if dream is not None:
        drecs, dream_note = dream_records(dream.runner, kannaka_bin=dream.kannaka_bin,
                                          data_dir=dream.data_dir, colony=dream.colony)
        records.extend(drecs)
        by_kind["dream"] = len(drecs)
    for name, rows in (("records.jsonl", records), ("negatives.jsonl", negatives)):
        with open(out / name, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    manifest = {"records": len(records), "negatives": len(negatives), "min_tier": min_tier,
                "by_tier": by_tier, "skipped_missing_prompt": skipped,
                "skipped_missing_entry": skipped_missing_entry,
                "skipped_below_threshold": skipped_below_threshold,
                "skipped_no_reply": skipped_no_reply,
                "skipped_preexisting": excluded["preexisting"], "skipped_unknown": excluded["unknown"],
                "chain_head": ledger.head(), "history_snapshots": sorted(snapshots),
                "since_ts": min(stamps) if stamps else None, "until_ts": max(stamps) if stamps else None,
                "cumulative": True, "verdicts_dir": str(vdir),
                "by_kind": by_kind, "dream_note": dream_note, "since_ts_filter": since_ts}
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
    ap.add_argument("--since-ts", type=int, default=None)
    ap.add_argument("--dream", action="store_true")
    a = ap.parse_args(argv)
    dream = DreamSource(kannaka_bin=env("KANNAKA_BIN", "kannaka"),
                         data_dir=Path(env("MIND_DATA_DIR", "/srv/grid/colony-one/memory")),
                         colony=colony) if a.dream else None
    print(json.dumps(export(Path(a.verdicts_dir), Path(a.out), min_tier=a.min_tier,
                             since_ts=a.since_ts, dream=dream)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
