"""Crash-safe file-generation primitives shared by hook and server.

Callers hold their protocol's stable lock while collecting authority. This
module deliberately owns no locks or process state; it only owns the durable
write, publish, and retirement ordering.
"""

import glob
import errno
import json
import os


PENDING_SUFFIX = ".pending"
LOCK_SUFFIX = ".lock"
REPLAY_PREFIX = ".replay."


def pending_path(path):
    return path + PENDING_SUFFIX


def lock_path(path):
    return path + LOCK_SUFFIX


def replay_paths(path):
    return sorted(glob.glob(path + REPLAY_PREFIX + "*"))


def replay_path(path, generation):
    if not generation or os.sep in generation:
        raise ValueError("invalid replay generation: %r" % (generation,))
    return path + REPLAY_PREFIX + generation


def fsync_parent(path):
    descriptor = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stage_lines(path, lines):
    pending = pending_path(path)
    with open(pending, "w", encoding="utf-8") as stream:
        stream.writelines(lines)
        stream.flush()
        os.fsync(stream.fileno())
    return pending


def stage_json(path, value, ensure_ascii=True):
    pending = pending_path(path)
    with open(pending, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=ensure_ascii, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    return pending


def publish_staged(replacements):
    targets = []
    for pending, target in replacements:
        os.replace(pending, target)
        targets.append(target)
    for directory in {os.path.dirname(os.path.abspath(path)) for path in targets}:
        fsync_parent(os.path.join(directory, "."))


def retire_files(paths):
    changed_parents = []
    failure = None
    try:
        for path in paths:
            try:
                os.unlink(path)
                parent = os.path.dirname(os.path.abspath(path))
                if parent not in changed_parents:
                    changed_parents.append(parent)
            except OSError as error:
                if isinstance(error, FileNotFoundError) or error.errno == errno.ENOENT:
                    continue
                failure = error
                break
    finally:
        # A later unlink failure must not leave earlier successful removals only
        # in the directory cache.  Sync those removals before reporting failure.
        for directory in changed_parents:
            try:
                fsync_parent(os.path.join(directory, "."))
            except OSError as error:
                if failure is None:
                    failure = error
    if failure is not None:
        raise failure
    return bool(changed_parents)


def publish_lines(path, lines, retire=()):
    """Durably publish one generation, then durably retire its inputs."""
    publish_staged(((stage_lines(path, lines), path),))
    retire_files(retire)
