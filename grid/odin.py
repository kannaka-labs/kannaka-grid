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
from typing import Awaitable, Callable

import httpx

from .envelope import envelope, now_ms, subject
from .mind import ASK_TIMEOUT_S, REMEMBER_MIN_INTERVAL_S, Mind, MindError, Throttle
from .natsio import Bus, BusClosed, BusError, BusTimeout
from .proposal import Rejection, parse_request, render_prompt, validate, write_proposal
from .proposal_log import ProposalLog, proposal_entry

log = logging.getLogger("grid.odin")

UNPARSEABLE_LIMIT = 3       # consecutive failed parses before the file is binned


class Relay:
    def __init__(self, queue: Path, *, colony: str, agent_id: str, mind, bus,
                 timeout_s: float = ASK_TIMEOUT_S, retry_after_s: float = 60.0,
                 clock=time.monotonic, throttle: Throttle | None = None,
                 state_fetch: Callable[[], Awaitable[dict]] | None = None,
                 proposal_log: ProposalLog | None = None, model_alias: str = ""):
        self.queue = Path(queue)
        self.colony = colony
        self.agent_id = agent_id
        self.mind = mind
        self.bus = bus
        self.timeout_s = timeout_s
        self.retry_after_s = retry_after_s
        self._clock = clock
        self.state_fetch = state_fetch
        self.throttle = throttle or Throttle(REMEMBER_MIN_INTERVAL_S, clock=clock)
        self.proposal_log = proposal_log
        self.model_alias = model_alias
        self.stale_count = 0
        self._retry_not_before: dict[str, float] = {}
        self._unparseable: dict[str, int] = {}

    async def _publish(self, kind: str, epoch: int, tick: int, **fields) -> None:
        try:
            await self.bus.publish(subject(self.colony, "event", f"odin.{kind}"),
                                   envelope(agent_id=self.agent_id, colony=self.colony,
                                            epoch=epoch, tick=tick, kind=f"odin.{kind}", **fields))
        except BusClosed:
            raise
        except BusError as e:
            log.error("odin.%s publish failed: %s", kind, e)

    def _log(self, req, prompt: str, proposal: list[str] | None, outcome: str, reason: str | None,
             epoch: int, tick: int) -> None:
        if self.proposal_log is None:
            return
        entry = proposal_entry(req=req, proposal=proposal, outcome=outcome, reason=reason,
                               epoch=epoch, tick=tick, colony=self.colony, mind=self.agent_id,
                               model_alias=self.model_alias, prompt=prompt, ts_ms=now_ms())
        self.proposal_log.record(entry, prompt)

    async def _epoch_tick(self) -> tuple[int, int]:
        """thegrid's Organism.telemetry() carries no epoch or tick; /api/state does."""
        if self.state_fetch is None:
            return 0, 0
        try:
            state = await self.state_fetch()
            return int(state.get("epoch", 0) or 0), int(state.get("tick", 0) or 0)
        except Exception as e:      # an envelope with 0/0 beats no envelope at all
            log.warning("epoch/tick unavailable from the colony: %s", e)
            return 0, 0

    async def poll_once(self) -> str | None:
        request_path = self.queue / "request.json"
        proposal_path = self.queue / "proposal.json"
        if proposal_path.exists():
            if request_path.exists():
                return None                     # thegrid has not consumed it yet
            # The request is gone, so thegrid will never read this proposal.
            stale = self.queue / f"proposal.stale-{int(time.time())}.json"
            proposal_path.rename(stale)
            self.stale_count += 1
            log.warning("stale proposal with no request: renamed to %s", stale.name)
        if not request_path.exists():
            return None
        key = str(request_path)
        try:
            req = parse_request(request_path)
        except ValueError as e:
            seen = self._unparseable[key] = self._unparseable.get(key, 0) + 1
            log.error("unreadable request (%d/%d): %s", seen, UNPARSEABLE_LIMIT, e)
            if seen < UNPARSEABLE_LIMIT:
                return None
            # thegrid writes a request only when none exists, so leaving a file
            # nobody can read here would block the Odin path forever.
            request_path.unlink(missing_ok=True)
            del self._unparseable[key]
            epoch, tick = await self._epoch_tick()
            await self._publish("rejected", epoch, tick, request_id=None,
                                reason="unparseable request")
            return "rejected"
        self._unparseable.pop(key, None)        # consecutive, so a good parse resets
        if self._clock() < self._retry_not_before.get(req.request_id, 0.0):
            return None

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
        epoch, tick = await self._epoch_tick()
        if isinstance(result, Rejection):
            log.info("rejected %s: %s", req.request_id, result.reason)
            request_path.unlink(missing_ok=True)   # let thegrid file a fresh request
            await self._publish("rejected", epoch, tick, request_id=req.request_id, reason=result.reason)
            self._log(req, prompt, None, "rejected", result.reason, epoch, tick)
            if self.throttle.allow("odin.rejected"):
                # Spec 6: the mind learns what does not work, not only what does.
                await asyncio.to_thread(
                    self.mind.remember,
                    f"Odin rejected a proposal for request {req.request_id}: "
                    f"{result.reason} (parent genome {req.genome[:8]}...)",
                    [self.colony, "odin.rejected"])
            return "rejected"
        if not write_proposal(self.queue, req, result):
            log.info("request %s vanished before the proposal landed", req.request_id)
            return None
        await self._publish("proposed", epoch, tick, request_id=req.request_id,
                            genome_len=len(result), mind=self.agent_id)
        self._log(req, prompt, result, "proposed", None, epoch, tick)
        return "proposed"


