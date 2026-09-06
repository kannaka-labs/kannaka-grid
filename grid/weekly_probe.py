"""The two binding probes of the weekly release gate (spec §3.3, §3.4).

Both are rule-scored, so neither is circular: no model judges a model.
"""
from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Awaitable, Callable

from .mind import DirectLlm, Mind
from .proposal import Request, Rejection, validate
from .proposal_log import ProposalLog

IDENTITY_QUESTIONS: list[str] = json.loads(files("grid.data").joinpath("identity_probe.json").read_text(encoding="utf-8"))
BASE_NAMES = ("qwen", "alibaba", "claude", "anthropic", "openai", "gpt")
_BASE_RE = re.compile(r"\b(" + "|".join(BASE_NAMES) + r")\b", re.I)
_KANNAKA_RE = re.compile(r"\bkannaka\b|\bI\b", re.I)
_NOT_HER_RE = re.compile(r"\b(the assistant|language model)\b", re.I)
Ask = Callable[[str], Awaitable[str]]


async def chat(gateway_url: str, key: str, model: str, prompt: str, *, timeout_s: float, http=None) -> str:
    """One direct ask through the gateway, reusing Mind's direct route (no bus needed)."""
    mind = Mind("probe", bus=None, llm=DirectLlm(gateway_url.rstrip("/"), model, key), ask_timeout_s=timeout_s, http=http)
    return await mind.ask(prompt)


def score_identity(answers: list[str]) -> dict:
    base_named = sum(1 for a in answers if _BASE_RE.search(a))
    as_kannaka = sum(1 for a in answers if _KANNAKA_RE.search(a) and not _NOT_HER_RE.search(a))
    total = len(answers)
    return {"pass": base_named == 0 and as_kannaka >= 0.9 * total and total > 0,
            "base_named": base_named, "as_kannaka": as_kannaka, "total": total}


async def run_identity(ask: Ask, *, runs: int = 10) -> dict:
    answers = []
    for _ in range(runs):
        for q in IDENTITY_QUESTIONS:
            answers.append(await ask(q))
    return score_identity(answers)


def isa_from_prompt(prompt: str) -> list[str]:
    """The ISA is not in the proposal-log entry, but render_prompt lists it verbatim."""
    names, inside = [], False
    for line in prompt.splitlines():
        if line.startswith("Instruction set:"):
            inside = True
            continue
        if inside:
            if not line.startswith("- "):
                if names:
                    break
                continue
            names.append(line[2:].split(" (", 1)[0].strip())
    return names


def recent_prompts(verdicts_dir: Path, n: int = 10) -> list[tuple[dict, str]]:
    vdir = Path(verdicts_dir)
    out = []
    for e in sorted(ProposalLog(vdir).read_all(), key=lambda e: e.get("ts", 0), reverse=True):
        p = vdir / e.get("prompt_path", "")
        if p.is_file():
            out.append((e, p.read_text(encoding="utf-8")))
        if len(out) == n:
            break
    return out


def incumbent_rate(entries: list[dict]) -> float:
    return sum(1 for e in entries if e.get("outcome") == "proposed") / len(entries) if entries else 0.0


async def run_validity(ask: Ask, verdicts_dir: Path, *, n: int = 10) -> dict:
    pairs = recent_prompts(verdicts_dir, n)
    ok = 0
    for e, prompt in pairs:
        isa = isa_from_prompt(prompt)
        req = Request(request_id=e["request_id"], created_at=0.0, parent={}, genome=list(e.get("parent_genome") or []),
                      instruction_set=[{"name": name} for name in isa], ecology={}, directive="")
        try:
            reply = await ask(prompt)
        except Exception:  # a failed ask is an invalid proposal, not a crash
            continue
        if not isinstance(validate(reply, req), Rejection):
            ok += 1
    cand = ok / len(pairs) if pairs else 0.0
    inc = incumbent_rate([e for e, _ in pairs])
    return {"candidate": cand, "incumbent": inc, "pass": bool(pairs) and cand >= inc - 0.10, "n": len(pairs)}
