import os
import tempfile
import unittest
from unittest import mock

from hooks import durable


class DurableGenerationTests(unittest.TestCase):
    def test_publish_orders_file_fsync_replace_directory_fsync_and_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "authority")
            replay = path + durable.REPLAY_PREFIX + "old"
            with open(replay, "w", encoding="utf-8") as stream:
                stream.write("old\n")
            operations = []
            real_replace = os.replace
            real_unlink = os.unlink

            def replace(source, target):
                operations.append("replace")
                real_replace(source, target)

            def unlink(target):
                operations.append("retire")
                real_unlink(target)

            with (
                mock.patch.object(durable.os, "replace", side_effect=replace),
                mock.patch.object(durable.os, "unlink", side_effect=unlink),
                mock.patch.object(
                    durable,
                    "fsync_parent",
                    side_effect=lambda _path: operations.append("dir-fsync"),
                ),
            ):
                durable.publish_lines(path, ("new\n",), retire=(replay,))

            self.assertEqual(
                operations, ["replace", "dir-fsync", "retire", "dir-fsync"]
            )
            with open(path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "new\n")
            self.assertFalse(os.path.exists(replay))

    def test_generation_path_conventions_are_centralized(self):
        self.assertEqual(durable.pending_path("state"), "state.pending")
        self.assertEqual(durable.lock_path("state"), "state.lock")
        self.assertEqual(durable.replay_path("state", "one"), "state.replay.one")
        with self.assertRaisesRegex(ValueError, "invalid replay generation"):
            durable.replay_path("state", "../elsewhere")

    def test_retirement_syncs_prior_success_before_propagating_real_error(self):
        operations = []
        failure = PermissionError("authority cannot be retired")

        def unlink(path):
            operations.append(("unlink", path))
            if path == "second":
                raise failure

        with (
            mock.patch.object(durable.os, "unlink", side_effect=unlink),
            mock.patch.object(
                durable,
                "fsync_parent",
                side_effect=lambda path: operations.append(("fsync", path)),
            ),
        ):
            with self.assertRaises(PermissionError) as raised:
                durable.retire_files(("first", "second", "never-attempted"))
        self.assertIs(raised.exception, failure)
        self.assertEqual(
            [kind for kind, _ in operations], ["unlink", "unlink", "fsync"]
        )

    def test_retirement_suppresses_only_missing_files(self):
        missing = FileNotFoundError("already gone")
        denied = OSError(5, "I/O failure")
        unlink = mock.Mock(side_effect=(missing, None, denied))
        fsync = mock.Mock()
        with (
            mock.patch.object(durable.os, "unlink", unlink),
            mock.patch.object(durable, "fsync_parent", fsync),
        ):
            with self.assertRaises(OSError) as raised:
                durable.retire_files(("missing", "removed", "failed"))
        self.assertIs(raised.exception, denied)
        fsync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
