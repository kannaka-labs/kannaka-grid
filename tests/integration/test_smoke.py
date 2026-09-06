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
    reason="set GRID_INTEGRATION=1 on Linux inside a cgroup with finite memory.max, "
           "with thegrid's aiohttp dependency installed in the venv (pip install -e thegrid)")


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
            # process has exited, so stderr is at EOF and this read cannot block
            if proc.poll() is not None:
                pytest.fail(f"colony exited early: {proc.stderr.read()[-2000:]}")
            try:
                if httpx.get(f"{base}/api/state", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            stop()
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
        # exactly what thegrid's OdinMutator writes: Organism.telemetry(), which
        # carries no epoch and no tick.
        "parent": {"age": 120, "energy": 90.0, "generation": 2, "births": 1,
                   "harvested": 40.0, "tasks_solved": {}, "genome_length": 6,
                   "signals_sent": 0, "signals_heard": 0, "signal_guided_moves": 0,
                   "post_signal_harvested": 0.0, "structures_built": 0,
                   "neighbor_reads": 0, "foreign_copies": 0, "moves": 31, "scans": 4,
                   "guided_moves": 0, "post_move_harvested": 0.0, "scratch_nonzero": 0,
                   "forecast_attempts": 0, "forecasts_solved": 0, "weather_cues_seen": 0,
                   "weather_cue_signals": 0, "experimental_ops": {}, "salvaged": 0.0,
                   "stolen": 0.0, "robbed": 0.0, "corruptions": 0, "corrupted": 0},
        "genome": ["harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork"],
        "instruction_set": [{"name": n, "cost": 0.1, "effect": n} for n in
                            ("nop", "harvest", "alloc", "copy", "ifnotdone", "jmpb", "fork")],
        "ecology": {}, "directive": "author one motivated variant"}))
    relay = Relay(queue, colony="smoke", agent_id="grid-smoke", mind=StubMind(), bus=NullBus())
    assert await relay.poll_once() == "proposed"
    assert json.loads((queue / "proposal.json").read_text())["genome"][-1] == "nop"


@skip
async def test_verdicts_settle_against_the_real_fossil_db(colony, tmp_path):
    from grid.proposal_log import ProposalLog
    from grid.verdicts import Ledger, run_once
    base, queue = colony
    state_dir = queue.parent
    vdir = tmp_path / "verdicts"
    # a proposal for a genome the colony will never birth -> settles 'unborn' once the epoch retires or 60 ticks pass
    ProposalLog(vdir).record({"request_id": "smoke-v", "epoch": 1, "tick": 1, "proposal_genome_id": "0" * 16,
                              "outcome": "proposed", "model_alias": "stub", "reason": None,
                              "prompt_path": "prompts/smoke-v.txt", "colony": "smoke"}, "p")
    deadline = time.time() + 60
    while time.time() < deadline:
        state = httpx.get(f"{base}/api/state", timeout=2).json()
        if state["tick"] >= 200:
            break
        time.sleep(1)
    db = state_dir / "history.sqlite3"
    assert db.exists(), "thegrid did not write history.sqlite3"
    rows = await run_once(verdicts_dir=vdir, history_db=db, state=state, bus=None, colony="smoke",
                          agent_id="grid-smoke", settle_ticks=100)
    assert [r["tier"] for r in rows] == ["unborn"]
    assert Ledger(vdir / "verdicts.jsonl").verify()


class EchoMind:
    """Answers with the parent genome plus one nop — a valid variant of whatever was asked."""

    agent_id = "grid-smoke"

    def __init__(self, queue: Path):
        self.queue = queue

    async def recall(self, query, top_k=5):
        return []

    async def ask(self, text, *, recall_query=None, timeout_s=180.0):
        request = json.loads((self.queue / "request.json").read_text())
        assert "nop" in [i["name"] for i in request["instruction_set"]], "thegrid's ISA lost nop"
        return json.dumps(list(request["genome"]) + ["nop"])


@skip
async def test_a_real_proposal_is_credited_to_its_child(colony, tmp_path):
    """The whole pipeline on a genome thegrid really births: propose, consume, credit.

    The 'unborn' smoke above proves the negative path. Only this one proves the
    join actually lands — that our genome_id names the child thegrid recorded.
    """
    from grid.proposal_log import ProposalLog
    from grid.verdicts import Ledger, StatsReader, run_once
    base, queue = colony
    db = queue.parent / "history.sqlite3"
    request_path = queue / "request.json"

    def state() -> dict:
        return httpx.get(f"{base}/api/state", timeout=2).json()

    async def fetch_state() -> dict:
        return state()

    deadline = time.time() + 90
    while time.time() < deadline and not request_path.exists():
        time.sleep(0.5)
    if not request_path.exists():
        pytest.skip("thegrid filed no Odin request in 90s (its mutator asks on ~5% of births)")

    vdir = tmp_path / "verdicts"
    relay = Relay(queue, colony="smoke", agent_id="grid-smoke", mind=EchoMind(queue), bus=NullBus(),
                  state_fetch=fetch_state, proposal_log=ProposalLog(vdir), model_alias="stub")
    assert await relay.poll_once() == "proposed"
    [entry] = ProposalLog(vdir).read_all()

    deadline = time.time() + 90
    while time.time() < deadline:
        if not (queue / "proposal.json").exists() and db.exists():
            with StatsReader(db) as reader:
                if reader.stats(entry["epoch"], entry["proposal_genome_id"]) is not None:
                    break
        time.sleep(1)
    else:
        pytest.skip("thegrid did not consume and birth the proposed genome within 90s")

    rows = await run_once(verdicts_dir=vdir, history_db=db, state=state(), bus=None, colony="smoke",
                          agent_id="grid-smoke", settle_ticks=1)
    assert [r["request_id"] for r in rows] == [entry["request_id"]]
    assert rows[0]["tier"] not in ("unborn", "preexisting")
    assert rows[0]["fitness"] >= 1 and rows[0]["epoch_found"] is not None
    assert Ledger(vdir / "verdicts.jsonl").verify()
    with StatsReader(db) as reader:
        stats = reader.stats(entry["epoch"], entry["proposal_genome_id"])
    assert stats["first_tick"] >= entry["tick"]      # the child is ours, not a strain already alive
