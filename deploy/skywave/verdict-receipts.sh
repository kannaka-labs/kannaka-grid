#!/usr/bin/env bash
# deploy/skywave/verdict-receipts.sh - unattended receipts for the verdict
# pipeline (deploy/README.md §4). Waits for the relay to log its first proposal,
# waits for that proposal to settle (SETTLE_TICKS later, or the next epoch),
# captures GRID.<colony>.verdict while the nightly unit runs once by hand, then
# exports the corpus. Writes /srv/grid/receipts-verdicts-<ts>/ and prints it.
#
#   nohup bash ~/grid-deploy/verdict-receipts.sh > ~/grid-deploy/verdict-receipts.log 2>&1 &
#
# Nothing here prints prompts, replies, or corpus records: those are held-back
# artifacts. The receipts are counts, manifests, journal lines, and one envelope.
set -uo pipefail
ROOT=/srv/grid
COLONY=${COLONY_SLUG:-colony-one}
VDIR=$ROOT/$COLONY/verdicts
URL=${COLONY_URL:-http://127.0.0.1:8787}
SETTLE=${SETTLE_TICKS:-3000}
OUT=$ROOT/receipts-verdicts-$(date -u +%Y%m%dT%H%M%SZ)
PY=$ROOT/venv/bin/python
# the units get these from EnvironmentFile=; bus_capture.py needs them too or it
# connects anonymously and anon may not subscribe to GRID.>
set -a; . "$HOME/.kannaka-nats.env"; set +a
mkdir -p "$OUT"
log() { printf '[verdict-receipts] %s %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$OUT/log.txt" >&2; }
state() { curl -sf "$URL/api/state" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["epoch"], d["tick"])'; }

# 1. first logged proposal
log "waiting for a proposed entry in $VDIR/proposals.jsonl that the ledger has not settled"
pending() { python3 - "$VDIR/proposals.jsonl" "$VDIR/verdicts.jsonl" <<'PY'
import json, sys
def rows(path):
    try:
        for line in open(path, encoding="utf-8"):
            try: yield json.loads(line)
            except ValueError: return
    except OSError: return
done = {r.get("request_id") for r in rows(sys.argv[2])}
for e in rows(sys.argv[1]):
    if e.get("outcome") == "proposed" and e["request_id"] not in done:
        print(e["epoch"], e["tick"]); break
PY
}
until [ -n "$(pending)" ]; do sleep 30; done
read -r E T <<<"$(pending)"
log "earliest unsettled proposal logged at epoch $E tick $T; settles at tick $((T + SETTLE)) or the next epoch"
{ echo "entries_total=$(wc -l < "$VDIR/proposals.jsonl")"
  echo "entries_proposed=$(grep -c '"outcome":"proposed"' "$VDIR/proposals.jsonl")"
  echo "entries_rejected=$(grep -c '"outcome":"rejected"' "$VDIR/proposals.jsonl")"
  echo "prompts=$(ls "$VDIR/prompts" | wc -l)"; } > "$OUT/1-proposal-log-counts.txt"
ls -la "$VDIR" > "$OUT/1-verdicts-dir.txt"

# 2. settle
while read -r NE NT <<<"$(state)"; do
  [ "$NE" -gt "$E" ] && break
  [ "$NE" -eq "$E" ] && [ $((NT - T)) -ge "$SETTLE" ] && break
  sleep 60
done
log "settled: colony at epoch $NE tick $NT"

# 3. capture the verdict subject while the unit runs once
$PY "$HOME/grid-deploy/bus_capture.py" 240 "GRID.$COLONY.verdict" > "$OUT/3-verdict-capture.jsonl" 2> "$OUT/3-capture.stderr" &
CAP=$!
sleep 5
sudo systemctl start grid-verdicts.service
sleep 3
journalctl -u grid-verdicts --no-pager --since "-3m" 2>/dev/null | grep -v '^Hint' > "$OUT/2-verdicts-journal.txt"
systemctl status grid-verdicts.timer --no-pager 2>/dev/null | head -6 > "$OUT/2-timer.txt"
systemctl list-timers grid-verdicts.timer --no-pager 2>/dev/null >> "$OUT/2-timer.txt"
wait $CAP
log "capture: $(wc -l < "$OUT/3-verdict-capture.jsonl") verdict envelopes"

# 4. ledger receipts (counts and chain only)
cd $ROOT/kannaka-grid && $PY - "$VDIR/verdicts.jsonl" > "$OUT/4-ledger.txt" <<'PY'
import collections, sys
from pathlib import Path
from grid.verdicts import Ledger
led = Ledger(Path(sys.argv[1]))
rows = led.rows()
print("rows", len(rows)); print("verify", led.verify()); print("head", led.head())
print("by_tier", dict(collections.Counter(r["tier"] for r in rows)))
print("epoch_found", dict(collections.Counter(str(r.get("epoch_found")) for r in rows)))
PY

# 5. export
EXP=$(mktemp -d "$VDIR/export.XXXXXX")
$PY -m grid.verdict_corpus --verdicts-dir "$VDIR" --out "$EXP" > "$OUT/5-export.stdout" 2>&1
cp "$EXP/manifest.json" "$OUT/5-manifest.json" 2>/dev/null
{ echo "records=$(wc -l < "$EXP/records.jsonl" 2>/dev/null || echo 0)"; echo "negatives=$(wc -l < "$EXP/negatives.jsonl" 2>/dev/null || echo 0)"; } > "$OUT/5-export-counts.txt"
rm -rf "$EXP"
log "export manifest written; corpus records stay on the host"
echo "$OUT"
