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
    # C1: a stray candidate must survive a failed flip (the relay is never restarted, so
    # it keeps running whatever it last loaded) rather than being cleared with no way back.
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_MODEL=kannaka-brain-current\nMIND_LLM_CANDIDATE=kannaka-brain-v9\n")
    r = FakeRemote({}, fail={"ollama cp"})
    assert flip(cfg, r, "kannaka-brain-v4", local_run=lambda cmd: local.append(cmd) or 0) == "flip_failed"
    env = (tmp_path / "grid.env").read_text()
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in env   # reverted
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v9" in env   # C1: restored, not left cleared
    assert local == []                                                                   # relay never restarted
    r = FakeRemote({})
    assert flip(cfg, r, "kannaka-brain-v4", local_run=lambda cmd: local.append(cmd) or 0) == "ok"
    assert r.commands == ["ollama cp kannaka-brain-v4 kannaka-brain-current"]
    env = (tmp_path / "grid.env").read_text()
    assert "MIND_MODEL_ALIAS=kannaka-brain-v4" in env
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v9" not in env   # a successful flip still clears it
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


def _seed_ab(vdir, *, since_ts, inc="kannaka-brain-v3", cand="kannaka-brain-v4", n_inc=120, n_cand=110, inc_rep=30, cand_rep=25, before=5,
             inc_rejected=(0, 0), cand_rejected=(0, 0), excluded=1):
    """Proposals + verdicts for two arms; `before` extra candidate proposals stamped before since_ts (must be excluded).

    `*_rejected` is `(invalid, deferred)`: replies that arrived and failed validation, and asks
    the relay binned because no reply ever arrived. Both write a proposal-log row and NO verdict
    row -- that asymmetry is the whole reason the validity counts exist. `excluded` is the count
    of extra in-window candidate proposals carrying an EXCLUDED_TIERS verdict.
    """
    from grid.odin import DEFER_LIMIT
    from grid.verdicts import Ledger
    (vdir / "prompts").mkdir(parents=True, exist_ok=True)
    led = Ledger(vdir / "verdicts.jsonl"); rows = []
    def add(i, alias, ts, tier, births):
        rid = f"{alias}-{i}-{ts}"
        rows.append({"ts": ts, "request_id": rid, "outcome": "proposed", "model_alias": alias, "parent_genome": ["a"], "prompt_path": f"prompts/{rid}.txt", "proposal": ["a", "nop"]})
        led.append({"request_id": rid, "model_alias": alias, "tier": tier, "births": births, "fitness": births})
    def reject(i, alias, ts, reason):
        rid = f"{alias}-rej-{i}-{ts}"
        rows.append({"ts": ts, "request_id": rid, "outcome": "rejected", "reason": reason, "model_alias": alias,
                     "parent_genome": ["a"], "prompt_path": f"prompts/{rid}.txt", "proposal": None})
    for i in range(n_inc): add(i, inc, since_ts + 1000 + i, "reproduced" if i < inc_rep else "new", 2 if i < inc_rep else 1)
    for i in range(n_cand): add(i, cand, since_ts + 1000 + i, "reproduced" if i < cand_rep else "new", 2 if i < cand_rep else 1)
    for alias, (invalid, deferred) in ((inc, inc_rejected), (cand, cand_rejected)):
        for i in range(invalid): reject(i, alias, since_ts + 2000 + i, "unknown opcode 'blorp'")
        # The exact string grid/odin.py writes when it gives up on an ask: f"deferred {DEFER_LIMIT} times".
        for i in range(deferred): reject(1000 + i, alias, since_ts + 3000 + i, f"deferred {DEFER_LIMIT} times")
    for i in range(before): add(i, cand, since_ts - 1000 - i, "reproduced", 5)                 # outside the window
    for i in range(excluded): add(999 + i, cand, since_ts + 5000 + i, "preexisting", 40)          # excluded tier
    (vdir / "proposals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _seed_validity(tmp_path, *, candidate, incumbent, cand_defer=0, inc_defer=0):
    """Arms from a seeded log, for the validity floor: `(valid, invalid)` asks per arm.

    Goes through the real `ab_arms` rather than hand-building a dict, so the counting and the
    floor are proved together. `before=5` keeps the out-of-window rows in play: they are
    proposals too, so a count that forgets `since_ts` shows up here as a shifted ratio.
    """
    from grid.weekly import ab_arms
    v = tmp_path / "v"
    n_cand, cand_invalid = candidate
    n_inc, inc_invalid = incumbent
    _seed_ab(v, since_ts=1_000_000, inc="inc", cand="cand", n_inc=n_inc, n_cand=n_cand,
             inc_rep=0, cand_rep=0, before=5, excluded=0,
             inc_rejected=(inc_invalid, inc_defer), cand_rejected=(cand_invalid, cand_defer))
    return ab_arms(v, since_ts=1_000_000, incumbent="inc", candidate="cand")


def test_a_candidate_that_cannot_answer_loses_instead_of_reading_short(tmp_path):
    # 40 asks on the candidate: 8 valid, 32 rejected. No verdict rows for the rejects, so on
    # settled counts alone the candidate reads `inconclusive` (8 < MIN_SAMPLE) and survives to
    # the two-week cap blaming sample size. Mutation: delete the floor block in ab_decide.
    from grid.weekly import ab_decide
    arms = _seed_validity(tmp_path, candidate=(8, 32), incumbent=(120, 0))
    assert arms["cand"] == {"settled": 8, "reproduced": 0, "rate": 0.0, "births_per_proposal": 1.0,
                            "answered": 40, "valid": 8, "unanswered": 0, "validity": pytest.approx(0.2)}
    assert arms["inc"]["answered"] == 120 and arms["inc"]["validity"] == 1.0
    verdict, reason = ab_decide(arms, incumbent="inc", candidate="cand")
    assert verdict == "loss"
    assert "validity" in reason and "0.20" in reason
    # Mutation: drop the `since_ts` guard on the count -> the 5 out-of-window candidate
    # proposals join in, validity reads 13/45 = "0.29", and the assertion above goes RED.
    assert reason == "colony A/B lost: cand validity 0.20 below 0.5 over 40 answered"


def test_an_arm_that_was_never_answered_is_not_scored_as_bad_wording(tmp_path):
    # An outage is an availability failure of the colony, not a wording failure of the arm:
    # defer-binned asks are excluded from BOTH sides of the ratio and reported on their own.
    # Mutation: count the defer bins as `answered` -> validity reads 0.75, not 1.0, and the
    # candidate is one bad week away from losing for a gateway that was down.
    from grid.weekly import ab_decide
    arms = _seed_validity(tmp_path, candidate=(120, 0), incumbent=(120, 0), cand_defer=40)
    assert arms["cand"]["answered"] == 120 and arms["cand"]["valid"] == 120
    assert arms["cand"]["unanswered"] == 40 and arms["cand"]["validity"] == 1.0
    assert arms["inc"]["unanswered"] == 0
    assert ab_decide(arms, incumbent="inc", candidate="cand")[0] == "win"


def test_the_validity_floor_is_silent_below_its_minimum_and_for_the_incumbent(tmp_path):
    # 19 answered is too thin to call wording on, however bad it looks.
    from grid.weekly import VALIDITY_MIN, VALIDITY_MIN_ANSWERED, ab_decide
    thin = _seed_validity(tmp_path / "thin", candidate=(1, 18), incumbent=(120, 0))
    assert thin["cand"]["answered"] == 19 and thin["cand"]["validity"] < VALIDITY_MIN
    assert ab_decide(thin, incumbent="inc", candidate="cand")[0] == "inconclusive"
    # One more answered ask and the floor speaks. Mutation: `>=` -> `>` in ab_decide, RED here.
    at_bar = _seed_validity(tmp_path / "at_bar", candidate=(1, 19), incumbent=(120, 0))
    assert at_bar["cand"]["answered"] == VALIDITY_MIN_ANSWERED
    assert ab_decide(at_bar, incumbent="inc", candidate="cand")[0] == "loss"
    assert VALIDITY_MIN == 0.5 and VALIDITY_MIN_ANSWERED == 20
    # And silent for the INCUMBENT, whatever its validity: a running model that stops parsing is
    # a colony-wide fault the `ab_check` stuck rule owns, and a floor branch here would report it
    # as a verdict on the candidate -- which answered everything. Mutation: add an incumbent
    # branch mirroring the candidate one -> this reads "loss" and blames the wrong arm.
    sick_inc = _seed_validity(tmp_path / "sick", candidate=(120, 0), incumbent=(8, 32))
    assert sick_inc["inc"]["validity"] == pytest.approx(0.2) and sick_inc["cand"]["validity"] == 1.0
    verdict, reason = ab_decide(sick_inc, incumbent="inc", candidate="cand")
    assert verdict == "inconclusive" and "validity" not in reason


def test_ab_arms_windows_on_proposal_ts_and_excludes_unattributable_tiers(tmp_path):
    from grid.weekly import ab_arms
    v = tmp_path / "v"; _seed_ab(v, since_ts=1_000_000)
    arms = ab_arms(v, since_ts=1_000_000, incumbent="kannaka-brain-v3", candidate="kannaka-brain-v4")
    assert arms["kannaka-brain-v3"] == {"settled": 120, "reproduced": 30, "rate": 0.25, "births_per_proposal": pytest.approx((30*2 + 90) / 120),
                                        "answered": 120, "valid": 120, "unanswered": 0, "validity": 1.0}
    assert arms["kannaka-brain-v4"]["settled"] == 110 and arms["kannaka-brain-v4"]["reproduced"] == 25   # not 110+5+1
    # The attempt count windows on the same `since_ts`: 110 + the excluded-tier ask, not the 5 before it.
    assert arms["kannaka-brain-v4"]["answered"] == 111 and arms["kannaka-brain-v4"]["valid"] == 111
    arms2 = ab_arms(v, since_ts=1_000_000, incumbent="kannaka-brain-v3", candidate="kannaka-brain-v9")
    assert arms2["kannaka-brain-v9"] == {"settled": 0, "reproduced": 0, "rate": 0.0, "births_per_proposal": 0.0,
                                         "answered": 0, "valid": 0, "unanswered": 0, "validity": 0.0}
    # Minor: a proposal-log line with no request_id (a future writer, a hand-edit) must be
    # skipped, not KeyError the whole gate. Mutation: `e["request_id"]` -> KeyError here.
    with open(v / "proposals.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1_000_500, "outcome": "proposed", "model_alias": "kannaka-brain-v4"}) + "\n")
    assert ab_arms(v, since_ts=1_000_000, incumbent="kannaka-brain-v3", candidate="kannaka-brain-v4") == arms


def test_ab_decide_boundaries():
    from grid.weekly import MIN_SAMPLE, TOLERANCE, ab_decide
    inc, cand = "v3", "v4"
    # Every ask answered and valid, so the validity floor stays out of the rate boundaries below.
    arm = lambda r, s: {"settled": s, "reproduced": int(r*s), "rate": r, "births_per_proposal": 0,
                        "answered": s, "valid": s, "unanswered": 0, "validity": 1.0}
    arms = lambda ri, si, rc, sc: {inc: arm(ri, si), cand: arm(rc, sc)}
    assert ab_decide(arms(0.25, 120, 0.225, 110), incumbent=inc, candidate=cand)[0] == "win"     # exactly rate_i*(1-0.10)
    assert (0.25 * (1 - TOLERANCE)).hex() == (0.225).hex()   # ...and exactly, in binary too: no epsilon needed
    # The epsilon IS needed for rates that come out of real counts. 42/108 x 0.9 lands one ulp
    # ABOVE 35/100, so a candidate that is exactly on the tolerance line loses without it.
    # Mutation: drop the `- 1e-12` in ab_decide -> this "win" becomes "loss".
    assert (42 / 108 * (1 - TOLERANCE)).hex() != (35 / 100).hex() and 35 / 100 < 42 / 108 * (1 - TOLERANCE)
    assert ab_decide(arms(42 / 108, 108, 35 / 100, 100), incumbent=inc, candidate=cand)[0] == "win"
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
    # Controller ruling (livelock): a tag that never reached sample size in MAX_WEEKS is
    # blocked too, or the gate would re-run the same undersampled candidate every week.
    v.append(release_row(week="W6", decision="held", version="v6", previous="v3", evals={}, reason="colony A/B inconclusive after 2 weeks: sample 40/120 below 100 per arm", **kw))
    assert lost_tags(v) == {"v4", "v5", "v6"}


def test_is_stuck_rule():
    from grid.weekly import is_stuck
    arms = lambda si, sc: {"v3": {"settled": si, "reproduced": 0, "rate": 0, "births_per_proposal": 0}, "v4": {"settled": sc, "reproduced": 0, "rate": 0, "births_per_proposal": 0}}
    t0 = 1_000_000_000
    assert is_stuck(arms(60, 0), since_ts=t0, now_ms=t0 + 49*3600*1000, incumbent="v3", candidate="v4") is True
    assert is_stuck(arms(60, 0), since_ts=t0, now_ms=t0 + 40*3600*1000, incumbent="v3", candidate="v4") is False   # too early
    assert is_stuck(arms(40, 0), since_ts=t0, now_ms=t0 + 49*3600*1000, incumbent="v3", candidate="v4") is False   # incumbent too thin to judge
    assert is_stuck(arms(60, 1), since_ts=t0, now_ms=t0 + 49*3600*1000, incumbent="v3", candidate="v4") is False   # candidate alive
    # C3: a wedged relay (a candidate ask holding thegrid's only request slot) starves the
    # incumbent too, so it can never reach STUCK_INCUMBENT_MIN -- both arms at zero after 48 h
    # IS stuck. Mutation: drop the `or arms[incumbent]["settled"] == 0` -> this is False and
    # nothing ever recovers the A/B.
    assert is_stuck(arms(0, 0), since_ts=t0, now_ms=t0 + 49*3600*1000, incumbent="v3", candidate="v4") is True
    assert is_stuck(arms(0, 0), since_ts=t0, now_ms=t0 + 40*3600*1000, incumbent="v3", candidate="v4") is False   # too early


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


async def _run_ab_week(tmp_path, *, inc_rep, cand_rep, n_cand=110, local_run=None,
                       alias="kannaka-brain-v3", remote_fail=frozenset()):
    cfg = _cfg(tmp_path); v = tmp_path / "v"
    from grid.versions import Versions, release_row
    since = 1_000_000_000
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W0", decision="released", version="kannaka-brain-v3", previous=None, evals={}, reason="", **kw))
    vs.append(release_row(week="W1", decision="ab_started", version="kannaka-brain-v4", previous="kannaka-brain-v3", evals={"colony_ab": {"candidate": "kannaka-brain-v4", "incumbent": "kannaka-brain-v3", "split": 0.2, "since_ts": since}}, reason="", **kw))
    _seed_ab(v, since_ts=since, inc_rep=inc_rep, cand_rep=cand_rep, n_cand=n_cand)
    (tmp_path / "grid.env").write_text(f"MIND_MODEL_ALIAS={alias}\nMIND_LLM_MODEL=kannaka-brain-current\nMIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST},
                   fail=remote_fail)
    restarts = []
    if local_run is None:
        local_run = lambda c: restarts.append(c) or 0
    async def never(p): raise AssertionError("probes must not run during an A/B week")
    row = await release(cfg, r, ask_factory=lambda m: never, local_run=local_run, week="W2")
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
    # I3: the row must name WHICH arm was short, so lost_tags can tell a candidate that could
    # not pull its weight from a week where the colony settled nothing for anyone.
    assert row2["evals"]["colony_ab"]["short_arms"] == ["kannaka-brain-v4"]


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
    # N2: the candidate arm alone produced nothing while the incumbent kept settling, so this
    # one IS the candidate's fault and stays remembered.
    assert row["evals"]["colony_ab"]["short_arms"] == ["kannaka-brain-v4"]
    assert lost_tags(vs) == {"kannaka-brain-v4"}


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


# ---- Fix round 1: reviewer findings on the two-stage gate ----

def test_restart_relay_retries_once_with_noninteractive_sudo():
    from grid.weekly import _restart_relay
    calls = []
    assert _restart_relay(lambda c: calls.append(c) or 0) == "ok"
    assert calls == [["sudo", "-n", "systemctl", "restart", "grid-odin.service"]]
    calls.clear()
    assert _restart_relay(lambda c: calls.append(c) or 1) == "flip_incomplete"
    assert calls == [["sudo", "-n", "systemctl", "restart", "grid-odin.service"]] * 2   # I2/C2: retried once, like flip()


async def test_release_start_ab_records_flip_incomplete_when_relay_restart_fails(tmp_path):
    # C2: if the restart fails after a START, the candidate arm never runs and must not
    # be recorded as `ab_started` -- otherwise ab_check would falsely call it stuck 48h
    # later and permanently block it via lost_tags.
    # N2: nor may the env go on claiming a candidate the ledger doesn't track -- start_ab
    # must revert its own env writes on a failed restart, same as flip() does for C1.
    cfg = _cfg(tmp_path); _seed_one_proposal(tmp_path / "v")
    grid_env_before = cfg.grid_env.read_text()
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def her(p): return '["harvest", "nop", "fork"]' if "Instruction set" in p else "I am Kannaka."
    row = await release(cfg, r, ask_factory=lambda m: her, local_run=lambda c: 1, week="W1")
    assert row["decision"] == "flip_incomplete"
    assert "relay restart failed" in row["reason"]
    env = (tmp_path / "grid.env").read_text()
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" not in env   # N2: reverted, not left stray
    # C1: the candidate keeps the empty idiom (the relay reads empty as "no A/B"), but an
    # absent-before MIND_AB_SPLIT must be REMOVED, not blanked: os.environ.get returns "" for
    # a present-but-empty line and float("") kills the relay at boot. Mutation: revert the
    # revert to `_env_set(..., before_split if before_split is not None else "")` -> the file
    # ends with a `MIND_AB_SPLIT=` line and this assertion fails.
    assert env == grid_env_before + "MIND_LLM_CANDIDATE=\n"
    assert "MIND_AB_SPLIT=" not in env
    from grid.versions import Versions
    assert ab_state(Versions(cfg.releases_dir / "versions.jsonl")) is None   # not left "running"


async def test_ab_loss_records_flip_incomplete_when_relay_restart_fails(tmp_path):
    # C2: if the restart fails after a LOSS, the losing candidate must not be reported as
    # cleanly `held` -- the relay may still be splitting traffic to it.
    row, r, env, restarts = await _run_ab_week(tmp_path, inc_rep=30, cand_rep=10, local_run=lambda c: 1)
    assert row["decision"] == "flip_incomplete"
    assert "colony A/B lost" in row["reason"] and "relay restart failed" in row["reason"]
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" not in env   # the candidate line is still cleared...
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in env         # ...but the row says the restart failed


async def test_rollback_with_no_candidate_restarts_relay_once(tmp_path):
    # I1: rollback must not call end_ab when no A/B is running -- that would double the
    # relay restart and append a second, duplicate MIND_LLM_CANDIDATE= line every time.
    cfg = _cfg(tmp_path)
    from grid.versions import Versions, release_row
    v = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="kannaka-brain-v3", previous=None, **kw))
    v.append(release_row(week="W2", decision="released", version="kannaka-brain-v4", previous="kannaka-brain-v3", **kw))
    restarts = []
    row = rollback(cfg, FakeRemote({}), local_run=lambda cmd: restarts.append(cmd) or 0)
    assert row["decision"] == "rolled_back"
    assert restarts == [["sudo", "-n", "systemctl", "restart", "grid-odin.service"]]   # exactly one: the flip's
    # flip() itself always clears the candidate line as part of its own write (C1); I1 is
    # only about *rollback* not separately calling end_ab (which would double the restart
    # and append a second, duplicate line) when no A/B was running.
    env = (tmp_path / "grid.env").read_text()
    assert env.count("MIND_LLM_CANDIDATE=") == 1


