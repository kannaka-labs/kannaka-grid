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
#      bus connected and the Relay was built -- i.e. after every fail-closed gate; and
#   2. the model and prompt lines it logged name the values this unit's EnvironmentFile
#      just handed it, so a relay that came back on a STALE configuration (the file
#      changed under a process that was never actually restarted, a candidate that is
#      not the one the gate wrote) fails the check too.
# It gives up the moment the main process is gone rather than waiting out the deadline:
# that is the refused-configuration case, and it does not improve with time.
#
# The journal is matched on _SYSTEMD_INVOCATION_ID so that a line from the PREVIOUS
# start can never be read as this one's -- which would turn this gate into a light that
# is always green. That match reads the system journal, hence
# SupplementaryGroups=systemd-journal on grid-odin.service.
#
# Overrides (environment): RELAY_READY_TIMEOUT_S (default 60), RELAY_READY_UNIT.
set -uo pipefail

UNIT=${RELAY_READY_UNIT:-grid-odin.service}
DEADLINE_S=${RELAY_READY_TIMEOUT_S:-60}
INTERVAL_S=0.25

say()  { printf 'relay-ready: %s\n' "$*" >&2; }
fail() { say "$*"; exit 1; }

# No fallback to `journalctl -u <unit> --since ...`: that window can contain the
# previous start's lines, and a readiness check that can pass on last start's evidence
# is worse than none. Fail closed and say why.
[ -n "${INVOCATION_ID:-}" ] || fail "INVOCATION_ID is unset, so this start's journal cannot be told from the last one's; systemd 232+ sets it for ExecStartPost"

# What the relay must have logged, built from the environment IT was started with.
# grid/odin.py logs "A/B: candidate <tag> at split <x>" or "A/B: none", and
# "prompt A/B: <candidate> vs <incumbent> at split <x>" or "prompt: <id> (no A/B)".
if [ -n "${MIND_LLM_CANDIDATE:-}" ]; then
    want_model="A/B: candidate ${MIND_LLM_CANDIDATE} at split"
else
    want_model="A/B: none"
fi
# An empty MIND_PROMPT_VARIANT is not "unknown": odin resolves it to the registry
# default and logs THAT id, so the expected string stops before the id and the trailing
# space is what keeps it from matching a longer line. When the line is set, the ` at
# split` / ` (no A/B)` suffix is included so "v1" cannot match "v1-sandbox".
if [ -n "${MIND_PROMPT_CANDIDATE:-}" ]; then
    if [ -n "${MIND_PROMPT_VARIANT:-}" ]; then
        want_prompt="prompt A/B: ${MIND_PROMPT_CANDIDATE} vs ${MIND_PROMPT_VARIANT} at split"
    else
        want_prompt="prompt A/B: ${MIND_PROMPT_CANDIDATE} vs "
    fi
else
    if [ -n "${MIND_PROMPT_VARIANT:-}" ]; then
        want_prompt="prompt: ${MIND_PROMPT_VARIANT} (no A/B)"
    else
        want_prompt="prompt: "
    fi
fi

deadline=$(( $(date +%s) + DEADLINE_S ))
journal=""
while : ; do
    journal=$(journalctl "_SYSTEMD_INVOCATION_ID=${INVOCATION_ID}" --no-pager -o cat 2>/dev/null)
    printf '%s\n' "$journal" | grep -qF "grid.odin INFO watching " && break
    pid=$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null)
    if [ -z "$pid" ] || [ "$pid" = "0" ] || ! kill -0 "$pid" 2>/dev/null; then
        printf '%s\n' "$journal" | tail -n 20 >&2
        fail "the relay exited during start: it read /srv/grid/grid.env and refused it (see the lines above). grid.env and the running unit now disagree; whatever asked for this restart is being told so."
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        printf '%s\n' "$journal" | tail -n 20 >&2
        fail "the relay is still alive after ${DEADLINE_S}s but has not logged 'watching': it never got past connecting to NATS or resolving its config (see the lines above). Raise RELAY_READY_TIMEOUT_S if the bus is merely slow."
    fi
    sleep "$INTERVAL_S"
done

for want in "$want_model" "$want_prompt"; do
    printf '%s\n' "$journal" | grep -qF "$want" || {
        printf '%s\n' "$journal" | grep -E 'A/B:|prompt:|prompt A/B:' >&2
        fail "the relay came up on a configuration this unit did not hand it: expected a log line containing \"${want}\" (see the lines above). It is serving something other than /srv/grid/grid.env."
    }
done
say "$UNIT is up and serving the configuration in /srv/grid/grid.env"