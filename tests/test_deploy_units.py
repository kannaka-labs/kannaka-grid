import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UNITS = REPO / "deploy" / "systemd"
SKYWAVE = REPO / "deploy" / "skywave"


def unit(name: str) -> str:
    return (UNITS / name).read_text()


def test_all_units_exist_and_restart():
    for name in ("thegrid-colony.service", "grid-bridge.service", "grid-odin.service", "grid-mind.service"):
        text = unit(name)
        assert "Restart=always" in text, name
        assert "ExecStart=" in text, name
        assert "StartLimitBurst=" in text, name


def test_colony_binds_localhost_and_is_bounded():
    text = unit("thegrid-colony.service")
    assert "--host 127.0.0.1" in text
    assert "--mutator odin" in text
    assert "--state /srv/grid/colony-one/state/colony.pkl" in text
    assert "MemoryMax=192M" in text and "MemorySwapMax=0" in text and "CPUQuota=5%" in text
    assert "grid.launcher" in text


def test_mind_is_readonly_daemon_with_own_data_dir():
    text = unit("grid-mind.service")
    assert "KANNAKA_READONLY=1" in text
    assert "KANNAKA_DATA_DIR=/srv/grid/colony-one/memory" in text
    assert "swarm serve" in text


def test_dream_timer_restarts_the_mind_so_new_memories_load():
    assert "OnUnitActiveSec=6h" in unit("grid-mind-dream.timer")
    dream = unit("grid-mind-dream.service")
    assert "kannaka dream" in dream and "systemctl restart grid-mind.service" in dream


def test_bridge_and_relay_read_the_shared_env():
    for name in ("grid-bridge.service", "grid-odin.service"):
        assert "EnvironmentFile=/srv/grid/grid.env" in unit(name), name


def test_verdicts_timer_runs_nightly_and_reads_the_shared_env():
    svc = unit("grid-verdicts.service")
    assert "Type=oneshot" in svc and "grid.verdicts" in svc and "EnvironmentFile=/srv/grid/grid.env" in svc
    assert "EnvironmentFile=/home/nick/.kannaka-nats.env" in svc
    timer = unit("grid-verdicts.timer")
    assert "OnCalendar=" in timer and "Persistent=true" in timer


def test_weekly_release_timer_runs_sunday_morning_after_the_rogue_weekly():
    svc = unit("grid-weekly-release.service")
    assert "Type=oneshot" in svc and "grid.weekly release" in svc and "EnvironmentFile=/srv/grid/grid.env" in svc
    assert "/srv/grid/venv/bin/python" in svc
    timer = unit("grid-weekly-release.timer")
    assert "OnCalendar=Sun *-*-* 06:00:00" in timer and "Persistent=true" in timer


def test_verdicts_unit_runs_the_ab_stuck_check_after_settling():
    svc = unit("grid-verdicts.service")
    assert "ExecStartPost=/srv/grid/venv/bin/python -m grid.weekly ab --check" in svc


def inline_comment_keys(text: str) -> list[tuple[int, str]]:
    """(line number, key) for every KEY=value line whose VALUE contains a `#`.

    Commented-out keys count: they are templates an operator uncomments. D6: any `#` at all,
    not just ` #` -- `ODIN_RETRY_S=60# x` and a tab before the `#` kill the relay identically.
    """
    import re
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.lstrip("#").lstrip())
        if m and "#" in m.group(2):
            out.append((n, m.group(1)))
    return out


def test_grid_env_example_has_no_inline_comments():
    """N3: systemd's EnvironmentFile keeps everything after the `=`, so a value line such as
    `ODIN_RETRY_S=60          # 900 with a CPU model` makes float() raise inside
    RelayConfig.from_env -- grid-odin then crash-loops into its StartLimit and stays dead, the
    exact C1 failure, shipped from the example file. Neither the gate nor the relay strips
    comments (deliberately: they must read a value identically), so the file must never carry
    one after a value. Mutation: put the comment back on ODIN_RETRY_S -> RED.
    """
    example = UNITS.parent / "grid.env.example"
    assert inline_comment_keys(example.read_text(encoding="utf-8")) == []


