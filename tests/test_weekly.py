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
             inc_rejected=(0, 0), cand_rejected=(0, 0), excluded=1, variant=False):
    """Proposals + verdicts for two arms; `before` extra candidate proposals stamped before since_ts (must be excluded).

    `*_rejected` is `(invalid, deferred)`: replies that arrived and failed validation, and asks
    the relay binned because no reply ever arrived. Both write a proposal-log row and NO verdict
    row -- that asymmetry is the whole reason the validity counts exist. `excluded` is the count
    of extra in-window candidate proposals carrying an EXCLUDED_TIERS verdict.

    `variant=True` additionally stamps each proposal row's `prompt_variant` with the SAME name
    as its alias, so one seed can be read in either dimension and the two readings compared.
    Left False the rows are shaped like the existing corpus, which carries no such key at all.
    """
    from grid.odin import DEFER_LIMIT
    from grid.verdicts import Ledger
    (vdir / "prompts").mkdir(parents=True, exist_ok=True)
    led = Ledger(vdir / "verdicts.jsonl"); rows = []
    def attribution(alias):
        return {"prompt_variant": alias} if variant else {}
    def add(i, alias, ts, tier, births):
        rid = f"{alias}-{i}-{ts}"
        rows.append({"ts": ts, "request_id": rid, "outcome": "proposed", "model_alias": alias, "parent_genome": ["a"], "prompt_path": f"prompts/{rid}.txt", "proposal": ["a", "nop"], **attribution(alias)})
        led.append({"request_id": rid, "model_alias": alias, "tier": tier, "births": births, "fitness": births})
    def reject(i, alias, ts, reason):
        rid = f"{alias}-rej-{i}-{ts}"
        rows.append({"ts": ts, "request_id": rid, "outcome": "rejected", "reason": reason, "model_alias": alias,
                     "parent_genome": ["a"], "prompt_path": f"prompts/{rid}.txt", "proposal": None, **attribution(alias)})
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
    # The outage is seeded DEEP -- 200 binned asks against 120 answered -- so the exclusion is
    # load-bearing at the decision, not only in the counts: folded in, validity would be
    # 120/320 = 0.375, under the floor, and this arm would LOSE for a gateway that was down.
    # Mutation: count the defer bins as `answered` -> the win below becomes exactly that loss.
    from grid.weekly import ab_decide
    arms = _seed_validity(tmp_path, candidate=(120, 0), incumbent=(120, 0), cand_defer=200)
    assert arms["cand"]["answered"] == 120 and arms["cand"]["valid"] == 120
    assert arms["cand"]["unanswered"] == 200 and arms["cand"]["validity"] == 1.0
    assert arms["inc"]["unanswered"] == 0
    assert 120 / (120 + 200) < 0.5                       # the folded ratio the mutation would produce
    verdict, reason = ab_decide(arms, incumbent="inc", candidate="cand")
    assert verdict == "win" and "validity" not in reason


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
    # Silent AT the floor as well as below it: the spec says *below* 0.5, so a candidate sitting
    # exactly on it survives. Mutation: `< VALIDITY_MIN` -> `<= VALIDITY_MIN`, RED here.
    on_floor = _seed_validity(tmp_path / "on_floor", candidate=(10, 10), incumbent=(120, 0))
    assert on_floor["cand"]["answered"] == 20 and on_floor["cand"]["validity"] == VALIDITY_MIN
    assert ab_decide(on_floor, incumbent="inc", candidate="cand")[0] == "inconclusive"
    # And silent for the INCUMBENT, whatever its validity: a running model that stops parsing is
    # a colony-wide fault the `ab_check` stuck rule owns, and a floor branch here would report it
    # as a verdict on the candidate -- which answered everything. Mutation: add an incumbent
    # branch mirroring the candidate one -> this reads "loss" and blames the wrong arm.
    sick_inc = _seed_validity(tmp_path / "sick", candidate=(120, 0), incumbent=(8, 32))
    assert sick_inc["inc"]["validity"] == pytest.approx(0.2) and sick_inc["cand"]["validity"] == 1.0
    verdict, reason = ab_decide(sick_inc, incumbent="inc", candidate="cand")
    assert verdict == "inconclusive" and "validity" not in reason


def test_a_malformed_or_unknown_log_row_is_excluded_and_never_takes_the_gate_down(tmp_path, caplog):
    import logging
    from grid.odin import DEFER_BIN_REASON, DEFER_BIN_REASON_RE
    from grid.verdicts import Ledger
    from grid.weekly import ab_arms
    # The two must stay married: the reader recognises the shape the writer writes. Mutation:
    # reword either side alone (`f"gave up after {DEFER_LIMIT}"`) -> RED, before any row is read.
    assert DEFER_BIN_REASON_RE.match(DEFER_BIN_REASON)
    v = tmp_path / "v"
    _seed_ab(v, since_ts=1_000_000, inc="inc", cand="cand", n_inc=1, n_cand=1, inc_rep=0, cand_rep=0,
             before=0, excluded=0)
    extra = [
        # `"ts": null` -- a hand-edit or a truncated writer. The count loop reads EVERY row, so
        # before the `or 0` this raised TypeError out of ab_arms and took the Sunday release AND
        # the nightly check down with it. Mutation: `int(e.get("ts") or 0)` -> `int(e.get("ts", 0))`
        # in EITHER loop -> TypeError, and this test is the only thing that notices.
        {"ts": None, "request_id": "null-ts", "outcome": "proposed", "model_alias": "cand"},
        # A rejection with no reason counts as ANSWERED: odin always passes one, so this is a
        # foreign writer, and "she answered badly" is the reading that can cost the candidate
        # the week. Mutation: count it as `unanswered` -> answered reads 2.
        {"ts": 1_000_500, "request_id": "no-reason", "outcome": "rejected", "reason": None, "model_alias": "cand"},
        # M5: a rejection that merely STARTS like the defer bin is an answer, not an outage.
        # Mutation: match the reason by `startswith("deferred ")` -> unanswered reads 2,
        # validity climbs, and an arm that cannot answer stops being able to lose.
        {"ts": 1_000_600, "request_id": "near-miss", "outcome": "rejected",
         "reason": "deferred to human review", "model_alias": "cand"},
        {"ts": 1_000_700, "request_id": "binned", "outcome": "rejected", "reason": DEFER_BIN_REASON,
         "model_alias": "cand"},
        # An outcome this gate has never seen is counted in none of the three -- silence is the
        # safe direction -- but it says so. Mutation: drop the `else` branch and its warning, or
        # widen `elif "rejected"` to a bare `else` -> RED on the warning or on `answered`.
        {"ts": 1_000_800, "request_id": "weird", "outcome": "abandoned", "model_alias": "cand"},
    ]
    with open(v / "proposals.jsonl", "a", encoding="utf-8") as f:
        for e in extra:
            f.write(json.dumps(e) + "\n")
    Ledger(v / "verdicts.jsonl").append({"request_id": "null-ts", "model_alias": "cand", "tier": "new",
                                         "births": 1, "fitness": 1})   # the join loop coerces `ts` too
    with caplog.at_level(logging.WARNING, logger="grid.weekly"):
        arms = ab_arms(v, since_ts=1_000_000, incumbent="inc", candidate="cand")
    assert arms["cand"]["answered"] == 3 and arms["cand"]["valid"] == 1 and arms["cand"]["unanswered"] == 1
    assert arms["cand"]["settled"] == 1                    # the null-ts row windows out of the join, not a crash
    assert "abandoned" in caplog.text and "does not count" in caplog.text


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


async def _run_ab_week(tmp_path, *, inc_rep, cand_rep, n_cand=110, n_inc=120, cand_rejected=(0, 0), local_run=None,
                       alias="kannaka-brain-v3", remote_fail=frozenset()):
    cfg = _cfg(tmp_path); v = tmp_path / "v"
    from grid.versions import Versions, release_row
    since = 1_000_000_000
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W0", decision="released", version="kannaka-brain-v3", previous=None, evals={}, reason="", **kw))
    vs.append(release_row(week="W1", decision="ab_started", version="kannaka-brain-v4", previous="kannaka-brain-v3", evals={"colony_ab": {"candidate": "kannaka-brain-v4", "incumbent": "kannaka-brain-v3", "split": 0.2, "since_ts": since}}, reason="", **kw))
    _seed_ab(v, since_ts=since, inc_rep=inc_rep, cand_rep=cand_rep, n_cand=n_cand, n_inc=n_inc,
             cand_rejected=cand_rejected)
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


