# AI Fabric Lab

This lab stages the first k1s/WorkerBee AI infrastructure experiment from the
local RTX 8000 host. It uses two small dense LLM lanes plus retrieval and
Hyperon/DAS-adjacent symbolic state:

- coordinator LLM for planning, routing, and user-facing synthesis
- Python/k1s/Hyperon expert LLM for code and control-plane advisory work
- Redis, Mongo, and Qdrant for hot state, symbolic persistence, and retrieval
- job-style modality and indexing workers instead of resident modality services

The lab intentionally runs two GPU-owning LLM workloads that share the local
development GPU. This keeps each vLLM server isolated for startup and memory
profiling while still producing evidence for future VRAM-aware admission fields
such as `gpu_vram_gib`, `kv_cache_budget_gib`, `adapter_hotset`, and
`context_budget_tokens`.

## Model Tracks

The model matrix lives in
[`examples/ai-fabric-lab/model-tracks.json`](../examples/ai-fabric-lab/model-tracks.json).
All models are pinned to Hugging Face revision SHAs as of the lab definition.

| Track | Coordinator | Expert | Purpose |
| --- | --- | --- | --- |
| `smoke` | `Qwen/Qwen2.5-3B-Instruct-AWQ` | `Qwen/Qwen2.5-Coder-7B-Instruct-AWQ` | All-Qwen fallback path with smaller coordinator and lower expert VRAM pressure. |
| `lora-plumbing` | `Qwen/Qwen2.5-3B-Instruct-AWQ` | `Qwen/Qwen2.5-Coder-7B-Instruct-AWQ` | LoRA plumbing validation with expert LoRA support enabled, no required adapter artifact, and a 4k resident context budget. |
| `lora-adapter-smoke` | `Qwen/Qwen2.5-3B-Instruct-AWQ` | `Qwen/Qwen2.5-Coder-7B-Instruct-AWQ` | Static LoRA adapter smoke using `k1s-code-expert-lora-smoke` at `/adapters/expert/validation`; no quality claim. |
| `baseline` | `Qwen/Qwen2.5-7B-Instruct-AWQ` | `Qwen/Qwen2.5-Coder-14B-Instruct-AWQ` | Primary all-Qwen development baseline. |
| `quality` | `Qwen/Qwen2.5-7B-Instruct-AWQ` | `Qwen/Qwen2.5-Coder-14B-Instruct-AWQ` | Compare coordinator quality versus the baseline. |
| `legacy-smollm-smoke` | `HuggingFaceTB/SmolLM3-3B` | `Qwen/Qwen2.5-Coder-7B-Instruct-AWQ` | Legacy plumbing-only track, not a baseline. |

The default runtime track is `baseline`. Start with `smoke` before downloading
the larger expert model, then run `baseline`, then run `quality` against the
same prompt set, corpus snapshot, and DAS facts.

## Storage

The storage layout lives in
[`examples/ai-fabric-lab/storage-layout.json`](../examples/ai-fabric-lab/storage-layout.json)
and defaults to `/srv/storage/k1s/ai-fabric-lab`.

Prepare the layout and copy the pinned lab config into `/srv/storage`:

```bash
python3 scripts/dev/ai_fabric_lab.py init-storage
```

Seed a bounded corpus snapshot from this repo and the sibling `../k1s`
checkout before starting the retrieval lane:

```bash
python3 scripts/dev/ai_fabric_lab.py sync-corpus
```

Validate the static lab bundle without starting workloads:

```bash
python3 scripts/dev/ai_fabric_lab.py validate
python3 scripts/dev/ai_fabric_lab.py validate --stage examples/ai-fabric-lab/stage-plumbing
```

## Images

Build the local development images before deploying the stage:

```bash
workerbee build-image examples/ai-fabric-lab/images/ai-models \
  --tag localhost/workerbee-ai-fabric-models:dev
workerbee build-image examples/ai-fabric-lab/images/router \
  --tag localhost/workerbee-ai-fabric-router:dev
workerbee build-image examples/ai-fabric-lab/images/das-bridge \
  --tag localhost/workerbee-ai-fabric-das-bridge:dev
workerbee build-image examples/ai-fabric-lab/images/retrieval-indexer \
  --tag localhost/workerbee-ai-fabric-retrieval-indexer:dev
```

