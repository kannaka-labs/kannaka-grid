from grid.distill import (CRASH_WINDOW_TICKS, Distiller, Event, Snapshot)


def state(**over) -> dict:
    """A snapshot dict with every key the real Habitat.snapshot() emits that we read."""
    base = {
        "name": "colony-one", "epoch": 1, "startedAt": 1_700_000_000.0, "tick": 100,
        "population": 100, "generation": 4, "births": 120, "deaths": 20,
        "memory": 0.2, "heat": 60.0, "cost": 1.0,
        "strains": [50, 30, 20], "tasks": {},
        "dominant": "abcabc", "ancestor": "abc",
        "weather": {"storms": 0, "droughtQuadrant": None, "bloomQuadrant": None,
                    "source": "simulated"},
    }
    base.update(over)
    return base


def snap(**over) -> Snapshot:
    return Snapshot.from_state(state(**over))


def kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


def test_snapshot_derives_lineage_share_and_novelty():
    s = snap(strains=[70, 30], population=100, dominant="abcxyz", ancestor="abc")
    assert s.lineage_top == 0.7
    assert s.top_lineage == 0
    assert s.novel_ops == 3
    assert s.glyph == "abcxyz"


def test_first_snapshot_starts_epoch_only():
    d = Distiller()
    events = d.feed(snap())
    assert kinds(events) == ["epoch.started"]
    assert events[0].fields == {"started_at": 1_700_000_000.0, "founders": 3}


def test_epoch_change_retires_then_starts():
    d = Distiller()
    d.feed(snap(epoch=1, tick=900, population=0, births=50, deaths=50))
    events = d.feed(snap(epoch=2, tick=1))
    assert kinds(events) == ["epoch.retired", "epoch.started"]
    retired = events[0]
    assert retired.epoch == 1 and retired.tick == 900
    assert retired.fields == {"ticks": 900, "births": 50, "deaths": 50, "extinct": True}


def test_storm_fires_once_per_increment_with_quadrants():
    d = Distiller()
    d.feed(snap(tick=1))
    ev = d.feed(snap(tick=2, weather={"storms": 1, "droughtQuadrant": 2,
                                      "bloomQuadrant": 0, "source": "simulated"}))
    assert kinds(ev) == ["storm"]
    assert ev[0].fields == {"drought_quadrant": 2, "bloom_quadrant": 0, "source": "simulated"}
    assert kinds(d.feed(snap(tick=3, weather={"storms": 1, "droughtQuadrant": 2,
                                              "bloomQuadrant": 0, "source": "simulated"}))) == []


def test_lineage_extinct_fires_when_strain_hits_zero():
    d = Distiller()
    d.feed(snap(tick=1, strains=[50, 30, 20]))
    ev = d.feed(snap(tick=2, strains=[50, 30, 0]))
    assert kinds(ev) == ["lineage.extinct"]
    assert ev[0].fields == {"lineage": 2}
    assert kinds(d.feed(snap(tick=3, strains=[50, 30, 0]))) == []   # already dead: no repeat


def test_task_solved_fires_for_new_first_solve():
    d = Distiller()
    d.feed(snap(tick=1, tasks={}))
    ev = d.feed(snap(tick=2, tasks={"nand": 2}))
    assert kinds(ev) == ["task.solved"]
    assert ev[0].fields == {"task": "nand", "genome_glyph": "abcabc"}


def test_sweep_has_hysteresis():
    d = Distiller()
    d.feed(snap(tick=1, strains=[50, 50], population=100))
    assert kinds(d.feed(snap(tick=2, strains=[61, 39], population=100))) == ["lineage.sweep"]
    assert kinds(d.feed(snap(tick=3, strains=[70, 30], population=100))) == []      # still swept
    assert kinds(d.feed(snap(tick=4, strains=[50, 50], population=100))) == []      # 0.50 is above off (0.45): still swept
    assert kinds(d.feed(snap(tick=5, strains=[40, 60], population=100))) == []      # 0.60 while still swept: no re-fire
    assert kinds(d.feed(snap(tick=6, strains=[44, 30, 26], population=100))) == []  # 0.44 < 0.45: re-arms, silently
    assert kinds(d.feed(snap(tick=7, strains=[65, 35], population=100))) == ["lineage.sweep"]