def test_the_inline_comment_guard_catches_every_shape():
    # D6: the guard matched only " #", so a tight `KEY=60# x` and a tab before the `#` both
    # sailed past while killing the relay exactly the same way. Mutation: `"#" in m.group(2)`
    # -> `" #" in m.group(2)` -> the tight and tabbed cases come back empty.
    assert inline_comment_keys("ODIN_RETRY_S=60          # spaced\n") == [(1, "ODIN_RETRY_S")]
    assert inline_comment_keys("ODIN_RETRY_S=60# tight\n") == [(1, "ODIN_RETRY_S")]
    assert inline_comment_keys("ODIN_RETRY_S=60\t# tabbed\n") == [(1, "ODIN_RETRY_S")]
    assert inline_comment_keys("#MIND_AB_SPLIT=0.2  # a commented-out template\n") == [(1, "MIND_AB_SPLIT")]
    assert inline_comment_keys("# prose about key=value with a # in it\n") == []      # not a key line
    assert inline_comment_keys("ODIN_RETRY_S=60\nMIND_MODEL_ALIAS=kannaka-brain-v3\n") == []


# --- the prompt dimension's three grid.env lines -------------------------------------------

PROMPT_ENV_KEYS = ("MIND_PROMPT_VARIANT", "MIND_PROMPT_CANDIDATE", "MIND_PROMPT_AB_SPLIT")


def test_grid_env_example_documents_the_prompt_keys():
    """The example file is where an operator learns a knob exists, and `deploy.sh` copies it to
    /srv/grid/grid.env on a fresh host. All three lines of the prompt dimension have to be in it
    or the feature is undiscoverable: `grid.weekly`'s `_DIMENSION_ENV` names exactly these three,
    and it is the gate that writes them.

    Each must be COMMENTED OUT with its explanation on its own line ABOVE it. The inline-comment
    guard above owns the second half of that: a `#` after the value is kept IN the value by
    systemd's EnvironmentFile and kills the relay at boot.

    Mutation: delete any one of the three lines, or uncomment one -> RED.
    """
    lines = (UNITS.parent / "grid.env.example").read_text(encoding="utf-8").splitlines()
    for key in PROMPT_ENV_KEYS:
        commented = [n for n, line in enumerate(lines) if line.startswith(f"#{key}=")]
        assert commented, f"{key} is missing from grid.env.example, or is not commented out"
        for n in commented:
            assert n > 0 and lines[n - 1].startswith("#"), f"{key} has no explanation line above it"


def test_the_prompt_keys_in_the_example_are_the_ones_the_gate_and_the_relay_read():
    # Pinned to both readers rather than typed twice: the gate writes these lines and the relay
    # parses them, so a rename that reaches only one side is what this catches. Mutation: rename a
    # key in _DIMENSION_ENV or in RelayConfig.from_env without touching the example -> RED.
    from grid.weekly import _DIMENSION_ENV
    assert set(_DIMENSION_ENV["prompt"].values()) == set(PROMPT_ENV_KEYS)
    odin = (REPO / "grid" / "odin.py").read_text(encoding="utf-8")
    for key in PROMPT_ENV_KEYS:
        assert f'"{key}"' in odin, f"{key} is in grid.env.example but grid/odin.py never reads it"


# --- a restart that reports success must mean the relay came back ---------------------------

def test_the_relay_start_is_gated_on_the_relay_actually_coming_up():
    """C1, reopened by systemd's own semantics: Type=simple finishes the start job at fork, so
    `systemctl restart grid-odin.service` exits 0 even when the relay dies a second later on a
    config it refuses -- and grid.weekly's `_restart_relay` reads that exit code as "the relay
    picked up the new values", so the revert-and-restart path never ran for the case its own
    comments describe. ExecStartPost is what makes the job's result the truth.

    Mutation: drop the ExecStartPost line -> RED, and every A/B start silently succeeds again.
    """
    text = unit("grid-odin.service")
    assert "Type=simple" in text
    assert "ExecStartPost=/bin/bash /srv/grid/kannaka-grid/deploy/skywave/relay-ready.sh" in text
    # the probe reads this start's journal; without the group it cannot, and fails every start
    assert "SupplementaryGroups=systemd-journal" in text
    # ...and systemd must not kill the start job before the probe's own 60 s deadline
    assert "TimeoutStartSec=120" in text
    assert (SKYWAVE / "relay-ready.sh").is_file()


