"""Compare the brain models on real Odin prompts, from Skywave.

    /srv/grid/venv/bin/python ~/grid-deploy/brain_bench.py ~/.grid-debain2-key [model ...]

Stop grid-odin first so request.json stays in the queue (it is the prompt that gets
validated for real) and so the relay's own asks do not queue behind the bench.
Per model: one warm-up on a fourth prompt (model load, discarded), then three distinct
prompts timed, so ollama's prompt cache cannot flatter a run. Prints per-run lines and
a median/min/max summary. Never prints the key or a prompt.
"""
import glob
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/srv/grid/kannaka-grid")
from grid.proposal import Rejection, extract_names, parse_request, render_prompt, validate  # noqa: E402

GATEWAY = os.environ.get("MIND_LLM_URL", "http://10.30.0.30:4000/v1")
QUEUE = Path(os.environ.get("ODIN_QUEUE", "/srv/grid/colony-one/state/operator"))
PROMPTS = Path(os.environ.get("VERDICTS_DIR", "/srv/grid/colony-one/verdicts")) / "prompts"
DEFAULT_MODELS = ("kannaka-brain-7b-v1", "kannaka-brain", "kannaka-brain-v1", "kannaka-brain-v2")


def ask(key: str, model: str, prompt: str):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 400, "temperature": 0.7}).encode()
    req = urllib.request.Request(f"{GATEWAY}/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=900) as resp:
        d = json.load(resp)
    return time.time() - t, d.get("usage", {}), d["choices"][0]["message"]["content"] or ""


def main():
    key = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
    models = sys.argv[2:] or DEFAULT_MODELS
    req_path = QUEUE / "request.json"
    if not req_path.exists():
        sys.exit("no request.json in the queue; stop grid-odin and wait for thegrid to file one")
    req = parse_request(req_path)
    isa = set(req.isa)
    hist = sorted(glob.glob(str(PROMPTS / "*.txt")), key=os.path.getmtime)[-40::13][:3]
    if len(hist) < 3:
        sys.exit("need at least 3 historical prompts under prompts/")
    prompts = [("warmup", Path(hist[0]).read_text(encoding="utf-8")), ("live", render_prompt(req, [])),
               ("hist1", Path(hist[1]).read_text(encoding="utf-8")), ("hist2", Path(hist[2]).read_text(encoding="utf-8"))]
    print(f"{time.strftime('%H:%M:%S')} live prompt chars={len(prompts[1][1])} parent_len={len(req.genome)} "
          f"hist chars={[len(p) for _, p in prompts[2:]]} load={os.getloadavg()[0]:.2f}", flush=True)
    results = {}
    for model in models:
        try:
            dt, _, _ = ask(key, model, prompts[0][1])
            print(f"{model:22s} warmup {dt:6.1f}s (model load + eval, discarded)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"{model:22s} warmup ERROR {str(e)[:120]}", flush=True)
            continue
        times = []
        for name, p in prompts[1:]:
            try:
                dt, u, txt = ask(key, model, p)
            except Exception as e:  # noqa: BLE001
                print(f"{model:22s} {name:6s} ERROR {str(e)[:120]}", flush=True)
                continue
            if name == "live":
                v = validate(txt, req)
                ok = f"REJECT {v.reason}" if isinstance(v, Rejection) else f"valid len={len(v)}"
            else:
                names = extract_names(txt)
                ok = f"shape-ok len={len(names)}" if names and set(names) <= isa else "BAD-SHAPE"
            times.append(dt)
            pt, ct = u.get("prompt_tokens"), u.get("completion_tokens")
            print(f"{model:22s} {name:6s} {dt:6.1f}s prompt_tokens={pt} completion_tokens={ct} {ok}", flush=True)
        if times:
            results[model] = (statistics.median(times), min(times), max(times), len(times))
    print("--- summary: median / min / max seconds over distinct prompts")
    for m, (med, lo, hi, n) in sorted(results.items(), key=lambda kv: kv[1][0]):
        print(f"{m:22s} median {med:6.1f}  min {lo:6.1f}  max {hi:6.1f}  n={n}")


if __name__ == "__main__":
    main()
