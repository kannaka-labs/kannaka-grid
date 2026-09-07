"""Weekly release gate, alias flip, and rollback (spec §3-§5, §7).

`rogue.weekly` on debain2 trains and promotes; this runs afterwards on Skywave,
probes the promoted candidate, ledgers the decision, and moves the alias the
colony relay follows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .proposal_log import ProposalLog
from .remote import Remote, RemoteError
from .verdicts import Ledger
from .versions import Versions, decide, release_row
from .weekly_probe import chat, run_identity, run_validity

log = logging.getLogger("grid.weekly")
A100_USD_PER_H = 2.49        # run_qbraid's gpu-a100-sxm rate; the manifest records seconds, not dollars

AB_SPLIT = 0.2
MIN_SAMPLE = 100
TOLERANCE = 0.10
MAX_WEEKS = 2
STUCK_AFTER_S = 48 * 3600
STUCK_INCUMBENT_MIN = 50
POSITIVE_TIERS = ("reproduced", "growing", "enduring", "established")
EXCLUDED_TIERS = ("preexisting", "unknown", "rejected")


@dataclass(frozen=True)
class WeeklyConfig:
    debain2_host: str
    releases_dir: Path
    rogue_current_model: str
    rogue_ledger: str
    rogue_runs_dir: str
    gateway_url: str
    key_file: Path
    verdicts_dir: Path
    grid_env: Path
    current_alias: str
    probe_timeout_s: float
    identity_runs: int

    @classmethod
    def from_env(cls) -> "WeeklyConfig":
        env = os.environ.get
        colony = env("COLONY_SLUG", "colony-one")
        return cls(debain2_host=env("DEBAIN2_HOST", "debain2"),
                   releases_dir=Path(env("RELEASES_DIR", "/srv/grid/releases")),
                   rogue_current_model=env("ROGUE_CURRENT_MODEL", "/srv/rogue/current-model"),
                   rogue_ledger=env("ROGUE_LEDGER", "/srv/rogue/ledger.jsonl"),
                   rogue_runs_dir=env("ROGUE_RUNS_DIR", "/home/nick/.kannaka-corpus/runs"),
                   gateway_url=env("MIND_LLM_URL", "http://10.30.0.30:4000/v1"),
                   key_file=Path(env("MIND_LLM_KEY_FILE", f"/srv/grid/{colony}/mind-llm.key")),
                   verdicts_dir=Path(env("VERDICTS_DIR", f"/srv/grid/{colony}/verdicts")),
                   grid_env=Path(env("GRID_ENV", "/srv/grid/grid.env")),
                   current_alias=env("CURRENT_ALIAS", "kannaka-brain-current"),
                   probe_timeout_s=float(env("PROBE_TIMEOUT_S", "900")),
                   # 10 questions x this many runs = the identity ask count. On a CPU model
                   # each ask is ~1-2 min, so 3 runs (30 asks) keeps a release under ~1 h.
                   identity_runs=int(env("IDENTITY_PROBE_RUNS", "3")))


def _env_get(path: Path, key: str) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return None


def _env_set(path: Path, key: str, value: str) -> None:
    lines, done = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            lines.append(f"{key}={value}"); done = True
        else:
            lines.append(line)
    if not done:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_dir(cfg: WeeklyConfig, run: str) -> str:
    """The absolute run directory. rogue.weekly's ledger records `run` as a full
    path (verified on debain2: /home/nick/.kannaka-corpus/runs/gpu-a100-sxm-...),
    but a bare run name is also accepted, so join it under rogue_runs_dir only
    when it is not already absolute."""
    return run if run.startswith("/") else f"{cfg.rogue_runs_dir}/{run}"


def find_candidate(remote, cfg: WeeklyConfig) -> dict | None:
    try:
        tag = remote.read(cfg.rogue_current_model).strip()
        rows = [json.loads(l) for l in remote.read(cfg.rogue_ledger).splitlines() if l.strip()]
    except (RemoteError, ValueError) as e:
        log.error("cannot read the Rogue promotion state: %s", e)
        return None
    promos = [r for r in rows if r.get("event") == "promotion" and r.get("model") == tag]
    if not tag or not promos:
        return None
    p = promos[-1]
    run = p.get("run") or ""
    try:
        manifest = json.loads(remote.read(f"{_run_dir(cfg, run)}/train.manifest.json"))
    except (RemoteError, ValueError):
        manifest = {}
    return {"tag": tag, "run": run, "rogue_ts": float(p.get("ts", 0)), "manifest": manifest}


def hashes(remote, cfg: WeeklyConfig, run: str) -> dict:
    """sha256 of the adapter and the served GGUF, computed on debain2; nothing is copied."""
    out = {"adapter_sha256": "", "gguf_sha256": "", "set_manifest_sha256": ""}
    r = remote.run(f"sha256sum {shlex.quote(_run_dir(cfg, run) + '/adapter/adapter_model.safetensors')}")
    if r.returncode == 0:
        out["adapter_sha256"] = r.stdout.split()[0]
    # rogue.weekly writes no manifest into the run directory itself for the weekly SFT
    # set it trained on; it names that data dir from the run's own trailing timestamp
    # stamp (`weekly-<stamp>-data`, a sibling of the runs dir). Best-effort: if the
    # run name carries no such stamp, or the guessed path is wrong, this stays "".
    m = re.search(r"(\d{8}-\d{4})$", run)
    if m:
        data_dir = f"{cfg.rogue_runs_dir}/../weekly-{m.group(1)}-data/manifest.json"
        r = remote.run(f"sha256sum {shlex.quote(data_dir)}")
        if r.returncode == 0 and r.stdout.strip():
            out["set_manifest_sha256"] = r.stdout.split()[0]
    return out


def gguf_hash(remote, tag: str) -> str:
    r = remote.run(f"ollama show --modelfile {shlex.quote(tag)}")
    m = re.search(r"^FROM\s+(\S+)", r.stdout, re.M)
    if r.returncode != 0 or not m:
        return ""
    r = remote.run(f"sha256sum {shlex.quote(m.group(1))}", timeout=900)
    return r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else ""


_WILDCARD_MODEL_LISTS = (["all-team-models"], ["*"])


def key_models(cfg: WeeklyConfig, remote) -> list[str] | None:
    """Best-effort read of the colony key's allowed models from the gateway (spec §5: the
    release step verifies the candidate tag is on the key before flipping). Returns None
    when it cannot be determined (unreadable key file, curl failure, malformed reply) —
    callers must not block on that; a real auth failure still surfaces during the probe.
    """
    try:
        key = cfg.key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    base = cfg.gateway_url[: -len("/v1")] if cfg.gateway_url.endswith("/v1") else cfg.gateway_url.rstrip("/")
    cmd = f"curl -sf -H {shlex.quote('Authorization: Bearer ' + key)} {shlex.quote(base + '/key/info')}"
    r = remote.run(cmd)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        models = json.loads(r.stdout).get("info", {}).get("models")
    except (ValueError, AttributeError, TypeError):
        return None
    return models if isinstance(models, list) else None


def colony_ab(verdicts_dir: Path) -> dict:
    by: dict[str, dict] = {}
    for row in Ledger(Path(verdicts_dir) / "verdicts.jsonl").rows():
        a = by.setdefault(str(row.get("model_alias", "")), {"proposals": 0, "births": 0, "reproduced": 0})
        a["proposals"] += 1
        a["births"] += int(row.get("births") or 0)
        a["reproduced"] += 1 if row.get("tier") in ("reproduced", "growing", "enduring", "established") else 0
    return {"advisory": True, "by_alias": {
        k: {"proposals": v["proposals"], "births_per_proposal": v["births"] / v["proposals"],
            "reproduced_rate": v["reproduced"] / v["proposals"]} for k, v in by.items() if v["proposals"]}}


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
    """A tag is lost iff its most recent lost/stuck/inconclusive-at-max `held` row comes
    after its most recent operator-forget `unchanged` row (spec §8): forgetting makes it
    retestable until it loses, gets stuck, or exhausts MAX_WEEKS inconclusive again.
    (Controller ruling: a tag that could never reach sample size in MAX_WEEKS is not
    auto-retried either — that would livelock the gate re-running the same undersampled
    candidate every week.)"""
    lost_at: dict[str, float] = {}
    forgotten_at: dict[str, float] = {}
    for r in versions.rows():
        tag = r.get("version")
        if not tag:
            continue
        if r["decision"] == "held" and str(r.get("reason", "")).startswith(
                ("colony A/B lost", "colony A/B stuck", "colony A/B inconclusive")):
            lost_at[tag] = r["ts"]
        elif r["decision"] == "unchanged" and str(r.get("reason", "")).startswith("operator forgot"):
            forgotten_at[tag] = r["ts"]
    return {tag for tag, ts in lost_at.items() if ts > forgotten_at.get(tag, float("-inf"))}


def flip(cfg: WeeklyConfig, remote, version: str, *, local_run, dry_run: bool = False) -> str:
    """Spec §7 order: local env, remote tag copy, relay restart.

    `dry_run` performs no real mutation: `remote.run` is already a no-op when the Remote
    itself is dry-run, but `_env_set` writes `cfg.grid_env` directly on disk and
    `local_run` may be the real `subprocess.run` restart — neither is safe to call for
    real here, so both are skipped (and logged) when `dry_run` is true.
    """
    before = _env_get(cfg.grid_env, "MIND_MODEL_ALIAS")
    before_cand = _env_get(cfg.grid_env, "MIND_LLM_CANDIDATE")
    if dry_run:
        log.info("[dry-run] flip: would set MIND_MODEL_ALIAS=%s in %s", version, cfg.grid_env)
    else:
        _env_set(cfg.grid_env, "MIND_MODEL_ALIAS", version)
        # A flip always ends any colony A/B in progress: the relay is about to serve
        # `version` outright, so a stray candidate split would fight the new alias.
        _env_set(cfg.grid_env, "MIND_LLM_CANDIDATE", "")
    r = remote.run(f"ollama cp {shlex.quote(version)} {shlex.quote(cfg.current_alias)}", timeout=600)
    if r.returncode != 0:
        # C1: the relay is never restarted on a failed tag copy, so it keeps running
        # whatever it last loaded -- both env lines must revert together, or the file
        # would claim "no A/B" while the live relay keeps splitting to the candidate.
        if not dry_run:
            if before is not None:
                _env_set(cfg.grid_env, "MIND_MODEL_ALIAS", before)
            if before_cand is not None:
                _env_set(cfg.grid_env, "MIND_LLM_CANDIDATE", before_cand)
        log.error("tag copy failed: %s", r.stderr.strip())
        return "flip_failed"
    # The relay already asks cfg.current_alias (one-time install); a release only changes
    # the author label in grid.env and restarts the relay so it reads it.
    cmd = ["sudo", "-n", "systemctl", "restart", "grid-odin.service"]
    if dry_run:
        log.info("[dry-run] flip: would run %s", " ".join(cmd))
        return "ok"
    for attempt in (1, 2):
        if local_run(cmd) == 0:
            return "ok"
        log.error("relay restart attempt %d failed", attempt)
    return "flip_incomplete"


def _restart_relay(local_run) -> str:
    """C2/I2: mirror flip()'s own restart retry (same -n sudoers line, same two attempts)
    so a failed restart is reported, not silently swallowed."""
    cmd = ["sudo", "-n", "systemctl", "restart", "grid-odin.service"]
    for attempt in (1, 2):
        if local_run(cmd) == 0:
            return "ok"
        log.error("relay restart attempt %d failed", attempt)
    return "flip_incomplete"


def start_ab(cfg: WeeklyConfig, *, candidate: str, incumbent: str, local_run, dry_run: bool = False,
            now_ms: int | None = None) -> tuple[dict, str]:
    """Spec §8: probes passed, but a release is now a binding colony A/B, not a flip.

    Returns `(block, outcome)`: `outcome` is "ok" or "flip_incomplete" (C2) -- a caller
    that ignores it would otherwise record `ab_started` even when the relay never picked
    up the candidate, silently blocking that arm from ever running.
    """
    since = now_ms if now_ms is not None else int(time.time() * 1000)
    block = {"binding": True, "candidate": candidate, "incumbent": incumbent, "split": AB_SPLIT, "since_ts": since,
             "min_sample": MIN_SAMPLE, "tolerance": TOLERANCE, "weeks": 0, "arms": {}, "winner": None}
    if dry_run:
        return block, "ok"
    _env_set(cfg.grid_env, "MIND_LLM_CANDIDATE", candidate)
    _env_set(cfg.grid_env, "MIND_AB_SPLIT", str(AB_SPLIT))
    return block, _restart_relay(local_run)


def end_ab(cfg: WeeklyConfig, *, local_run, dry_run: bool = False) -> str:
    """Clear the candidate split (win/loss/inconclusive-at-max/stuck/abort all land here).

    Returns the same "ok" / "flip_incomplete" outcome as `start_ab` (C2): a caller that
    ignores it would record the A/B as closed even while the losing candidate keeps its
    share of live traffic because the relay was never restarted.
    """
    if dry_run:
        return "ok"
    _env_set(cfg.grid_env, "MIND_LLM_CANDIDATE", "")
    return _restart_relay(local_run)


def _iso_week() -> str:
    return time.strftime("%G-W%V")


async def release(cfg: WeeklyConfig, remote, *, ask_factory, local_run, week: str | None = None,
                  dry_run: bool = False, key_models=key_models) -> dict:
    week = week or _iso_week()
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    last = versions.last_released()
    released = last["version"] if last else None
    state = ab_state(versions)
    if state is not None:
        # A colony A/B is running: this Sunday is the binding verdict, not a probe day
        # (spec §8) — no candidate discovery, no key check, no probes.
        arms = ab_arms(cfg.verdicts_dir, since_ts=state["since_ts"], incumbent=state["incumbent"], candidate=state["candidate"])
        verdict, reason = ab_decide(arms, incumbent=state["incumbent"], candidate=state["candidate"])
        # I3: an ab_started row with no evals (e.g. hand-edited, or a future writer that
        # omits it) must not KeyError here.
        started_ab = (state["started_row"].get("evals") or {}).get("colony_ab") or {}
        block = {**started_ab, "binding": True, "arms": arms, "weeks": state["weeks"] + 1, "winner": None}
        common = dict(week=week, base="", run="", grid_examples={}, verdict_window=_window(cfg.verdicts_dir), rogue_ledger_ts=0.0,
                      adapter_sha256="", gguf_sha256="", set_manifest_sha256="", spend_usd=0.0)
        if verdict == "inconclusive" and state["weeks"] + 1 >= MAX_WEEKS:
            outcome = end_ab(cfg, local_run=local_run, dry_run=dry_run)
            base_reason = f"colony A/B inconclusive after {MAX_WEEKS} weeks: {reason}"
            if outcome == "ok":
                decision, why = "held", base_reason
            else:
                decision, why = "flip_incomplete", f"{base_reason}; relay restart failed clearing the A/B"
            row = release_row(decision=decision, version=state["candidate"], previous=released, evals={"colony_ab": block},
                              reason=why, **common)
        elif verdict == "inconclusive":
            row = release_row(decision="ab_inconclusive", version=state["candidate"], previous=released,
                              evals={"colony_ab": block}, reason=reason, **common)
        elif verdict == "win":
            block["winner"] = state["candidate"]
            outcome = flip(cfg, remote, state["candidate"], local_run=local_run, dry_run=dry_run)
            if outcome == "ok":
                decision, why = "released", reason
            elif outcome == "flip_failed":
                decision, why = "held", "flip failed: tag copy"
            else:
                decision, why = "flip_incomplete", "alias serves the candidate; relay restart failed twice"
            row = release_row(decision=decision, version=state["candidate"], previous=released,
                              evals={"colony_ab": block}, reason=why, **common)
        else:
            outcome = end_ab(cfg, local_run=local_run, dry_run=dry_run)
            if outcome == "ok":
                decision, why = "held", reason
            else:
                decision, why = "flip_incomplete", f"{reason}; relay restart failed clearing the A/B"
            row = release_row(decision=decision, version=state["candidate"], previous=released,
                              evals={"colony_ab": block}, reason=why, **common)
        return _ledger(versions, row, dry_run=dry_run)
    cand = find_candidate(remote, cfg)
    common = dict(week=week, base=(cand or {}).get("manifest", {}).get("base", ""), run=(cand or {}).get("run", ""),
                  grid_examples={}, verdict_window=_window(cfg.verdicts_dir), rogue_ledger_ts=(cand or {}).get("rogue_ts", 0.0))
    if cand is None or cand["tag"] == released:
        decision, reason = decide(candidate=(cand or {}).get("tag"), released=released, identity=True, validity=True)
        row = release_row(decision=decision, version=(cand or {}).get("tag"), previous=released, adapter_sha256="", gguf_sha256="",
                          set_manifest_sha256="", evals={"colony_ab": colony_ab(cfg.verdicts_dir)}, spend_usd=0.0, reason=reason, **common)
        return _ledger(versions, row, dry_run=dry_run)
    if cand["tag"] in lost_tags(versions):
        decision = "held"
        reason = f"{cand['tag']} already lost/stuck the colony A/B; run `grid.weekly ab --forget {cand['tag']}` to retest"
        row = release_row(decision=decision, version=cand["tag"], previous=released, adapter_sha256="", gguf_sha256="",
                          set_manifest_sha256="", evals={"colony_ab": colony_ab(cfg.verdicts_dir)}, spend_usd=0.0, reason=reason, **common)
        return _ledger(versions, row, dry_run=dry_run)
    # I1: the probes ask the concrete candidate tag; the colony key may not list it yet if it
    # was minted before this vN existed. Verify first — a candidate off the key would just
    # make the gateway reject the probe, which C1 would then (correctly, but confusingly) hold
    # as a probe failure. Skip the check (proceed) when the key's model list can't be read.
    models = key_models(cfg, remote)
    if models is not None and cand["tag"] not in models and models not in _WILDCARD_MODEL_LISTS:
        decision, reason = "held", f"candidate {cand['tag']} not on the colony key"
        row = release_row(decision=decision, version=cand["tag"], previous=released, adapter_sha256="", gguf_sha256="",
                          set_manifest_sha256="", evals={"colony_ab": colony_ab(cfg.verdicts_dir)}, spend_usd=0.0, reason=reason, **common)
        return _ledger(versions, row, dry_run=dry_run)
    ask = ask_factory(cand["tag"])
    evals = {"holdout_ppl": cand["manifest"].get("holdout_ppl", {}), "colony_ab": colony_ab(cfg.verdicts_dir)}
    try:
        # C1: any probe failure (gateway error, timeout, malformed or empty completion) must
        # record `held`, never crash out of `release` (spec §7).
        evals["identity"] = identity = await run_identity(ask, runs=cfg.identity_runs)
        evals["validity"] = validity = await run_validity(ask, cfg.verdicts_dir)
    except Exception as e:
        decision, reason = "held", f"probe failed: {e}"
        evals["probe_error"] = str(e)
        row = release_row(decision=decision, version=cand["tag"], previous=released, adapter_sha256="", gguf_sha256="",
                          set_manifest_sha256="", evals=evals, spend_usd=0.0, reason=reason, **common)
        return _ledger(versions, row, dry_run=dry_run)
    spend = float(cand["manifest"].get("seconds", 0)) / 3600 * A100_USD_PER_H
    h = hashes(remote, cfg, cand["run"])
    h["gguf_sha256"] = gguf_hash(remote, cand["tag"])
    decision, reason = decide(candidate=cand["tag"], released=released, identity=identity["pass"], validity=validity["pass"])
    if decision == "released":
        # spec §8: probes passed, but a release is now a binding colony A/B, not a flip.
        incumbent = _env_get(cfg.grid_env, "MIND_MODEL_ALIAS") or released
        block, outcome = start_ab(cfg, candidate=cand["tag"], incumbent=incumbent, local_run=local_run, dry_run=dry_run)
        evals["colony_ab"] = block
        if outcome == "ok":
            decision, reason = "ab_started", "probes passed; colony A/B started"
        else:
            # C2: the candidate's arm never ran, so it must not be recorded as started --
            # otherwise ab_check would falsely declare it stuck 48h later and block it.
            decision, reason = "flip_incomplete", "probes passed but the relay restart failed twice starting the colony A/B"
    row = release_row(decision=decision, version=cand["tag"], previous=released, evals=evals, spend_usd=spend, reason=reason, **h, **common)
    return _ledger(versions, row, dry_run=dry_run)


def _window(verdicts_dir: Path) -> dict:
    led = Ledger(Path(verdicts_dir) / "verdicts.jsonl")
    rows = led.rows()
    return {"since_ts": None, "until_ts": None, "chain_head": led.head(), "rows": len(rows)}


def _ledger(versions: Versions, row: dict, *, dry_run: bool = False) -> dict:
    if not dry_run:
        with versions.lock():
            versions.append(row)
    log.info("%s: %s (%s)", row["week"], row["decision"], row["reason"])
    return row


def ab_check(cfg: WeeklyConfig, *, now_ms: int | None = None, local_run, dry_run: bool = False) -> dict | None:
    """The daily stuck check (spec §8): abort a dead A/B within 48 h instead of waiting for
    Sunday. Returns None when no A/B is running, or when it isn't (yet) stuck."""
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    state = ab_state(versions)
    if state is None:
        return None
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    arms = ab_arms(cfg.verdicts_dir, since_ts=state["since_ts"], incumbent=state["incumbent"], candidate=state["candidate"])
    if not is_stuck(arms, since_ts=state["since_ts"], now_ms=now, incumbent=state["incumbent"], candidate=state["candidate"]):
        return None
    outcome = end_ab(cfg, local_run=local_run, dry_run=dry_run)
    started_ab = (state["started_row"].get("evals") or {}).get("colony_ab") or {}   # I3
    block = {**started_ab, "binding": True, "arms": arms, "weeks": state["weeks"], "winner": None}
    last = versions.last_released()
    base_reason = f"colony A/B stuck: candidate 0 settled in 48h (incumbent {arms[state['incumbent']]['settled']})"
    if outcome == "ok":
        decision, reason = "held", base_reason
    else:
        decision, reason = "flip_incomplete", f"{base_reason}; relay restart failed clearing the A/B"
    row = release_row(week=_iso_week(), decision=decision, version=state["candidate"], previous=last["version"] if last else None,
                      base="", run="", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={},
                      verdict_window=_window(cfg.verdicts_dir), evals={"colony_ab": block}, spend_usd=0.0, rogue_ledger_ts=0.0,
                      reason=reason)
    return _ledger(versions, row, dry_run=dry_run)


