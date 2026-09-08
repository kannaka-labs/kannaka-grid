# Colony Live A/B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the colony's own verdict binding on releases: the relay splits Odin asks 80/20 between the incumbent and a promoted candidate, verdicts attribute each child to its author, and the weekly gate releases the candidate only if its reproduced-rate is no worse than the incumbent's — with a daily stuck check so a dead A/B is aborted within 48 hours.

**Architecture:** Three touch points. `grid/odin.py` gains a second direct arm chosen deterministically per request. `grid/weekly.py` gains the A/B evaluation (`ab_arms`, `ab_decide`, `ab_state`) and a two-stage `release` plus an `ab` verb. `grid/versions.py` gains two decisions. Deploy adds the nightly stuck check to the existing verdicts unit. No new dependencies.

**Tech Stack:** Python 3.12+ stdlib, httpx (present), pytest `asyncio_mode=auto`, systemd.

**Spec:** `docs/superpowers/specs/2026-09-07-colony-live-ab-design.md` (§8 is binding for stuck recovery).

## Global Constraints

- **Arm assignment is deterministic:** `candidate iff int(sha256(request_id.encode()).hexdigest()[:8], 16) / 2**32 < split`. Split default `0.2`. An empty `MIND_LLM_CANDIDATE` means the relay is byte-for-byte today's behaviour.
- **Labels are concrete tags:** on the candidate arm `model_alias` = `MIND_LLM_CANDIDATE`; on the incumbent arm `model_alias` = `MIND_MODEL_ALIAS`. Verdict rows are unchanged.
- **A failed candidate ask defers**, never re-routes to the incumbent.
- **Window = proposal `ts` ≥ `since_ts`** (joined to verdict rows by `request_id`), tiers `preexisting`/`unknown`/`rejected` excluded from both arms. `reproduced` = tier ∈ {reproduced, growing, enduring, established}.
- **Constants:** `AB_SPLIT = 0.2`, `MIN_SAMPLE = 100`, `TOLERANCE = 0.10`, `MAX_WEEKS = 2`, `STUCK_AFTER_S = 48*3600`, `STUCK_INCUMBENT_MIN = 50`. Win iff both arms ≥ `MIN_SAMPLE` and `rate_c >= rate_i * (1 - TOLERANCE)`.
- **The loser is never released.** Win = the existing three-step flip with `MIND_LLM_CANDIDATE` cleared in the same env write; loss/stuck/inconclusive-at-max = `held`, candidate cleared, relay restarted, `current` untouched. A lost/stuck tag is blocked from a new A/B until `ab --forget`.
- Dry-run writes nothing (no ledger row, no env, no restart). Every gate failure records `held` and never flips (sub-project 2 rules stand).
- LF, no BOM; `grid/` never imports thegrid; YAGNI; pristine test output; suite green under `-W error` after every task; commit trailer lines as this repo uses.

---

### Task 1: The relay's second arm

**Files:**
- Modify: `grid/odin.py`
- Test: `tests/test_odin.py`

**Interfaces:**
- Produces: `arm_for(request_id: str, split: float) -> str` (`"candidate"`|`"incumbent"`); `Relay(..., candidate_mind=None, candidate_alias: str = "", split: float = AB_SPLIT)`; `RelayConfig.candidate_tag: str` (`MIND_LLM_CANDIDATE`, default `""`), `RelayConfig.ab_split: float` (`MIND_AB_SPLIT`, default `0.2`), `RelayConfig.candidate_llm: DirectLlm | None` (built from `MIND_LLM_URL`/`MIND_LLM_KEY_FILE` with model = candidate tag; `None` when no candidate or no direct route); `AB_SPLIT = 0.2`.
- Consumes: `grid.mind.Mind`, `DirectLlm`, `grid.proposal_log.proposal_entry` (unchanged: `model_alias` param).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_odin.py`; reuse its existing fixtures/fakes for a request dir, a fake mind, a fake bus)

```python
def test_arm_for_is_deterministic_and_respects_the_split():
    from grid.odin import arm_for
    assert arm_for("r1", 0.2) == arm_for("r1", 0.2)
    assert arm_for("anything", 0.0) == "incumbent" and arm_for("anything", 1.0) == "candidate"
    ids = [f"req-{i}" for i in range(2000)]
    share = sum(arm_for(i, 0.2) == "candidate" for i in ids) / len(ids)
    assert 0.15 < share < 0.25                       # ~20%, deterministic per id


async def test_relay_asks_the_candidate_arm_and_labels_it(tmp_path):
    # Build a request whose id hashes to the candidate arm at split 0.2, and one that does not.
    from grid.odin import arm_for
    cand_id = next(i for i in (f"c{i}" for i in range(500)) if arm_for(i, 0.2) == "candidate")
    inc_id = next(i for i in (f"i{i}" for i in range(500)) if arm_for(i, 0.2) == "incumbent")
    asked = {"incumbent": [], "candidate": []}
    incumbent = _FakeMind(reply='["nop"]', seen=asked["incumbent"])     # the file's existing fake mind pattern
    candidate = _FakeMind(reply='["nop"]', seen=asked["candidate"])
    for rid, expect_arm, expect_alias in ((cand_id, "candidate", "kannaka-brain-v4"), (inc_id, "incumbent", "kannaka-brain-v3")):
        queue = tmp_path / rid; queue.mkdir()
        _write_request(queue, request_id=rid)                            # the file's existing request helper
        log = ProposalLog(tmp_path / f"v-{rid}")
        relay = Relay(queue, colony="c", agent_id="a", mind=incumbent, bus=_FakeBus(), state_fetch=_state(1, 10),
                      proposal_log=log, model_alias="kannaka-brain-v3",
                      candidate_mind=candidate, candidate_alias="kannaka-brain-v4", split=0.2)
        assert await relay.poll_once() == "proposed"
        assert log.read_all()[-1]["model_alias"] == expect_alias
    assert len(asked["candidate"]) == 1 and len(asked["incumbent"]) == 1


