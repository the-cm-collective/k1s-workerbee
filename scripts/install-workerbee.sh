#!/usr/bin/env sh
set -eu
umask 077

DEFAULT_BASE_URL="https://github.com/the-cm-collective/k1s-workerbee/releases/latest/download"
BASE_URL="${WORKERBEE_INSTALL_BASE_URL:-$DEFAULT_BASE_URL}"
ARCHIVE_NAME="${WORKERBEE_WHEELHOUSE_ARCHIVE:-workerbee-wheelhouse.tar.gz}"
CHECKSUM_NAME="${WORKERBEE_CHECKSUM_FILE:-SHA256SUMS}"
FORCE_STANDALONE="${WORKERBEE_FORCE_STANDALONE:-0}"
INSTALL_DIR="${WORKERBEE_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/workerbee-install}"
BIN_DIR="${WORKERBEE_BIN_DIR:-$HOME/.local/bin}"
ACTION="install"
python_bin="${PYTHON:-python3}"

case "${1:-}" in
  "") ;;
  --rollback) ACTION="rollback" ;;
  -h|--help)
    printf '%s\n' "usage: install-workerbee.sh [--rollback]"
    exit 0
    ;;
  *)
    printf 'workerbee install: unknown argument: %s\n' "$1" >&2
    exit 2
    ;;
esac

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

