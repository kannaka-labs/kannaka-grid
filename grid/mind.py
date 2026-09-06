"""The colony mind, as seen from the bridge and the relay.

Writes go through the `kannaka remember` CLI against the mind's own data dir
(the serve daemon is read-only and holds the store in RAM; it sees new
memories after its scheduled dream+restart). Reads and asks go over NATS to
the daemon using kannaka's existing ask/recall contract.

With a `DirectLlm`, asks go straight to an OpenAI-compatible gateway instead:
the serve daemon is single-threaded and blocks on its model call, so a CPU
model that needs minutes per answer makes it miss NATS pings, get dropped as
a stale connection, and lose the reply. Recall and remember stay on the daemon.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("grid.mind")

ASK_TIMEOUT_S = 180.0
REMEMBER_MIN_INTERVAL_S = 300.0
CALLER_ID = "kannaka-grid"


class MindError(Exception):
    pass


@dataclass(frozen=True)
class DirectLlm:
    """An OpenAI-compatible chat endpoint the mind asks directly."""
    base_url: str
    model: str
    api_key: str = ""

    @classmethod
    def from_env(cls, env=os.environ.get) -> "DirectLlm | None":
        url, model = env("MIND_LLM_URL"), env("MIND_LLM_MODEL")
        if not url or not model:
            return None
        key_file = env("MIND_LLM_KEY_FILE")
        key = Path(key_file).read_text(encoding="utf-8").strip() if key_file else ""
        return cls(base_url=url.rstrip("/"), model=model, api_key=key)


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
                 data_dir: Path | None = None, runner=subprocess.run,
                 ask_timeout_s: float = ASK_TIMEOUT_S, llm: DirectLlm | None = None,
                 http=None):
        # A local CPU model (kannaka-brain on debain2) takes ~2 min on an Odin
        # prompt, so the deadline is a deployment knob (MIND_ASK_TIMEOUT_S).
        # bus=None is safe whenever llm is set: ask() takes the direct route and
        # never touches self.bus, and remember()/recall() are not called on that path.
        self.ask_timeout_s = ask_timeout_s
        self.llm = llm
        self._http = http    # test seam: an httpx.AsyncClient factory
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
        try:
            result = self._run(cmd, env=env, capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("kannaka remember failed to run: %s", e)
            return False
        return result.returncode == 0

    async def ask(self, text: str, *, recall_query: str | None = None,
                  timeout_s: float | None = None) -> str:
        if timeout_s is None:
            timeout_s = self.ask_timeout_s
        if self.llm is not None:
            return await self._ask_direct(text, timeout_s)
        payload = {"from": CALLER_ID, "text": text, "recall_query": recall_query,
                   "no_tools": True, "mode": "full_recall"}
        reply = await self.bus.request(f"KANNAKA.ask.{self.agent_id}", payload, timeout_s=timeout_s)
        if reply.get("error"):
            raise MindError(f"{self.agent_id}: {reply['error']}")
        answer = str(reply.get("text") or "").strip()
        if not answer:
            raise MindError(f"{self.agent_id}: empty answer")
        return answer

    async def _ask_direct(self, text: str, timeout_s: float) -> str:
        import httpx
        llm = self.llm
        headers = {"Content-Type": "application/json"}
        if llm.api_key:
            headers["Authorization"] = f"Bearer {llm.api_key}"
        body = {"model": llm.model, "messages": [{"role": "user", "content": text}],
                "temperature": 0.7, "max_tokens": 400}
        factory = self._http or (lambda: httpx.AsyncClient(timeout=httpx.Timeout(10, read=timeout_s)))
        try:
            async with factory() as client:
                r = await client.post(f"{llm.base_url}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise MindError(f"{llm.model}@{llm.base_url}: {e.__class__.__name__}: {e}") from None
        if r.status_code != 200:
            raise MindError(f"{llm.model}@{llm.base_url}: HTTP {r.status_code}: {r.text[:200]}")
        try:
            answer = str(r.json()["choices"][0]["message"]["content"] or "").strip()
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise MindError(f"{llm.model}@{llm.base_url}: malformed reply: {e}") from None
        if not answer:
            raise MindError(f"{llm.model}@{llm.base_url}: empty answer")
        return answer

    async def recall(self, query: str, top_k: int = 5) -> list[str]:
        try:
            reply = await self.bus.request(f"KANNAKA.recall.{self.agent_id}",
                                           {"query": query, "top_k": top_k}, timeout_s=15)
        except Exception:
            return []
        return [str(r.get("content", "")) for r in reply.get("results", []) if r.get("content")]