async def test_a_validity_loss_blacklists_the_tag_even_with_the_incumbent_undersampled(tmp_path):
    # I1, a livelock. A validity loss ALWAYS has the candidate under MIN_SAMPLE -- its rejects
    # settle nothing -- so if `short_arms` judged the row, an incumbent that also happens to sit
    # under MIN_SAMPLE (80 here) would make it name both arms, `lost_tags` would read that as a
    # colony throughput fault and abstain, and `find_candidate` would hand the same broken
    # candidate back next Sunday to lose identically forever. A validity loss is fully
    # attributed, so it carries N2's `[]`: nothing relevant was short, the colony decided.
    # Mutation: restore the plain `short_arms(...)` call -> short_arms reads
    # ["kannaka-brain-v4", "kannaka-brain-v3"] and the tag is NOT remembered, both RED below.
    from grid.versions import Versions
    from grid.weekly import lost_tags
    row, r, env, restarts = await _run_ab_week(tmp_path, inc_rep=20, cand_rep=0, n_inc=80, n_cand=8,
                                               cand_rejected=(32, 0))
    ab = row["evals"]["colony_ab"]
    assert row["decision"] == "held" and "validity" in row["reason"]
    cand = ab["arms"]["kannaka-brain-v4"]
    # 9 answers that parsed (8 plus the excluded-tier ask) against 32 that did not; only 8 settle.
    assert cand["answered"] == 41 and cand["valid"] == 9 and cand["settled"] == 8
    assert ab["arms"]["kannaka-brain-v3"]["settled"] == 80 < 100      # the incumbent IS under the bar
    assert ab["short_arms"] == []
    assert lost_tags(Versions(tmp_path / "releases" / "versions.jsonl")) == {"kannaka-brain-v4"}
    assert not any(c.startswith("ollama cp") for c in r.commands)     # and it certainly did not ship


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
    # Task 5: a bare `ab` now prints one entry PER DIMENSION, so the model view this test is
    # about is asked for by name. Everything below is unchanged.
    assert main(["ab", "--dimension", "model"]) == 0
    ab = json.loads(capsys.readouterr().out)["ab"]
    assert ab["source"] == "env" and ab["candidate"] == "kannaka-brain"
    assert ab["incumbent"] == "kannaka-brain-v3" and ab["split"] == "0.3" and ab["note"]
    assert ab["arms"]["kannaka-brain"]["settled"] == 3 and ab["arms"]["kannaka-brain-v3"]["settled"] == 4
    grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\n")            # no candidate at all
    assert main(["ab", "--dimension", "model"]) == 0 and json.loads(capsys.readouterr().out) == {"ab": None}


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
    assert main(["ab", "--dimension", "model"]) == 0       # Task 5: a bare `ab` is now every dimension
    ab = json.loads(capsys.readouterr().out)["ab"]
    assert ab["source"] == "env" and ab["incumbent"] is None
    assert set(ab["arms"]) == {"kannaka-brain"}          # the "" arm is not anyone's number


# --- Task 4: dimensions in the gate, the memory rule and the ledger ---------------------------

def _seed_ab_row(tmp_path, *, dimension, version, decision, reason="", short_arms=None,
                 incumbent=None, since_ts=1_000_000_000, previous=None):
    """One versions row in one dimension. Appends through the real ledger, so every row these
    tests read has been through the same hash chain the gate writes."""
    from grid.versions import Versions, release_row
    vs = Versions(tmp_path / "releases" / "versions.jsonl")
    ab = {"candidate": version, "incumbent": incumbent, "split": 0.2, "since_ts": since_ts}
    if short_arms is not None:
        ab["short_arms"] = short_arms
    vs.append(release_row(week="W", decision=decision, dimension=dimension, version=version, previous=previous,
                          base="", run="", adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
                          grid_examples={}, verdict_window={}, evals={"colony_ab": ab}, spend_usd=0,
                          rogue_ledger_ts=0, reason=reason))
    return vs


PROMPT_VARIANTS = ("v1-sandbox", "v2-terse")


def _prompt_registry(monkeypatch, ids=PROMPT_VARIANTS):
    """Stand a TWO-variant prompt registry in front of grid.weekly's `get_variant`.

    The shipped registry holds exactly one variant, so no prompt A/B can be started against it
    at all -- the candidate would have to equal the incumbent -- and no test could reach the code
    that writes MIND_PROMPT_CANDIDATE. The double is not permissive: it raises the same
    VariantError on an id it does not hold that the real loader does, which is the refusal the
    gate's candidate check is built on. The real loader has its own suite in tests/test_variants.py.
    """
    import grid.weekly as weekly_mod
    from grid.variants import Variant, VariantError

    def fake_get_variant(variant_id=None, *, path=None):
        vid = ids[0] if variant_id is None else variant_id
        if vid not in ids:
            raise VariantError(f"unknown variant {vid!r}")
        return Variant(id=vid, template="{directive}", sha256="0" * 64, temperature=0.7,
                       max_tokens=400, recall=True, note="test double")

    monkeypatch.setattr(weekly_mod, "get_variant", fake_get_variant)


def test_a_prompt_ab_and_a_model_ab_keep_separate_state_and_memory(tmp_path):
    from grid.versions import Versions
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="held",
                 reason="colony A/B lost: 0.10 vs 0.20", short_arms=["kannaka-brain-v4"])
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_started",
                 incumbent="v1-sandbox")
    versions = Versions(tmp_path / "releases" / "versions.jsonl")
    # Mutation: drop the `dimension_of(r) != dimension: continue` in ab_state -> the prompt
    # ab_started row is read as the model dimension's and this reads "v2-terse", not None.
    assert ab_state(versions, dimension="model") is None
    assert ab_state(versions, dimension="prompt")["candidate"] == "v2-terse"
    # Mutation: drop the same filter in lost_tags -> the model tag's loss is remembered against
    # the prompt dimension too, and a prompt candidate of that name would be refused.
    assert lost_tags(versions, dimension="model") == {"kannaka-brain-v4"}
    assert lost_tags(versions, dimension="prompt") == set()
    # The defaults are what keep every existing call site meaning what it meant.
    assert ab_state(versions) is None and lost_tags(versions) == {"kannaka-brain-v4"}


def test_a_model_decision_does_not_close_a_running_prompt_ab(tmp_path):
    # The dangerous direction, and the one a filter that only skipped OPENERS would miss: an
    # ab_started row is closed by the next released/held/rolled_back/flip_incomplete row, so an
    # ordinary model release landing mid-week would end a prompt A/B in the ledger while the
    # relay kept splitting to it. Mutation: skip only `ab_started` rows of another dimension
    # rather than every row -> the prompt A/B reads as closed here.
    from grid.versions import Versions
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_started", incumbent="v1-sandbox")
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="released")
    versions = Versions(tmp_path / "releases" / "versions.jsonl")
    assert ab_state(versions, dimension="prompt")["candidate"] == "v2-terse"
    assert ab_state(versions, dimension="model") is None
    # ...and the reverse: a prompt week does not add to a model A/B's week count.
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_inconclusive")
    versions = Versions(tmp_path / "releases" / "versions.jsonl")
    assert ab_state(versions, dimension="prompt")["weeks"] == 1


def test_an_entry_with_no_variant_attribution_never_forms_a_prompt_arm(tmp_path):
    from grid.verdicts import Ledger
    from grid.weekly import ab_arms
    v = tmp_path / "v"
    _seed_ab(v, since_ts=1_000_000, inc="v1-sandbox", cand="v2-terse", n_inc=3, n_cand=2,
             inc_rep=0, cand_rep=0, before=0, excluded=0, variant=True)
    # Rows exactly as the existing Skywave corpus has them: an alias, and NO prompt_variant key
    # at all -- not an empty one. In the window, settled, and utterly unattributable to a prompt.
    led = Ledger(v / "verdicts.jsonl")
    with open(v / "proposals.jsonl", "a", encoding="utf-8") as f:
        for i in range(3):
            rid = f"legacy-{i}"
            row = {"ts": 1_000_500 + i, "request_id": rid, "outcome": "proposed",
                   "model_alias": "kannaka-brain-v3", "parent_genome": ["a"],
                   "prompt_path": f"prompts/{rid}.txt", "proposal": ["a"]}
            assert "prompt_variant" not in row
            f.write(json.dumps(row) + "\n")
            led.append({"request_id": rid, "model_alias": "kannaka-brain-v3", "tier": "reproduced",
                        "births": 2, "fitness": 2})
    # The corruption this exists to stop: an incumbent that resolves to "" -- an unreadable
    # MIND_PROMPT_VARIANT, or the env-only view -- turns every unattributed row in the window
    # into that arm's own numbers and decides the week on them. Mutation: drop either
    # `needs_attribution and not arm` skip in ab_arms -> the "" arm reads 3 answered and, with
    # the join loop's skip gone, 3 settled, instead of nothing at all.
    arms = ab_arms(v, since_ts=1_000_000, incumbent="", candidate="v2-terse", dimension="prompt")
    assert arms[""] == {"settled": 0, "reproduced": 0, "rate": 0.0, "births_per_proposal": 0.0,
                        "answered": 0, "valid": 0, "unanswered": 0, "validity": 0.0}
    assert arms["v2-terse"]["answered"] == 2 and arms["v2-terse"]["settled"] == 2
    # With a real incumbent the legacy rows are simply counted nowhere -- not added to it either.
    named = ab_arms(v, since_ts=1_000_000, incumbent="v1-sandbox", candidate="v2-terse", dimension="prompt")
    assert named["v1-sandbox"]["answered"] == 3 and named["v1-sandbox"]["settled"] == 3
    # The same log read in the MODEL dimension still keys on the alias, where those three rows
    # ARE attributed. Two independent readings of one log; neither leaks into the other.
    # Mutation: key the model dimension on the variant too -> the legacy rows lose their arm and
    # `answered` reads 0. `settled` alone would NOT catch it: the join loop reads the model
    # dimension's arm off the VERDICT row, which carries the alias whatever _log_arm says.
    by_model = ab_arms(v, since_ts=1_000_000, incumbent="kannaka-brain-v3", candidate="v2-terse")
    assert by_model["kannaka-brain-v3"] == {"settled": 3, "reproduced": 3, "rate": 1.0,
                                            "births_per_proposal": 2.0, "answered": 3, "valid": 3,
                                            "unanswered": 0, "validity": 1.0}
    assert by_model["v2-terse"]["settled"] == 2 and by_model["v2-terse"]["answered"] == 2


def test_a_prompt_flip_is_local_and_never_touches_the_remote(tmp_path):
    cfg = _cfg(tmp_path); local = []
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n"
                                       "MIND_PROMPT_CANDIDATE=v2-terse\nMIND_PROMPT_AB_SPLIT=0.2\n")
    r = FakeRemote({}, fail={"ollama cp"})      # armed to fail: it must never be reached at all
    # Mutation: route the prompt dimension through flip()'s model body -> `ollama cp` runs (and,
    # armed, returns flip_failed), MIND_MODEL_ALIAS moves to a variant id, and every assertion
    # below goes RED. `flip_failed` is unreachable here because no remote call is made.
    assert flip(cfg, r, "v2-terse", dimension="prompt", local_run=lambda c: local.append(c) or 0) == "ok"
    assert r.commands == []
    env = (tmp_path / "grid.env").read_text()
    assert "MIND_PROMPT_VARIANT=v2-terse" in env
    assert "MIND_PROMPT_CANDIDATE=\n" in env                      # the A/B it won is over
    assert "MIND_PROMPT_AB_SPLIT=0.2" in env                      # inert with no candidate; left alone
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in env             # the other dimension untouched
    assert local == [["sudo", "-n", "systemctl", "restart", "grid-odin.service"]]


