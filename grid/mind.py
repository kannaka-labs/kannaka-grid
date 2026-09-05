"""The colony mind, as seen from the bridge and the relay.

Writes go through the `kannaka remember` CLI against the mind's own data dir
(the serve daemon is read-only and holds the store in RAM; it sees new
memories after its scheduled dream+restart). Reads and asks go over NATS to
the daemon using kannaka's existing ask/recall contract.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

ASK_TIMEOUT_S = 180.0
REMEMBER_MIN_INTERVAL_S = 300.0
CALLER_ID = "kannaka-grid"


class MindError(Exception):
    pass


class Throttle:
    def __init__(self, min_interval_s: float, clock: Callable[[], float] = time.monotonic):
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self.min_interval_s:
            return False
        self._last[key] = now
        return True


class Mind:
    def __init__(self, agent_id: str, bus, *, kannaka_bin: str = "kannaka",
                 data_dir: Path | None = None, runner=subprocess.run):
        self.agent_id = agent_id
        self.bus = bus
        self.kannaka_bin = kannaka_bin
        self.data_dir = data_dir
        self._run = runner

    def remember(self, text: str, tags: list[str], *, category: str = "grid") -> bool:
        cmd = [self.kannaka_bin, "remember", text, "--category", category]
        if tags:
            cmd += ["--tags", ",".join(tags)]
        env = dict(os.environ)
        if self.data_dir is not None:
            env["KANNAKA_DATA_DIR"] = str(self.data_dir)
        result = self._run(cmd, env=env, capture_output=True, text=True, timeout=60)
        return result.returncode == 0

    async def ask(self, text: str, *, recall_query: str | None = None,
                  timeout_s: float = ASK_TIMEOUT_S) -> str:
        payload = {"from": CALLER_ID, "text": text, "recall_query": recall_query,
                   "no_tools": True, "mode": "full_recall"}
        reply = await self.bus.request(f"KANNAKA.ask.{self.agent_id}", payload, timeout_s=timeout_s)
        if reply.get("error"):
            raise MindError(f"{self.agent_id}: {reply['error']}")
        answer = str(reply.get("text") or "").strip()
        if not answer:
            raise MindError(f"{self.agent_id}: empty answer")
        return answer

    async def recall(self, query: str, top_k: int = 5) -> list[str]:
        try:
            reply = await self.bus.request(f"KANNAKA.recall.{self.agent_id}",
                                           {"query": query, "top_k": top_k}, timeout_s=15)
        except Exception:
            return []
        return [str(r.get("content", "")) for r in reply.get("results", []) if r.get("content")]
