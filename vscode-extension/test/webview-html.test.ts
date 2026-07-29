import { describe, it, expect } from "vitest";
import { renderHtml, escapeHtml, makeNonce, WebviewAction } from "../src/webview-html";

const NONCE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345";
const RETRY: WebviewAction = { label: "↻ Retry", command: "claudeUsage.open" };

describe("escapeHtml", () => {
  it("escapes the five HTML-significant characters", () => {
    expect(escapeHtml(`<script>alert("x&y'z")</script>`))
      .toBe("&lt;script&gt;alert(&quot;x&amp;y&#39;z&quot;)&lt;/script&gt;");
  });

  it("passes through safe text unchanged", () => {
    expect(escapeHtml("Claude Usage Dashboard")).toBe("Claude Usage Dashboard");
  });

  it("handles empty input", () => {
    expect(escapeHtml("")).toBe("");
  });
});

describe("makeNonce", () => {
  it("is base64url (alphanumeric plus - and _, no padding)", () => {
    expect(makeNonce()).toMatch(/^[A-Za-z0-9_-]+$/);
  });

  it("is exactly 32 chars (24 random bytes → 32 base64url chars)", () => {
    expect(makeNonce()).toHaveLength(32);
  });

  it("yields different values on consecutive calls", () => {
    expect(makeNonce()).not.toBe(makeNonce());
  });
});

describe("renderHtml with iframe URL", () => {
  it("embeds the iframe pointing at the given URL", () => {
    const html = renderHtml("http://127.0.0.1:54321/", "", NONCE);
    expect(html).toContain('src="http://127.0.0.1:54321/"');
    expect(html).toContain("<iframe");
  });

  it("escapes URL into the iframe src so attribute syntax can't break", () => {
    const html = renderHtml(`http://127.0.0.1:9000/?q="><script>x</script>`, "", NONCE);
    expect(html).not.toContain("<script>x</script>");
    expect(html).toContain("&quot;");
    expect(html).toContain("&lt;script&gt;");
  });

  it("includes a CSP frame-src that allows localhost", () => {
    const html = renderHtml("http://127.0.0.1:9000/", "", NONCE);
    expect(html).toContain("frame-src http://127.0.0.1:* http://localhost:*");
  });

  it("includes the script-src nonce", () => {
    const html = renderHtml("http://127.0.0.1:9000/", "", NONCE);
    expect(html).toContain(`script-src 'nonce-${NONCE}'`);
  });

  it("sandbox grants only what the dashboard needs (incl. downloads for CSV export)", () => {
    const html = renderHtml("http://127.0.0.1:9000/", "", NONCE);
    expect(html).toContain('sandbox="allow-scripts allow-same-origin allow-forms allow-downloads"');
    expect(html).not.toContain("allow-popups");
  });

  it("frame-src does NOT include third-party CDN (iframe has its own CSP)", () => {
    const html = renderHtml("http://127.0.0.1:9000/", "", NONCE);
    expect(html).not.toContain("cdn.jsdelivr.net");
  });
});

describe("renderHtml with null URL (status pane)", () => {
  it("renders the placeholder when no URL is set", () => {
    const html = renderHtml(null, "", NONCE);
    expect(html).toContain("Claude Code Usage");
    expect(html).toContain("not running yet");
    expect(html).not.toContain("<iframe");
  });

  it("renders a custom status message when provided", () => {
    const html = renderHtml(null, "Server failed to bind to port 8080", NONCE);
    expect(html).toContain("Server failed to bind to port 8080");
  });

  it("escapes status text", () => {
    const html = renderHtml(null, "<img onerror=x>", NONCE);
    expect(html).not.toContain("<img onerror=x>");
    expect(html).toContain("&lt;img onerror=x&gt;");
  });

  it("does NOT include the frame-src CSP (no iframe to allow)", () => {
    const html = renderHtml(null, "", NONCE);
    expect(html).not.toContain("frame-src");
  });

  it("renders the logo and an img-src CSP when an icon URI is provided", () => {
    const html = renderHtml(null, "", NONCE, "https://host/icon.svg", "vscode-webview://abc");
    expect(html).toContain('class="logo"');
    expect(html).toContain("img-src vscode-webview://abc");
    expect(html).toContain('mask: url("https://host/icon.svg")');
  });

  it("omits the logo and img-src when no icon URI is provided", () => {
    const html = renderHtml(null, "", NONCE);
    expect(html).not.toContain('class="logo"');
    expect(html).not.toContain("img-src");
  });
});

describe("renderHtml action button", () => {
  it("renders no action button when no action is given", () => {
    const html = renderHtml(null, "Starting dashboard at http://127.0.0.1:8080/…", NONCE);
    expect(html).not.toContain('class="retry"');
    expect(html).not.toContain("command:");
  });

  it("renders the retry action with its command URI", () => {
    const html = renderHtml(null, "Failed to start dashboard: timed out", NONCE, "", "", RETRY);
    expect(html).toContain('href="command:claudeUsage.open"');
    expect(html).toContain("Retry");
  });

  it("renders an arbitrary action label and command (not hardcoded to retry)", () => {
    const html = renderHtml(null, "The dashboard is open in an editor tab.", NONCE, "", "", {
      label: "Show tab",
      command: "claudeUsage.openInEditor",
    });
    expect(html).toContain('href="command:claudeUsage.openInEditor"');
    expect(html).toContain("Show tab");
    // The old template hardcoded claudeUsage.open — make sure it's really gone.
    expect(html).not.toContain('command:claudeUsage.open"');
  });

  it("escapes the action label so it can't inject markup", () => {
    const html = renderHtml(null, "", NONCE, "", "", {
      label: `<img onerror=x>`,
      command: "claudeUsage.open",
    });
    expect(html).not.toContain("<img onerror=x>");
    expect(html).toContain("&lt;img onerror=x&gt;");
  });

  it("escapes the action command so it can't break out of the href attribute", () => {
    const html = renderHtml(null, "", NONCE, "", "", {
      label: "Go",
      command: `x" onclick="evil()`,
    });
    expect(html).not.toContain('onclick="evil()');
    expect(html).toContain("&quot;");
  });

  it("ignores the action in the iframe branch — there is no status pane to host it", () => {
    const html = renderHtml("http://127.0.0.1:9000/", "", NONCE, "", "", RETRY);
    expect(html).toContain("<iframe");
    expect(html).not.toContain('class="retry"');
  });
});
