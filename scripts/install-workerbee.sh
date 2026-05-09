#!/usr/bin/env sh
set -eu

DEFAULT_BASE_URL="https://github.com/the-cm-collective/k1s-workerbee/releases/latest/download"
BASE_URL="${WORKERBEE_INSTALL_BASE_URL:-$DEFAULT_BASE_URL}"
ARCHIVE_NAME="${WORKERBEE_WHEELHOUSE_ARCHIVE:-workerbee-wheelhouse.tar.gz}"
FORCE_STANDALONE="${WORKERBEE_FORCE_STANDALONE:-0}"
INSTALL_DIR="${WORKERBEE_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/workerbee}"
BIN_DIR="${WORKERBEE_BIN_DIR:-$HOME/.local/bin}"

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'workerbee install: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

fetch() {
  url="$1"
  dest="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$dest"
  else
    fail "missing curl or wget"
  fi
}

python_bin="${PYTHON:-python3}"
need_cmd "$python_bin"
need_cmd mktemp
need_cmd tar
"$python_bin" - <<'PY' || fail "Python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT INT TERM

archive="$tmp_dir/$ARCHIVE_NAME"
wheelhouse="$tmp_dir/wheelhouse"
log "Downloading WorkerBee wheelhouse from $BASE_URL/$ARCHIVE_NAME"
fetch "$BASE_URL/$ARCHIVE_NAME" "$archive"
mkdir -p "$wheelhouse"
tar -xzf "$archive" -C "$wheelhouse"
if [ -d "$wheelhouse/workerbee-wheelhouse" ]; then
  wheelhouse="$wheelhouse/workerbee-wheelhouse"
fi

if [ "${VIRTUAL_ENV:-}" != "" ] && [ "$FORCE_STANDALONE" != "1" ]; then
  target_python="$VIRTUAL_ENV/bin/python"
  install_mode="active-venv"
else
  target_python="$INSTALL_DIR/venv/bin/python"
  install_mode="standalone"
  mkdir -p "$INSTALL_DIR"
  "$python_bin" -m venv "$INSTALL_DIR/venv" || fail "failed to create venv; install the Python venv package or activate an existing venv"
fi

package_spec="k1s-workerbee"
for candidate in "$wheelhouse"/k1s_workerbee-*.whl; do
  [ -f "$candidate" ] || continue
  wheel_base="$(basename "$candidate")"
  package_version="${wheel_base#k1s_workerbee-}"
  package_version="${package_version%%-*}"
  if [ "$package_version" != "" ]; then
    package_spec="k1s-workerbee==$package_version"
  fi
  break
done

"$target_python" -m pip install --upgrade pip >/dev/null
if ! "$target_python" -m pip install --no-index --find-links "$wheelhouse" "$package_spec"; then
  log ""
  log "Bundled wheelhouse install failed; retrying with package index access for platform-specific wheels."
  "$target_python" -m pip install --find-links "$wheelhouse" "$package_spec"
fi

if [ "$install_mode" = "standalone" ]; then
  mkdir -p "$BIN_DIR"
  cat >"$BIN_DIR/workerbee" <<EOF
#!/usr/bin/env sh
exec "$target_python" -m workerbee "\$@"
EOF
  chmod +x "$BIN_DIR/workerbee"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
      log ""
      log "Add WorkerBee to your PATH:"
      log "  export PATH=\"$BIN_DIR:\$PATH\""
      ;;
  esac
fi

workerbee_cmd="$target_python -m workerbee"
if command -v workerbee >/dev/null 2>&1; then
  workerbee_cmd="workerbee"
fi

log ""
log "WorkerBee installed ($install_mode)."
if command -v podman >/dev/null 2>&1 || command -v docker >/dev/null 2>&1; then
  log "Container runtime detected."
else
  log "No Podman or Docker runtime detected. Install one, then run: $workerbee_cmd doctor"
fi
log "Verify with: $workerbee_cmd doctor"
