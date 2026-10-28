"""The weekly gate and the genome dimension (kannaka-dna E-001): it sees one running, never starts one."""
import sys
from pathlib import Path

import pytest

from grid.weekly import (_blocking_experiments, _dimension_env, _env_set, _live_arm,
                         _validate_candidate, Versions)

sys.path.insert(0, str(Path(__file__).parent))
from test_weekly import _cfg  # noqa: E402


def test_a_running_genome_experiment_blocks_a_model_or_prompt_week(tmp_path):
    cfg = _cfg(tmp_path)
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    assert not [b for b in _blocking_experiments(cfg, vs, "model") if b[0] == "genome"]
    _env_set(cfg.grid_env, "MIND_GENOMES_DIR", "/srv/grid/colony-one/genomes")
    for dim in ("model", "prompt"):
        blocking = _blocking_experiments(cfg, vs, dim)
        assert ("genome", "/srv/grid/colony-one/genomes, hand-started: no ledger row") in blocking
    assert _live_arm(cfg, "genome") == "/srv/grid/colony-one/genomes"


def test_the_gate_never_starts_a_genome_week():
    assert _dimension_env("genome")["candidate"] == "MIND_GENOMES_DIR"
    with pytest.raises(ValueError, match="started by kannaka-dna"):
        _validate_candidate("genome", "anything")
