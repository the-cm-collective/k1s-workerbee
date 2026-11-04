#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/dist/pypi"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

usage() {
  cat <<'EOF'
usage: scripts/build_release_candidate.sh [--out PATH] [--python PYTHON]

Build one deterministic wheel/sdist candidate, audit it, run Twine checks, and
write an exact-byte RELEASE-MANIFEST.json. This script never publishes.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)
      OUT_DIR="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

OUT_PARENT="$(mkdir -p "$(dirname "$OUT_DIR")" && cd "$(dirname "$OUT_DIR")" && pwd)"
OUT_DIR="${OUT_PARENT}/$(basename "$OUT_DIR")"
rm -rf "$OUT_DIR" "${ROOT_DIR}/build" "${ROOT_DIR}/src/k1s_workerbee.egg-info"
mkdir -p "$OUT_DIR"

SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT_DIR" log -1 --format=%ct)}"
export SOURCE_DATE_EPOCH
export PYTHONDONTWRITEBYTECODE=1

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" verify-project
"${PYTHON_BIN}" -m build --no-isolation --sdist --wheel --outdir "$OUT_DIR" "$ROOT_DIR"
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  normalize-sdist \
  --epoch "$SOURCE_DATE_EPOCH" \
  "$OUT_DIR"/*.tar.gz
"${PYTHON_BIN}" -m twine check "$OUT_DIR"/*
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" audit "$OUT_DIR"/*
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  write-manifest \
  --output "$OUT_DIR/RELEASE-MANIFEST.json" \
  "$OUT_DIR"/*.whl "$OUT_DIR"/*.tar.gz
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/audit_release_artifacts.py" \
  verify-manifest \
  --manifest "$OUT_DIR/RELEASE-MANIFEST.json" \
  --directory "$OUT_DIR"

echo "release candidate: ${OUT_DIR}"
