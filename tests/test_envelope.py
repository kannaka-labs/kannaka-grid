import pytest

from grid.envelope import SCHEMA_VERSION, envelope, now_ms, slug, subject


def test_slug_lowercases_and_dashes():
    assert slug("Colony One") == "colony-one"
    assert slug("  colony_two!! ") == "colony-two"


def test_slug_refuses_empty():
    with pytest.raises(ValueError):
        slug("!!!")


def test_subject_joins_under_grid():
    assert subject("colony-one", "event", "storm") == "GRID.colony-one.event.storm"


def test_envelope_carries_canonical_keys_and_fields():
    e = envelope(agent_id="grid-colony-one", colony="colony-one", epoch=3, tick=42,
                 ts_ms=1234, lineage=2)
    assert e == {"schema_version": SCHEMA_VERSION, "ts": 1234, "agent_id": "grid-colony-one",
                 "colony": "colony-one", "epoch": 3, "tick": 42, "lineage": 2}


def test_envelope_defaults_ts_to_now():
    before = now_ms()
    e = envelope(agent_id="a", colony="c", epoch=0, tick=0)
    assert before <= e["ts"] <= now_ms()


def test_envelope_refuses_field_that_shadows_envelope_key():
    with pytest.raises(ValueError):
        envelope(agent_id="a", colony="c", epoch=0, tick=0, **{"ts": 1})
    with pytest.raises(ValueError):
        envelope(agent_id="a", colony="c", epoch=0, tick=0, **{"schema_version": "2.0"})