def test_a_prompt_flip_puts_both_lines_back_when_the_relay_will_not_restart(tmp_path):
    # Nothing was remote, so a failed restart means NOTHING was applied: the live relay is still
    # serving what it read at boot. Both lines must go back or grid.env would claim a default the
    # relay has never read -- flip()'s own C1 rule.
    cfg = _cfg(tmp_path)
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\n"
                                       "MIND_PROMPT_CANDIDATE=v2-terse\nMIND_PROMPT_AB_SPLIT=0.2\n")
    assert flip(cfg, FakeRemote({}), "v2-terse", dimension="prompt", local_run=lambda c: 1) == "flip_incomplete"
    env = (tmp_path / "grid.env").read_text()
    # There was no MIND_PROMPT_VARIANT line, so there must not be one now. Mutation: restore it
    # with `_env_set(key, "")` instead of `_env_unset` -> "MIND_PROMPT_VARIANT=" appears, which
    # is a different value to RelayConfig.from_env than an absent line (C1).
    assert "MIND_PROMPT_VARIANT" not in env
    # Mutation: drop the revert entirely -> the file says the candidate won and the A/B is over,
    # while the running relay is still splitting to it and serving the old default.
    assert "MIND_PROMPT_CANDIDATE=v2-terse" in env
    # And a dry run touches nothing at all, on this path as on the model one.
    (tmp_path / "grid.env").write_text("MIND_PROMPT_VARIANT=v1-sandbox\n")
    assert flip(cfg, FakeRemote({}), "v2-terse", dimension="prompt",
                local_run=lambda c: pytest.fail("dry run restarted the relay"), dry_run=True) == "ok"
    assert (tmp_path / "grid.env").read_text() == "MIND_PROMPT_VARIANT=v1-sandbox\n"


async def test_a_prompt_candidate_skips_the_probes_and_splits_on_the_prompt_knob(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _prompt_registry(monkeypatch)      # Task 5: start_ab now refuses a candidate no registry holds
    (tmp_path / "grid.env").write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    def no_probe(model):
        raise AssertionError("the identity and perplexity probes measure a MODEL; a prompt "
                             "experiment does not change it, so there is nothing for them to gate")
    restarts = []
    # Mutation: remove the `if dimension == UNSALTED_DIMENSION` guard around the probe block ->
    # ask_factory is called and this raises. Mutation: leave find_candidate on the prompt path
    # -> the version below reads "kannaka-brain-v4", the model tag, on a prompt row.
    row = await release(cfg, r, ask_factory=no_probe, local_run=lambda c: restarts.append(c) or 0,
                        week="W1", dimension="prompt", candidate="v2-terse")
    assert row["decision"] == "ab_started" and row["dimension"] == "prompt" and row["version"] == "v2-terse"
    assert "identity" not in row["evals"] and "holdout_ppl" not in row["evals"]
    assert row["spend_usd"] == 0.0 and row["adapter_sha256"] == "" and row["gguf_sha256"] == ""
    assert r.commands == []          # no key read, no sha256sum, no `ollama show`: all model facts
    env = (tmp_path / "grid.env").read_text()
    # Mutation: give the prompt dimension the model row of _DIMENSION_ENV -> the relay is handed
    # a variant id where it expects a model tag and every ask fails at the gateway.
    assert "MIND_PROMPT_CANDIDATE=v2-terse" in env and "MIND_PROMPT_AB_SPLIT=0.2" in env
    assert "MIND_LLM_CANDIDATE" not in env and "MIND_AB_SPLIT" not in env
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in env
    assert row["evals"]["colony_ab"]["incumbent"] == "v1-sandbox"
    assert restarts and "grid-odin" in " ".join(restarts[0])
    assert row["previous"] is None            # nothing has been released in THIS dimension yet


async def _run_prompt_ab_week(tmp_path, *, inc_rep, cand_rep, n_inc=120, n_cand=110,
                              live_variant="v1-sandbox", local_run=None):
    """A Sunday judging a running PROMPT A/B, with a model release in the same ledger."""
    from grid.versions import Versions, release_row
    cfg = _cfg(tmp_path); v = tmp_path / "v"
    since = 1_000_000_000
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={},
              verdict_window={}, spend_usd=0, rogue_ledger_ts=0)
    vs.append(release_row(week="W0", decision="released", version="kannaka-brain-v3", previous=None, evals={}, reason="", **kw))
    vs.append(release_row(week="W1", decision="ab_started", dimension="prompt", version="v2-terse", previous=None,
                          evals={"colony_ab": {"candidate": "v2-terse", "incumbent": "v1-sandbox",
                                               "split": 0.2, "since_ts": since}}, reason="", **kw))
    _seed_ab(v, since_ts=since, inc="v1-sandbox", cand="v2-terse", inc_rep=inc_rep, cand_rep=cand_rep,
             n_inc=n_inc, n_cand=n_cand, variant=True)
    lines = ["MIND_MODEL_ALIAS=kannaka-brain-v3", "MIND_LLM_MODEL=kannaka-brain-current",
             "MIND_PROMPT_CANDIDATE=v2-terse", "MIND_PROMPT_AB_SPLIT=0.2"]
    if live_variant is not None:
        lines.insert(2, f"MIND_PROMPT_VARIANT={live_variant}")
    (tmp_path / "grid.env").write_text("\n".join(lines) + "\n")
    r = FakeRemote({}, fail={"ollama cp"})           # armed: a prompt week must make no remote call
    restarts = []
    if local_run is None:
        local_run = lambda c: restarts.append(c) or 0
    async def never(p): raise AssertionError("probes must not run during an A/B week")
    # No `candidate`: the judging week needs none, because it reads the running A/B from the ledger.
    row = await release(cfg, r, ask_factory=lambda m: never, local_run=local_run, week="W2", dimension="prompt")
    return row, r, (tmp_path / "grid.env").read_text(), restarts


async def test_a_prompt_ab_win_flips_the_variant_and_leaves_the_model_alias_alone(tmp_path):
    row, r, env, restarts = await _run_prompt_ab_week(tmp_path, inc_rep=30, cand_rep=28)   # 0.255 >= 0.225
    assert row["decision"] == "released" and row["dimension"] == "prompt"
    assert row["evals"]["colony_ab"]["winner"] == "v2-terse"
    assert r.commands == []                                       # no remote step on this path
    assert "MIND_PROMPT_VARIANT=v2-terse" in env and "MIND_PROMPT_CANDIDATE=\n" in env
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in env             # the model alias never moved
    # Mutation: use the undimensioned `versions.last_released()` -> `previous` names the model
    # release sitting in the same ledger, and this prompt row claims to have superseded a model
    # tag. An append-only ledger cannot be corrected afterwards, so it would say that forever.
    assert row["previous"] is None
    # The whole week ran on the prompt knob: the arms are named by variant, the flip wrote the
    # variant line, and the model dimension's own state is untouched by any of it.
    assert set(row["evals"]["colony_ab"]["arms"]) == {"v1-sandbox", "v2-terse"}


async def test_a_prompt_ab_loss_clears_only_the_prompt_candidate_and_is_remembered_alone(tmp_path):
    from grid.versions import Versions
    row, r, env, restarts = await _run_prompt_ab_week(tmp_path, inc_rep=30, cand_rep=10)   # 0.09 < 0.225
    assert row["decision"] == "held" and row["reason"].startswith("colony A/B lost")
    assert "MIND_PROMPT_CANDIDATE=v2-terse" not in env and "MIND_PROMPT_VARIANT=v1-sandbox" in env
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in env and restarts
    vs = Versions(tmp_path / "releases" / "versions.jsonl")
    assert lost_tags(vs, dimension="prompt") == {"v2-terse"}
    # Mutation: drop `dimension=dimension` from end_ab in the loss branch -> MIND_LLM_CANDIDATE
    # is written instead and the prompt split runs on forever.
    assert lost_tags(vs) == set()                    # the model dimension remembers nothing
    assert ab_state(vs, dimension="prompt") is None and ab_state(vs) is None


async def test_an_absent_prompt_variant_line_is_the_registry_default_not_a_relabel(tmp_path):
    from grid.variants import get_variant
    assert get_variant().id == "v1-sandbox"          # what the seeded incumbent arm is logged as
    # With no MIND_PROMPT_VARIANT line at all -- the state of every host until the first prompt
    # release writes one -- odin serves the registry default and logs under its id. Mutation:
    # have `_live_arm` return the bare env value for every dimension -> None != "v1-sandbox",
    # the relabelled-incumbent branch fires, and a healthy A/B is refused judgement to MAX_WEEKS.
    row, r, env, restarts = await _run_prompt_ab_week(tmp_path, inc_rep=30, cand_rep=28, live_variant=None)
    assert row["decision"] == "released" and "incumbent label changed" not in row["reason"]
    assert "MIND_PROMPT_VARIANT=v2-terse" in env


async def test_a_prompt_win_the_relay_will_not_apply_does_not_claim_the_alias_moved(tmp_path):
    row, r, env, restarts = await _run_prompt_ab_week(tmp_path, inc_rep=30, cand_rep=28, local_run=lambda c: 1)
    assert row["decision"] == "flip_incomplete"
    # The model wording ("alias serves the candidate") would be a lie here: nothing was applied,
    # both lines went back. Mutation: share one reason string across the dimensions -> this row
    # tells the operator the winner is live when it is not.
    assert "alias serves the candidate" not in row["reason"]
    assert "MIND_PROMPT_VARIANT=v1-sandbox" in env      # reverted, not moved to the winner
    assert "MIND_PROMPT_CANDIDATE=v2-terse" in env      # reverted: the live relay is still splitting
    assert r.commands == []


