# WorkerBee C0 release foundation

This checkpoint records the immutable WorkerBee `0.1.7` publication baseline
and the four reviewed candidate commits layered on it. It defines review gates;
it does not promote a branch, create a tag, or publish a package.

## Published baseline receipt

- release tag: `v0.1.7`
- annotated tag object: `2bac976794ecb61f53f0f1f4568ee22debeacd57`
- commit: `ab0c39dd4fd49ea18e0195be4db580dc4f7fa94c`
- tree: `945de1b550c9244049b3da9a2bc2ab81d9a31fa6`
- parent: `8a68c6b7ac863765579a4f5bd4e4d4228bf23053`
- distribution/version: `k1s-workerbee==0.1.7`

The local `origin/dev` receipt points at that commit. The C0 coordinator's
read-only remote receipt also observed both the Gitea `origin/dev` and GitHub
`github/dev` refs at the same commit, with no `main` ref advertised by either
remote. C0 does not fetch or create those refs.

## Candidate lineage receipts

| Commit | Tree | Parent | Reviewed purpose |
| --- | --- | --- | --- |
| `cd61ffde26a1bb466c5f2099882b0276b97ab228` | `cd3e303439debe3d6abe66c4c9c32520988fe5aa` | `ab0c39dd4fd49ea18e0195be4db580dc4f7fa94c` | deterministic release, packaging, exact-byte, and provider-lane hardening |
| `b0e3e4943d3f624b3a52db5fce4ade003e421ec1` | `d05d955cd7f2277ec4aa88d571d867438eef1cba` | `cd61ffde26a1bb466c5f2099882b0276b97ab228` | exact temporary `k1s-core==0.1.8a1` compatibility integration |
| `6a205c75d993712f8e526b111b5c14c1c2f5cce4` | `b8009383204141a847351dfcbb588e966f99bb3f` | `b0e3e4943d3f624b3a52db5fce4ade003e421ec1` | default-disabled strict controlled-run MCP contract |
| `a742080177fd3f141ba1520a1a9af16fb5dafc5a` | `8e0c784d9629a98fbda64facdab786f8fda74de8` | `6a205c75d993712f8e526b111b5c14c1c2f5cce4` | additive native-direct interoperability contract |

The candidate chain keeps version `0.1.7` while it remains unpublished. The
controlled-run and native-direct commits remain distinct review evidence and
must not be squashed away before the release decision.

## Branch and workflow authority

`dev` remains the implementation source. `main` is the public release-promotion
branch once the repository operator creates and protects it. C0 does not create
or update `main`.

The GitHub `promote-main.yml` workflow is a read-only pull-request gate. It
accepts only a same-repository `dev` head targeting `main`, verifies that the
proposed head is the checked-out commit and that the pull-request base is an
ancestor of it, and runs the release-foundation tests. It has no write or OIDC
permission and never pushes. Repository branch protection must require this
check plus human review; that external setting is not changed by C0. Merging or
fast-forwarding the reviewed pull request updates `main` without rewriting
`dev`.

Gitea owns integration checks on `dev`. Its workflow has read-only contents
permission and runs only the static release-foundation test set. It has no
release trigger, publication action, OIDC permission, package credential, or
host-runtime command.

GitHub alone contains the public TestPyPI/PyPI workflows. They remain manual,
default to no publication, use OIDC trusted publishing, and verify the exact
candidate bytes. No Gitea workflow may call or reproduce those publishing jobs.

## Temporary provider gate

The current provider requirement remains exactly `k1s-core==0.1.8a1` with the
accepted nonpublished artifact identity already recorded in `pyproject.toml`.
Its publication status is `temporary-nonpublished`. Both GitHub publishing jobs
run `verify-publication-ready` immediately before their publishing action, so
this C0 source state fails closed even if a manual operator selects
`publish=true`. The same check rejects the candidate while its version remains
the already-published `0.1.7` baseline.

Changing the status to `published` is not sufficient by itself: a later
reviewed checkpoint must prove that the exact provider artifact is publicly
available, preserve the single-provider/no-index contract, rerun deterministic
build and offline-install gates, and approve the final release candidate. C0
does not perform that rebind or begin WorkerBee C4 work.

## Host-safety boundary

The Gitea integration and GitHub promotion gates run source-only tests. They do
not invoke WorkerBee lifecycle commands, `sudo`, MicroK8s, containerd, Docker,
Podman, Kubernetes, system services, host networking, deployment, publication,
or cleanup. The workflow contract tests reject those command families. C0 does
not restart a managed runtime or validate against a live service.
