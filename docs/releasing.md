# WorkerBee Python release runbook

This repository publishes the `k1s-workerbee` distribution. Its import package
and command remain `workerbee`, and its k1s compatibility probe continues to
import `ae`.

## Current provider lane

The WorkerBee 1.0 alpha lane requires the published `k1s-core==1.0.0a3`
provider. The accepted wheel is
`k1s_core-1.0.0a3-py3-none-any.whl`, size `1056823`, SHA256
`f642244ebbd9bdfa22eea1b848b1f8a34f8ee69b233f1135dc63bf058c1c5ba1`.
The release contract in `pyproject.toml`, runtime provider guard, artifact
audit, wheelhouse audit, and tests require that exact artifact to be the only
installed distribution advertising the `ae` package. The legacy
`k1s-workerbee-runtime` distribution is forbidden in the active lane.
The MCP SDK remains on `mcp>=1.8,<2` because WorkerBee's current server contract
uses the 1.x `FastMCP` API; a 2.x migration must be a separate compatibility
change rather than an implicit dependency upgrade.

The accepted artifact supplies the required compatibility imports: `ae`,
`ae.apishim`, `ae.cli`, and `ae.controller`. PyPI and TestPyPI serve the same
wheel bytes. The K1s version tag is `v1.0.0a3`, and the final production
workflow correction is tagged `v1.0.0a3-publish1`; the exact index artifact
identity above is authoritative for WorkerBee's release contract. Do not
substitute a local rebuild.

Its release-contract publication status is `published`, and the contract still
records `0.1.7` as the immutable already-published WorkerBee baseline.
WorkerBee's candidate version is `1.0.0a2`. The GitHub publishing jobs call
`verify-publication-ready` immediately before trusted publishing and fail
closed if a future provider is temporary or the WorkerBee version regresses to
the published baseline.

The dependency integration and future rebind points are `[project].dependencies` together with
`[tool.workerbee.release]` in `pyproject.toml` and the constants in
`src/workerbee/runtime_provider.py`, the wheelhouse builder, release artifact
auditor, contract tests, and this document. Before the final RC1 artifact
freeze, stop and obtain explicit confirmation that the accepted K1s baseline
has not been superseded. If it has, update every rebind point and rerun all
affected build, wheelhouse, offline-install, and compatibility gates. Never
leave both providers active.

## Build and local validation

Use Python 3.11 or newer and build from a clean commit. The candidate builder
does not publish:

```bash
python3.11 -m pip install \
  --index-url https://pypi.org/simple \
  build==1.3.0 setuptools==80.9.0 twine==6.2.0 wheel==0.45.1
PYTHON_BIN=python3.11 scripts/build_release_candidate.sh --out dist/pypi
python3.11 scripts/audit_release_artifacts.py verify-manifest \
  --manifest dist/pypi/RELEASE-MANIFEST.json \
  --directory dist/pypi
```

For reproducibility, build twice from the same commit with the same
`SOURCE_DATE_EPOCH` and verify the first manifest against the second directory.
The wheel and sdist filenames, sizes, and SHA256 values must match exactly.

The wheelhouse builder accepts the exact verified `k1s-core` wheel plus a local
directory containing all transitive wheels. It sets `PIP_NO_INDEX=1` and
`PIP_CONFIG_FILE=/dev/null`, rejects provider wheels in the dependency input,
builds WorkerBee without dependency or build-isolation resolution, validates
that only the accepted wheel provides `ae`, proves pip can resolve the complete
wheelhouse with `--no-index`, writes an internal manifest, and creates a
normalized archive. It has no package-index resolution path.
The installer requires the external `SHA256SUMS`, verifies the internal
manifest, and never falls back to an index.

Both candidate and wheelhouse builders emit deterministic `SBOM.cdx.json`
inventories. The manifest records the SBOM as nonpublishable support evidence;
PyPI fetch verification therefore compares only the wheel and sdist while the
preserved candidate bundle still verifies the complete local inventory.
Archive checks reject private markers, protected dotenv names, unapproved
generated roots, duplicate entries, symlinks, hardlinks, and special members.
Manifest and checksum verification reject missing, extra, duplicate, malformed,
or byte-mismatched inventory entries.

The standalone installer uses versioned environments under
`workerbee-install/releases`, validates the isolated candidate and exact
single-provider ownership, then atomically switches the launcher. It retains a
previous release for `install-workerbee.sh --rollback`; WorkerBee runtime state
is not stored inside a versioned environment.

## Staged trusted publishing

The immutable C0 baseline, four candidate receipts, `dev` to `main` promotion
gate, Gitea/GitHub authority split, and host-safety boundary are recorded in
[`release-foundation.md`](release-foundation.md).

Both workflows are manual-only and default to validation without publication:

1. `release-testpypi.yml` builds the wheel and sdist twice, requires identical
   manifests, uploads one exact-byte candidate bundle, and publishes only when
   its `publish` input is explicitly true. The publish job uses the `testpypi`
   environment and OIDC trusted publishing.
2. `release-pypi.yml` accepts the successful TestPyPI workflow run ID. It never
   builds. It downloads that candidate, verifies the manifest and archive
   hygiene, downloads the TestPyPI files by their JSON API URLs, and compares
   exact SHA256 values. Production publication is separately gated by its
   `publish` input and the `pypi` environment, again using OIDC.

Do not add API tokens, index passwords, `skip-existing`, automatic tag/release
triggers, or a secondary dependency index. Repository environments and trusted
publisher configuration are operator-managed and outside this source change.

## Rollback

Before publication, discard the candidate bundle and fix the source; no package
index state has changed. After TestPyPI publication, keep production gated,
prepare a new version, and rerun the TestPyPI path. After PyPI publication,
Python package files are immutable: do not replace or delete them as a normal
rollback. Restore behavior with a new patch release that re-establishes the
last accepted provider contract, then promote that new exact-byte candidate.

If this provider migration fails before publication, revert the focused
integration commits together so the dependency, release contract, runtime
guard, wheelhouse contract, tests, and documentation return to the previously
accepted state. Never install both providers as a rollback. Confirm a clean
no-index wheelhouse install and the CLI/MCP smoke tests before considering
another candidate. After publication, roll forward with a new version rather
than replacing immutable package files.