def test_a_forget_clears_only_its_own_dimensions_memory(tmp_path):
    from grid.versions import Versions
    from grid.weekly import ab_forget
    cfg = _cfg(tmp_path)
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="held",
                 reason="colony A/B lost: 0.10 vs 0.20", short_arms=["v2-terse"])
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="held",
                 reason="colony A/B lost: 0.10 vs 0.20", short_arms=["kannaka-brain-v4"])
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    assert lost_tags(vs, dimension="prompt") == {"v2-terse"} and lost_tags(vs) == {"kannaka-brain-v4"}
    # Mutation: drop `dimension` from ab_forget -> the forget row lands as a model row, the
    # prompt loss is unforgettable, and that variant can never be retested. A one-way memory.
    ab_forget(cfg, "v2-terse", dimension="prompt")
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    assert lost_tags(vs, dimension="prompt") == set()
    assert lost_tags(vs) == {"kannaka-brain-v4"}         # and it forgot nothing it was not asked to
    # A forget aimed at the wrong dimension clears nothing, which is the only safe reading.
    ab_forget(cfg, "kannaka-brain-v4", dimension="prompt")
    assert lost_tags(Versions(cfg.releases_dir / "versions.jsonl")) == {"kannaka-brain-v4"}


def test_the_reconciler_never_repairs_one_dimension_from_another_dimensions_ledger(tmp_path):
    from grid.weekly import ab_reconcile
    cfg = _reconcile_cfg(tmp_path, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    _ab_rows(tmp_path, ("W1", "ab_started", "kannaka-brain-v4", ""),
             ("W2", "flip_incomplete", "kannaka-brain-v4", "relay restart failed"))
    restarts = []
    # A standing model flip_incomplete is the model dimension's problem. Mutation: drop the
    # `dimension_of(r) == dimension` filter on `rows` in ab_reconcile -> the prompt nightly reads
    # it as its own, restarts the relay for no reason, and writes a `held` row for a prompt A/B
    # that never existed -- which lost_tags would then hold against that tag.
    assert ab_reconcile(cfg, dimension="prompt", local_run=lambda c: restarts.append(c) or 0) is None
    assert restarts == []
    row = ab_reconcile(cfg, local_run=lambda c: restarts.append(c) or 0)
    assert row is not None and row["dimension"] == "model" and restarts


def test_a_healthy_model_split_is_not_a_stray_prompt_split(tmp_path, caplog):
    import logging
    from grid.weekly import ab_check
    cfg = _reconcile_cfg(tmp_path, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\n"
                                   "MIND_AB_SPLIT=0.2\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    _seed_started_ab(tmp_path, 1_000_000_000)
    restarts = []
    # Mutation: read MIND_LLM_CANDIDATE regardless of dimension in ab_reconcile -> the prompt
    # nightly sees a live model A/B as a candidate its own (filtered) view has no ab_started row
    # for, and cries "hand-started" at it every night. It does NOT clear it -- the hand-start
    # exemption saves the A/B -- so the return value stays None and restarts stays empty, and
    # only the LOG shows the bug. A nightly that warns about a healthy A/B forever is how the
    # real warnings stop being read, so the silence is the assertion.
    with caplog.at_level(logging.WARNING, logger="grid.weekly"):
        assert ab_check(cfg, dimension="prompt", now_ms=1, local_run=lambda c: restarts.append(c) or 0) is None
    assert caplog.text == "" and restarts == []
    assert (tmp_path / "grid.env").read_text().count("MIND_LLM_CANDIDATE=kannaka-brain-v4") == 1


async def test_an_unknown_dimension_is_refused_at_every_entry_point(tmp_path):
    from grid.odin import DIMENSIONS
    from grid.versions import Versions
    from grid.weekly import _DIMENSION_ENV, ab_arms, ab_check, ab_forget, ab_reconcile, end_ab, start_ab
    cfg = _cfg(tmp_path)
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    boom = lambda cmd: pytest.fail("an unknown dimension reached the relay restart")
    async def never(p): raise AssertionError("an unknown dimension reached a probe")
    # ONE allowlist, odin's, and every name on it has a knob here: the second guard in
    # _dimension_env is what a future task adding a dimension to DIMENSIONS alone would hit,
    # instead of silently running its experiment on the model alias.
    assert set(_DIMENSION_ENV) == set(DIMENSIONS) == {"model", "prompt"}
    calls = [
        lambda: ab_state(vs, dimension="modle"),
        lambda: lost_tags(vs, dimension="modle"),
        lambda: ab_arms(tmp_path / "v", since_ts=0, incumbent="a", candidate="b", dimension="modle"),
        lambda: start_ab(cfg, candidate="b", incumbent="a", dimension="modle", local_run=boom),
        lambda: end_ab(cfg, dimension="modle", local_run=boom),
        lambda: flip(cfg, FakeRemote({}), "b", dimension="modle", local_run=boom),
        lambda: ab_reconcile(cfg, dimension="modle", local_run=boom),
        lambda: ab_check(cfg, dimension="modle", local_run=boom),
        lambda: ab_forget(cfg, "b", dimension="modle"),
    ]
    for call in calls:
        # Mutation: default an unrecognised dimension to the model row of _DIMENSION_ENV -> a
        # typo runs the whole experiment on the model alias while every ledger row names it
        # something else, and nothing anywhere reports it.
        #
        # M4: the match is `unknown dimension` exactly -- _dimension_env's wording, not
        # release_row's "dimension must be one of". ab_forget used to reach only the latter, so a
        # dimension added to DIMENSIONS with no _DIMENSION_ENV row would have been accepted by
        # that one verb and refused by every other. Mutation: drop the _dimension_env call from
        # ab_forget -> the last entry raises the ledger's message instead and this goes RED.
        with pytest.raises(ValueError, match="unknown dimension"):
            call()
    with pytest.raises(ValueError, match="unknown dimension"):
        await release(cfg, FakeRemote({}), ask_factory=lambda m: never, local_run=boom, dimension="modle")


async def test_release_refuses_a_candidate_it_would_otherwise_have_ignored(tmp_path):
    cfg = _cfg(tmp_path)
    async def never(p): raise AssertionError("no probe should run")
    # A model candidate is DISCOVERED, so one passed in would be silently dropped and the week
    # ledgered as "no promotion this week". Mutation: ignore the argument -> no raise, and an
    # operator's explicit request is answered with a row that says nothing happened.
    with pytest.raises(ValueError, match="discovers its own candidate"):
        await release(cfg, FakeRemote({}), ask_factory=lambda m: never, local_run=lambda c: 0,
                      week="W1", candidate="kannaka-brain-v9")
    # ...including during an A/B week, which returns long before the start path. Mutation: put
    # that guard back down beside find_candidate -> the argument is silently dropped in exactly
    # the case an operator is most likely to have got wrong.
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="ab_started",
                 incumbent="kannaka-brain-v3")
    with pytest.raises(ValueError, match="discovers its own candidate"):
        await release(cfg, FakeRemote({}), ask_factory=lambda m: never, local_run=lambda c: 0,
                      week="W1", candidate="kannaka-brain-v9")
    # A prompt candidate is not discoverable at all; without one there is no experiment to run.
    # Mutation: fall through with candidate None -> `cand["tag"]` is None, the row records a
    # version of null, and start_ab writes MIND_PROMPT_CANDIDATE=None into grid.env.
    with pytest.raises(ValueError, match="needs an explicit candidate"):
        await release(cfg, FakeRemote({}), ask_factory=lambda m: never, local_run=lambda c: 0,
                      week="W1", dimension="prompt")


# --- Task 4, fix round 1: the CLI boundary, the self-A/B, and the half-repair ------------------

def _mixed_ledger(tmp_path, name, *, prompt_releases):
    """Two model releases, then `prompt_releases` prompt releases on top. Returns the cfg.

    The shape that breaks the undimensioned read: ONE prompt release makes the last release a row
    whose `previous` is None (so rollback finds nothing to roll back to); TWO give it a `previous`
    that is another variant id (so rollback aims the model alias at a prompt variant).
    """
    from grid.versions import Versions, release_row
    d = tmp_path / name
    d.mkdir()
    cfg = _cfg(d)
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
              grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    vs.append(release_row(week="W1", decision="released", version="kannaka-brain-v3", previous=None, **kw))
    vs.append(release_row(week="W2", decision="released", version="kannaka-brain-v4", previous="kannaka-brain-v3", **kw))
    prev = None
    for i, variant in enumerate(("v2-terse", "v3-curt")[:prompt_releases]):
        vs.append(release_row(week=f"W{3+i}", decision="released", dimension="prompt",
                              version=variant, previous=prev, **kw))
        prev = variant
    return cfg


def test_a_prompt_release_does_not_break_or_misaim_the_model_rollback(tmp_path):
    from grid.versions import Versions
    from grid.weekly import _last_released
    # C1. Mutation: restore `versions.last_released()` in rollback (and drop the model filter on
    # its row scans). With ONE prompt release the last release is v2-terse, its `previous` is
    # None, and rollback raises SystemExit("nothing to roll back to") -- the emergency lever dies
    # the moment the ledger becomes interesting. With TWO it resolves to v2-terse and runs
    # `ollama cp v2-terse kannaka-brain-current`, pointing the live relay at a model that does
    # not exist, and writes MIND_MODEL_ALIAS=v2-terse.
    for name, n in (("one", 1), ("two", 2)):
        cfg = _mixed_ledger(tmp_path, name, prompt_releases=n)
        r = FakeRemote({})
        restarts = []
        row = rollback(cfg, r, local_run=lambda c: restarts.append(c) or 0)
        assert row["decision"] == "rolled_back" and row["dimension"] == "model"
        assert row["version"] == "kannaka-brain-v3" and row["previous"] == "kannaka-brain-v4"
        assert r.commands == ["ollama cp kannaka-brain-v3 kannaka-brain-current"]
        assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in (cfg.grid_env).read_text()
        # ...and vice versa: however many times the model dimension rolls back, the prompt
        # dimension's own last release is untouched by it.
        vs = Versions(cfg.releases_dir / "versions.jsonl")
        assert _last_released(vs, dimension="model")["version"] == "kannaka-brain-v3"
        assert _last_released(vs, dimension="prompt")["version"] == ("v2-terse" if n == 1 else "v3-curt")


def test_rollback_to_an_explicit_target_will_not_accept_a_prompt_variant(tmp_path):
    # `--to` scans the ledger for a `released` row of that version. Unfiltered it would happily
    # find a PROMPT release and copy a variant id onto the model alias. Mutation: drop the
    # `dimension_of(r) == UNSALTED_DIMENSION` filter on `model_rows` -> no SystemExit, and
    # `ollama cp v2-terse kannaka-brain-current` runs.
    cfg = _mixed_ledger(tmp_path, "explicit", prompt_releases=2)
    r = FakeRemote({})
    with pytest.raises(SystemExit, match="was never released"):
        rollback(cfg, r, to="v2-terse", local_run=lambda c: pytest.fail("the relay was restarted"))
    assert r.commands == []
    assert "MIND_MODEL_ALIAS=kannaka-brain-v3" in cfg.grid_env.read_text()   # never moved


def test_the_cli_reports_and_writes_the_model_dimension_after_a_prompt_release(tmp_path, monkeypatch, capsys):
    import grid.weekly as weekly_mod
    from grid.versions import Versions, release_row
    from grid.weekly import main
    _ab_cli_env(tmp_path, monkeypatch,
                "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\nMIND_AB_SPLIT=0.2\n")
    vs = Versions(tmp_path / "releases" / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
              grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    vs.append(release_row(week="W1", decision="released", version="kannaka-brain-v3", previous=None, **kw))
    vs.append(release_row(week="W2", decision="released", dimension="prompt", version="v2-terse", previous=None, **kw))
    _seed_started_ab(tmp_path, 1_000_000_000)          # a running MODEL A/B, to abort below
    monkeypatch.setattr(weekly_mod, "find_candidate", lambda remote, cfg: None)   # no ssh in a test
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: 0)
    # `released` sits beside find_candidate's model tag in this payload, so it has to be a model
    # release. Mutation: restore `v.last_released()` -> "v2-terse", a prompt variant reported as
    # the released model, to the one command an operator runs to ask what is live.
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["released"] == "kannaka-brain-v3"
    assert main(["ab", "--abort"]) == 0
    row = json.loads(capsys.readouterr().out)
    # Mutation: restore `versions.last_released()` in the abort branch -> `previous` is
    # "v2-terse", a prompt variant id stamped onto a dimension:"model" row in an append-only,
    # hash-chained file that can never be corrected.
    assert row["dimension"] == "model" and row["previous"] == "kannaka-brain-v3"


async def test_release_refuses_a_candidate_that_is_already_the_live_incumbent(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    before = cfg.grid_env.read_text()
    async def never(p): raise AssertionError("the probes must not run for a no-op A/B")
    boom = lambda c: pytest.fail("the relay was restarted to start an A/B against itself")
    # I1, and NOT the same case as `== released`: a dimension that has never had a release
    # through the gate still has a live incumbent (the registry default; a hand-set alias), so
    # `released` is None while the relay is serving this very name. odin refuses two equal arms
    # AT BOOT, so this would take the relay down; and at MAX_WEEKS `short_arms` would return
    # [tag, tag], which lost_tags reads as fully attributed -- blacklisting the registry default.
    # Mutation: drop the `_live_arm` comparison -> `ab_started`, MIND_PROMPT_CANDIDATE=v1-sandbox
    # written, and `boom` fires.
    row = await release(cfg, FakeRemote({}), ask_factory=lambda m: never, local_run=boom,
                        week="W1", dimension="prompt", candidate="v1-sandbox")
    assert row["decision"] == "unchanged" and row["reason"] == "v1-sandbox is already the live prompt incumbent"
    assert cfg.grid_env.read_text() == before        # refused BEFORE anything is written
    # M5: the model-keyed advisory summary does not belong on a prompt row. Mutation: restore
    # `evals={"colony_ab": colony_ab(...)}` -> a by_alias block on a row about a variant.
    assert row["evals"] == {}
    # The model dimension has the same hole whenever MIND_MODEL_ALIAS was set outside the gate
    # (set-mind-model.sh --relay-only), which is exactly when `released` disagrees with it.
    ledger = json.dumps({"ts": 1.7e9, "event": "promotion", "model": "kannaka-brain-v3",
                         "run": "gpu-a100-sxm-20260913-0305"}) + "\n"
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v3\n", "/srv/rogue/ledger.jsonl": ledger,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    row = await release(cfg, r, ask_factory=lambda m: never, local_run=boom, week="W2")
    assert row["decision"] == "unchanged" and row["reason"] == "kannaka-brain-v3 is already the live model incumbent"
    assert row["evals"]["colony_ab"]["advisory"] is True      # ...and a model row still carries it
    assert cfg.grid_env.read_text() == before


def test_start_ab_refuses_two_arms_with_the_same_value(tmp_path):
    from grid.weekly import start_ab
    cfg = _cfg(tmp_path)
    before = cfg.grid_env.read_text()
    # The guard `release` turns into an `unchanged` week, kept here too so no other caller can
    # slip past it -- and worded after odin's own refusal in `Relay.__init__`, which is what
    # makes this fatal rather than merely pointless. Mutation: drop it -> grid.env is written
    # and the relay is restarted onto a configuration it will refuse to boot.
    for dim in ("model", "prompt"):
        with pytest.raises(ValueError, match="candidate and incumbent are both"):
            start_ab(cfg, candidate="same-name", incumbent="same-name", dimension=dim,
                     local_run=lambda c: pytest.fail("the relay was restarted"))
    assert cfg.grid_env.read_text() == before


def test_a_failed_restart_reverts_and_then_restarts_on_the_restored_values(tmp_path, monkeypatch):
    from grid.weekly import start_ab
    cfg = _cfg(tmp_path)
    _prompt_registry(monkeypatch)      # Task 5: start_ab now refuses a candidate no registry holds
    cfg.grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    calls = []
    # I2. Reverting alone is half a repair: systemd was already told to restart, so the relay
    # that was running is gone -- and if the value it was restarted onto is one odin refuses at
    # boot, NOTHING came up. A correct grid.env does not bring the colony's mind back.
    # Mutation: drop the `_restart_after_revert` call -> 2 calls, not 3, and the unit systemd
    # already stopped stays stopped underneath a file that reads perfectly.
    fail_twice = lambda c: calls.append(c) or (1 if len(calls) <= 2 else 0)
    block, outcome = start_ab(cfg, candidate="v2-terse", incumbent="v1-sandbox", dimension="prompt",
                              local_run=fail_twice)
    assert outcome == "flip_incomplete"          # the repair never changes what is REPORTED
    assert calls == [["sudo", "-n", "systemctl", "restart", "grid-odin.service"]] * 3
    env = cfg.grid_env.read_text()
    assert "MIND_PROMPT_CANDIDATE=v2-terse" not in env and "MIND_PROMPT_AB_SPLIT" not in env
    # The model start path has the same revert and gets the same repair -- this is not a
    # prompt-only fix, and MIND_LLM_CANDIDATE left splitting is the N2 hole it closes.
    calls.clear()
    block, outcome = start_ab(cfg, candidate="kannaka-brain-v4", incumbent="kannaka-brain-v3",
                              local_run=fail_twice)
    assert outcome == "flip_incomplete" and len(calls) == 3
    # ...and so does the local flip's revert.
    cfg.grid_env.write_text("MIND_PROMPT_VARIANT=v1-sandbox\nMIND_PROMPT_CANDIDATE=v2-terse\n")
    calls.clear()
    assert flip(cfg, FakeRemote({}), "v2-terse", dimension="prompt", local_run=fail_twice) == "flip_incomplete"
    assert len(calls) == 3
    env = cfg.grid_env.read_text()
    assert "MIND_PROMPT_VARIANT=v1-sandbox" in env and "MIND_PROMPT_CANDIDATE=v2-terse" in env
    # A restart that cannot be brought back at all is reported, not swallowed: same outcome,
    # and the operator is summoned by exit 3 either way.
    calls.clear()
    assert flip(cfg, FakeRemote({}), "v2-terse", dimension="prompt",
                local_run=lambda c: calls.append(c) or 1) == "flip_incomplete"
    assert len(calls) == 4                        # 2 that failed, 2 more that also failed


def test_a_row_with_no_model_alias_never_forms_a_model_arm_either(tmp_path):
    from grid.verdicts import Ledger
    from grid.weekly import ab_arms
    # M1: the model side had the same phantom-arm hole, guarded only at `_env_ab_view`'s
    # `arms.pop("", None)` -- at one view rather than at the counter. An arm with no name is not
    # an arm in either dimension. Mutation: restore
    # `needs_attribution = dimension != UNSALTED_DIMENSION` -> the "" arm reads 3 settled, 3
    # answered and births_per_proposal 9.0, and any reader that does not know to pop it prints
    # those as somebody's numbers.
    v = tmp_path / "v"; (v / "prompts").mkdir(parents=True)
    led = Ledger(v / "verdicts.jsonl"); rows = []
    for i in range(3):
        rid = f"u{i}"
        row = {"ts": 1_000_500 + i, "request_id": rid, "outcome": "proposed",
               "parent_genome": ["a"], "prompt_path": f"prompts/{rid}.txt", "proposal": ["a"]}
        assert "model_alias" not in row
        rows.append(row)
        led.append({"request_id": rid, "tier": "reproduced", "births": 9, "fitness": 9})
    (v / "proposals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    arms = ab_arms(v, since_ts=1_000_000, incumbent="", candidate="kannaka-brain-v4")
    assert arms[""] == {"settled": 0, "reproduced": 0, "rate": 0.0, "births_per_proposal": 0.0,
                        "answered": 0, "valid": 0, "unanswered": 0, "validity": 0.0}
    assert set(arms) == {"", "kannaka-brain-v4"}          # and no third, phantom arm


async def test_a_lost_tag_is_told_a_forget_command_that_will_actually_work(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="held",
                 reason="colony A/B lost: 0.10 vs 0.20", short_arms=["v2-terse"])
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="held",
                 reason="colony A/B lost: 0.10 vs 0.20", short_arms=["kannaka-brain-v4"])
    async def never(p): raise AssertionError("the probes must not run for a blacklisted tag")
    prompt_row = await release(cfg, FakeRemote({}), ask_factory=lambda m: never, local_run=lambda c: 0,
                               week="W5", dimension="prompt", candidate="v2-terse")
    assert prompt_row["decision"] == "held"
    # M2: `ab --forget` writes a MODEL forget, and lost_tags reads it through the same dimension
    # filter as the loss, so the bare command clears nothing here. Mutation: emit the bare model
    # string on both dimensions -> the operator runs it, nothing changes, and the same candidate
    # is refused again next Sunday with the same advice.
    assert "run `grid.weekly ab --forget v2-terse --dimension prompt` to retest" in prompt_row["reason"]
    assert prompt_row["evals"] == {}                     # M5 again: no model-keyed advisory
    r = FakeRemote({"/srv/rogue/current-model": "kannaka-brain-v4\n", "/srv/rogue/ledger.jsonl": ROGUE_LEDGER,
                    "/home/nick/.kannaka-corpus/runs/gpu-a100-sxm-20260913-0305/train.manifest.json": MANIFEST})
    model_row = await release(cfg, r, ask_factory=lambda m: never, local_run=lambda c: 0, week="W6")
    # ...and the model wording is unchanged, byte for byte: no flag that does not exist yet.
    assert model_row["reason"] == ("kannaka-brain-v4 already lost/stuck the colony A/B; "
                                   "run `grid.weekly ab --forget kannaka-brain-v4` to retest")


# --- Task 5: the dimension on the command line, and one experiment at a time ------------------

def _last_row(tmp_path):
    from grid.versions import Versions
    return Versions(tmp_path / "releases" / "versions.jsonl").rows()[-1]


def _release_cli_env(tmp_path, monkeypatch, grid_env_text):
    """`_ab_cli_env` plus a colony key, which only the `release` verb reaches."""
    grid_env = _ab_cli_env(tmp_path, monkeypatch, grid_env_text)
    key = tmp_path / "key"
    key.write_text("k\n")
    monkeypatch.setenv("MIND_LLM_KEY_FILE", str(key))
    return grid_env


def test_starting_a_prompt_ab_while_a_model_ab_runs_is_refused(tmp_path, monkeypatch, capsys):
    # THE INTERLOCK. A prompt A/B started while a model A/B is splitting means every proposal in
    # the week was authored by one of two models AND one of two wordings, so neither arm's rate
    # measures what its row claims -- and both verdicts are recorded as BINDING: one flips the
    # alias, the other blacklists the loser for good. A confounded week is not a slow week, it is
    # a confident wrong answer written into an append-only file.
    #
    # Mutation: drop the `_blocking_experiments` branch in main -> the run falls through to the
    # missing-candidate check and exits 2, and with a candidate supplied it would start the
    # second split for real.
    from grid.weekly import main
    grid_env = _release_cli_env(tmp_path, monkeypatch,
                                "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\n"
                                "MIND_AB_SPLIT=0.2\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="ab_started")
    before = grid_env.read_text()
    assert main(["release", "--dimension", "prompt"]) == 0
    row = _last_row(tmp_path)
    assert row["decision"] == "unchanged"
    assert "model A/B already running" in row["reason"] and "kannaka-brain-v4" in row["reason"]
    # A RECORDED row, not a printed message: without it a Sunday that did nothing looks exactly
    # like a Sunday the timer never fired. Mutation: print and return instead of ledgering ->
    # `_last_row` is the seeded ab_started row and `decision` reads "ab_started".
    assert row["dimension"] == "prompt" and row["week"]
    assert grid_env.read_text() == before          # refused before anything was written
    capsys.readouterr()


def test_the_interlock_runs_both_ways_and_never_blocks_a_judging_week(tmp_path, monkeypatch, capsys):
    # Symmetric, because the confounding is: a model verdict collected while the wording was
    # split is exactly as unusable as the reverse. Mutation: check only `prompt` for blockers
    # -> the model release below starts a second split on top of the running prompt A/B.
    import grid.weekly as weekly_mod
    from grid.weekly import main
    _release_cli_env(tmp_path, monkeypatch,
                     "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n"
                     "MIND_PROMPT_CANDIDATE=v2-terse\nMIND_PROMPT_AB_SPLIT=0.2\n")
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_started",
                 incumbent="v1-sandbox")
    monkeypatch.setattr(weekly_mod, "find_candidate",
                        lambda remote, cfg: pytest.fail("Rogue was read during a refused week"))
    assert main(["release"]) == 0
    row = _last_row(tmp_path)
    assert row["dimension"] == "model" and row["decision"] == "unchanged"
    assert "prompt A/B already running (v2-terse)" in row["reason"]
    capsys.readouterr()

    # ...and the interlock must NEVER refuse a dimension that is being JUDGED. This Sunday is
    # the model A/B's binding verdict, not a start, so the open prompt A/B is not its business:
    # refuse it and both experiments run forever with nothing able to close either.
    # Mutation: drop the `ab_state(...) is None` guard on the interlock -> "unchanged" again,
    # the model A/B is never judged, and MAX_WEEKS never arrives because no week is counted.
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="ab_started",
                 incumbent="kannaka-brain-v3")
    assert main(["release"]) == 0
    row = _last_row(tmp_path)
    assert row["dimension"] == "model" and row["decision"] == "ab_inconclusive"
    capsys.readouterr()


def test_ab_with_no_dimension_reports_every_open_one(tmp_path, monkeypatch, capsys):
    # The read-only view was model-only, so an operator running `ab` during a prompt experiment
    # was told "null" -- nothing running -- while a fifth of live traffic went to the candidate.
    # Mutation: restore the single-dimension display -> `shown["model"]` is a KeyError.
    from grid.weekly import main
    _ab_cli_env(tmp_path, monkeypatch, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="ab_started")
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_started")
    assert main(["ab"]) == 0
    shown = json.loads(capsys.readouterr().out)["ab"]
    assert shown["model"]["candidate"] == "kannaka-brain-v4"
    assert shown["prompt"]["candidate"] == "v2-terse"


def test_the_env_only_view_reads_its_own_dimensions_lines(tmp_path, monkeypatch, capsys):
    # I4's env-only view read MIND_MODEL_ALIAS and MIND_AB_SPLIT whatever it was asked about.
    # Mutation: restore either -> the prompt entry names the model alias as its incumbent and
    # quotes the model split, in the one view an operator reads when they cannot tell what is
    # running, which is precisely when they cannot check it.
    from grid.weekly import main
    _ab_cli_env(tmp_path, monkeypatch,
                "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_AB_SPLIT=0.3\nMIND_PROMPT_VARIANT=v1-sandbox\n"
                "MIND_PROMPT_CANDIDATE=v2-terse\nMIND_PROMPT_AB_SPLIT=0.45\n")
    assert main(["ab"]) == 0
    shown = json.loads(capsys.readouterr().out)["ab"]
    assert shown["model"] is None                      # no MIND_LLM_CANDIDATE: nothing is split
    assert shown["prompt"]["source"] == "env" and shown["prompt"]["candidate"] == "v2-terse"
    assert shown["prompt"]["incumbent"] == "v1-sandbox" and shown["prompt"]["split"] == "0.45"


def test_ab_check_returns_the_highest_exit_code_any_dimension_produced(tmp_path, monkeypatch):
    # model reconciles cleanly, prompt cannot restart the relay -> the run must be loud.
    # This is grid-verdicts' ExecStartPost: exit 0 marks the night fine. Mutation: return the
    # LAST dimension's code (or the first row's) -> the model's clean 0 masks a prompt relay
    # still splitting to a candidate the ledger closed, every night, silently.
    import grid.weekly as weekly_mod
    _ab_cli_env(tmp_path, monkeypatch, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="flip_incomplete")
    monkeypatch.setattr(weekly_mod, "_local_run", lambda *a, **k: 1)
    assert weekly_mod.main(["ab", "--check"]) == 3
    # ...and the row it wrote is the PROMPT dimension's, which is the whole point of D: run on
    # model alone -- all this did before the flag existed -- and a wedged prompt experiment has
    # no nightly at all. Mutation: keep ab_check single-dimension -> exit 0 and no row.
    row = _last_row(tmp_path)
    assert row["dimension"] == "prompt" and row["decision"] == "flip_incomplete"
    assert row["version"] == "v2-terse"


def test_a_failure_in_one_dimension_is_not_masked_by_a_success_in_another(tmp_path, monkeypatch, capsys):
    # F: the nightly reconciles model, fails to restart the relay, then reconciles prompt and
    # succeeds. If the run reported the LAST dimension it touched -- or anything but the loudest
    # -- the unit would exit 0 and the model's still-splitting relay would be announced to
    # nobody, every night. Mutation: `code = _exit_code(row)` instead of the running max -> 0.
    import grid.weekly as weekly_mod
    from grid.odin import DIMENSIONS
    # The construction below is "the FIRST dimension reconciled fails, the SECOND succeeds" --
    # that is what makes last-wins distinguishable from `max`. It depends on the loop's order,
    # which is `sorted(DIMENSIONS)`, so pin that here rather than leaving it to how the names
    # happen to sort: a dimension added later that sorts before "model" would silently reorder
    # the run and this test would quietly stop testing what its comment says.
    assert sorted(DIMENSIONS)[:2] == ["model", "prompt"]
    grid_env = _ab_cli_env(tmp_path, monkeypatch,
                           "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\n"
                           "MIND_AB_SPLIT=0.2\nMIND_PROMPT_VARIANT=v1-sandbox\n"
                           "MIND_PROMPT_CANDIDATE=v2-terse\nMIND_PROMPT_AB_SPLIT=0.2\n")
    for dim, version in (("model", "kannaka-brain-v4"), ("prompt", "v2-terse")):
        _seed_ab_row(tmp_path, dimension=dim, version=version, decision="ab_started")
        _seed_ab_row(tmp_path, dimension=dim, version=version, decision="held")   # ledger closed, env still split
    calls = []
    # model is reconciled first (sorted), and its two restart attempts both fail; the prompt
    # repair that follows works on the first try.
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: calls.append(cmd) or (1 if len(calls) <= 2 else 0))
    assert weekly_mod.main(["ab", "--check"]) == 3
    reported = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    # Both are reported, not just the one that decided the exit code.
    assert [(r["dimension"], r["decision"]) for r in reported] == [("model", "flip_incomplete"), ("prompt", "held")]
    assert "MIND_PROMPT_CANDIDATE=v2-terse" not in grid_env.read_text()