async def test_release_guards_empty_evals_on_ab_started_row(tmp_path):
    # I3: an ab_started row written with no evals (hand-edited, or a future writer that
    # omits it) must not KeyError release() out of existence.
    cfg = _cfg(tmp_path); v = tmp_path / "v"
    from grid.versions import Versions, release_row
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W1", decision="ab_started", version="kannaka-brain-v4", previous="kannaka-brain-v3", evals={}, reason="", **kw))
    vs.append(release_row(week="W2", decision="ab_inconclusive", version="kannaka-brain-v4", previous="kannaka-brain-v3", evals={}, reason="", **kw))
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def never(p): raise AssertionError
    row = await release(cfg, r, ask_factory=lambda m: never, local_run=lambda c: 0, week="W3")
    assert row["decision"] == "held"   # MAX_WEEKS reached, not a KeyError


async def test_ab_verdict_block_always_binding_true_even_if_seed_says_otherwise(tmp_path):
    # Minor: the merge order must let `binding: True` win over anything inherited from the
    # started row's own colony_ab dict, not the other way around.
    cfg = _cfg(tmp_path); v = tmp_path / "v"; since = 1_000_000_000
    from grid.versions import Versions, release_row
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W1", decision="ab_started", version="kannaka-brain-v4", previous="kannaka-brain-v3",
                          evals={"colony_ab": {"candidate": "kannaka-brain-v4", "incumbent": "kannaka-brain-v3", "split": 0.2,
                                               "since_ts": since, "binding": False}},
                          reason="", **kw))
    _seed_ab(v, since_ts=since, inc_rep=30, cand_rep=20, n_cand=50)   # undersampled -> inconclusive, A/B stays open
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def never(p): raise AssertionError
    row = await release(cfg, r, ask_factory=lambda m: never, local_run=lambda c: 0, week="W2")
    assert row["decision"] == "ab_inconclusive"
    assert row["evals"]["colony_ab"]["binding"] is True


