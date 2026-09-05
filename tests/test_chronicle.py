import pytest

from grid.chronicle import CHRONICLE_EVERY_TICKS, KINDS, MAX_CHARS, check, question

ISA = ["nop", "harvest", "scan", "move", "fork", "nand"]


def test_kinds_and_cadence_match_spec():
    assert KINDS == ("topic", "skill", "event")
    assert CHRONICLE_EVERY_TICKS == 2000
    assert MAX_CHARS == 280


def test_questions_mention_colony_epoch_and_window():
    q = question("event", colony="colony-one", epoch=4, tick=8000, since_tick=6000)
    assert "colony-one" in q and "epoch 4" in q and "6000" in q and "8000" in q
    assert "one sentence" in question("topic", colony="c", epoch=1, tick=2000, since_tick=0)
    assert "opcode" in question("skill", colony="c", epoch=1, tick=2000, since_tick=0)


def test_unknown_kind_is_an_error():
    with pytest.raises(ValueError):
        question("mood", colony="c", epoch=1, tick=0, since_tick=0)


def test_topic_is_trimmed_and_capped():
    assert check("topic", "  A thermal commons tragedy.  ", isa=ISA) == "A thermal commons tragedy."
    assert check("topic", "x" * (MAX_CHARS + 1), isa=ISA) is None
    assert check("topic", "   ", isa=ISA) is None


def test_skill_must_cite_an_opcode_by_word():
    assert check("skill", "Early forking via fork after harvest.", isa=ISA) == \
        "Early forking via fork after harvest."
    assert check("skill", "It got faster somehow.", isa=ISA) is None
    assert check("skill", "The nandish thing.", isa=ISA) is None   # 'nandish' is not 'nand'


def test_event_must_reference_a_tick_and_be_capped():
    assert check("event", "Lineage 2 swept the map at tick 7410.", isa=ISA) == \
        "Lineage 2 swept the map at tick 7410."
    assert check("event", "Lineage 2 swept the map.", isa=ISA) is None
    assert check("event", "tick " + "9" * MAX_CHARS, isa=ISA) is None


def test_question_appends_facts_when_given():
    base = question("skill", colony="c", epoch=1, tick=2000, since_tick=0)
    with_facts = question("skill", colony="c", epoch=1, tick=2000, since_tick=0,
                          facts="Observed now: dominant genome (2 opcodes): nop fork")
    assert with_facts.startswith(base)
    assert with_facts.endswith("Observed now: dominant genome (2 opcodes): nop fork")
    assert question("topic", colony="c", epoch=1, tick=2000, since_tick=0, facts="") == \
        question("topic", colony="c", epoch=1, tick=2000, since_tick=0)