async def test_relay_without_a_candidate_is_unchanged(tmp_path):
    queue = tmp_path / "q"; queue.mkdir(); _write_request(queue, request_id="x1")
    log = ProposalLog(tmp_path / "v")
    relay = Relay(queue, colony="c", agent_id="a", mind=_FakeMind(reply='["nop"]'), bus=_FakeBus(), state_fetch=_state(1, 10),
                  proposal_log=log, model_alias="kannaka-brain-v3")           # no candidate kwargs
    assert await relay.poll_once() == "proposed" and log.read_all()[-1]["model_alias"] == "kannaka-brain-v3"


async def test_failed_candidate_ask_defers_and_never_reroutes(tmp_path):
    from grid.mind import MindError
    from grid.odin import arm_for
    cand_id = next(i for i in (f"c{i}" for i in range(500)) if arm_for(i, 0.2) == "candidate")
    queue = tmp_path / "q"; queue.mkdir(); _write_request(queue, request_id=cand_id)
    inc_seen = []
    relay = Relay(queue, colony="c", agent_id="a", mind=_FakeMind(reply='["nop"]', seen=inc_seen), bus=_FakeBus(),
                  state_fetch=_state(1, 10), proposal_log=ProposalLog(tmp_path / "v"), model_alias="v3",
                  candidate_mind=_FakeMind(exc=MindError("gateway 500")), candidate_alias="v4", split=0.2)
    assert await relay.poll_once() == "deferred"
    assert inc_seen == []                                                 # the incumbent was NOT asked instead


def test_config_from_env_candidate(monkeypatch, tmp_path):
    from grid.odin import RelayConfig
    monkeypatch.setenv("COLONY_SLUG", "colony-one")
    for k in ("MIND_LLM_CANDIDATE", "MIND_AB_SPLIT"):
        monkeypatch.delenv(k, raising=False)
    c = RelayConfig.from_env()
    assert c.candidate_tag == "" and c.ab_split == 0.2 and c.candidate_llm is None
    kf = tmp_path / "k"; kf.write_text("sk-x\n")
    monkeypatch.setenv("MIND_LLM_URL", "http://gw/v1"); monkeypatch.setenv("MIND_LLM_MODEL", "kannaka-brain-current")
    monkeypatch.setenv("MIND_LLM_KEY_FILE", str(kf)); monkeypatch.setenv("MIND_LLM_CANDIDATE", "kannaka-brain-v4"); monkeypatch.setenv("MIND_AB_SPLIT", "0.3")
    c = RelayConfig.from_env()
    assert c.candidate_tag == "kannaka-brain-v4" and c.ab_split == 0.3
    assert c.candidate_llm == DirectLlm("http://gw/v1", "kannaka-brain-v4", "sk-x")
```

(If the file's fake-mind helper does not accept `seen=`/`exc=`, extend that helper minimally: record asked prompts into `seen` and raise `exc` from `ask` when set. Do not add a second fake.)

- [ ] **Step 2: Run to verify failure** — `ImportError: arm_for` / `TypeError: unexpected keyword 'candidate_mind'`.

- [ ] **Step 3: Implement**

In `grid/odin.py`:

```python
import hashlib
AB_SPLIT = 0.2


def arm_for(request_id: str, split: float) -> str:
    """Deterministic per request: a deferred and retried request lands on the same arm,
    and the assignment is reproducible from the log alone (no RNG state)."""
    if split <= 0:
        return "incumbent"
    if split >= 1:
        return "candidate"
    h = int(hashlib.sha256(request_id.encode()).hexdigest()[:8], 16) / 2**32
    return "candidate" if h < split else "incumbent"
```

`Relay.__init__` gains `candidate_mind=None, candidate_alias: str = "", split: float = AB_SPLIT` stored as `self.candidate_mind`, `self.candidate_alias`, `self.split`. In `poll_once`, right before the recall/ask:

```python
        arm = "candidate" if self.candidate_mind is not None and arm_for(req.request_id, self.split) == "candidate" else "incumbent"
        mind = self.candidate_mind if arm == "candidate" else self.mind
        alias = self.candidate_alias if arm == "candidate" else self.model_alias
```

then use `mind.ask(...)` (recall stays on `self.mind` — the daemon's memory is shared), and pass `alias` to `_log` (give `_log` a `model_alias` parameter defaulting to `self.model_alias`; both call sites pass `alias`). The existing except-branch (`BusTimeout, BusError, MindError` → defer) is untouched, so a failed candidate ask defers.

`RelayConfig`: add fields `candidate_tag: str = ""`, `ab_split: float = AB_SPLIT`, `candidate_llm: DirectLlm | None = None`. In `from_env`:

```python
            candidate_tag=env("MIND_LLM_CANDIDATE", "") or "",
            ab_split=float(env("MIND_AB_SPLIT", str(AB_SPLIT))),
            candidate_llm=_candidate_llm(env),
