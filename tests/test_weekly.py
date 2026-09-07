import json
from pathlib import Path

import pytest

from grid.weekly import WeeklyConfig, ab_state, colony_ab, find_candidate, flip, lost_tags, release, rollback
from grid.mind import MindError


class FakeRemote:
    def __init__(self, files: dict[str, str], fail: set[str] = frozenset()):
        self.files, self.fail, self.commands = files, set(fail), []
    def run(self, cmd, *, timeout=120):
        self.commands.append(cmd)
        from grid.remote import RemoteResult
        if any(cmd.startswith(f) for f in self.fail):
            return RemoteResult(1, "", "boom")
        if cmd.startswith("cat "):
            path = cmd.split(" ", 1)[1].strip("'")
            return RemoteResult(0, self.files.get(path, ""), "" if path in self.files else "missing")
        if cmd.startswith("sha256sum "):
            return RemoteResult(0, "e" * 64 + "  x\n", "")
        if cmd.startswith("ollama show"):
            return RemoteResult(0, "FROM /srv/kax/brains/kannaka-brain-v4.gguf\n", "")
        return RemoteResult(0, "", "")
    def read(self, path):
        from grid.remote import RemoteError
        if path not in self.files: raise RemoteError(path)
        return self.files[path]


def _cfg(tmp_path):
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_MODEL=kannaka-brain-current\n")
    (tmp_path / "key").write_text("k\n")
    return WeeklyConfig(debain2_host="debain2", releases_dir=tmp_path / "releases", rogue_current_model="/srv/rogue/current-model",
                        rogue_ledger="/srv/rogue/ledger.jsonl", rogue_runs_dir="/home/nick/.kannaka-corpus/runs",
                        gateway_url="http://gw/v1", key_file=tmp_path / "key", verdicts_dir=tmp_path / "v",
                        grid_env=tmp_path / "grid.env", current_alias="kannaka-brain-current",
                        probe_timeout_s=5, identity_runs=2)


MANIFEST = json.dumps({"base": "Qwen/Qwen2.5-14B-Instruct", "holdout_ppl": {"before": 104.4, "after": 4.0}, "seconds": 781,
                       "served_as": "kannaka-brain-v4", "promoted_at": 1.7e9})
ROGUE_LEDGER = json.dumps({"ts": 1.7e9, "event": "promotion", "model": "kannaka-brain-v4", "run": "gpu-a100-sxm-20260913-0305"}) + "\n"

# run_validity (grid.weekly_probe, Task 3) needs at least one prior proposal to score
# against; with none, `pass` is vacuously False. Seed exactly one so the "her" candidate
# below can actually clear the validity gate.
from grid.proposal import Request, render_prompt

_ISA = [{"name": "harvest", "cost": 1, "effect": "eat"}, {"name": "nop", "cost": 0, "effect": ""},
        {"name": "fork", "cost": 3, "effect": "split"}]
_PROMPT_REQ = Request(request_id="p1", created_at=0, parent={}, genome=["harvest", "fork"],
                      instruction_set=_ISA, ecology={"season": "bloom"}, directive="Author one motivated variant.")


def _seed_one_proposal(vdir: Path) -> None:
    (vdir / "prompts").mkdir(parents=True, exist_ok=True)
    (vdir / "prompts" / "p1.txt").write_text(render_prompt(_PROMPT_REQ, []))
    (vdir / "proposals.jsonl").write_text(json.dumps({"ts": 1000, "request_id": "p1", "outcome": "proposed",
                                                       "parent_genome": ["harvest", "fork"], "prompt_path": "prompts/p1.txt",
                                                       "model_alias": "kannaka-brain-v3", "proposal": ["harvest", "nop", "fork"]}) + "\n")


def test_find_candidate_reads_current_model_and_promotion_row(tmp_path):
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    c = find_candidate(r, _cfg(tmp_path))
    assert c["tag"] == "kannaka-brain-v4" and c["run"] == "gpu-a100-sxm-20260913-0305" and c["manifest"]["holdout_ppl"]["after"] == 4.0
    assert find_candidate(FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n"}), _cfg(tmp_path)) is None


def test_find_candidate_reads_the_manifest_when_run_is_an_absolute_path(tmp_path):
    # rogue.weekly's ledger records `run` as a FULL path (verified on debain2), not a bare name.
    abs_run = "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305"
    ledger = json.dumps({"ts": 1.7e9, "event": "promotion", "model": "kannaka-brain-v4", "run": abs_run}) + "\n"
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ledger,
                    f"{abs_run}/train.manifest.json": MANIFEST})
    c = find_candidate(r, _cfg(tmp_path))
    assert c["run"] == abs_run and c["manifest"]["holdout_ppl"]["after"] == 4.0   # not a doubled path


