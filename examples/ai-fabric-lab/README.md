# AI Fabric Lab Example

This directory contains the runnable skeleton for the first two-small-LLM plus
Hyperon/DAS-adjacent k1s fabric experiment.

Start with:

```bash
python3 scripts/dev/ai_fabric_lab.py validate
python3 scripts/dev/ai_fabric_lab.py init-storage
python3 scripts/dev/ai_fabric_lab.py sync-corpus
```

Then build the four local images documented in `docs/ai-fabric-lab.md` and
deploy `examples/ai-fabric-lab/stage` through WorkerBee. The shared model image
is deployed as separate coordinator and expert workloads so each vLLM server has
isolated startup and memory profiling.
For the first GPU-free integration check, build the router, DAS bridge,
retrieval indexer, and fake model images, then deploy
`examples/ai-fabric-lab/stage-plumbing`.
The shared launcher reads `run_defaults.attention_backend` and passes it as
vLLM's `--attention-backend` argument; the smoke default uses Triton attention
for RTX 8000 compatibility.

The default model track is the all-Qwen `baseline`. Use `smoke` before
downloading the larger expert model, and use `quality` for comparison runs.
The `legacy-smollm-smoke` track is retained only for fast plumbing checks.

The router advisory endpoints now include retrieval evidence from the local
corpus index. The indexer writes a manifest under
`/srv/storage/k1s/ai-fabric-lab/artifacts/indexes` and serves `/v1/search` for
router and direct runtime probes.

The DAS bridge runs Hyperon DAS when the runtime image is built and keeps a
JSONL fact audit under `/srv/storage/k1s/ai-fabric-lab/das`. Use
`scripts/dev/ai_fabric_lab.py import-runtime-facts` from inside the lab network
or with a reachable DAS URL to seed track, model, project, and repo facts.
Use `scripts/dev/ai_fabric_lab.py emit-f5-evidence` to generate the first
k1s-compatible DAS-cell, query-warming, replication-intent, and cognitive-signal
records under `/srv/storage/k1s/ai-fabric-lab/runs/f5-evidence.json`.
Deploy `stage-hyperon-sidecar` and run
`scripts/dev/ai_fabric_lab.py import-hyperon-advisory` to import a pinned
trueagi Hyperon experimental advisory trace into the k1s Fabric Advisory store
for dashboard review.
