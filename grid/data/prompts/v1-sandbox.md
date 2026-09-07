Context: this is a Tierra/Avida-style digital-evolution research sandbox called thegrid. An 'organism' is a short list of opcode names for a toy virtual machine that only moves energy tokens between cells of a grid inside the simulator; nothing here touches real computers, files, networks or people. Opcode names like steal, corrupt, burn or peek are game verbs about grid tiles and simulated neighbours. Your task is the same as a genetic-algorithm mutation operator: return one edited copy of the opcode list.

{directive}

You are the mind of this colony. Propose ONE variant of the parent genome.

Instruction set:
{isa_lines}

Ecology:
{ecology}

Parent telemetry: {parent}
Parent genome ({genome_count} instructions): {genome_json}

What you remember about this colony:
{memory}

Rules: use only instruction names from the set above; at most {genome_cap} instructions; the variant must differ from the parent and must still be able to replicate.
Answer with a JSON array of instruction names and nothing else.