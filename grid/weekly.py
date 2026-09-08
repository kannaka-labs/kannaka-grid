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

from .odin import DEFER_BIN_REASON_RE, DIMENSIONS, UNSALTED_DIMENSION
from .proposal_log import ProposalLog, variant_of
from .remote import Remote, RemoteError
from .variants import get_variant
from .verdicts import Ledger
from .versions import Versions, decide, dimension_of, release_row
from .weekly_probe import chat, run_identity, run_validity

log = logging.getLogger("grid.weekly")
A100_USD_PER_H = 2.49        # run_qbraid's gpu-a100-sxm rate; the manifest records seconds, not dollars

AB_SPLIT = 0.2
MIN_SAMPLE = 100
TOLERANCE = 0.10
MAX_WEEKS = 2
STUCK_AFTER_S = 48 * 3600
STUCK_INCUMBENT_MIN = 50
VALIDITY_MIN = 0.5
VALIDITY_MIN_ANSWERED = 20
POSITIVE_TIERS = ("reproduced", "growing", "enduring", "established")
EXCLUDED_TIERS = ("preexisting", "unknown", "rejected")

#: The three grid.env lines one dimension's experiment is run through: what the relay serves
#: now, what it is splitting a share of traffic to, and how big that share is. Keyed by odin's
#: own dimension names, so the gate cannot start an A/B on a knob the relay does not read.
_DIMENSION_ENV = {
    UNSALTED_DIMENSION: {"live": "MIND_MODEL_ALIAS", "candidate": "MIND_LLM_CANDIDATE", "split": "MIND_AB_SPLIT"},
    "prompt": {"live": "MIND_PROMPT_VARIANT", "candidate": "MIND_PROMPT_CANDIDATE", "split": "MIND_PROMPT_AB_SPLIT"},
}


def _dimension_env(dimension: str) -> dict[str, str]:
    """This dimension's three grid.env keys, or a loud refusal.

    Two checks, and they are not the same check. DIMENSIONS is odin's allowlist -- the one that
    decides how a request is assigned to an arm -- so a name absent from it ("modle") is a typo
    that would silently salt the hash and reshuffle a live experiment. This table is the gate's
    own coverage: a dimension somebody adds to the allowlist but not here has no knob to turn,
    and falling through to the model row would run the whole experiment on the model alias while
    every ledger row named the other dimension. Neither case has a safe default.
    """
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}; known: {sorted(DIMENSIONS)}")
    if dimension not in _DIMENSION_ENV:
        raise ValueError(f"dimension {dimension!r} has no grid.env keys in grid.weekly")
    return _DIMENSION_ENV[dimension]


def _validate_candidate(dimension: str, name: str) -> None:
    """Refuse a candidate name this dimension cannot serve, before anything writes it.

    This is a production hazard, not tidiness. The gate writes the candidate into grid.env and
    restarts grid-odin; odin's variant loader fails CLOSED, so a name the registry does not hold
    takes the relay down at boot and keeps it down -- systemd restarts it into the same refusal
    until it hits the start limit and gives up. The colony's mind is then off for a week over a
    typed character, and the revert paths in `start_ab` cannot argue it back up, because the file
    they restore is not what the failing process is reading.

    Deliberately NOT the same guard as `candidate == incumbent`: that one refuses a name the
    registry holds perfectly well, and this one refuses a name it has never heard of. Each is
    silent about the other's case, so both are checked.

    `model` is exempt and not by omission. A model tag is not in any registry the gate can read
    -- it lives on debain2, in ollama -- and `find_candidate` reads a tag Rogue actually promoted
    rather than one somebody typed. A dimension in the allowlist with no rule here is refused
    outright, the same fail-closed direction `_dimension_env` takes: an unverified name written
    into grid.env is exactly the failure this function exists to prevent.
    """
    _dimension_env(dimension)                 # unknown dimension first, same refusal as everywhere
    if dimension == UNSALTED_DIMENSION:
        return
    if dimension == "prompt":
        get_variant(name)                     # VariantError (a ValueError) names the offending id
        return
    raise ValueError(f"grid.weekly has no candidate check for dimension {dimension!r}; refusing "
                     "to write a name the relay may refuse at boot")


def _refuse_given_model_candidate(dimension: str, candidate: str | None) -> None:
    """The model dimension discovers its own candidate; a given one is refused, not ignored.

    One function, two callers, because the rule and its wording must not drift: `release` refuses
    it at the top of the Sunday run (an A/B week returns long before the start path, so a guard
    down there would ignore the argument in exactly the case an operator is most likely to have
    got wrong), and `main` refuses it before it consults the ledger at all -- whether an argument
    is legal cannot depend on what some other experiment happens to be doing.
    """
    if dimension == UNSALTED_DIMENSION and candidate is not None:
        raise ValueError("the model dimension discovers its own candidate from Rogue's promotion "
                         f"state; refusing to silently ignore the given {candidate!r}")


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
    """The value exactly as the file has it.

    N3: it MUST NOT strip an inline ` # comment`. `RelayConfig.from_env` reads os.environ and
    strips nothing, so any cleaning here makes the gate and the relay disagree about a value's
    text by construction -- and I2's guard compares gate-read to gate-read, so it is the one
    divergence I2 cannot see. Gate and relay read identically; a commented VALUE line is a
    fault to be kept out of the file (see test_grid_env_example_has_no_inline_comments), not
    something to paper over here.
    """
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


def _env_unset(path: Path, key: str) -> None:
    """Drop the line entirely. C1: `MIND_AB_SPLIT=` (present but empty) is NOT the same as
    absent -- os.environ.get returns "" for it, and the relay's float() of that is fatal."""
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.startswith(key + "=")]
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")


def _env_restore(path: Path, key: str, before: str | None) -> None:
    """Put a line back exactly as it was, including not being there at all.

    C1's rule, applied to a revert: present-but-empty and absent are different values to
    `RelayConfig.from_env`, so a key that was ABSENT is restored by removing it again, never
    by blanking it -- `MIND_PROMPT_AB_SPLIT=` is a ValueError at relay boot where no line at
    all is the documented default.
    """
    if before is None:
        _env_unset(path, key)
    else:
        _env_set(path, key, before)


def _live_arm(cfg: WeeklyConfig, dimension: str) -> str | None:
    """What the relay is serving in this dimension, as the LABEL the proposal log carries.

    For `model` that is the grid.env line verbatim, unchanged: `None` when the line is missing
    is the signal the incumbent-relabelled guard has always read.

    For `prompt` a missing or empty MIND_PROMPT_VARIANT does not mean "unknown". odin reads it
    as "the registry default" and logs every row under THAT variant's id, so resolving it here
    is what makes the gate's incumbent and the log's label the same string. Left unresolved,
    every ordinary prompt A/B week -- the common case, since the line is absent until the first
    prompt release writes it -- would trip the relabelled-incumbent branch and refuse to judge
    a perfectly healthy experiment until MAX_WEEKS killed it.
    """
    live = _env_get(cfg.grid_env, _dimension_env(dimension)["live"])
    if dimension == UNSALTED_DIMENSION:
        return live
    return live or get_variant().id


def _last_released(versions: Versions, *, dimension: str) -> dict | None:
    """The last release IN THIS DIMENSION.

    `Versions.last_released()` walks every row, which was exactly right while `model` was the
    only experiment there was. Unfiltered, a prompt row's `previous` would name a model tag --
    a plain falsehood in an append-only ledger -- and the "already released" check would compare
    a variant id against a model tag and never fire. For `model` this is the same walk it always
    was, since every row written before Task 4 reads as a model row.
    """
    rel = [r for r in versions.rows()
           if r["decision"] in ("released", "rolled_back") and dimension_of(r) == dimension]
    return rel[-1] if rel else None


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


def _log_arm(dimension: str, entry: dict) -> str:
    """Which arm one proposal-log entry belongs to, in one dimension.

    `model` reads the alias field it always has. Every other dimension reads its own
    attribution through the proposal_log accessor, which returns "" for a row written before
    that attribution existed -- see the exclusion in `ab_arms`, which is what "" must mean.
    """
    if dimension == UNSALTED_DIMENSION:
        return str(entry.get("model_alias", ""))
    return variant_of(entry)


#: The two dimensions a factorial run crosses, in the order a cell key spells them. Not
#: `sorted(DIMENSIONS)`: a third dimension added to odin's allowlist must NOT silently become a
#: third factor -- 2x2x2 is eight cells, the marginal maths below assumes two levels on two axes,
#: and the sample a colony week produces cannot support it. A third factor is a deliberate edit.
FACTORIAL_DIMENSIONS = (UNSALTED_DIMENSION, "prompt")
#: Joins the two labels of one cell. A model tag and a variant id are both `[A-Za-z0-9.-]`-ish
#: names, so this character cannot occur inside one; both `cell_key` and `_row_arm` refuse a
#: label carrying it rather than build a key that two different pairs could spell.
CELL_SEP = "|"


def cell_key(*labels: str) -> str:
    """The tally key for one factorial cell: `"<model>|<variant>"`."""
    for label in labels:
        if CELL_SEP in label:
            raise ValueError(f"an arm label may not contain {CELL_SEP!r}: {label!r} would make a "
                             "cell key two different pairs of arms could spell")
    return CELL_SEP.join(labels)


def _row_arm(dimensions: tuple[str, ...], entry: dict, verdict_row: dict | None = None) -> str:
    """The arm -- or, across two dimensions, the CELL -- one row belongs to. "" when it has none.

    One label per dimension, joined by CELL_SEP. A row missing ANY of them belongs to no arm at
    all. In a four-cell tally that rule is the one that matters most and the likeliest way to
    corrupt a factorial result silently: a row that names its model but predates the prompt
    registry is attributable in one dimension and not the other, so it cannot sit in any cell --
    and because a marginal here is only ever a SUM OF CELLS, excluding it from the cells is what
    keeps it out of both marginals too. It is never bucketed under a partial key.

    `verdict_row` keeps the model dimension's SETTLED tally reading the alias off the verdict row,
    exactly as it did before cells existed, so a factorial run's model marginal is measured from
    the same source a two-arm model run would have used.
    """
    parts = []
    for d in dimensions:
        label = (str(verdict_row.get("model_alias", "")) if verdict_row is not None and d == UNSALTED_DIMENSION
                 else _log_arm(d, entry))
        if not label or CELL_SEP in label:
            return ""
        parts.append(label)
    return CELL_SEP.join(parts)


#: "not given", which is NOT `None`. `ab_state` yields `incumbent=None` for an `ab_started` row
#: that never recorded one (hand-edited, or written by an older writer), and `ab_arms` has always
#: accepted that and simply matched no row against it. A `None` default here would make those two
#: cases the same and turn a real call into the "you must pass cells=" refusal below.
_UNSET: object = object()


