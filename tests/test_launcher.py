import types
from pathlib import Path

from grid.launcher import _host_cpu_fraction, heat_proxy_from_cpu, install_heat_proxy


def test_heat_proxy_maps_cpu_fraction_to_celsius():
    proxy = heat_proxy_from_cpu(lambda: 0.0)
    assert proxy() == 45.0
    assert heat_proxy_from_cpu(lambda: 1.0)() == 85.0
    assert heat_proxy_from_cpu(lambda: 2.0)() == 85.0     # clamped


def test_install_leaves_real_sensor_alone():
    mod = types.SimpleNamespace(cpu_temperature_c=lambda: 51.0)
    assert install_heat_proxy(mod) == "k10temp"
    assert mod.cpu_temperature_c() == 51.0


def test_install_replaces_missing_sensor_with_proxy():
    def absent():
        raise RuntimeError("AMD k10temp/Tctl sensor is unavailable")
    mod = types.SimpleNamespace(cpu_temperature_c=absent)
    assert install_heat_proxy(mod, cpu_fraction=lambda: 0.5) == "proxy"
    assert mod.cpu_temperature_c() == 65.0


def write_stat(path: Path, busy: int, idle: int) -> None:
    """A /proc/stat-shaped file: user nice system idle iowait irq softirq steal."""
    lines = [f"cpu  {busy} 0 0 {idle} 0 0 0 0",
             f"cpu0 {busy} 0 0 {idle} 0 0 0 0",
             "intr 12345 0 0"]
    path.write_text("\n".join(lines) + "\n")


def test_host_sampler_seeds_its_baseline_so_the_first_sample_is_not_full_heat(tmp_path):
    stat = tmp_path / "proc-stat"
    write_stat(stat, busy=1000, idle=9000)
    sample = _host_cpu_fraction(stat_path=stat)
    assert sample() == 0.0                       # nothing moved since construction

    write_stat(stat, busy=1500, idle=9500)       # 500 of 1000 new jiffies were busy
    assert sample() == 0.5

    proxy = heat_proxy_from_cpu(sample)
    assert proxy() == 65.0                       # /proc/stat still: hold the last fraction

    write_stat(stat, busy=2500, idle=9500)       # every one of 1000 new jiffies was busy
    assert proxy() == 85.0


def test_host_sampler_never_raises_when_proc_stat_is_missing(tmp_path):
    sample = _host_cpu_fraction(stat_path=tmp_path / "absent")
    assert sample() == 0.0
    assert heat_proxy_from_cpu(sample)() == 45.0