def test_the_readiness_probe_looks_for_lines_grid_odin_actually_logs():
    """The probe is worth exactly as much as its coupling to the relay's log. Looking for a line
    odin no longer writes makes every start fail; looking for one it writes unconditionally makes
    every start pass, which is this task's hole silently reopened. So each literal the probe greps
    is pinned to odin's own format string, and the `grid.odin INFO ` prefix to the logger name and
    the basicConfig format that produce it.

    Mutation: reword any one of these log calls in grid/odin.py, or any one grep in the probe -> RED.
    """
    probe = (SKYWAVE / "relay-ready.sh").read_text(encoding="utf-8")
    odin = (REPO / "grid" / "odin.py").read_text(encoding="utf-8")
    assert 'logging.getLogger("grid.odin")' in odin
    assert '"%(asctime)s %(name)s %(levelname)s %(message)s"' in odin
    for fmt, wanted in (
        ('log.info("watching %s for %s"', "grid.odin INFO watching "),
        ('log.info("A/B: none")', 'want_model="grid.odin INFO A/B: none"'),
        ('log.info("A/B: candidate %s at split %.2f"',
         'want_model="grid.odin INFO A/B: candidate ${MIND_LLM_CANDIDATE} at split "'),
        ('log.info("prompt: %s (no A/B)"',
         'want_prompt="grid.odin INFO prompt: ${live_variant} (no A/B)"'),
        ('log.info("prompt A/B: %s vs %s at split %.2f (MIND_PROMPT_AB_SPLIT)"',
         'want_prompt="grid.odin INFO prompt A/B: ${MIND_PROMPT_CANDIDATE} vs ${live_variant} at split "'),
    ):
        assert fmt in odin, f"grid/odin.py no longer logs {fmt}"
        assert wanted in probe, f"relay-ready.sh no longer checks for {wanted}"


def test_the_readiness_probe_scopes_the_journal_to_this_start():
    # Without _SYSTEMD_INVOCATION_ID the probe can pass on the PREVIOUS start's "watching" line --
    # it goes green while the relay is down, which is worse than no check at all. The unset case is
    # refused rather than falling back to a time window, for the same reason.
    # Mutation: swap the match for `-u grid-odin.service --since -2min` -> RED.
    probe = (SKYWAVE / "relay-ready.sh").read_text(encoding="utf-8")
    assert "_SYSTEMD_INVOCATION_ID=${INVOCATION_ID}" in probe
    assert '[ -n "${INVOCATION_ID:-}" ] || fail' in probe
    # and it must give up on a dead process rather than waiting out the deadline
    assert 'kill -0 "$pid"' in probe


# --- the receipt shows the prompt state the way it shows the model state --------------------

def _receipts_block(marker: str) -> str:
    """The python heredoc verdict-receipts.sh runs on the host, extracted verbatim.

    Run, not grepped: a receipt section is a program, and the way it breaks is grid.weekly's API
    moving under it on a host where nobody is looking until the receipt comes back empty.
    """
    text = (SKYWAVE / "verdict-receipts.sh").read_text(encoding="utf-8")
    body = text.split(marker, 1)[1].split("<<'PY'\n", 1)[1]
    return body.split("\nPY\n", 1)[0]


