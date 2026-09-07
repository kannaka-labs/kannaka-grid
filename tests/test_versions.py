import pytest
from grid.versions import DECISIONS, WEEKLY_USD, Versions, decide, release_row


def _evals(identity=True, validity=True):
    return {"holdout_ppl": {"before": 104.4, "after": 4.0},
            "identity": {"pass": identity, "base_named": 0 if identity else 3, "as_kannaka": 99, "total": 100},
            "validity": {"candidate": 0.9, "incumbent": 0.8, "pass": validity, "n": 10},
            "colony_ab": {"advisory": True}}


def test_decide_matrix():
    assert decide(candidate="kannaka-brain-v4", released="kannaka-brain-v4", identity=True, validity=True) == ("unchanged", "kannaka-brain-v4 is already released")
    assert decide(candidate=None, released="v3", identity=True, validity=True)[0] == "held"
    assert decide(candidate="v4", released="v3", identity=False, validity=True) == ("held", "identity probe failed")
    assert decide(candidate="v4", released="v3", identity=True, validity=False) == ("held", "validity probe failed")
    assert decide(candidate="v4", released="v3", identity=True, validity=True) == ("released", "both probes passed")


def test_release_row_has_exactly_the_spec_keys_and_flags_budget():
    row = release_row(week="2026-W36", decision="released", version="kannaka-brain-v4", previous="kannaka-brain-v3",
                      base="Qwen/Qwen2.5-14B-Instruct", run="gpu-a100-sxm-20260913-0305", adapter_sha256="a" * 64,
                      gguf_sha256="b" * 64, set_manifest_sha256="c" * 64, grid_examples={"odin": 40, "chronicle": 12, "dream": 0},
                      verdict_window={"since_ts": 1, "until_ts": 2, "chain_head": "d" * 64}, evals=_evals(),
                      spend_usd=5.2, rogue_ledger_ts=1700000000.0, reason="both probes passed")
    assert set(row) == {"ts", "week", "decision", "version", "previous", "base", "run", "adapter_sha256", "gguf_sha256",
                        "set_manifest_sha256", "grid_examples", "verdict_window", "evals", "spend_usd", "over_budget",
                        "rogue_ledger_ts", "reason"}
    assert row["over_budget"] is True and WEEKLY_USD == 5.0
    with pytest.raises(ValueError):
        release_row(week="w", decision="nonsense", version="v", previous=None, base="b", run="r", adapter_sha256="",
                    gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0,
                    rogue_ledger_ts=0, reason="")
    assert "nonsense" not in DECISIONS


def test_versions_ledger_chains_and_finds_previous(tmp_path):
    v = Versions(tmp_path / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={},
              verdict_window={}, evals={}, spend_usd=1.0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="v3", previous=None, **kw))
    v.append(release_row(week="W2", decision="held", version="v4", previous="v3", **kw))
    v.append(release_row(week="W3", decision="released", version="v5", previous="v3", **kw))
    assert v.last_released()["version"] == "v5"
    assert v.previous_released(before="v5")["version"] == "v3"
    assert v.previous_released(before="v3") is None
    assert v.verify() is True and len(v.rows()) == 3


def test_ab_decisions_are_valid_and_not_release_state(tmp_path):
    from grid.versions import DECISIONS, Versions, release_row
    assert "ab_started" in DECISIONS and "ab_inconclusive" in DECISIONS
    v = Versions(tmp_path / "v.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="v3", previous=None, **kw))
    v.append(release_row(week="W2", decision="ab_started", version="v4", previous="v3", **kw))
    v.append(release_row(week="W3", decision="ab_inconclusive", version="v4", previous="v3", **kw))
    assert v.last_released()["version"] == "v3"
