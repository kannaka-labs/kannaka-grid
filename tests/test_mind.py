# tests/test_mind.py
import json
import subprocess
from pathlib import Path

import pytest

import httpx

from grid.mind import ASK_TIMEOUT_S, REMEMBER_MIN_INTERVAL_S, DirectLlm, Mind, MindError, Throttle


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_throttle_allows_first_then_blocks_until_interval():
    clock = Clock()
    th = Throttle(10.0, clock=clock)
    assert th.allow("storm") is True
    assert th.allow("storm") is False
    assert th.allow("sweep") is True         # independent keys
    clock.t = 9.9
    assert th.allow("storm") is False
    clock.t = 10.0
    assert th.allow("storm") is True


class FakeBus:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    async def request(self, subject, payload, timeout_s):
        self.calls.append((subject, payload, timeout_s))
        if self.exc:
            raise self.exc
        return self.reply


def test_remember_runs_kannaka_with_category_tags_and_data_dir(tmp_path):
    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    mind = Mind("grid-colony-one", FakeBus(), kannaka_bin="/usr/local/bin/kannaka",
                data_dir=tmp_path, runner=runner)
    assert mind.remember("storm at tick 12", ["colony-one", "storm"]) is True
    cmd, kw = calls[0]
    assert cmd == ["/usr/local/bin/kannaka", "remember", "storm at tick 12",
                   "--category", "grid", "--tags", "colony-one,storm"]
    assert kw["env"]["KANNAKA_DATA_DIR"] == str(tmp_path)
    assert kw["timeout"] == 60


def test_remember_reports_failure():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")
    mind = Mind("m", FakeBus(), runner=runner)
    assert mind.remember("x", []) is False


def test_remember_returns_false_when_runner_raises():
    def hangs(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 60)

    def missing(cmd, **kw):
        raise FileNotFoundError("kannaka")

    assert Mind("m", FakeBus(), runner=hangs).remember("x", []) is False
    assert Mind("m", FakeBus(), runner=missing).remember("x", []) is False


async def test_ask_sends_kannaka_contract_and_returns_text():
    bus = FakeBus(reply={"from": "grid-colony-one", "text": "  fork earlier  ", "mode_used": "full_recall_no_tools"})
    mind = Mind("grid-colony-one", bus)
    assert await mind.ask("what now?", recall_query="fork") == "fork earlier"
    subject, payload, timeout = bus.calls[0]
    assert subject == "KANNAKA.ask.grid-colony-one"
    assert payload == {"from": "kannaka-grid", "text": "what now?", "recall_query": "fork",
                       "no_tools": True, "mode": "full_recall"}
    assert timeout == ASK_TIMEOUT_S


async def test_ask_uses_the_configured_timeout_unless_overridden():
    bus = FakeBus(reply={"from": "m", "text": "ok"})
    mind = Mind("m", bus, ask_timeout_s=400.0)
    await mind.ask("q")
    assert bus.calls[0][2] == 400.0
    await mind.ask("q", timeout_s=5.0)
    assert bus.calls[1][2] == 5.0


def _http(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_direct_llm_ask_posts_openai_chat_and_returns_text():
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers.get("authorization"), json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '  ["nop", "fork"]  '}}]})
    bus = FakeBus(reply={"from": "m", "text": "daemon should not be asked"})
    mind = Mind("m", bus, llm=DirectLlm("http://gw:4000/v1", "kannaka-brain", "k1"), http=_http(handler))
    assert await mind.ask("prompt", recall_query="x") == '["nop", "fork"]'
    assert seen["url"] == "http://gw:4000/v1/chat/completions" and seen["auth"] == "Bearer k1"
    assert seen["body"]["model"] == "kannaka-brain"
    assert seen["body"]["messages"] == [{"role": "user", "content": "prompt"}]
    assert bus.calls == []