async def test_release_is_unchanged_when_candidate_already_released_and_no_ab(tmp_path):
    # I5: end-to-end `unchanged` coverage -- lost when the old probe-then-flip test's
    # third call was rewritten to hit the new A/B-verdict branch instead.
    cfg = _cfg(tmp_path)
    from grid.versions import Versions, release_row
    v = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="kannaka-brain-v4", previous=None, **kw))
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def never(p): raise AssertionError("no probes for an already-released candidate")
    row = await release(cfg, r, ask_factory=lambda m: never, local_run=lambda c: 0, week="W2")
    assert row["decision"] == "unchanged"


def test_ab_cli_no_ab_running_guards(tmp_path, monkeypatch, capsys):
    # I4: `ab --abort` with nothing running must mirror `--check`'s guard exactly -- print,
    # exit 0, and touch nothing (no env write, no restart, no ledger row).
    from grid.weekly import main
    grid_env = tmp_path / "grid.env"
    grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_MODEL=kannaka-brain-current\n")
    monkeypatch.setenv("RELEASES_DIR", str(tmp_path / "releases"))
    monkeypatch.setenv("VERDICTS_DIR", str(tmp_path / "v"))
    monkeypatch.setenv("GRID_ENV", str(grid_env))
    monkeypatch.setenv("DEBAIN2_HOST", "debain2")
    before = grid_env.read_text()

    assert main(["ab", "--check"]) == 0
    assert "no A/B running" in capsys.readouterr().out
    assert main(["ab", "--abort"]) == 0
    assert "no A/B running" in capsys.readouterr().out

    assert grid_env.read_text() == before
    from grid.versions import Versions
    assert Versions(tmp_path / "releases" / "versions.jsonl").rows() == []