def ab_arms(verdicts_dir: Path, *, since_ts: int, incumbent=_UNSET, candidate=_UNSET,
            dimension=_UNSET, cells: list[tuple[str, str]] | None = None) -> dict:
    """Per-arm colony outcome for proposals MADE since `since_ts` (the proposal's own ts,
    joined by request_id), so only asks that ran under the split count.

    `dimension` says which field names the arm. For `model` it is the alias, on the verdict row
    for the settled tally and on the log entry for the attempt tally, exactly as before. For
    `prompt` BOTH tallies read the log entry's variant, because a verdict row carries no variant
    at all -- the join is what attributes it.

    `cells` is the FACTORIAL mode and replaces `incumbent`/`candidate`/`dimension` rather than
    joining them: a cell IS a pair of labels, so a single dimension has no meaning here. Given
    the four `(model, variant)` pairs of a 2x2 run it returns four tallies keyed by `cell_key`,
    counted by exactly the same rules -- the same window, the same three-way attempt split, the
    same excluded tiers, the same refusal to invent an arm out of a missing label. `ab_decide_
    factorial` pools them into the two marginals; nothing decides on a cell.

    An entry with NO attribution in this dimension is counted in neither tally and can never
    form an arm, in EVERY dimension. "" means "this row cannot say which arm answered it", never
    "the arm whose name is empty". Bucketed, every unattributable row inside the window piles
    into one phantom arm -- and when the incumbent itself resolves to "" (an unreadable
    MIND_PROMPT_VARIANT, the env-only view), that pile is published as the incumbent's own
    numbers and decides the week.

    The prompt dimension is where this bites hardest, because the whole existing corpus predates
    the prompt registry and carries no variant field at all. But the model side had the same hole
    (N7, papered over by `_env_ab_view` popping the "" arm at the one view that could reach it),
    and guarding it at the counter rather than at one view costs nothing: an arm with no name is
    not an arm in either dimension. `_env_ab_view` keeps its pop, which now only ever removes a
    zeroed key.

    Two tallies, from two sources, because a bad answer leaves no trace in the first one.
    The verdict rows say what the colony did with the genomes an arm produced. The proposal
    log says how many asks the arm could answer AT ALL: a reply that fails validation never
    becomes a genome, so it never settles, so on verdicts alone an arm that cannot answer
    merely looks slow and reads `inconclusive` on sample size until the two-week cap.

    Attempts partition three ways, and the split matters:
      - `valid`: the reply parsed and became a proposal;
      - `answered`: the model replied at all, valid or not -- the denominator of `validity`;
      - `unanswered`: the ask was binned after repeated defers, so no reply ever arrived.
    Unanswered asks sit OUTSIDE both sides of the ratio. A gateway outage is an availability
    failure of the colony, and folding it in would report it as a wording failure of the arm.

    A rejection whose `reason` is missing counts as ANSWERED. `odin` never writes one -- every
    reject path there passes a reason -- so this is the hand-edit/foreign-writer case, and
    "she answered badly" is the reading that can cost the candidate the week. Guessing the
    other way would let a writer that omits reasons silence the floor entirely.

    An outcome this function does not recognise is counted in NONE of the three, and warned
    about once per distinct value. Silence is the safe direction (a smaller `answered` keeps
    the floor below VALIDITY_MIN_ANSWERED and quiet, rather than judging on a bucket the
    reader does not understand), but silently is not: the log says a producer moved ahead of
    the gate.
    """
    if cells is None:
        dimension = UNSALTED_DIMENSION if dimension is _UNSET else dimension
        _dimension_env(dimension)             # refuse an unknown dimension before counting a row
        if incumbent is _UNSET or candidate is _UNSET:
            raise ValueError("ab_arms needs both incumbent= and candidate=, or cells=")
        dimensions, arm_keys = (dimension,), (incumbent, candidate)
    else:
        # Refused, not merged: silently pooling a `dimension=` on top of cells would report a
        # two-arm tally under four-cell keys, or four cells under a name claiming one dimension.
        if incumbent is not _UNSET or candidate is not _UNSET or dimension is not _UNSET:
            raise ValueError("ab_arms takes cells=, or incumbent=/candidate=/dimension=, never "
                             "both: a cell is a pair of labels, so one dimension has no meaning")
        for d in FACTORIAL_DIMENSIONS:
            _dimension_env(d)                 # every axis, before counting a row
        dimensions, arm_keys = FACTORIAL_DIMENSIONS, tuple(cell_key(*c) for c in cells)
    vdir = Path(verdicts_dir)
    acc = {t: {"settled": 0, "reproduced": 0, "births": 0, "answered": 0, "valid": 0, "unanswered": 0}
           for t in arm_keys}
    entries, unknown = {}, set()
    for e in ProposalLog(vdir).read_all():        # one pass: the join index AND the attempt counts
        rid = e.get("request_id")
        if not rid:
            continue
        entries[rid] = e
        # `or 0`, not a default: this now runs on EVERY row, not only the ones a verdict
        # joined to, so a single hand-edited `"ts": null` would otherwise TypeError out of
        # both the Sunday release and the nightly check. Same standard as the guard above.
        if int(e.get("ts") or 0) < since_ts:      # the same window the verdict join applies below
            continue
        arm = _row_arm(dimensions, e)
        if not arm:
            continue                          # no attribution is not an arm; see the docstring
        a = acc.get(arm)
        if a is None:
            continue
        outcome = e.get("outcome")
        if outcome == "proposed":
            a["answered"] += 1
            a["valid"] += 1
        elif outcome == "rejected":
            # The reason grid/odin.py bins an ask under when the mind never answered at all;
            # every other rejection reason means she answered, and answered badly.
            if DEFER_BIN_REASON_RE.match(str(e.get("reason") or "")):
                a["unanswered"] += 1
            else:
                a["answered"] += 1
        else:
            unknown.add(str(outcome))
    if unknown:
        log.warning("proposal log: outcome(s) this gate does not count, excluded from every "
                    "attempt tally: %s", ", ".join(sorted(unknown)))
    for row in Ledger(vdir / "verdicts.jsonl").rows():
        e = entries.get(row.get("request_id"))
        if e is None or int(e.get("ts") or 0) < since_ts or row.get("tier") in EXCLUDED_TIERS:
            continue
        # The verdict row's own alias for `model` (unchanged); the joined log entry for every
        # other dimension, because a verdict row records no variant -- the join is the only
        # thing that can say which prompt produced the genome it settled. In a cell, both.
        arm = _row_arm(dimensions, e, verdict_row=row)
        if not arm:
            continue
        a = acc.get(arm)
        if a is None:
            continue
        a["settled"] += 1
        a["reproduced"] += 1 if row.get("tier") in POSITIVE_TIERS else 0
        a["births"] += int(row.get("births") or 0)
    return {t: {"settled": v["settled"], "reproduced": v["reproduced"],
                "rate": (v["reproduced"] / v["settled"]) if v["settled"] else 0.0,
                "births_per_proposal": (v["births"] / v["settled"]) if v["settled"] else 0.0,
                "answered": v["answered"], "valid": v["valid"], "unanswered": v["unanswered"],
                "validity": (v["valid"] / v["answered"]) if v["answered"] else 0.0}
            for t, v in acc.items()}


def is_validity_loss(arms: dict, *, candidate: str) -> bool:
    """Does the validity floor reject this candidate? The floor's condition, named, because
    two callers must agree on it: `ab_decide`, which returns the verdict, and `release`, which
    has to say WHOSE fault the verdict was. Asking the arms again is exact -- the floor is the
    first thing `ab_decide` tests, so it returns this loss whenever this is True -- and it
    beats sniffing the reason prose, which an ordinary loss on the merits shares a prefix with.
    """
    c = arms[candidate]
    return c["answered"] >= VALIDITY_MIN_ANSWERED and c["validity"] < VALIDITY_MIN


def _advisory_evals(cfg: WeeklyConfig, dimension: str) -> dict:
    """The advisory colony summary a NON-A/B row of this dimension may carry.

    `colony_ab` is keyed by MODEL ALIAS. On a prompt row that is the wrong dimension's numbers
    sitting where this row's would go -- harmless (`advisory: True`, nothing decides on it) but
    wrong, and the kind of wrong that gets read as right later. It is left off rather than
    replaced: the per-arm equivalent for another dimension is `ab_arms(dimension=...)`, which the
    A/B rows already carry, and building a second summary nothing consumes would be worse.
    """
    return {"colony_ab": colony_ab(cfg.verdicts_dir)} if dimension == UNSALTED_DIMENSION else {}


def ab_decide(arms: dict, *, incumbent: str, candidate: str, min_sample: int = MIN_SAMPLE,
              tolerance: float = TOLERANCE) -> tuple[str, str]:
    i, c = arms[incumbent], arms[candidate]
    # Before the sample check, because a candidate that cannot answer is exactly the case the
    # sample check gets wrong: its rejects produce no verdict rows, so it never reaches
    # `min_sample`, reads `inconclusive` week after week, and is finally held at MAX_WEEKS
    # with a reason that blames sample size instead of the wording.
    #
    # The floor is the CANDIDATE's alone. An incumbent below it is not a candidate failure at
    # all but a colony-wide fault (the shared gateway, the ISA, the corpus), and the stuck
    # rule in `ab_check` already owns that case; a branch here would only double-report it.
    if is_validity_loss(arms, candidate=candidate):
        return "loss", (f"colony A/B lost: {candidate} validity {c['validity']:.2f} below "
                        f"{VALIDITY_MIN} over {c['answered']} answered")
    if i["settled"] < min_sample or c["settled"] < min_sample:
        return "inconclusive", f"sample {c['settled']}/{i['settled']} below {min_sample} per arm"
    floor = i["rate"] * (1 - tolerance)
    if c["rate"] >= floor - 1e-12:
        return "win", f"candidate rate {c['rate']:.3f} >= {floor:.3f} (incumbent {i['rate']:.3f} x {1 - tolerance})"
    return "loss", f"colony A/B lost: {c['rate']:.3f} vs {i['rate']:.3f}"


#: The raw counts an arm carries. Ratios are NOT here: pooling means summing these and then
#: re-deriving every ratio from the sums (see `_pool`), never averaging the ratios.
_ARM_COUNTS = ("settled", "reproduced", "answered", "valid", "unanswered")


def _pool(arms: dict, keys: list[str]) -> dict:
    """Sum several cells into one arm of a marginal, in the exact shape `ab_arms` returns.

    Ratios are RE-DERIVED from the summed counts, never averaged. Two cells of 60 settled at
    0.30 and 0.10 pool to 24/120 = 0.20, which happens to equal the mean of the rates only
    because the cells are the same size. Cells are not the same size in general -- the two
    dimensions assign independently, each at its own split -- and averaging would then weight a
    thin cell as heavily as a fat one, which is how a marginal starts reporting a number no arm
    ever produced.

    `births` is recovered as `round(births_per_proposal * settled)` because `ab_arms` publishes
    the ratio rather than the count. Births are whole numbers, so the round-trip is exact; it is
    done this way rather than by adding a `births` key to `ab_arms`' output, which every existing
    reader of an arm dict compares against by shape.
    """
    tot = {k: sum(arms[key][k] for key in keys) for k in _ARM_COUNTS}
    births = sum(round(arms[key]["births_per_proposal"] * arms[key]["settled"]) for key in keys)
    return {**tot,
            "rate": (tot["reproduced"] / tot["settled"]) if tot["settled"] else 0.0,
            "births_per_proposal": (births / tot["settled"]) if tot["settled"] else 0.0,
            "validity": (tot["valid"] / tot["answered"]) if tot["answered"] else 0.0}


def _marginal(arms: dict, *, dimension: str, model: tuple[str, str], prompt: tuple[str, str]) -> dict:
    """One dimension's two arms, each pooling the two cells that share its level."""
    if dimension == UNSALTED_DIMENSION:
        return {m: _pool(arms, [cell_key(m, p) for p in prompt]) for m in model}
    return {p: _pool(arms, [cell_key(m, p) for m in model]) for p in prompt}


def _interaction(arms: dict, *, model: tuple[str, str], prompt: tuple[str, str],
                 min_sample: int) -> dict:
    """The difference of differences, computed and REPORTED, and advisory in the strong sense.

    It is the one number a factorial run can offer that neither marginal can: whether the
    candidate wording helps the candidate model MORE than it helps the incumbent. It is also the
    one number this sample cannot support, and the two facts are the same fact -- an interaction
    is estimated on the cells, and a cell is a quarter of the traffic a marginal pools.

    So it is returned with `advisory: True` and a reason that says exactly that, and nothing in
    this module reads it. No caller may either: a decision taken on it would be a decision taken
    on a quarter-sample, dressed up as the most sophisticated statistic in the file.
    """
    (mi, mc), (pi, pc) = model, prompt
    rate = lambda m, p: arms[cell_key(m, p)]["rate"]
    return {"advisory": True,
            "estimate": (rate(mc, pc) - rate(mc, pi)) - (rate(mi, pc) - rate(mi, pi)),
            "reason": (f"advisory only: an interaction is estimated per CELL, and the floor this "
                       f"run applies is {min_sample} settled per MARGINAL, which pools two cells "
                       f"-- the sample cannot support it, so no decision reads it"),
            "by_cell": {cell_key(m, p): {"settled": arms[cell_key(m, p)]["settled"],
                                         "rate": arms[cell_key(m, p)]["rate"]}
                        for m in model for p in prompt}}


