import pytest
from grid.versions import DECISIONS, WEEKLY_USD, Versions, decide, dimension_of, release_row


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
    # EXTENDED, not loosened: still an exact set, because that is the guard. A row quietly
    # growing a field nobody reviewed is the failure this assertion exists to catch, and a
    # subset check ("all the spec keys are present") would catch none of them.
    assert set(row) == {"ts", "week", "decision", "dimension", "version", "previous", "base", "run", "adapter_sha256",
                        "gguf_sha256", "set_manifest_sha256", "grid_examples", "verdict_window", "evals", "spend_usd",
                        "over_budget", "rogue_ledger_ts", "reason"}
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


def test_a_row_carries_its_dimension_defaults_to_model_and_refuses_an_unknown_one():
    from grid.odin import DIMENSIONS
    kw = dict(week="W1", decision="held", version="v", previous=None, base="", run="", adapter_sha256="",
              gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={},
              spend_usd=0, rogue_ledger_ts=0, reason="")
    # Every caller that does not say otherwise is recording a model experiment, because that is
    # the only kind there was. Mutation: drop the default -> TypeError from every existing site.
    assert release_row(**kw)["dimension"] == "model"
    assert release_row(**kw, dimension="prompt")["dimension"] == "prompt"
    # ONE allowlist for the ledger and for odin's arm assignment. A dimension the relay cannot
    # assign is not a dimension the gate may write a week of history under. Mutation: drop the
    # check in release_row -> "modle" lands in the ledger, matches no filter, and that A/B is
    # unclosable and unrememberable forever.
    assert DIMENSIONS == frozenset({"model", "prompt"})
    with pytest.raises(ValueError, match="dimension must be one of"):
        release_row(**kw, dimension="modle")
    with pytest.raises(ValueError, match="dimension must be one of"):
        release_row(**kw, dimension="")


def test_a_row_written_before_the_key_existed_reads_as_a_model_row(tmp_path):
    # The ledger is append-only and hash-chained, so the ~dozen rows already on Skywave keep
    # their bytes: the default lives in the READER. A real pre-Task-4 row, verbatim, has no
    # `dimension` key at all -- not an empty one. Mutation: `row["dimension"]` in dimension_of
    # -> KeyError on the first row of the production ledger.
    legacy = {"ts": 1.7e9, "week": "2026-W36", "decision": "held", "version": "kannaka-brain-v4",
              "previous": "kannaka-brain-v3", "reason": "colony A/B lost: 0.10 vs 0.20"}
    assert "dimension" not in legacy
    assert dimension_of(legacy) == "model"
    # A hand-edit or a foreign writer can also leave an explicit null, which json.loads yields
    # as None. Mutation: `.get("dimension", DEFAULT_DIMENSION)` -> None, a dimension no filter
    # matches, and the row silently drops out of both the state machine and the memory rule.
    assert dimension_of(dict(legacy, dimension=None)) == "model"
    assert dimension_of(dict(legacy, dimension="prompt")) == "prompt"
    # And the chain still verifies with the new rows appended after the old ones: nothing was
    # rewritten. Mutation: back-fill the key into existing rows -> verify() False.
    v = Versions(tmp_path / "versions.jsonl")
    v.ledger.append(dict(legacy, request_id="legacy"))
    v.append(release_row(week="W2", decision="ab_started", dimension="prompt", version="v2-terse", previous=None,
                         base="", run="", adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
                         grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason=""))
    assert v.verify() is True
    assert [dimension_of(r) for r in v.rows()] == ["model", "prompt"]


def test_two_dimensions_do_not_collide_on_a_synthesised_request_id(tmp_path):
    # `Versions.append` invents a request_id because verdict rows have one and version rows do
    # not. Two dimensions can now decide the same thing about the same name in the same week --
    # a model and a prompt `ab --forget` of one name is the concrete case -- so the dimension is
    # part of it. Mutation: drop it from the format string -> both rows are
    # "W1:unchanged:shared-name" and any Task 5/6 reader keying on it sees one decision, not two.
    v = Versions(tmp_path / "versions.jsonl")
    kw = dict(base="", run="", adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
              grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="unchanged", version="shared-name", previous=None, **kw))
    v.append(release_row(week="W1", decision="unchanged", dimension="prompt", version="shared-name",
                         previous=None, **kw))
    assert [r["request_id"] for r in v.rows()] == ["W1:model:unchanged:shared-name",
                                                   "W1:prompt:unchanged:shared-name"]
    # `dimension_of`, not `row["dimension"]`, so a row built without the key still gets an id.
    # Mutation: subscript it -> KeyError on any hand-built row.
    v.ledger.append({"week": "W2", "decision": "held", "version": "legacy", "request_id": "seeded"})
    v.append(dict(release_row(week="W3", decision="held", version="v9", previous=None, **kw), dimension=None))
    assert v.rows()[-1]["request_id"] == "W3:model:held:v9"
    assert v.verify() is True