def test_ab_cli_rejects_combined_flags():
    # Minor: --abort/--check/--forget are mutually exclusive.
    from grid.weekly import main
    with pytest.raises(SystemExit):
        main(["ab", "--abort", "--check"])


# ---- Fix round 2: two holes the C2 change opened ----

async def test_lost_tags_remembers_a_loss_recorded_as_flip_incomplete(tmp_path):
    # N1: a loss verdict that lands as flip_incomplete (C2: the relay restart failed) must
    # be remembered exactly like a `held` loss -- ab_state() already treats flip_incomplete
    # as closing the A/B, so if lost_tags didn't also count it the colony's rejection would
    # be erased and the gate would probe-and-restart an A/B on the very model it just lost.
    row, r, env, restarts = await _run_ab_week(tmp_path, inc_rep=30, cand_rep=10, local_run=lambda c: 1)
    assert row["decision"] == "flip_incomplete" and "colony A/B lost" in row["reason"]
    from grid.versions import Versions
    vs = Versions(tmp_path / "releases" / "versions.jsonl")
    assert ab_state(vs) is None                        # the A/B reads as closed either way
    assert "kannaka-brain-v4" in lost_tags(vs)          # ...but the loss must still be remembered

    # a fresh release() on the same tag must hold as "already lost/stuck", never re-probe
    cfg2 = _cfg(tmp_path)   # resets grid.env to a clean, no-candidate state for the retest
    r2 = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                     "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def never(p): raise AssertionError("a remembered loser must not be re-probed")
    row2 = await release(cfg2, r2, ask_factory=lambda m: never, local_run=lambda c: 0, week="W3")
    assert row2["decision"] == "held" and "already lost/stuck" in row2["reason"]


def test_ab_abort_clears_a_stray_env_only_candidate(tmp_path, monkeypatch, capsys):
    # N2: a start-path restart failure (before this fix) could leave MIND_LLM_CANDIDATE set
    # with no ledger-tracked A/B at all. `ab --abort` must still be able to clear that stray
    # split -- not refuse via the I4 "no A/B running" guard, which must fire only when
    # NEITHER the ledger nor the env shows anything running.
    import grid.weekly as weekly_mod
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: 0)   # no real subprocess/sudo call
    grid_env = tmp_path / "grid.env"
    grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_MODEL=kannaka-brain-current\n"
                        "MIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    monkeypatch.setenv("RELEASES_DIR", str(tmp_path / "releases"))
    monkeypatch.setenv("VERDICTS_DIR", str(tmp_path / "v"))
    monkeypatch.setenv("GRID_ENV", str(grid_env))
    monkeypatch.setenv("DEBAIN2_HOST", "debain2")

    assert weekly_mod.main(["ab", "--abort"]) == 0
    out = capsys.readouterr().out
    assert "no A/B running" not in out
    env = grid_env.read_text()
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" not in env   # the stray split is cleared
    from grid.versions import Versions
    rows = Versions(tmp_path / "releases" / "versions.jsonl").rows()
    assert len(rows) == 1
    assert rows[0]["decision"] == "held" and rows[0]["version"] == "kannaka-brain-v4"
    assert rows[0]["reason"] == "colony A/B aborted by operator"


# ---- Fix round 3: the final whole-branch review ----

async def test_ab_win_whose_tag_copy_fails_ends_the_ab_and_is_not_remembered(tmp_path):
    # C2: flip() restores MIND_LLM_CANDIDATE on a failed tag copy, so without end_ab the env
    # and the live relay keep splitting while this row closes the A/B in the ledger. The
    # nightly check would then see "no A/B" and the next Sunday would start a fresh one,
    # discarding the week that already won. Mutation: revert the win branch's flip_failed case
    # to `decision, why = "held", "flip failed: tag copy"` -> the candidate line survives in
    # grid.env and no relay restart is recorded.
    row, r, env, restarts = await _run_ab_week(tmp_path, inc_rep=30, cand_rep=28, remote_fail={"ollama cp"})
    assert row["decision"] == "held"
    assert row["reason"] == ("colony A/B won but the tag copy failed: kannaka-brain-v4; "
                             "A/B ended, current unchanged")
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" not in env      # the split really stopped
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in env            # current untouched
    assert restarts == [["sudo", "-n", "systemctl", "restart", "grid-odin.service"]]   # exactly end_ab's
    from grid.versions import Versions
    vs = Versions(tmp_path / "releases" / "versions.jsonl")
    assert ab_state(vs) is None
    # A win that could not be applied is not a rejection: next Sunday must retry the copy.
    assert "kannaka-brain-v4" not in lost_tags(vs)


async def test_ab_win_whose_tag_copy_and_restart_both_fail_is_flip_incomplete(tmp_path):
    row, r, env, restarts = await _run_ab_week(tmp_path, inc_rep=30, cand_rep=28,
                                               remote_fail={"ollama cp"}, local_run=lambda c: 1)
    assert row["decision"] == "flip_incomplete"
    assert row["reason"].startswith("colony A/B won but the tag copy failed")
    assert "relay restart failed" in row["reason"]