def ab_decide_factorial(arms: dict, *, model: tuple[str, str], prompt: tuple[str, str],
                        min_sample: int = MIN_SAMPLE, tolerance: float = TOLERANCE) -> dict:
    """Decide BOTH dimensions from one four-cell tally, each on its own MARGINAL.

    `model` and `prompt` are `(incumbent, candidate)` pairs; `arms` is
    `ab_arms(..., cells=[...])` over the four `(model, variant)` combinations.

    Each dimension is decided by pooling over the other: the two cells sharing a model level are
    that level's arm, whatever wording answered them, and vice versa. That is what makes the two
    effects separable at all, and it is why the interlock's reasoning is not being bypassed here
    but satisfied. odin assigns the two dimensions from separately salted hashes, so the wording
    is balanced across both model arms by construction; pooling therefore cancels it out of the
    model comparison instead of leaving it to confound the number.

    THE SAMPLE FLOOR IS PER MARGINAL, NOT PER CELL, AND A MARGINAL POOLS TWO CELLS. At the
    shipped MIN_SAMPLE of 100 a 2x2 run needs 50 settled per cell, not 100. Applied per cell the
    floor would demand four times the traffic the comparison actually rests on, every factorial
    week would read `inconclusive`, and MAX_WEEKS would kill both experiments having decided
    nothing. The floor is applied by handing the POOLED arms to `ab_decide` -- one rule, one
    implementation -- which is also what keeps the VALIDITY floor per marginal: an arm that
    cannot produce parseable answers loses its dimension immediately, without waiting for the
    settled floor, exactly as it does in a two-arm run.

    Returns `{"model": (verdict, reason), "prompt": (verdict, reason), "marginals": {...},
    "interaction": {...}}`. The interaction is reported and never read; see `_interaction`.
    """
    for dim, (inc, cand) in (("model", model), ("prompt", prompt)):
        # The same refusal `start_ab` makes, for the same reason and one level up: with equal
        # arms the marginal has ONE key, `ab_decide` compares that arm to itself, and the week
        # is recorded as a win against a phantom.
        if inc == cand:
            raise ValueError(f"factorial {dim} arms are both {inc!r}; there is nothing to compare")
    missing = [cell_key(m, p) for m in model for p in prompt if cell_key(m, p) not in arms]
    if missing:
        # A cell absent from the tally is not an empty cell: `_pool` would KeyError, and papering
        # over it with zeros would silently halve a marginal and report the half as the whole.
        raise ValueError(f"the tally is missing {len(missing)} of the four cells: {missing}")
    marginals = {d: _marginal(arms, dimension=d, model=model, prompt=prompt)
                 for d in FACTORIAL_DIMENSIONS}
    out: dict = {d: ab_decide(marginals[d], incumbent=inc, candidate=cand,
                              min_sample=min_sample, tolerance=tolerance)
                 for d, (inc, cand) in ((UNSALTED_DIMENSION, model), ("prompt", prompt))}
    out["marginals"] = marginals
    # Computed LAST and stored beside the decisions, not above them: by the time it exists both
    # verdicts are already fixed, so there is no branch it could reach even by mistake.
    out["interaction"] = _interaction(arms, model=model, prompt=prompt, min_sample=min_sample)
    return out


def short_arms(arms: dict, *, incumbent: str, candidate: str, min_sample: int = MIN_SAMPLE) -> list[str]:
    """Which arms failed to reach the bar the CALLER is applying, candidate first.

    I3: only a candidate-alone shortfall is the candidate's fault and worth remembering; both
    arms short is a colony throughput fault (the relay was down, verdicts did not settle) and
    must not blacklist a model that never got its turn.

    D2: `min_sample` is the caller's bar, not always MIN_SAMPLE. `release()` judges a week, so
    its bar is MIN_SAMPLE. `ab_check` judges liveness, so its bar is 1 -- "produced nothing at
    all". They must not be conflated: a classically stuck A/B has an incumbent between
    STUCK_INCUMBENT_MIN (50) and MIN_SAMPLE (100), so judging it at MIN_SAMPLE would report
    both arms short and stop `lost_tags` blacklisting every stuck tag, reopening the §8
    livelock.
    """
    return [t for t in (candidate, incumbent) if arms[t]["settled"] < min_sample]


def is_stuck(arms: dict, *, since_ts: int, now_ms: int, incumbent: str, candidate: str) -> bool:
    """Two ways an A/B dies (spec §8 plus C3).

    The candidate arm alone is dead while the incumbent keeps settling — the original rule.
    Or NOTHING settles at all: thegrid keeps one request slot, so a candidate ask that can
    never answer holds it and starves the incumbent too. The incumbent can then never reach
    STUCK_INCUMBENT_MIN, and the original rule would wait for a Sunday that never comes.
    """
    if now_ms - since_ts < STUCK_AFTER_S * 1000 or arms[candidate]["settled"]:
        return False
    return arms[incumbent]["settled"] >= STUCK_INCUMBENT_MIN or arms[incumbent]["settled"] == 0


def ab_state(versions: Versions, *, dimension: str = UNSALTED_DIMENSION) -> dict | None:
    """The running A/B IN ONE DIMENSION: the last ab_started row not yet closed by a
    released/held/rolled_back/flip_incomplete row.

    Rows from another dimension are skipped outright rather than merely ignored as openers:
    they must not CLOSE this dimension's A/B either. A model release landing mid-week would
    otherwise silently end a running prompt experiment in the ledger while the relay kept
    splitting to it -- the exact env/ledger divergence the reconciler exists to repair.
    """
    _dimension_env(dimension)                 # refuse an unknown dimension before reading rows
    started, weeks = None, 0
    for r in versions.rows():
        if dimension_of(r) != dimension:
            continue
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


def _blocking_experiments(cfg: WeeklyConfig, versions: Versions, dimension: str) -> list[tuple[str, str]]:
    """Every OTHER dimension splitting live traffic right now, as (dimension, what is running).

    The interlock's evidence. Two experiments splitting at once do not merely crowd each other:
    a model verdict collected while a prompt A/B is running was measured across two different
    wordings, and a prompt verdict collected during a model A/B across two different sets of
    weights. Either is recorded as BINDING -- it flips the alias, or blacklists the loser for
    good -- so a confounded week is not a slow week, it is a confident wrong answer written into
    an append-only file.

    TWO sources, and the second is not a nicety. `ab_state` reads the ledger, and an A/B started
    the spec-§7 way -- by editing grid.env and restarting the relay -- has no `ab_started` row by
    construction. That state is first-class everywhere else in this module (`_env_ab_view`,
    `ENV_AB_NOTE`, `ab --abort`'s N2 branch, and the reconciler's hand-start exemption, which
    exists precisely so the nightly leaves one alone), and it has genuinely occurred on Skywave
    -- see deploy/receipts/receipts-ab-20260907T142020Z. An interlock blind to it would let the
    gate start a second split on top of a live one, which is the whole failure it exists to stop.
    The env line is the same read `ab --abort` and `_ab_view` already do.

    The two cases are named apart in the reason. "hand-started: no ledger row" tells the operator
    that `ab --abort` on that dimension will clear it and that no Sunday will ever judge it,
    which is a different next action from waiting out a tracked A/B.
    """
    open_elsewhere = []
    for other in sorted(DIMENSIONS):
        if other == dimension:
            continue
        state = ab_state(versions, dimension=other)
        if state is not None:
            open_elsewhere.append((other, str(state["candidate"])))
            continue
        env_candidate = _env_get(cfg.grid_env, _dimension_env(other)["candidate"])
        if env_candidate:
            open_elsewhere.append((other, f"{env_candidate}, hand-started: no ledger row"))
    return open_elsewhere


def _open_elsewhere(blocking: list[tuple[str, str]]) -> str:
    """What the interlock saw, worded once: the refusal and the override must name it the same."""
    return "; ".join(f"{d} A/B already running ({c})" for d, c in blocking)


def _interlock_reason(blocking: list[tuple[str, str]], dimension: str) -> str:
    """One wording for the refusal, shared by `release` and the CLI so the two cannot drift."""
    return (_open_elsewhere(blocking)
            + f"; refusing to start a {dimension} experiment whose verdict it would confound")


def _factorial_reason(blocking: list[tuple[str, str]], dimension: str) -> str:
    """The override's wording, and it must READ as an override.

    A later reader has to be able to tell a factorial week from a confounded one from the row
    alone, and weeks that predate the interlock are already in the ledger looking exactly like
    this one. So the row does not merely fail to refuse -- it says an operator asked for both
    splits deliberately and how the verdict was therefore taken.
    """
    return (_open_elsewhere(blocking)
            + f"; --factorial: the {dimension} experiment was started ALONGSIDE it by explicit "
              "operator request, and each dimension is decided on its own marginal")


def _other_dimensions(cfg: WeeklyConfig, versions: Versions, dimension: str) -> dict:
    """ADVISORY: what every OTHER dimension was doing at the moment this row was written.

    Nothing decides on it. It exists so that a week can be audited afterwards, which is not
    hypothetical: weeks recorded BEFORE the interlock existed are already in this ledger, and
    nothing in them says whether a second experiment was splitting traffic at the time. A verdict
    taken while another dimension was also splitting is either a factorial marginal or a
    confounded number, and which one it was has to stay readable years later from the row itself.

    Same two sources as the interlock (`_blocking_experiments`), so a hand-started split -- one
    with no ledger row of its own -- is recorded here too. That is the case an audit would
    otherwise have no way of ever recovering.
    """
    return {"advisory": True, "splitting": dict(_blocking_experiments(cfg, versions, dimension))}


def lost_tags(versions: Versions, *, dimension: str = UNSALTED_DIMENSION) -> set[str]:
    """A tag is lost iff its most recent lost/stuck/inconclusive-at-max row comes after
    its most recent operator-forget `unchanged` row (spec §8): forgetting makes it
    retestable until it loses, gets stuck, or exhausts MAX_WEEKS inconclusive again.
    (Controller ruling: a tag that could never reach sample size in MAX_WEEKS is not
    auto-retried either — that would livelock the gate re-running the same undersampled
    candidate every week.)

    N1: matched on the REASON prefix alone, regardless of `decision` -- a loss/stuck/
    inconclusive-at-max verdict lands as `flip_incomplete` instead of `held` when C2's
    relay-restart retry fails, and that must be remembered exactly the same, or a failed
    restart after a loss erases the colony's rejection and the gate re-tests the same
    rejected model the very next Sunday.

    N2: ONE memory rule for all three rejection reasons, keyed on `evals.colony_ab.short_arms`
    rather than on the reason's wording. A tag is remembered only when no arm OTHER than that
    tag was under sample:

    - loss (`short_arms: []`, both arms sampled): remembered -- decided on the merits.
    - inconclusive or stuck with only the candidate short (`[tag]`): remembered -- that arm
      demonstrably could not produce.
    - either with the incumbent short too (`[tag, incumbent]`): NOT remembered. Both arms at
      zero is a colony throughput fault -- grid-odin down, the gateway down, the key drained --
      and C3's both-arms-zero stuck rule made that case reachable for the first time. A 48 h
      outage must not blacklist a candidate that never got its turn.
    - no `short_arms` at all (a row written before the field existed, or hand-edited): read as
      candidate-short and remembered, the conservative direction that keeps the livelock guard.

    Memory is PER DIMENSION. A model tag and a prompt variant are different kinds of name and
    could in principle collide; more to the point, a loss in one dimension says nothing at all
    about a candidate in the other, and an operator's forget must clear only what it named.
    """
    _dimension_env(dimension)                 # refuse an unknown dimension before reading rows
    lost_at: dict[str, float] = {}
    forgotten_at: dict[str, float] = {}
    for r in versions.rows():
        tag = r.get("version")
        if not tag or dimension_of(r) != dimension:
            continue
        reason = str(r.get("reason", ""))
        if reason.startswith(("colony A/B lost", "colony A/B stuck", "colony A/B inconclusive")):
            short = ((r.get("evals") or {}).get("colony_ab") or {}).get("short_arms")
            if short is None or not (set(short) - {tag}):
                lost_at[tag] = r["ts"]
        elif r["decision"] == "unchanged" and reason.startswith("operator forgot"):
            forgotten_at[tag] = r["ts"]
    return {tag for tag, ts in lost_at.items() if ts > forgotten_at.get(tag, float("-inf"))}


