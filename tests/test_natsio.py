import json

import nats.errors
import pytest

from grid.natsio import Bus, BusClosed, BusError, BusTimeout


class FakeMsg:
    def __init__(self, data: bytes):
        self.data = data


class FakeNC:
    def __init__(self):
        self.published: list[tuple[str, bytes]] = []
        self.flushed = 0
        self.closed = False
        self.is_closed = False
        self.reply: bytes | None = None
        self.raise_timeout = False
        self.fail_flush = False
        self.error_cb = None            # the callback the bus registered at connect
        self.error_on_publish = None    # reported through it, the way the server does

    async def publish(self, subject, data):
        self.published.append((subject, data))
        if self.error_on_publish is not None:
            await self.error_cb(self.error_on_publish)

    async def flush(self, timeout=None):
        self.flushed += 1
        if self.fail_flush:
            raise RuntimeError("nats: -ERR permissions violation")

    async def request(self, subject, data, timeout=None):
        if self.raise_timeout:
            import nats.errors
            raise nats.errors.TimeoutError()
        self.last_request = (subject, data, timeout)
        return FakeMsg(self.reply or b"{}")

    async def close(self):
        self.closed = True


async def make_bus(nc: FakeNC) -> Bus:
    async def connect(**kwargs):
        connect.kwargs = kwargs
        return nc
    bus = Bus("nats://example:4222", user="u", password="p", connect=connect)
    await bus.connect()
    bus._connect_kwargs = connect.kwargs
    nc.error_cb = connect.kwargs["error_cb"]
    return bus


async def test_connect_passes_url_and_creds():
    bus = await make_bus(FakeNC())
    assert bus._connect_kwargs["servers"] == ["nats://example:4222"]
    assert bus._connect_kwargs["user"] == "u" and bus._connect_kwargs["password"] == "p"


async def test_connect_never_gives_up_reconnecting_and_installs_callbacks():
    bus = await make_bus(FakeNC())
    kwargs = bus._connect_kwargs
    # 60 x 2 s is the nats-py default: two minutes unreachable and the client
    # is CLOSED for good, with every later publish failing silently.
    assert kwargs["max_reconnect_attempts"] == -1
    assert kwargs["reconnect_time_wait"] == 2
    assert {"disconnected_cb", "reconnected_cb", "closed_cb", "error_cb"} <= kwargs.keys()
    assert all(callable(kwargs[k]) for k in
               ("disconnected_cb", "reconnected_cb", "closed_cb", "error_cb"))


async def test_publish_and_request_raise_bus_closed_once_the_client_gave_up():
    nc = FakeNC()
    bus = await make_bus(nc)
    nc.is_closed = True
    with pytest.raises(BusClosed):
        await bus.publish("GRID.c.event.storm", {"a": 1})
    with pytest.raises(BusClosed):
        await bus.request("KANNAKA.ask.m", {"text": "hi"}, timeout_s=1)
    assert nc.published == [] and nc.flushed == 0


async def test_publish_encodes_json_and_flushes():
    nc = FakeNC()
    bus = await make_bus(nc)
    await bus.publish("GRID.c.event.storm", {"a": 1})
    assert nc.published == [("GRID.c.event.storm", json.dumps({"a": 1}, separators=(",", ":")).encode())]
    assert nc.flushed == 1


async def test_publish_raises_when_server_refuses():
    nc = FakeNC()
    nc.fail_flush = True
    bus = await make_bus(nc)
    with pytest.raises(BusError):
        await bus.publish("GRID.c.event.storm", {"a": 1})


async def test_publish_raises_when_the_server_refuses_the_subject():
    nc = FakeNC()
    bus = await make_bus(nc)
    # flush is PING/PONG and succeeds; the refusal arrives on the error callback.
    nc.error_on_publish = nats.errors.Error(
        'nats: Permissions Violation for Publish to "GRID.c.event.storm"')
    with pytest.raises(BusError, match="refused"):
        await bus.publish("GRID.c.event.storm", {"a": 1})
    assert nc.flushed == 1


async def test_a_violation_on_another_subject_does_not_fail_this_publish():
    nc = FakeNC()
    bus = await make_bus(nc)
    nc.error_on_publish = nats.errors.Error(
        'nats: Permissions Violation for Publish to "KANNAKA.ask.other"')
    await bus.publish("GRID.c.event.storm", {"a": 1})


async def test_request_decodes_reply_and_passes_timeout():
    nc = FakeNC()
    nc.reply = b'{"from":"m","text":"ok"}'
    bus = await make_bus(nc)
    assert await bus.request("KANNAKA.ask.m", {"text": "hi"}, timeout_s=7.5) == {"from": "m", "text": "ok"}
    assert nc.last_request[0] == "KANNAKA.ask.m" and nc.last_request[2] == 7.5


async def test_request_timeout_is_bus_timeout():
    nc = FakeNC()
    nc.raise_timeout = True
    bus = await make_bus(nc)
    with pytest.raises(BusTimeout):
        await bus.request("KANNAKA.ask.m", {"text": "hi"}, timeout_s=1)


async def test_publish_before_connect_is_an_error():
    bus = Bus("nats://x:4222")
    with pytest.raises(BusError):
        await bus.publish("s", {})


async def test_close_closes():
    nc = FakeNC()
    bus = await make_bus(nc)
    await bus.close()
    assert nc.closed
