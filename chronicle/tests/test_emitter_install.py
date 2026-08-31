import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class InstalledEmitterTest(unittest.TestCase):
    def run_installer(self, install_root, **extra_env):
        return subprocess.run(
            ["sh", str(ROOT / "scripts" / "install-emitter.sh")],
            check=True,
            cwd=ROOT,
            env={**os.environ, "CHRONICLE_INSTALL_ROOT": str(install_root), **extra_env},
            capture_output=True,
            text=True,
        )

    def test_canonical_installer_copies_complete_executable_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            install_root = pathlib.Path(temporary) / "bundle"
            self.run_installer(install_root)
            self.assertEqual(
                {path.name for path in install_root.iterdir()},
                {"chronicle-emit", "burrow-emit", "emit.py", "durable.py"},
            )
            for entry_point in ("chronicle-emit", "burrow-emit"):
                self.assertTrue(os.access(install_root / entry_point, os.X_OK))
                self.assertEqual(
                    (install_root / entry_point).stat().st_mode & 0o777, 0o700
                )
            self.assertEqual(
                (install_root / "chronicle-emit").read_bytes(),
                (install_root / "burrow-emit").read_bytes(),
            )
            self.assertEqual((install_root / "emit.py").stat().st_mode & 0o777, 0o600)

    def test_upgrade_reconciles_stale_content_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary) / "bundle"
            self.run_installer(target)
            (target / "stale.py").write_text("obsolete")
            (target / "emit.py").write_text("broken")
            self.run_installer(target)
            first = {
                p.name: (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in target.iterdir()
            }
            self.run_installer(target)
            second = {
                p.name: (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in target.iterdir()
            }
            self.assertEqual(first, second)
            self.assertNotIn("stale.py", first)

    def test_failed_staging_preserves_published_bundle_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            target = parent / "bundle"
            self.run_installer(target)
            before = {p.name: p.read_bytes() for p in target.iterdir()}
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_installer(target, CHRONICLE_INSTALL_FAIL_BEFORE_PUBLISH="1")
            self.assertEqual(before, {p.name: p.read_bytes() for p in target.iterdir()})
            self.assertEqual([], list(parent.glob(".bundle.*")))

    def test_hazardous_targets_are_rejected_without_following_symlinks(self):
        for target in ("", "/", ".", "relative/bundle"):
            with self.subTest(target=target):
                result = subprocess.run(
                    ["sh", str(ROOT / "scripts" / "install-emitter.sh")],
                    cwd=ROOT,
                    env={**os.environ, "CHRONICLE_INSTALL_ROOT": target},
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            outside = parent / "outside"
            outside.mkdir()
            target = parent / "bundle"
            target.symlink_to(outside, target_is_directory=True)
            result = subprocess.run(
                ["sh", str(ROOT / "scripts" / "install-emitter.sh")],
                cwd=ROOT,
                env={**os.environ, "CHRONICLE_INSTALL_ROOT": str(target)},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual([], list(outside.iterdir()))

    def install(self, root):
        bundle = pathlib.Path(root) / ".local" / "lib" / "chronicle-emitter"
        bundle.mkdir(parents=True, mode=0o700)
        for name in ("emit.py", "durable.py", "chronicle-emit"):
            shutil.copy2(ROOT / "hooks" / name, bundle / name)
        (bundle / "chronicle-emit").chmod(0o700)
        shutil.copy2(bundle / "chronicle-emit", bundle / "burrow-emit")
        return bundle

    def test_documented_bundle_runs_outside_repo_and_failed_transport_is_fail_open(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as cwd,
        ):
            bundle = self.install(home)
            event = {
                "session_id": "installed-layout",
                "cwd": cwd,
                "hook_event_name": "UserPromptSubmit",
                "prompt": "work",
            }
            env = dict(
                os.environ,
                HOME=home,
                CHRONICLE_MIRROR="",
                CHRONICLE_URL="http://127.0.0.1:1",
            )
            result = subprocess.run(
                [str(bundle / "chronicle-emit"), "--runner", "codex"],
                input=json.dumps(event),
                text=True,
                cwd=cwd,
                env=env,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0)
            log = pathlib.Path(home) / ".chronicle" / "events.jsonl"
            records = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(records[-1]["agent_id"], "codex:installed-layout")

    def default_root(self, existing=()):
        """Where the installer puts a bundle when no root is given."""
        with tempfile.TemporaryDirectory() as home:
            lib = pathlib.Path(home) / ".local" / "lib"
            for name in existing:
                (lib / name).mkdir(parents=True)
            environment = {
                k: v
                for k, v in os.environ.items()
                if k not in ("CHRONICLE_INSTALL_ROOT", "BURROW_INSTALL_ROOT")
            }
            subprocess.run(
                ["sh", str(ROOT / "scripts" / "install-emitter.sh")],
                check=True,
                cwd=ROOT,
                env={**environment, "HOME": home},
                capture_output=True,
                text=True,
            )
            return sorted(p.name for p in lib.iterdir())

    def test_a_first_install_uses_the_new_bundle_name(self):
        self.assertEqual(self.default_root(), ["chronicle-emitter"])

    def test_an_existing_bundle_is_upgraded_where_it_already_lives(self):
        """Hook configs name this directory absolutely; moving it breaks them."""
        self.assertEqual(self.default_root(["burrow-emitter"]), ["burrow-emitter"])

    def test_the_new_bundle_wins_once_the_operator_has_migrated(self):
        self.assertEqual(
            self.default_root(["burrow-emitter", "chronicle-emitter"]),
            ["burrow-emitter", "chronicle-emitter"],
        )

    def test_the_pre_rename_entry_point_still_runs_the_emitter(self):
        """A hook wired to burrow-emit must keep working across the upgrade."""
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as cwd,
        ):
            bundle = self.install(home)
            result = subprocess.run(
                [str(bundle / "burrow-emit")],
                input=json.dumps(
                    {
                        "session_id": "old-entry-point",
                        "cwd": cwd,
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "work",
                    }
                ),
                text=True,
                cwd=cwd,
                env=dict(os.environ, HOME=home, CHRONICLE_MIRROR="", CHRONICLE_URL=""),
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0)
            log = pathlib.Path(home) / ".chronicle" / "events.jsonl"
            records = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(records[-1]["agent_id"], "claude-code:old-entry-point")

    def test_launcher_is_exit_zero_when_bundle_dependency_is_missing(self):
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as cwd,
        ):
            bundle = self.install(home)
            (bundle / "durable.py").unlink()
            result = subprocess.run(
                [str(bundle / "chronicle-emit")],
                input="{}",
                text=True,
                cwd=cwd,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