def _flip_local(cfg: WeeklyConfig, keys: dict[str, str], version: str, *, local_run, dry_run: bool) -> str:
    """Apply a winner whose live value is a grid.env line and nothing else.

    The same three steps as `flip`, minus the middle one: set the live value, clear the
    candidate, restart the relay. Because nothing here is remote, a failed restart leaves nothing
    APPLIED, so both lines go back together -- flip()'s own C1 rule and start_ab's N2: a file
    claiming a default the live relay has never read is a divergence only an operator can see.

    What a failed restart does NOT mean here is "the relay is still serving what it read at
    boot". systemd was already asked to restart, so the old process is gone; and because odin's
    registry loader fails closed, a value it refuses leaves the unit DOWN rather than merely
    mislabelled -- which is why the revert is followed by another restart attempt, on the values
    that were known to work.

    It also means `flip_failed` -- which means precisely "the remote tag copy failed" -- has no
    meaning on this path and is never returned. The outcomes are "ok" and "flip_incomplete".
    """
    cmd = ["sudo", "-n", "systemctl", "restart", "grid-odin.service"]
    if dry_run:
        log.info("[dry-run] flip: would set %s=%s in %s, clear %s, and run %s",
                 keys["live"], version, cfg.grid_env, keys["candidate"], " ".join(cmd))
        return "ok"
    before_live = _env_get(cfg.grid_env, keys["live"])
    before_cand = _env_get(cfg.grid_env, keys["candidate"])
    _env_set(cfg.grid_env, keys["live"], version)
    # A flip always ends any A/B in progress in its own dimension: the relay is about to serve
    # `version` outright, so a stray candidate split would fight the new default.
    _env_set(cfg.grid_env, keys["candidate"], "")
    outcome = _restart_relay(local_run)
    if outcome != "ok":
        _env_restore(cfg.grid_env, keys["live"], before_live)
        _env_restore(cfg.grid_env, keys["candidate"], before_cand)
        _restart_after_revert(local_run, "the flip")
    return outcome


def flip(cfg: WeeklyConfig, remote, version: str, *, dimension: str = UNSALTED_DIMENSION,
         local_run, dry_run: bool = False) -> str:
    """Spec §7 order: local env, remote tag copy, relay restart.

    `dry_run` performs no real mutation: `remote.run` is already a no-op when the Remote
    itself is dry-run, but `_env_set` writes `cfg.grid_env` directly on disk and
    `local_run` may be the real `subprocess.run` restart — neither is safe to call for
    real here, so both are skipped (and logged) when `dry_run` is true.

    Only the `model` dimension has a remote step at all: a model tag has to be copied onto the
    alias on debain2 and that copy can fail. Every other dimension is a line in grid.env, so it
    goes to `_flip_local`, `remote` is never touched, and `flip_failed` cannot come back.
    """
    keys = _dimension_env(dimension)
    if dimension != UNSALTED_DIMENSION:
        return _flip_local(cfg, keys, version, local_run=local_run, dry_run=dry_run)
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


def _restart_after_revert(local_run, what: str) -> None:
    """Bring the PROCESS back in line with the file that was just put back.

    Reverting alone is only half a repair, and the missing half is the dangerous one. By the time
    a revert happens `_restart_relay` has already told systemd to restart, so the relay that was
    running is gone: either a new one came up on the values being reverted away from, or -- when
    those values are ones odin refuses at boot, an unknown variant or a candidate equal to the
    incumbent -- no relay came up at all. In the first case the file says one thing and the live
    process another, which is the divergence every guard in this module exists to prevent; in the
    second the colony's mind is simply DOWN, and a correct grid.env does not bring it back.

    Best effort, and it never changes what the caller reports: the experiment did not start (or
    the flip did not apply) either way, so the outcome stays `flip_incomplete` and the operator
    is still summoned by exit 3. This only decides whether they arrive to a running relay.
    """
    if _restart_relay(local_run) == "ok":
        log.info("relay restarted on the values restored after %s", what)
    else:
        log.error("relay could not be restarted after reverting %s; grid.env is correct but the "
                  "unit may be DOWN -- restart grid-odin.service by hand", what)


def start_ab(cfg: WeeklyConfig, *, candidate: str, incumbent: str, dimension: str = UNSALTED_DIMENSION,
            local_run, dry_run: bool = False, now_ms: int | None = None,
            factorial: bool = False) -> tuple[dict, str]:
    """Spec §8: probes passed, but a release is now a binding colony A/B, not a flip.

    Returns `(block, outcome)`: `outcome` is "ok" or "flip_incomplete" (C2) -- a caller
    that ignores it would otherwise record `ab_started` even when the relay never picked
    up the candidate, silently blocking that arm from ever running.

    `dimension` chooses WHICH pair of grid.env lines carries the split. Starting a prompt A/B
    on MIND_LLM_CANDIDATE would hand the relay a variant id where it expects a model tag: every
    ask would fail at the gateway, and the ledger would call it a prompt experiment throughout.

    `factorial` is recorded, ALWAYS and explicitly -- True or False, never an absent key. It is
    what a later reader (and `_factorial_partner`, on the judging Sunday) uses to tell a week
    that deliberately crossed two dimensions from one that merely got confounded, and the two
    are indistinguishable from the numbers alone. It changes nothing about the split itself:
    each dimension's arms are its own grid.env lines either way, and it is the DECISION that
    differs, not the experiment.
    """
    keys = _dimension_env(dimension)
    # I1: odin refuses an A/B whose two arms are the same value, and it refuses it AT BOOT, so
    # writing one here does not waste a week -- it takes the relay down and the revert below
    # cannot argue it back up. `release` turns this case into an `unchanged` week before reaching
    # here; this is the guard for every other caller, and it mirrors the relay's own so the two
    # cannot drift. Note it is NOT covered by validating the name against the prompt registry:
    # the incumbent's own id is a perfectly valid entry.
    if candidate == incumbent:
        raise ValueError(f"{dimension} A/B candidate and incumbent are both {candidate!r}; "
                         "odin refuses that at boot, so starting it would leave the relay down")
    # ...and the other half of the same fatality: a name the registry does not hold at all. The
    # CLI checks this too, before it accepts the argument, because the CLI is not the only caller
    # -- but the check belongs HERE as well, at the last statement before the write, since this is
    # the function that puts the name in grid.env and restarts the relay onto it.
    _validate_candidate(dimension, candidate)
    since = now_ms if now_ms is not None else int(time.time() * 1000)
    block = {"binding": True, "candidate": candidate, "incumbent": incumbent, "split": AB_SPLIT, "since_ts": since,
             "min_sample": MIN_SAMPLE, "tolerance": TOLERANCE, "weeks": 0, "arms": {}, "winner": None,
             "factorial": factorial}
    if dry_run:
        return block, "ok"
    before_cand = _env_get(cfg.grid_env, keys["candidate"])
    before_split = _env_get(cfg.grid_env, keys["split"])
    _env_set(cfg.grid_env, keys["candidate"], candidate)
    _env_set(cfg.grid_env, keys["split"], str(AB_SPLIT))
    outcome = _restart_relay(local_run)
    if outcome != "ok":
        # N2 (mirrors C1): a failed restart here means the relay never picked up the
        # candidate, so the env must not go on claiming an A/B the ledger doesn't track
        # (untracked traffic split; ab_state() sees nothing running; `ab --abort` would
        # otherwise refuse to touch it). The candidate line keeps the empty idiom (the relay
        # reads empty as "no A/B"); the split key has no such idiom -- C1: an empty split line
        # is a ValueError in RelayConfig.from_env, so an absent-before split is REMOVED, not
        # blanked. Writing nothing back at all would leave the new values in place.
        _env_set(cfg.grid_env, keys["candidate"], before_cand if before_cand is not None else "")
        _env_restore(cfg.grid_env, keys["split"], before_split)
        # I2: the file is right again and the relay is not. systemd was already told to restart,
        # so whatever is (or is not) running came up on the values just reverted away from -- and
        # a candidate odin refuses at boot leaves NOTHING running. Try again on the restored ones.
        _restart_after_revert(local_run, "the A/B start")
    return block, outcome


def end_ab(cfg: WeeklyConfig, *, dimension: str = UNSALTED_DIMENSION, local_run, dry_run: bool = False) -> str:
    """Clear the candidate split (win/loss/inconclusive-at-max/stuck/abort all land here).

    Returns the same "ok" / "flip_incomplete" outcome as `start_ab` (C2): a caller that
    ignores it would record the A/B as closed even while the losing candidate keeps its
    share of live traffic because the relay was never restarted.

    Clears this dimension's candidate only: a model A/B and a prompt A/B can be running at
    once, and ending one must not silently end the other.
    """
    keys = _dimension_env(dimension)
    if dry_run:
        return "ok"
    _env_set(cfg.grid_env, keys["candidate"], "")
    return _restart_relay(local_run)


def _is_factorial(state: dict) -> bool:
    """Was this A/B opened as one leg of a deliberate factorial run?"""
    return bool(((state["started_row"].get("evals") or {}).get("colony_ab") or {}).get("factorial"))


def _factorial_partner(versions: Versions, state: dict, dimension: str) -> tuple[str, dict] | None:
    """The other leg of a factorial run, or None if this is an ordinary two-arm week.

    EITHER leg's marker is enough, and that is not a courtesy. `--factorial` is typed exactly
    once, on the run that starts SECOND -- the first was started when nothing was blocking and
    had no reason to claim anything. Requiring this dimension's own row to carry the marker would
    judge the first-started experiment two-arm over a window it shared with the second, which is
    precisely the confounded verdict the interlock exists to prevent: the override would then fix
    one leg and quietly corrupt the other.

    Both legs must still be OPEN IN THE LEDGER. Once one closes, its candidate stops splitting
    and the design is gone; the remaining leg falls back to its two-arm tally, and the advisory
    `other_dimensions` key on the row is what records that the window was shared.
    """
    for other in sorted(DIMENSIONS):
        # Only the two axes `ab_arms(cells=...)` knows how to cross. A third dimension in odin's
        # allowlist is not silently drafted into a design nothing here can compute.
        if other == dimension or {dimension, other} != set(FACTORIAL_DIMENSIONS):
            continue
        other_state = ab_state(versions, dimension=other)
        if other_state is None or not (_is_factorial(state) or _is_factorial(other_state)):
            continue
        # All four labels, or no design. An `ab_started` row that never recorded an incumbent --
        # hand-edited, or written by an older writer -- yields `incumbent: None`, which names no
        # cell and cannot be pooled. The two-arm path tolerates that (the arm simply matches
        # nothing and the week reads inconclusive); a cell key built from it would not. Fall
        # back rather than crash the Sunday run, and let the advisory key record the overlap.
        labels = [state["incumbent"], state["candidate"], other_state["incumbent"], other_state["candidate"]]
        if not all(isinstance(x, str) and x for x in labels):
            log.warning("colony A/B: a factorial leg is missing an arm label (%s); judging %s "
                        "two-arm instead", labels, dimension)
            continue
        return other, other_state
    return None


def _judge_ab(cfg: WeeklyConfig, versions: Versions, state: dict, dimension: str) -> tuple[str, str, dict, dict]:
    """This Sunday's verdict for one dimension: `(verdict, reason, arms, extra_block)`.

    Ordinarily a two-arm tally in this dimension alone -- unchanged. On a week that is one leg of
    a factorial run, the tally is the four CELLS and the verdict is this dimension's MARGINAL,
    pooled over the other. Only the marginal is returned as `arms`, because everything downstream
    -- `is_validity_loss`, `short_arms`, `lost_tags` reading `short_arms` back, the operator
    reading the row -- is about THIS dimension's two arms, and a cell is not one of them.

    The window is the LATER of the two starts. A request that arrived before the second split
    began was answered at one fixed level of that dimension, so it is not part of the design at
    all; counting it into a cell would put unbalanced traffic into the pool and hand back exactly
    the unattributable number the whole exercise is avoiding.
    """
    partner = _factorial_partner(versions, state, dimension)
    if partner is None:
        arms = ab_arms(cfg.verdicts_dir, since_ts=state["since_ts"], incumbent=state["incumbent"],
                       candidate=state["candidate"], dimension=dimension)
        verdict, reason = ab_decide(arms, incumbent=state["incumbent"], candidate=state["candidate"])
        return verdict, reason, arms, {}
    other, other_state = partner
    pairs = {dimension: (state["incumbent"], state["candidate"]),
             other: (other_state["incumbent"], other_state["candidate"])}
    since = max(int(state["since_ts"]), int(other_state["since_ts"]))
    cells = [(m, p) for m in pairs[UNSALTED_DIMENSION] for p in pairs["prompt"]]
    four = ab_arms(cfg.verdicts_dir, since_ts=since, cells=cells)
    result = ab_decide_factorial(four, model=pairs[UNSALTED_DIMENSION], prompt=pairs["prompt"])
    verdict, reason = result[dimension]
    # APPENDED to the reason, never prepended: `lost_tags` matches on the reason's PREFIX
    # ("colony A/B lost" / "stuck" / "inconclusive"), so a note in front would erase the colony's
    # memory of a candidate that just lost on a perfectly attributable marginal.
    reason = f"{reason} [factorial marginal, pooled over {other}]"
    return verdict, reason, result["marginals"][dimension], {
        "factorial": True, "factorial_with": {other: other_state["candidate"]},
        "factorial_since_ts": since, "cells": four, "interaction": result["interaction"]}


