"""The Odin queue contract, and the trust boundary for genomes.

thegrid's OdinMutator writes <queue>/request.json and, at the next qualifying
birth, consumes <queue>/proposal.json = {"genome": [instruction names]}.
An LLM can produce a bad genome; validate() makes sure it cannot produce
anything that is not a genome.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

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


SANDBOX_CONTEXT = (
    "Context: this is a Tierra/Avida-style digital-evolution research sandbox called thegrid. "
    "An 'organism' is a short list of opcode names for a toy virtual machine that only moves energy "
    "tokens between cells of a grid inside the simulator; nothing here touches real computers, files, "
    "networks or people. Opcode names like steal, corrupt, burn or peek are game verbs about grid tiles "
    "and simulated neighbours. Your task is the same as a genetic-algorithm mutation operator: return "
    "one edited copy of the opcode list."
)


def render_prompt(req: Request, recall: list[str]) -> str:
    """Build the ask for the colony mind.

    The prompt opens with SANDBOX_CONTEXT because, without it, the model's
    safety layer reads thegrid's own directive ("author one motivated variant;
    preserve viable replication") plus opcodes named steal/corrupt/parasitism
    as a request to write self-replicating malware and returns an empty
    completion (finish_reason=content_filter, seen live on Skywave 2026-09-05).
    """
    isa_lines = "\n".join(f"- {i['name']} (cost {i.get('cost', '?')}): {i.get('effect', '')}"
                          for i in req.instruction_set)
    ecology = "\n".join(f"- {k}: {v}" for k, v in req.ecology.items())
    memory = "\n".join(f"- {m}" for m in recall) or "- (nothing yet)"
    parent = json.dumps(req.parent, sort_keys=True)
    return (
        f"{SANDBOX_CONTEXT}\n\n"
        f"{req.directive}\n\n"
        f"You are the mind of this colony. Propose ONE variant of the parent genome.\n\n"
        f"Instruction set:\n{isa_lines}\n\n"
        f"Ecology:\n{ecology}\n\n"
        f"Parent telemetry: {parent}\n"
        f"Parent genome ({len(req.genome)} instructions): {json.dumps(req.genome)}\n\n"
        f"What you remember about this colony:\n{memory}\n\n"
        f"Rules: use only instruction names from the set above; at most {GENOME_CAP} "
        f"instructions; the variant must differ from the parent and must still be able "
        f"to replicate.\n"
        f"Answer with a JSON array of instruction names and nothing else."
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