```

with

```python
def _candidate_llm(env) -> DirectLlm | None:
    tag = env("MIND_LLM_CANDIDATE", "") or ""
    base = DirectLlm.from_env(env)
    if not tag or base is None:
        return None
    return DirectLlm(base.base_url, tag, base.api_key)
```

In `run()`: build `candidate_mind = Mind(cfg.agent_id, bus, kannaka_bin=cfg.kannaka_bin, data_dir=..., ask_timeout_s=cfg.ask_timeout_s, llm=cfg.candidate_llm) if cfg.candidate_llm else None` and pass `candidate_mind=candidate_mind, candidate_alias=cfg.candidate_tag, split=cfg.ab_split` to `Relay(...)`. Log one line at start: `A/B: candidate %s at split %.2f` or `A/B: none`.

- [ ] **Step 4: Run tests** → PASS; full suite green. **Step 5: Commit** — `feat(odin): second direct arm chosen deterministically per request (colony A/B)`.

---

### Task 2: A/B evaluation and ledger decisions

**Files:**
- Modify: `grid/versions.py`, `grid/weekly.py`
- Test: `tests/test_versions.py`, `tests/test_weekly.py`

**Interfaces:**
- Produces (in `grid/weekly.py`): constants `MIN_SAMPLE = 100`, `TOLERANCE = 0.10`, `MAX_WEEKS = 2`, `STUCK_AFTER_S = 48*3600`, `STUCK_INCUMBENT_MIN = 50`, `POSITIVE_TIERS`; `ab_arms(verdicts_dir, *, since_ts: int, incumbent: str, candidate: str) -> dict` (`{tag: {"settled", "reproduced", "rate", "births_per_proposal"}}` for both tags, zeros when absent — since runtime-and-prompt-loop Task 3, also `{"answered", "valid", "unanswered", "validity"}`, counted from the proposal log, and `ab_decide` reads them); `ab_decide(arms, *, incumbent, candidate, min_sample=MIN_SAMPLE, tolerance=TOLERANCE) -> tuple[str, str]` (`("win"|"loss"|"inconclusive", reason)`); `ab_state(versions) -> dict | None` (the running A/B: `{"candidate","incumbent","split","since_ts","weeks","started_row"}` or `None`); `lost_tags(versions) -> set[str]`; `is_stuck(arms, *, since_ts, now_ms, incumbent, candidate) -> bool`.
- Produces (in `grid/versions.py`): `DECISIONS += ("ab_started", "ab_inconclusive")`; `last_released()` unchanged (ignores them).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_versions.py — append
def test_ab_decisions_are_valid_and_not_release_state(tmp_path):
    from grid.versions import DECISIONS, Versions, release_row
    assert "ab_started" in DECISIONS and "ab_inconclusive" in DECISIONS
    v = Versions(tmp_path / "v.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="v3", previous=None, **kw))
    v.append(release_row(week="W2", decision="ab_started", version="v4", previous="v3", **kw))
    v.append(release_row(week="W3", decision="ab_inconclusive", version="v4", previous="v3", **kw))
    assert v.last_released()["version"] == "v3"
```

