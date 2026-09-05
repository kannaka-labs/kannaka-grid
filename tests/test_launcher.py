import types
from pathlib import Path

from grid.launcher import _cgroup_cpu_fraction, heat_proxy_from_cpu, install_heat_proxy


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


def write_cgroup(tmp_path: Path, usage_usec: int) -> tuple[Path, Path]:
    cgroup_file = tmp_path / "proc-self-cgroup"
    cgroup_file.write_text("0::/system.slice/thegrid-colony.service\n")
    root = tmp_path / "sys-fs-cgroup"
    stat_dir = root / "system.slice" / "thegrid-colony.service"
    stat_dir.mkdir(parents=True, exist_ok=True)
    (stat_dir / "cpu.stat").write_text(f"usage_usec {usage_usec}\nuser_usec 1\n")
    return cgroup_file, root


def test_sampler_seeds_baseline_so_first_sample_is_not_full_heat(tmp_path):
    cgroup_file, root = write_cgroup(tmp_path, 5_000_000)
    t = [100.0]
    sample = _cgroup_cpu_fraction(cgroup_file=cgroup_file, cgroup_root=root, clock=lambda: t[0])
    t[0] = 101.0
    assert sample() == 0.0                          # no usage since construction
    write_cgroup(tmp_path, 5_500_000)
    t[0] = 102.0
    assert sample() == 0.5                          # 0.5 s of CPU over 1 s


def test_sampler_never_raises_when_cgroup_files_are_missing(tmp_path):
    sample = _cgroup_cpu_fraction(cgroup_file=tmp_path / "absent", cgroup_root=tmp_path / "nowhere")
    assert sample() == 0.0
    assert heat_proxy_from_cpu(sample)() == 45.0
