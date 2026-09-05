"""User service lifecycle for the standalone Chronicle emitter (macOS/Linux)."""

import json
import os
import pathlib
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
import threading

try:
    from hooks import delivery_worker, emit, presence
except ImportError:
    import delivery_worker
    import emit
    import presence

LABEL = "org.warren.chronicle-delivery"
SETTINGS = ("URL", "TOKEN", "MIRROR", "MIRROR_TOKEN")


def configuration_path():
    return pathlib.Path(emit.LOG_DIR) / "delivery-config.json"


def load_configuration():
    settings = presence.read(str(configuration_path()))
    for name in SETTINGS:
        os.environ["CHRONICLE_" + name] = settings.get(name, "")
    return settings


def service_file(system=None):
    system = system or platform.system()
    if system == "Darwin":
        return pathlib.Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    if system == "Linux":
        return (
            pathlib.Path.home() / ".config" / "systemd" / "user" / (LABEL + ".service")
        )
    raise RuntimeError("delivery service supports macOS and Linux")


def service_definition(python, script, system=None):
    system = system or platform.system()
    if system == "Darwin":
        return plistlib.dumps(
            dict(
                Label=LABEL,
                ProgramArguments=[python, script, "run"],
                RunAtLoad=True,
                KeepAlive=True,
                ThrottleInterval=5,
                Umask=63,
            )
        )

    # systemd has its own quoting rules, including percent specifiers.
    def quote(value):
        return (
            '"'
            + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            + '"'
        )

    return (
        "[Unit]\nDescription=Chronicle telemetry delivery\nAfter=network.target\n"
        "[Service]\nExecStart=" + quote(python) + " " + quote(script) + " run\n"
        "Restart=always\nRestartSec=5\nUMask=0077\n"
        "[Install]\nWantedBy=default.target\n"
    ).encode()


def control(action):
    system = platform.system()
    path = service_file(system)
    domain = "gui/" + str(os.getuid())
    if system == "Darwin":
        if action == "start":
            subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        elif action == "stop":
            subprocess.run(
                ["launchctl", "bootout", domain + "/" + LABEL],
                check=False,
                capture_output=True,
            )
        elif action == "restart":
            subprocess.run(
                ["launchctl", "kickstart", "-k", domain + "/" + LABEL], check=True
            )
        else:
            subprocess.run(["launchctl", "print", domain + "/" + LABEL], check=True)
    else:
        if action == "start":
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", LABEL], check=True
            )
        elif action == "stop":
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", LABEL], check=False
            )
        else:
            subprocess.run(["systemctl", "--user", action, LABEL], check=True)


def main(action):
    os.umask(0o077)
    if action == "install":
        path = configuration_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings = presence.read(str(path))
        settings.setdefault("MIRROR", emit.DEFAULT_MIRROR)
        for name in SETTINGS:
            if "CHRONICLE_" + name in os.environ:
                settings[name] = os.environ["CHRONICLE_" + name]
        if not settings.get("URL"):
            raise RuntimeError("set CHRONICLE_URL before installing the service")
        settings["BUNDLE"] = str(pathlib.Path(__file__).resolve().parent)
        with presence.transaction(str(path)) as current:
            current.update(settings)
        target = service_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        # An absolute interpreter survives login PATH differences and bundle upgrades.
        python = shutil.which("python3") or sys.executable
        script = str(pathlib.Path(__file__).resolve())
        control("stop")
        target.write_bytes(service_definition(python, script))
        target.chmod(0o600)
        control("start")
    elif action == "run":
        load_configuration()
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        delivery_worker.DeliveryWorker().run(stop)
    elif action == "remove":
        control("stop")
        service_file().unlink(missing_ok=True)
        # Disabling managed mode restores legacy delivery; queued data is untouched.
        path = configuration_path()
        if path.exists():
            path.rename(path.with_suffix(".disabled.json"))
    elif action == "status":
        print(
            json.dumps(
                presence.read(os.path.join(emit.LOG_DIR, "delivery-status.json")),
                indent=2,
            )
        )
        control("status")
    elif action in {"restart", "stop", "start"}:
        control(action)
    else:
        raise RuntimeError(
            "usage: delivery_service.py install|run|start|stop|restart|status|remove"
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) == 2 else "")
