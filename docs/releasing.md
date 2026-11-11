# WorkerBee Python release runbook

This repository publishes the `k1s-workerbee` distribution. Its import package
and command remain `workerbee`, and its k1s compatibility probe continues to
import `ae`.

## Current provider lane

The WorkerBee `1.0.0rc1` lane requires the accepted sealed
`k1s-core==1.0.0rc1` provider. Its source identity is commit
`0827108aa2ee429de491de0842d33871e84b9eba`, tree
`44b9aaab8073f5534f38655b29258b4eb1774f0c`. The exact wheel is
`k1s_core-1.0.0rc1-py3-none-any.whl`, size `1057213`, SHA256
`f341d21a2b0297aaa598826b694fccbc2e07aedc6a449b259010d9e07f520d20`.
The normalized sdist is `k1s_core-1.0.0rc1.tar.gz`, size `977871`, SHA256
`6af74da687aceb93f15d9057963edf2b03ced5ebf32d4a56e9230d2728d8f822`.
The sealed R2 receipt records two byte-identical builds, two strict installed
validations, and no network, provider, runtime, or protected-content action.

The release contract in `pyproject.toml`, runtime provider guard, artifact
audit, wheelhouse audit, and tests require that exact wheel to be the only
installed distribution advertising the `ae` package. The legacy
`k1s-workerbee-runtime` distribution is forbidden. The accepted artifact
supplies `ae`, `ae.apishim`, `ae.cli`, and `ae.controller`; do not substitute a
local rebuild or an index-resolved wheel.

The MCP SDK remains on `mcp>=1.8,<2` because WorkerBee's current server contract
uses the 1.x `FastMCP` API. A 2.x migration must be a separate compatibility
change rather than an implicit dependency upgrade.

This is sealed local candidate authority, not evidence that the K1s RC wheel is
published on a package index. Its release-contract publication status is
`temporary-nonpublished`. WorkerBee remains locally qualifiable and taggable,
but `verify-publication-ready` and both OIDC publishing jobs fail closed until
separate root authority proves published bytes with the same identity. The
contract continues to record `0.1.7` as the immutable already-published
WorkerBee baseline.

### Accepted `k1s-core==1.0.0rc1` checkpoint

The accepted dependency checkpoint updates these version-bearing paths:

- `pyproject.toml` and `src/workerbee/runtime_provider.py` bind the exact pin,
  filename, size, SHA256, and nonpublished status;
- `scripts/build_wheelhouse.sh` emits the exact provider and WorkerBee RC pins;
- `CHANGELOG.md`, `docs/release-foundation.md`, `docs/releasing.md`, and
  `docs/v1-contract.md` record current authority and claim boundaries; and
- `tests/test_installer.py`, `tests/test_package_entrypoints.py`,
  `tests/test_profile_evidence.py`, `tests/test_release_artifacts.py`,
  `tests/test_runtime_provider.py`, and `tests/test_v1_contract_docs.py` enforce
  the exact contract.

The contract-driven `scripts/audit_release_artifacts.py`,
`scripts/install-workerbee.sh`, `src/workerbee/profile_evidence.py`, and
`README.md` require no version-specific source edit. The version-neutral
`.gitea/workflows/integration.yml`, `.github/workflows/promote-main.yml`,
`.github/workflows/release-testpypi.yml`, and
`.github/workflows/release-pypi.yml` remain unchanged and retain their existing
authority split and no-index/fail-closed gates.

Every previous WorkerBee candidate bundle is superseded. Rebuild and reseal:

- `k1s_workerbee-1.0.0rc1-py3-none-any.whl` and
  `k1s_workerbee-1.0.0rc1.tar.gz`;
- candidate `SBOM.cdx.json` and `RELEASE-MANIFEST.json`;
- the accepted `k1s_core-1.0.0rc1-py3-none-any.whl` plus exact transitive
  dependency wheels;
- wheelhouse `INSTALL.txt`, `SBOM.cdx.json`, and `RELEASE-MANIFEST.json`; and
- normalized `workerbee-wheelhouse.tar.gz` plus external `SHA256SUMS` covering
  the archive and installer.

Acceptance requires two byte-identity builds, Twine and archive hygiene checks,
complete manifest/SBOM/SHA256 verification, exactly-one-`ae` provider checks,
mixed legacy-provider rejection, and two clean installs with
`PIP_NO_INDEX=1`, `PIP_CONFIG_FILE=/dev/null`, and explicit `--no-index`.
Each install must pass `pip check`, `workerbee`/MCP/import/`ae` smoke checks,
runtime-provider discovery, and the five-operation K1s profile-evidence
contract plus the internal pure bootstrap projection.

The public consumer qualification is a separate non-live receipt. Run
`scripts/qualify_installed_consumer.py` with the exact candidate and provider
filename, size, SHA256, source commit, source tree, source roots, sealed
wheelhouse, and a new owner-private output root as documented in
[`k1s-consumer-contract.md`](k1s-consumer-contract.md). It must perform at least
two fresh strict no-index installs, reject either source checkout on `sys.path`,
and produce byte-identical canonical run receipts. This proves installed
consumer compatibility only; it does not qualify profiles, providers,
strict-CRI host mutation, or production fitness.

Creating or pushing the reviewed `v1.0.0rc1` source tag does not publish either
Python distribution. Package-index publication remains a separate, gated OIDC
decision.

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

For an already sealed complete wheelhouse, direct pip installation is the
supported environment-local path:

```bash
python3.11 -m venv /path/to/fresh-venv
/path/to/fresh-venv/bin/python -m pip install \
  --no-index --find-links /path/to/workerbee-wheelhouse \
  'k1s-workerbee==1.0.0rc1'
/path/to/fresh-venv/bin/python -m pip check
```

Set `PIP_NO_INDEX=1` and `PIP_CONFIG_FILE=/dev/null` in release validation as a
second fail-closed layer. Use `scripts/install-workerbee.sh` instead when
validating the versioned standalone layout, atomic `current` switch, retained
`previous` target, wrapper, or rollback behavior.

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