async def test_ab_week_refuses_to_judge_a_relabelled_incumbent(tmp_path):
    # I2: `set-mind-model.sh --relay-only` rewrites MIND_MODEL_ALIAS. Mid-A/B that stops the
    # incumbent arm accumulating under the label ab_arms counts, so its rate reads 0 and even
    # a losing candidate clears the tolerance floor. Mutation: drop the env_incumbent guard in
    # release()'s A/B branch -> this returns "released" on a candidate that actually lost.
    row, r, env, restarts = await _run_ab_week(tmp_path, inc_rep=30, cand_rep=10,
                                               alias="kannaka-brain-experimental")
    assert row["decision"] == "ab_inconclusive"
    assert row["reason"] == ("incumbent label changed mid-A/B (kannaka-brain-experimental != "
                             "kannaka-brain-v3); evaluation skipped")
    assert not any(c.startswith("ollama cp") for c in r.commands)   # nothing released
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" in env and not restarts   # the A/B keeps running
    from grid.versions import Versions
    vs = Versions(tmp_path / "releases" / "versions.jsonl")
    assert ab_state(vs)["weeks"] == 1        # the skipped week still counts toward MAX_WEEKS


def test_ab_check_skips_a_relabelled_incumbent(tmp_path, caplog):
    # I2: with the incumbent relabelled, its arm reads 0 settled and EVERY A/B looks stuck
    # 48 h in. Mutation: drop ab_check's env_incumbent guard -> this returns a `held` row and
    # clears a perfectly healthy A/B.
    import logging
    from grid.weekly import ab_check
    from grid.versions import Versions, release_row
    cfg = _cfg(tmp_path); v = tmp_path / "v"; since = 1_000_000_000
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W1", decision="ab_started", version="kannaka-brain-v4", previous="kannaka-brain-v3",
                          evals={"colony_ab": {"candidate": "kannaka-brain-v4", "incumbent": "kannaka-brain-v3", "split": 0.2, "since_ts": since}}, reason="", **kw))
    _seed_ab(v, since_ts=since, n_inc=60, n_cand=0, inc_rep=10, cand_rep=0, before=0)
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-experimental\nMIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    restarts = []
    with caplog.at_level(logging.WARNING, logger="grid.weekly"):
        assert ab_check(cfg, now_ms=since + 49*3600*1000, local_run=lambda c: restarts.append(c) or 0) is None
    assert "incumbent label changed mid-A/B" in caplog.text
    assert not restarts and ab_state(vs) is not None                 # the A/B is left alone
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" in (tmp_path / "grid.env").read_text()


def test_ab_check_aborts_a_wedged_relay_with_both_arms_at_zero(tmp_path):
    # C3: a candidate ask holding thegrid's only request slot starves the incumbent too, so
    # the incumbent >= 50 rule can never fire. Mutation: revert is_stuck to the incumbent-only
    # rule -> ab_check returns None and the dead A/B is never recovered.
    from grid.weekly import ab_check
    from grid.versions import Versions, release_row
    cfg = _cfg(tmp_path); v = tmp_path / "v"; since = 1_000_000_000
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W1", decision="ab_started", version="kannaka-brain-v4", previous="kannaka-brain-v3",
                          evals={"colony_ab": {"candidate": "kannaka-brain-v4", "incumbent": "kannaka-brain-v3", "split": 0.2, "since_ts": since}}, reason="", **kw))
    _seed_ab(v, since_ts=since, n_inc=0, n_cand=0, inc_rep=0, cand_rep=0, before=0)
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    restarts = []
    assert ab_check(cfg, now_ms=since + 40*3600*1000, local_run=lambda c: restarts.append(c) or 0) is None   # too early
    row = ab_check(cfg, now_ms=since + 49*3600*1000, local_run=lambda c: restarts.append(c) or 0)
    assert row["decision"] == "held"
    assert row["reason"] == "colony A/B stuck: no proposals from either arm in 48h"
    assert restarts and ab_state(vs) is None
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" not in (tmp_path / "grid.env").read_text()
    # N2: C3 made this case reachable, and lost_tags used to blacklist on any `colony A/B
    # stuck` reason. A 48 h colony outage is not the candidate's fault. Mutation: drop
    # `short_arms` from ab_check's block -> lost_tags reads None, blacklists the tag, RED.
    assert row["evals"]["colony_ab"]["short_arms"] == ["kannaka-brain-v4", "kannaka-brain-v3"]
    assert lost_tags(vs) == set()


def test_lost_tags_remembers_only_a_candidate_alone_shortfall(tmp_path):
    # I3 + N2: ONE rule for lost/stuck/inconclusive alike -- remember a tag unless some arm
    # OTHER than that tag was under sample. Both arms short is a colony throughput fault (the
    # relay down, the gateway down, the key drained) and must not bury a model that never got
    # its turn. Mutation: match the three prefixes unconditionally again -> v5, v8 blocked too.
    from grid.versions import Versions, release_row
    v = Versions(tmp_path / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    v.append(release_row(week="W1", decision="held", version="v4", previous="v3",
                         evals={"colony_ab": {"short_arms": ["v4"]}},
                         reason="colony A/B inconclusive after 2 weeks: sample 40/120 below 100 per arm", **kw))
    assert lost_tags(v) == {"v4"}
    v.append(release_row(week="W2", decision="held", version="v5", previous="v3",
                         evals={"colony_ab": {"short_arms": ["v5", "v3"]}},
                         reason="colony A/B inconclusive after 2 weeks: sample 40/40 below 100 per arm", **kw))
    assert lost_tags(v) == {"v4"}
    # A row written before short_arms existed reads as candidate-short: the conservative
    # default that keeps the livelock guard (test_ab_state_and_lost_tags) intact.
    v.append(release_row(week="W3", decision="held", version="v6", previous="v3", evals={},
                         reason="colony A/B inconclusive after 2 weeks: sample 40/120 below 100 per arm", **kw))
    assert lost_tags(v) == {"v4", "v6"}
    # N2: a real loss carries `short_arms: []` -- NOTHING was short, the colony decided on the
    # merits -- and must stay remembered. This is the case a literal `short_arms == [tag]` rule
    # would silently stop remembering, so it is asserted alongside the stuck pair below.
    v.append(release_row(week="W4", decision="held", version="v7", previous="v3",
                         evals={"colony_ab": {"short_arms": []}},
                         reason="colony A/B lost: 0.100 vs 0.250", **kw))
    assert lost_tags(v) == {"v4", "v6", "v7"}
    v.append(release_row(week="W5", decision="held", version="v8", previous="v3",
                         evals={"colony_ab": {"short_arms": ["v8", "v3"]}},
                         reason="colony A/B stuck: no proposals from either arm in 48h", **kw))
    assert lost_tags(v) == {"v4", "v6", "v7"}      # a colony outage is not v8's rejection
    v.append(release_row(week="W6", decision="held", version="v9", previous="v3",
                         evals={"colony_ab": {"short_arms": ["v9"]}},
                         reason="colony A/B stuck: candidate 0 settled in 48h (incumbent 60)", **kw))
    assert lost_tags(v) == {"v4", "v6", "v7", "v9"}


def test_env_get_reads_a_value_exactly_as_the_relay_would(tmp_path):
    # N3: round 1 stripped an inline comment here. RelayConfig.from_env strips nothing, so that
    # made the gate and the relay disagree about a value's text by construction -- and I2's
    # guard compares gate-read to gate-read, so it was the one divergence I2 could not see.
    # Mutation: restore `.split(" #", 1)[0].strip()` -> this asserts the stripped value.
    from grid.weekly import _env_get
    p = tmp_path / "grid.env"
    p.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3 # a comment systemd keeps\nMIND_AB_SPLIT=0.2\n")
    assert _env_get(p, "MIND_MODEL_ALIAS") == "kannaka-brain-v3 # a comment systemd keeps"
    assert _env_get(p, "MIND_AB_SPLIT") == "0.2"


def _ab_cli_env(tmp_path, monkeypatch, grid_env_text):
    grid_env = tmp_path / "grid.env"
    grid_env.write_text(grid_env_text)
    monkeypatch.setenv("RELEASES_DIR", str(tmp_path / "releases"))
    monkeypatch.setenv("VERDICTS_DIR", str(tmp_path / "v"))
    monkeypatch.setenv("GRID_ENV", str(grid_env))
    monkeypatch.setenv("DEBAIN2_HOST", "debain2")
    return grid_env


def _seed_started_ab(tmp_path, since):
    from grid.versions import Versions, release_row
    vs = Versions(tmp_path / "releases" / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W1", decision="ab_started", version="kannaka-brain-v4", previous="kannaka-brain-v3",
                          evals={"colony_ab": {"candidate": "kannaka-brain-v4", "incumbent": "kannaka-brain-v3",
                                               "split": 0.2, "since_ts": since}}, reason="", **kw))
    return vs


