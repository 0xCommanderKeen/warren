import base64
import dataclasses
import datetime
import os
from pathlib import Path
import struct
import tempfile
import unittest
from fastapi.testclient import TestClient
import artifact_preview as previews
from config import Config
import serve


class ArtifactPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "exports"
        self.root.mkdir()
        self.identity = {"agent_id": "test:preview", "path": "report.md", "ts": "2026-09-05T12:00:00Z"}
        (self.root / "report.md").write_text("# A real report\n<script>never execute</script>", encoding="utf-8")

    def read(self, path=None, roots=None):
        identity = {**self.identity, "path": path or self.identity["path"]}
        record = {"agent_id": identity["agent_id"], "artifact": identity["path"], "ts": identity["ts"]}
        return previews.read_preview([record], (self.root,) if roots is None else roots, **identity)

    def error(self, status, **kwargs):
        with self.assertRaises(previews.PreviewError) as raised:
            self.read(**kwargs)
        self.assertEqual(raised.exception.status, status)

    def test_markdown_and_absolute_paths_return_current_file_content(self):
        result = self.read()
        self.assertEqual(result["content"], self.read(path=str(self.root / "report.md"))["content"])
        self.assertEqual((result["kind"], result["encoding"], result["source"]), ("markdown", "utf-8", "current-file"))
        self.assertIn("<script>", result["content"])
        self.assertEqual(result["bytes"], len(result["content"].encode()))

    def test_disabled_and_identity_must_match_all_three_fields(self):
        self.error(503, roots=())
        record = {"agent_id": self.identity["agent_id"], "artifact": "report.md", "ts": self.identity["ts"]}
        for field, value in [("agent_id", "other"), ("path", "unknown.md"), ("ts", "other")]:
            with self.subTest(field=field), self.assertRaises(previews.PreviewError) as raised:
                previews.read_preview([record], (self.root,), **{**self.identity, field: value})
            self.assertEqual(raised.exception.status, 404)

    def test_traversal_outside_roots_and_sensitive_paths_are_denied(self):
        for path in ["../outside.txt", "nested/../../outside.txt", ".env.txt", ".hidden/report.md", "secrets/report.md", "credentials.json", "access-token.txt", str(self.root.parent / "outside.txt")]:
            with self.subTest(path=path): self.error(403, path=path)

    def test_symlink_files_and_parent_directories_never_escape(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("not published")
        (self.root / "link.txt").symlink_to(outside)
        (self.root / "inside.txt").symlink_to(self.root / "report.md")
        (self.root / "linked").symlink_to(self.root.parent, target_is_directory=True)
        for path in ["link.txt", "inside.txt", "linked/outside.txt"]:
            with self.subTest(path=path): self.error(403, path=path)

    def test_a_replaced_or_symlinked_configured_root_is_not_followed(self):
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.error(403, roots=(alias,))

    def test_hardlinks_and_fifos_are_not_read(self):
        os.link(self.root / "report.md", self.root / "copy.md")
        self.error(403, path="copy.md")
        os.mkfifo(self.root / "pipe.txt")
        self.error(403, path="pipe.txt")

    def test_missing_file_and_ambiguous_relative_paths(self):
        self.error(404, path="missing.txt")
        second = self.root.parent / "second"
        second.mkdir()
        (second / "report.md").write_text("other")
        self.error(409, roots=(self.root, second))

    def test_size_type_and_binary_limits(self):
        (self.root / "large.txt").write_bytes(b"x" * (previews.TEXT_LIMIT + 1))
        self.error(413, path="large.txt")
        for path in ["page.html", "image.svg", "archive.zip", "https://example.org/file.txt", "report\\file.txt"]:
            with self.subTest(path=path): self.error(415, path=path)
        for path, content in [("binary.txt", b"\x00test"), ("invalid.txt", b"\xff\xfe")]:
            (self.root / path).write_bytes(content)
            self.error(415, path=path)

    def test_png_preview_fixed_mime_and_bounded_dimensions(self):
        content = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
        (self.root / "pixel.png").write_bytes(content)
        result = self.read(path="pixel.png")
        self.assertEqual((result["kind"], result["media_type"], result["width"], result["height"]), ("image", "image/png", 1, 1))
        self.assertEqual(base64.b64decode(result["content"]), content)
        (self.root / "huge.png").write_bytes(content[:16] + struct.pack(">II", 100000, 100000) + content[24:])
        self.error(415, path="huge.png")
        (self.root / "fake.png").write_text("<svg onload='bad()'/>")
        self.error(415, path="fake.png")

    def test_jpeg_and_webp_dimensions(self):
        jpeg = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x02\x00\x03\x01\x01\x11\x00\xff\xd9"
        self.assertEqual(previews._image_dimensions(jpeg, ".jpg"), (3, 2))
        webp = b"RIFF" + struct.pack("<I", 22) + b"WEBPVP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + b"\x02\x00\x00\x01\x00\x00"
        self.assertEqual(previews._image_dimensions(webp, ".webp"), (3, 2))
        self.assertIsNone(previews._image_dimensions(webp[:20] + b"\x02" + webp[21:], ".webp"))


class ArtifactPreviewHTTPTests(unittest.TestCase):
    def test_route_uses_retained_projection_and_opted_in_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            exports = root / "exports"
            exports.mkdir()
            (exports / "report.md").write_text("# Recorded output")
            config = dataclasses.replace(Config(), events=root / "events.jsonl", villagers_dir=root / "villagers", artifact_preview_roots=(exports,))
            ts = datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            event = {"v": 0, "ts": ts, "source": "test", "agent_id": "test:preview", "project": "arcadia", "cwd": "", "type": "artifact_produced", "payload": {"artifact": "report.md"}}
            query = {"agent_id": event["agent_id"], "path": "report.md", "ts": ts}
            with TestClient(serve.create_app(config)) as client:
                self.assertEqual(client.get("/artifacts/preview", params=query).status_code, 404)
                self.assertEqual(client.post("/events", json=event).status_code, 204)
                response = client.get("/artifacts/preview", params=query)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["content"], "# Recorded output")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(client.get("/artifacts/preview", params={**query, "ts": "not-retained"}).status_code, 404)
            with TestClient(serve.create_app(dataclasses.replace(config, artifact_preview_roots=()))) as disabled:
                self.assertEqual(disabled.get("/artifacts/preview", params=query).status_code, 503)

    def test_explicit_opt_in_setting(self):
        self.assertEqual(Config.from_env({}).artifact_preview_roots, ())
        config = Config.from_env({"CHRONICLE_ARTIFACT_PREVIEW_ROOTS": os.pathsep.join(["/srv/published", "", "/srv/reports"])})
        self.assertEqual(config.artifact_preview_roots, (Path("/srv/published"), Path("/srv/reports")))


if __name__ == "__main__":
    unittest.main()
