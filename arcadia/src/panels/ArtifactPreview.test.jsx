import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ArtifactPreview } from "./ArtifactPreview.jsx";

const artifact = { agent_id: "agent:keeper", artifact: "reports/a & b.md", ts: "2026-09-05T12:00:00Z" };
const preview = { kind: "markdown", content: "# Real report\n\n- First finding\n- Second finding", source: "current-file", media_type: "text/markdown", encoding: "utf-8", bytes: 50 };
const response = value => ({ ok: true, json: async () => value });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("controlled artifact previews", () => {
  it("only requests an exact recorded identity after an explicit click", async () => {
    const fetch = vi.fn().mockResolvedValue(response(preview));
    vi.stubGlobal("fetch", fetch);
    render(<ArtifactPreview artifact={artifact} />);
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Preview file" }));
    await screen.findByRole("heading", { name: "Real report" });
    const [url, options] = fetch.mock.calls[0];
    const parsed = new URL(url, "http://local.test");
    expect(parsed.pathname).toBe("/chronicle/artifacts/preview");
    expect(Object.fromEntries(parsed.searchParams)).toEqual({ agent_id: artifact.agent_id, path: artifact.artifact, ts: artifact.ts });
    expect(options.redirect).toBe("error");
    expect(options.credentials).toBe("same-origin");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText(/This may differ from the file/)).toBeVisible();
  });

  it("renders Markdown HTML, scripts and image URLs as inert text", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ ...preview, content: '# Heading\n\n<script>alert(1)</script>\n\n<img src="https://untrusted.invalid/a.png">\n\n![remote](https://untrusted.invalid/image.png)\n\n```html\n<iframe src="evil"></iframe>\n```' })));
    const { container } = render(<ArtifactPreview artifact={artifact} />);
    fireEvent.click(screen.getByRole("button", { name: "Preview file" }));
    await screen.findByRole("heading", { name: "Heading" });
    expect(container.querySelectorAll("script,img,iframe,a")).toHaveLength(0);
    expect(container).toHaveTextContent("<script>alert(1)</script>");
    expect(container).toHaveTextContent('<iframe src="evil"></iframe>');
  });

  it("shows disabled and unsupported backends without inventing content", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 503 });
    vi.stubGlobal("fetch", fetch);
    render(<ArtifactPreview artifact={artifact} />);
    fireEvent.click(screen.getByRole("button", { name: "Preview file" }));
    await screen.findByText("Artifact previews are not enabled on this Chronicle server.");
    fetch.mockResolvedValue({ ok: true, json: async () => { throw new SyntaxError("HTML fallback"); } });
    fireEvent.click(screen.getByRole("button", { name: "Preview file" }));
    await screen.findByText("This Chronicle server does not provide artifact previews yet.");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("only renders supported raster MIME types and rejects SVG responses", async () => {
    const image = { ...preview, kind: "image", media_type: "image/png", encoding: "base64", content: "aGVsbG8=", width: 1, height: 1 };
    const fetch = vi.fn().mockResolvedValue(response(image));
    vi.stubGlobal("fetch", fetch);
    render(<ArtifactPreview artifact={artifact} />);
    fireEvent.click(screen.getByRole("button", { name: "Preview file" }));
    expect(await screen.findByRole("img")).toHaveAttribute("src", "data:image/png;base64,aGVsbG8=");
    fetch.mockResolvedValue(response({ ...image, media_type: "image/svg+xml" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh preview" }));
    await screen.findByText("This image format cannot be previewed safely.");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("aborts stale requests when another artifact is selected", async () => {
    let resolve;
    const fetch = vi.fn().mockReturnValue(new Promise(done => { resolve = done; }));
    vi.stubGlobal("fetch", fetch);
    const view = render(<ArtifactPreview artifact={artifact} />);
    fireEvent.click(screen.getByRole("button", { name: "Preview file" }));
    const signal = fetch.mock.calls[0][1].signal;
    view.rerender(<ArtifactPreview artifact={{ ...artifact, artifact: "different.md" }} />);
    expect(signal.aborted).toBe(true);
    await act(async () => { resolve(response(preview)); });
    expect(screen.queryByRole("heading", { name: "Real report" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview file" })).toBeEnabled();
  });

  it("keeps text escaped and reports an image decoding failure", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ ...preview, kind: "text", content: "<b>literal</b>" }));
    vi.stubGlobal("fetch", fetch);
    const { container } = render(<ArtifactPreview artifact={artifact} />);
    fireEvent.click(screen.getByRole("button", { name: "Preview file" }));
    await screen.findByText("<b>literal</b>");
    expect(container.querySelector("b")).toBeNull();
    fetch.mockResolvedValue(response({ ...preview, kind: "image", media_type: "image/png", encoding: "base64", content: "aGVsbG8=", width: 1, height: 1 }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh preview" }));
    fireEvent.error(await screen.findByRole("img"));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("The recorded image could not be decoded."));
  });
  it("bounds formatted Markdown nodes and offers the complete text", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ ...preview, content: "- item\n".repeat(1200) + "Last line" })));
    render(<ArtifactPreview artifact={artifact} />);
    fireEvent.click(screen.getByRole("button", { name: "Preview file" }));
    await screen.findByText(/Showing the first 600 lines/);
    expect(screen.getAllByRole("listitem")).toHaveLength(600);
    fireEvent.click(screen.getByRole("button", { name: "Plain text", exact: true }));
    expect(screen.getByText(/Last line/)).toBeVisible();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });

});
