"""The Odin relay: thegrid's mutation request → the colony mind → a proposal.

Polls the queue directory once a second. inotify would be tighter, but a
request is consumed only at a later birth, so a second of latency is nothing
and one fewer dependency is something.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .envelope import envelope, subject
from .mind import ASK_TIMEOUT_S, Mind, MindError
from .natsio import Bus, BusClosed, BusError, BusTimeout
from .proposal import Rejection, parse_request, render_prompt, validate, write_proposal

log = logging.getLogger("grid.odin")


class Relay:
    def __init__(self, queue: Path, *, colony: str, agent_id: str, mind, bus,
                 timeout_s: float = ASK_TIMEOUT_S, retry_after_s: float = 60.0,
                 clock=time.monotonic):
        self.queue = Path(queue)
        self.colony = colony
        self.agent_id = agent_id
        self.mind = mind
        self.bus = bus
        self.timeout_s = timeout_s
        self.retry_after_s = retry_after_s
        self._clock = clock
        self._retry_not_before: dict[str, float] = {}

    async def _publish(self, kind: str, epoch: int, tick: int, **fields) -> None:
        try:
            await self.bus.publish(subject(self.colony, "event", f"odin.{kind}"),
                                   envelope(agent_id=self.agent_id, colony=self.colony,
                                            epoch=epoch, tick=tick, kind=f"odin.{kind}", **fields))
        except BusClosed:
            raise
        except BusError as e:
            log.error("odin.%s publish failed: %s", kind, e)

    async def poll_once(self) -> str | None:
        request_path = self.queue / "request.json"
        if (self.queue / "proposal.json").exists() or not request_path.exists():
            return None
        try:
            req = parse_request(request_path)
        except ValueError as e:
            log.error("unreadable request: %s", e)
            return None
        if self._clock() < self._retry_not_before.get(req.request_id, 0.0):
            return None
        epoch = int(req.parent.get("epoch", 0) or 0)
        tick = int(req.parent.get("tick", 0) or 0)

        recall = await self.mind.recall(f"colony {self.colony} genome lineage {' '.join(req.genome[:6])}")
        prompt = render_prompt(req, recall)
        try:
            reply = await self.mind.ask(prompt, recall_query=f"colony {self.colony}", timeout_s=self.timeout_s)
        except BusClosed:
            raise
        except (BusTimeout, BusError, MindError) as e:
            log.warning("mind unavailable for %s: %s; retry in %.0fs", req.request_id, e, self.retry_after_s)
            self._retry_not_before[req.request_id] = self._clock() + self.retry_after_s
            return "deferred"

        result = validate(reply, req)
        if isinstance(result, Rejection):
            log.info("rejected %s: %s", req.request_id, result.reason)
            request_path.unlink(missing_ok=True)   # let thegrid file a fresh request
            await self._publish("rejected", epoch, tick, request_id=req.request_id, reason=result.reason)
            return "rejected"
        if not write_proposal(self.queue, req, result):
            log.info("request %s vanished before the proposal landed", req.request_id)
            return None
        await self._publish("proposed", epoch, tick, request_id=req.request_id,
                            genome_len=len(result), mind=self.agent_id)
        return "proposed"


@dataclass(frozen=True)
class RelayConfig:
    queue: Path
    colony: str
    agent_id: str
    nats_url: str
    nats_user: str | None
    nats_password: str | None
    poll_s: float

    @classmethod
    def from_env(cls) -> "RelayConfig":
        env = os.environ.get
        colony = env("COLONY_SLUG")
        if not colony:
            raise SystemExit("COLONY_SLUG is required (the slug of thegrid's --name)")
        return cls(
            queue=Path(env("ODIN_QUEUE", f"/srv/grid/{colony}/state/operator")),
            colony=colony,
            agent_id=env("MIND_AGENT_ID", f"grid-{colony}"),
            nats_url=env("NATS_URL", "nats://swarm.ninja-portal.com:4222"),
            nats_user=env("NATS_USER") or None,
            nats_password=env("NATS_PASSWORD") or None,
            poll_s=float(env("ODIN_POLL_S", "1")),
        )


async def run(cfg: RelayConfig) -> None:
    bus = Bus(cfg.nats_url, user=cfg.nats_user, password=cfg.nats_password)
    backoff = 1.0
    while True:
        try:
            await bus.connect()
            break
        except Exception as e:      # a boot-time DNS or NATS blip must not burn a start slot
            log.error("nats connect failed: %s; retrying in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    mind = Mind(cfg.agent_id, bus)
    relay = Relay(cfg.queue, colony=cfg.colony, agent_id=cfg.agent_id, mind=mind, bus=bus)
    log.info("watching %s for %s", cfg.queue, cfg.agent_id)
    while True:
        try:
            outcome = await relay.poll_once()
            if outcome:
                log.info("%s", outcome)
        except BusClosed as e:  # EX_TEMPFAIL: only a restart gets a live client back
            log.error("nats gave up: %s; exiting for systemd to restart", e)
            raise SystemExit(75) from e
        except Exception as e:  # one bad poll must not kill the relay
            log.exception("poll failed: %s", e)
        await asyncio.sleep(cfg.poll_s)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run(RelayConfig.from_env()))


if __name__ == "__main__":
    main()