def _iso_week() -> str:
    return time.strftime("%G-W%V")


async def release(cfg: WeeklyConfig, remote, *, ask_factory, local_run, week: str | None = None,
                  dry_run: bool = False, key_models=key_models, dimension: str = UNSALTED_DIMENSION,
                  candidate: str | None = None, factorial: bool = False) -> dict:
    """One Sunday's decision, in one dimension.

    `candidate` is the challenger for a dimension that has no discovery of its own. The model
    dimension reads Rogue's promotion state and must NOT be given one; a prompt dimension has
    nothing to read -- which of the registry's variants challenges the default is a choice, not
    a fact about a host -- so it must be. Passing the wrong one is refused rather than ignored:
    a silently discarded candidate would ledger a week of "no promotion this week".

    `factorial` is the deliberate override of the interlock, and it is an override of the
    REFUSAL, not of the reasoning behind it. The interlock stops a second split because a verdict
    collected while another dimension is also splitting has been measured across two conditions
    and is not attributable. This flag says: start both anyway, record on the row that an
    operator asked for that, and on the judging Sunday decide each dimension on its MARGINAL --
    which is what makes the verdict attributable again. It is refused when there is nothing to
    override (see the two guards below); ignoring it there would make "factorial" a word that
    sometimes appears in the ledger for no reason.
    """
    keys = _dimension_env(dimension)
    # Up here, not down in the start path: the model dimension never accepts a candidate, and an
    # A/B week returns long before that path, so a guard placed there would ignore the argument
    # in exactly the case the operator is most likely to be confused about. (The mirror guard --
    # a non-model dimension REQUIRING one -- has to stay below, because a judging week needs none.)
    _refuse_given_model_candidate(dimension, candidate)
    week = week or _iso_week()
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    last = _last_released(versions, dimension=dimension)
    released = last["version"] if last else None
    state = ab_state(versions, dimension=dimension)
    if factorial and state is not None:
        # `--factorial` STARTS a run; it cannot retro-fit one. How a running A/B is judged is
        # already settled by the `ab_started` row that opened it, and honouring the flag here
        # would let a Sunday's typing decide the shape of an experiment that has been collecting
        # traffic for a week. Refused rather than ignored, per the same rule as `--candidate`.
        raise ValueError(f"there is already a {dimension} A/B open; --factorial starts a "
                         "factorial run, it does not change how a running one is judged")
    if state is not None:
        # A colony A/B is running: this Sunday is the binding verdict, not a probe day
        # (spec §8) — no candidate discovery, no key check, no probes.
        common = dict(week=week, base="", run="", grid_examples={}, verdict_window=_window(cfg.verdicts_dir), rogue_ledger_ts=0.0,
                      adapter_sha256="", gguf_sha256="", set_manifest_sha256="", spend_usd=0.0)
        # I3: an ab_started row with no evals (e.g. hand-edited, or a future writer that
        # omits it) must not KeyError here.
        started_ab = (state["started_row"].get("evals") or {}).get("colony_ab") or {}
        env_incumbent = _live_arm(cfg, dimension)
        if env_incumbent != state["incumbent"]:
            # I2: `set-mind-model.sh --relay-only` rewrote MIND_MODEL_ALIAS mid-A/B, so the
            # incumbent's proposals have been logged under a label ab_arms does not count.
            # Its rate is a fiction and the week would read `inconclusive` (or worse, a win
            # against a phantom zero); refuse to decide. The week is spent like any other
            # inconclusive one -- and N1: MAX_WEEKS binds here exactly as it does there.
            # Without the cap `ab_inconclusive` never closes an A/B, so the gate would take
            # this branch every Sunday forever: no probes, no releases, the relay splitting
            # to an unjudgeable candidate, and the nightly check silent.
            # D3: `short_arms` names BOTH arms, because that is what happened -- `arms` is {}
            # and neither arm was judged. `[]` would have been the encoding a genuine loss
            # carries ("both fully sampled, decided on the merits"), i.e. the strongest possible
            # attribution to the candidate, leaving only the reason prefix between an operator's
            # mislabel and a blacklisting. Now lost_tags abstains for two independent reasons.
            block = {**started_ab, "binding": True, "arms": {}, "weeks": state["weeks"] + 1,
                     "winner": None, "short_arms": [state["candidate"], state["incumbent"]],
                     "other_dimensions": _other_dimensions(cfg, versions, dimension)}
            base_reason = (f"incumbent label changed mid-A/B ({env_incumbent} != "
                           f"{state['incumbent']}); evaluation skipped")
            if state["weeks"] + 1 >= MAX_WEEKS:
                end = end_ab(cfg, dimension=dimension, local_run=local_run, dry_run=dry_run)
                closed = f"{base_reason}; A/B ended after {MAX_WEEKS} weeks"
                if end == "ok":
                    decision, why = "held", closed
                else:
                    decision, why = "flip_incomplete", f"{closed}; relay restart failed clearing the A/B"
            else:
                decision, why = "ab_inconclusive", base_reason
            row = release_row(decision=decision, dimension=dimension, version=state["candidate"], previous=released,
                              evals={"colony_ab": block}, **common, reason=why)
            return _ledger(versions, row, dry_run=dry_run)
        # Two-arm in this dimension, or -- on one leg of a factorial run -- this dimension's
        # marginal, pooled over the other. `arms` is THIS dimension's two arms either way, so
        # everything below (the validity floor, short_arms, lost_tags' later read of it) is
        # unchanged and needs no factorial branch of its own.
        verdict, reason, arms, factorial_block = _judge_ab(cfg, versions, state, dimension)
        # I1/N2: a validity loss is FULLY ATTRIBUTED to the candidate -- it was asked plenty and
        # answered badly -- so it carries the `[]` encoding, exactly like a loss on the merits.
        # Left to short_arms() it would not: a validity loss has the candidate under MIN_SAMPLE
        # by definition (its rejects settle nothing), so whenever the incumbent also happens to
        # sit under MIN_SAMPLE the row would name BOTH arms short, lost_tags would read that as
        # a colony throughput fault and abstain, and find_candidate would hand the same broken
        # candidate back every Sunday to lose identically forever -- on a verdict whose whole
        # claim is that sample size was not the problem.
        short = ([] if is_validity_loss(arms, candidate=state["candidate"])
                 else short_arms(arms, incumbent=state["incumbent"], candidate=state["candidate"]))
        block = {**started_ab, **factorial_block, "binding": True, "arms": arms,
                 "weeks": state["weeks"] + 1, "winner": None, "short_arms": short,
                 "other_dimensions": _other_dimensions(cfg, versions, dimension)}
        if verdict == "inconclusive" and state["weeks"] + 1 >= MAX_WEEKS:
            outcome = end_ab(cfg, dimension=dimension, local_run=local_run, dry_run=dry_run)
            base_reason = f"colony A/B inconclusive after {MAX_WEEKS} weeks: {reason}"
            if outcome == "ok":
                decision, why = "held", base_reason
            else:
                decision, why = "flip_incomplete", f"{base_reason}; relay restart failed clearing the A/B"
            row = release_row(decision=decision, dimension=dimension, version=state["candidate"], previous=released,
                              evals={"colony_ab": block}, reason=why, **common)
        elif verdict == "inconclusive":
            row = release_row(decision="ab_inconclusive", dimension=dimension, version=state["candidate"], previous=released,
                              evals={"colony_ab": block}, reason=reason, **common)
        elif verdict == "win":
            block["winner"] = state["candidate"]
            outcome = flip(cfg, remote, state["candidate"], dimension=dimension, local_run=local_run, dry_run=dry_run)
            if outcome == "ok":
                decision, why = "released", reason
            elif outcome == "flip_failed":
                # Reachable for `model` alone, and not by omission: `flip_failed` means exactly
                # "the remote tag copy failed", and no other dimension makes a remote call, so
                # there is no such failure for one to report. flip() is where that is enforced.
                # C2: flip() restored MIND_LLM_CANDIDATE on its way out, so env and the live
                # relay would keep splitting while this row closes the A/B in the ledger --
                # the nightly check would then see "no A/B", and next Sunday would start a
                # fresh one, discarding the week that already won. End it for real.
                end = end_ab(cfg, dimension=dimension, local_run=local_run, dry_run=dry_run)
                base_reason = (f"colony A/B won but the tag copy failed: {state['candidate']}; "
                               "A/B ended, current unchanged")
                # NOT a lost/stuck/inconclusive reason: a win that could not be applied is no
                # rejection by the colony, so lost_tags must not remember it -- next Sunday
                # re-probes the same tag and tries the copy again.
                if end == "ok":
                    decision, why = "held", base_reason
                else:
                    decision, why = "flip_incomplete", f"{base_reason}; relay restart failed clearing the A/B"
            elif dimension == UNSALTED_DIMENSION:
                decision, why = "flip_incomplete", "alias serves the candidate; relay restart failed twice"
            else:
                # The opposite situation to the line above, and the reason must not claim
                # otherwise: the tag copy there had already happened, so the alias DOES serve
                # the winner and only the relay lags. A local flip applied nothing -- both lines
                # went back -- so grid.env and the live relay still agree on the old default and
                # the A/B is still splitting there. The ledger closes the A/B either way (the
                # colony decided), and flip_incomplete is what exits 3 and asks for a human.
                decision, why = "flip_incomplete", (f"colony A/B won but the relay restart failed twice; "
                                                    f"{keys['live']} unchanged")
            row = release_row(decision=decision, dimension=dimension, version=state["candidate"], previous=released,
                              evals={"colony_ab": block}, reason=why, **common)
        else:
            outcome = end_ab(cfg, dimension=dimension, local_run=local_run, dry_run=dry_run)
            if outcome == "ok":
                decision, why = "held", reason
            else:
                decision, why = "flip_incomplete", f"{reason}; relay restart failed clearing the A/B"
            row = release_row(decision=decision, dimension=dimension, version=state["candidate"], previous=released,
                              evals={"colony_ab": block}, reason=why, **common)
        return _ledger(versions, row, dry_run=dry_run)
    if dimension == UNSALTED_DIMENSION:
        cand = find_candidate(remote, cfg)
    else:
        # Nothing discovers a prompt candidate. The registry holds several variants and which
        # one challenges the default is a judgement, not a fact to be read off a host, so it is
        # supplied. `run`/`manifest`/`rogue_ts` stay empty for the same reason: they are facts
        # about a training run, and a prompt experiment has none.
        if not candidate:
            raise ValueError(f"a {dimension} release needs an explicit candidate; nothing discovers one")
        cand = {"tag": candidate, "run": "", "rogue_ts": 0.0, "manifest": {}}
    common = dict(week=week, base=(cand or {}).get("manifest", {}).get("base", ""), run=(cand or {}).get("run", ""),
                  grid_examples={}, verdict_window=_window(cfg.verdicts_dir), rogue_ledger_ts=(cand or {}).get("rogue_ts", 0.0))
    # THE INTERLOCK, at the point the gate is about to start an experiment rather than judge one:
    # everything above this line either returned an A/B week's binding verdict or resolved a
    # challenger, and `state is None` up there already established that this dimension has no A/B
    # of its own. `main` refuses this too, and must -- the CLI's candidate-less case has to become
    # a recorded row rather than the raise above, which is a decision only the argument layer can
    # make. This copy is the guard for every OTHER caller, the same reason `start_ab` keeps its own
    # copy of the equal-arms refusal and `_validate_candidate` is checked twice.
    blocking = _blocking_experiments(cfg, versions, dimension)
    if factorial and not blocking:
        # The second guard, and the reason it is not tidiness: with nothing blocking, a
        # `--factorial` start would write `factorial: True` onto an ordinary single-dimension
        # A/B, and `_factorial_partner` keys the JUDGING path on exactly that marker. The next
        # experiment started in the other dimension would then be drafted into a "design" whose
        # first leg had already been collecting unbalanced traffic for a week.
        raise ValueError("--factorial overrides the interlock, and nothing is blocking: no other "
                         f"dimension is splitting live traffic, so a {dimension} experiment "
                         "started now is an ordinary single-dimension run")
    if blocking and not factorial:
        row = release_row(decision="unchanged", dimension=dimension, version=(cand or {}).get("tag"),
                          previous=released, adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
                          evals=_advisory_evals(cfg, dimension), spend_usd=0.0,
                          reason=_interlock_reason(blocking, dimension), **common)
        return _ledger(versions, row, dry_run=dry_run)
    if cand is None or cand["tag"] == released:
        decision, reason = decide(candidate=(cand or {}).get("tag"), released=released, identity=True, validity=True)
        row = release_row(decision=decision, dimension=dimension, version=(cand or {}).get("tag"), previous=released,
                          adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
                          evals=_advisory_evals(cfg, dimension), spend_usd=0.0, reason=reason, **common)
        return _ledger(versions, row, dry_run=dry_run)
    if cand["tag"] == _live_arm(cfg, dimension):
        # I1, and it is not the same case as `== released` above: a dimension that has never had
        # a release through the gate still HAS an incumbent (the registry default variant; a
        # hand-set MIND_MODEL_ALIAS), so `released` is None while the relay is serving this very
        # name. odin refuses an A/B whose arms are equal AT BOOT, so starting it would take the
        # relay down; and if traffic somehow accrued anyway, `arms` would have one key and
        # `ab_decide` would compare an arm to itself and call it a win. Worse at MAX_WEEKS:
        # `short_arms` returns [tag, tag], `set(short) - {tag}` is empty, and lost_tags would
        # blacklist the live incumbent -- for prompt, the registry default, the one variant that
        # must never be blacklisted. `unchanged`, the same reading `== released` gets, because it
        # is the same fact: there is nothing here to test.
        decision, reason = "unchanged", f"{cand['tag']} is already the live {dimension} incumbent"
        row = release_row(decision=decision, dimension=dimension, version=cand["tag"], previous=released,
                          adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
                          evals=_advisory_evals(cfg, dimension), spend_usd=0.0, reason=reason, **common)
        return _ledger(versions, row, dry_run=dry_run)
    if cand["tag"] in lost_tags(versions, dimension=dimension):
        decision = "held"
        # M2: `ab --forget` writes a MODEL forget, and lost_tags reads the forget row through the
        # same dimension filter as the loss, so on a prompt row the bare command clears nothing.
        # The model string stays verbatim; the other dimensions name themselves, in the flag
        # Task 5 adds to this parser. Telling an operator to run a command that silently does
        # nothing is worse than telling them nothing.
        forget = f"grid.weekly ab --forget {cand['tag']}"
        if dimension != UNSALTED_DIMENSION:
            forget = f"{forget} --dimension {dimension}"
        reason = f"{cand['tag']} already lost/stuck the colony A/B; run `{forget}` to retest"
        row = release_row(decision=decision, dimension=dimension, version=cand["tag"], previous=released,
                          adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
                          evals=_advisory_evals(cfg, dimension), spend_usd=0.0, reason=reason, **common)
        return _ledger(versions, row, dry_run=dry_run)
    if dimension == UNSALTED_DIMENSION:
        # I1: the probes ask the concrete candidate tag; the colony key may not list it yet if it
        # was minted before this vN existed. Verify first — a candidate off the key would just
        # make the gateway reject the probe, which C1 would then (correctly, but confusingly) hold
        # as a probe failure. Skip the check (proceed) when the key's model list can't be read.
        models = key_models(cfg, remote)
        if models is not None and cand["tag"] not in models and models not in _WILDCARD_MODEL_LISTS:
            decision, reason = "held", f"candidate {cand['tag']} not on the colony key"
            row = release_row(decision=decision, dimension=dimension, version=cand["tag"], previous=released,
                              adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
                              evals=_advisory_evals(cfg, dimension), spend_usd=0.0, reason=reason, **common)
            return _ledger(versions, row, dry_run=dry_run)
        ask = ask_factory(cand["tag"])
        evals = {"holdout_ppl": cand["manifest"].get("holdout_ppl", {}), **_advisory_evals(cfg, dimension)}
        try:
            # C1: any probe failure (gateway error, timeout, malformed or empty completion) must
            # record `held`, never crash out of `release` (spec §7).
            evals["identity"] = identity = await run_identity(ask, runs=cfg.identity_runs)
            evals["validity"] = validity = await run_validity(ask, cfg.verdicts_dir)
        except Exception as e:
            decision, reason = "held", f"probe failed: {e}"
            evals["probe_error"] = str(e)
            row = release_row(decision=decision, dimension=dimension, version=cand["tag"], previous=released,
                              adapter_sha256="", gguf_sha256="", set_manifest_sha256="", evals=evals,
                              spend_usd=0.0, reason=reason, **common)
            return _ledger(versions, row, dry_run=dry_run)
        spend = float(cand["manifest"].get("seconds", 0)) / 3600 * A100_USD_PER_H
        h = hashes(remote, cfg, cand["run"])
        h["gguf_sha256"] = gguf_hash(remote, cand["tag"])
        decision, reason = decide(candidate=cand["tag"], released=released, identity=identity["pass"], validity=validity["pass"])
    else:
        # The probes gate a MODEL candidate and only a model candidate. `run_identity` asks
        # "does this tag still answer as Kannaka" and `holdout_ppl` is the training run's own
        # perplexity: both are measurements OF the weights. A prompt experiment does not change
        # the weights -- the same model that is already serving answers both arms -- so the
        # probes would interrogate the incumbent, pass by construction, and charge an hour of
        # gateway time for an answer that was never in doubt. The key check and the run hashes
        # are model facts too, and a variant id is on no key and in no run. The colony A/B,
        # which is where a prompt can actually be judged, is the whole test here.
        evals = _advisory_evals(cfg, dimension)
        spend = 0.0
        h = {"adapter_sha256": "", "gguf_sha256": "", "set_manifest_sha256": ""}
        decision, reason = "released", f"{dimension} candidate {cand['tag']}: no model probes to run"
    if decision == "released":
        # spec §8: probes passed, but a release is now a binding colony A/B, not a flip.
        incumbent = _live_arm(cfg, dimension) or released
        block, outcome = start_ab(cfg, candidate=cand["tag"], incumbent=incumbent, dimension=dimension,
                                  local_run=local_run, dry_run=dry_run, factorial=factorial)
        # Read AFTER start_ab, which only ever writes this dimension's own env lines, so what the
        # other dimensions are doing is the same fact before and after.
        block["other_dimensions"] = _other_dimensions(cfg, versions, dimension)
        evals["colony_ab"] = block
        if outcome == "ok":
            decision, reason = "ab_started", ("probes passed; colony A/B started" if dimension == UNSALTED_DIMENSION
                                              else f"{dimension} colony A/B started")
            if factorial:
                # The row has to SAY it was an override. `factorial: True` in the block is what
                # the code reads; this is what a person reads, and a person is who has to tell a
                # deliberate factorial week from a confounded one years later.
                reason = f"{reason}; {_factorial_reason(blocking, dimension)}"
        else:
            # C2: the candidate's arm never ran, so it must not be recorded as started --
            # otherwise ab_check would falsely declare it stuck 48h later and block it.
            decision, reason = "flip_incomplete", (
                "probes passed but the relay restart failed twice starting the colony A/B"
                if dimension == UNSALTED_DIMENSION
                else f"the relay restart failed twice starting the {dimension} colony A/B")
    row = release_row(decision=decision, dimension=dimension, version=cand["tag"], previous=released, evals=evals,
                      spend_usd=spend, reason=reason, **h, **common)
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


