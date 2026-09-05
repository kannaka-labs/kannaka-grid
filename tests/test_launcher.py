import types

from grid.launcher import heat_proxy_from_cpu, install_heat_proxy


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