For the GPU-free advisor plumbing smoke, build the fake OpenAI-compatible model
instead of the vLLM image and deploy `examples/ai-fabric-lab/stage-plumbing`:

```bash
workerbee build-image examples/ai-fabric-lab/images/fake-model \
  --tag localhost/workerbee-ai-fabric-fake-model:dev
workerbee manifest deploy-local --stage examples/ai-fabric-lab/stage-plumbing
```

Use `examples/ai-fabric-lab/stage` for the two-lane Qwen smoke track,
`examples/ai-fabric-lab/stage-baseline` for the resident baseline track, and
`examples/ai-fabric-lab/stage-quality` for quality-contract comparison runs.
Use `examples/ai-fabric-lab/stage-lora-plumbing` for the small Qwen LoRA
plumbing track. That track keeps the same small all-Qwen model pair as smoke
but lowers both lanes to 4k context and shifts more GPU budget to the expert so
vLLM can allocate KV cache with LoRA support enabled. Use
`examples/ai-fabric-lab/stage-lora-adapter-smoke` when a real validation
adapter is present under
`/srv/storage/k1s/ai-fabric-lab/adapters/expert/validation`. That stage serves
the base expert as `k1s-code-expert` and statically registers the adapter as
`k1s-code-expert-lora-smoke`. These stages use the same router, DAS, retrieval,
and storage layout.

The model image wraps the pinned
`vllm/vllm-openai:v0.22.0-x86_64-cu129-ubuntu2404` image. The stage deploys the
same image twice with `AI_FABRIC_LANE` selecting one OpenAI-compatible vLLM
server per container:

- coordinator: `http://ai-coordinator:8001/v1/chat/completions`
- expert: `http://ai-expert:8002/v1/chat/completions`

Set `AI_FABRIC_TRACK=smoke`, `baseline`, or `quality` on both model
deployments before each serious run. Use `legacy-smollm-smoke` only for
plumbing checks that do not measure the intended two-Qwen architecture.

The WorkerBee smoke manifests also set
`VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0`. On the RTX 8000 development host,
vLLM 0.22's CUDA graph memory estimate can otherwise consume the small smoke
track's KV-cache budget before either lane accepts requests.
The smoke coordinator and expert use Qwen AWQ models so the fallback still
matches the intended two-Qwen architecture while preserving VRAM headroom. The
baseline keeps the stronger 7B coordinator target for quality validation.
The smoke coordinator cap is higher than the expert cap because vLLM 0.22
reported a larger CUDA graph reservation for the smaller general Qwen model.
The baseline and quality tracks keep the 7B coordinator at a slightly higher
GPU memory cap than the first draft so it can reliably allocate KV cache after
warm restarts alongside the resident 14B expert.
The model launcher passes `--attention-backend TRITON_ATTN` from
`run_defaults.attention_backend`; FlashInfer initialized on the RTX 8000 but
failed during prefill in the smoke test.

WorkerBee service alias refresh is intentionally short, so the model manifests
do not gate deployment readiness on vLLM cold start. Treat `/v1/models` on both
model services as the runtime readiness signal for this lab. The router also
proxies `/v1/models?lane=expert` so adapter smoke validation can verify that
`k1s-code-expert-lora-smoke` is exposed before sending an adapter chat request.
The router, DAS bridge, and retrieval indexer expose host service ports
`18180`, `18181`, and `18182` respectively to avoid colliding with other
WorkerBee development services.

## Retrieval Evidence

The retrieval indexer scans `/srv/storage/k1s/ai-fabric-lab/corpus`, writes
`artifacts/indexes/corpus-manifest.json`, and upserts deterministic hashed
vectors into the `ai_fabric_corpus` Qdrant collection. If Qdrant is temporarily
unavailable, the indexer still serves a local in-memory fallback so advisory
requests can show which corpus chunks were considered.