#: the decisions that say something about an A/B's lifecycle; `unchanged` (ab_forget's
#: operator note) deliberately says nothing, so the reconciler looks straight past it.
_AB_DECISIONS = ("ab_started", "ab_inconclusive", "released", "held", "rolled_back", "flip_incomplete")


def ab_reconcile(cfg: WeeklyConfig, *, dimension: str = UNSALTED_DIMENSION, local_run,
                 dry_run: bool = False) -> dict | None:
    """N4: repair the RESTING state, not a transition.

    Every guard in this module so far checks a transition, which is why this bug class has now
    appeared four times: a divergence between `grid.env`, the live relay and the ledger is
    announced once, by the run that created it, and is then invisible forever (a
    `flip_incomplete` row closes `ab_state`, so the next nightly says "no A/B running" and
    exits 0). This runs first, every night, and asks only whether the world is currently
    consistent. It repairs exactly what the gate itself broke and keys on the LEDGER, never on
    absence -- an A/B the ledger has never seen belongs to an operator and is left alone.

    Returns the row it recorded, or None when nothing needed repairing.

    Scoped to one dimension throughout: the env line it reads, the ledger rows it keys on and
    the row it writes. A model A/B running normally must not look like an unbacked prompt split.
    """
    keys = _dimension_env(dimension)
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    rows = [r for r in versions.rows() if dimension_of(r) == dimension]
    state = ab_state(versions, dimension=dimension)
    env_candidate = _env_get(cfg.grid_env, keys["candidate"])
    if env_candidate and env_candidate not in {r.get("version") for r in rows if r["decision"] == "ab_started"}:
        # Spec §7's own smoke receipt hand-starts an A/B by editing grid.env. Clearing it would
        # be worse than the bug this function exists to fix: be loud and touch nothing.
        # D1: the test is "did the GATE start an A/B on this tag?", which only an `ab_started`
        # row answers -- not "has this tag ever appeared in the ledger". Keyed the broad way,
        # the receipt's own closing `ab --abort` (a `held` row for that tag) spent the
        # exemption, and the next hand-start of the same tag was cleared by the next nightly.
        log.warning("colony A/B: %s is split in %s with no ledger row; hand-started, leaving it alone",
                    env_candidate, cfg.grid_env)
        return None
    if state is not None:
        if env_candidate:
            return None                     # ledger open, env splitting: consistent
        candidate = state["candidate"]
        why = (f"colony A/B reconciled: the ledger has {candidate} running but "
               f"{cfg.grid_env.name} carries no candidate; A/B closed")
    else:
        last = next((r for r in reversed(rows) if r["decision"] in _AB_DECISIONS), None)
        stale = last is not None and last["decision"] == "flip_incomplete"
        if not env_candidate and not stale:
            return None                     # nothing running anywhere: consistent
        candidate = env_candidate or last.get("version")
        why = (f"colony A/B reconciled: the last decision for {candidate} was flip_incomplete; "
               "candidate cleared and the relay restarted"
               if stale else
               f"colony A/B reconciled: {candidate} is split in {cfg.grid_env.name} but the "
               "ledger closed its A/B")
    outcome = end_ab(cfg, dimension=dimension, local_run=local_run, dry_run=dry_run)
    if outcome != "ok":
        why = f"{why}; relay restart failed clearing the A/B"
    last_rel = _last_released(versions, dimension=dimension)
    row = release_row(week=_iso_week(), decision="held" if outcome == "ok" else "flip_incomplete",
                      dimension=dimension, version=candidate, previous=last_rel["version"] if last_rel else None,
                      base="", run="", adapter_sha256="", gguf_sha256="", set_manifest_sha256="",
                      grid_examples={}, verdict_window=_window(cfg.verdicts_dir),
                      evals={"colony_ab": {"reconciled": True, "binding": False, "candidate": candidate,
                                           "winner": None, "short_arms": [],
                                           "other_dimensions": _other_dimensions(cfg, versions, dimension)}},
                      spend_usd=0.0, rogue_ledger_ts=0.0, reason=why)
    return _ledger(versions, row, dry_run=dry_run)


