# Published artifact previews

Arcadia previews current text, Markdown, PNG, JPEG, and WebP artifacts from
explicitly published directories. **Disabled by default**; ordinary artifact
metadata and path copying continue to work without previews.

Set `CHRONICLE_ARTIFACT_PREVIEW_ROOTS` to an OS-path-separated list. On Linux/macOS:

```sh
CHRONICLE_ARTIFACT_PREVIEW_ROOTS=/srv/published-artifacts:/srv/published-reports
```

Use dedicated output directories containing material suitable for everyone who
can read Chronicle. This endpoint has the same read access as `/state`; the
event-ingest token does not authenticate reads. Artifact events do not grant
permission to publish private working trees. Dot paths and common secret,
credential, and token filenames are denied, but no filename check can identify
every secret embedded in ordinary documents. Do not publish home directories,
filesystem roots, or private project directories.

The directories must exist in **Chronicle's filesystem**. Containers require an
explicit read-only output mount and the environment setting. The normal compose
service already loads `env_file: .env`, so place the setting in the `.env` beside
`chronicle/deploy/compose.yaml` on the host; no additional environment forwarding
is required. Recreate the container after editing its environment or mounts.
This change does
not add mounts, enable deployed previews, or fetch files from agent machines.
Older backends keep Arcadia's metadata usable and show previews as unavailable.

## Endpoint and identity

`GET /artifacts/preview?agent_id=…&path=…&ts=…`

All three values must exactly match a currently retained artifact in the same
projection as `/state`. `path` is the record's `artifact` value. Absolute paths
must lie beneath a published root. Relative paths are tried beneath the roots
and must match exactly one file; Chronicle never guesses a historical agent cwd.
URLs are never fetched.

The JSON response contains `kind`, `media_type`, `encoding`, `content`, `bytes`,
`recorded_at`, `modified_at`, and `source: "current-file"`. Images also have
`width` and `height`, with base64 content. This previews the file **now**, not an
immutable copy from the recorded timestamp. Responses are `no-store` and
`X-Content-Type-Options: nosniff`.

Fixed limits in `artifact_preview.py`:

- UTF-8 `.txt`, `.md`, `.markdown`, `.csv`, `.json`, `.log`: 256 KiB.
- `.png`, `.jpg`, `.jpeg`, `.webp`: 2 MiB; at most 8192 pixels per axis and
  16 million pixels overall. Animated WebP is unsupported.
- HTML, SVG, binary text, traversal, dot paths, sensitive filenames, symlinked
  files/directories, hard-linked files, and special files are refused.

Descriptor-relative `O_NOFOLLOW` opens protect every path component, including
root ancestors. Reading stops at the size limit plus one byte even when a file
grows after its size check. Configured roots must use their canonical, non-symlink spelling too (for example
`/private/tmp/exports` rather than `/tmp/exports` on macOS).

Arcadia requests `/chronicle/artifacts/preview` only after **Preview file** is
selected. Production nginx forwards that exact route; the existing Vite
`/chronicle` proxy handles development. Markdown supports React-rendered headings,
paragraphs, lists, and code blocks. HTML, embedded images, and Markdown links stay
inert text. Image MIME types are restricted to the three supported raster formats.
The client never loads an artifact's original path or URL as a preview source.

Errors: 403 restricted, 404 missing file/record, 409 ambiguous relative path,
413 size limit, 415 unsupported format, 503 disabled.
