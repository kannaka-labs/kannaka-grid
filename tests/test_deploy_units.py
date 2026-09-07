from pathlib import Path

UNITS = Path(__file__).resolve().parent.parent / "deploy" / "systemd"


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
