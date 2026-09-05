import { createElement, useEffect, useRef, useState } from "react";
import "./artifact-preview.css";

const imageTypes = new Set(["image/png", "image/jpeg", "image/webp"]);
function checkedPreview(value) {
  if (!value || typeof value.content !== "string" || value.source !== "current-file" || value.content.length > 2_800_000) throw new Error("This server returned an unsupported preview.");
  if (value.kind === "image") {
    if (!imageTypes.has(value.media_type) || value.encoding !== "base64" || !/^[A-Za-z0-9+/]*={0,2}$/.test(value.content) || !Number.isInteger(value.width) || !Number.isInteger(value.height) || value.width <= 0 || value.height <= 0 || value.width > 8192 || value.height > 8192 || value.width * value.height > 16_000_000) throw new Error("This image format cannot be previewed safely.");
  } else if (!["text", "markdown"].includes(value.kind) || value.encoding !== "utf-8" || value.content.length > 262144) throw new Error("This text format cannot be previewed safely.");
  return value;
}

// Deliberately small Markdown vocabulary: headings, paragraphs, lists and fenced
// code. Every source string stays a React text child; HTML and URLs never execute.
function Markdown({ content }) {
  const blocks = [];
  const allLines = content.replaceAll("\r\n", "\n").split("\n");
  const lines = allLines.slice(0, 600);
  let paragraph = [], code = null, list = [], ordered = false;
  const flushParagraph = () => { if (paragraph.length) blocks.push(<p key={blocks.length}>{paragraph.join(" ")}</p>); paragraph = []; };
  const flushList = () => { if (list.length) blocks.push(createElement(ordered ? "ol" : "ul", { key: blocks.length }, list.map((text, index) => <li key={index}>{text}</li>))); list = []; };
  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      flushParagraph(); flushList();
      if (code !== null) { blocks.push(<pre key={blocks.length}><code>{code.join("\n")}</code></pre>); code = null; }
      else code = [];
    } else if (code !== null) code.push(line);
    else if (!line.trim()) { flushParagraph(); flushList(); }
    else {
      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      const bullet = /^\s*(?:([-*])|\d+\.)\s+(.+)$/.exec(line);
      if (heading) { flushParagraph(); flushList(); blocks.push(createElement(`h${Math.min(6, heading[1].length + 3)}`, { key: blocks.length }, heading[2])); }
      else if (bullet) {
        flushParagraph();
        const nextOrdered = !bullet[1];
        if (list.length && ordered !== nextOrdered) flushList();
        ordered = nextOrdered; list.push(bullet[2]);
      } else { flushList(); paragraph.push(line); }
    }
  }
  flushParagraph(); flushList();
  if (code !== null) blocks.push(<pre key={blocks.length}><code>{code.join("\n")}</code></pre>);
  return <div className="ap-markdown">{blocks}{allLines.length > lines.length && <p className="ap-note">Showing the first 600 lines. Choose Plain text to read the complete preview.</p>}</div>;
}

export function ArtifactPreview({ artifact }) {
  const [preview, setPreview] = useState(null);
  const [plain, setPlain] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const controller = useRef(null);
  const identity = JSON.stringify([artifact?.agent_id, artifact?.artifact, artifact?.ts]);
  useEffect(() => {
    controller.current?.abort();
    controller.current = null;
    setPreview(null); setError(null); setLoading(false); setPlain(false);
    return () => controller.current?.abort();
  }, [identity]);
  async function load() {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setLoading(true); setError(null); setPreview(null); setPlain(false);
    try {
      const query = new URLSearchParams({ agent_id: artifact.agent_id, path: artifact.artifact, ts: artifact.ts });
      const response = await fetch(`/chronicle/artifacts/preview?${query}`, { signal: request.signal, credentials: "same-origin", redirect: "error", headers: { Accept: "application/json" } });
      if (!response.ok) {
        const messages = { 403: "This file is not in a published preview location, or its path is restricted.", 404: "This file or its recorded artifact is no longer available here.", 409: "This path matches multiple published files, so a preview cannot be selected.", 413: "This file is too large to preview.", 415: "This file type or encoding does not support a preview.", 503: "Artifact previews are not enabled on this Chronicle server." };
        throw new Error(messages[response.status] || "The preview service is unavailable. The recorded path is still available above.");
      }
      const result = checkedPreview(await response.json());
      if (!request.signal.aborted) setPreview(result);
    } catch (failure) {
      if (!request.signal.aborted) setError(failure instanceof SyntaxError ? "This Chronicle server does not provide artifact previews yet." : failure.message || "The preview could not be loaded.");
    } finally { if (!request.signal.aborted) setLoading(false); }
  }
  return <section className="ap-preview" aria-label="Artifact content preview">
    <div className="ap-toolbar"><button type="button" disabled={loading || !artifact?.artifact || !artifact?.agent_id || !artifact?.ts} onClick={load}>{loading ? "Loading preview…" : preview ? "Refresh preview" : "Preview file"}</button>{preview?.kind === "markdown" && <button type="button" aria-pressed={plain} onClick={() => setPlain(!plain)}>{plain ? "Formatted view" : "Plain text"}</button>}{preview && <button type="button" className="ap-close" onClick={() => setPreview(null)}>Close preview</button>}</div>
    {loading && <p role="status" className="ap-note">Reading the published artifact…</p>}
    {error && <p role="status" className="ap-error">{error}</p>}
    {preview && <><p className="ap-note">Current file preview · {Number.isFinite(preview.bytes) ? `${preview.bytes.toLocaleString()} bytes` : preview.media_type}. This may differ from the file when the artifact was recorded.</p><div className="ap-content">{preview.kind === "image" ? <img src={`data:${preview.media_type};base64,${preview.content}`} width={preview.width} height={preview.height} alt={`Preview of ${artifact.artifact}`} onError={() => { setPreview(null); setError("The recorded image could not be decoded."); }} /> : preview.kind === "markdown" && !plain ? <Markdown content={preview.content} /> : <pre className="ap-text">{preview.content || "This file is empty."}</pre>}</div></>}
    {!loading && !preview && !error && <p className="ap-note">Available for supported files in Chronicle's published preview directories.</p>}
  </section>;
}
