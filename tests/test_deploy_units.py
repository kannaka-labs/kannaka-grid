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