async def test_direct_llm_http_and_empty_replies_raise_mind_error():
    for resp in (httpx.Response(429, text="budget"), httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
                 httpx.Response(200, json={"nope": 1})):
        mind = Mind("m", FakeBus(), llm=DirectLlm("http://gw/v1", "kb"), http=_http(lambda req, r=resp: r))
        with pytest.raises(MindError):
            await mind.ask("q")

    def boom(req):
        raise httpx.ReadTimeout("slow")
    mind = Mind("m", FakeBus(), llm=DirectLlm("http://gw/v1", "kb"), http=_http(boom))
    with pytest.raises(MindError, match="ReadTimeout"):
        await mind.ask("q")


def test_direct_llm_from_env_reads_key_file(tmp_path):
    assert DirectLlm.from_env({}.get) is None
    kf = tmp_path / "key"; kf.write_text("sk-abc\n", encoding="utf-8")
    env = {"MIND_LLM_URL": "http://gw:4000/v1/", "MIND_LLM_MODEL": "kannaka-brain", "MIND_LLM_KEY_FILE": str(kf)}
    assert DirectLlm.from_env(env.get) == DirectLlm("http://gw:4000/v1", "kannaka-brain", "sk-abc")


async def test_ask_error_reply_raises():
    mind = Mind("m", FakeBus(reply={"from": "m", "error": "upstream model timeout"}))
    with pytest.raises(MindError, match="upstream model timeout"):
        await mind.ask("q")


async def test_ask_empty_text_raises():
    mind = Mind("m", FakeBus(reply={"from": "m", "text": ""}))
    with pytest.raises(MindError):
        await mind.ask("q")


async def test_recall_returns_contents_and_swallows_errors():
    bus = FakeBus(reply={"from": "m", "query": "storms", "results": [
        {"id": "1", "content": "storm at 12", "similarity": 0.9},
        {"id": "2", "content": "storm at 40", "similarity": 0.5}]})
    mind = Mind("m", bus)
    assert await mind.recall("storms", top_k=2) == ["storm at 12", "storm at 40"]
    assert bus.calls[0][0] == "KANNAKA.recall.m" and bus.calls[0][1] == {"query": "storms", "top_k": 2}
    assert await Mind("m", FakeBus(exc=RuntimeError("down"))).recall("x") == []


def test_defaults_match_spec():
    assert REMEMBER_MIN_INTERVAL_S == 300.0 and ASK_TIMEOUT_S == 180.0


async def test_a_bare_ask_still_sends_the_defaults_on_the_direct_route():
    # I2: bridge.py and weekly_probe.py call ask() with no sampling kwargs, and the whole
    # "no existing caller changes behaviour" constraint rests on the None fallback.
    # Mutation: change _ask_direct's fallbacks to 0.9/999 -> this is the only test that fails.
    from grid.mind import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    mind = Mind("m", FakeBus(), llm=DirectLlm("http://gw/v1", "kb"), http=_http(handler))
    assert await mind.ask("q") == "ok"
    assert seen["temperature"] == 0.7 and seen["max_tokens"] == 400
    assert (seen["temperature"], seen["max_tokens"]) == (DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS)
    # and an explicit None is the same as not passing it at all
    seen.clear()
    await mind.ask("q", temperature=None, max_tokens=None)
    assert seen["temperature"] == 0.7 and seen["max_tokens"] == 400


async def test_the_bus_route_refuses_sampling_it_cannot_carry():
    # I4: the NATS ask contract has no field for these, so dropping them would let a
    # prompt A/B whose arms differ only in temperature run two identical arms while the
    # log claimed two. Mutation: delete the guard -> the ask succeeds and the payload
    # silently carries neither parameter.
    bus = FakeBus(reply={"from": "m", "text": "ok"})
    mind = Mind("m", bus)
    assert await mind.ask("q") == "ok"                                   # defaults: unaffected
    assert await mind.ask("q", temperature=0.7, max_tokens=400) == "ok"  # explicit defaults too
    for kwargs in ({"temperature": 0.9}, {"max_tokens": 999}, {"temperature": 0.2, "max_tokens": 50}):
        with pytest.raises(MindError, match="cannot carry sampling parameters"):
            await mind.ask("q", **kwargs)
    assert len(bus.calls) == 2                     # the refused asks never reached the bus
