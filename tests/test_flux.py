import httpx
import pytest

from grid.flux import Flux, flux_event


def test_flux_event_shape():
    body = flux_event(stream="grid", source="kannaka-grid", entity_id="grid:colony-one",
                      properties={"population": 3}, ts_ms=99)
    assert body == {
        "streamId": "grid", "sourceId": "kannaka-grid", "entityId": "grid:colony-one",
        "stream": "grid", "source": "kannaka-grid", "timestamp": 99,
        "payload": {"entity_id": "grid:colony-one", "properties": {"population": 3}},
    }


async def test_upsert_posts_to_api_events():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read()
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    flux = Flux("http://flux:3000/", client=client)
    await flux.upsert("grid:colony-one", {"population": 3})
    assert seen["url"] == "http://flux:3000/api/events"
    assert b'"entityId":"grid:colony-one"' in seen["json"]


async def test_upsert_raises_on_error_status():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    flux = Flux("http://flux:3000", client=client)
    with pytest.raises(httpx.HTTPStatusError):
        await flux.upsert("grid:x", {})
