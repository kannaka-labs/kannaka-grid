#!/usr/bin/env bash
# deploy/skywave/relay-ready.sh - grid-odin.service's ExecStartPost readiness gate.
#
# WHY THIS EXISTS. grid-odin is Type=simple, so systemd calls the start job done the
# moment it has forked the process: `systemctl restart grid-odin.service` returned 0
# even when the relay died a second later on a configuration it refuses. Every one of
# those refusals is deliberate and fail-closed in grid/odin.py -- an unknown
# MIND_PROMPT_VARIANT or MIND_PROMPT_CANDIDATE (VariantError), a candidate equal to the
# incumbent, a split outside (0,1), a variant whose sampling the NATS route cannot carry
# -- and each one leaves the colony's mind DOWN, not merely mislabelled.
#
# The weekly gate reads that exit code and nothing else: `_restart_relay` in
# grid/weekly.py treats 0 as "the relay picked up the new values", so the revert-and-
# restart path whose own comments say "a value it refuses leaves the unit DOWN" never
# ran for the exact case it was written for. The gate wrote `ab_started` and moved on.
# Fixing it here, in the unit, fixes it for every caller of `systemctl restart` at once:
# the gate, deploy.sh, and an operator's own hand.
#
# WHAT IT CHECKS. Not "did enough time pass": there is no sleep standing in for a check
# here. It waits for POSITIVE evidence, scoped to this exact start:
#   1. the relay logged "watching <queue> for <agent>", which grid/odin.py emits only
#      after the env parsed, the variants resolved, the sampling route was accepted, the
#      bus connected and the Relay was built -- i.e. after every fail-closed gate;
#   2. the model and prompt lines it logged name the values this unit's EnvironmentFile
#      just handed it, so a relay that came back on a STALE configuration (the file
#      changed under a process that was never actually restarted, a candidate that is
#      not the one the gate wrote) fails the check too; and
#   3. the relay is STILL alive after all of that -- see the re-check at the end.
# It gives up the moment the main process is gone rather than waiting out the deadline:
# that is the refused-configuration case, and it does not improve with time.
#
# The journal is matched on _SYSTEMD_INVOCATION_ID so that a line from the PREVIOUS
# start can never be read as this one's -- which would turn this gate into a light that
# is always green. That match reads the system journal, hence
# SupplementaryGroups=systemd-journal on grid-odin.service.
#
# Overrides (environment): RELAY_READY_TIMEOUT_S (default 60), RELAY_READY_UNIT,
# RELAY_READY_PYTHON. For a systemd unit these are set with a drop-in, not an export:
#   sudo systemctl edit grid-odin.service   ->   [Service]
#                                                Environment=RELAY_READY_TIMEOUT_S=120
# and TimeoutStartSec= in the unit is the ceiling above whatever is set here.
set -uo pipefail

UNIT=${RELAY_READY_UNIT:-grid-odin.service}
DEADLINE_S=${RELAY_READY_TIMEOUT_S:-60}
PY=${RELAY_READY_PYTHON:-/srv/grid/venv/bin/python}
INTERVAL_S=0.25

say()  { printf 'relay-ready: %s\n' "$*" >&2; }
fail() { say "$*"; exit 1; }
tail_journal() { printf '%s\n' "$journal" | tail -n 20 >&2; }

# The relay's main process, or "0"/empty when systemd has none. An empty or failing
# `systemctl` reads as dead, which is the fail-closed direction.
relay_alive() {
    local pid
    pid=$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null)
    [ -n "$pid" ] && [ "$pid" != "0" ] && kill -0 "$pid" 2>/dev/null
}

# No fallback to `journalctl -u <unit> --since ...`: that window can contain the
# previous start's lines, and a readiness check that can pass on last start's evidence
# is worse than none. Fail closed and say why.
[ -n "${INVOCATION_ID:-}" ] || fail "INVOCATION_ID is unset, so this start's journal cannot be told from the last one's; systemd 232+ sets it for ExecStartPost"