@dataclass(frozen=True)
class RelayConfig:
    queue: Path
    colony: str
    colony_url: str
    agent_id: str
    nats_url: str
    nats_user: str | None
    nats_password: str | None
    mind_data_dir: str | None
    kannaka_bin: str
    poll_s: float
    verdicts_dir: str
    model_alias: str

    @classmethod
    def from_env(cls) -> "RelayConfig":
        env = os.environ.get
        colony = env("COLONY_SLUG")
        if not colony:
            raise SystemExit("COLONY_SLUG is required (the slug of thegrid's --name)")
        return cls(
            queue=Path(env("ODIN_QUEUE", f"/srv/grid/{colony}/state/operator")),
            colony=colony,
            colony_url=env("COLONY_URL", "http://127.0.0.1:8787").rstrip("/"),
            agent_id=env("MIND_AGENT_ID", f"grid-{colony}"),
            nats_url=env("NATS_URL", "nats://swarm.ninja-portal.com:4222"),
            nats_user=env("NATS_USER") or None,
            nats_password=env("NATS_PASSWORD") or None,
            mind_data_dir=env("MIND_DATA_DIR") or None,
            kannaka_bin=env("KANNAKA_BIN", "kannaka"),
            poll_s=float(env("ODIN_POLL_S", "1")),
            verdicts_dir=env("VERDICTS_DIR", f"/srv/grid/{colony}/verdicts"),
            model_alias=env("MIND_MODEL_ALIAS", "agent-brain"),
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
    mind = Mind(cfg.agent_id, bus, kannaka_bin=cfg.kannaka_bin,
                data_dir=Path(cfg.mind_data_dir) if cfg.mind_data_dir else None)
    async with httpx.AsyncClient(timeout=2) as client:
        async def fetch_state() -> dict:
            r = await client.get(f"{cfg.colony_url}/api/state")
            r.raise_for_status()
            return r.json()

        relay = Relay(cfg.queue, colony=cfg.colony, agent_id=cfg.agent_id, mind=mind, bus=bus,
                      state_fetch=fetch_state, proposal_log=ProposalLog(Path(cfg.verdicts_dir)),
                      model_alias=cfg.model_alias)
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
