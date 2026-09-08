#!/usr/bin/env bash
# deploy/skywave/verdict-receipts.sh - unattended receipts for the verdict
# pipeline (deploy/README.md §4). Waits for the relay to log its first proposal,
# waits for that proposal to settle (SETTLE_TICKS later, or the next epoch),
# captures GRID.<colony>.verdict while the nightly unit runs once by hand, exports
# the corpus, and records which prompt variant produced the window's proposals and
# how well each arm answered. Writes /srv/grid/receipts-verdicts-<ts>/ and prints it.
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

# 6. prompt state - the prompt dimension's counterpart of the model_alias receipts above.
# A receipt has shown WHICH MODEL answered since sub-project 2; since the prompt loop it
# also has to show WHICH WORDING, or a week's numbers cannot be attributed at all. Prints
# the registry default, the variant the relay is actually serving, the candidate and split
# if a prompt A/B is running, and each arm's answer quality over the same window the gate
# judges on. Ids, counts and ratios only: no template text and no reply text, the same
# held-back rule as everything above.
cd $ROOT/kannaka-grid && $PY - "$VDIR" "${GRID_ENV:-$ROOT/grid.env}" "${RELEASES_DIR:-$ROOT/releases}" \
  > "$OUT/6-prompt-state.txt" 2>&1 <<'PY'
import sys
from pathlib import Path

from grid.variants import load_registry
from grid.versions import Versions
# _env_get on purpose: a receipt that read grid.env more forgivingly than the gate does
# would hide exactly the divergence it exists to expose.
from grid.weekly import _env_get, ab_arms, ab_state

vdir, env_path, releases = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
default_id, variants = load_registry()
print("registry_default", default_id)
print("registry_variants", ",".join(sorted(variants)))
for vid in sorted(variants):
    v = variants[vid]
    print(f"variant {vid} sha256={v.sha256} temperature={v.temperature} "
          f"max_tokens={v.max_tokens} recall={v.recall}")

# Blank or absent is not "unknown": odin resolves it to the registry default and logs every
# proposal under THAT id, so this is the label the arms below are counted under.
live = (_env_get(env_path, "MIND_PROMPT_VARIANT") or "").strip() or default_id
candidate = (_env_get(env_path, "MIND_PROMPT_CANDIDATE") or "").strip()
split = (_env_get(env_path, "MIND_PROMPT_AB_SPLIT") or "").strip() or "0.2 (default)"
print("active_variant", live, "(known to the registry)" if live in variants else "(NOT IN THE REGISTRY: the relay refuses this at boot)")
print("prompt_candidate", candidate or "none")
print("prompt_split", split if candidate else "n/a")

state = ab_state(Versions(releases / "versions.jsonl"), dimension="prompt")
if state is not None:
    since, source = state["since_ts"], "ledger ab_started row"
    incumbent, cand = state["incumbent"], state["candidate"]
else:
    # No ab_started row: either nothing is running, or the A/B was hand-started by editing
    # grid.env, which has no row by construction. Either way there is no window start, so the
    # arms cover all recorded history -- the same reading grid.weekly's env-only view takes.
    since, source = 0, "no ledger row; arms cover all recorded history"
    incumbent, cand = live, (candidate or live)
print("window_since_ts", since, "-", source)
arms = ab_arms(vdir, since_ts=since, incumbent=incumbent, candidate=cand, dimension="prompt")
for arm in sorted(arms):
    if not arm:
        continue
    a = arms[arm]
    role = "candidate" if arm == cand and cand != incumbent else "incumbent"
    print(f"arm {arm} ({role}) answered={a['answered']} valid={a['valid']} "
          f"unanswered={a['unanswered']} validity={a['validity']:.3f} "
          f"settled={a['settled']} reproduced={a['reproduced']} rate={a['rate']:.3f}")
PY
log "prompt state written: $(grep -c '^arm ' "$OUT/6-prompt-state.txt" 2>/dev/null || echo 0) arm(s)"
echo "$OUT"