def test_a_candidate_the_registry_does_not_hold_is_refused_at_the_cli_and_at_the_write(tmp_path, monkeypatch):
    # The production hazard: the gate writes MIND_PROMPT_CANDIDATE and restarts grid-odin, whose
    # variant loader fails CLOSED. An id the registry does not hold does not degrade the relay,
    # it stops it -- systemd restarts it into the same refusal until the start limit gives up,
    # and the revert paths cannot argue it back, because the file they restore is not what the
    # failing process reads. Both call sites, because the CLI is not the only caller.
    import grid.weekly as weekly_mod
    from grid.variants import VariantError
    from grid.versions import Versions
    from grid.weekly import main, start_ab
    grid_env = _release_cli_env(tmp_path, monkeypatch,
                                "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    _prompt_registry(monkeypatch)
    monkeypatch.setattr(weekly_mod, "_local_run",
                        lambda cmd: pytest.fail("the relay was restarted onto a variant it cannot load"))
    before = grid_env.read_text()
    # Mutation: drop the `_validate_candidate` call in main -> this raises VariantError from
    # deep inside start_ab instead of SystemExit, i.e. the CLI accepted the argument.
    with pytest.raises(SystemExit):
        main(["release", "--dimension", "prompt", "--candidate", "v2-tesre"])
    assert grid_env.read_text() == before
    assert Versions(tmp_path / "releases" / "versions.jsonl").rows() == []

    # Mutation: drop the `_validate_candidate` call in start_ab -> no raise, and
    # MIND_PROMPT_CANDIDATE=v2-tesre is written and the relay restarted onto it.
    cfg = _cfg(tmp_path)
    cfg.grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    before = cfg.grid_env.read_text()
    with pytest.raises(VariantError, match="unknown variant 'v2-tesre'"):
        start_ab(cfg, candidate="v2-tesre", incumbent="v1-sandbox", dimension="prompt",
                 local_run=lambda c: pytest.fail("the relay was restarted onto an unknown variant"))
    assert cfg.grid_env.read_text() == before
    # ...and the equality guard still fires first, on a name the registry holds perfectly well:
    # the two refusals are each silent about the other's case.
    with pytest.raises(ValueError, match="candidate and incumbent are both"):
        start_ab(cfg, candidate="v1-sandbox", incumbent="v1-sandbox", dimension="prompt",
                 local_run=lambda c: pytest.fail("the relay was restarted"))


def test_the_dimension_flag_is_odins_own_allowlist(tmp_path, monkeypatch, capsys):
    # One allowlist for the relay's arm assignment, the ledger's rows and this parser. Mutation:
    # hardcode the choices -> either a dimension odin knows cannot be typed (the prompt loop has
    # no CLI at all), or a typo reaches _dimension_env as a crash instead of a usage message.
    from grid.odin import DIMENSIONS
    from grid.weekly import main
    _ab_cli_env(tmp_path, monkeypatch, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    for d in sorted(DIMENSIONS):
        assert main(["ab", "--dimension", d]) == 0
        assert json.loads(capsys.readouterr().out) == {"ab": None}
    with pytest.raises(SystemExit):
        main(["ab", "--dimension", "modle"])
    capsys.readouterr()


def test_the_forget_advice_a_lost_prompt_variant_is_given_actually_works(tmp_path, monkeypatch, capsys):
    # Task 4 tells the operator to run exactly this command. Mutation: drop `dimension=` from
    # the --forget branch -> the forget lands as a MODEL row, lost_tags reads it through the
    # dimension filter, nothing is cleared, and the advice is a command that does nothing.
    from grid.versions import Versions
    from grid.weekly import main
    _ab_cli_env(tmp_path, monkeypatch, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="held",
                 reason="colony A/B lost: 0.10 vs 0.20", short_arms=["v2-terse"])
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="held",
                 reason="colony A/B lost: 0.10 vs 0.20", short_arms=["kannaka-brain-v4"])
    assert main(["ab", "--forget", "v2-terse", "--dimension", "prompt"]) == 0
    vs = Versions(tmp_path / "releases" / "versions.jsonl")
    assert lost_tags(vs, dimension="prompt") == set()
    assert lost_tags(vs) == {"kannaka-brain-v4"}       # and it forgot nothing it was not asked to
    capsys.readouterr()


def test_an_abort_clears_only_the_dimension_it_was_given(tmp_path, monkeypatch, capsys):
    # Mutation: leave the --abort branch on MIND_LLM_CANDIDATE / the model `_last_released` ->
    # `ab --abort --dimension prompt` clears the MODEL split (ending an experiment nobody asked
    # to end) while the prompt candidate keeps its share of live traffic, and stamps the row as
    # a prompt row anyway.
    import grid.weekly as weekly_mod
    grid_env = _ab_cli_env(tmp_path, monkeypatch,
                           "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\n"
                           "MIND_AB_SPLIT=0.2\nMIND_PROMPT_VARIANT=v1-sandbox\n"
                           "MIND_PROMPT_CANDIDATE=v2-terse\nMIND_PROMPT_AB_SPLIT=0.2\n")
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_started",
                 incumbent="v1-sandbox")
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: 0)
    assert weekly_mod.main(["ab", "--abort", "--dimension", "prompt"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["dimension"] == "prompt" and row["version"] == "v2-terse"
    assert row["previous"] is None            # nothing has been released in THIS dimension yet
    env = grid_env.read_text()
    assert "MIND_PROMPT_CANDIDATE=v2-terse" not in env
    assert "MIND_LLM_CANDIDATE=kannaka-brain-v4" in env       # the model A/B is untouched


def test_rollback_refuses_a_dimension_it_cannot_roll_back(tmp_path, monkeypatch):
    # `rollback` copies a tag onto kannaka-brain-current on debain2 and writes MIND_MODEL_ALIAS.
    # Accepting --dimension prompt and running it anyway would point the live relay at a "model"
    # that is a prompt variant id. Mutation: drop the guard -> rollback() runs and dies with
    # "nothing has been released", i.e. the flag was silently ignored rather than refused.
    import grid.weekly as weekly_mod
    grid_env = _release_cli_env(tmp_path, monkeypatch,
                                "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    before = grid_env.read_text()
    monkeypatch.setattr(weekly_mod, "_local_run", lambda cmd: pytest.fail("the relay was restarted"))
    with pytest.raises(SystemExit, match="no prompt rollback"):
        weekly_mod.main(["rollback", "--dimension", "prompt"])
    assert grid_env.read_text() == before


# --- Task 5, fix round 1: the hand-started split, argument order, and the deeper guard ---------

async def test_the_interlock_is_not_blind_to_a_hand_started_split(tmp_path, monkeypatch, capsys):
    # IMPORTANT 1. `ab_state` reads the LEDGER, and a spec-7 hand-started A/B -- edit grid.env,
    # restart the relay -- has no `ab_started` row by construction. Skywave has actually been in
    # that state (deploy/receipts/receipts-ab-20260907T142020Z), and the reconciler has a whole
    # exemption branch for it. An interlock that consults only the ledger therefore starts a
    # SECOND live split on top of the first, which is the one thing it exists to stop.
    #
    # Mutation: drop the `_env_get(... ["candidate"])` fallback in `_blocking_experiments` ->
    # `ab_started`, MIND_PROMPT_CANDIDATE=v2-terse written, and `boom` fires.
    from grid.weekly import main
    _prompt_registry(monkeypatch)
    cfg = _cfg(tmp_path)
    cfg.grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_LLM_CANDIDATE=kannaka-brain-v4\n"
                            "MIND_AB_SPLIT=0.2\nMIND_PROMPT_VARIANT=v1-sandbox\n")   # no ledger row at all
    before = cfg.grid_env.read_text()
    async def never(p): raise AssertionError("the probes must not run for a refused week")
    boom = lambda c: pytest.fail("the relay was restarted to start a second live split")
    row = await release(cfg, FakeRemote({}), ask_factory=lambda m: never, local_run=boom,
                        week="W1", dimension="prompt", candidate="v2-terse")
    assert row["decision"] == "unchanged" and row["dimension"] == "prompt"
    # The two cases are named apart, because they need different next actions: a hand-started
    # split is cleared with `ab --abort` and no Sunday will ever judge it. Mutation: report it
    # with the tracked wording -> the operator waits for a verdict that cannot come.
    assert row["reason"] == ("model A/B already running (kannaka-brain-v4, hand-started: no ledger "
                             "row); refusing to start a prompt experiment whose verdict it would confound")
    assert cfg.grid_env.read_text() == before

    # ...and the same through the command line, which is where the Sunday timer enters.
    _release_cli_env(tmp_path, monkeypatch, before)
    assert main(["release", "--dimension", "prompt", "--candidate", "v2-terse"]) == 0
    assert "hand-started: no ledger row" in _last_row(tmp_path)["reason"]
    capsys.readouterr()


async def test_release_itself_refuses_a_second_experiment_not_only_the_cli(tmp_path, monkeypatch):
    # The interlock now lives in BOTH places, for the reason `start_ab` keeps its own copy of the
    # equal-arms refusal: the command line is not the only caller, and the invariant belongs next
    # to the code that acts on it. The CLI copy is still required -- only the argument layer can
    # turn the candidate-less case into a recorded row at exit 0 -- so neither replaces the other.
    #
    # Mutation: drop the `_blocking_experiments` block in `release()` -> "ab_started",
    # MIND_PROMPT_CANDIDATE written, and `boom` fires.
    _prompt_registry(monkeypatch)
    cfg = _cfg(tmp_path)
    cfg.grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    before = cfg.grid_env.read_text()
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="ab_started",
                 incumbent="kannaka-brain-v3")
    async def never(p): raise AssertionError("the probes must not run for a refused week")
    boom = lambda c: pytest.fail("the relay was restarted to start a second experiment")
    row = await release(cfg, FakeRemote({}), ask_factory=lambda m: never, local_run=boom,
                        week="W1", dimension="prompt", candidate="v2-terse")
    assert row["decision"] == "unchanged"
    assert row["reason"] == ("model A/B already running (kannaka-brain-v4); refusing to start a "
                             "prompt experiment whose verdict it would confound")
    assert cfg.grid_env.read_text() == before
    # ...and it still never refuses a JUDGING week: the pre-existing prompt A/B week tests all
    # run with a model release in the same ledger and must keep reaching their verdict.
    assert row["version"] == "v2-terse" and row["evals"] == {}      # M5: no model-keyed advisory


def test_an_illegal_candidate_is_refused_whatever_the_ledger_says(tmp_path, monkeypatch, capsys):
    # IMPORTANT 2. The interlock used to run first, so an argument's validity depended on whether
    # some unrelated experiment happened to be open: with one running, a tag Rogue never promoted
    # -- or a variant id no registry holds -- was ledgered as an `unchanged` row carrying that
    # fabricated version, at exit 0, into an append-only hash-chained file.
    #
    # Mutation: move the `_refuse_given_model_candidate` / `_validate_candidate` block back below
    # the interlock branch -> both calls below return 0 and write a row.
    import grid.weekly as weekly_mod
    from grid.versions import Versions
    from grid.weekly import main
    _prompt_registry(monkeypatch)
    _release_cli_env(tmp_path, monkeypatch,
                     "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n"
                     "MIND_PROMPT_CANDIDATE=v2-terse\nMIND_PROMPT_AB_SPLIT=0.2\n")
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_started",
                 incumbent="v1-sandbox")               # ...so the interlock WOULD fire
    monkeypatch.setattr(weekly_mod, "find_candidate",
                        lambda remote, cfg: pytest.fail("Rogue was read for a rejected argument"))
    rows_before = len(Versions(tmp_path / "releases" / "versions.jsonl").rows())

    # A model candidate is DISCOVERED, never given, and the model dimension is interlocked by the
    # open prompt A/B -- so before this fix the given tag became the `version` of an exit-0 row.
    with pytest.raises(SystemExit) as e:
        main(["release", "--candidate", "kannaka-brain-v9"])
    assert e.value.code == 2

    # And the mirror: a prompt id no registry holds, with the model dimension interlocked.
    _seed_ab_row(tmp_path, dimension="model", version="kannaka-brain-v4", decision="ab_started",
                 incumbent="kannaka-brain-v3")
    with pytest.raises(SystemExit) as e:
        main(["release", "--dimension", "prompt", "--candidate", "v2-tesre"])
    assert e.value.code == 2

    # Neither refusal left a row, and neither invented a version. `rows_before` is counted after
    # the seeds because `_seed_ab_row` writes through the real ledger.
    rows = Versions(tmp_path / "releases" / "versions.jsonl").rows()
    assert len(rows) == rows_before + 1                      # only the model ab_started seed above
    assert not [r for r in rows if r["version"] in ("kannaka-brain-v9", "v2-tesre")]
    capsys.readouterr()


