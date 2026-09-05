#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
# CHRONICLE_INSTALL_ROOT names the target. The pre-rename BURROW_INSTALL_ROOT was
# read here too until warren#361 finished the rename.
if [ "${CHRONICLE_INSTALL_ROOT+x}" = x ]; then
  install_root=$CHRONICLE_INSTALL_ROOT
else
  lib="${HOME:?HOME is required}/.local/lib"
  # An existing bundle is upgraded where it already lives. Runner hook configs
  # name this directory by absolute path, and moving the bundle out from under
  # them would leave those hooks pointing at nothing — silently, since a missing
  # hook command is not an error the runner reports. Only a first install picks
  # the new name; moving an existing one is an operator step.
  if [ -d "$lib/burrow-emitter" ] && [ ! -d "$lib/chronicle-emitter" ]; then
    install_root="$lib/burrow-emitter"
  else
    install_root="$lib/chronicle-emitter"
  fi
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

install -m 600 "$repo_root/hooks/emit.py" "$repo_root/hooks/durable.py" "$repo_root/hooks/delivery_worker.py" "$repo_root/hooks/delivery_service.py" "$repo_root/hooks/presence.py" "$stage/"
install -m 700 "$repo_root/hooks/chronicle-emit" "$stage/chronicle-emit"
# The bundle answers to both entry-point names for one release. A hook config
# written before the rename invokes burrow-emit by absolute path, and an upgrade
# that published only the new name would break it the next time the runner fired
# a hook rather than at install time.
install -m 700 "$repo_root/hooks/chronicle-emit" "$stage/burrow-emit"

# Validate the private, complete candidate before the published path changes.
[ "$(find "$stage" -mindepth 1 -maxdepth 1 -type f | wc -l | tr -d ' ')" = 7 ]
[ -x "$stage/chronicle-emit" ]
[ -x "$stage/burrow-emit" ]
python3 -m py_compile "$stage/"*.py
rm -rf -- "$stage/__pycache__"

if [ "${CHRONICLE_INSTALL_FAIL_BEFORE_PUBLISH:-}" = 1 ]; then
  printf '%s\n' "Injected failure before publication" >&2
  exit 1
fi

# Publish a clean install with rename(2), or atomically exchange complete old and
# new directories on upgrade. Unsupported platforms fail safely instead of
# exposing a gap or a partially reconciled bundle.
CHRONICLE_PUBLISH_STAGE=$stage CHRONICLE_PUBLISH_TARGET=$install_root python3 - <<'PY'
import ctypes
import os
import platform

stage = os.environ["CHRONICLE_PUBLISH_STAGE"]
target = os.environ["CHRONICLE_PUBLISH_TARGET"]
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
printf '%s\n' "Installed Chronicle emitter: $install_root/chronicle-emit"

# Opt in only after Chronicle supports /telemetry and /events/batch. An already
# installed service is restarted on every atomic bundle upgrade.
if [ "${1:-}" = --service ]; then
  python3 "$install_root/delivery_service.py" install
elif [ -f "${HOME}/.chronicle/delivery-config.json" ] && python3 - "$install_root" <<'PYCONFIG'
import json, os, sys
with open(os.path.expanduser("~/.chronicle/delivery-config.json")) as stream:
    config = json.load(stream)
sys.exit(0 if config.get("BUNDLE") == sys.argv[1] else 1)
PYCONFIG
then
  python3 "$install_root/delivery_service.py" restart
fi
