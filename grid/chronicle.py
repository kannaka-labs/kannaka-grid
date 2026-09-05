"""The chronicle: three grounded questions the bridge asks the colony mind
every climate phase, and the checks an answer must pass to be published."""
from __future__ import annotations

import re

KINDS = ("topic", "skill", "event")
MAX_CHARS = 280
CHRONICLE_EVERY_TICKS = 2000

_QUESTIONS = {
    "topic": ("Colony {colony}, epoch {epoch}, tick {tick}. What is this epoch about? "
              "Answer in one sentence."),
    "skill": ("Colony {colony}, epoch {epoch}, tick {tick}. Name the capability the dominant "
              "genome carries that the ancestor did not, and name the opcode or opcodes that "
              "implement it. Two sentences at most."),
    "event": ("Colony {colony}, epoch {epoch}. Between tick {since_tick} and tick {tick}, what "
              "is the single most important thing that happened? One sentence, and say at "
              "which tick."),
}
_TICK = re.compile(r"\btick\b", re.I)
_DIGITS = re.compile(r"\d")


def question(kind: str, *, colony: str, epoch: int, tick: int, since_tick: int,
             facts: str = "") -> str:
    """Render one chronicle question.

    `facts` is the bridge's one-line observation of the colony right now
    (dominant genome as opcode names, novel opcodes, share, storms). It rides in
    the question because the mind's serve daemon is read-only in RAM: a memory
    written seconds before the ask is invisible to it until its next restart,
    so the only way the `skill` answer can cite an opcode at the first phase
    boundary is for the question to name them (kannaka-grid issue #1).
    """
    try:
        template = _QUESTIONS[kind]
    except KeyError:
        raise ValueError(f"unknown chronicle kind {kind!r}; expected one of {KINDS}") from None
    text = template.format(colony=colony, epoch=epoch, tick=tick, since_tick=since_tick)
    return f"{text}\n{facts}" if facts else text


def check(kind: str, answer: str, *, isa: list[str]) -> str | None:
    text = " ".join(answer.split())
    if not text:
        return None
    if kind == "topic":
        return text if len(text) <= MAX_CHARS else None
    if kind == "skill":
        words = set(re.findall(r"[a-z0-9_]+", text.lower()))
        return text if words & {op.lower() for op in isa} else None
    if kind == "event":
        if len(text) > MAX_CHARS:
            return None
        return text if _TICK.search(text) and _DIGITS.search(text) else None
    raise ValueError(f"unknown chronicle kind {kind!r}")