Probe retrieval from inside the stage:

```bash
python3 - <<'PY'
import json
from urllib.request import Request, urlopen

body = json.dumps({"query": "WorkerBee k1s GPU fabric", "limit": 3}).encode()
request = Request(
    "http://retrieval-indexer.ai-fabric-lab.svc.cluster.local:8082/v1/search",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
print(urlopen(request, timeout=10).read().decode())
PY
```

The router calls the same search endpoint for `/v1/advisory/query` and
`/v1/advisory/evaluate`, includes the retrieval evidence in the response, and
keeps `authoritative: false` with `controller_authority: k1s`.

## Symbolic Evidence

The DAS bridge runs with `AI_DAS_BACKEND=hyperon-das` and imports facts into a
local Hyperon `DistributedAtomSpace` while also appending the durable JSONL
audit log under `/srv/storage/k1s/ai-fabric-lab/das/facts.jsonl`.

Seed the initial runtime facts after deployment:

```bash
python3 scripts/dev/ai_fabric_lab.py import-runtime-facts \
  --das-url http://das-bridge.ai-fabric-lab.svc.cluster.local:8081 \
  --project k1s-workerbee-dev-2592c13f5e
```

When the advisory run references k1s roadmap state, generate the authoritative
phase report from the sibling `../k1s` checkout and import it as DAS facts:

```bash
cd ../k1s
python3 scripts/dev/fabric_phase_assurance.py \
  --evidence /srv/storage/k1s/ai-fabric-lab/runs/fabric-evidence.json \
  --json > /srv/storage/k1s/ai-fabric-lab/runs/fabric-phase-report.json

cd ../k1s-workerbee
python3 scripts/dev/ai_fabric_lab.py import-phase-facts \
  --phase-report /srv/storage/k1s/ai-fabric-lab/runs/fabric-phase-report.json \
  --das-url http://das-bridge.ai-fabric-lab.svc.cluster.local:8081
```

`import-runtime-facts` also accepts `--phase-report` when a run should seed
model, repo, project, and k1s phase facts in one pass.

Runtime fact import uses the DAS bridge batch endpoint,
`POST /v1/import/runtime`, and seeds a narrow relationship graph with these
predicates:

- `owns_service`
- `depends_on`
- `serves_model`
- `requires_resource`
- `produced_artifact`
- `supports_advisory`

The generated facts cover the staged services, stable WorkerBee host ports,
router dependencies, model lanes, LoRA adapter registrations, resource
requests/limits, storage mounts, runtime validation output files, and the
retrieval/DAS/router advisory evidence roles. The DAS bridge exposes the active
predicate list at `/v1/relationships`.

The router queries `/v1/query` on the DAS bridge for advisory requests and
passes symbolic facts to the selected model alongside retrieval evidence.
Each DAS query also records F5-compatible local-first query evidence in
`/srv/storage/k1s/ai-fabric-lab/das/f5-evidence.jsonl`, and the bridge exposes
the recent records at `/v1/f5/evidence`.

The DAS bridge also exposes `POST /v1/advisory/decision`. The response uses
`workerbee.ai-fabric.advisory-decision/v1` and is always advisory-only with
`authoritative=false`. The decision includes a subject, intent, recommended
action, confidence, DAS evidence refs, risks, and blocked conditions. The router
requests this structured DAS decision before calling the selected model lane and
stores it in the advisory trace alongside retrieval, symbolic facts, and model
output.

`import-runtime-facts` seeds live local snapshots when local host aliases are
available: router/DAS/retrieval readiness, host alias health, model lane
readiness, retrieval corpus counts, DAS fact counts, and optional WorkerBee
project status from a `workerbee-status.json` file.

Emit a durable F5 evidence bundle for import or review without starting the
runtime:

```bash
python3 scripts/dev/ai_fabric_lab.py emit-f5-evidence \
  --storage-root /srv/storage/k1s/ai-fabric-lab \
  --site-id site-a \
  --peer-site-id site-b \
  --project k1s-workerbee-dev-2592c13f5e \
  --track smoke
```