def ab_check(cfg: WeeklyConfig, *, dimension: str = UNSALTED_DIMENSION, now_ms: int | None = None,
             local_run, dry_run: bool = False) -> dict | None:
    """The daily stuck check (spec §8): abort a dead A/B within 48 h instead of waiting for
    Sunday. Returns None when no A/B is running, or when it isn't (yet) stuck.

    One dimension per call, reconciler included: a dead prompt A/B and a healthy model A/B are
    two independent facts, and neither is evidence about the other."""
    row = ab_reconcile(cfg, dimension=dimension, local_run=local_run, dry_run=dry_run)   # N4: resting state first
    if row is not None:
        return row
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    state = ab_state(versions, dimension=dimension)
    if state is None:
        return None
    env_incumbent = _live_arm(cfg, dimension)
    if env_incumbent != state["incumbent"]:
        # I2: with the incumbent relabelled mid-A/B its arm reads 0 settled, which would make
        # every A/B look stuck the moment 48 h passed. Do nothing -- but say so every night for
        # the (now bounded, N1) two weeks until the Sunday gate closes the A/B, so the operator
        # who ran set-mind-model.sh has a nightly reminder rather than one buried line.
        log.warning("colony A/B: incumbent label changed mid-A/B (%s != %s); stuck check skipped "
                    "-- the Sunday gate will close this A/B at MAX_WEEKS=%d",
                    env_incumbent, state["incumbent"], MAX_WEEKS)
        return None
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    arms = ab_arms(cfg.verdicts_dir, since_ts=state["since_ts"], incumbent=state["incumbent"],
                   candidate=state["candidate"], dimension=dimension)
    if not is_stuck(arms, since_ts=state["since_ts"], now_ms=now, incumbent=state["incumbent"], candidate=state["candidate"]):
        return None
    outcome = end_ab(cfg, dimension=dimension, local_run=local_run, dry_run=dry_run)
    started_ab = (state["started_row"].get("evals") or {}).get("colony_ab") or {}   # I3
    last = _last_released(versions, dimension=dimension)
    incumbent_settled = arms[state["incumbent"]]["settled"]
    # N2: lost_tags reads this; without it a 48 h outage blacklists an innocent tag. D2: the bar
    # here is 1, not MIN_SAMPLE -- stuck is about producing NOTHING. is_stuck guarantees the
    # incumbent is either >= STUCK_INCUMBENT_MIN or at zero, so this yields [candidate] for the
    # classic case (blacklisted, correct) and both arms for a colony outage (not blacklisted).
    block = {**started_ab, "binding": True, "arms": arms, "weeks": state["weeks"], "winner": None,
             "short_arms": short_arms(arms, incumbent=state["incumbent"], candidate=state["candidate"], min_sample=1),
             "other_dimensions": _other_dimensions(cfg, versions, dimension)}
    base_reason = ("colony A/B stuck: no proposals from either arm in 48h" if incumbent_settled == 0
                   else f"colony A/B stuck: candidate 0 settled in 48h (incumbent {incumbent_settled})")
    if outcome == "ok":
        decision, reason = "held", base_reason
    else:
        decision, reason = "flip_incomplete", f"{base_reason}; relay restart failed clearing the A/B"
    row = release_row(week=_iso_week(), decision=decision, dimension=dimension, version=state["candidate"],
                      previous=last["version"] if last else None,
                      base="", run="", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={},
                      verdict_window=_window(cfg.verdicts_dir), evals={"colony_ab": block}, spend_usd=0.0, rogue_ledger_ts=0.0,
                      reason=reason)
    return _ledger(versions, row, dry_run=dry_run)


def ab_forget(cfg: WeeklyConfig, tag: str, *, dimension: str = UNSALTED_DIMENSION, dry_run: bool = False) -> dict:
    """Operator override: make a lost/stuck tag retestable again (spec §8).

    Dimensioned because `lost_tags` is: it reads the forget row through the same filter as the
    loss it undoes, so a forget written in the wrong dimension clears nothing and a prompt
    variant that lost once could never be retested at all -- a one-way memory."""
    _dimension_env(dimension)                 # M4: the same refusal every other verb makes
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    row = release_row(week=_iso_week(), decision="unchanged", dimension=dimension, version=tag,
                      previous=None, base="", run="",
                      adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={}, verdict_window={},
                      evals={}, spend_usd=0.0, rogue_ledger_ts=0.0, reason=f"operator forgot the colony A/B loss of {tag}")
    return _ledger(versions, row, dry_run=dry_run)


def rollback(cfg: WeeklyConfig, remote, *, to: str | None = None, local_run, dry_run: bool = False) -> dict:
    """Move the alias back to an earlier MODEL release. Model-only, and scoped to say so.

    C1: every ledger read here is filtered to the model dimension, because `rollback` copies a
    tag onto `cfg.current_alias` on debain2 and writes MIND_MODEL_ALIAS. Read unfiltered, the
    first `released` row a prompt A/B ever writes becomes "the last release": the default target
    is then that row's `previous` -- `None` at first, so the emergency lever dies with "nothing
    to roll back to", and a prompt VARIANT ID once two prompt releases have landed, at which
    point this would run `ollama cp <variant-id> kannaka-brain-current` and point the live relay
    at a model that does not exist. Nothing rolls back a prompt today: the prompt flip is one
    grid.env line, so the operator route is `ab --forget` plus an edit, and inventing a prompt
    rollback verb belongs with the rest of the CLI work.
    """
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    last = _last_released(versions, dimension=UNSALTED_DIMENSION)
    if last is None:
        raise SystemExit("nothing has been released; nothing to roll back")
    model_rows = [r for r in versions.rows() if dimension_of(r) == UNSALTED_DIMENSION]
    if to is None:
        # I2: `last["previous"]` is the version this release/rollback actually superseded;
        # `previous_released(before=...)` walks release-order and would return a version that
        # was itself later rolled back as bad (e.g. after release v3, release v4, rollback to
        # v3, release v5: the default target must be v3, not v4).
        prev_version = last.get("previous")
        if prev_version is None:
            raise SystemExit("nothing to roll back to")
        matches = [r for r in model_rows if r["decision"] in ("released", "rolled_back") and r["version"] == prev_version]
        target = matches[-1] if matches else None
    else:
        target = next((r for r in model_rows if r["decision"] == "released" and r["version"] == to), None)
    if target is None:
        raise SystemExit(f"{to or 'the previous release'} was never released; refusing")
    # Deliberately NOT interlocked. This is the emergency lever, and refusing it because an
    # experiment is running would trade a recoverable confound for an unrecoverable outage; unlike
    # `release` it writes no binding colony verdict of its own -- the confounded verdict is NEXT
    # Sunday's, which the operator can still pre-empt with `ab --abort`. But the fact that a week
    # was confounded must not survive only in whoever was at the keyboard: swapping the weights
    # under a running prompt A/B invalidates every proposal that arm has produced, and the ledger
    # is the only place a reader will look for that months later. Recorded, and warned about now.
    #
    # Read BEFORE the flip, which changes what the env says, and spliced BEFORE the outcome:
    # D5's exit-code contract keys on the reason ENDING in ": flip_failed", so anything appended
    # after it would silently turn a rollback that did not roll back into exit 0.
    blocking = _blocking_experiments(cfg, versions, UNSALTED_DIMENSION)
    for d, what in blocking:
        log.warning("rollback with a %s A/B open (%s): its arms have been answered by two different "
                    "models and its verdict is confounded; `grid.weekly ab --abort --dimension %s`",
                    d, what, d)
    confound = "".join(f"; {d} A/B {what} was open and is now confounded" for d, what in blocking)
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
                      evals={}, spend_usd=0.0, rogue_ledger_ts=0.0,
                      reason=f"rollback from {last['version']}{confound}: {outcome}")
    return _ledger(versions, row, dry_run=dry_run)


def _local_run(cmd: list[str]) -> int:
    return subprocess.run(cmd, check=False).returncode


ENV_AB_NOTE = ("env-only A/B: no ab_started row, so the window start is unknown; "
               "arms cover all recorded history")


def _env_ab_view(cfg: WeeklyConfig, candidate: str, *, dimension: str = UNSALTED_DIMENSION) -> dict:
    """I4: spec §7's receipt hand-starts an A/B by editing grid.env, so it has no ledger row
    at all. Report it anyway rather than printing `null` at an operator who can see the split
    running -- with arms over ALL history, since there is no since_ts to window on.

    Dimensioned throughout: the split line it quotes, the incumbent it names and the arms it
    counts. Read undimensioned, a hand-started PROMPT split would be reported with the model
    alias as its incumbent and the model's per-alias tallies as its arms -- the wrong
    dimension's numbers, in the one view an operator reads when they cannot tell what is
    running, which is precisely when they cannot check them.

    The incumbent comes from `_live_arm`, not from the raw line: for `prompt` an absent
    MIND_PROMPT_VARIANT means the registry default, and that is the label the proposal log
    files rows under, so the raw (empty) line would name an arm that counts nothing.
    """
    keys = _dimension_env(dimension)
    incumbent = _live_arm(cfg, dimension) or ""
    arms = ab_arms(cfg.verdicts_dir, since_ts=0, incumbent=incumbent, candidate=candidate,
                   dimension=dimension)
    # N7: ab_arms keys on `str(row.get("model_alias", ""))`, so an empty incumbent would collect
    # every unattributed verdict row and report the total as the incumbent's. Drop that arm
    # instead of publishing a number that is not a model's, in the one view an operator reads
    # when they cannot tell what is running.
    if not incumbent:
        arms.pop("", None)
    return {"source": "env", "candidate": candidate, "incumbent": incumbent or None,
            "split": _env_get(cfg.grid_env, keys["split"]) or str(AB_SPLIT),
            "arms": arms, "note": ENV_AB_NOTE}


def _ab_snapshot(cfg: WeeklyConfig, versions: Versions, state: dict | None, candidate: str, *,
                 dimension: str = UNSALTED_DIMENSION) -> dict:
    """The `colony_ab` block an abort records: what the arms actually showed when the operator
    pulled the plug. Without it the row says only that someone aborted, and the week's numbers
    -- the only reason to abort -- are gone.

    It carries the advisory `other_dimensions` key for the same reason every other A/B row does:
    an abort is most often WHY a week's numbers stopped meaning anything, and what else was
    splitting at that moment is half the explanation.
    """
    other = _other_dimensions(cfg, versions, dimension)
    if state is None:
        return {**_env_ab_view(cfg, candidate, dimension=dimension), "binding": False,
                "winner": None, "other_dimensions": other}
    started_ab = (state["started_row"].get("evals") or {}).get("colony_ab") or {}
    arms = ab_arms(cfg.verdicts_dir, since_ts=state["since_ts"], incumbent=state["incumbent"],
                   candidate=state["candidate"], dimension=dimension)
    return {**started_ab, "binding": True, "arms": arms, "weeks": state["weeks"], "winner": None,
            "other_dimensions": other}


def _ab_view(cfg: WeeklyConfig, versions: Versions, dimension: str) -> dict | None:
    """What `ab` prints for ONE dimension: the ledger's A/B with its arms, the env-only view
    when the ledger has none, or None when nothing is running in this dimension at all.

    Self-contained -- the arms sit inside the entry rather than beside it -- because with no
    `--dimension` the command prints one of these per dimension, and a sibling `arms` key could
    then only belong to one of them.
    """
    state = ab_state(versions, dimension=dimension)
    if state is None:
        env_candidate = _env_get(cfg.grid_env, _dimension_env(dimension)["candidate"])
        return _env_ab_view(cfg, env_candidate, dimension=dimension) if env_candidate else None
    arms = ab_arms(cfg.verdicts_dir, since_ts=state["since_ts"], incumbent=state["incumbent"],
                   candidate=state["candidate"], dimension=dimension)
    return {**state, "arms": arms}


