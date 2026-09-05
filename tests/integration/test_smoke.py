import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from grid.bridge import iter_sse_data
from grid.distill import Distiller, Snapshot
from grid.odin import Relay

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
THEGRID = ROOT / "thegrid"


def _finite_memory_max() -> bool:
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                text = (Path("/sys/fs/cgroup") / line[3:].lstrip("/") / "memory.max").read_text().strip()
                return text != "max"
    except OSError:
        pass
    return False


skip = pytest.mark.skipif(
    os.environ.get("GRID_INTEGRATION") != "1" or sys.platform != "linux" or not _finite_memory_max(),
    reason="set GRID_INTEGRATION=1 on Linux inside a cgroup with finite memory.max")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def colony(tmp_path):
    port = free_port()
    state = tmp_path / "state" / "colony.pkl"
    state.parent.mkdir(parents=True)
    env = dict(os.environ, THEGRID_DIR=str(THEGRID), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, "-m", "grid.launcher", "--host", "127.0.0.1", "--port", str(port),
                             "--ticks-per-second", "50", "--mutator", "odin", "--name", "smoke",
                             "--state", str(state)], env=env, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def stop() -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"colony exited early: {proc.stderr.read()[-2000:]}")
            try:
                if httpx.get(f"{base}/api/state", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.fail(f"colony did not come up: {proc.stderr.read()[-2000:]}")
    except BaseException:
        stop()
        raise
    try:
        yield base, state.parent / "operator"
    finally:
        stop()


@skip
def test_stream_distills_epoch_started(colony):
    base, _ = colony
    d = Distiller()
    seen = []
    with httpx.stream("GET", f"{base}/api/stream", timeout=httpx.Timeout(10, read=15)) as r:
        for line in r.iter_lines():
            for data in iter_sse_data([line]):
                seen += d.feed(Snapshot.from_state(json.loads(data)))
                if len(seen) >= 1:
                    break
            if seen:
                break
    assert seen[0].kind == "epoch.started"


class StubMind:
    agent_id = "grid-smoke"

    async def recall(self, query, top_k=5):
        return []

    async def ask(self, text, *, recall_query=None, timeout_s=180.0):
        return '["harvest","alloc","copy","ifnotdone","jmpb","fork","nop"]'


class NullBus:
    async def publish(self, subject, payload):
        pass


@skip
async def test_relay_answers_a_request_written_by_thegrid_shape(colony):
    _, queue = colony
    queue.mkdir(exist_ok=True)
    (queue / "request.json").write_text(json.dumps({
        "request_id": "smoke1", "created_at": time.time(),
        "parent": {"id": 1, "energy": 90.0, "tick": 10, "epoch": 1},
        "genome": ["harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork"],
        "instruction_set": [{"name": n, "cost": 0.1, "effect": n} for n in
                            ("nop", "harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork")],
        "ecology": {}, "directive": "author one motivated variant"}))
    relay = Relay(queue, colony="smoke", agent_id="grid-smoke", mind=StubMind(), bus=NullBus())
    assert await relay.poll_once() == "proposed"
    assert json.loads((queue / "proposal.json").read_text())["genome"][-1] == "nop"