# What the relay must have logged, built from the environment IT was started with.
# grid/odin.py logs "A/B: candidate <tag> at split <x>" or "A/B: none", and
# "prompt A/B: <candidate> vs <incumbent> at split <x>" or "prompt: <id> (no A/B)".
#
# EVERY expected string is anchored at BOTH ends -- the `grid.odin INFO ` prefix that
# logging's basicConfig format puts in front of the message, and the message's own
# trailing text. `grep -F` is a substring match, so an expectation that stopped at
# "prompt: " would be satisfied by a relay serving any variant at all; that is not a
# theoretical hole, it was the shipped default configuration, because
# MIND_PROMPT_VARIANT is blank until the first prompt release writes it and the gate
# writes only the candidate and the split when it starts an experiment.
if [ -n "${MIND_LLM_CANDIDATE:-}" ]; then
    want_model="grid.odin INFO A/B: candidate ${MIND_LLM_CANDIDATE} at split "
else
    want_model="grid.odin INFO A/B: none"
fi

# A blank MIND_PROMPT_VARIANT is not "unknown" and must not degrade the check: odin
# resolves it to the registry's own default and logs THAT id, so the probe resolves the
# same default from the same registry and expects the same id. Resolved before the wait
# loop and failed closed, because an expectation that cannot be built is not an
# expectation that can be skipped.
live_variant=${MIND_PROMPT_VARIANT:-}
if [ -z "$live_variant" ]; then
    # Status, not shape: get_variant() is the same fail-closed call the relay makes, so a
    # non-zero exit here means the relay's own start is failing on the same registry.
    if ! resolved=$("$PY" -c 'from grid.variants import get_variant; print(get_variant().id)' 2>&1); then
        fail "MIND_PROMPT_VARIANT is blank and the registry default could not be resolved with $PY: ${resolved}. The relay's own start fails on the same registry."
    fi
    live_variant=${resolved%%$'\n'*}
    [ -n "$live_variant" ] || fail "MIND_PROMPT_VARIANT is blank and $PY printed no registry default"
fi
if [ -n "${MIND_PROMPT_CANDIDATE:-}" ]; then
    want_prompt="grid.odin INFO prompt A/B: ${MIND_PROMPT_CANDIDATE} vs ${live_variant} at split "
else
    want_prompt="grid.odin INFO prompt: ${live_variant} (no A/B)"
fi

deadline=$(( $(date +%s) + DEADLINE_S ))
journal=""
while : ; do
    journal=$(journalctl "_SYSTEMD_INVOCATION_ID=${INVOCATION_ID}" --no-pager -o cat 2>/dev/null)
    printf '%s\n' "$journal" | grep -qF "grid.odin INFO watching " && break
    if ! relay_alive; then
        tail_journal
        fail "the relay exited during start: it read /srv/grid/grid.env and refused it (see the lines above). grid.env and the running unit now disagree; whatever asked for this restart is being told so."
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        tail_journal
        fail "the relay is still alive after ${DEADLINE_S}s but has not logged 'watching': it never got past connecting to NATS or resolving its config (see the lines above). Raise RELAY_READY_TIMEOUT_S (a systemctl edit drop-in) if the bus is merely slow."
    fi
    sleep "$INTERVAL_S"
done

for want in "$want_model" "$want_prompt"; do
    printf '%s\n' "$journal" | grep -qF "$want" || {
        printf '%s\n' "$journal" | grep -E 'A/B:|prompt:' >&2
        fail "the relay came up on a configuration this unit did not hand it: expected a log line containing \"${want}\" (see the lines above). It is serving something other than /srv/grid/grid.env."
    }
done

# Liveness again, AFTER the ready line. Reaching `watching` is not the same as staying
# up: the first poll can hit BusClosed and exit 75, and a probe that stopped checking at
# the ready line would report that relay as started. Cheap, and it closes the window
# between the log write and the end of this script.
relay_alive || {
    tail_journal
    fail "the relay logged that it was watching the queue and then exited before this check finished (see the lines above); it is NOT serving."
}
say "$UNIT is up and serving the configuration in /srv/grid/grid.env"