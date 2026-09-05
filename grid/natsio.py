"""A thin, honest wrapper over nats-py.

Honest means: a publish that the server refuses raises. The hive bridge once
read the post-PING response and discarded it, so a permissions error looked
like success and data was lost with the process still healthy. Not here.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import nats
import nats.errors


class BusError(Exception):
    pass


class BusTimeout(BusError):
    pass


def _encode(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


class Bus:
    def __init__(self, url: str, *, user: str | None = None, password: str | None = None,
                 connect: Callable[..., Any] = nats.connect):
        self.url = url
        self.user = user
        self.password = password
        self._connect = connect
        self._nc = None

    async def connect(self) -> None:
        kwargs: dict[str, Any] = {"servers": [self.url], "name": "kannaka-grid"}
        if self.user:
            kwargs["user"] = self.user
            kwargs["password"] = self.password
        self._nc = await self._connect(**kwargs)

    def _require(self):
        if self._nc is None:
            raise BusError("bus not connected")
        return self._nc

    async def publish(self, subject: str, payload: dict) -> None:
        nc = self._require()
        try:
            await nc.publish(subject, _encode(payload))
            await nc.flush(timeout=5)
        except Exception as e:  # nats-py raises a family of errors; all mean "not delivered"
            raise BusError(f"publish {subject} failed: {e}") from e

    async def request(self, subject: str, payload: dict, timeout_s: float) -> dict:
        nc = self._require()
        try:
            msg = await nc.request(subject, _encode(payload), timeout=timeout_s)
        except nats.errors.TimeoutError as e:
            raise BusTimeout(f"no reply on {subject} within {timeout_s}s") from e
        except Exception as e:
            raise BusError(f"request {subject} failed: {e}") from e
        try:
            return json.loads(msg.data)
        except ValueError as e:
            raise BusError(f"reply on {subject} is not JSON: {msg.data[:80]!r}") from e

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.close()
            self._nc = None