def ab_forget(cfg: WeeklyConfig, tag: str, *, dry_run: bool = False) -> dict:
    """Operator override: make a lost/stuck tag retestable again (spec §8)."""
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    row = release_row(week=_iso_week(), decision="unchanged", version=tag, previous=None, base="", run="",
                      adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={},
                      evals={}, spend_usd=0.0, rogue_ledger_ts=0.0, reason=f"operator forgot the colony A/B loss of {tag}")
    return _ledger(versions, row, dry_run=dry_run)


def rollback(cfg: WeeklyConfig, remote, *, to: str | None = None, local_run, dry_run: bool = False) -> dict:
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    last = versions.last_released()
    if last is None:
        raise SystemExit("nothing has been released; nothing to roll back")
    if to is None:
        # I2: `last["previous"]` is the version this release/rollback actually superseded;
        # `previous_released(before=...)` walks release-order and would return a version that
        # was itself later rolled back as bad (e.g. after release v3, release v4, rollback to
        # v3, release v5: the default target must be v3, not v4).
        prev_version = last.get("previous")
        if prev_version is None:
            raise SystemExit("nothing to roll back to")
        matches = [r for r in versions.rows() if r["decision"] in ("released", "rolled_back") and r["version"] == prev_version]
        target = matches[-1] if matches else None
    else:
        target = next((r for r in versions.rows() if r["decision"] == "released" and r["version"] == to), None)
    if target is None:
        raise SystemExit(f"{to or 'the previous release'} was never released; refusing")
    # I1: only a running A/B needs ending -- otherwise this doubles the relay restart
    # and appends a spurious empty MIND_LLM_CANDIDATE= line on every rollback.
    if _env_get(cfg.grid_env, "MIND_LLM_CANDIDATE"):
        end_ab(cfg, local_run=local_run, dry_run=dry_run)
    outcome = flip(cfg, remote, target["version"], local_run=local_run, dry_run=dry_run)
    decision = "rolled_back" if outcome == "ok" else outcome
    row = release_row(week=_iso_week(), decision=decision if decision in ("rolled_back", "flip_incomplete") else "held",
                      version=target["version"], previous=last["version"], base=target.get("base", ""), run=target.get("run", ""),
                      adapter_sha256=target.get("adapter_sha256", ""), gguf_sha256=target.get("gguf_sha256", ""),
                      set_manifest_sha256=target.get("set_manifest_sha256", ""), grid_examples={}, verdict_window=_window(cfg.verdicts_dir),
                      evals={}, spend_usd=0.0, rogue_ledger_ts=0.0, reason=f"rollback from {last['version']}: {outcome}")
    return _ledger(versions, row, dry_run=dry_run)


