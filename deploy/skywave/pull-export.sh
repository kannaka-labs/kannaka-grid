#!/bin/bash
# Forced command for the rogue-grid-pull key: export the settled verdict corpus AND the
# authored identity example set as corpus records, and stream them as one tar on stdout.
# Read-only for the caller: no shell, no arguments honoured.
set -euo pipefail
cd /srv/grid/kannaka-grid
OUT=/srv/grid/colony-one/export
# Fail closed on purpose: with no CI on this path, records() raising on the host (e.g.
# a base-naming leak) is the last guard before a bad export reaches the retrain, so
# `set -e` must abort the pull rather than stream a partial tar. Only stdout (the
# export's own printed JSON) is discarded; stderr is left to surface to the puller so
# the reason for the abort is visible instead of a bare failing exit code.
/srv/grid/venv/bin/python -m grid.verdict_corpus --verdicts-dir /srv/grid/colony-one/verdicts --out "$OUT" --dream >/dev/null
/srv/grid/venv/bin/python -m grid.identity export --out "$OUT" >/dev/null
cp /srv/grid/colony-one/verdicts/verdicts.jsonl "$OUT/verdicts.jsonl"
tar -C "$OUT" -cf - manifest.json records.jsonl negatives.jsonl verdicts.jsonl identity_examples.jsonl identity_manifest.json
