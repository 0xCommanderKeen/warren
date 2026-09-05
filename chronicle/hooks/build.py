#!/usr/bin/env python3
"""Flatten the hook emitter into one self-contained stdlib-only file.

    python3 hooks/build.py                     # to stdout
    python3 hooks/build.py --output emit.py    # atomically, to a file

**Two shapes, one emitter.** ``scripts/install-emitter.sh`` publishes the *installed
bundle*: a directory holding ``emit.py``, ``durable.py`` and the ``chronicle-emit``
launcher. This build produces the *one-file bundle*: the same emitter flattened into a
single script, for a host that can only take one file. Bare "the bundle" means neither;
docs/protocol.md says which is which.

``hooks/emit.py`` is the emitter's source and stays directly runnable in place — it is
what the operator's own ``~/.claude/settings.json`` fires on every tool use, and it is
allowed to grow siblings. ``hooks/durable.py`` is the first of those. Anywhere the
emitter has to arrive as *one file* — steward vendors it into the resident image, where
the build context is a single directory and there is no pip — that split is the problem
this build solves.

**How.** The bundle is ``emit.py`` verbatim, with one block replaced: the
``import durable`` fallback becomes ``durable.py``'s own source, embedded as a string
and materialized as a module with ``types.ModuleType`` + ``exec(compile(...))``. No
import hoisting, no rewriting, no analysis of what either file uses — the two sources
are carried as they were written, and the only thing this build understands about them
is where one imports the other. A traceback still names ``durable.py`` and the right
line, because the embedded source is compiled under its own filename.

**Determinism.** Same two source files in, byte-identical artifact out. That is what
lets steward's suite rebuild the bundle at HEAD and compare it to its vendored copy
byte for byte, which is the drift guard that a recorded checksum could never be: a
pinned hash detects tampering with the copy and stays green forever while the source
sails away (warren#234).

The provenance header therefore carries **content digests and no git values**. A commit
sha or a committer date would be honest for exactly as long as nobody rebases: the
artifact's bytes would change with no source change at all, and the comparison test
would go red for a reason that has nothing to do with the emitter. ``git log -1 --
chronicle/hooks/`` names the commit whenever a human wants it; the digests below name
the bytes, which is the thing a byte comparison can actually check.
"""

import argparse
import hashlib
import os
import sys
import tempfile


HOOKS = os.path.dirname(os.path.abspath(__file__))

#: The exact block in emit.py that reaches for the sibling module. The bundle replaces
#: it, so it is also the build's anchor: if emit.py's import of durable is ever written
#: some other way, this build must fail loudly rather than quietly ship a one-file
#: artifact that still imports a module that is not there.
IMPORT_ANCHOR = """\
try:
    from hooks import durable
except ImportError:  # standalone deployment invokes this file from hooks/
    import durable
"""

#: The embedded source is a raw triple-single-quoted literal, opened on the assignment
#: line so that line N of durable.py is line N of the compiled module. That only works
#: while durable.py contains no ``'''`` of its own, which the build checks rather than
#: assumes.
DELIMITER = "'''"

SHEBANG = "#!/usr/bin/env python3\n"

HEADER = """\
#
# GENERATED FILE — DO NOT EDIT.
#
# The chronicle hook emitter as one self-contained stdlib-only file: hooks/emit.py
# verbatim, with its `import durable` block replaced by hooks/durable.py's own source,
# materialized as a module. Built by hooks/build.py; the emitter is not written here.
#
# To change what this file does, edit chronicle/hooks/emit.py or chronicle/hooks/durable.py
# and rebuild:
#
#     python3 chronicle/hooks/build.py --output <path>
#
# steward vendors a copy of this artifact into docker/resident/chronicle-emit.py with
# `make vendor-emitter`, and its suite rebuilds the bundle at HEAD and compares byte for
# byte — so a hand edit here, or a source change nobody re-vendored, is a red build
# rather than a resident emitting a protocol nobody reads.
#
# Built from these bytes and nothing else:
#   hooks/emit.py     sha256:{emit}
#   hooks/durable.py  sha256:{durable}
#
# No commit and no date, deliberately: this header is compared byte for byte against a
# rebuild, and every git-derived value changes under a rebase while the sources do not.
# `git log -1 -- chronicle/hooks/` names the commit; the digests above name the bytes.
#
"""

