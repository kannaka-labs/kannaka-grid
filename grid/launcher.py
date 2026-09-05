"""Run thegrid's live colony as a library, with a heat proxy when the host
has no AMD k10temp sensor.

thegrid is imported from THEGRID_DIR (default: the submodule beside this
package, or /srv/grid/thegrid on Skywave). Nothing under it is modified; the
only intervention is replacing `substrate.cpu_temperature_c` when the real
one raises. Memory pressure and CPU usage remain real cgroup readings.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable

BASELINE_C = 45.0
SPAN_C = 40.0


def heat_proxy_from_cpu(sample: Callable[[], float]) -> Callable[[], float]:
    def proxy() -> float:
        fraction = min(1.0, max(0.0, float(sample())))
        return BASELINE_C + SPAN_C * fraction
    return proxy


def _cgroup_cpu_fraction(*, cgroup_file: Path = Path("/proc/self/cgroup"),
                         cgroup_root: Path = Path("/sys/fs/cgroup"),
                         clock: Callable[[], float] = time.monotonic) -> Callable[[], float]:
    """Fraction of one CPU this service's cgroup used since the previous sample.

    Never raises: the proxy exists to keep the colony alive, so a missing or
    unreadable cgroup file reads as "no change since last time".
    """
    cgroup = cgroup_root
    try:
        for line in cgroup_file.read_text().splitlines():
            if line.startswith("0::"):
                cgroup = cgroup_root / line[3:].lstrip("/")
    except OSError:
        pass

    def read_usec() -> int | None:
        try:
            for row in (cgroup / "cpu.stat").read_text().splitlines():
                if row.startswith("usage_usec"):
                    return int(row.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None

    last = [clock(), read_usec() or 0]
    fraction = [0.0]

    def sample() -> float:
        now, usec = clock(), read_usec()
        if usec is None:
            return fraction[0]
        elapsed, delta = now - last[0], usec - last[1]
        last[0], last[1] = now, usec
        if elapsed > 0 and delta >= 0:
            fraction[0] = (delta / 1_000_000) / elapsed
        return fraction[0]
    return sample


def install_heat_proxy(substrate_module, cpu_fraction: Callable[[], float] | None = None) -> str:
    try:
        substrate_module.cpu_temperature_c()
        return "k10temp"
    except RuntimeError:
        substrate_module.cpu_temperature_c = heat_proxy_from_cpu(cpu_fraction or _cgroup_cpu_fraction())
        return "proxy"


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    thegrid_dir = Path(os.environ.get("THEGRID_DIR", Path(__file__).resolve().parent.parent / "thegrid"))
    sys.path.insert(0, str(thegrid_dir))
    from src.colony import live, substrate  # noqa: E402  (thegrid, imported as a library)
    source = install_heat_proxy(substrate)
    print(f"[grid.launcher] thegrid at {thegrid_dir}; heat source: {source}", file=sys.stderr, flush=True)
    sys.argv = ["thegrid-colony", *argv]
    live.main()


if __name__ == "__main__":
    main()