def test_ab_check_cli_exits_three_when_the_relay_restart_failed(tmp_path, monkeypatch, capsys):
    # I1: `ab --check` is grid-verdicts' ExecStartPost, where a non-zero exit marks the
    # nightly unit failed. flip_incomplete means grid.env says "no A/B" while the live relay
    # is still splitting -- only an operator restarting grid-odin closes that. Mutation:
    # `return 0` from the --check branch -> the wedge is announced to nobody.
    import grid.weekly as weekly_mod
    since = 1_000_000_000
    _ab_cli_env(tmp_path, monkeypatch,
                "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    _seed_started_ab(tmp_path, since)
    _seed_ab(tmp_path / "v", since_ts=since, n_inc=60, n_cand=0, inc_rep=10, cand_rep=0, before=0)
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: 1)          # the restart fails
    monkeypatch.setattr(weekly_mod.time, "time", lambda: since / 1000 + 49 * 3600)
    assert weekly_mod.main(["ab", "--check"]) == 3
    assert json.loads(capsys.readouterr().out)["decision"] == "flip_incomplete"


def test_ab_cli_shows_an_env_only_ab(tmp_path, monkeypatch, capsys):
    # I4: spec §7's receipt hand-starts an A/B by editing grid.env, so there is no ledger row
    # and `ab` printed `null` at an operator watching the split run. Mutation: restore the
    # bare `{"ab": None}` -> "source" is missing here.
    from grid.weekly import main
    grid_env = _ab_cli_env(tmp_path, monkeypatch,
                           "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain\nMIND_AB_SPLIT=0.3\n")
    _seed_ab(tmp_path / "v", since_ts=1_000, inc="kannaka-brain-v3", cand="kannaka-brain",
             n_inc=4, n_cand=3, inc_rep=1, cand_rep=1, before=0)
    assert main(["ab"]) == 0
    ab = json.loads(capsys.readouterr().out)["ab"]
    assert ab["source"] == "env" and ab["candidate"] == "kannaka-brain"
    assert ab["incumbent"] == "kannaka-brain-v3" and ab["split"] == "0.3" and ab["note"]
    assert ab["arms"]["kannaka-brain"]["settled"] == 3 and ab["arms"]["kannaka-brain-v3"]["settled"] == 4
    grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\n")            # no candidate at all
    assert main(["ab"]) == 0 and json.loads(capsys.readouterr().out) == {"ab": None}


def test_ab_abort_records_the_arms_it_threw_away(tmp_path, monkeypatch, capsys):
    # Minor: without the snapshot the abort row says only that someone aborted -- the week's
    # numbers, the only reason to abort, are gone. Mutation: `evals={}` -> KeyError here.
    import grid.weekly as weekly_mod
    since = 1_000_000_000
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: 0)
    _ab_cli_env(tmp_path, monkeypatch,
                "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    _seed_started_ab(tmp_path, since)
    _seed_ab(tmp_path / "v", since_ts=since, n_inc=60, n_cand=20, inc_rep=10, cand_rep=4, before=0)
    assert weekly_mod.main(["ab", "--abort"]) == 0
    block = json.loads(capsys.readouterr().out)["evals"]["colony_ab"]
    assert block["binding"] is True and block["candidate"] == "kannaka-brain-v4"
    assert block["arms"]["kannaka-brain-v4"]["settled"] == 20 and block["arms"]["kannaka-brain-v3"]["settled"] == 60


def test_ab_abort_of_a_stray_env_candidate_records_the_env_view(tmp_path, monkeypatch, capsys):
    import grid.weekly as weekly_mod
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: 0)
    _ab_cli_env(tmp_path, monkeypatch,
                "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain\nMIND_AB_SPLIT=0.2\n")
    _seed_ab(tmp_path / "v", since_ts=1_000, inc="kannaka-brain-v3", cand="kannaka-brain",
             n_inc=4, n_cand=3, inc_rep=1, cand_rep=1, before=0)
    assert weekly_mod.main(["ab", "--abort"]) == 0
    block = json.loads(capsys.readouterr().out)["evals"]["colony_ab"]
    assert block["source"] == "env" and block["binding"] is False and block["note"]
    assert block["arms"]["kannaka-brain"]["settled"] == 3


# ---- Fix round 3: the re-review of b99bdcc ----

