"""Capture GRID.> traffic from the swarm bus for N seconds and print NDJSON.

Runs on Skywave inside /srv/grid/venv with NATS creds from the environment
(NATS_URL, NATS_USER, NATS_PASSWORD — the same names the daemons read).
Usage: bus_capture.py <seconds> [subject]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import nats


async def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    subject = sys.argv[2] if len(sys.argv) > 2 else "GRID.>"
    kwargs = {"servers": [os.environ.get("NATS_URL", "nats://swarm.ninja-portal.com:4222")], "name": "grid-receipts"}
    if os.environ.get("NATS_USER"):
        kwargs["user"] = os.environ["NATS_USER"]
        kwargs["password"] = os.environ.get("NATS_PASSWORD", "")
    nc = await nats.connect(**kwargs)
    seen = 0

    async def on_msg(msg):
        nonlocal seen
        seen += 1
        try:
            payload = json.loads(msg.data)
        except ValueError:
            payload = {"raw": msg.data[:200].decode(errors="replace")}
        print(json.dumps({"subject": msg.subject, "payload": payload}, separators=(",", ":")), flush=True)

    await nc.subscribe(subject, cb=on_msg)
    await asyncio.sleep(seconds)
    await nc.drain()
    print(json.dumps({"_capture": {"subject": subject, "seconds": seconds, "messages": seen}}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
