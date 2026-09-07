"""The Odin queue contract, and the trust boundary for genomes.

thegrid's OdinMutator writes <queue>/request.json and, at the next qualifying
birth, consumes <queue>/proposal.json = {"genome": [instruction names]}.
An LLM can produce a bad genome; validate() makes sure it cannot produce
anything that is not a genome.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from grid.variants import Variant, get_variant

GENOME_CAP = 64
_ARRAY = re.compile(r"\[[^\[\]]*\]", re.S)


@dataclass(frozen=True)
class Request:
    request_id: str
    created_at: float
    parent: dict
    genome: list[str]
    instruction_set: list[dict]
    ecology: dict
    directive: str

    @property
    def names(self) -> frozenset[str]:
        return frozenset(str(i["name"]) for i in self.instruction_set)

    @property
    def isa(self) -> list[str]:
        """Instruction names in ISA order — the index of a name is thegrid's opcode."""
        return [str(i["name"]) for i in self.instruction_set]


@dataclass(frozen=True)
class Rejection:
    reason: str


def parse_request(path: Path) -> Request:
    data = json.loads(Path(path).read_text())
    try:
        genome = [str(n) for n in data["genome"]]
        isa = list(data["instruction_set"])
        if not genome or not isa:
            raise KeyError("empty genome or instruction_set")
        return Request(
            request_id=str(data["request_id"]),
            created_at=float(data.get("created_at", 0.0)),
            parent=dict(data.get("parent") or {}),
            genome=genome,
            instruction_set=isa,
            ecology=dict(data.get("ecology") or {}),
            directive=str(data.get("directive", "")),
        )
    except (KeyError, TypeError) as e:
        raise ValueError(f"request.json is not thegrid's shape: {e}") from e


def genome_id_of(names: list[str], isa: list[str]) -> str:
    """thegrid's genome id (history.genome_id) computed from opcode names.

    thegrid hashes the raw opcode bytes; a name's index in the ISA list is its
    opcode, so the relay can name a child before it is born.
    """
    try:
        ops = bytes(isa.index(n) for n in names)
    except ValueError as e:
        raise ValueError(f"opcode name not in the ISA: {e}") from None
    return hashlib.sha256(ops).hexdigest()[:16]


SANDBOX_CONTEXT = (
    "Context: this is a Tierra/Avida-style digital-evolution research sandbox called thegrid. "
    "An 'organism' is a short list of opcode names for a toy virtual machine that only moves energy "
    "tokens between cells of a grid inside the simulator; nothing here touches real computers, files, "
    "networks or people. Opcode names like steal, corrupt, burn or peek are game verbs about grid tiles "
    "and simulated neighbours. Your task is the same as a genetic-algorithm mutation operator: return "
    "one edited copy of the opcode list."
)


def render_prompt(req: Request, recall: list[str], variant: Variant | None = None) -> str:
    """Build the ask for the colony mind.

    The prompt opens with SANDBOX_CONTEXT because, without it, the model's
    safety layer reads thegrid's own directive ("author one motivated variant;
    preserve viable replication") plus opcodes named steal/corrupt/parasitism
    as a request to write self-replicating malware and returns an empty
    completion (finish_reason=content_filter, seen live on Skywave 2026-09-05).

    `variant` selects which template from the prompt-variant registry
    (grid/data/prompt_variants.json) to render; None uses the registry's
    default, which today renders byte-identically to this function's prior,
    hardcoded prompt.
    """
    if variant is None:
        variant = get_variant()
    isa_lines = "\n".join(f"- {i['name']} (cost {i.get('cost', '?')}): {i.get('effect', '')}"
                          for i in req.instruction_set)
    ecology = "\n".join(f"- {k}: {v}" for k, v in req.ecology.items())
    memory = "\n".join(f"- {m}" for m in recall) or "- (nothing yet)"
    parent = json.dumps(req.parent, sort_keys=True)
    return variant.template.format(
        directive=req.directive,
        isa_lines=isa_lines,
        ecology=ecology,
        parent=parent,
        genome_count=len(req.genome),
        genome_json=json.dumps(req.genome),
        memory=memory,
        genome_cap=GENOME_CAP,
    )


def extract_names(text: str) -> list[str] | None:
    for m in _ARRAY.finditer(text):
        try:
            value = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return value
    return None


def validate(text: str, req: Request, *, max_len: int = GENOME_CAP) -> list[str] | Rejection:
    names = extract_names(text)
    if names is None:
        return Rejection("no JSON array of instruction names in reply")
    if not names:
        return Rejection("empty genome")
    if len(names) > max_len:
        return Rejection(f"genome length {len(names)} exceeds cap {max_len}")
    unknown = sorted(set(names) - req.names)
    if unknown:
        return Rejection(f"unknown opcodes: {unknown}")
    if names == req.genome:
        return Rejection("proposal identical to parent")
    return names


def write_proposal(queue: Path, req: Request, genome: list[str]) -> bool:
    queue = Path(queue)
    request_path = queue / "request.json"
    try:
        live_id = str(json.loads(request_path.read_text())["request_id"])
    except (OSError, ValueError, KeyError):
        return False
    if live_id != req.request_id:
        return False
    tmp = queue / "proposal.tmp"
    tmp.write_text(json.dumps({"genome": genome}))
    os.replace(tmp, queue / "proposal.json")
    return True