def _prompt_state(tmp_path, monkeypatch, capsys, *, env_text: str, rows: list[dict]) -> str:
    import json

    vdir, releases = tmp_path / "verdicts", tmp_path / "releases"
    vdir.mkdir()
    releases.mkdir()
    env = tmp_path / "grid.env"
    env.write_text(env_text, encoding="utf-8")
    (vdir / "proposals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["6-prompt-state", str(vdir), str(env), str(releases)])
    exec(compile(_receipts_block('"$OUT/6-prompt-state.txt"'), "verdict-receipts.sh:6", "exec"),
         {"__name__": "__main__"})
    return capsys.readouterr().out


def test_the_verdict_receipt_prints_the_prompt_state(tmp_path, monkeypatch, capsys):
    """A receipt has shown WHICH MODEL answered since sub-project 2. Since the prompt loop it has
    to show WHICH WORDING and how well each arm answered, or a window's rows cannot be attributed
    at all. Runs the shipped section against a fixture corpus.

    Mutation: drop the `validity=` field, the `active_variant` line or the `registry_default` line
    from the section -> RED. Move `ab_arms` or `_env_get` in grid.weekly without it -> RED.
    """
    out = _prompt_state(
        tmp_path, monkeypatch, capsys,
        env_text="MIND_MODEL_ALIAS=kannaka-brain-v3\n",
        rows=[
            {"request_id": "r1", "ts": 10, "outcome": "proposed", "prompt_variant": "v1-sandbox"},
            {"request_id": "r2", "ts": 11, "outcome": "rejected", "reason": "unknown opcodes: ['x']",
             "prompt_variant": "v1-sandbox"},
            # binned after repeated defers: a gateway outage, outside BOTH sides of the ratio
            {"request_id": "r3", "ts": 12, "outcome": "rejected", "reason": "deferred 4 times",
             "prompt_variant": "v1-sandbox"},
        ])
    assert "registry_default v1-sandbox" in out
    assert "registry_variants v1-sandbox," in out       # the default AND something to test it against
    # a blank MIND_PROMPT_VARIANT is not "unknown": odin resolves it to the registry default and
    # logs every row under that id, so that is the label the arm below is counted under
    assert "active_variant v1-sandbox (known to the registry)" in out
    assert "prompt_candidate none" in out
    assert "arm v1-sandbox (incumbent) answered=2 valid=1 unanswered=1 validity=0.500" in out


def test_the_receipt_names_the_candidate_arm_when_a_prompt_ab_is_running(tmp_path, monkeypatch, capsys):
    # The hand-started shape from README section 6: a split in grid.env with no ledger row at all.
    # Mutation: read the candidate from the ledger only -> the candidate arm vanishes from the
    # receipt for every smoke test, which is the one run an operator collects it for.
    out = _prompt_state(
        tmp_path, monkeypatch, capsys,
        env_text=("MIND_PROMPT_VARIANT=v1-sandbox\nMIND_PROMPT_CANDIDATE=v2-minimal-edit\n"
                  "MIND_PROMPT_AB_SPLIT=0.2\n"),
        rows=[{"request_id": f"r{i}", "ts": 100 + i, "outcome": "proposed",
               "prompt_variant": "v1-sandbox" if i % 2 else "v2-minimal-edit"} for i in range(6)])
    assert "prompt_candidate v2-minimal-edit" in out and "prompt_split 0.2" in out
    assert "arm v1-sandbox (incumbent) answered=3 valid=3" in out
    assert "arm v2-minimal-edit (candidate) answered=3 valid=3" in out


# --- the units that PAY for the restart must outlive it -------------------------------------

def restart_budget_s() -> int:
    """Worst-case seconds one gate verb can spend inside `systemctl restart grid-odin.service`.

    Derived, never typed: the readiness gate turned a restart from a milliseconds-long fork into
    something bounded by grid-odin's own start timeout, and this budget has to move when any of
    the three numbers it is built from moves.

    dimensions x {the restart, the restart after the revert} x attempts x grid-odin's ceiling.
    Each `systemctl restart` is bounded by grid-odin's TimeoutStartSec (systemd kills the start
    job there), `_restart_relay` makes that many attempts, `_restart_after_revert` calls it a
    second time on the failure path, and `ab --check` reconciles every dimension in turn.
    """
    from grid.odin import DIMENSIONS
    odin_ceiling = int(re.search(r"(?m)^TimeoutStartSec=(\d+)$", unit("grid-odin.service")).group(1))
    weekly = (REPO / "grid" / "weekly.py").read_text(encoding="utf-8")
    attempts = len(re.search(r"for attempt in \(([^)]*)\):", weekly).group(1).split(","))
    return len(DIMENSIONS) * 2 * attempts * odin_ceiling


def start_timeout_s(name: str) -> float:
    m = re.search(r"(?m)^TimeoutStartSec=(\S+)$", unit(name))
    assert m, f"{name} has no TimeoutStartSec="
    return float("inf") if m.group(1) == "infinity" else float(m.group(1))


def test_the_oneshot_units_outlive_the_restarts_they_now_pay_for():
    """`ab --check` and `release` both call `_restart_relay`, and a restart used to return in
    milliseconds. It can now block for the readiness gate's whole deadline, twice per attempt
    pair. A SIGTERM partway through `end_ab` is the worst outcome available on this path:
    grid.env would have the candidate cleared with NO ledger row written, which is precisely the
    env/ledger divergence the gate exists to prevent, arriving through the timeout side door.

    Asserted as "larger than the worst case", not "present": a `TimeoutStartSec=` that is merely
    there is what a future bump of RELAY_READY_TIMEOUT_S or of the attempt count would quietly
    outgrow. `infinity` is accepted for the weekly, where the probes in front of the restart run
    for hours and any finite watchdog would kill a legitimate release mid-teardown.

    Mutation: TimeoutStartSec=300 on grid-verdicts -> RED (300 < the 960 s budget). Remove either
    line -> RED. Raise grid-odin's own TimeoutStartSec to 900 -> RED, because the budget grows
    with it and 1800 no longer covers it.
    """
    budget = restart_budget_s()
    assert budget >= 480, f"the derived restart budget collapsed to {budget}s; check the regexes"
    for name in ("grid-verdicts.service", "grid-weekly-release.service"):
        assert start_timeout_s(name) > budget, (
            f"{name} can be killed mid-teardown: TimeoutStartSec is not above the {budget}s "
            f"worst-case relay-restart budget")


# --- the readiness probe, EXECUTED ----------------------------------------------------------

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="the readiness probe is a bash script")

