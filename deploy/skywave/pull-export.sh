#!/bin/bash
# Forced command for the rogue-grid-pull key: export the settled verdict corpus AND the
# authored identity example set as corpus records, and stream them as one tar on stdout.
# Read-only for the caller: no shell, no arguments honoured.
set -euo pipefail
cd /srv/grid/kannaka-grid
OUT=/srv/grid/colony-one/export
/srv/grid/venv/bin/python -m grid.verdict_corpus --verdicts-dir /srv/grid/colony-one/verdicts --out "$OUT" --dream >/dev/null 2>&1
/srv/grid/venv/bin/python -m grid.identity export --out "$OUT" >/dev/null 2>&1
cp /srv/grid/colony-one/verdicts/verdicts.jsonl "$OUT/verdicts.jsonl"
tar -C "$OUT" -cf - manifest.json records.jsonl negatives.jsonl verdicts.jsonl identity_examples.jsonl
