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

## Temporary provider gate at C0

At C0, the provider requirement was exactly `k1s-core==0.1.8a1` with an
accepted nonpublished artifact identity recorded in `pyproject.toml`. Its
publication status was `temporary-nonpublished`, and both GitHub publishing
jobs failed closed even if a manual operator selected `publish=true`. The same
check rejected the candidate while its version remained the already-published
`0.1.7` baseline.

C4 later fulfills that deferred gate by binding the exact published
`k1s-core==1.0.0a3` wheel, advancing WorkerBee to `1.0.0a2`, and rerunning the
single-provider, deterministic-build, and offline-install checks. The C0
receipts above remain immutable historical evidence.

## RC1 R3 provider checkpoint

R3 advances WorkerBee to `1.0.0rc1` and binds the accepted sealed
`k1s-core==1.0.0rc1` candidate from source commit
`0827108aa2ee429de491de0842d33871e84b9eba`, tree
`44b9aaab8073f5534f38655b29258b4eb1774f0c`. The exact provider wheel is
`k1s_core-1.0.0rc1-py3-none-any.whl`, size `1057213`, SHA256
`f341d21a2b0297aaa598826b694fccbc2e07aedc6a449b259010d9e07f520d20`.
Its normalized source distribution is `k1s_core-1.0.0rc1.tar.gz`, size
`977871`, SHA256
`6af74da687aceb93f15d9057963edf2b03ced5ebf32d4a56e9230d2728d8f822`.

The K1s R2 seal records two byte-identical build lanes and two strict installed
validations. This is accepted local candidate authority, not evidence of a
published package-index artifact. The WorkerBee release contract therefore
uses `temporary-nonpublished`, and trusted publication remains fail-closed.
The legacy provider remains forbidden, `ae` compatibility remains required,
and wheelhouse installation remains no-index and exactly-one-provider.

## RC2 published-provider checkpoint

RC2 advances WorkerBee to `1.0.0rc2` and binds the published
`k1s-core==1.0.0rc2` provider from source commit
`4d91576326105fbe8a62c1dc5c01c6d5cfdca460`, tree
`ba22d40066f0cade372e0105fff1ec0f6fd5e953`. The exact provider wheel is
`k1s_core-1.0.0rc2-py3-none-any.whl`, size `1057577`, SHA256
`2affbd3fbb086a847a01327c29c77c582b37d44b447e689f8b2ed9e5a5480699`.
Its normalized source distribution is `k1s_core-1.0.0rc2.tar.gz`, size
`978593`, SHA256
`2628101ee4269ee465bdacce1d254680df86fad17f714fb501c0f283f855922b`.

K1s TestPyPI run `43108928300` built, verified, attested, and published the
canonical bundle. Production run `43109495393` downloaded that successful
run's artifact and promoted the exact same wheel and source distribution to
PyPI without rebuilding. Both indexes report the identities above. WorkerBee's
provider status is therefore `published`; its own publication still requires
independent deterministic builds, installed-consumer qualification, protected
environment review, and exact-byte TestPyPI-to-PyPI promotion.

## Stable 1.0 published-provider checkpoint

Stable WorkerBee `1.0.0` binds the published `k1s-core==1.0.0` provider from
source commit `047c601c0c8426f1dc4c6da579a80ccd175c55cb`, tree
`2ef5e79791e9779180629e51db3974bba47e29b1`. The exact provider wheel is
`k1s_core-1.0.0-py3-none-any.whl`, size `1057377`, SHA256
`1208552881446eb7b4fdefd27a22ca2f79f70fb851445bebf0a17273a507724f`.
Its normalized source distribution is `k1s_core-1.0.0.tar.gz`, size `978158`,
SHA256 `92744149ac25625f627db7b06ce75853eb3da274f82a6135e8f36d89adfceba6`.

K1s TestPyPI run `43119549839` built, verified, attested, and published the
canonical bundle. Production run `43119935798` promoted those exact bytes to
PyPI without rebuilding. Stable WorkerBee preserves all RC2 live-evidence
limits and runs its own independent deterministic, installed-consumer, and
exact-promotion gates before publication.

## Patch 1.0.1 metadata checkpoint

WorkerBee `1.0.1` is a package-metadata and documentation-only forward patch.
It adds public project links and PyPI badges while retaining the exact
`k1s-core==1.0.0` provider identity, dependency closure, runtime behavior, MCP
surface, installer contract, and release-artifact construction established by
the stable 1.0 checkpoint. Its qualification lane remains deterministic and
exact-byte, but it does not repeat deferred live runtime or host qualification.

## Patch 1.0.2 Python metadata checkpoint

WorkerBee `1.0.2` corrects public Python-version metadata without changing
runtime behavior. The package continues to require Python 3.11 or newer and is
qualified on Python 3.11, 3.12, and 3.13. The Trove classifiers, Gitea
integration matrix, GitHub dev-to-main matrix, and built-wheel `METADATA` audit
are one contract and are tested against drift. The release retains the exact
published `k1s-core==1.0.0` provider and the existing deterministic,
no-index, exactly-one-provider, and installed-consumer qualification gates.

## Host-safety boundary

The Gitea integration and GitHub promotion gates run source-only tests. They do
not invoke WorkerBee lifecycle commands, `sudo`, MicroK8s, containerd, Docker,
Podman, Kubernetes, system services, host networking, deployment, publication,
or cleanup. The workflow contract tests reject those command families. C0 does
not restart a managed runtime or validate against a live service.