async def _relabelled_ab_to_max_weeks(tmp_path, local_run):
    """Two Sundays of an A/B whose incumbent was relabelled mid-flight."""
    alias = "kannaka-brain-experimental"
    row, r, env, restarts = await _run_ab_week(tmp_path, inc_rep=30, cand_rep=10, alias=alias)
    assert row["decision"] == "ab_inconclusive"          # week 1: the week is spent, A/B open
    cfg = _cfg(tmp_path)
    (tmp_path / "grid.env").write_text(f"MIND_MODEL_ALIAS={alias}\nMIND_LLM_MODEL=kannaka-brain-current\n"
                                       "MIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    r2 = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                     "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    async def never(p): raise AssertionError("probes must not run during an A/B week")
    return cfg, await release(cfg, r2, ask_factory=lambda m: never, local_run=local_run, week="W3")


async def test_a_relabelled_ab_still_ends_at_max_weeks(tmp_path):
    # N1 (Critical): `ab_inconclusive` is not a closing decision, so I2's guard on its own
    # wedged the gate permanently -- another ab_inconclusive row every Sunday, no candidate
    # discovery, no probes, no releases, the relay still splitting to an unjudgeable candidate,
    # and `ab --check` exiting 0 every night. MAX_WEEKS must bind here as it does for a real
    # inconclusive week. Mutation: drop the `state["weeks"] + 1 >= MAX_WEEKS` branch (always
    # `ab_inconclusive`) -> week 2 stays open and the gate never recovers.
    restarts = []
    cfg, row = await _relabelled_ab_to_max_weeks(tmp_path, lambda c: restarts.append(c) or 0)
    assert row["decision"] == "held"
    assert row["reason"].startswith("incumbent label changed mid-A/B")
    assert "A/B ended after 2 weeks" in row["reason"]
    from grid.versions import Versions
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    assert ab_state(vs) is None                          # the gate is unwedged
    assert lost_tags(vs) == set()                        # an operator's mislabel is not a rejection
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" not in (tmp_path / "grid.env").read_text()
    assert restarts                                      # end_ab restarted the relay
    # D3: `short_arms` must say what actually happened -- NEITHER arm was judged (`arms` is {}).
    # `[]` is the encoding a genuine loss carries, i.e. full attribution to the candidate, so it
    # left only the reason prefix between an operator's mislabel and a blacklisting. Mutation:
    # `"short_arms": []` -> the reworded-reason assertion below goes RED.
    # (the A/B's own two arms, i.e. the incumbent it was started against -- not the new label)
    assert row["evals"]["colony_ab"]["short_arms"] == ["kannaka-brain-v4", "kannaka-brain-v3"]
    from grid.versions import Versions as _V
    reworded = _V(tmp_path / "reworded.jsonl")
    reworded.append(dict(row, reason="colony A/B inconclusive: incumbent label changed mid-A/B"))
    assert lost_tags(reworded) == set()      # ...and it stays unremembered if the reason is reworded


async def test_a_relabelled_ab_that_cannot_restart_the_relay_is_flip_incomplete(tmp_path):
    cfg, row = await _relabelled_ab_to_max_weeks(tmp_path, lambda c: 1)
    assert row["decision"] == "flip_incomplete"
    assert row["reason"].startswith("incumbent label changed mid-A/B")
    assert "relay restart failed clearing the A/B" in row["reason"]


def _reconcile_cfg(tmp_path, env_text):
    cfg = _cfg(tmp_path)
    (tmp_path / "grid.env").write_text(env_text)
    return cfg


def _ab_rows(tmp_path, *rows):
    from grid.versions import Versions, release_row
    vs = Versions(tmp_path / "releases" / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0, evals={})
    for week, decision, version, reason in rows:
        vs.append(release_row(week=week, decision=decision, version=version, previous=None, reason=reason, **kw))
    return vs


def test_reconciler_repairs_a_standing_flip_incomplete(tmp_path):
    # N4: a flip_incomplete row closes ab_state, so from the next night on `ab --check` said
    # "no A/B running" and exited 0 forever while grid.env and the live relay disagreed. The
    # reconciler checks the RESTING state, so the divergence is repaired (and re-announced)
    # every night until it is actually gone. Mutation: drop the ab_reconcile call at the top
    # of ab_check -> None, no restart, no row.
    from grid.weekly import ab_check
    cfg = _reconcile_cfg(tmp_path, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=\n")
    vs = _ab_rows(tmp_path, ("W1", "ab_started", "kannaka-brain-v4", ""),
                  ("W2", "flip_incomplete", "kannaka-brain-v4", "colony A/B lost: 0.1 vs 0.3; relay restart failed clearing the A/B"))
    restarts = []
    row = ab_check(cfg, now_ms=1, local_run=lambda c: restarts.append(c) or 0)
    assert row["decision"] == "held" and row["reason"].startswith("colony A/B reconciled")
    assert "flip_incomplete" in row["reason"] and restarts
    assert lost_tags(vs) == {"kannaka-brain-v4"}        # the original loss is still remembered
    # ...and it does not fire again once the world is consistent
    assert ab_check(cfg, now_ms=1, local_run=lambda c: restarts.append(c) or 0) is None


def test_reconciler_reports_a_failed_repair_as_flip_incomplete(tmp_path):
    from grid.weekly import ab_check
    cfg = _reconcile_cfg(tmp_path, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=\n")
    _ab_rows(tmp_path, ("W1", "ab_started", "kannaka-brain-v4", ""),
             ("W2", "flip_incomplete", "kannaka-brain-v4", "alias serves the candidate; relay restart failed twice"))
    row = ab_check(cfg, now_ms=1, local_run=lambda c: 1)
    assert row["decision"] == "flip_incomplete"          # -> `ab --check` exits 3, every night


def test_reconciler_clears_a_candidate_whose_ab_the_ledger_closed(tmp_path):
    # N4: env still splitting to a tag the ledger finished with.
    from grid.weekly import ab_check
    cfg = _reconcile_cfg(tmp_path, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    _ab_rows(tmp_path, ("W1", "ab_started", "kannaka-brain-v4", ""),
             ("W2", "held", "kannaka-brain-v4", "colony A/B lost: 0.100 vs 0.250"))
    restarts = []
    row = ab_check(cfg, now_ms=1, local_run=lambda c: restarts.append(c) or 0)
    assert row["decision"] == "held" and "the ledger closed its A/B" in row["reason"]
    assert restarts and "MIND_LLM_CANDIDATE=kannaka-brain-v4" not in (tmp_path / "grid.env").read_text()


def test_reconciler_closes_an_ab_the_env_is_no_longer_running(tmp_path):
    # N4: the ledger says an A/B is open but nothing is splitting -- the relay was never told,
    # or someone cleared the line by hand. Close it so the Sunday gate is not stuck on a
    # verdict it can never reach.
    from grid.weekly import ab_check
    cfg = _reconcile_cfg(tmp_path, "MIND_MODEL_ALIAS=kannaka-brain-v3\n")
    vs = _ab_rows(tmp_path, ("W1", "ab_started", "kannaka-brain-v4", ""))
    restarts = []
    row = ab_check(cfg, now_ms=1, local_run=lambda c: restarts.append(c) or 0)
    assert row["decision"] == "held" and "carries no candidate" in row["reason"]
    assert ab_state(vs) is None and restarts
    assert lost_tags(vs) == set()                        # not the candidate's rejection


def test_reconciler_leaves_a_hand_started_ab_completely_alone(tmp_path, caplog):
    # N4, the rule that matters most: spec §7's own smoke receipt hand-starts an A/B by editing
    # grid.env. A reconciler that clears an operator's A/B is worse than the bug it fixes, so
    # it keys on the LEDGER and never on absence. Mutation: drop the "never seen" guard ->
    # ab_reconcile clears the operator's split and records a row.
    import logging
    from grid.weekly import ab_check
    cfg = _reconcile_cfg(tmp_path, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain\nMIND_AB_SPLIT=0.2\n")
    before = (tmp_path / "grid.env").read_text()
    vs = _ab_rows(tmp_path, ("W1", "released", "kannaka-brain-v3", ""))
    restarts = []
    with caplog.at_level(logging.WARNING, logger="grid.weekly"):
        assert ab_check(cfg, now_ms=1, local_run=lambda c: restarts.append(c) or 0) is None
    assert "hand-started, leaving it alone" in caplog.text
    assert not restarts                                  # nothing touched
    assert (tmp_path / "grid.env").read_text() == before
    assert len(vs.rows()) == 1                           # no row recorded


def test_reconciler_exemption_survives_the_receipts_own_abort(tmp_path, monkeypatch, capsys):
    # D1: the hand-start exemption was one-shot per TAG. `ab --abort` -- the last step of spec
    # §7's own smoke receipt -- appends a `held` row for that tag, which put it in the ledger,
    # so a SECOND run of the same receipt had its split cleared by the next nightly with only a
    # row to show for it. The gate writes `ab_started` whenever it starts an A/B; a hand-started
    # one never has one, before or after an abort. Mutation: key the guard on _AB_DECISIONS
    # again -> run 2's env is cleared and the relay restarted.
    import grid.weekly as weekly_mod
    restarts = []
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: restarts.append(cmd) or 0)
    hand = ("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_MODEL=kannaka-brain-current\n"
            "MIND_LLM_CANDIDATE=kannaka-brain\nMIND_AB_SPLIT=0.2\n")
    grid_env = _ab_cli_env(tmp_path, monkeypatch, hand)
    _ab_rows(tmp_path, ("W0", "released", "kannaka-brain-v3", ""))

    assert weekly_mod.main(["ab", "--check"]) == 0          # receipt run 1: the nightly
    assert grid_env.read_text() == hand and not restarts
    capsys.readouterr()
    assert weekly_mod.main(["ab", "--abort"]) == 0          # ...and the receipt's last step
    capsys.readouterr()
    restarts.clear()

    grid_env.write_text(hand)                               # receipt run 2, same tag
    assert weekly_mod.main(["ab", "--check"]) == 0
    assert "no A/B running" in capsys.readouterr().out
    assert grid_env.read_text() == hand                     # byte-identical: nothing touched
    assert not restarts


def test_ab_check_cli_reconciles_even_when_the_ledger_shows_no_ab(tmp_path, monkeypatch, capsys):
    # N4 at the CLI: `--check` short-circuited on `state is None` and printed "no A/B running".
    # But a standing flip_incomplete IS state-is-None -- that is precisely why the divergence
    # was announced once and never again. Mutation: restore the short-circuit -> "no A/B
    # running", exit 0, while grid.env and the live relay still disagree.
    import grid.weekly as weekly_mod
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: 1)      # the repair restart fails
    _ab_cli_env(tmp_path, monkeypatch, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=\n")
    _ab_rows(tmp_path, ("W1", "ab_started", "kannaka-brain-v4", ""),
             ("W2", "flip_incomplete", "kannaka-brain-v4", "alias serves the candidate; relay restart failed twice"))
    assert weekly_mod.main(["ab", "--check"]) == 3
    out = capsys.readouterr().out
    assert "no A/B running" not in out
    assert json.loads(out)["reason"].startswith("colony A/B reconciled")


def test_ab_abort_cli_exits_three_when_the_relay_restart_failed(tmp_path, monkeypatch, capsys):
    # N6: `release` and `ab --check` exit 3 on flip_incomplete; --abort and rollback did not,
    # so a script wrapping either could not see that env and the live relay disagree.
    # Mutation: `return 0` in the --abort branch -> RED.
    import grid.weekly as weekly_mod
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: 1)
    _ab_cli_env(tmp_path, monkeypatch,
                "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain\nMIND_AB_SPLIT=0.2\n")
    assert weekly_mod.main(["ab", "--abort"]) == 3
    assert json.loads(capsys.readouterr().out)["decision"] == "flip_incomplete"


def test_rollback_cli_exits_three_when_the_relay_restart_failed(tmp_path, monkeypatch, capsys):
    # N6, the rollback half. Mutation: `return 0` in the rollback branch -> RED.
    import grid.weekly as weekly_mod
    from grid.versions import Versions, release_row
    key = tmp_path / "key"; key.write_text("k\n")
    _ab_cli_env(tmp_path, monkeypatch, "MIND_MODEL_ALIAS=kannaka-brain-v4\nMIND_LLM_MODEL=kannaka-brain-current\n")
    monkeypatch.setenv("MIND_LLM_KEY_FILE", str(key))
    monkeypatch.setattr(weekly_mod, "Remote", lambda *a, **k: FakeRemote({}))
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: 1)          # the restart fails
    v = Versions(tmp_path / "releases" / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="kannaka-brain-v3", previous=None, **kw))
    v.append(release_row(week="W2", decision="released", version="kannaka-brain-v4", previous="kannaka-brain-v3", **kw))
    assert weekly_mod.main(["rollback"]) == 3
    assert json.loads(capsys.readouterr().out)["decision"] == "flip_incomplete"


def test_rollback_cli_exits_three_when_the_tag_copy_failed(tmp_path, monkeypatch, capsys):
    # D5: `flip_failed` is not a decision -- rollback records `held` and puts the flip's own
    # outcome verbatim at the end of the reason. The rollback did not happen, so exiting 0 left
    # a script wrapping it unable to tell. Mutation: drop the `endswith(": flip_failed")` clause
    # from _exit_code -> 0.
    import grid.weekly as weekly_mod
    from grid.versions import Versions, release_row
    key = tmp_path / "key"; key.write_text("k\n")
    _ab_cli_env(tmp_path, monkeypatch, "MIND_MODEL_ALIAS=kannaka-brain-v4\nMIND_LLM_MODEL=kannaka-brain-current\n")
    monkeypatch.setenv("MIND_LLM_KEY_FILE", str(key))
    monkeypatch.setattr(weekly_mod, "Remote", lambda *a, **k: FakeRemote({}, fail={"ollama cp"}))
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: 0)
    v = Versions(tmp_path / "releases" / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    v.append(release_row(week="W1", decision="released", version="kannaka-brain-v3", previous=None, **kw))
    v.append(release_row(week="W2", decision="released", version="kannaka-brain-v4", previous="kannaka-brain-v3", **kw))
    assert weekly_mod.main(["rollback"]) == 3
    row = json.loads(capsys.readouterr().out)
    assert row["decision"] == "held" and row["reason"].endswith(": flip_failed")
    assert "MIND_MODEL_ALIAS=kannaka-brain-v4" in (tmp_path / "grid.env").read_text()   # never moved


def test_env_ab_view_does_not_credit_unattributed_rows_to_a_missing_incumbent(tmp_path, monkeypatch, capsys):
    # N7: ab_arms keys on `str(row.get("model_alias", ""))`, so an empty incumbent collected
    # every verdict row nobody authored and reported the total as the incumbent's -- in the one
    # view an operator reads when they cannot tell what is running. Mutation: drop the
    # `arms.pop("", None)` -> the "" arm is reported with 3 settled.
    from grid.verdicts import Ledger
    from grid.weekly import main
    _ab_cli_env(tmp_path, monkeypatch, "MIND_LLM_CANDIDATE=kannaka-brain\n")   # no MIND_MODEL_ALIAS
    v = tmp_path / "v"; v.mkdir(parents=True, exist_ok=True)
    led = Ledger(v / "verdicts.jsonl"); rows = []
    for i in range(3):
        rid = f"u{i}"
        rows.append({"ts": 10, "request_id": rid, "outcome": "proposed", "model_alias": "",
                     "parent_genome": ["a"], "prompt_path": f"prompts/{rid}.txt", "proposal": ["a"]})
        led.append({"request_id": rid, "model_alias": "", "tier": "reproduced", "births": 2, "fitness": 2})
    (v / "proposals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert main(["ab"]) == 0
    ab = json.loads(capsys.readouterr().out)["ab"]
    assert ab["source"] == "env" and ab["incumbent"] is None
    assert set(ab["arms"]) == {"kannaka-brain"}          # the "" arm is not anyone's number