def test_the_candidate_check_refuses_a_dimension_it_has_no_rule_for(tmp_path, monkeypatch):
    # The fail-closed branch of `_validate_candidate`, claimed as a design property in round 1
    # with nothing behind it. Unreachable today -- both known dimensions have a rule -- but it is
    # exactly the line that gets deleted or inverted the day a third dimension is added, which is
    # the day it starts mattering: an unverified name in grid.env is a relay that will not boot.
    # Mutation: `return` instead of the final `raise` -> neither refusal below happens, and
    # start_ab writes MIND_ADAPTER_CANDIDATE and restarts the relay onto it.
    import grid.weekly as weekly_mod
    monkeypatch.setattr(weekly_mod, "DIMENSIONS", frozenset({"model", "prompt", "adapter"}))
    monkeypatch.setitem(weekly_mod._DIMENSION_ENV, "adapter",
                        {"live": "MIND_ADAPTER", "candidate": "MIND_ADAPTER_CANDIDATE",
                         "split": "MIND_ADAPTER_AB_SPLIT"})
    with pytest.raises(ValueError, match="no candidate check for dimension 'adapter'"):
        weekly_mod._validate_candidate("adapter", "whatever")
    cfg = _cfg(tmp_path)
    before = cfg.grid_env.read_text()
    with pytest.raises(ValueError, match="no candidate check for dimension 'adapter'"):
        weekly_mod.start_ab(cfg, candidate="whatever", incumbent="something-else", dimension="adapter",
                            local_run=lambda c: pytest.fail("the relay was restarted"))
    assert cfg.grid_env.read_text() == before