```python
# tests/test_weekly.py — append
import json, time
from grid.weekly import MIN_SAMPLE, TOLERANCE, ab_arms, ab_decide, ab_state, is_stuck, lost_tags


def _seed_ab(vdir, *, since_ts, inc="kannaka-brain-v3", cand="kannaka-brain-v4", n_inc=120, n_cand=110, inc_rep=30, cand_rep=25, before=5):
    """Proposals + verdicts for two arms; `before` extra candidate proposals stamped before since_ts (must be excluded)."""
    from grid.verdicts import Ledger
    (vdir / "prompts").mkdir(parents=True, exist_ok=True)
    led = Ledger(vdir / "verdicts.jsonl"); rows = []
    def add(i, alias, ts, tier, births):
        rid = f"{alias}-{i}-{ts}"
        rows.append({"ts": ts, "request_id": rid, "outcome": "proposed", "model_alias": alias, "parent_genome": ["a"], "prompt_path": f"prompts/{rid}.txt", "proposal": ["a", "nop"]})
        led.append({"request_id": rid, "model_alias": alias, "tier": tier, "births": births, "fitness": births})
    for i in range(n_inc): add(i, inc, since_ts + 1000 + i, "reproduced" if i < inc_rep else "new", 2 if i < inc_rep else 1)
    for i in range(n_cand): add(i, cand, since_ts + 1000 + i, "reproduced" if i < cand_rep else "new", 2 if i < cand_rep else 1)
    for i in range(before): add(i, cand, since_ts - 1000 - i, "reproduced", 5)                 # outside the window
    add(999, cand, since_ts + 5000, "preexisting", 40)                                            # excluded tier
    (vdir / "proposals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_ab_arms_windows_on_proposal_ts_and_excludes_unattributable_tiers(tmp_path):
    v = tmp_path / "v"; _seed_ab(v, since_ts=1_000_000)
    arms = ab_arms(v, since_ts=1_000_000, incumbent="kannaka-brain-v3", candidate="kannaka-brain-v4")
    assert arms["kannaka-brain-v3"] == {"settled": 120, "reproduced": 30, "rate": 0.25, "births_per_proposal": pytest.approx((30*2 + 90) / 120)}
    assert arms["kannaka-brain-v4"]["settled"] == 110 and arms["kannaka-brain-v4"]["reproduced"] == 25   # not 110+5+1
    arms2 = ab_arms(v, since_ts=1_000_000, incumbent="kannaka-brain-v3", candidate="kannaka-brain-v9")
    assert arms2["kannaka-brain-v9"] == {"settled": 0, "reproduced": 0, "rate": 0.0, "births_per_proposal": 0.0}


def test_ab_decide_boundaries():
    inc, cand = "v3", "v4"
    arms = lambda ri, si, rc, sc: {inc: {"settled": si, "reproduced": int(ri*si), "rate": ri, "births_per_proposal": 0}, cand: {"settled": sc, "reproduced": int(rc*sc), "rate": rc, "births_per_proposal": 0}}
    assert ab_decide(arms(0.25, 120, 0.225, 110), incumbent=inc, candidate=cand)[0] == "win"     # exactly rate_i*(1-0.10)
    assert ab_decide(arms(0.25, 120, 0.224, 110), incumbent=inc, candidate=cand)[0] == "loss"
    assert ab_decide(arms(0.25, 120, 0.9, 99), incumbent=inc, candidate=cand)[0] == "inconclusive"  # 99 < MIN_SAMPLE
    assert ab_decide(arms(0.25, 99, 0.9, 200), incumbent=inc, candidate=cand)[0] == "inconclusive"
    assert MIN_SAMPLE == 100 and TOLERANCE == 0.10


def test_ab_state_and_lost_tags(tmp_path):
    from grid.versions import Versions, release_row
    v = Versions(tmp_path / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    assert ab_state(v) is None
    v.append(release_row(week="W1", decision="released", version="v3", previous=None, evals={}, reason="", **kw))
    v.append(release_row(week="W2", decision="ab_started", version="v4", previous="v3", ts=2_000_000.0, reason="",
                         evals={"colony_ab": {"candidate": "v4", "incumbent": "v3", "split": 0.2, "since_ts": 2_000_000_000}}, **kw))
    s = ab_state(v)
    assert s["candidate"] == "v4" and s["incumbent"] == "v3" and s["since_ts"] == 2_000_000_000 and s["weeks"] == 0
    v.append(release_row(week="W3", decision="ab_inconclusive", version="v4", previous="v3", evals={}, reason="", **kw))
    assert ab_state(v)["weeks"] == 1
    v.append(release_row(week="W4", decision="held", version="v4", previous="v3", evals={}, reason="colony A/B lost: 0.1 vs 0.25", **kw))
    assert ab_state(v) is None and lost_tags(v) == {"v4"}
    v.append(release_row(week="W5", decision="held", version="v5", previous="v3", evals={}, reason="colony A/B stuck: candidate 0 settled in 48h (incumbent 60)", **kw))
    assert lost_tags(v) == {"v4", "v5"}


def test_is_stuck_rule():
    arms = lambda si, sc: {"v3": {"settled": si, "reproduced": 0, "rate": 0, "births_per_proposal": 0}, "v4": {"settled": sc, "reproduced": 0, "rate": 0, "births_per_proposal": 0}}
    t0 = 1_000_000_000
    assert is_stuck(arms(60, 0), since_ts=t0, now_ms=t0 + 49*3600*1000, incumbent="v3", candidate="v4") is True
    assert is_stuck(arms(60, 0), since_ts=t0, now_ms=t0 + 40*3600*1000, incumbent="v3", candidate="v4") is False   # too early
    assert is_stuck(arms(40, 0), since_ts=t0, now_ms=t0 + 49*3600*1000, incumbent="v3", candidate="v4") is False   # incumbent too thin to judge
    assert is_stuck(arms(60, 1), since_ts=t0, now_ms=t0 + 49*3600*1000, incumbent="v3", candidate="v4") is False   # candidate alive
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

`grid/versions.py`: `DECISIONS = ("released", "held", "rolled_back", "unchanged", "flip_incomplete", "ab_started", "ab_inconclusive")`.

`grid/weekly.py` (add near `colony_ab`):

```python
from .proposal_log import ProposalLog

AB_SPLIT = 0.2
MIN_SAMPLE = 100
TOLERANCE = 0.10
MAX_WEEKS = 2
STUCK_AFTER_S = 48 * 3600
STUCK_INCUMBENT_MIN = 50
POSITIVE_TIERS = ("reproduced", "growing", "enduring", "established")
EXCLUDED_TIERS = ("preexisting", "unknown", "rejected")


def ab_arms(verdicts_dir: Path, *, since_ts: int, incumbent: str, candidate: str) -> dict:
    """Per-arm colony outcome for proposals MADE since `since_ts` (the proposal's own ts,
    joined by request_id), so only asks that ran under the split count."""
    vdir = Path(verdicts_dir)
    entries = {e["request_id"]: e for e in ProposalLog(vdir).read_all()}
    acc = {t: {"settled": 0, "reproduced": 0, "births": 0} for t in (incumbent, candidate)}
    for row in Ledger(vdir / "verdicts.jsonl").rows():
        e = entries.get(row.get("request_id"))
        if e is None or int(e.get("ts", 0)) < since_ts or row.get("tier") in EXCLUDED_TIERS:
            continue
        a = acc.get(str(row.get("model_alias", "")))
        if a is None:
            continue
        a["settled"] += 1
        a["reproduced"] += 1 if row.get("tier") in POSITIVE_TIERS else 0
        a["births"] += int(row.get("births") or 0)
    return {t: {"settled": v["settled"], "reproduced": v["reproduced"],
                "rate": (v["reproduced"] / v["settled"]) if v["settled"] else 0.0,
                "births_per_proposal": (v["births"] / v["settled"]) if v["settled"] else 0.0}
            for t, v in acc.items()}