READY_LINE = ("2026-09-08 12:00:01 grid.odin INFO watching "
              "/srv/grid/colony-one/state/operator for grid-colony-one")
NO_AB = "2026-09-08 12:00:01 grid.odin INFO A/B: none"


def journal_of(*lines: str) -> str:
    return "".join(line + "\n" for line in lines)


@pytest.fixture(scope="module")
def live_pid(tmp_path_factory):
    """A pid the probe's own `kill -0` calls alive, in the shell's OWN pid namespace.

    A pid taken from Python is a Windows pid on the dev box and msys's `kill` does not know it,
    so the holder shell reports its own `$$` and the stub `systemctl` hands that back. On Linux
    the two numbers are the same and this is a no-op.
    """
    pidfile = tmp_path_factory.mktemp("holder") / "pid"
    proc = subprocess.Popen([BASH, "-c", f'echo $$ > "{pidfile.as_posix()}"; sleep 120'])
    try:
        for _ in range(200):
            if pidfile.is_file() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        else:
            pytest.skip("the holder shell never reported its pid")
        yield pidfile.read_text().strip()
    finally:
        proc.kill()
        proc.wait()


def run_probe(tmp_path, *, journal: str, mainpid: str, invocation: str = "0123456789abcdef",
              env: dict | None = None, timeout_s: str = "2"):
    """Run the SHIPPED probe with journalctl and systemctl replaced by stubs.

    The `journalctl` stub behaves like the real one in the one way that matters: an
    `_SYSTEMD_INVOCATION_ID=` match returns the fixture only when the id is the fixture's own,
    while a `-u <unit>` read returns it regardless -- which is what makes a probe that abandoned
    the scoping visibly pass on the previous start's evidence, instead of quietly seeing nothing.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "journalctl").write_bytes(
        b'#!/bin/sh\n'
        b'case "$1" in\n'
        b'  "_SYSTEMD_INVOCATION_ID=$JOURNAL_INVOCATION") cat "$JOURNAL_FIXTURE" ;;\n'
        b'  -u) cat "$JOURNAL_FIXTURE" ;;\n'
        b'esac\n'
        b'exit 0\n')
    (bin_dir / "systemctl").write_bytes(b'#!/bin/sh\necho "$FAKE_MAINPID"\n')
    for stub in ("journalctl", "systemctl"):
        (bin_dir / stub).chmod(0o755)
    fixture = tmp_path / "journal.txt"
    fixture.write_bytes(journal.encode("utf-8"))
    e = {k: v for k, v in os.environ.items()
         if k not in ("MIND_PROMPT_VARIANT", "MIND_PROMPT_CANDIDATE", "MIND_LLM_CANDIDATE")}
    e.update({"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
              "INVOCATION_ID": invocation,
              "JOURNAL_INVOCATION": "0123456789abcdef",
              "JOURNAL_FIXTURE": fixture.as_posix(),
              "FAKE_MAINPID": mainpid,
              "RELAY_READY_TIMEOUT_S": timeout_s,
              "RELAY_READY_PYTHON": sys.executable})
    e.update(env or {})
    return subprocess.run([BASH, (SKYWAVE / "relay-ready.sh").as_posix()],
                          cwd=str(REPO), env=e, capture_output=True, text=True)


def test_deploy_sh_cannot_report_done_over_a_stale_checkout():
    """A deploy that fetched nothing and said DONE is worse than one that failed.

    On 2026-09-08 the repository moved owner, the host's remote still named the old one, and
    `git fetch` failed with "Repository not found". The update was written as
    `fetch && checkout && pull`, and under `set -e` a failure of any command in an AND-list
    EXCEPT THE LAST does not exit the shell -- so the failure was swallowed, every step below
    ran against a day-old checkout, and the script printed DONE. The host was left without the
    code the operator believed they had just deployed.

    Two guards, because the first only covers the steps: each git command fails loudly on its
    own, and then the RESTING STATE is asserted -- the checkout's HEAD must equal
    origin/BRANCH -- because every one of those commands can return zero and still leave the
    checkout behind.

    Mutation A: restore the `fetch && checkout && pull` chain -> RED.
    Mutation B: delete the HEAD-equals-origin assertion -> RED.
    """
    text = (SKYWAVE / "deploy.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text                       # the reason the AND-list is a trap
    fetches = [ln for ln in text.splitlines()
               if "git -C $CHECKOUT fetch" in ln and not ln.lstrip().startswith("#")]
    assert fetches, "deploy.sh no longer fetches"
    for ln in fetches:
        assert "&&" not in ln, ("a failed fetch inside an AND-list is swallowed by set -e; "
                                "it must fail on its own line")
        assert "|| die" in ln, "a failed fetch must die rather than fall through"
    # The resting-state check: what is actually here, not what the steps returned.
    assert 'rev-parse "origin/$BRANCH"' in text, "deploy.sh does not read origin/$BRANCH"
    assert 'rev-parse HEAD' in text, "deploy.sh does not read the checkout HEAD"
    assert any('[ "$have" = "$want" ]' in ln and "die" in ln for ln in text.splitlines()),         "deploy.sh does not refuse when the checkout is not at origin/$BRANCH"


def test_deploy_sh_keeps_the_informative_failure_on_a_failed_relay_start():
    """`enable --now grid-odin.service` can now return non-zero, because the readiness gate can
    fail the start job. Under `set -euo pipefail` a bare call aborts the script with a naked
    exit 1 three lines before the `die` that names the unit and prints the journal - and a failed
    relay start is the single failure where the operator most needs that excerpt.

    Mutation: delete the `|| log ...` handler -> RED.
    """
    text = (SKYWAVE / "deploy.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text                       # the reason the handler is needed
    joined = text.replace("\\\n", " ")
    enable = [ln for ln in joined.splitlines()
              if "systemctl enable --now" in ln and "grid-odin.service" in ln]
    assert enable, "deploy.sh no longer enables grid-odin.service"
    for ln in enable:
        assert "||" in ln, ("a bare `enable --now grid-odin.service` under set -e aborts before "
                            "the per-unit die that prints the journal")
    # ...and the diagnosis it defers to is still there
    assert 'die "$u.service not active: $(journalctl -u $u -n 8 --no-pager)" 6' in text


@needs_bash
def test_the_probe_is_syntactically_valid():
    # The four coupling tests above read the script as text and would pass on a file that bash
    # cannot even parse. Mutation: delete a `fi` -> RED.
    assert subprocess.run([BASH, "-n", (SKYWAVE / "relay-ready.sh").as_posix()],
                          capture_output=True, text=True).returncode == 0


@needs_bash
def test_the_probe_passes_when_the_relay_is_up_on_this_configuration(tmp_path, live_pid):
    # Blank MIND_PROMPT_VARIANT is the shipped default, and it must resolve through the registry
    # to the id odin logs. Mutation: point RELAY_READY_PYTHON at a broken interpreter -> the probe
    # fails closed instead of skipping the comparison.
    r = run_probe(tmp_path, mainpid=live_pid,
                  journal=journal_of(NO_AB,
                                     "2026-09-08 12:00:01 grid.odin INFO prompt: v1-sandbox (no A/B)",
                                     READY_LINE))
    assert r.returncode == 0, r.stderr
    assert "is up and serving the configuration" in r.stderr


@needs_bash
def test_the_probe_fails_when_a_blank_variant_resolves_to_a_different_id(tmp_path, live_pid):
    """The defect this replaced: with MIND_PROMPT_VARIANT blank the expectation was the bare
    string `prompt: `, and `grep -F` is a substring match, so a relay serving ANY variant passed.
    Blank is not an edge case - it is the shipped default and the state through every
    gate-started prompt experiment, because `start_ab` writes only the candidate and the split.

    Mutation: drop the registry resolution and go back to `want_prompt="prompt: "` -> this test
    goes GREEN-when-it-should-be-RED, i.e. exit 0 on a relay serving v99-something-else.
    """
    r = run_probe(tmp_path, mainpid=live_pid,
                  journal=journal_of(NO_AB,
                                     "2026-09-08 12:00:01 grid.odin INFO prompt: v99-something-else (no A/B)",
                                     READY_LINE))
    assert r.returncode == 1
    assert "did not hand it" in r.stderr
    assert "grid.odin INFO prompt: v1-sandbox (no A/B)" in r.stderr      # names what it wanted


@needs_bash
def test_the_probe_checks_the_incumbent_of_a_running_prompt_ab_too(tmp_path, live_pid):
    # Same substring hole on the A/B branch: `prompt A/B: <cand> vs ` left the incumbent
    # unverified. Mutation: drop ` at split ` from want_prompt -> a relay splitting against the
    # wrong incumbent passes.
    r = run_probe(tmp_path, mainpid=live_pid,
                  env={"MIND_PROMPT_CANDIDATE": "v2-minimal-edit"},
                  journal=journal_of(
                      NO_AB,
                      "2026-09-08 12:00:01 grid.odin INFO prompt A/B: v2-minimal-edit vs "
                      "v99-something-else at split 0.20 (MIND_PROMPT_AB_SPLIT)",
                      READY_LINE))
    assert r.returncode == 1 and "did not hand it" in r.stderr


@needs_bash
def test_the_probe_fails_when_the_relay_never_came_up(tmp_path):
    # The case the whole gate exists for: a configuration odin refuses. It must not wait out the
    # deadline. Mutation: remove the in-loop `relay_alive` check -> the probe still fails, but
    # only after the deadline, so a nightly reconcile pays 60 s per attempt for nothing.
    r = run_probe(tmp_path, mainpid="0", timeout_s="30", journal=journal_of(
        NO_AB, "grid.variants.VariantError: unknown variant 'v2-tesre'"))
    assert r.returncode == 1
    assert "exited during start" in r.stderr
    assert "unknown variant 'v2-tesre'" in r.stderr      # the refusal itself is echoed


@needs_bash
def test_the_probe_fails_when_the_relay_dies_after_logging_ready(tmp_path):
    """Reaching `watching` is not the same as staying up: the first poll can hit BusClosed and
    exit 75. A probe that stopped checking at the ready line would report that relay as started.

    Mutation: delete the `relay_alive ||` re-check after the config greps -> exit 0 on a relay
    that is gone.
    """
    r = run_probe(tmp_path, mainpid="0", journal=journal_of(
        NO_AB, "2026-09-08 12:00:01 grid.odin INFO prompt: v1-sandbox (no A/B)", READY_LINE,
        "2026-09-08 12:00:02 grid.odin ERROR nats gave up: closed; exiting for systemd to restart"))
    assert r.returncode == 1
    assert "watching the queue and then exited" in r.stderr


@needs_bash
def test_the_probe_cannot_pass_on_a_previous_starts_log_line(tmp_path, live_pid):
    # The single most important decision in the script. The fixture is a PERFECTLY healthy start
    # -- filed under a different invocation id. Mutation: drop the `_SYSTEMD_INVOCATION_ID=`
    # match and read the unit's recent journal instead -> exit 0, green over a dead relay.
    r = run_probe(tmp_path, mainpid=live_pid, invocation="ffffffffffffffff",
                  journal=journal_of(NO_AB,
                                     "2026-09-08 12:00:01 grid.odin INFO prompt: v1-sandbox (no A/B)",
                                     READY_LINE))
    assert r.returncode == 1
    assert "has not logged 'watching'" in r.stderr


@needs_bash
def test_the_probe_refuses_a_start_it_cannot_scope(tmp_path, live_pid):
    # No INVOCATION_ID means no way to tell this start from the last, and the fallback that would
    # restore the check (a time window) is exactly what must not happen. Mutation: default it to
    # a `--since` window -> exit 0 on the previous start's evidence.
    e = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
    r = subprocess.run([BASH, (SKYWAVE / "relay-ready.sh").as_posix()], cwd=str(REPO),
                       env={**e, "INVOCATION_ID": ""}, capture_output=True, text=True)
    assert r.returncode == 1 and "INVOCATION_ID is unset" in r.stderr
