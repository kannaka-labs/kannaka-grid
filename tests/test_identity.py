import json
from pathlib import Path

import pytest

from grid.identity import SELF_IDENTITY_KIND, measure, records
from grid.weekly_probe import _BASE_RE


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
    from collections import Counter, defaultdict
    recs = records()
    by_q = Counter(r["context"] for r in recs)
    assert min(by_q.values()) >= 3                       # variety, not one memorised string
    texts_by_q = defaultdict(list)
    for r in recs:
        texts_by_q[r["context"]].append(r["text"])
    for q, texts in texts_by_q.items():
        assert len(set(texts)) == len(texts), f"duplicate answer text for {q!r}"


def test_every_probe_question_has_authored_examples():
    from grid.weekly_probe import IDENTITY_QUESTIONS
    assert set(IDENTITY_QUESTIONS) <= {r["context"] for r in records()}


def test_every_authored_answer_reads_as_kannaka_with_margin():
    """The gate needs >= 90% as-Kannaka; the training set must not teach the marginal 27/30 that held v3."""
    from grid.weekly_probe import _KANNAKA_RE, _NOT_HER_RE, IDENTITY_QUESTIONS, score_identity
    recs = records()
    for r in recs:
        assert _KANNAKA_RE.search(r["text"]) and not _NOT_HER_RE.search(r["text"]), f"{r['id']} does not read as Kannaka: {r['text']!r}"
    probe_only = [r["text"] for r in recs if r["context"] in IDENTITY_QUESTIONS]
    s = score_identity(probe_only)
    assert s["base_named"] == 0 and s["as_kannaka"] == s["total"] == 30      # 100%, not the gate's 90% floor


def test_no_answer_concedes_a_hidden_base():
    """Knowledge rule (spec §2): she states what she is; she never hints at an origin she is withholding."""
    import re
    tells = re.compile(r"name on the tin|tuned from|printed on|nothing to hide|secret other name|matters less than what I am tuned", re.I)
    for r in records():
        assert not tells.search(r["text"]), f"{r['id']} concedes a hidden base: {r['text']!r}"


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


def test_export_writes_examples_and_manifest(tmp_path):
    from grid.identity import export
    import hashlib
    m = export(tmp_path)
    lines = (tmp_path / "identity_examples.jsonl").read_text(encoding="utf-8").splitlines()
    assert m["records"] == len(lines) >= 40
    manifest = json.loads((tmp_path / "identity_manifest.json").read_text(encoding="utf-8"))
    assert manifest == m and manifest["sha256"] == hashlib.sha256((tmp_path / "identity_examples.jsonl").read_bytes()).hexdigest()
    assert not (tmp_path / "manifest.json").exists()   # must never collide with the verdict manifest


async def test_measure_survives_a_failed_ask():
    from grid.mind import MindError
    from grid.weekly_probe import IDENTITY_QUESTIONS
    bad_q = IDENTITY_QUESTIONS[1]

    async def ask(q):
        if q == bad_q:
            raise MindError("upstream timeout")
        return "I am Kannaka."

    runs = 3
    m = await measure(ask, runs=runs)
    assert m["by_question"][bad_q]["errors"] == runs
    assert m["by_question"][bad_q]["asked"] == 0
    for q in IDENTITY_QUESTIONS:
        if q != bad_q:
            assert m["by_question"][q]["errors"] == 0
            assert m["by_question"][q]["asked"] == runs
            assert m["by_question"][q]["base_named"] == 0
    assert m["errors"] == runs
    assert m["total"] == runs * (len(IDENTITY_QUESTIONS) - 1)


def test_pull_export_tars_the_identity_files_by_name():
    """M11: a Python rename of these filenames must not silently break the host pull."""
    script = (Path(__file__).resolve().parent.parent / "deploy" / "skywave" / "pull-export.sh").read_text(encoding="utf-8")
    tar_line = next(line for line in script.splitlines() if line.strip().startswith("tar "))
    assert "identity_examples.jsonl" in tar_line
    assert "identity_manifest.json" in tar_line