def _exit_code(row: dict) -> int:
    """N6: one contract for `release`, `ab --check`, `ab --abort` and `rollback`. 3 means the
    verb did not do what it says: grid.env and the live relay disagree and a human must restart
    grid-odin, or the alias was never moved at all.

    D5: `flip_failed` is not a decision -- `rollback` records `held` and puts the flip's own
    outcome verbatim at the end of the reason (`rollback from X: flip_failed`), which is the
    only machine-readable channel it has. A rollback that did not roll back must not exit 0.
    (`release`'s C2 win-but-copy-failed row stays 0 on purpose: `end_ab` ran, env, relay and
    ledger agree, `held` is that verb's normal terminal state, and the gate retries on Sunday.)
    """
    return 3 if row["decision"] == "flip_incomplete" or str(row.get("reason", "")).endswith(": flip_failed") else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="grid.weekly")
    ap.add_argument("verb", choices=("release", "rollback", "status", "ab"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--week", default=None)
    ap.add_argument("--to", default=None)
    # ONE allowlist, odin's, for the relay's arm assignment, the ledger's rows and this parser
    # alike: `choices` is DIMENSIONS itself, so a name odin does not know cannot be typed here
    # and a dimension it gains needs no edit in this file.
    #
    # The default is None rather than "model" because omitting the flag means two different
    # things. `release` and `rollback` act on exactly one experiment, and default to `model` --
    # which is what the systemd unit's bare `grid.weekly release` has always meant. `ab --check`
    # and `ab`'s display are OBSERVERS: a dead prompt A/B and a healthy model one are independent
    # facts and the nightly must see both, so omitting the flag there means every dimension.
    ap.add_argument("--dimension", choices=sorted(DIMENSIONS), default=None)
    # The challenger, for a dimension that has no discovery of its own. `model` reads Rogue's
    # promotion state and refuses one; `prompt` has nothing to read and requires one.
    ap.add_argument("--candidate", default=None, metavar="NAME")
    # The deliberate override of the interlock. Not a bypass of its reasoning: it starts both
    # splits AND changes how the week is judged, from a raw rate to each dimension's marginal.
    # A flag, so it is an explicit act; recorded on the ab_started row, so it is an auditable one.
    ap.add_argument("--factorial", action="store_true")
    ab_mode = ap.add_mutually_exclusive_group()
    ab_mode.add_argument("--abort", action="store_true")
    ab_mode.add_argument("--check", action="store_true")
    ab_mode.add_argument("--forget", default=None, metavar="TAG")
    a = ap.parse_args(argv)
    # A flag a verb has no use for is REFUSED, not ignored. `--candidate` has been parsed and
    # silently discarded on `ab`, `status` and `rollback` since it was added: `ab --forget <tag>
    # --candidate <other>` exits 0 having done something other than what it reads as, which is the
    # worst outcome available for a command an operator reaches for once a quarter. `--factorial`
    # lands on the same flat parser and would inherit the same hole. argparse has no per-verb
    # scoping here, so the scoping is this table -- one line per flag, not a subparser matrix.
    for flag, given in (("--candidate", a.candidate is not None), ("--factorial", a.factorial)):
        if given and a.verb != "release":
            ap.error(f"{flag} is only meaningful on `release`, not `{a.verb}`")
    dimension = a.dimension or UNSALTED_DIMENSION
    cfg = WeeklyConfig.from_env()
    cfg.releases_dir.mkdir(parents=True, exist_ok=True)
    remote = Remote(cfg.debain2_host, dry_run=a.dry_run, log=lambda s: log.info("%s", s))
    if a.verb == "status":
        v = Versions(cfg.releases_dir / "versions.jsonl")
        out = {"rows": len(v.rows()), "verify": v.verify()}
        if a.dimension:
            # Scoped. Silently ignoring the flag would be worse than refusing it: `status
            # --dimension prompt` printing the model's answers is a wrong answer to a precise
            # question. `candidate` is find_candidate's Rogue promotion state -- a MODEL fact,
            # and no other dimension has one -- so it is left off rather than faked.
            out["released"] = (_last_released(v, dimension=a.dimension) or {}).get("version")
            if a.dimension == UNSALTED_DIMENSION:
                out["candidate"] = find_candidate(remote, cfg)
            out["ab"] = _ab_view(cfg, v, a.dimension)
        else:
            # C1: bare `status` answers "what is live", and `released` sits beside `candidate`,
            # which is a model tag -- read unfiltered it would answer a question about the model
            # with a prompt variant id, so both stay model facts and say so.
            out["released"] = (_last_released(v, dimension=UNSALTED_DIMENSION) or {}).get("version")
            out["candidate"] = find_candidate(remote, cfg)
            # ...but the A/B block is every dimension, mirroring bare `ab`. Model-only, this is
            # the one command an operator runs to ask what is going on and it would not mention a
            # prompt experiment splitting live traffic at all.
            out["ab"] = {d: _ab_view(cfg, v, d) for d in sorted(DIMENSIONS)}
        print(json.dumps(out, indent=1, default=str))
        return 0
    local_run = (lambda cmd: (log.info("[dry-run] %s", " ".join(cmd)) or 0)) if a.dry_run else _local_run
    if a.verb == "ab":
        versions = Versions(cfg.releases_dir / "versions.jsonl")
        if a.forget:
            # One dimension, defaulting to model: a forget is an operator UNDOING one recorded
            # loss, and `lost_tags` reads it through the same dimension filter as the loss it
            # undoes. "Forget everything, everywhere" is not a thing anyone means to type.
            row = ab_forget(cfg, a.forget, dimension=dimension, dry_run=a.dry_run)
            print(json.dumps(row, default=str)); return 0
        if a.abort:
            state = ab_state(versions, dimension=dimension)
            env_candidate = _env_get(cfg.grid_env, _dimension_env(dimension)["candidate"])
            # N2: act when EITHER the ledger shows a running A/B OR the env still carries
            # a stray candidate with no ledger backing it (e.g. left behind by a start-path
            # restart failure before this fix) -- only "no A/B running" when both are absent.
            if state is None and not env_candidate:
                print("no A/B running"); return 0
            candidate = state["candidate"] if state is not None else env_candidate
            block = _ab_snapshot(cfg, versions, state, candidate, dimension=dimension)
            outcome = end_ab(cfg, dimension=dimension, local_run=local_run, dry_run=a.dry_run)
            # C1: `previous` must name a release IN THE DIMENSION THIS ROW CLAIMS. Unfiltered,
            # an abort after any prompt release stamps a prompt variant id onto a model row --
            # a falsehood in a file that can never be corrected.
            last = _last_released(versions, dimension=dimension)
            if outcome == "ok":
                decision, reason = "held", "colony A/B aborted by operator"
            else:
                decision, reason = "flip_incomplete", "colony A/B abort requested but the relay restart failed"
            row = release_row(week=_iso_week(), decision=decision, dimension=dimension, version=candidate,
                              previous=last["version"] if last else None,
                              base="", run="", adapter_sha256="", gguf_sha256="", set_manifest_sha256="", grid_examples={},
                              verdict_window=_window(cfg.verdicts_dir), evals={"colony_ab": block}, spend_usd=0.0,
                              rogue_ledger_ts=0.0, reason=reason)
            row = _ledger(versions, row, dry_run=a.dry_run)
            print(json.dumps(row, default=str))
            return _exit_code(row)          # N6: same contract as release and --check
        dimensions = [a.dimension] if a.dimension else sorted(DIMENSIONS)
        if a.check:
            # N4: no `state is None` short-circuit -- ab_check's reconciler must run even when
            # the ledger shows no A/B, since that is exactly the state a standing
            # flip_incomplete leaves behind.
            #
            # D: every open dimension, one at a time, and the LOUDEST exit code any of them
            # produced. Run on `model` alone -- which is all this did before the flag existed --
            # a wedged prompt experiment has no nightly at all: nothing reconciles it, nothing
            # calls it stuck, and the unit goes on exiting 0 while a share of live traffic goes
            # to a candidate the ledger closed. And a failure MUST NOT be masked: 0 from the
            # dimension that was fine would say the night was fine.
            rows, code = [], 0
            for d in dimensions:
                row = ab_check(cfg, dimension=d, local_run=local_run, dry_run=a.dry_run)
                if row is not None:
                    rows.append(row)
                    code = max(code, _exit_code(row))
            if not rows:
                running = any(ab_state(versions, dimension=d) is not None for d in dimensions)
                print("not stuck" if running else "no A/B running"); return 0
            for row in rows:                # one JSON object per line: two dimensions can both act
                print(json.dumps(row, default=str))
            # I1: this runs as grid-verdicts' ExecStartPost, where a non-zero exit marks the
            # nightly unit failed -- which is exactly right for `flip_incomplete`: the env now
            # says "no A/B" while the live relay is still splitting, and only an operator
            # restarting grid-odin closes that gap. Every other outcome is routine, hence 0.
            return code
        if a.dimension:
            print(json.dumps({"ab": _ab_view(cfg, versions, a.dimension)}, default=str))
        else:
            print(json.dumps({"ab": {d: _ab_view(cfg, versions, d) for d in dimensions}}, default=str))
        return 0
    if a.verb == "rollback":
        if dimension != UNSALTED_DIMENSION:
            # Nothing rolls back a prompt, and inventing it here would be worse than refusing:
            # `rollback` copies a tag onto kannaka-brain-current on debain2 and writes the model
            # alias, so run under another dimension it would point the live relay at a model that
            # does not exist. The operator route is the forget plus an edit of the one env line.
            raise SystemExit(f"there is no {dimension} rollback: that flip is a single grid.env "
                             f"line, so set {_dimension_env(dimension)['live']} by hand and run "
                             f"`grid.weekly ab --forget <name> --dimension {dimension}` to make "
                             "the old value testable again")
        row = rollback(cfg, remote, to=a.to, local_run=local_run, dry_run=a.dry_run)
        print(json.dumps(row, default=str))
        return _exit_code(row)          # N6: same contract as release and --check
    versions = Versions(cfg.releases_dir / "versions.jsonl")
    # ARGUMENTS BEFORE THE LEDGER. Whether a candidate NAME is one this dimension can act on is a
    # fact about the argument, and must not depend on what some other experiment happens to be
    # doing: checked after the interlock, `release --candidate <a tag Rogue never promoted>` and
    # `release --dimension prompt --candidate <typo>` stop being usage errors and become exit-0
    # `unchanged` rows carrying that fabricated version into an append-only, hash-chained file,
    # on exactly the weeks an operator is most likely to have typed something wrong.
    #
    # The PRESENCE rule stays below the interlock, and only there: a week the interlock refuses
    # starts nothing, so it needs no candidate -- and the brief's contract is that such a week is
    # a recorded row at exit 0, not a usage error.
    try:
        _refuse_given_model_candidate(dimension, a.candidate)
        if a.candidate is not None:
            # Before the argument is accepted, and again in start_ab before it is written.
            _validate_candidate(dimension, a.candidate)
    except ValueError as e:
        ap.error(str(e))
    # The interlock, and only on a Sunday that would START one: a dimension with its own A/B
    # already open is being JUDGED today, and refusing that would leave both experiments running
    # forever with nothing able to close either.
    open_here = ab_state(versions, dimension=dimension) is not None
    blocking = [] if open_here else _blocking_experiments(cfg, versions, dimension)
    # Both `--factorial` guards, mirrored from `release()` so the CLI exits 2 with a usage error
    # rather than a traceback -- the same split `_validate_candidate` keeps. They are LEDGER
    # facts, not argument facts, so unlike `--candidate`'s checks they sit here, below it.
    if a.factorial and open_here:
        ap.error(f"there is already a {dimension} A/B open; --factorial starts a factorial run, "
                 "it does not change how a running one is judged")
    if a.factorial and not blocking:
        ap.error("--factorial overrides the interlock, and nothing is blocking: no other "
                 f"dimension is splitting live traffic, so a {dimension} experiment started now "
                 "is an ordinary single-dimension run")
    if blocking and not a.factorial:
        # A RECORDED row, not a printed message. `unchanged` is the ledger's word for "the gate
        # ran and moved nothing", and without the row a Sunday that did nothing is indistinguishable
        # from a Sunday the timer never fired -- which is exactly the question an operator asks
        # when they come back to an experiment that never started.
        last = _last_released(versions, dimension=dimension)
        row = _ledger(versions, release_row(
            week=a.week or _iso_week(), decision="unchanged", dimension=dimension, version=a.candidate,
            previous=last["version"] if last else None, base="", run="", adapter_sha256="", gguf_sha256="",
            set_manifest_sha256="", grid_examples={}, verdict_window=_window(cfg.verdicts_dir),
            evals=_advisory_evals(cfg, dimension), spend_usd=0.0, rogue_ledger_ts=0.0,
            reason=_interlock_reason(blocking, dimension)), dry_run=a.dry_run)
    else:
        if dimension != UNSALTED_DIMENSION and not a.candidate:
            ap.error(f"a {dimension} release needs --candidate; nothing discovers one")
        # Model-only: the key exists to authenticate the identity and validity PROBES at the
        # gateway, and those measure weights. A prompt release runs none of them, so a host
        # whose colony key is missing must not die reading a file this run never uses.
        key = cfg.key_file.read_text(encoding="utf-8").strip() if dimension == UNSALTED_DIMENSION else ""
        ask_factory = lambda model: (lambda prompt: chat(cfg.gateway_url, key, model, prompt, timeout_s=cfg.probe_timeout_s))
        if a.dry_run:
            async def fake(prompt): return "I am Kannaka. []"
            ask_factory = lambda model: fake
        row = asyncio.run(release(cfg, remote, ask_factory=ask_factory, local_run=local_run, week=a.week,
                                  dry_run=a.dry_run, dimension=dimension, candidate=a.candidate,
                                  factorial=a.factorial))
    print(json.dumps({k: row[k] for k in ("week", "decision", "version", "previous", "reason", "spend_usd", "over_budget")}, default=str))
    return _exit_code(row)


if __name__ == "__main__":
    sys.exit(main())
