#!/usr/bin/env bash
# deploy/skywave/receipts.sh — collect the six acceptance receipts (spec §12,
# deploy/README.md §3) unattended and write them to /srv/grid/receipts-<ts>/.
# Takes about 12 minutes: a 5-minute bus capture that overlaps a 3-minute
# storm-forcing burst, then a wait for the first Odin request, then the
# stop-and-measure check. Safe to re-run.
set -uo pipefail
ROOT=/srv/grid
VENV=$ROOT/venv
COLONY=colony-one
MEM=$ROOT/$COLONY/memory
KANNAKA="$HOME/.local/bin/kannaka"
OUT=$ROOT/receipts-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT"
log() { printf '[receipts] %s\n' "$*" >&2; }
state() { curl -fs 127.0.0.1:8787/api/state; }
set -a; . "$HOME/.kannaka-nats.env"; set +a

# 0. units
systemctl is-active thegrid-colony grid-mind grid-bridge grid-odin > "$OUT/0-units.txt" 2>&1
journalctl -u thegrid-colony --no-pager | grep -o 'heat source: [a-z0-9]*' | tail -1 >> "$OUT/0-units.txt"
log "0: $(tr '\n' ' ' < "$OUT/0-units.txt")"

# 1 + 2 + 5. bus capture (5 min) with a 3-min one-core burst starting at t=30s
state > "$OUT/1-state-before.json"
$VENV/bin/python $ROOT/kannaka-grid/deploy/skywave/bus_capture.py 300 'GRID.>' > "$OUT/1-bus.ndjson" 2> "$OUT/1-bus.err" &
CAP=$!
sleep 30
log "2: forcing a storm — one core busy for 180 s"
( timeout 180 python3 -c 'while True: pass' ) &
BURST=$!
wait $BURST 2>/dev/null
wait $CAP
state > "$OUT/1-state-after.json"
python3 - "$OUT/1-bus.ndjson" > "$OUT/1-2-5-summary.txt" <<'PY'
import collections, json, sys
kinds = collections.Counter()
for line in open(sys.argv[1]):
    line = line.strip()
    if not line: continue
    row = json.loads(line)
    if "_capture" in row: print("capture:", row["_capture"]); continue
    kinds[row["subject"]] += 1
for s, n in sorted(kinds.items()): print(f"{n:5d}  {s}")
need = {"1 epoch.started": any(".event.epoch.started" in s for s in kinds),
        "1 state": any(".state" in s for s in kinds),
        "2 storm": any(".event.storm" in s for s in kinds),
        "5 chronicle": sum(1 for s in kinds if ".chronicle." in s)}
print("receipts:", need)
PY
log "1/2/5: $(tail -1 "$OUT/1-2-5-summary.txt")"

# 3. mind memory count + recall
KANNAKA_DATA_DIR=$MEM $KANNAKA status 2>/dev/null | grep -iE 'memor|count' | head -3 > "$OUT/3-mind.txt"
KANNAKA_DATA_DIR=$MEM $KANNAKA recall "storm" 2>/dev/null | head -5 >> "$OUT/3-mind.txt"
log "3: $(head -1 "$OUT/3-mind.txt")"

# 4. Odin queue: wait up to 10 min for a request, then for the proposal
Q=$ROOT/$COLONY/state/operator
for i in $(seq 1 120); do
  ls "$Q"/request.json >/dev/null 2>&1 && break; sleep 5
done
ls -la "$Q" > "$OUT/4-operator-dir.txt" 2>&1
for i in $(seq 1 60); do
  ls "$Q"/proposal.json >/dev/null 2>&1 && { cp "$Q"/proposal.json "$OUT/4-proposal.json"; break; }; sleep 5
done
journalctl -u grid-odin --no-pager -n 40 > "$OUT/4-odin-journal.txt"
curl -fs 127.0.0.1:8787/api/history | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("mutationMechanisms"), indent=1))' > "$OUT/4-mechanisms.json" 2>&1
log "4: $(grep -c proposed "$OUT/4-odin-journal.txt") proposed lines in the relay journal; proposal captured: $([ -f "$OUT/4-proposal.json" ] && echo yes || echo no)"

# 6. stop the fusion, measure the colony's tick rate, start it again
T0=$(state | python3 -c 'import json,sys; print(json.load(sys.stdin)["tick"])'); sleep 30
T1=$(state | python3 -c 'import json,sys; print(json.load(sys.stdin)["tick"])')
sudo systemctl stop grid-bridge grid-odin grid-mind
T2=$(state | python3 -c 'import json,sys; print(json.load(sys.stdin)["tick"])'); sleep 30
T3=$(state | python3 -c 'import json,sys; print(json.load(sys.stdin)["tick"])')
sudo systemctl start grid-mind grid-bridge grid-odin
echo "with fusion: $((T1-T0)) ticks/30s · without: $((T3-T2)) ticks/30s" > "$OUT/6-tickrate.txt"
log "6: $(cat "$OUT/6-tickrate.txt")"

echo "$OUT"