def ab_decide(arms: dict, *, incumbent: str, candidate: str, min_sample: int = MIN_SAMPLE,
              tolerance: float = TOLERANCE) -> tuple[str, str]:
    i, c = arms[incumbent], arms[candidate]
    if i["settled"] < min_sample or c["settled"] < min_sample:
        return "inconclusive", f"sample {c['settled']}/{i['settled']} below {min_sample} per arm"
    floor = i["rate"] * (1 - tolerance)
    if c["rate"] >= floor - 1e-12:
        return "win", f"candidate rate {c['rate']:.3f} >= {floor:.3f} (incumbent {i['rate']:.3f} x {1 - tolerance})"
    return "loss", f"colony A/B lost: {c['rate']:.3f} vs {i['rate']:.3f}"


def is_stuck(arms: dict, *, since_ts: int, now_ms: int, incumbent: str, candidate: str) -> bool:
    return (now_ms - since_ts >= STUCK_AFTER_S * 1000 and arms[candidate]["settled"] == 0
            and arms[incumbent]["settled"] >= STUCK_INCUMBENT_MIN)


def ab_state(versions: Versions) -> dict | None:
    """The running A/B: the last ab_started row not yet closed by a released/held/rolled_back row."""
    started, weeks = None, 0
    for r in versions.rows():
        if r["decision"] == "ab_started":
            started, weeks = r, 0
        elif started is not None and r["decision"] == "ab_inconclusive":
            weeks += 1
        elif started is not None and r["decision"] in ("released", "held", "rolled_back", "flip_incomplete"):
            started = None
    if started is None:
        return None
    ab = (started.get("evals") or {}).get("colony_ab") or {}
    return {"candidate": ab.get("candidate", started.get("version")), "incumbent": ab.get("incumbent", started.get("previous")),
            "split": ab.get("split", AB_SPLIT), "since_ts": int(ab.get("since_ts") or started["ts"] * 1000),
            "weeks": weeks, "started_row": started}


def lost_tags(versions: Versions) -> set[str]:
    return {r["version"] for r in versions.rows()
            if r["decision"] == "held" and str(r.get("reason", "")).startswith(("colony A/B lost", "colony A/B stuck"))
            and r.get("version")}
```

- [ ] **Step 4: Run tests** → PASS; suite green. **Step 5: Commit** — `feat(weekly): colony A/B arms, decision rule, state and stuck detection`.

---

### Task 3: The two-stage gate and the `ab` verb

**Files:**
- Modify: `grid/weekly.py`
- Test: `tests/test_weekly.py`

**Interfaces:**
- Produces: `start_ab(cfg, remote, *, candidate, incumbent, local_run, dry_run) -> dict` (env writes `MIND_LLM_CANDIDATE`, `MIND_AB_SPLIT`; relay restart; returns the `colony_ab` block with `since_ts = now_ms`); `end_ab(cfg, *, local_run, dry_run)` (clears `MIND_LLM_CANDIDATE`, relay restart); `release(...)` state machine per spec §4; `ab_check(cfg, *, now_ms, local_run, dry_run) -> dict | None` (the stuck check); CLI `python -m grid.weekly ab [--abort | --check | --forget <tag>]`; `flip()` also clears `MIND_LLM_CANDIDATE` in its env write.
- Consumes: Task 2's functions; existing `flip`, `_env_set`/`_env_get`, `_ledger`, `release_row`, `Versions`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_weekly.py`; reuse `_cfg`, `FakeRemote`, `MANIFEST`, `ROGUE_LEDGER`, `_seed_one_proposal`, `_seed_ab`)

