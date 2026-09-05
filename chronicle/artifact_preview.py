"""Bounded previews of retained artifacts inside explicitly published directories.

This module never fetches URLs, guesses an agent's historical cwd, or exposes a
file browser. All path components are opened relative to no-follow descriptors.
"""
from __future__ import annotations

import base64
import datetime
import os
import re
import stat
from pathlib import Path
from collections.abc import Iterable, Mapping

TEXT_LIMIT = 256 * 1024
IMAGE_LIMIT = 2 * 1024 * 1024
PIXEL_LIMIT = 16_000_000
TEXT_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv", ".json": "application/json", ".log": "text/plain"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
SENSITIVE = re.compile(r"(?:^|[._-])(?:secrets?|credentials?|passwords?|tokens?|private[_-]?keys?|id_rsa|id_ed25519)(?:$|[._-])", re.I)


class PreviewError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _safe_parts(path: Path) -> bool:
    return all(part not in {"..", "."} and not part.startswith(".") and not SENSITIVE.search(part)
               for part in path.parts if part not in {path.anchor, ""})


def _open_directory(path: Path) -> int:
    """Walk even configured root ancestors without following a replaced symlink."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_file(root: Path, relative: Path) -> int:
    descriptor = _open_directory(root)
    try:
        for part in relative.parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def _image_dimensions(data: bytes, extension: str) -> tuple[int, int] | None:
    if extension == ".png":
        if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return None
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if extension in {".jpg", ".jpeg"}:
        if not data.startswith(b"\xff\xd8"):
            return None
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                return None
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                return None
            marker = data[offset]
            offset += 1
            if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if marker in {0xD9, 0xDA} or offset + 2 > len(data):
                return None
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                return None
            if marker in {0xC0, 0xC1, 0xC2} and length >= 8:
                return int.from_bytes(data[offset + 5:offset + 7], "big"), int.from_bytes(data[offset + 3:offset + 5], "big")
            offset += length
        return None
    if extension == ".webp" and len(data) >= 25 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X" and len(data) >= 30 and not data[20] & 0x02:
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
        if kind == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    return None


def read_preview(artifacts: Iterable[Mapping], roots: tuple[Path, ...], *, agent_id: str, path: str, ts: str) -> dict:
    if not roots:
        raise PreviewError(503, "Artifact previews are disabled on this Chronicle server.")
    if not any(record.get("agent_id") == agent_id and record.get("artifact") == path and record.get("ts") == ts for record in artifacts):
        raise PreviewError(404, "This artifact record is no longer available.")
    if not path or len(path) > 4096 or "\x00" in path or "\\" in path or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", path):
        raise PreviewError(415, "Only recorded local files can be previewed.")
    artifact = Path(path)
    if ".." in path.split("/") or not _safe_parts(artifact):
        raise PreviewError(403, "This artifact path is not allowed for preview.")
    extension = artifact.suffix.lower()
    media_type = TEXT_TYPES.get(extension) or IMAGE_TYPES.get(extension)
    if not media_type:
        raise PreviewError(415, "This file type does not support a safe preview.")
    candidates: list[tuple[Path, Path]] = []
    for configured in roots:
        # Normalize spelling only, never follow a configured-root symlink. The
        # descriptor walk also rejects symlinked ancestors such as /tmp on macOS;
        # operators must configure their canonical published-directory spelling.
        root = Path(os.path.abspath(Path(configured).expanduser()))
        if artifact.is_absolute():
            try:
                relative = artifact.relative_to(root)
            except ValueError:
                continue
        else:
            relative = artifact
        if not relative.parts or not _safe_parts(relative):
            continue
        candidate = (root, relative)
        if candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        raise PreviewError(403, "This artifact is outside the published preview directories.")
    descriptors: list[int] = []
    denied = False
    try:
        for root, relative in candidates:
            try:
                descriptor = _open_file(root, relative)
            except FileNotFoundError:
                continue
            except OSError:
                denied = True
                continue
            descriptors.append(descriptor)
        if not descriptors:
            raise PreviewError(403 if denied else 404, "This artifact file is unavailable for preview.")
        if len(descriptors) != 1:
            raise PreviewError(409, "This relative artifact path matches more than one published directory.")
        descriptor = descriptors[0]
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PreviewError(403, "Only ordinary, unlinked artifact files can be previewed.")
        limit = IMAGE_LIMIT if extension in IMAGE_TYPES else TEXT_LIMIT
        if info.st_size > limit:
            raise PreviewError(413, "This artifact is too large to preview.")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise PreviewError(413, "This artifact is too large to preview.")
        result = {"media_type": media_type, "bytes": len(data), "recorded_at": ts,
                  "modified_at": datetime.datetime.fromtimestamp(info.st_mtime, datetime.UTC).isoformat(), "source": "current-file"}
        if extension in IMAGE_TYPES:
            dimensions = _image_dimensions(data, extension)
            if not dimensions or min(dimensions) <= 0 or max(dimensions) > 8192 or dimensions[0] * dimensions[1] > PIXEL_LIMIT:
                raise PreviewError(415, "This image has an unsupported format or dimensions.")
            return {**result, "kind": "image", "encoding": "base64", "content": base64.b64encode(data).decode("ascii"), "width": dimensions[0], "height": dimensions[1]}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PreviewError(415, "Only UTF-8 text can be previewed.") from error
        if "\x00" in text:
            raise PreviewError(415, "Binary files cannot be previewed as text.")
        return {**result, "kind": "markdown" if extension in {".md", ".markdown"} else "text", "encoding": "utf-8", "content": text}
    except OSError as error:
        raise PreviewError(404, "This artifact file is unavailable for preview.") from error
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