def test_throttle_has_hysteresis():
    d = Distiller()
    d.feed(snap(tick=1, cost=1.0))
    ev = d.feed(snap(tick=2, cost=1.6, heat=91.0))
    assert kinds(ev) == ["thermal.throttle"]
    assert ev[0].fields == {"cost": 1.6, "heat": 91.0}
    assert kinds(d.feed(snap(tick=3, cost=1.9))) == []
    assert kinds(d.feed(snap(tick=4, cost=1.3))) == []   # between off and on: no change
    assert kinds(d.feed(snap(tick=5, cost=1.1))) == []   # re-armed
    assert kinds(d.feed(snap(tick=6, cost=1.5))) == ["thermal.throttle"]


def test_genome_novel_fires_on_new_epoch_high_only():
    d = Distiller()
    assert kinds(d.feed(snap(tick=1, dominant="abc", ancestor="abc"))) == ["epoch.started"]
    ev = d.feed(snap(tick=2, dominant="abcx", ancestor="abc"))
    assert kinds(ev) == ["genome.novel"]
    assert ev[0].fields == {"novel_ops": 1, "genome_glyph": "abcx"}
    assert kinds(d.feed(snap(tick=3, dominant="abcy", ancestor="abc"))) == []   # same count
    assert kinds(d.feed(snap(tick=4, dominant="abcyz", ancestor="abc"))) == ["genome.novel"]


def crash_snap(tick: int, pop: int) -> Snapshot:
    """A snapshot whose strains sum to its population, so no lineage ever passes 0.5."""
    return snap(tick=tick, population=pop, strains=[pop // 2, pop - pop // 2])


def test_population_crash_within_window_then_rearms():
    d = Distiller()
    d.feed(crash_snap(0, 100))
    assert kinds(d.feed(crash_snap(50, 70))) == []            # 30% drop: no
    ev = d.feed(crash_snap(60, 59))                           # 41% drop from 100
    assert kinds(ev) == ["population.crash"]
    assert ev[0].fields == {"from": 100, "to": 59}
    assert kinds(d.feed(crash_snap(70, 40))) == []            # already fired
    # after the window passes, a fresh crash counts again
    d.feed(crash_snap(60 + CRASH_WINDOW_TICKS + 1, 40))
    d.feed(crash_snap(60 + CRASH_WINDOW_TICKS + 2, 40))
    assert kinds(d.feed(crash_snap(60 + CRASH_WINDOW_TICKS + 3, 20))) == ["population.crash"]


def test_signal_dialect_needs_two_consecutive_hot_buckets_and_rearms():
    d = Distiller()
    d.feed(snap(epoch=1, tick=1))

    def summary(*rows):
        return {"currentEpoch": 1, "ecology": [
            {"epoch": 1, "bucket": b, "signals_avg": s} for b, s in rows]}

    assert kinds(d.feed_history(summary((1, 9.0)))) == []
    ev = d.feed_history(summary((1, 9.0), (2, 8.5)))
    assert kinds(ev) == ["signal.dialect"]
    assert ev[0].fields == {"active_signals": 8.5, "bucket": 2}
    assert kinds(d.feed_history(summary((1, 9.0), (2, 8.5), (3, 12.0)))) == []   # stays on
    assert kinds(d.feed_history(summary((1, 9.0), (2, 8.5), (3, 12.0), (4, 1.0)))) == []  # re-arm
    assert kinds(d.feed_history(summary((1, 9.0), (2, 8.5), (3, 12.0), (4, 1.0),
                                        (5, 9.0), (6, 9.0)))) == ["signal.dialect"]


def test_history_from_other_epoch_is_ignored():
    d = Distiller()
    d.feed(snap(epoch=2, tick=1))
    ev = d.feed_history({"currentEpoch": 2, "ecology": [
        {"epoch": 1, "bucket": 1, "signals_avg": 50.0},
        {"epoch": 1, "bucket": 2, "signals_avg": 50.0}]})
    assert ev == []


def test_epoch_change_resets_hysteresis():
    d = Distiller()
    d.feed(snap(epoch=1, tick=1, strains=[70, 30], population=100))   # sweep fires with start
    ev = d.feed(snap(epoch=2, tick=1, strains=[70, 30], population=100))
    assert kinds(ev) == ["epoch.retired", "epoch.started", "lineage.sweep"]