```python
async def test_release_starts_an_ab_instead_of_flipping(tmp_path):
    cfg = _cfg(tmp_path); _seed_one_proposal(tmp_path / "v")
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def her(p): return '["harvest", "nop", "fork"]' if "Instruction set" in p else "I am Kannaka."
    restarts = []
    row = await release(cfg, r, ask_factory=lambda m: her, local_run=lambda c: restarts.append(c) or 0, week="W1")
    assert row["decision"] == "ab_started" and row["version"] == "kannaka-brain-v4"
    assert not any(c.startswith("ollama cp") for c in r.commands)                      # no flip
    env = (tmp_path / "grid.env").read_text()
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" in env and "MIND_AB_SPLIT=0.2" in env and "MIND_MODEL_ALIAS=kannaka-brain-v3" in env
    assert restarts and "grid-odin" in " ".join(restarts[0])                             # relay restarted
    ab = row["evals"]["colony_ab"]
    assert ab["candidate"] == "kannaka-brain-v4" and ab["incumbent"] == "kannaka-brain-v3" and ab["since_ts"] > 0


async def _run_ab_week(tmp_path, *, inc_rep, cand_rep, n_cand=110):
    cfg = _cfg(tmp_path); v = tmp_path / "v"
    from grid.versions import Versions, release_row
    since = 1_000_000_000
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W0", decision="released", version="kannaka-brain-v3", previous=None, evals={}, reason="", **kw))
    vs.append(release_row(week="W1", decision="ab_started", version="kannaka-brain-v4", previous="kannaka-brain-v3", evals={"colony_ab": {"candidate": "kannaka-brain-v4", "incumbent": "kannaka-brain-v3", "split": 0.2, "since_ts": since}}, reason="", **kw))
    _seed_ab(v, since_ts=since, inc_rep=inc_rep, cand_rep=cand_rep, n_cand=n_cand)
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_MODEL=kannaka-brain-current\nMIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    restarts = []
    async def never(p): raise AssertionError("probes must not run during an A/B week")
    row = await release(cfg, r, ask_factory=lambda m: never, local_run=lambda c: restarts.append(c) or 0, week="W2")
    return row, r, (tmp_path / "grid.env").read_text(), restarts


async def test_ab_win_flips_and_clears_the_candidate(tmp_path):
    row, r, env, restarts = await _run_ab_week(tmp_path, inc_rep=30, cand_rep=28)          # 0.255 >= 0.225
    assert row["decision"] == "released" and row["evals"]["colony_ab"]["binding"] is True and row["evals"]["colony_ab"]["winner"] == "kannaka-brain-v4"
    assert "ollama cp kannaka-brain-v4 kannaka-brain-current" in r.commands
    assert "MIND_MODEL_ALIAS=kannaka-brain-v4" in env and "MIND_LLM_CANDIDATE=" not in env.replace("MIND_LLM_CANDIDATE=\n", "")  # cleared


async def test_ab_loss_holds_and_never_releases(tmp_path):
    row, r, env, restarts = await _run_ab_week(tmp_path, inc_rep=30, cand_rep=10)          # 0.09 < 0.225
    assert row["decision"] == "held" and row["reason"].startswith("colony A/B lost")
    assert not any(c.startswith("ollama cp") for c in r.commands)
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in env and "MIND_LLM_CANDIDATE=kannaka-brain-v4" not in env and restarts


async def test_ab_inconclusive_continues_then_holds_at_max_weeks(tmp_path):
    row, r, env, restarts = await _run_ab_week(tmp_path, inc_rep=30, cand_rep=20, n_cand=50)  # candidate < MIN_SAMPLE
    assert row["decision"] == "ab_inconclusive" and "MIND_LLM_CANDIDATE=kannaka-brain-v4" in env and not restarts
    # a second inconclusive week hits MAX_WEEKS -> held, candidate cleared
    from grid.versions import Versions
    cfg = _cfg(tmp_path)
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_MODEL=kannaka-brain-current\nMIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    r2 = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                     "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def never(p): raise AssertionError
    row2 = await release(cfg, r2, ask_factory=lambda m: never, local_run=lambda c: 0, week="W3")
    assert row2["decision"] == "held" and "inconclusive after 2 weeks" in row2["reason"]
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" not in (tmp_path / "grid.env").read_text()


async def test_lost_tag_is_not_retested_until_forgotten(tmp_path):
    cfg = _cfg(tmp_path); _seed_one_proposal(tmp_path / "v")
    from grid.versions import Versions, release_row
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W0", decision="released", version="kannaka-brain-v3", previous=None, reason="", **kw))
    vs.append(release_row(week="W1", decision="held", version="kannaka-brain-v4", previous="kannaka-brain-v3", reason="colony A/B lost: 0.1 vs 0.3", **kw))
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def never(p): raise AssertionError
    row = await release(cfg, r, ask_factory=lambda m: never, local_run=lambda c: 0, week="W2")
    assert row["decision"] == "held" and "already lost" in row["reason"]
    from grid.weekly import ab_forget
    ab_forget(cfg, "kannaka-brain-v4")
    assert lost_tags(vs) == set()


def test_ab_check_aborts_a_stuck_ab(tmp_path):
    from grid.weekly import ab_check
    from grid.versions import Versions, release_row
    cfg = _cfg(tmp_path); v = tmp_path / "v"; since = 1_000_000_000
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W1", decision="ab_started", version="kannaka-brain-v4", previous="kannaka-brain-v3", evals={"colony_ab": {"candidate": "kannaka-brain-v4", "incumbent": "kannaka-brain-v3", "split": 0.2, "since_ts": since}}, reason="", **kw))
    _seed_ab(v, since_ts=since, n_inc=60, n_cand=0, inc_rep=10, cand_rep=0, before=0)
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    restarts = []
    assert ab_check(cfg, now_ms=since + 40*3600*1000, local_run=lambda c: restarts.append(c) or 0) is None      # too early
    row = ab_check(cfg, now_ms=since + 49*3600*1000, local_run=lambda c: restarts.append(c) or 0)
    assert row["decision"] == "held" and row["reason"].startswith("colony A/B stuck") and restarts
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" not in (tmp_path / "grid.env").read_text() and ab_state(vs) is None


async def test_dry_run_ab_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path); _seed_one_proposal(tmp_path / "v")
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def her(p): return '["harvest", "nop", "fork"]' if "Instruction set" in p else "I am Kannaka."
    before = (tmp_path / "grid.env").read_text()
    row = await release(cfg, r, ask_factory=lambda m: her, local_run=lambda c: (_ for _ in ()).throw(AssertionError()), week="W1", dry_run=True)
    assert row["decision"] == "ab_started" and (tmp_path / "grid.env").read_text() == before
    from grid.versions import Versions
    assert Versions(cfg.releases_dir / "versions.jsonl").rows() == []
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** in `grid/weekly.py`:

```python
def _restart_relay(local_run) -> int:
    return local_run(["sudo", "systemctl", "restart", "grid-odin.service"])