EMBED = """\
# --- hooks/durable.py, embedded ---------------------------------------------------
# emit.py imports durable as a sibling module; a one-file artifact has no siblings, so
# the source is carried here and materialized as one. The text between the delimiters is
# durable.py byte for byte, compiled under its own name so a traceback still points at
# the right line of the right file.
import types as _bundled_types

_DURABLE_SOURCE = r{delimiter}{source}{delimiter}

durable = _bundled_types.ModuleType("durable")
durable.__file__ = "durable.py"
exec(compile(_DURABLE_SOURCE, "durable.py", "exec"), durable.__dict__)
del _bundled_types
# --- end of hooks/durable.py ------------------------------------------------------
"""


def digest(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def bundle(emit_source, durable_source):
    """The one-file emitter, as text. Pure: same sources in, same bytes out."""
    if not emit_source.startswith(SHEBANG):
        raise ValueError("emit.py must begin with %r; the bundle keeps its shebang first" % SHEBANG)
    if emit_source.count(IMPORT_ANCHOR) != 1:
        raise ValueError(
            "emit.py no longer contains exactly one copy of the durable import block "
            "this build replaces:\n\n%s\nUpdate IMPORT_ANCHOR in hooks/build.py to match "
            "how emit.py imports durable now." % IMPORT_ANCHOR
        )
    if DELIMITER in durable_source:
        raise ValueError(
            "durable.py contains %s, which would close the literal the bundle embeds it "
            "in. Use double quotes there, or change how build.py embeds it." % DELIMITER
        )
    if not durable_source.endswith("\n"):
        raise ValueError(
            "durable.py must end with a newline, or the closing delimiter joins its last line"
        )

    header = HEADER.format(emit=digest(emit_source), durable=digest(durable_source))
    embed = EMBED.format(delimiter=DELIMITER, source=durable_source)
    body = emit_source[len(SHEBANG) :].replace(IMPORT_ANCHOR, embed)
    return SHEBANG + header + body


def build(hooks=HOOKS):
    """Read the two sources out of a hooks directory and bundle them."""
    sources = []
    for name in ("emit.py", "durable.py"):
        with open(os.path.join(hooks, name), encoding="utf-8") as stream:
            sources.append(stream.read())
    text = bundle(*sources)
    start = text.index("def delivery_module():")
    end = text.index("\n\ndef main(", start)
    modules = {}
    for name in ("presence", "delivery_worker"):
        with open(os.path.join(hooks, name + ".py"), encoding="utf-8") as stream:
            modules[name] = stream.read()
    loader = """def delivery_module():
    import types
    if 'delivery_worker' not in sys.modules:
        sys.modules['durable'] = durable
        sys.modules['emit'] = sys.modules[__name__]
        for name, source in _DELIVERY_MODULES.items():
            module = types.ModuleType(name)
            sys.modules[name] = module
            exec(compile(source, name + '.py', 'exec'), module.__dict__)
    return sys.modules['delivery_worker']
"""
    return text[:start] + "_DELIVERY_MODULES = " + repr(modules) + "\n\n" + loader + text[end:]


def write(text, destination):
    """Publish through a temporary file: a failed build leaves no half-written artifact."""
    directory = os.path.dirname(os.path.abspath(destination))
    handle, staged = tempfile.mkstemp(dir=directory, prefix=".bundle.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(staged, 0o644)
        os.replace(staged, destination)
    except BaseException:
        if os.path.exists(staged):
            os.unlink(staged)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--hooks", default=HOOKS, help="the directory holding emit.py and durable.py"
    )
    parser.add_argument(
        "--output", help="write the bundle here instead of to standard output"
    )
    arguments = parser.parse_args(argv)

    try:
        text = build(arguments.hooks)
    except (OSError, ValueError) as exc:
        print("build.py: %s" % exc, file=sys.stderr)
        return 1

    if arguments.output:
        write(text, arguments.output)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
