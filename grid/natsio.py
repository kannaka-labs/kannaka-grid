"""A thin, honest wrapper over nats-py.

Honest means: a publish that the server refuses raises. nats-py does not raise
for one by itself. A `Permissions Violation for Publish` arrives out of band on
the client's error callback and leaves the connection open, and `flush()` is
only PING/PONG, so a naive publish returns cleanly while the message is
discarded. So the bus installs that callback, records every error it reports,
and drains the record after each flush: an entry naming a permissions violation
on the subject just published becomes a BusError. The hive bridge once read the
post-PING response and discarded it, so a permissions error looked like success
and data was lost with the process still healthy. Not here.
"""
from __future__ import annotations

import collections
import json
import logging
from typing import Any, Callable

import nats
import nats.errors

log = logging.getLogger("grid.natsio")


class BusError(Exception):
    pass


class BusTimeout(BusError):
    pass


class BusClosed(BusError):
    """The client gave up reconnecting; only a process restart fixes it."""


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
        self._errors: collections.deque[str] = collections.deque(maxlen=64)

    async def connect(self) -> None:
        async def on_disconnected() -> None:
            log.warning("nats disconnected from %s; reconnecting", self.url)

        async def on_reconnected() -> None:
            log.info("nats reconnected to %s", self.url)

        async def on_closed() -> None:
            log.warning("nats connection to %s closed for good", self.url)

        async def on_error(e: Exception) -> None:
            self._errors.append(str(e))
            log.warning("nats error: %s", e)

        kwargs: dict[str, Any] = {
            "servers": [self.url], "name": "kannaka-grid",
            # 60 attempts x 2 s is two minutes; after that nats-py closes the
            # client for good and every publish fails silently forever.
            "max_reconnect_attempts": -1, "reconnect_time_wait": 2,
            "disconnected_cb": on_disconnected, "reconnected_cb": on_reconnected,
            "closed_cb": on_closed, "error_cb": on_error,
        }
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
        if nc.is_closed:
            raise BusClosed("connection closed for good")
        try:
            await nc.publish(subject, _encode(payload))
            await nc.flush(timeout=5)
        except Exception as e:  # nats-py raises a family of errors; all mean "not delivered"
            raise BusError(f"publish {subject} failed: {e}") from e
        while self._errors:     # a refusal arrives on the callback, never on flush
            entry = self._errors.popleft()
            if "Permissions Violation" in entry and subject in entry:
                raise BusError(f"publish {subject} refused: {entry}")

    async def request(self, subject: str, payload: dict, timeout_s: float) -> dict:
        nc = self._require()
        if nc.is_closed:
            raise BusClosed("connection closed for good")
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