def start_ab(cfg: WeeklyConfig, *, candidate: str, incumbent: str, local_run, dry_run: bool = False, now_ms: int | None = None) -> dict:
    since = now_ms if now_ms is not None else int(time.time() * 1000)
    block = {"binding": True, "candidate": candidate, "incumbent": incumbent, "split": AB_SPLIT, "since_ts": since,
             "min_sample": MIN_SAMPLE, "tolerance": TOLERANCE, "weeks": 0, "arms": {}, "winner": None}
    if not dry_run:
        _env_set(cfg.grid_env, "MIND_LLM_CANDIDATE", candidate)
        _env_set(cfg.grid_env, "MIND_AB_SPLIT", str(AB_SPLIT))
        _restart_relay(local_run)
    return block


def end_ab(cfg: WeeklyConfig, *, local_run, dry_run: bool = False) -> None:
    if not dry_run:
        _env_set(cfg.grid_env, "MIND_LLM_CANDIDATE", "")
        _restart_relay(local_run)
```

`flip()`: in the same place it writes `MIND_MODEL_ALIAS`, also `_env_set(cfg.grid_env, "MIND_LLM_CANDIDATE", "")` (the relay restart it already does picks both up).

`release()` — restructure the body after `cand = find_candidate(...)`:

```python
    versions = Versions(...)  # as today
    state = ab_state(versions)
    if state is not None:
        # An A/B is running: this Sunday is the colony's verdict, not a probe day.
        arms = ab_arms(cfg.verdicts_dir, since_ts=state["since_ts"], incumbent=state["incumbent"], candidate=state["candidate"])
        verdict, reason = ab_decide(arms, incumbent=state["incumbent"], candidate=state["candidate"])
        block = dict(state["started_row"]["evals"]["colony_ab"], arms=arms, weeks=state["weeks"] + 1, winner=None)
        common = dict(week=week, base="", run="", grid_examples={}, verdict_window=_window(cfg.verdicts_dir), rogue_ledger_ts=0.0,
                      adapter_sha256="", gguf_sha256="", set_manifest_sha256="", spend_usd=0.0)
        if verdict == "inconclusive" and state["weeks"] + 1 >= MAX_WEEKS:
            end_ab(cfg, local_run=local_run, dry_run=dry_run)
            row = release_row(decision="held", version=state["candidate"], previous=released, evals={"colony_ab": block},
                              reason=f"colony A/B inconclusive after {MAX_WEEKS} weeks: {reason}", **common)
        elif verdict == "inconclusive":
            row = release_row(decision="ab_inconclusive", version=state["candidate"], previous=released, evals={"colony_ab": block}, reason=reason, **common)
        elif verdict == "win":
            block["winner"] = state["candidate"]
            outcome = flip(cfg, remote, state["candidate"], local_run=local_run, dry_run=dry_run)   # flip clears the candidate too
            decision, why = ("released", reason) if outcome == "ok" else (("held", "flip failed: tag copy") if outcome == "flip_failed" else ("flip_incomplete", "alias serves the candidate; relay restart failed twice"))
            row = release_row(decision=decision, version=state["candidate"], previous=released, evals={"colony_ab": block}, reason=why, **common)
        else:
            end_ab(cfg, local_run=local_run, dry_run=dry_run)
            row = release_row(decision="held", version=state["candidate"], previous=released, evals={"colony_ab": block}, reason=reason, **common)
        return _ledger(versions, row, dry_run=dry_run)
```

Then the existing no-candidate / unchanged / key-check / probes path runs as today, except: (a) before the probes, if `cand["tag"] in lost_tags(versions)` → `held`, reason `f"{cand['tag']} already lost/stuck the colony A/B; run `grid.weekly ab --forget {tag}` to retest"`; (b) where today `decision == "released"` triggers `flip(...)`, instead call `start_ab(cfg, candidate=cand["tag"], incumbent=released or cfg-derived incumbent label, local_run=..., dry_run=...)` and record `decision="ab_started"`, `evals["colony_ab"] = block`, `reason="probes passed; colony A/B started"`. (The incumbent label is `_env_get(cfg.grid_env, "MIND_MODEL_ALIAS")` — the concrete version the relay labels today — falling back to `released`.) If there is no released version yet (`released is None`, first ever): still start the A/B against the current label; the flip on a win establishes the first `released` row.

```python
def ab_check(cfg: WeeklyConfig, *, now_ms: int | None = None, local_run, dry_run: bool = False) -> dict | None:
    """The daily stuck check (spec §8): abort a dead A/B within 48 h instead of waiting for Sunday."""
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    state = ab_state(versions)
    if state is None:
        return None
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    arms = ab_arms(cfg.verdicts_dir, since_ts=state["since_ts"], incumbent=state["incumbent"], candidate=state["candidate"])
    if not is_stuck(arms, since_ts=state["since_ts"], now_ms=now, incumbent=state["incumbent"], candidate=state["candidate"]):
        return None
    end_ab(cfg, local_run=local_run, dry_run=dry_run)
    block = dict(state["started_row"]["evals"]["colony_ab"], arms=arms, weeks=state["weeks"], winner=None)
    row = release_row(week=_iso_week(), decision="held", version=state["candidate"], previous=versions.last_released()["version"] if versions.last_released() else None,
                      base="", run="", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window=_window(cfg.verdicts_dir),
                      evals={"colony_ab": block}, spend_usd=0.0, rogue_ledger_ts=0.0,
                      reason=f"colony A/B stuck: candidate 0 settled in 48h (incumbent {arms[state['incumbent']]['settled']})")
    return _ledger(versions, row, dry_run=dry_run)


