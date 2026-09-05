# tests/test_mind.py
import subprocess
from pathlib import Path

import pytest

from grid.mind import ASK_TIMEOUT_S, REMEMBER_MIN_INTERVAL_S, Mind, MindError, Throttle


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