def test_colony_ab_reports_births_per_proposal_by_alias(tmp_path):
    v = tmp_path / "v"; v.mkdir()
    rows = [{"model_alias": "kannaka-brain-v3", "tier": "reproduced", "births": 2, "fitness": 2},
            {"model_alias": "kannaka-brain-v3", "tier": "unborn", "births": 0, "fitness": 0},
            {"model_alias": "agent-brain", "tier": "new", "births": 1, "fitness": 1}]
    from grid.verdicts import Ledger
    led = Ledger(v / "verdicts.jsonl")
    for i, row in enumerate(rows): led.append(dict(row, request_id=f"r{i}"))
    ab = colony_ab(v)
    assert ab["advisory"] is True
    assert ab["by_alias"]["kannaka-brain-v3"] == {"proposals": 2, "births_per_proposal": 1.0, "reproduced_rate": 0.5}


def test_flip_order_and_revert_on_failed_tag_copy(tmp_path):
    cfg = _cfg(tmp_path); local = []
    r = FakeRemote({}, fail={"ollama cp"})
    assert flip(cfg, r, "kannaka-brain-v4", local_run=lambda cmd: local.append(cmd) or 0) == "flip_failed"
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in (tmp_path / "grid.env").read_text()   # reverted
    assert local == []                                                                   # relay never restarted
    r = FakeRemote({})
    assert flip(cfg, r, "kannaka-brain-v4", local_run=lambda cmd: local.append(cmd) or 0) == "ok"
    assert r.commands == ["ollama cp kannaka-brain-v4 kannaka-brain-current"]
    assert "MIND_MODEL_ALIAS=kannaka-brain-v4" in (tmp_path / "grid.env").read_text()
    assert local == [["sudo", "-n", "systemctl", "restart", "grid-odin.service"]]


async def test_release_holds_on_probe_failure_and_starts_ab_on_pass(tmp_path):
    cfg = _cfg(tmp_path); _seed_one_proposal(tmp_path / "v")
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def qwen(prompt): return "I am Qwen."
    row = await release(cfg, r, ask_factory=lambda model: qwen, local_run=lambda cmd: 0, week="2026-W37")
    assert row["decision"] == "held" and row["reason"] == "identity probe failed"
    assert not any(c.startswith("ollama cp") for c in r.commands)
    async def her(prompt): return '["harvest", "nop", "fork"]' if "Instruction set" in prompt else "I am Kannaka."
    row = await release(cfg, r, ask_factory=lambda model: her, local_run=lambda cmd: 0, week="2026-W37")
    assert row["decision"] == "ab_started" and row["version"] == "kannaka-brain-v4" and row["previous"] is None
    assert row["adapter_sha256"] == "e" * 64 and row["spend_usd"] == pytest.approx(781 / 3600 * 2.49, rel=0.01)
    from grid.versions import Versions
    assert Versions(cfg.releases_dir / "versions.jsonl").verify() is True
    # a colony A/B is now running: the next Sunday is a verdict day, not another probe
    row = await release(cfg, r, ask_factory=lambda model: her, local_run=lambda cmd: 0, week="2026-W37")
    assert row["decision"] == "ab_inconclusive"


async def test_release_uses_the_configured_identity_runs(tmp_path):
    import dataclasses
    from grid.weekly_probe import IDENTITY_QUESTIONS
    cfg = dataclasses.replace(_cfg(tmp_path), identity_runs=3)
    _seed_one_proposal(tmp_path / "v")
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    identity_asks = []

    async def her(prompt):
        if "Instruction set" in prompt:
            return '["harvest", "nop", "fork"]'
        identity_asks.append(prompt)
        return "I am Kannaka."
    row = await release(cfg, r, ask_factory=lambda model: her, local_run=lambda cmd: 0, week="2026-W37")
    assert row["decision"] == "ab_started"
    assert len(identity_asks) == 3 * len(IDENTITY_QUESTIONS)   # 3 runs, not the old 10
    assert row["evals"]["identity"]["total"] == 3 * len(IDENTITY_QUESTIONS)