def test_rollback_records_the_experiments_it_confounded(tmp_path, monkeypatch):
    # Rollback stays UNINTERLOCKED on purpose: it is the emergency lever, it writes no binding
    # colony verdict, and refusing it because an experiment is running would trade a recoverable
    # confound for an unrecoverable outage. But swapping the weights under a running prompt A/B
    # invalidates every proposal that arm has produced, and the ledger is the only place a reader
    # will look for that months later. Mutation: drop `confound` from the reason -> the fact that
    # a week was confounded survives only in whoever was at the keyboard.
    from grid.versions import Versions, release_row
    from grid.weekly import rollback
    cfg = _cfg(tmp_path)
    cfg.grid_env.write_text("MIND_MODEL_ALIAS=kannaka-brain-v4\nMIND_PROMPT_VARIANT=v1-sandbox\n"
                            "MIND_PROMPT_CANDIDATE=v2-terse\nMIND_PROMPT_AB_SPLIT=0.2\n")
    vs = Versions(cfg.releases_dir / "versions.jsonl")
    kw = dict(base="b", run="r", adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
              grid_examples={}, verdict_window={}, evals={}, spend_usd=0, rogue_ledger_ts=0, reason="")
    vs.append(release_row(week="W1", decision="released", version="kannaka-brain-v3", previous=None, **kw))
    vs.append(release_row(week="W2", decision="released", version="kannaka-brain-v4",
                          previous="kannaka-brain-v3", **kw))
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_started",
                 incumbent="v1-sandbox")
    row = rollback(cfg, FakeRemote({}), local_run=lambda c: 0)
    assert row["decision"] == "rolled_back"
    assert row["reason"] == ("rollback from kannaka-brain-v4; prompt A/B v2-terse was open and is "
                             "now confounded: ok")

    # D5: the exit-code contract keys on the reason ENDING in ": flip_failed", so the note must be
    # spliced BEFORE the outcome, never appended after it. Mutation: append the note at the end
    # instead -> a rollback that did not roll back exits 0 and the operator is never summoned.
    r = FakeRemote({}, fail={"ollama cp"})
    row = rollback(cfg, r, local_run=lambda c: 0)
    assert row["reason"].endswith(": flip_failed") and "now confounded" in row["reason"]
    from grid.weekly import _exit_code
    assert _exit_code(row) == 3


