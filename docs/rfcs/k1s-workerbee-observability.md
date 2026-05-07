# RFC: k1s Observability Surface For WorkerBee

## Summary

WorkerBee should not duplicate k1s dashboard and runtime observability. For v0.1,
WorkerBee needs a small, MCP-friendly k1s API surface that returns structured
resource, event, log, health, and ingress facts for an agent-facing workbench.

## Requested k1s Surface

- Project/resource summary: list namespaces, apps, services, pods/replicas, images,
  readiness, liveness, restart count, and last transition time.
- Event stream: bounded recent events by namespace/app with stable severities,
  reasons, messages, timestamps, and source component.
- Log summary: app/pod log handles plus recent-tail metadata without embedding
  unbounded logs in status payloads.
- Ingress route status: host, paths, upstreams, Caddy snippet path, reload status,
  TLS mode, and last route error.
- App health: structured readiness/liveness/startup probe state and validation
  failures suitable for MCP result payloads.
- Artifact/status schema: stable JSON shapes that WorkerBee can pass through in
  `workerbee_v1_project_status` and future dashboard views.

## Non-goals

- WorkerBee should not become a second k1s dashboard implementation.
- WorkerBee should not scrape Caddy or runtime state when k1s can expose the
  authoritative fact.
- WorkerBee should not invent separate event or resource schemas if k1s can own
  them.

## WorkerBee Consumer Expectations

- JSON only, no HTML parsing.
- Token-safe payloads with no secret values or bearer tokens.
- Bounded payload sizes by default, with explicit pagination or tail limits.
- Works for local WorkerBee and remote k1s controller targets.
- Stable enough for MCP contract tests.