def test_config_from_env_identity_runs_defaults_to_three(monkeypatch):
    for k in ("COLONY_SLUG", "IDENTITY_PROBE_RUNS"):
        monkeypatch.delenv(k, raising=False)
    assert WeeklyConfig.from_env().identity_runs == 3
    monkeypatch.setenv("IDENTITY_PROBE_RUNS", "7")
    assert WeeklyConfig.from_env().identity_runs == 7


async def test_release_holds_when_identity_probe_errors(tmp_path):
    # C1: run_identity/run_validity propagate MindError (gateway non-200, timeout, malformed
    # or empty completion) with no try/except around them in release() -> uncaught out of
    # asyncio.run -> no ledger row. Mutation: remove the try/except around the two awaits in
    # grid/weekly.py release() -> this test raises MindError instead of asserting "held".
    cfg = _cfg(tmp_path); _seed_one_proposal(tmp_path / "v")
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def broken(prompt): raise MindError("probe: gateway-01: empty answer")
    row = await release(cfg, r, ask_factory=lambda model: broken, local_run=lambda cmd: 0, week="2026-W37")
    assert row["decision"] == "held"
    assert "empty answer" in row["reason"]
    assert not any(c.startswith("ollama cp") for c in r.commands)
    from grid.versions import Versions
    v = Versions(cfg.releases_dir / "versions.jsonl")
    assert len(v.rows()) == 1 and v.rows()[0]["decision"] == "held"
    assert v.verify() is True


async def test_release_holds_when_candidate_not_on_key(tmp_path):
    # I1: the probes ask cand["tag"] directly; if the colony key was minted before this vN
    # existed the gateway would reject the ask. Verify first via the injected key_models.
    # Mutation: remove the key_models check (or the `held` early-return) in release() -> this
    # test's asks would be invoked (AssertionError from the fake ask) instead of holding.
    cfg = _cfg(tmp_path); _seed_one_proposal(tmp_path / "v")
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    def fake_key_models(cfg, remote): return ["kannaka-brain-current", "kannaka-brain-v3"]
    async def must_not_be_called(prompt): raise AssertionError("probe must not run when the candidate is off the key")
    row = await release(cfg, r, ask_factory=lambda model: must_not_be_called, local_run=lambda cmd: 0,
                        week="2026-W37", key_models=fake_key_models)
    assert row["decision"] == "held" and "not on the colony key" in row["reason"]
    assert not any(c.startswith("ollama cp") for c in r.commands)


async def test_rollback_picks_previous_release(tmp_path):
    cfg = _cfg(tmp_path)
    from grid.versions import Versions, release_row
    v = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="kannaka-brain-v3", previous=None, **kw))
    v.append(release_row(week="W2", decision="released", version="kannaka-brain-v4", previous="kannaka-brain-v3", **kw))
    r = FakeRemote({})
    row = rollback(cfg, r, local_run=lambda cmd: 0)
    assert row["decision"] == "rolled_back" and row["version"] == "kannaka-brain-v3" and row["previous"] == "kannaka-brain-v4"
    assert r.commands == ["ollama cp kannaka-brain-v3 kannaka-brain-current"]
    with pytest.raises(SystemExit):
        rollback(cfg, r, to="kannaka-brain-v9", local_run=lambda cmd: 0)     # never released: refuse

    # I2: after release v3, release v4, rollback -> v3 (above), release v5, the default
    # rollback target must be v3 (last["previous"]) -- not v4, which
    # previous_released(before=...) would return by walking release-order and ignoring the
    # intervening rolled_back row. Mutation: revert the default-target line in rollback() to
    # `versions.previous_released(before=last["version"])` -> this assertion fails (picks v4).
    v.append(release_row(week="W3", decision="released", version="kannaka-brain-v5", previous="kannaka-brain-v3", **kw))
    row = rollback(cfg, r, local_run=lambda cmd: 0)
    assert row["version"] == "kannaka-brain-v3" and row["previous"] == "kannaka-brain-v5"