The generated `runs/f5-evidence.json` contains k1s-compatible records for DAS
cell bundles, local-first query traces, controlled replication intent, and
cognitive-fabric signals.

## Deployment

The WorkerBee stage is rooted at
[`examples/ai-fabric-lab/stage`](../examples/ai-fabric-lab/stage):

```bash
workerbee manifest validate examples/ai-fabric-lab/stage
workerbee manifest deploy-local --stage examples/ai-fabric-lab/stage
```

The committed WorkerBee stage currently deploys the all-Qwen `smoke` track so
runtime validation can proceed with a smaller coordinator and coder expert while
the all-Qwen `baseline` remains the target profile to validate separately.

For k1s controller/fabric validation, keep this co-resident LLM workload
separate from existing `InferenceCell` examples. Run the single-node GPU
`InferenceCell` smoke separately in the sibling `../k1s` checkout, then compare
the evidence captured under `/srv/storage/k1s/ai-fabric-lab/runs`.

## Acceptance Evidence

Each run should record:

- storage and GPU preflight output
- serving image digest and model revisions
- imported `k1s.fabric.phase-assurance/v1` report and DAS facts
- per-lane health, latency, and VRAM samples
- retrieval traces and DAS fact snapshots
- advisory request/response transcripts
- k1s controller or fabric status when the advisory flow references live state

The lab is successful when `baseline` stays resident for 30 minutes without
VRAM pressure, `quality` produces comparable runtime evidence, and the evidence
is sufficient to draft a concrete VRAM-aware k1s scheduling contract.

Run the next validation batch with the runtime runner:

```bash
python3 scripts/dev/ai_fabric_lab.py validate-runtime \
  --suite all \
  --run-id ai-fabric-baseline-$(date -u +%Y%m%dT%H%M%SZ)
```

For short iteration on the remaining untested areas, run these suites in order:

```bash
python3 scripts/dev/ai_fabric_lab.py validate-runtime --suite adapter-preflight
python3 scripts/dev/ai_fabric_lab.py validate-runtime --suite lora-adapter-smoke
python3 scripts/dev/ai_fabric_lab.py validate-runtime --suite quality-comparison
python3 scripts/dev/ai_fabric_lab.py validate-runtime --suite stress-burst
python3 scripts/dev/ai_fabric_lab.py validate-runtime --suite recovery-smoke
```

`adapter-preflight` checks
`/srv/storage/k1s/ai-fabric-lab/adapters/expert/validation` for a real adapter
payload. If no payload exists, it records `state=blocked` and leaves the run
successful so unrelated runtime validation can continue. If a payload exists,
preflight verifies `adapter_config.json`, one adapter weight file, expected
Qwen coder base metadata, LoRA rank no greater than 16, and non-empty target
modules. Invalid adapter metadata is a failing result. `lora-adapter-smoke`
runs the same preflight first, then checks `/v1/models?lane=expert`, sends one
base expert chat request, sends one `k1s-code-expert-lora-smoke` chat request,
and records both model IDs without treating the result as a quality benchmark.

LoRA training remains deferred for this stage. The readiness contract lives in
`examples/ai-fabric-lab/lora-readiness.json`, and the first expert-only eval set
lives in `examples/ai-fabric-lab/prompts/k1s-code-expert-lora-eval.jsonl`.
Use those artifacts to shape a future corpus manifest and baseline comparison;
do not treat the current `k1s-code-expert-lora-smoke` adapter as an ops-quality
claim.

The runner writes `summary.json`, `requests.jsonl`, `gpu-samples.jsonl`,
`health.json`, `lane-readiness.json`, `f5-evidence.json`, and a
`workerbee-status.json` placeholder under
`/srv/storage/k1s/ai-fabric-lab/runs/<run-id>/`. Capture final WorkerBee MCP
project status during closeout and store it in that placeholder path when a run
is promoted to acceptance evidence. Runtime summaries also record selected
defaults, blocked items, host alias health for the validated router, DAS, and
retrieval endpoints, and model lane readiness for suites that exercise
coordinator or expert chat/advisory calls.
