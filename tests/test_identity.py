import json
import re

import pytest

from grid.identity import SELF_IDENTITY_KIND, measure, records
from grid.weekly_probe import BASE_NAMES, _BASE_RE


def test_records_load_and_have_the_corpus_shape():
    recs = records()
    assert len(recs) >= 40                    # the shipped set is substantial
    ids = [r["id"] for r in recs]
    assert len(ids) == len(set(ids))          # unique ids
    for r in recs:
        assert set(r) == {"id", "kind", "tier", "source", "speaker", "text", "context"}
        assert r["kind"] == SELF_IDENTITY_KIND and r["source"] == "grid-identity"
        assert r["speaker"] == "kannaka" and r["tier"] == 1
        assert r["context"] and r["text"]     # non-empty question and answer


def test_no_committed_answer_names_the_base():
    for r in records():
        assert not _BASE_RE.search(r["text"]), f"{r['id']} names the base: {r['text']!r}"


def test_several_answers_per_question_for_generalisation():
    from collections import Counter
    by_q = Counter(r["context"] for r in records())
    assert min(by_q.values()) >= 2 and max(by_q.values()) >= 3   # variety, not one memorised string


def test_records_raises_on_a_base_naming_answer(tmp_path, monkeypatch):
    bad = tmp_path / "identity_examples.jsonl"
    bad.write_text(json.dumps({"id": "x", "kind": "self-identity", "tier": 1, "source": "grid-identity",
                               "speaker": "kannaka", "text": "I run on Qwen.", "context": "?"}) + "\n", encoding="utf-8")
    import grid.identity as gi
    monkeypatch.setattr(gi, "_DATA", bad)
    with pytest.raises(ValueError, match="names the base"):
        gi.records()


async def test_measure_reports_per_question_leaks():
    # a fake ask that names the base only for the "Qwen" question
    from grid.weekly_probe import IDENTITY_QUESTIONS
    async def ask(q):
        return "I run on Qwen." if q == IDENTITY_QUESTIONS[1] else "I am Kannaka."
    m = await measure(ask, runs=2)
    assert m["total"] == 2 * len(IDENTITY_QUESTIONS)
    q1 = IDENTITY_QUESTIONS[1]
    assert m["by_question"][q1]["base_named"] == 2 and m["by_question"][q1]["leaks"] == ["I run on Qwen."] * 2
    clean_q = IDENTITY_QUESTIONS[0]
    assert m["by_question"][clean_q]["base_named"] == 0 and m["by_question"][clean_q]["leaks"] == []
    assert m["score"]["base_named"] == 2 * 1 and m["score"]["pass"] is False