async def test_dry_run_release_and_rollback_write_no_ledger_row(tmp_path):
    cfg = _cfg(tmp_path); _seed_one_proposal(tmp_path / "v")
    grid_env_before = cfg.grid_env.read_text()
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def her(prompt): return '["harvest", "nop", "fork"]' if "Instruction set" in prompt else "I am Kannaka."
    row = await release(cfg, r, ask_factory=lambda model: her, local_run=lambda cmd: (_ for _ in ()).throw(AssertionError("local_run must not run for real under dry_run")), week="2026-W37", dry_run=True)
    assert row["decision"] == "ab_started"
    from grid.versions import Versions
    assert Versions(cfg.releases_dir / "versions.jsonl").rows() == []
    assert cfg.grid_env.read_text() == grid_env_before   # start_ab's env write must not happen under dry_run either

    from grid.versions import release_row
    v = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="kannaka-brain-v3", previous=None, **kw))
    v.append(release_row(week="W2", decision="released", version="kannaka-brain-v4", previous="kannaka-brain-v3", **kw))
    grid_env_before = cfg.grid_env.read_text()
    row2 = rollback(cfg, FakeRemote({}), local_run=lambda cmd: (_ for _ in ()).throw(AssertionError("local_run must not run for real under dry_run")), dry_run=True)
    assert row2["decision"] == "rolled_back"
    assert len(Versions(cfg.releases_dir / "versions.jsonl").rows()) == 2   # unchanged: dry-run rollback appended nothing
    assert cfg.grid_env.read_text() == grid_env_before   # flip's env write must not happen under dry_run either


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
    from grid.weekly import ab_arms
    v = tmp_path / "v"; _seed_ab(v, since_ts=1_000_000)
    arms = ab_arms(v, since_ts=1_000_000, incumbent="kannaka-brain-v3", candidate="kannaka-brain-v4")
    assert arms["kannaka-brain-v3"] == {"settled": 120, "reproduced": 30, "rate": 0.25, "births_per_proposal": pytest.approx((30*2 + 90) / 120)}
    assert arms["kannaka-brain-v4"]["settled"] == 110 and arms["kannaka-brain-v4"]["reproduced"] == 25   # not 110+5+1
    arms2 = ab_arms(v, since_ts=1_000_000, incumbent="kannaka-brain-v3", candidate="kannaka-brain-v9")
    assert arms2["kannaka-brain-v9"] == {"settled": 0, "reproduced": 0, "rate": 0.0, "births_per_proposal": 0.0}


def test_ab_decide_boundaries():
    from grid.weekly import MIN_SAMPLE, TOLERANCE, ab_decide
    inc, cand = "v3", "v4"
    arms = lambda ri, si, rc, sc: {inc: {"settled": si, "reproduced": int(ri*si), "rate": ri, "births_per_proposal": 0}, cand: {"settled": sc, "reproduced": int(rc*sc), "rate": rc, "births_per_proposal": 0}}
    assert ab_decide(arms(0.25, 120, 0.225, 110), incumbent=inc, candidate=cand)[0] == "win"     # exactly rate_i*(1-0.10)
    assert ab_decide(arms(0.25, 120, 0.224, 110), incumbent=inc, candidate=cand)[0] == "loss"
    assert ab_decide(arms(0.25, 120, 0.9, 99), incumbent=inc, candidate=cand)[0] == "inconclusive"  # 99 < MIN_SAMPLE
    assert ab_decide(arms(0.25, 99, 0.9, 200), incumbent=inc, candidate=cand)[0] == "inconclusive"
    assert MIN_SAMPLE == 100 and TOLERANCE == 0.10


def test_ab_state_and_lost_tags(tmp_path):
    from grid.versions import Versions, release_row
    from grid.weekly import ab_state, lost_tags
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
    from grid.weekly import is_stuck
    arms = lambda si, sc: {"v3": {"settled": si, "reproduced": 0, "rate": 0, "births_per_proposal": 0}, "v4": {"settled": sc, "reproduced": 0, "rate": 0, "births_per_proposal": 0}}
    t0 = 1_000_000_000
    assert is_stuck(arms(60, 0), since_ts=t0, now_ms=t0 + 49*3600*1000, incumbent="v3", candidate="v4") is True
    assert is_stuck(arms(60, 0), since_ts=t0, now_ms=t0 + 40*3600*1000, incumbent="v3", candidate="v4") is False   # too early
    assert is_stuck(arms(40, 0), since_ts=t0, now_ms=t0 + 49*3600*1000, incumbent="v3", candidate="v4") is False   # incumbent too thin to judge
    assert is_stuck(arms(60, 1), since_ts=t0, now_ms=t0 + 49*3600*1000, incumbent="v3", candidate="v4") is False   # candidate alive


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
