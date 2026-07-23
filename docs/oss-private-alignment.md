# OSS and Private Alignment Ledger

This ledger records the current boundary between the open-source k1s/WorkerBee
repos and the private SaaS/OpenStack pilot mirrors.

## Scope

- Public k1s keeps controller, fabric, runtime, ingress, scheduler, and Hive
  observability improvements that are useful to ordinary self-hosted users.
- Public WorkerBee keeps local runtime, export, manifest validation, namespaced
  RBAC, ingress, and handoff improvements that make OSS development more
  correct and reproducible.
- Private mirrors keep SaaS packaging, Horizon/OpenStack product surfaces,
  enterprise control-plane orchestration, marketplace features, and managed
  observability bridge components.

## Public k1s Alignment

Public k1s should include:

- edge/core ingress fixes for `core-proxy`, `core-to-edge-public`, and
  `edge-local` lanes;
- env-scoped edge admission and registration primitives;
- scheduler and runtime fixes that allow an edge gateway to remain schedulable
  when it is the only node present;
- Hive surfaces that show public-safe edge gateway, site, health, build, and
  schedulable state;
- tests for edge gateway dashboard payloads and Hive template rendering.

Public k1s should not include:

- Horizon service panels;
- private OpenStack SaaS environment management;
- enterprise installer issuance, signing workflow, or secure-token product
  surfaces;
- partner-report or vendor-proof packaging assets.

## Public WorkerBee Alignment

Public WorkerBee should include:

- staged manifest validation that identifies local loopback and WorkerBee
  images before remote handoff;
- native k1s to Kubernetes/Helm export hardening, including storage export
  pass-through and preservation of non-root container intent;
- namespaced RBAC bundle validation for practical Kubernetes stages;
- direct-containerd alias refresh and local ingress fixes that keep local
  development deterministic;
- user-facing docs for local edge-link and AI Max edge-cell simulation where
  the path does not depend on enterprise services.

Public WorkerBee should not include:

- SaaS/Horizon administration or user panels;
- OpenStack tenant/product lifecycle controllers;
- private marketplace surfaces;
- enterprise Grafana/metrics bridge services;
- partner-specific report packages, proof harnesses, or generated screenshots.

## Private Mirror Alignment

Private k1s and WorkerBee mirrors should periodically forward-sync public
controller, fabric, runtime, ingress, export, and RBAC fixes. Those syncs must
preserve private SaaS behavior and must not revert unrelated lab artifacts.

Private-only surfaces remain private unless deliberately re-scoped:

- WorkerBee SaaS and K1S SaaS Horizon services;
- OpenStack managed-service packaging;
- edge installer issuance UI, signing ceremony, LUKS-token handling, and
  partner pilot workflow automation;
- enterprise observability bridge and pilot Grafana dashboards;
- vendor proof and partner briefing package generation.

## Latest Checkpoints

- k1s OSS Hive surface: `Expose edge gateway schedulability in Hive`
- WorkerBee OSS export/runtime hardening:
  `Backport generic bundle export hardening`
- WorkerBee direct-containerd runtime alignment:
  `Align containerd service alias refresh`

## Validation Expectations

For OSS backports, run focused tests in the repo being changed. In the lab, a
running edge gateway config at `/run/k1s-dashboard-edge-gateways.json` can make
k1s dashboard layout tests select site layout by design; disable that path for
unit-only layout assertions with `AE_DASHBOARD_EDGE_GATEWAYS_FILE=/dev/null`.

For private forward-syncs, run the matching focused tests and commit only the
code/docs files that belong to the sync. Leave unrelated OpenStack lab artifacts
unstaged.
