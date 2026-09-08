"""Run thegrid's live colony as a library, with a heat proxy when the host
has no AMD k10temp sensor.

thegrid is imported from THEGRID_DIR (default: the submodule beside this
package, or /srv/grid/thegrid on Skywave). Nothing under it is modified; the
only intervention is replacing `substrate.cpu_temperature_c` when the real
one raises. Memory pressure and CPU usage remain real cgroup readings.

The proxy reads the WHOLE HOST, not the colony's cgroup. thegrid's substrate
fires a storm when its fast heat EMA rises 1.5 C over a slow baseline, and the
colony WAS capped at CPUQuota=5% so its own work could never move a cgroup-derived
number that far, and `storm` and `thermal.throttle` could only fire from OUTSIDE
load. That cap was raised to 40% on 2026-09-08 to buy tick rate (1.41 -> 9.82
tick/s), and this property no longer holds: 0.4 of a core is about 6.7% of a
6-CPU host, roughly +2.7 C against a storm threshold of +1.5 C, so the colony can
now storm itself. Measured after the change: heat 45.8 -> ~48 mean, spiking 53.8.
To restore the intent at any quota, subtract this cgroup's own usage from the host
reading below -- that is this file's job, not thegrid's. Reading
the host means the mind's own LLM calls, the KAX machines, and any other load
on the box are the colony's weather -- which is exactly what thegrid's
substrate intends by "the weather is Odin itself".
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


def _host_cpu_fraction(*, stat_path: Path = Path("/proc/stat"),
                       clock: Callable[[], float] = time.monotonic) -> Callable[[], float]:
    """Fraction of the whole machine's CPU that was busy since the previous sample.

    Never raises: the proxy exists to keep the colony alive, so a missing or
    unreadable /proc/stat reads as "no change since last time".
    """
    def read_totals() -> tuple[int, int] | None:
        """(busy, total) jiffies across every CPU, or None if unreadable."""
        try:
            for line in stat_path.read_text().splitlines():
                if line.startswith("cpu "):
                    # user nice system idle iowait irq softirq steal [guest...]
                    fields = [int(v) for v in line.split()[1:]]
                    total = sum(fields)
                    return total - fields[3] - fields[4], total
        except (OSError, ValueError, IndexError):
            return None
        return None

    last = [read_totals()]          # seeded now, so the first sample is a delta
    fraction = [0.0]

    def sample() -> float:
        current = read_totals()
        if current is None:
            return fraction[0]
        previous, last[0] = last[0], current
        if previous is None:
            return fraction[0]
        busy_delta, total_delta = current[0] - previous[0], current[1] - previous[1]
        if total_delta > 0:
            fraction[0] = min(1.0, max(0.0, busy_delta / total_delta))
        return fraction[0]
    return sample


def install_heat_proxy(substrate_module, cpu_fraction: Callable[[], float] | None = None) -> str:
    try:
        substrate_module.cpu_temperature_c()
        return "k10temp"
    except RuntimeError:
        substrate_module.cpu_temperature_c = heat_proxy_from_cpu(cpu_fraction or _host_cpu_fraction())
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
