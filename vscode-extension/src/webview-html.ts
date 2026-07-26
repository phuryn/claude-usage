import { randomBytes } from "node:crypto";

/**
 * A single call-to-action button rendered in the status / placeholder pane.
 * `command` is a VS Code command id invoked via a `command:` URI, which
 * requires the hosting webview to set `enableCommandUris: true`.
 *
 * Any glyph belongs inside `label` as literal Unicode (e.g. "↻ Retry") — the
 * whole label is HTML-escaped, so there is no raw-markup escape hatch.
 */
export interface WebviewAction {
  label: string;
  command: string;
}

/**
 * Webview HTML that embeds the running Python dashboard via an iframe.
 * Shared by both surfaces: the sidebar WebviewView and the editor WebviewPanel.
 *
 * Two states:
 *   - When `url` is set we render the iframe at that URL.
 *   - When `url` is null we render a status / placeholder pane showing
 *     `statusText` plus an optional action button.
 *
 * We rely on VS Code's webview Content-Security-Policy. Allowing the
 * dashboard's localhost origin via `frame-src http://127.0.0.1:* http://localhost:*`
 * is enough; the dashboard ships its own CSP for what it loads inside.
 *
 * The iframe sandbox includes `allow-downloads` so the dashboard's CSV export
 * (a Blob + `a.download` click) works inside the webview — without it Chromium
 * silently blocks the download.
 */
export function renderHtml(
  url: string | null,
  statusText: string,
  nonce: string,
  iconUri = "",
  cspSource = "",
  action?: WebviewAction,
): string {
  if (url) {
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; frame-src http://127.0.0.1:* http://localhost:*; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>Claude Usage</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; background: #161617; }
  iframe { border: 0; width: 100%; height: 100vh; display: block; }
</style>
</head>
<body>
<iframe src="${escapeHtml(url)}" sandbox="allow-scripts allow-same-origin allow-forms allow-downloads"></iframe>
</body>
</html>`;
  }

  // Status / placeholder pane — styled to match the dashboard header (same icon,
  // title, and elevated-palette colors) so the cold-start screen doesn't jar.
  const imgSrc = cspSource ? ` img-src ${cspSource};` : "";
  const logo = iconUri ? `<span class="logo"></span>` : "";
  const button = action
    ? `<p><a class="retry" href="command:${escapeHtml(action.command)}">${escapeHtml(action.label)}</a></p>`
    : "";
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none';${imgSrc} style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>Claude Usage</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #BFBFBF; background: #161617; padding: 24px; line-height: 1.5; }
  .brand { display: flex; align-items: center; gap: 10px; margin: 0 0 18px; }
  .brand .logo { width: 26px; height: 26px; flex-shrink: 0; background-color: #BFBFBF; -webkit-mask: url("${iconUri}") no-repeat center / contain; mask: url("${iconUri}") no-repeat center / contain; }
  .brand h1 { font-size: 18px; font-weight: 600; color: #BFBFBF; margin: 0; }
  p { color: #BFBFBF; font-size: 13px; margin: 0 0 8px; }
  p.hint { color: #6F6F70; }
  code { background: #1E1F20; border: 1px solid #2C2D2E; border-radius: 4px; padding: 1px 5px; font-size: 12px; }
  a.retry { display: inline-block; margin: 6px 0 14px; padding: 6px 14px; font-size: 13px; font-weight: 600; color: #161617; background: #BFBFBF; border-radius: 6px; text-decoration: none; }
  a.retry:hover { background: #D8D8D8; }
</style>
</head>
<body>
<div class="brand">${logo}<h1>Claude Code Usage</h1></div>
<p>${escapeHtml(statusText) || "The dashboard server is not running yet."}</p>
${button}
<p class="hint">Run <code>Claude Usage: Open Dashboard</code> from the command palette.</p>
</body>
</html>`;
}

/**
 * Escape HTML for safe interpolation into the templates above.
 * Exported for testability.
 */
export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Generate a one-shot nonce for the CSP script-src directive.
 * 24 bytes of crypto-random output, base64url-encoded (URL-safe, no
 * padding, always 32 chars).
 */
export function makeNonce(): string {
  return randomBytes(24).toString("base64url");
}
