# AI Fabric Lab Example

This directory contains the runnable skeleton for the first two-small-LLM plus
Hyperon/DAS-adjacent k1s fabric experiment.

Start with:

```bash
python3 scripts/dev/ai_fabric_lab.py validate
python3 scripts/dev/ai_fabric_lab.py init-storage
```

Then build the four local images documented in `docs/ai-fabric-lab.md` and
deploy `examples/ai-fabric-lab/stage` through WorkerBee. The shared model image
is deployed as separate coordinator and expert workloads so each vLLM server has
isolated startup and memory profiling.
The shared launcher reads `run_defaults.attention_backend` and passes it as
vLLM's `--attention-backend` argument; the smoke default uses Triton attention
for RTX 8000 compatibility.

The default model track is the all-Qwen `baseline`. Use `smoke` before
downloading the larger expert model, and use `quality` for comparison runs.
The `legacy-smollm-smoke` track is retained only for fast plumbing checks.
