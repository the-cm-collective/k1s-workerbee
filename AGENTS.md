# WorkerBee Agent Instructions

<!-- workerbee-agent-instructions:v1 start -->
## WorkerBee

When a task involves containers, services, manifests, ingress, databases,
queues, security review, or integration behavior, call WorkerBee MCP
`workerbee_v1_session_start` with the absolute repo cwd and task goal. Use the
returned `project` for every WorkerBee tool call.

Use the local shell for repo edits and ordinary tests. Use WorkerBee MCP for
local image builds, native k1s/Kubernetes manifest staging, validation,
deployment, status, logs, HTTPS ingress probes, security review, dashboard URLs,
cleanup, and artifact export.

If this is the first time WorkerBee is coming up for a project, there may be no
deployed workload to inspect yet. Prefer existing repo manifests and
Containerfiles/Dockerfiles. When they are absent, build a temporary native k1s
deployment in WorkerBee state, deploy it locally, then rerun the requested
runtime validation or security review. Keep first-run generated artifacts in
WorkerBee state unless the user asks to commit them.
<!-- workerbee-agent-instructions:v1 end -->
