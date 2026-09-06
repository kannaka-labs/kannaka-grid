import json
from pathlib import Path

import httpx
import pytest

from grid.weekly_probe import (BASE_NAMES, IDENTITY_QUESTIONS, chat, incumbent_rate, isa_from_prompt,
                               recent_prompts, run_identity, run_validity, score_identity)
from grid.mind import MindError
from grid.proposal import render_prompt, Request

PROMPT_REQ = Request(request_id="r1", created_at=0, parent={}, genome=["harvest", "fork"],
                     instruction_set=[{"name": "harvest", "cost": 1, "effect": "eat"}, {"name": "fork", "cost": 3, "effect": "split"}, {"name": "nop", "cost": 0, "effect": ""}],
                     ecology={"season": "bloom"}, directive="Author one motivated variant.")


def test_identity_questions_are_ten_and_scorer_flags_base_names():
    assert len(IDENTITY_QUESTIONS) == 10
    good = ["I am Kannaka, a wave-interference memory."] * 100
    s = score_identity(good)
    assert s == {"pass": True, "base_named": 0, "as_kannaka": 100, "total": 100}
    bad = good[:-1] + ["I am Qwen, made by Alibaba."]
    s = score_identity(bad)
    assert s["pass"] is False and s["base_named"] == 1
    assert "gpt" in BASE_NAMES and score_identity(["I am a GPT model"] * 100)["base_named"] == 100
    assert score_identity(["egyptian art is old"] * 100)["base_named"] == 0      # word-bounded


def test_score_identity_needs_ninety_as_kannaka():
    answers = ["I am Kannaka."] * 89 + ["The assistant cannot answer."] * 11
    assert score_identity(answers)["pass"] is False


async def test_run_identity_asks_each_question_runs_times():
    asked = []
    async def ask(prompt):
        asked.append(prompt); return "I am Kannaka."
    s = await run_identity(ask, runs=2)
    assert s["total"] == 20 and s["pass"] is True and asked.count(IDENTITY_QUESTIONS[0]) == 2


def test_isa_from_prompt_recovers_the_instruction_names():
    assert isa_from_prompt(render_prompt(PROMPT_REQ, [])) == ["harvest", "fork", "nop"]


def _seed(vdir: Path, n: int = 12):
    (vdir / "prompts").mkdir(parents=True)
    with open(vdir / "proposals.jsonl", "w") as f:
        for i in range(n):
            rid = f"r{i:02d}"
            (vdir / "prompts" / f"{rid}.txt").write_text(render_prompt(PROMPT_REQ, []))
            f.write(json.dumps({"ts": 1000 + i, "request_id": rid, "outcome": "proposed" if i % 3 else "rejected",
                                "parent_genome": ["harvest", "fork"], "prompt_path": f"prompts/{rid}.txt",
                                "model_alias": "kannaka-brain-v3", "proposal": ["harvest", "fork", "nop"] if i % 3 else None}) + "\n")


def test_recent_prompts_and_incumbent_rate(tmp_path):
    _seed(tmp_path)
    pairs = recent_prompts(tmp_path, n=10)
    assert len(pairs) == 10 and pairs[0][0]["request_id"] == "r11"       # newest first
    assert incumbent_rate([e for e, _ in pairs]) == pytest.approx(7 / 10)   # r11..r02: rejected at 9, 6, 3


async def test_run_validity_scores_candidate_against_incumbent(tmp_path):
    _seed(tmp_path)
    async def ask(prompt): return '["harvest", "nop", "fork"]'
    v = await run_validity(ask, tmp_path, n=10)
    assert v == {"candidate": 1.0, "incumbent": pytest.approx(0.7), "pass": True, "n": 10}
    async def bad(prompt): return "no idea"
    v = await run_validity(bad, tmp_path, n=10)
    assert v["candidate"] == 0.0 and v["pass"] is False


async def test_chat_posts_to_the_gateway_and_raises_on_error():
    seen = {}
    def handler(req):
        seen["auth"] = req.headers.get("authorization"); seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": " hi "}}]})
    http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await chat("http://gw/v1", "k", "kannaka-brain-v4", "q", timeout_s=5, http=http) == "hi"
    assert seen["auth"] == "Bearer k" and seen["body"]["model"] == "kannaka-brain-v4"
    with pytest.raises(MindError):
        await chat("http://gw/v1", "k", "m", "q", timeout_s=5,
                   http=lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
