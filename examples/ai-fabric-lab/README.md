# AI Fabric Lab Example

This directory contains the runnable skeleton for the first two-small-LLM plus
Hyperon/DAS-adjacent k1s fabric experiment.

Start with:

```bash
python3 scripts/dev/ai_fabric_lab.py validate
python3 scripts/dev/ai_fabric_lab.py init-storage
```

Then build the four local images documented in `docs/ai-fabric-lab.md` and
deploy `examples/ai-fabric-lab/stage` through WorkerBee.

The default model track is `baseline`. Use `smoke` before downloading the
larger expert model, and use `quality` for the Qwen coordinator comparison.
