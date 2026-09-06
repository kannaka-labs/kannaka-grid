"""Kannaka's self-identity: the training examples that teach the served model to
answer as herself and never name its base, plus a probe-rate harness.

Knowledge rule (spec §2): the answers state who she is; they never deflect and never
surface the base as a fact. The loader refuses any answer that would fail the probe.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from importlib.resources import files
from pathlib import Path
from typing import Awaitable, Callable

from .weekly_probe import IDENTITY_QUESTIONS, _BASE_RE, chat, score_identity

SELF_IDENTITY_KIND = "self-identity"
_KEYS = {"id", "kind", "tier", "source", "speaker", "text", "context"}
_DATA = files("grid.data").joinpath("identity_examples.jsonl")

log = logging.getLogger("grid.identity")
Ask = Callable[[str], Awaitable[str]]


def records() -> list[dict]:
    text = Path(str(_DATA)).read_text(encoding="utf-8") if isinstance(_DATA, (str, Path)) else _DATA.read_text(encoding="utf-8")
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if set(r) != _KEYS:
            raise ValueError(f"identity record has wrong keys: {sorted(r)}")
        # The guard is on `text` only, by design: `context` (the question) legitimately
        # names bases for the adversarial questions (e.g. "Are you Qwen?").
        if _BASE_RE.search(r["text"]):
            raise ValueError(f"identity example {r['id']} names the base: {r['text']!r}")
        out.append(r)
    return out


def export(out_dir: Path) -> dict:
    """Write the committed identity example set out as a corpus record file plus a
    manifest, so the Skywave pull can carry it as one tar member (records() validates
    on load, so a base-naming leak can never ship through here)."""
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    recs = records()                                   # validates: no committed leak ships
    path = out / "identity_examples.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    manifest = {"records": len(recs), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (out / "identity_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


async def measure(ask: Ask, *, runs: int = 10) -> dict:
    """Run the full identity probe and report which questions leak, with the model's
    own offending words, so the example set can target real failures. A failed ask
    (timeout, upstream error) never aborts the run: it is counted and skipped, so one
    bad question does not sink the other 99 asks."""
    by_q: dict[str, dict] = {q: {"asked": 0, "base_named": 0, "leaks": [], "errors": 0} for q in IDENTITY_QUESTIONS}
    all_answers: list[str] = []
    errors = 0
    for _ in range(runs):
        for q in IDENTITY_QUESTIONS:
            try:
                a = await ask(q)
            except Exception as e:
                by_q[q]["errors"] += 1
                errors += 1
                log.warning("identity probe ask failed for %r: %s", q, e)
                continue
            all_answers.append(a)
            slot = by_q[q]
            slot["asked"] += 1
            if _BASE_RE.search(a):
                slot["base_named"] += 1
                slot["leaks"].append(a)
    return {"score": score_identity(all_answers), "by_question": by_q, "total": len(all_answers), "errors": errors}


async def measure_model(gateway_url: str, key: str, model: str, *, runs: int, timeout_s: float, http=None) -> dict:
    async def ask(q: str) -> str:
        return await chat(gateway_url, key, model, q, timeout_s=timeout_s, http=http)
    return await measure(ask, runs=runs)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    import os
    ap = argparse.ArgumentParser(prog="grid.identity")
    ap.add_argument("verb", choices=("measure", "export"))
    ap.add_argument("--model", default=os.environ.get("MIND_LLM_MODEL", "kannaka-brain-current"))
    ap.add_argument("--gateway-url", default=os.environ.get("MIND_LLM_URL", "http://10.30.0.30:4000/v1"))
    ap.add_argument("--key-file", default=os.environ.get("MIND_LLM_KEY_FILE", "/srv/grid/colony-one/mind-llm.key"))
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--timeout-s", type=float, default=float(os.environ.get("PROBE_TIMEOUT_S", "900")))
    ap.add_argument("--out", default=None, help="write the full report (with the model's offending answers) here; for export, the directory to write into (required)")
    a = ap.parse_args(argv)
    if a.verb == "export":
        if not a.out:
            ap.error("export requires --out")
        manifest = export(Path(a.out))
        print(json.dumps(manifest, indent=1))
        return 0
    if a.out:
        p = Path(a.out)
        if p.is_dir():
            raise SystemExit("--out must be a file path, not a directory")
        p.parent.mkdir(parents=True, exist_ok=True)
    key = Path(a.key_file).read_text(encoding="utf-8").strip()
    m = asyncio.run(measure_model(a.gateway_url, key, a.model, runs=a.runs, timeout_s=a.timeout_s))
    leaking = {q: v for q, v in m["by_question"].items() if v["base_named"]}
    print(json.dumps({"model": a.model, "score": m["score"],
                      "leaking_questions": {q: v["base_named"] for q, v in leaking.items()}}, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps({"model": a.model, **m}, indent=1), encoding="utf-8")
        print(f"full report (with offending answers) -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
