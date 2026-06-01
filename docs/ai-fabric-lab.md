# AI Fabric Lab

This lab stages the first k1s/WorkerBee AI infrastructure experiment from the
local RTX 8000 host. It uses two small dense LLM lanes plus retrieval and
Hyperon/DAS-adjacent symbolic state:

- coordinator LLM for planning, routing, and user-facing synthesis
- Python/k1s/Hyperon expert LLM for code and control-plane advisory work
- Redis, Mongo, and Qdrant for hot state, symbolic persistence, and retrieval
- job-style modality and indexing workers instead of resident modality services

The lab intentionally runs both LLM servers from one GPU-owning workload. That
matches the current k1s reality: `InferenceCell` scheduling is whole-GPU
oriented, while this experiment is meant to produce evidence for future
VRAM-aware admission fields such as `gpu_vram_gib`, `kv_cache_budget_gib`,
`adapter_hotset`, and `context_budget_tokens`.

## Model Tracks

The model matrix lives in
[`examples/ai-fabric-lab/model-tracks.json`](../examples/ai-fabric-lab/model-tracks.json).
All models are pinned to Hugging Face revision SHAs as of the lab definition.

| Track | Coordinator | Expert | Purpose |
| --- | --- | --- | --- |
| `smoke` | `HuggingFaceTB/SmolLM3-3B` | `Qwen/Qwen2.5-Coder-7B-Instruct-AWQ` | Prove the full path with maximum VRAM headroom. |
| `baseline` | `HuggingFaceTB/SmolLM3-3B` | `Qwen/Qwen2.5-Coder-14B-Instruct-AWQ` | Primary development baseline. |
| `quality` | `Qwen/Qwen2.5-7B-Instruct-AWQ` | `Qwen/Qwen2.5-Coder-14B-Instruct-AWQ` | Compare coordinator quality versus the baseline. |

The default runtime track is `baseline`. Start with `smoke` for image/runtime
plumbing, then run `baseline`, then run `quality` against the same prompt set,
corpus snapshot, and DAS facts.

## Storage

The storage layout lives in
[`examples/ai-fabric-lab/storage-layout.json`](../examples/ai-fabric-lab/storage-layout.json)
and defaults to `/srv/storage/k1s/ai-fabric-lab`.

Prepare the layout and copy the pinned lab config into `/srv/storage`:

```bash
python3 scripts/dev/ai_fabric_lab.py init-storage
```

Validate the static lab bundle without starting workloads:

```bash
python3 scripts/dev/ai_fabric_lab.py validate
```

## Images

Build the local development images before deploying the stage:

```bash
workerbee build-image examples/ai-fabric-lab/images/ai-models \
  --tag workerbee-ai-fabric-models:dev
workerbee build-image examples/ai-fabric-lab/images/router \
  --tag workerbee-ai-fabric-router:dev
workerbee build-image examples/ai-fabric-lab/images/das-bridge \
  --tag workerbee-ai-fabric-das-bridge:dev
workerbee build-image examples/ai-fabric-lab/images/retrieval-indexer \
  --tag workerbee-ai-fabric-retrieval-indexer:dev
```

The model image wraps the pinned
`vllm/vllm-openai:v0.22.0-x86_64-cu129-ubuntu2404` image and starts two
OpenAI-compatible vLLM servers from one container:

- coordinator: `http://ai-models:8001/v1/chat/completions`
- expert: `http://ai-models:8002/v1/chat/completions`

Set `AI_FABRIC_TRACK=smoke`, `baseline`, or `quality` on the `ai-models`
deployment before each run.

## Deployment

The WorkerBee stage is rooted at
[`examples/ai-fabric-lab/stage`](../examples/ai-fabric-lab/stage):

```bash
workerbee manifest validate examples/ai-fabric-lab/stage
workerbee manifest deploy-local --stage examples/ai-fabric-lab/stage
```

For k1s controller/fabric validation, keep this co-resident LLM workload
separate from existing `InferenceCell` examples. Run the single-node GPU
`InferenceCell` smoke separately in the sibling `../k1s` checkout, then compare
the evidence captured under `/srv/storage/k1s/ai-fabric-lab/runs`.

## Acceptance Evidence

Each run should record:

- storage and GPU preflight output
- serving image digest and model revisions
- per-lane health, latency, and VRAM samples
- retrieval traces and DAS fact snapshots
- advisory request/response transcripts
- k1s controller or fabric status when the advisory flow references live state

The lab is successful when `baseline` stays resident for 30 minutes without
VRAM pressure, `quality` produces comparable runtime evidence, and the evidence
is sufficient to draft a concrete VRAM-aware k1s scheduling contract.