case "$INSTALL_DIR" in
  /*) ;;
  *) fail "standalone install directory must be absolute" ;;
esac
case "$BIN_DIR" in
  /*) ;;
  *) fail "standalone launcher directory must be absolute" ;;
esac

atomic_link() {
  link_name="$1"
  link_target="$2"
  "$python_bin" - "$INSTALL_DIR" "$link_name" "$link_target" <<'PY'
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
name = sys.argv[2]
target = sys.argv[3]
parts = pathlib.PurePosixPath(target).parts
if (
    name not in {"current", "previous"}
    or len(parts) != 2
    or parts[0] != "releases"
    or parts[1] in {"", ".", ".."}
):
    raise SystemExit("unsafe release link")
temporary = root / f".{name}.new.{os.getpid()}"
temporary.unlink(missing_ok=True)
temporary.symlink_to(target)
try:
    temporary.replace(root / name)
finally:
    temporary.unlink(missing_ok=True)
PY
}

rollback_standalone() {
  [ -L "$INSTALL_DIR/current" ] || fail "no current standalone release"
  [ -L "$INSTALL_DIR/previous" ] || fail "no retained rollback release"
  current_target="$(readlink "$INSTALL_DIR/current")"
  previous_target="$(readlink "$INSTALL_DIR/previous")"
  validate_release_target "$current_target" || fail "current release link is unsafe"
  validate_release_target "$previous_target" || fail "previous release link is unsafe"
  validate_release_entrypoints "$INSTALL_DIR/$current_target" || fail "current release is invalid"
  validate_release_entrypoints "$INSTALL_DIR/$previous_target" || fail "rollback release is invalid"
  atomic_link previous "$current_target" || fail "failed to prepare rollback link"
  if ! atomic_link current "$previous_target"; then
    atomic_link previous "$previous_target" || fail "rollback failed and previous link recovery is required"
    fail "failed to switch rollback release"
  fi
  if ! "$INSTALL_DIR/current/bin/workerbee" --version >/dev/null \
    || ! "$INSTALL_DIR/current/bin/ae" version >/dev/null; then
    atomic_link current "$current_target" || fail "rollback validation failed and current link recovery is required"
    atomic_link previous "$previous_target" || fail "rollback validation failed and previous link recovery is required"
    fail "rollback release failed post-switch validation"
  fi
  log "WorkerBee rolled back to $previous_target."
}

validate_release_target() {
  "$python_bin" - "$1" <<'PY'
import pathlib
import sys

parts = pathlib.PurePosixPath(sys.argv[1]).parts
valid = len(parts) == 2 and parts[0] == "releases" and parts[1] not in {"", ".", ".."}
raise SystemExit(0 if valid else 1)
PY
}

validate_release_entrypoints() {
  release_dir="$1"
  [ -d "$release_dir" ] || return 1
  [ ! -L "$release_dir" ] || return 1
  [ -x "$release_dir/bin/python" ] || return 1
  for entrypoint in workerbee ae; do
    [ -f "$release_dir/bin/$entrypoint" ] || return 1
    [ ! -L "$release_dir/bin/$entrypoint" ] || return 1
    [ -x "$release_dir/bin/$entrypoint" ] || return 1
    "$python_bin" - "$release_dir" "$entrypoint" <<'PY' || return 1
import os
import pathlib
import shlex
import sys

release = pathlib.Path(os.path.abspath(sys.argv[1]))
script = release / "bin" / sys.argv[2]
expected_python = str(release / "bin" / "python")
if script.is_symlink() or not script.is_file():
    raise SystemExit("console script must be a regular file")
if script.stat().st_size > 1024 * 1024:
    raise SystemExit("console script exceeds validation bound")
payload = script.read_bytes()
first_line = payload.splitlines()[0] if payload else b""
if not first_line.startswith(b"#!") or len(first_line) > 4096:
    raise SystemExit("console script has an invalid shebang")
try:
    command = shlex.split(first_line[2:].decode("utf-8", errors="strict"))
except (UnicodeDecodeError, ValueError) as exc:
    raise SystemExit("console script shebang is not parseable") from exc
if not command or not os.path.isabs(command[0]) or not os.access(command[0], os.X_OK):
    raise SystemExit("console script shebang target is not executable")
interpreter = os.path.abspath(command[0])
if interpreter == expected_python:
    pass
elif interpreter in {"/bin/sh", "/usr/bin/sh"} and expected_python.encode() in payload:
    # pip uses this wrapper form when an absolute interpreter path is too long.
    pass
else:
    raise SystemExit("console script is not bound to the final release interpreter")
if b"/.candidate." in payload:
    raise SystemExit("console script retains a relocated candidate path")
PY
  done
  "$release_dir/bin/workerbee" --version >/dev/null || return 1
  "$release_dir/bin/ae" version >/dev/null || return 1
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

if [ "$ACTION" = "rollback" ]; then
  need_cmd "$python_bin"
  need_cmd readlink
  rollback_standalone
  exit 0
fi

need_cmd "$python_bin"
need_cmd mktemp
need_cmd tar
"$python_bin" - <<'PY' || fail "Python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

tmp_dir="$(mktemp -d)"
created_release_dir=""
launcher_tmp=""
receipt_tmp=""
cleanup() {
  rm -rf "$tmp_dir"
  case "$created_release_dir" in
    "$INSTALL_DIR"/releases/workerbee-*) rm -rf "$created_release_dir" ;;
    "") ;;
    *) fail "refusing to remove an unexpected release path" ;;
  esac
  case "$launcher_tmp" in
    "$BIN_DIR"/.workerbee.new.*) rm -f "$launcher_tmp" ;;
    "") ;;
    *) fail "refusing to remove an unexpected launcher path" ;;
  esac
  case "$receipt_tmp" in
    "$INSTALL_DIR"/.install-receipt.new.*) rm -f "$receipt_tmp" ;;
    "") ;;
    *) fail "refusing to remove an unexpected receipt path" ;;
  esac
}
trap cleanup EXIT INT TERM

archive="$tmp_dir/$ARCHIVE_NAME"
checksums="$tmp_dir/$CHECKSUM_NAME"
wheelhouse="$tmp_dir/wheelhouse"
log "Downloading WorkerBee wheelhouse from $BASE_URL/$ARCHIVE_NAME"
fetch "$BASE_URL/$ARCHIVE_NAME" "$archive"
log "Downloading WorkerBee checksums from $BASE_URL/$CHECKSUM_NAME"
fetch "$BASE_URL/$CHECKSUM_NAME" "$checksums"
"$python_bin" - "$archive" "$checksums" "$ARCHIVE_NAME" <<'PY' || fail "wheelhouse checksum verification failed"
import hashlib
import pathlib
import sys

archive = pathlib.Path(sys.argv[1])
checksums = pathlib.Path(sys.argv[2])
archive_name = sys.argv[3]
matches = []
for line in checksums.read_text(encoding="utf-8").splitlines():
    fields = line.split()
    if len(fields) == 2 and fields[1].lstrip("*") == archive_name:
        matches.append(fields[0].lower())
if len(matches) != 1:
    raise SystemExit(f"expected exactly one checksum for {archive_name}")
expected = matches[0]
actual = hashlib.sha256(archive.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit(f"sha256 mismatch for {archive_name}")
PY
archive_sha="$(
"$python_bin" - "$archive" <<'PY'
import hashlib
import pathlib
import sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
"$python_bin" - "$archive" <<'PY' || fail "wheelhouse archive contains an unsafe member"
import pathlib
import re
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
private = re.compile(r"(?i)(?:^|[-_.])(?:private|internal)(?:[-_.]|$)|platform[-_]debug")
forbidden = {
    ".git", ".gitea", ".github", ".mypy_cache", ".nox", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", ".workerbee", "__pycache__", "build",
    "dist", "generated", "htmlcov", "node_modules", "state", "temp", "tmp",
}
seen = set()
for member in tarfile.open(archive, "r:gz").getmembers():
    path = pathlib.PurePosixPath(member.name)
    if member.name in seen:
        raise SystemExit(f"duplicate archive member: {member.name}")
    seen.add(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"unsafe archive path: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise SystemExit(f"unsafe archive member type: {member.name}")
    if any(
        part in forbidden or part.startswith(".env") or private.search(part)
        for part in path.parts
    ):
        raise SystemExit(f"non-public archive member: {member.name}")
PY
mkdir -p "$wheelhouse"
tar -xzf "$archive" -C "$wheelhouse"
wheelhouse="$(
"$python_bin" - "$wheelhouse" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
if (root / "RELEASE-MANIFEST.json").is_file():
    print(root)
else:
    children = list(root.iterdir())
    directories = [path for path in children if path.is_dir()]
    if len(children) != 1 or len(directories) != 1:
        raise SystemExit("wheelhouse archive must contain exactly one top-level directory")
    print(directories[0])
PY
)" || fail "wheelhouse archive root is ambiguous"

"$python_bin" - "$wheelhouse" <<'PY' || fail "wheelhouse contents do not match RELEASE-MANIFEST.json"
import hashlib
import json
import pathlib
import sys

wheelhouse = pathlib.Path(sys.argv[1])
manifest = json.loads((wheelhouse / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
entries = manifest.get("artifacts", [])
names = []
for entry in entries:
    name = entry.get("filename")
    if not isinstance(name, str) or pathlib.Path(name).name != name or name in names:
        raise SystemExit("invalid or duplicate wheelhouse manifest filename")
    if entry.get("role") not in {"package", "support", "sbom"}:
        raise SystemExit(f"invalid wheelhouse member role: {name}")
    if not isinstance(entry.get("publish"), bool):
        raise SystemExit(f"invalid wheelhouse publish flag: {name}")
    names.append(name)
    path = wheelhouse / name
    if not path.is_file() or path.stat().st_size != entry["size"]:
        raise SystemExit(f"missing or invalid wheelhouse member: {path.name}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise SystemExit(f"sha256 mismatch for wheelhouse member: {path.name}")
actual = {path.name for path in wheelhouse.iterdir()}
if actual != {*names, "RELEASE-MANIFEST.json"}:
    raise SystemExit("wheelhouse inventory differs from RELEASE-MANIFEST.json")
if "SBOM.cdx.json" not in names:
    raise SystemExit("wheelhouse is missing its deterministic SBOM")
PY

"$python_bin" - <<'PY' || fail "mixed ae runtime providers detected in the active environment"
from importlib.metadata import packages_distributions

providers = sorted(
    {name.lower().replace("_", "-") for name in packages_distributions().get("ae", [])}
)
if len(providers) > 1:
    raise SystemExit("mixed ae providers: " + ", ".join(providers))
PY

export PIP_NO_INDEX=1
export PIP_CONFIG_FILE=/dev/null
export PIP_DISABLE_PIP_VERSION_CHECK=1

package_spec="k1s-workerbee"
package_version=""
for wheel_candidate in "$wheelhouse"/k1s_workerbee-*.whl; do
  [ -f "$wheel_candidate" ] || continue
  wheel_base="$(basename "$wheel_candidate")"
  package_version="${wheel_base#k1s_workerbee-}"
  package_version="${package_version%%-*}"
  if [ "$package_version" != "" ]; then
    package_spec="k1s-workerbee==$package_version"
  fi
  break
done
[ "$package_version" != "" ] || fail "wheelhouse does not contain one WorkerBee wheel"

if [ "${VIRTUAL_ENV:-}" != "" ] && [ "$FORCE_STANDALONE" != "1" ]; then
  target_python="$VIRTUAL_ENV/bin/python"
  install_mode="active-venv"
  install_required=1
else
  install_mode="standalone"
  release_id="workerbee-${package_version}-${archive_sha%${archive_sha#????????????}}"
  releases_dir="$INSTALL_DIR/releases"
  mkdir -p "$releases_dir"
  validate_release_target "releases/$release_id" || fail "derived release identity is unsafe"
  release_dir="$releases_dir/$release_id"
  [ ! -L "$release_dir" ] || fail "existing release identity must not be a symlink"
  if [ -e "$release_dir" ]; then
    [ -d "$release_dir" ] || fail "existing release identity is invalid"
    install_required=0
  else
    mkdir "$release_dir" || fail "failed to reserve final release directory"
    created_release_dir="$release_dir"
    "$python_bin" -m venv "$release_dir" || fail "failed to create isolated final-path venv"
    install_required=1
  fi
  target_python="$release_dir/bin/python"
fi

if [ "$install_required" = "1" ]; then
  "$target_python" -m pip install \
    --no-compile \
    --no-index \
    --find-links "$wheelhouse" \
    "$package_spec" || fail "offline wheelhouse is incomplete or incompatible; refusing package-index fallback"
fi

"$target_python" -m pip check || fail "candidate environment dependency validation failed"
"$target_python" - <<'PY' || fail "candidate contains mixed or unexpected ae providers"
from importlib.metadata import packages_distributions

import ae
import workerbee

providers = sorted(
    {name.lower().replace("_", "-") for name in packages_distributions().get("ae", [])}
)
if providers != ["k1s-core"]:
    raise SystemExit("expected exactly k1s-core to own ae, found: " + ", ".join(providers))
if not getattr(workerbee, "__version__", None) or not getattr(ae, "__file__", None):
    raise SystemExit("candidate import smoke failed")
PY
"$target_python" -m workerbee --version >/dev/null || fail "candidate CLI smoke failed"

if [ "$install_mode" = "standalone" ]; then
  validate_release_entrypoints "$release_dir" || fail "final-path console-script validation failed"
  old_target=""
  old_previous_target=""
  had_current=0
  had_previous=0
  if [ -L "$INSTALL_DIR/current" ]; then
    had_current=1
    old_target="$(readlink "$INSTALL_DIR/current")"
    validate_release_target "$old_target" || fail "current release link is unsafe"
  elif [ -e "$INSTALL_DIR/current" ]; then
    fail "current release identity must be a symlink"
  fi
  if [ -L "$INSTALL_DIR/previous" ]; then
    had_previous=1
    old_previous_target="$(readlink "$INSTALL_DIR/previous")"
    validate_release_target "$old_previous_target" || fail "previous release link is unsafe"
  elif [ -e "$INSTALL_DIR/previous" ]; then
    fail "previous release identity must be a symlink"
  fi
  mkdir -p "$BIN_DIR"
  launcher_tmp="$BIN_DIR/.workerbee.new.$$"
  cat >"$launcher_tmp" <<EOF
#!/usr/bin/env sh
exec "$INSTALL_DIR/current/bin/python" -m workerbee "\$@"
EOF
  chmod 755 "$launcher_tmp"
  sh -n "$launcher_tmp" || fail "generated launcher is invalid"
  launcher_probe="$tmp_dir/workerbee-launcher-probe"
  cat >"$launcher_probe" <<EOF
#!/usr/bin/env sh
exec "$release_dir/bin/python" -m workerbee "\$@"
EOF
  chmod 700 "$launcher_probe"
  sh -n "$launcher_probe" || fail "generated launcher probe is invalid"
  "$launcher_probe" --version >/dev/null || fail "final-path launcher probe failed"
  receipt_tmp="$INSTALL_DIR/.install-receipt.new.$$"
  "$python_bin" - "$receipt_tmp" "$release_id" "${old_target:-}" <<'PY'
import json
import pathlib
import sys

temporary = pathlib.Path(sys.argv[1])
payload = {
    "schema": "workerbee.standalone-install/v1",
    "release_id": sys.argv[2],
    "current": f"releases/{sys.argv[2]}",
    "previous": sys.argv[3] or None,
    "runtime_state": "outside-versioned-venv",
    "activation": "atomic-symlink",
    "rollback_retained": bool(sys.argv[3]),
}
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.chmod(0o600)
PY

  had_launcher=0
  launcher_backup="$tmp_dir/workerbee-launcher.backup"
  if [ -e "$BIN_DIR/workerbee" ] || [ -L "$BIN_DIR/workerbee" ]; then
    [ -f "$BIN_DIR/workerbee" ] && [ ! -L "$BIN_DIR/workerbee" ] \
      || fail "existing WorkerBee launcher must be a regular file"
    cp -p "$BIN_DIR/workerbee" "$launcher_backup" || fail "failed to back up existing launcher"
    had_launcher=1
  fi
  had_receipt=0
  receipt_backup="$tmp_dir/install-receipt.backup"
  if [ -e "$INSTALL_DIR/install-receipt.json" ] || [ -L "$INSTALL_DIR/install-receipt.json" ]; then
    [ -f "$INSTALL_DIR/install-receipt.json" ] && [ ! -L "$INSTALL_DIR/install-receipt.json" ] \
      || fail "existing install receipt must be a regular file"
    cp -p "$INSTALL_DIR/install-receipt.json" "$receipt_backup" \
      || fail "failed to back up existing install receipt"
    had_receipt=1
  fi

  restore_release_links() {
    if [ "$had_current" = "1" ]; then
      atomic_link current "$old_target" || return 1
    else
      rm -f "$INSTALL_DIR/current" || return 1
    fi
    if [ "$had_previous" = "1" ]; then
      atomic_link previous "$old_previous_target" || return 1
    else
      rm -f "$INSTALL_DIR/previous" || return 1
    fi
  }

  restore_install_metadata() {
    if [ "$had_launcher" = "1" ]; then
      cp -p "$launcher_backup" "$BIN_DIR/workerbee" || return 1
    else
      rm -f "$BIN_DIR/workerbee" || return 1
    fi
    if [ "$had_receipt" = "1" ]; then
      cp -p "$receipt_backup" "$INSTALL_DIR/install-receipt.json" || return 1
    else
      rm -f "$INSTALL_DIR/install-receipt.json" || return 1
    fi
  }

  if [ "$old_target" != "" ] && [ "$old_target" != "releases/$release_id" ]; then
    atomic_link previous "$old_target" || fail "failed to retain previous release"
  fi
  if ! atomic_link current "releases/$release_id"; then
    restore_release_links || {
      created_release_dir=""
      fail "activation failed and release-link recovery is required"
    }
    fail "failed to activate final release"
  fi
  if ! "$INSTALL_DIR/current/bin/workerbee" --version >/dev/null \
    || ! "$INSTALL_DIR/current/bin/ae" version >/dev/null \
    || ! "$launcher_tmp" --version >/dev/null; then
    restore_release_links || {
      created_release_dir=""
      fail "post-activation validation failed and release-link recovery is required"
    }
    fail "post-activation entrypoint or launcher validation failed"
  fi
  if ! mv -f "$launcher_tmp" "$BIN_DIR/workerbee"; then
    restore_release_links || {
      created_release_dir=""
      fail "launcher installation failed and release-link recovery is required"
    }
    fail "failed to install WorkerBee launcher"
  fi
  if ! mv -f "$receipt_tmp" "$INSTALL_DIR/install-receipt.json"; then
    restore_install_metadata || {
      created_release_dir=""
      fail "receipt installation failed and metadata recovery is required"
    }
    restore_release_links || {
      created_release_dir=""
      fail "receipt installation failed and release-link recovery is required"
    }
    fail "failed to install WorkerBee receipt"
  fi
  created_release_dir=""
  target_python="$INSTALL_DIR/current/bin/python"
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