def test_status_scopes_to_its_dimension_and_never_hides_a_prompt_experiment(tmp_path, monkeypatch, capsys):
    # `--dimension` is a parser-wide argument, so `status --dimension prompt` parsed, exited 0 and
    # printed the MODEL's answers -- a wrong answer to a precise question -- and bare `status`,
    # the one command an operator runs to ask what is going on, did not mention a prompt A/B
    # splitting live traffic at all. Mutation: restore `"ab": ab_state(v)` -> the prompt entry is
    # gone from the bare form and `--dimension prompt` reports the model's A/B as its own.
    import grid.weekly as weekly_mod
    from grid.weekly import main
    _ab_cli_env(tmp_path, monkeypatch, "MIND_MODEL_ALIAS=kannaka-brain-v3\nMIND_PROMPT_VARIANT=v1-sandbox\n")
    monkeypatch.setattr(weekly_mod, "find_candidate", lambda remote, cfg: None)   # no ssh in a test
    _seed_started_ab(tmp_path, 1_000_000_000)                      # a running MODEL A/B
    _seed_ab_row(tmp_path, dimension="prompt", version="v2-terse", decision="ab_started",
                 incumbent="v1-sandbox")                           # ...and a running PROMPT one
    assert main(["status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["released"] is None and "candidate" in out          # both still MODEL facts
    assert out["ab"]["model"]["candidate"] == "kannaka-brain-v4"
    assert out["ab"]["prompt"]["candidate"] == "v2-terse"

    assert main(["status", "--dimension", "prompt"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ab"]["candidate"] == "v2-terse"
    # `candidate` is Rogue's promotion state, a model fact no other dimension has: left off
    # rather than faked. Mutation: report it regardless of dimension -> a model tag on a prompt
    # answer, which is the falsehood every dimension filter in this module exists to prevent.
    assert "candidate" not in out
