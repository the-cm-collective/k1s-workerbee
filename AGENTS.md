# WorkerBee Agent Instructions

<!-- workerbee-agent-instructions:v1 start -->
## WorkerBee

When a task involves containers, services, manifests, ingress, databases,
queues, security review, or integration behavior, call WorkerBee MCP
`workerbee_v1_session_start` with the absolute repo cwd and task goal. Use the
returned `project` for every WorkerBee tool call. Respect the returned project
mode: `lazy` waits until deploy/start is needed, `start` may already be
launching the stack, and `stop` means WorkerBee is disabled for that project
until the mode is changed.

Use the local shell for repo edits, ordinary tests, and repo-local build scripts.
Use WorkerBee MCP for local image builds, native k1s/Kubernetes manifest
prepare/validate/deploy, project status, logs, exec, HTTPS ingress status and
probes, security assessment/review, secret policy checks, dashboard URLs, trust
guidance, cleanup, and artifact export. Use named stages or returned `stage_dir`
values for manifest operations. Use app names plus the optional `namespace` for
logs/exec; do not guess generated runtime container names. Use image build
hardening metadata to prefer minimal, non-root images; pass
`hardening_profile="hardened"` for WorkerBee-generated or deliberately minimal
images, and leave arbitrary repo Dockerfiles on `standard` unless asked to
harden them.

When `workerbee_v1_session_start` returns `project_runbook`, review it before
choosing a bring-up, deploy, validation, or repair path. After a successful
bring-up, deployment, repair, or security review, update it with
`workerbee_v1_project_runbook_update` so later agents and human operators can
repeat the proven project-specific process. Keep secrets out of runbooks.

If the user asks to bring, run, or start the project up in WorkerBee, treat that
as a request for a running app workload. Build needed local images, stage and
validate manifests, deploy with `workerbee_v1_manifest_deploy_local`, then
inspect status/logs and probe ingress. Do not stop after
`workerbee_v1_project_start` if deployable manifests or
Containerfiles/Dockerfiles exist.

If this is the first time WorkerBee is coming up for a project, there may be no
deployed workload to inspect yet. Prefer existing repo manifests and
Containerfiles/Dockerfiles. When they are absent, build a temporary native k1s
deployment in WorkerBee state, deploy it locally, then rerun the requested
runtime validation or security review. Keep first-run generated artifacts in
WorkerBee state unless the user asks to commit them.

For security reviews, call `workerbee_v1_project_status` first, then
`workerbee_v1_security_review_project` for deployed workloads. If WorkerBee
reports no deployment metadata, stage/deploy the app or pass an explicit stage
and rerun the review. Summarize critical/high findings before lower-severity
items and include the report path.

For larger multi-feature requests, when prior WorkerBee stages are performing
well, scope a coherent feature batch, split it into feature checkpoints,
validate each checkpoint with repo tests and WorkerBee deployments/probes, and
keep iterating autonomously while progress is being made. Use checkpoint commits
only when the user has asked for commits or the repo workflow already permits
them.

For k1s controller/runtime development, use an explicit direct-containerd
WorkerBee project and the sibling `../k1s` checkout as `k1s_root`. Prefer one
agent for tandem k1s/WorkerBee work unless tasks are separable. Start profiles
with `workerbee_v1_profile_start`, deploy staged workloads with
`workerbee_v1_manifest_deploy_local(target="profile")`, inspect with
`workerbee_v1_profile_workload_status` and `workerbee_v1_logs(target="profile")`,
and run `workerbee_v1_profile_workload_validate` for the bundled realtime smoke
test. Before profile start/stop/purge or profile-target deploy, verify
`workerbee_v1_capabilities` reports `runtime.selected == containerd`. If a
profile tool reports `K1S_PROFILE_REQUIRES_CONTAINERD`, restart or select the
direct-containerd MCP path with `--runtime containerd --containerd-privilege
sudo-helper` and retry the profile lifecycle command. Restart profiles after
k1s source changes and restart MCP after WorkerBee source changes. Stop stale
profiles with `workerbee_v1_profile_stop(purge=true)` when profile evidence or
validation is complete, and keep manual cleanup scoped to WorkerBee state-hash
namespaces rather than reserved namespaces such as `ae`, `k8s.io`, `moby`, or
`default`.

For short-lived links from this host to an external k1s core, use the edge-link
tools: `workerbee_v1_edge_link_start`, `workerbee_v1_edge_link_status`,
`workerbee_v1_edge_link_validate`, and `workerbee_v1_edge_link_stop`. Prefer
`--from-microk8s`/MicroK8s bootstrap only for the local dev HA stack, keep
bootstrap secrets out of output, and validate heartbeat plus GPU advertisement
when relevant.
<!-- workerbee-agent-instructions:v1 end -->