def ab_forget(cfg: WeeklyConfig, tag: str, *, dry_run: bool = False) -> dict:
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    row = release_row(week=_iso_week(), decision="unchanged", version=tag, previous=None, base="", run="", adapter_sha256="", gguf_sha256="",
                      set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0.0, rogue_ledger_ts=0.0,
                      reason=f"operator forgot the colony A/B loss of {tag}")
    return _ledger(versions, row, dry_run=dry_run)
```

`lost_tags` must then ignore a lost tag that has a later `unchanged` row whose reason starts `operator forgot`: implement as "a tag is lost iff its most recent lost/stuck `held` row comes after its most recent forget row" (update Task 2's `lost_tags` accordingly — same commit is fine; keep the Task 2 test passing).

CLI: `verb` choices gain `"ab"`; args `--abort`, `--check`, `--forget <tag>`. `ab` with no flag prints `ab_state` (+ live `ab_arms` when running); `--check` runs `ab_check` and prints the row or `no A/B running` / `not stuck`; `--abort` runs `end_ab` and ledgers `held: colony A/B aborted by operator` (version = the candidate); `--forget TAG` runs `ab_forget`. `rollback` also calls `end_ab` before its flip. `status` output gains `"ab": ab_state(...)`.

- [ ] **Step 4: Run tests** → PASS; suite green. **Step 5: Commit** — `feat(weekly): two-stage release gate with a binding colony A/B, stuck check, ab verb`.

---

### Task 4: Deploy — nightly stuck check, env, README

**Files:**
- Modify: `deploy/systemd/grid-verdicts.service`, `deploy/grid.env.example`, `deploy/README.md`
- Test: `tests/test_deploy_units.py`

- [ ] **Step 1: Failing test**

```python
def test_verdicts_unit_runs_the_ab_stuck_check_after_settling():
    svc = unit("grid-verdicts.service")
    assert "ExecStartPost=/srv/grid/venv/bin/python -m grid.weekly ab --check" in svc
```

- [ ] **Step 2: Implement.** Add to `[Service]` in `grid-verdicts.service`, after `ExecStart=`:
  `ExecStartPost=/srv/grid/venv/bin/python -m grid.weekly ab --check` (a oneshot runs it after ExecStart succeeds; it reads `grid.env` via the same `EnvironmentFile`; `--check` exits 0 when no A/B is running). In `grid.env.example` add commented `#MIND_LLM_CANDIDATE=` and `#MIND_AB_SPLIT=0.2` with a two-line explanation (written by the gate; empty = no A/B). In `deploy/README.md` §5 add a short "Colony A/B" block: what `ab_started`/`ab_inconclusive`/win/loss/stuck mean, the two-Sunday cadence, the nightly `ab --check`, `grid.weekly ab`, `ab --abort`, `ab --forget <tag>`, and the receipt: a hand-started A/B with the base `kannaka-brain` as candidate at split 0.2 for a few hours (`MIND_LLM_CANDIDATE=kannaka-brain` + relay restart), the proposal log showing ≈ 20% `kannaka-brain` labels, `grid.weekly ab` printing both arms, then `ab --abort`.

- [ ] **Step 3: Tests pass; commit** — `deploy(skywave): nightly colony A/B stuck check; env and README for the A/B`.

---

## Self-review notes

- Spec coverage: §3 relay → Task 1; §4 gate state machine + §8 stuck/forget → Tasks 2–3; §5 ledger → Task 2 (`DECISIONS`) + Task 3 (`colony_ab` block shape); §6 dry-run → Task 3 test; §7 tests listed → spread across tasks; the receipt → Task 4 README (run by the controller after merge).
- Cross-task names: `arm_for`/`AB_SPLIT` (T1); `ab_arms`/`ab_decide`/`ab_state`/`lost_tags`/`is_stuck` + constants (T2 → T3); `start_ab`/`end_ab`/`ab_check`/`ab_forget` (T3 → T4 unit + README). `flip` clears the candidate (T3) so a win never leaves a stale `MIND_LLM_CANDIDATE`.
- Deliberate: the window is the proposal's `ts` (not a settle time — verdict rows carry only ticks), which is also the semantically right window. `lost_tags` needs the forget-after-loss ordering (noted in T3). The incumbent label during an A/B is the existing `MIND_MODEL_ALIAS`, unchanged by `start_ab`.
- Soft spots for the reviewer: the first-ever release (no `released` row yet) still goes through an A/B; `ab_state` treats `flip_incomplete` as closing the A/B (the alias already moved); `ExecStartPost` runs only if the verdicts run succeeds (a failed settle skips the stuck check that night — the Sunday gate is the backstop).
