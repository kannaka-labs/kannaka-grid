"""Publish the colony's epoch state as one Flux entity per colony."""
from __future__ import annotations

import httpx

from .envelope import now_ms


def flux_event(*, stream: str, source: str, entity_id: str, properties: dict, ts_ms: int) -> dict:
    return {
        "streamId": stream, "sourceId": source, "entityId": entity_id,
        "stream": stream, "source": source, "timestamp": ts_ms,
        "payload": {"entity_id": entity_id, "properties": properties},
    }


class Flux:
    def __init__(self, base_url: str, *, stream: str = "grid", source: str = "kannaka-grid",
                 client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.stream = stream
        self.source = source
        self._client = client or httpx.AsyncClient(timeout=10)

    async def upsert(self, entity_id: str, properties: dict) -> None:
        body = flux_event(stream=self.stream, source=self.source, entity_id=entity_id,
                          properties=properties, ts_ms=now_ms())
        response = await self._client.post(f"{self.base_url}/api/events", json=body)
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
