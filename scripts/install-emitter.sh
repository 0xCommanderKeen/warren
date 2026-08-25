#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
if [ "${BURROW_INSTALL_ROOT+x}" = x ]; then
  install_root=$BURROW_INSTALL_ROOT
else
  install_root="${HOME:?HOME is required}/.local/lib/burrow-emitter"
fi

case $install_root in
  ''|/|.|..) printf '%s\n' "Refusing hazardous install target: $install_root" >&2; exit 2 ;;
esac
case $install_root in /*) ;; *) printf '%s\n' "Install target must be absolute: $install_root" >&2; exit 2 ;; esac

parent=$(dirname -- "$install_root")
name=$(basename -- "$install_root")
case $name in ''|.|..) printf '%s\n' "Refusing hazardous install target: $install_root" >&2; exit 2 ;; esac

# Creating only the parent keeps publication on the target filesystem. Refuse a
# link at the publication point: replacing one would be surprising and following
# one could write outside the requested boundary.
install -d -m 700 "$parent"
if [ -L "$install_root" ]; then
  printf '%s\n' "Refusing symlink install target: $install_root" >&2
  exit 2
fi
if [ -e "$install_root" ] && [ ! -d "$install_root" ]; then
  printf '%s\n' "Install target is not a directory: $install_root" >&2
  exit 2
fi

stage=$(mktemp -d "$parent/.${name}.stage.XXXXXX")
cleanup() {
  [ ! -e "$stage" ] || rm -rf -- "$stage"
}
trap cleanup EXIT HUP INT TERM

install -m 600 "$repo_root/hooks/emit.py" "$repo_root/hooks/durable.py" "$stage/"
install -m 700 "$repo_root/hooks/burrow-emit" "$stage/burrow-emit"

# Validate the private, complete candidate before the published path changes.
[ "$(find "$stage" -mindepth 1 -maxdepth 1 -type f | wc -l | tr -d ' ')" = 3 ]
[ -x "$stage/burrow-emit" ]
python3 -m py_compile "$stage/emit.py" "$stage/durable.py"
rm -rf -- "$stage/__pycache__"

if [ "${BURROW_INSTALL_FAIL_BEFORE_PUBLISH:-}" = 1 ]; then
  printf '%s\n' "Injected failure before publication" >&2
  exit 1
fi

# Publish a clean install with rename(2), or atomically exchange complete old and
# new directories on upgrade. Unsupported platforms fail safely instead of
# exposing a gap or a partially reconciled bundle.
BURROW_PUBLISH_STAGE=$stage BURROW_PUBLISH_TARGET=$install_root python3 - <<'PY'
import ctypes
import os
import platform

stage = os.environ["BURROW_PUBLISH_STAGE"]
target = os.environ["BURROW_PUBLISH_TARGET"]
if os.path.lexists(target) and os.path.islink(target):
    raise SystemExit("refusing symlink target during publication")
if not os.path.exists(target):
    os.rename(stage, target)
    raise SystemExit(0)

libc = ctypes.CDLL(None, use_errno=True)
system = platform.system()
if system == "Linux":
    exchange = libc.renameat2
    exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint]
    result = exchange(-100, os.fsencode(stage), -100, os.fsencode(target), 2)
elif system == "Darwin":
    exchange = libc.renamex_np
    exchange.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    result = exchange(os.fsencode(stage), os.fsencode(target), 2)
else:
    raise SystemExit("atomic directory exchange is unavailable on " + system)
if result:
    code = ctypes.get_errno()
    raise OSError(code, os.strerror(code), target)
PY

# After an upgrade, the stage name now holds the old complete directory.
if [ -d "$stage" ]; then rm -rf -- "$stage"; fi
stage=$parent/.${name}.published
trap - EXIT HUP INT TERM
printf '%s\n' "Installed Burrow emitter: $install_root/burrow-emit"