def _local_run(cmd: list[str]) -> int:
    return subprocess.run(cmd, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="grid.weekly")
    ap.add_argument("verb", choices=("release", "rollback", "status", "ab"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--week", default=None)
    ap.add_argument("--to", default=None)
    ab_mode = ap.add_mutually_exclusive_group()
    ab_mode.add_argument("--abort", action="store_true")
    ab_mode.add_argument("--check", action="store_true")
    ab_mode.add_argument("--forget", default=None, metavar="TAG")
    a = ap.parse_args(argv)
    cfg = WeeklyConfig.from_env()
    cfg.releases_dir.mkdir(parents=True, exist_ok=True)
    remote = Remote(cfg.debain2_host, dry_run=a.dry_run, log=lambda s: log.info("%s", s))
    if a.verb == "status":
        v = Versions(cfg.releases_dir / "versions.jsonl")
        print(json.dumps({"released": (v.last_released() or {}).get("version"), "rows": len(v.rows()), "verify": v.verify(),
                          "candidate": find_candidate(remote, cfg), "ab": ab_state(v)}, indent=1, default=str))
        return 0
    local_run = (lambda cmd: (log.info("[dry-run] %s", " ".join(cmd)) or 0)) if a.dry_run else _local_run
    if a.verb == "ab":
        versions = Versions(cfg.releases_dir / "versions.jsonl")
        if a.forget:
            row = ab_forget(cfg, a.forget, dry_run=a.dry_run)
            print(json.dumps(row, default=str)); return 0
        if a.abort:
            state = ab_state(versions)
            if state is None:                                    # I4: mirror --check's guard
                print("no A/B running"); return 0
            outcome = end_ab(cfg, local_run=local_run, dry_run=a.dry_run)
            last = versions.last_released()
            if outcome == "ok":
                decision, reason = "held", "colony A/B aborted by operator"
            else:
                decision, reason = "flip_incomplete", "colony A/B abort requested but the relay restart failed"
            row = release_row(week=_iso_week(), decision=decision, version=state["candidate"], previous=last["version"] if last else None,
                              base="", run="", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={},
                              verdict_window=_window(cfg.verdicts_dir), evals={}, spend_usd=0.0, rogue_ledger_ts=0.0,
                              reason=reason)
            row = _ledger(versions, row, dry_run=a.dry_run)
            print(json.dumps(row, default=str)); return 0
        if a.check:
            state = ab_state(versions)
            if state is None:
                print("no A/B running"); return 0
            row = ab_check(cfg, local_run=local_run, dry_run=a.dry_run)
            print("not stuck" if row is None else json.dumps(row, default=str))
            return 0
        state = ab_state(versions)
        if state is None:
            print(json.dumps({"ab": None}, default=str))
        else:
            arms = ab_arms(cfg.verdicts_dir, since_ts=state["since_ts"], incumbent=state["incumbent"], candidate=state["candidate"])
            print(json.dumps({"ab": state, "arms": arms}, default=str))
        return 0
    key = cfg.key_file.read_text(encoding="utf-8").strip()
    if a.verb == "rollback":
        row = rollback(cfg, remote, to=a.to, local_run=local_run, dry_run=a.dry_run)
        print(json.dumps(row, default=str)); return 0
    ask_factory = lambda model: (lambda prompt: chat(cfg.gateway_url, key, model, prompt, timeout_s=cfg.probe_timeout_s))
    if a.dry_run:
        async def fake(prompt): return "I am Kannaka. []"
        ask_factory = lambda model: fake
    row = asyncio.run(release(cfg, remote, ask_factory=ask_factory, local_run=local_run, week=a.week, dry_run=a.dry_run))
    print(json.dumps({k: row[k] for k in ("week", "decision", "version", "previous", "reason", "spend_usd", "over_budget")}, default=str))
    return 0 if row["decision"] != "flip_incomplete" else 3


if __name__ == "__main__":
    sys.exit(main())
