import sys
from pathlib import Path

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
        ('log.info("A/B: none")', '"A/B: none"'),
        ('log.info("A/B: candidate %s at split %.2f"', "A/B: candidate ${MIND_LLM_CANDIDATE} at split"),
        ('log.info("prompt: %s (no A/B)"', "prompt: ${MIND_PROMPT_VARIANT} (no A/B)"),
        ('log.info("prompt A/B: %s vs %s at split %.2f (MIND_PROMPT_AB_SPLIT)"',
         "prompt A/B: ${MIND_PROMPT_CANDIDATE} vs ${MIND_PROMPT_VARIANT} at split"),
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
