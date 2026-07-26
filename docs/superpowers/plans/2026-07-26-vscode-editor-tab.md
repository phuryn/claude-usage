# Open Dashboard as Editor Tab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users host the claude-usage dashboard in a VS Code editor tab instead of the cramped primary sidebar, via a setting plus an on-demand command.

**Architecture:** Extract the shared webview HTML renderer out of `sidebar.ts`, add a singleton `DashboardPanel` wrapping `vscode.WebviewPanel`, and refactor `extension.ts` so server startup (`ensureServer()`) is decoupled from surface reveal. Exactly one dashboard instance is live at a time, guaranteed by a *derived* rule — `panelExists ? "editor" : openLocation` — with no stored active-surface flag. All routing predicates live in vscode-free pure functions so they can be unit-tested.

**Tech Stack:** TypeScript 5.4, VS Code extension API `^1.94.0`, vitest 1.6 (node environment, `vscode` stubbed via `vi.mock`). Python side is untouched except the version constant.

**Spec:** [docs/superpowers/specs/2026-07-26-vscode-editor-tab-design.md](../specs/2026-07-26-vscode-editor-tab-design.md)

## Global Constraints

- All extension work happens in `vscode-extension/`. Run `npm test` (i.e. `vitest run`) from that directory.
- Python tests run from the repo root: `python3 -m unittest discover -s tests -v`.
- `src/webview-html.ts` and `src/open-target.ts` MUST NOT import `vscode` — their tests run with no mock at all. Keep vscode imports confined to `sidebar.ts`, `editor-panel.ts`, `extension.ts`.
- The existing `vi.mock("vscode", () => ({}), { virtual: true })` pattern in `test/sidebar.test.ts` is how this repo stubs the vscode module. Factories passed to `vi.mock` are hoisted, so any helper they reference must be declared inside `vi.hoisted(() => ...)`.
- Setting ids, verbatim: `claudeUsage.openLocation` (enum `"sidebar"` | `"editor"`, default `"sidebar"`), `claudeUsage.collapseSidebarOnOpenInEditor` (boolean, default `false`).
- Command id, verbatim: `claudeUsage.openInEditor`, title `"Claude Usage: Open Dashboard in Editor Tab"`, icon `"$(link-external)"`.
- Placeholder copy, verbatim: panel open → `The dashboard is open in an editor tab.` with button `Show tab`; no panel and `openLocation: editor` → `The dashboard opens in an editor tab.` with button `Open editor tab`. Both buttons invoke `claudeUsage.openInEditor`.
- Release version for this feature: **1.6.0**. `tests/test_version.py` asserts `scanner.VERSION` == top `## vX.Y.Z` CHANGELOG heading == `vscode-extension/package.json` `version`. All three move in one commit or the Python suite fails.
- Never add a `WebviewPanelSerializer`. Never add `onStartupFinished` activation. Both are explicit non-goals.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/webview-html.ts` | create | Pure HTML rendering for both surfaces: `renderHtml`, `escapeHtml`, `makeNonce`, `WebviewAction`. No vscode import. |
| `src/open-target.ts` | create | Pure routing predicates: `resolveOpenTarget`, `deriveActiveSurface`, `sidebarRenderFor`, `shouldCollapseSidebar`, and the two placeholder action constants. No vscode import. |
| `src/editor-panel.ts` | create | `DashboardPanel` — singleton `vscode.WebviewPanel` wrapper mirroring the sidebar's API. |
| `src/sidebar.ts` | modify | `DashboardSidebar` only. Render helpers move out; gains `setPlaceholder()` and `isVisible()`. |
| `src/extension.ts` | modify | Host/orchestrator. Splits `openDashboard()` into `ensureServer()` + `showSidebar()` + `showEditorTab()` + `renderSidebar()`. |
| `package.json` | modify | Two settings, one command, one `view/title` menu entry, version bump. |
| `test/webview-html.test.ts` | create | Render tests relocated from `sidebar.test.ts`, plus action-button coverage. |
| `test/open-target.test.ts` | create | The routing truth table. |
| `test/editor-panel.test.ts` | create | Singleton lifecycle and panel options. |
| `test/sidebar.test.ts` | modify | Keeps only `DashboardSidebar` tests; adds placeholder mode and `isVisible()`. |
| `scanner.py`, `CHANGELOG.md`, `vscode-extension/README.md` | modify | Release chores. |

---

### Task 1: Extract `webview-html.ts` with a parameterized action button

**Files:**
- Create: `vscode-extension/src/webview-html.ts`
- Create: `vscode-extension/test/webview-html.test.ts`
- Modify: `vscode-extension/src/sidebar.ts:1-100` (remove the render helpers, import them instead)
- Modify: `vscode-extension/test/sidebar.test.ts:1-144` (delete the relocated describes)

**Interfaces:**
- Consumes: nothing.
- Produces: `export interface WebviewAction { label: string; command: string }`; `export function renderHtml(url: string | null, statusText: string, nonce: string, iconUri?: string, cspSource?: string, action?: WebviewAction): string`; `export function escapeHtml(s: string): string`; `export function makeNonce(): string`.

The only behavior change is the last parameter: `showRetry: boolean` becomes `action?: WebviewAction`. The old template hardcoded `href="command:claudeUsage.open"` and the label `&#8635; Retry`; both become caller-supplied. Glyphs now travel inside `label` as literal Unicode (`"↻ Retry"`) so the whole label can be HTML-escaped — no raw-HTML injection point.

- [ ] **Step 1: Write the failing test**

Create `vscode-extension/test/webview-html.test.ts`. No `vi.mock` — this module has no vscode import.

```typescript
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vscode-extension && npx vitest run test/webview-html.test.ts`
Expected: FAIL — `Failed to resolve import "../src/webview-html"`.

- [ ] **Step 3: Create `src/webview-html.ts`**

Move the three functions out of `sidebar.ts` verbatim, changing only the last parameter of `renderHtml`.

```typescript
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
```

- [ ] **Step 4: Strip the moved code out of `src/sidebar.ts`**

Delete lines 1-100 of `sidebar.ts` (the `randomBytes` import, `renderHtml`, `escapeHtml`, `makeNonce` and their doc comments) and replace the file's top with:

```typescript
import * as vscode from "vscode";
import { renderHtml, makeNonce, WebviewAction } from "./webview-html";
import { ServerManager } from "./server-manager";
```

Then update the `DashboardSidebar` internals. Replace the `failed` field and the `setError` / `render` methods:

```typescript
  // The action button shown in the status pane, if any. Set by setError()
  // (Retry) and setPlaceholder(); cleared by setUrl() and setStatus().
  private action: WebviewAction | undefined;
```

```typescript
  /** Called from extension.ts after the server is ready. */
  setUrl(url: string | null): void {
    this.currentUrl = url;
    this.action = undefined;
    this.render();
  }

  /** Non-error status (initial / "starting…"): no action button. */
  setStatus(text: string): void {
    this.statusText = text;
    this.action = undefined;
    this.render();
  }

  /** A start attempt failed: show the status plus a Retry button. */
  setError(text: string): void {
    this.statusText = text;
    this.action = RETRY_ACTION;
    this.render();
  }

  private render(): void {
    if (!this.view) return;
    this.view.webview.html = renderHtml(
      this.currentUrl, this.statusText, makeNonce(), this.iconUri, this.cspSource, this.action,
    );
  }
```

Add the constant just above the class:

```typescript
/**
 * Retry button for the sidebar's error pane. Routes through claudeUsage.open
 * so retrying honors whatever claudeUsage.openLocation currently says.
 */
const RETRY_ACTION: WebviewAction = { label: "↻ Retry", command: "claudeUsage.open" };
```

Delete the now-unused `failed` field and its declaration comment. Leave `setPlaceholder` and `isVisible` for Task 3.

- [ ] **Step 5: Trim `test/sidebar.test.ts`**

Delete the `escapeHtml`, `makeNonce`, `renderHtml with iframe URL`, and `renderHtml with null URL (status pane)` describe blocks — they now live in `test/webview-html.test.ts`. Keep the `DashboardSidebar onShow auto-start` block, the `vi.mock`, and `makeFakeView`. Change the import line to:

```typescript
import { DashboardSidebar } from "../src/sidebar";
```

- [ ] **Step 6: Run the full extension suite and the compiler**

Run: `cd vscode-extension && npm test && npx tsc -p . --noEmit`
Expected: all tests PASS, no TypeScript errors.

- [ ] **Step 7: Commit**

```bash
git add vscode-extension/src/webview-html.ts vscode-extension/src/sidebar.ts \
        vscode-extension/test/webview-html.test.ts vscode-extension/test/sidebar.test.ts
git commit -m "refactor(vscode): extract webview-html with a parameterized action button

Both the sidebar and the forthcoming editor panel render identical HTML, so
renderHtml/escapeHtml/makeNonce move to their own vscode-free module. The
status pane's hardcoded 'Retry' -> claudeUsage.open button becomes a
caller-supplied WebviewAction, which the editor panel needs to point its own
retry at claudeUsage.openInEditor."
```

---

### Task 2: Pure routing predicates in `open-target.ts`

**Files:**
- Create: `vscode-extension/src/open-target.ts`
- Create: `vscode-extension/test/open-target.test.ts`

**Interfaces:**
- Consumes: `WebviewAction` (type-only) from Task 1's `src/webview-html.ts`.
- Produces:
  - `export type OpenTarget = "sidebar" | "editor"`
  - `export type SidebarRender = { kind: "live" } | { kind: "placeholder"; message: string; action: WebviewAction }`
  - `export function resolveOpenTarget(value: unknown): OpenTarget`
  - `export function deriveActiveSurface(panelExists: boolean, openLocation: OpenTarget): OpenTarget`
  - `export function sidebarRenderFor(panelExists: boolean, openLocation: OpenTarget): SidebarRender`
  - `export function shouldCollapseSidebar(enabled: boolean, sidebarVisible: boolean): boolean`
  - `export const SHOW_TAB_ACTION: WebviewAction`, `export const OPEN_TAB_ACTION: WebviewAction`

This module is the whole feature's decision table, isolated from vscode so it is directly testable. `extension.ts` becomes a thin adapter that reads config, calls these, and applies the result.

- [ ] **Step 1: Write the failing test**

Create `vscode-extension/test/open-target.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import {
  resolveOpenTarget,
  deriveActiveSurface,
  sidebarRenderFor,
  shouldCollapseSidebar,
  SHOW_TAB_ACTION,
  OPEN_TAB_ACTION,
} from "../src/open-target";

describe("resolveOpenTarget", () => {
  it("passes through the two declared enum values", () => {
    expect(resolveOpenTarget("sidebar")).toBe("sidebar");
    expect(resolveOpenTarget("editor")).toBe("editor");
  });

  it("falls back to sidebar for an unset value", () => {
    expect(resolveOpenTarget(undefined)).toBe("sidebar");
    expect(resolveOpenTarget(null)).toBe("sidebar");
    expect(resolveOpenTarget("")).toBe("sidebar");
  });

  it("falls back to sidebar for a hand-edited settings.json typo", () => {
    expect(resolveOpenTarget("Editor")).toBe("sidebar");
    expect(resolveOpenTarget("tab")).toBe("sidebar");
    expect(resolveOpenTarget("panel")).toBe("sidebar");
  });

  it("falls back to sidebar for non-string values", () => {
    expect(resolveOpenTarget(42)).toBe("sidebar");
    expect(resolveOpenTarget(true)).toBe("sidebar");
    expect(resolveOpenTarget({})).toBe("sidebar");
  });
});

describe("deriveActiveSurface", () => {
  it("is the setting when no panel exists", () => {
    expect(deriveActiveSurface(false, "sidebar")).toBe("sidebar");
    expect(deriveActiveSurface(false, "editor")).toBe("editor");
  });

  it("is always editor when a panel exists, whatever the setting says", () => {
    expect(deriveActiveSurface(true, "sidebar")).toBe("editor");
    expect(deriveActiveSurface(true, "editor")).toBe("editor");
  });
});

describe("sidebarRenderFor", () => {
  it("renders live only when there is no panel and the setting is sidebar", () => {
    expect(sidebarRenderFor(false, "sidebar")).toEqual({ kind: "live" });
  });

  it("shows the 'open in a tab' placeholder when a panel exists (setting: sidebar)", () => {
    expect(sidebarRenderFor(true, "sidebar")).toEqual({
      kind: "placeholder",
      message: "The dashboard is open in an editor tab.",
      action: SHOW_TAB_ACTION,
    });
  });

  it("shows the 'open in a tab' placeholder when a panel exists (setting: editor)", () => {
    expect(sidebarRenderFor(true, "editor")).toEqual({
      kind: "placeholder",
      message: "The dashboard is open in an editor tab.",
      action: SHOW_TAB_ACTION,
    });
  });

  it("shows the 'opens in a tab' placeholder when the setting is editor and no panel exists", () => {
    expect(sidebarRenderFor(false, "editor")).toEqual({
      kind: "placeholder",
      message: "The dashboard opens in an editor tab.",
      action: OPEN_TAB_ACTION,
    });
  });

  it("never renders live whenever a panel exists — the single-instance invariant", () => {
    for (const loc of ["sidebar", "editor"] as const) {
      expect(sidebarRenderFor(true, loc).kind).toBe("placeholder");
    }
  });

  it("both placeholder buttons invoke the open-in-editor command", () => {
    expect(SHOW_TAB_ACTION.command).toBe("claudeUsage.openInEditor");
    expect(OPEN_TAB_ACTION.command).toBe("claudeUsage.openInEditor");
    expect(SHOW_TAB_ACTION.label).toBe("Show tab");
    expect(OPEN_TAB_ACTION.label).toBe("Open editor tab");
  });
});

describe("shouldCollapseSidebar", () => {
  it("collapses only when enabled AND our view is the visible container", () => {
    expect(shouldCollapseSidebar(true, true)).toBe(true);
  });

  it("does not collapse when the setting is off", () => {
    expect(shouldCollapseSidebar(false, true)).toBe(false);
    expect(shouldCollapseSidebar(false, false)).toBe(false);
  });

  it("does not collapse when our view is hidden — closing would hit the Explorer instead", () => {
    expect(shouldCollapseSidebar(true, false)).toBe(false);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vscode-extension && npx vitest run test/open-target.test.ts`
Expected: FAIL — `Failed to resolve import "../src/open-target"`.

- [ ] **Step 3: Create `src/open-target.ts`**

```typescript
import type { WebviewAction } from "./webview-html";

/** Which surface hosts the dashboard. */
export type OpenTarget = "sidebar" | "editor";

/** What the sidebar should render right now. */
export type SidebarRender =
  | { kind: "live" }
  | { kind: "placeholder"; message: string; action: WebviewAction };

/** Shown when a panel is already open — focuses it. */
export const SHOW_TAB_ACTION: WebviewAction = {
  label: "Show tab",
  command: "claudeUsage.openInEditor",
};

/** Shown when the setting says editor but no panel exists yet — creates it. */
export const OPEN_TAB_ACTION: WebviewAction = {
  label: "Open editor tab",
  command: "claudeUsage.openInEditor",
};

/**
 * Normalize the raw `claudeUsage.openLocation` config value. VS Code delivers
 * exactly one of the declared enum strings, but settings.json is hand-editable,
 * so anything unrecognized falls back to the default rather than throwing.
 * Deliberately case-sensitive: "Editor" is a typo, not a synonym.
 */
export function resolveOpenTarget(value: unknown): OpenTarget {
  return value === "editor" ? "editor" : "sidebar";
}

/**
 * The active surface is DERIVED, never stored. An existing panel always wins,
 * which is what makes "exactly one live dashboard" true by construction rather
 * than by careful bookkeeping. Do not replace this with a mutable field.
 */
export function deriveActiveSurface(panelExists: boolean, openLocation: OpenTarget): OpenTarget {
  return panelExists ? "editor" : openLocation;
}

/**
 * The sidebar renders its live iframe only when it is the active surface;
 * otherwise it shows a placeholder pointing at the editor tab.
 */
export function sidebarRenderFor(panelExists: boolean, openLocation: OpenTarget): SidebarRender {
  if (panelExists) {
    return {
      kind: "placeholder",
      message: "The dashboard is open in an editor tab.",
      action: SHOW_TAB_ACTION,
    };
  }
  if (openLocation === "editor") {
    return {
      kind: "placeholder",
      message: "The dashboard opens in an editor tab.",
      action: OPEN_TAB_ACTION,
    };
  }
  return { kind: "live" };
}

/**
 * `workbench.action.closeSidebar` closes whichever container is showing, so we
 * only fire it when OUR view is the visible one — otherwise popping out while
 * the Explorer is open would close the Explorer.
 */
export function shouldCollapseSidebar(enabled: boolean, sidebarVisible: boolean): boolean {
  return enabled && sidebarVisible;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd vscode-extension && npx vitest run test/open-target.test.ts`
Expected: PASS, 17 tests.

- [ ] **Step 5: Commit**

```bash
git add vscode-extension/src/open-target.ts vscode-extension/test/open-target.test.ts
git commit -m "feat(vscode): add pure routing predicates for dashboard surface selection

Isolates the whole feature's decision table in a vscode-free module so it can
be unit-tested. deriveActiveSurface() encodes the single-instance invariant as
a derivation (panelExists ? editor : setting) rather than stored state."
```

---

### Task 3: Sidebar placeholder mode and visibility probe

**Files:**
- Modify: `vscode-extension/src/sidebar.ts` (`DashboardSidebar` class)
- Modify: `vscode-extension/test/sidebar.test.ts`

**Interfaces:**
- Consumes: `WebviewAction` from `src/webview-html.ts` (Task 1).
- Produces: `DashboardSidebar.setPlaceholder(message: string, action: WebviewAction): void` and `DashboardSidebar.isVisible(): boolean`.

`setPlaceholder` must clear `currentUrl` — otherwise the iframe branch of `renderHtml` wins and the placeholder never shows. `isVisible()` returns `false` when the view has never resolved or has been disposed.

- [ ] **Step 1: Write the failing test**

Append to `vscode-extension/test/sidebar.test.ts`. Also extend `makeFakeView()` to carry a settable `visible` flag — replace the existing helper with:

```typescript
function makeFakeView(visible = true) {
  let html = "";
  const disposeListeners: Array<() => void> = [];
  return {
    visible,
    webview: {
      get html() { return html; },
      set html(v: string) { html = v; },
      options: undefined as unknown,
    },
    onDidDispose(listener: () => void) {
      disposeListeners.push(listener);
      return { dispose: () => {} };
    },
    _triggerDispose() { disposeListeners.forEach((l) => l()); },
    _html: () => html,
  };
}
```

Then add:

```typescript
describe("DashboardSidebar placeholder mode", () => {
  const ACTION = { label: "Show tab", command: "claudeUsage.openInEditor" };

  it("renders the placeholder message and its action button", () => {
    const sidebar = new DashboardSidebar();
    const view = makeFakeView() as any;
    sidebar.resolveWebviewView(view);
    sidebar.setPlaceholder("The dashboard is open in an editor tab.", ACTION);
    expect(view._html()).toContain("The dashboard is open in an editor tab.");
    expect(view._html()).toContain('href="command:claudeUsage.openInEditor"');
    expect(view._html()).toContain("Show tab");
  });

  it("drops the iframe when switching from a live URL to a placeholder", () => {
    const sidebar = new DashboardSidebar();
    const view = makeFakeView() as any;
    sidebar.resolveWebviewView(view);
    sidebar.setUrl("http://127.0.0.1:9000/");
    expect(view._html()).toContain("<iframe");
    sidebar.setPlaceholder("The dashboard is open in an editor tab.", ACTION);
    expect(view._html()).not.toContain("<iframe");
  });

  it("drops the placeholder action when switching back to a live URL", () => {
    const sidebar = new DashboardSidebar();
    const view = makeFakeView() as any;
    sidebar.resolveWebviewView(view);
    sidebar.setPlaceholder("The dashboard is open in an editor tab.", ACTION);
    sidebar.setUrl("http://127.0.0.1:9000/");
    expect(view._html()).toContain("<iframe");
    expect(view._html()).not.toContain("claudeUsage.openInEditor");
  });

  it("does not throw when no view has resolved yet", () => {
    const sidebar = new DashboardSidebar();
    expect(() => sidebar.setPlaceholder("x", ACTION)).not.toThrow();
  });
});

describe("DashboardSidebar isVisible", () => {
  it("is false before any view resolves", () => {
    expect(new DashboardSidebar().isVisible()).toBe(false);
  });

  it("reflects the resolved view's visible flag", () => {
    const sidebar = new DashboardSidebar();
    sidebar.resolveWebviewView(makeFakeView(true) as any);
    expect(sidebar.isVisible()).toBe(true);
  });

  it("is false when the resolved view is hidden", () => {
    const sidebar = new DashboardSidebar();
    sidebar.resolveWebviewView(makeFakeView(false) as any);
    expect(sidebar.isVisible()).toBe(false);
  });

  it("is false again after the view is disposed", () => {
    const sidebar = new DashboardSidebar();
    const view = makeFakeView(true) as any;
    sidebar.resolveWebviewView(view);
    view._triggerDispose();
    expect(sidebar.isVisible()).toBe(false);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vscode-extension && npx vitest run test/sidebar.test.ts`
Expected: FAIL — `sidebar.setPlaceholder is not a function`.

- [ ] **Step 3: Add the two methods to `DashboardSidebar`**

Insert after `setError` in `src/sidebar.ts`:

```typescript
  /**
   * Render a non-error informational pane with a call-to-action — used when the
   * dashboard lives in an editor tab, so the sidebar explains where it went.
   * Clears currentUrl: the iframe branch of renderHtml would otherwise win.
   */
  setPlaceholder(message: string, action: WebviewAction): void {
    this.currentUrl = null;
    this.statusText = message;
    this.action = action;
    this.render();
  }

  /**
   * True only when our view is the visible sidebar container. Gates the
   * collapse-on-open-in-editor behavior so we never close someone else's panel.
   */
  isVisible(): boolean {
    return this.view?.visible ?? false;
  }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd vscode-extension && npm test && npx tsc -p . --noEmit`
Expected: all PASS, no TypeScript errors.

- [ ] **Step 5: Commit**

```bash
git add vscode-extension/src/sidebar.ts vscode-extension/test/sidebar.test.ts
git commit -m "feat(vscode): give the sidebar a placeholder mode and a visibility probe

setPlaceholder() renders the 'dashboard is in an editor tab' pane; isVisible()
gates sidebar collapse so we never close the Explorer by mistake."
```

---

### Task 4: `DashboardPanel` — the singleton editor tab

**Files:**
- Create: `vscode-extension/src/editor-panel.ts`
- Create: `vscode-extension/test/editor-panel.test.ts`

**Interfaces:**
- Consumes: `renderHtml`, `makeNonce`, `WebviewAction` from `src/webview-html.ts` (Task 1).
- Produces:
  - `DashboardPanel.viewType: string` (`"claudeUsage.dashboardPanel"`)
  - `static DashboardPanel.createOrShow(extensionUri: vscode.Uri, onDispose: () => void): DashboardPanel`
  - `static DashboardPanel.get(): DashboardPanel | undefined`
  - instance: `setUrl(url: string | null): void`, `setStatus(text: string): void`, `setError(text: string): void`, `refresh(): void`, `reveal(): void`, `dispose(): void`

Deliberately mirrors `DashboardSidebar`'s method names so `extension.ts` can treat both surfaces uniformly. Its retry action targets `claudeUsage.openInEditor`, not `claudeUsage.open`, so retrying from a tab reopens the tab.

- [ ] **Step 1: Write the failing test**

Create `vscode-extension/test/editor-panel.test.ts`. The `vi.mock` factory is hoisted above imports, so every helper it touches is declared inside `vi.hoisted`.

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";

const h = vi.hoisted(() => {
  const created: Array<{ args: unknown[]; panel: any }> = [];

  function makeFakePanel() {
    let html = "";
    const disposeListeners: Array<() => void> = [];
    return {
      webview: {
        get html() { return html; },
        set html(v: string) { html = v; },
        options: undefined as unknown,
      },
      iconPath: undefined as unknown,
      reveal: vi.fn(),
      dispose() { disposeListeners.forEach((l) => l()); },
      onDidDispose(listener: () => void) {
        disposeListeners.push(listener);
        return { dispose: () => {} };
      },
      _html: () => html,
    };
  }

  return { created, makeFakePanel };
});

vi.mock("vscode", () => ({
  ViewColumn: { Active: -1, Beside: -2, One: 1 },
  Uri: {
    joinPath: (base: unknown, ...parts: string[]) => ({
      toString: () => `${String((base as any)?.path ?? "ext")}/${parts.join("/")}`,
      path: `${String((base as any)?.path ?? "ext")}/${parts.join("/")}`,
    }),
  },
  window: {
    createWebviewPanel: (...args: unknown[]) => {
      const panel = h.makeFakePanel();
      h.created.push({ args, panel });
      return panel;
    },
  },
}));

import { DashboardPanel } from "../src/editor-panel";

const EXT_URI = { path: "/ext" } as any;

beforeEach(() => {
  DashboardPanel.get()?.dispose();
  h.created.length = 0;
});

describe("DashboardPanel.createOrShow", () => {
  it("creates a panel with the expected view type, title and column", () => {
    DashboardPanel.createOrShow(EXT_URI, () => {});
    expect(h.created).toHaveLength(1);
    const [viewType, title, column] = h.created[0].args as [string, string, number];
    expect(viewType).toBe("claudeUsage.dashboardPanel");
    expect(title).toBe("Claude Usage");
    expect(column).toBe(-1); // ViewColumn.Active
  });

  it("retains context when hidden so tab switches don't reload the dashboard", () => {
    DashboardPanel.createOrShow(EXT_URI, () => {});
    const options = h.created[0].args[3] as Record<string, unknown>;
    expect(options.retainContextWhenHidden).toBe(true);
    expect(options.enableScripts).toBe(true);
    expect(options.enableCommandUris).toBe(true);
  });

  it("is a singleton — a second call reveals instead of creating another panel", () => {
    const first = DashboardPanel.createOrShow(EXT_URI, () => {});
    const second = DashboardPanel.createOrShow(EXT_URI, () => {});
    expect(second).toBe(first);
    expect(h.created).toHaveLength(1);
    expect(h.created[0].panel.reveal).toHaveBeenCalledTimes(1);
  });

  it("exposes the live instance via get()", () => {
    const panel = DashboardPanel.createOrShow(EXT_URI, () => {});
    expect(DashboardPanel.get()).toBe(panel);
  });
});

describe("DashboardPanel disposal", () => {
  it("clears the singleton so the next createOrShow builds a fresh panel", () => {
    const first = DashboardPanel.createOrShow(EXT_URI, () => {});
    first.dispose();
    expect(DashboardPanel.get()).toBeUndefined();
    const second = DashboardPanel.createOrShow(EXT_URI, () => {});
    expect(second).not.toBe(first);
    expect(h.created).toHaveLength(2);
  });

  it("notifies the host via the onDispose callback", () => {
    const onDispose = vi.fn();
    DashboardPanel.createOrShow(EXT_URI, onDispose).dispose();
    expect(onDispose).toHaveBeenCalledTimes(1);
  });

  it("fires the same path when VS Code closes the tab itself", () => {
    const onDispose = vi.fn();
    DashboardPanel.createOrShow(EXT_URI, onDispose);
    h.created[0].panel.dispose(); // user clicked the tab's X
    expect(onDispose).toHaveBeenCalledTimes(1);
    expect(DashboardPanel.get()).toBeUndefined();
  });
});

describe("DashboardPanel rendering", () => {
  it("renders the status pane before any URL is set", () => {
    DashboardPanel.createOrShow(EXT_URI, () => {});
    expect(h.created[0].panel._html()).toContain("Claude Code Usage");
    expect(h.created[0].panel._html()).not.toContain("<iframe");
  });

  it("renders the iframe once a URL arrives", () => {
    const panel = DashboardPanel.createOrShow(EXT_URI, () => {});
    panel.setUrl("http://127.0.0.1:9000/");
    expect(h.created[0].panel._html()).toContain('src="http://127.0.0.1:9000/"');
  });

  it("shows a Retry button that reopens the TAB, not the sidebar", () => {
    const panel = DashboardPanel.createOrShow(EXT_URI, () => {});
    panel.setError("Failed to start dashboard: timed out");
    const html = h.created[0].panel._html();
    expect(html).toContain("Failed to start dashboard: timed out");
    expect(html).toContain('href="command:claudeUsage.openInEditor"');
    expect(html).not.toContain('href="command:claudeUsage.open"');
  });

  it("setStatus shows text with no action button", () => {
    const panel = DashboardPanel.createOrShow(EXT_URI, () => {});
    panel.setStatus("Starting dashboard at http://127.0.0.1:9000/…");
    const html = h.created[0].panel._html();
    expect(html).toContain("Starting dashboard");
    expect(html).not.toContain('class="retry"');
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd vscode-extension && npx vitest run test/editor-panel.test.ts`
Expected: FAIL — `Failed to resolve import "../src/editor-panel"`.

- [ ] **Step 3: Create `src/editor-panel.ts`**

```typescript
import * as vscode from "vscode";
import { renderHtml, makeNonce, WebviewAction } from "./webview-html";

/**
 * Retry button for the panel's error pane. Points at claudeUsage.openInEditor
 * rather than claudeUsage.open so retrying from a tab reopens the tab — with
 * claudeUsage.open and openLocation="sidebar" it would retry into the sidebar.
 */
const RETRY_ACTION: WebviewAction = { label: "↻ Retry", command: "claudeUsage.openInEditor" };

/**
 * The dashboard hosted in an editor tab. A module-level singleton: repeat opens
 * reveal the existing tab rather than stacking duplicates.
 *
 * Mirrors DashboardSidebar's method names (setUrl / setStatus / setError /
 * refresh) so extension.ts can drive either surface without branching on type.
 *
 * There is deliberately no WebviewPanelSerializer — the tab closes on window
 * reload and the user reopens it, which keeps VS Code from spawning Python at
 * every startup.
 */
export class DashboardPanel {
  public static readonly viewType = "claudeUsage.dashboardPanel";
  private static current: DashboardPanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private currentUrl: string | null = null;
  private statusText = "";
  private action: WebviewAction | undefined;
  private iconUri = "";
  private cspSource = "";

  /** The live panel, if one is open. Drives the derived active-surface rule. */
  static get(): DashboardPanel | undefined {
    return DashboardPanel.current;
  }

  /**
   * Reveal the existing panel, or create one. `onDispose` fires whenever the
   * tab goes away — whether the host called dispose() or the user closed it —
   * so the host can re-render the sidebar.
   */
  static createOrShow(extensionUri: vscode.Uri, onDispose: () => void): DashboardPanel {
    if (DashboardPanel.current) {
      DashboardPanel.current.reveal();
      return DashboardPanel.current;
    }
    const panel = vscode.window.createWebviewPanel(
      DashboardPanel.viewType,
      "Claude Usage",
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        // The status pane's action button uses a command: URI.
        enableCommandUris: true,
        // Keep the iframe alive across tab switches so filters, scroll position
        // and chart state survive; without it every switch back re-fetches.
        retainContextWhenHidden: true,
      },
    );
    DashboardPanel.current = new DashboardPanel(panel, extensionUri, onDispose);
    return DashboardPanel.current;
  }

  private constructor(panel: vscode.WebviewPanel, extensionUri: vscode.Uri, onDispose: () => void) {
    this.panel = panel;

    // Resolve the bundled icon for the tab and the status pane's brand header.
    // Guarded so node-only tests (whose fake panel has no asWebviewUri) don't blow up.
    if (typeof vscode.Uri?.joinPath === "function") {
      const icon = vscode.Uri.joinPath(extensionUri, "resources", "icon.svg");
      this.panel.iconPath = icon;
      if (typeof this.panel.webview.asWebviewUri === "function") {
        this.iconUri = this.panel.webview.asWebviewUri(icon).toString();
        this.cspSource = this.panel.webview.cspSource ?? "";
      }
    }

    this.panel.onDidDispose(() => {
      if (DashboardPanel.current === this) DashboardPanel.current = undefined;
      onDispose();
    });

    this.render();
  }

  /** Bring the tab to the front. */
  reveal(): void {
    this.panel.reveal();
  }

  /** Called from extension.ts after the server is ready. */
  setUrl(url: string | null): void {
    this.currentUrl = url;
    this.action = undefined;
    this.render();
  }

  /** Non-error status (initial / "starting…"): no action button. */
  setStatus(text: string): void {
    this.statusText = text;
    this.action = undefined;
    this.render();
  }

  /** A start attempt failed: show the status plus a Retry button. */
  setError(text: string): void {
    this.statusText = text;
    this.action = RETRY_ACTION;
    this.render();
  }

  /** Force the iframe to reload (e.g. after a rescan). */
  refresh(): void {
    this.render();
  }

  dispose(): void {
    this.panel.dispose();
  }

  private render(): void {
    this.panel.webview.html = renderHtml(
      this.currentUrl, this.statusText, makeNonce(), this.iconUri, this.cspSource, this.action,
    );
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd vscode-extension && npm test && npx tsc -p . --noEmit`
Expected: all PASS, no TypeScript errors.

- [ ] **Step 5: Commit**

```bash
git add vscode-extension/src/editor-panel.ts vscode-extension/test/editor-panel.test.ts
git commit -m "feat(vscode): add DashboardPanel, a singleton editor-tab host

Mirrors DashboardSidebar's API so the host can drive either surface uniformly.
retainContextWhenHidden keeps dashboard state across tab switches; no
serializer, so the tab closes on window reload by design."
```

---

### Task 5: Wire the host — split startup from reveal, add the setting and command

**Files:**
- Modify: `vscode-extension/src/extension.ts:1-207` (the `Extension` class)
- Modify: `vscode-extension/package.json:36-93` (`contributes`)

**Interfaces:**
- Consumes: `DashboardPanel` (Task 4); `resolveOpenTarget`, `deriveActiveSurface`, `sidebarRenderFor` (Task 2); `DashboardSidebar.setPlaceholder` (Task 3).
- Produces: no new exports. Registers command `claudeUsage.openInEditor`.

The core refactor: `openDashboard()` currently reveals the sidebar *then* starts the server, so the two are welded together. Split into `ensureServer()` (returns the URL, coalesced) and two reveal paths that consume it. **The `executeCommand("workbench.view.extension.claudeUsageSidebar")` call at line 59 must move into `showSidebar()` only** — if it stays on a shared path, opening a tab pops open the very sidebar the user is escaping.

- [ ] **Step 1: Add the manifest contributions**

In `vscode-extension/package.json`, add to `contributes.commands` (after the `claudeUsage.open` entry):

```json
      {
        "command": "claudeUsage.openInEditor",
        "title": "Claude Usage: Open Dashboard in Editor Tab",
        "icon": "$(link-external)"
      },
```

Add a new `contributes.menus` block (sibling of `commands`):

```json
    "menus": {
      "view/title": [
        {
          "command": "claudeUsage.openInEditor",
          "when": "view == claudeUsage.dashboard",
          "group": "navigation"
        }
      ]
    },
```

Add to `contributes.configuration.properties`, before `claudeUsage.pythonPath`:

```json
        "claudeUsage.openLocation": {
          "type": "string",
          "enum": ["sidebar", "editor"],
          "enumDescriptions": [
            "Show the dashboard in the activity-bar sidebar panel.",
            "Show the dashboard in an editor tab, which gives it the full window width."
          ],
          "default": "sidebar",
          "description": "Where to show the dashboard. 'editor' opens it as a tab in the editor area instead of the narrow sidebar."
        },
```

- [ ] **Step 2: Rewrite the `Extension` class in `src/extension.ts`**

Update the imports at the top of the file:

```typescript
import * as vscode from "vscode";
import * as path from "node:path";
import { locatePython } from "./python-locator";
import { resolveInstallMode, dashboardSpawnArgs, InstallMode } from "./install-mode";
import { resolveStablePort } from "./port-allocator";
import { ServerManager, OutputSink } from "./server-manager";
import { DashboardSidebar } from "./sidebar";
import { DashboardPanel } from "./editor-panel";
import { OpenTarget, resolveOpenTarget, deriveActiveSurface, sidebarRenderFor } from "./open-target";
```

Replace the class body (keep `LAST_PORT_KEY`, `describeMode`, `noInstallMessage`, `noPythonMessage`, `activate`, `deactivate` exactly as they are):

```typescript
class Extension {
  private context: vscode.ExtensionContext;
  private output: vscode.OutputChannel;
  private sidebar: DashboardSidebar;
  /** The editor tab, when one is open. Sole input to the derived active surface. */
  private panel: DashboardPanel | undefined;
  private server: ServerManager | undefined;
  /** URL of the ready server, so a second surface can attach without respawning. */
  private serverUrl: string | undefined;
  /** Last startup failure, replayed when a surface re-renders after the fact. */
  private lastError: string | undefined;
  /**
   * In-flight startup. Subsequent ensureServer() calls await this one
   * instead of spawning a second ServerManager. Cleared on resolve/reject.
   * Prevents the double-click orphaned-process race Codex flagged.
   */
  private startupInFlight: Promise<string | undefined> | undefined;

  constructor(context: vscode.ExtensionContext) {
    this.context = context;
    this.output = vscode.window.createOutputChannel("Claude Usage");
    // The sidebar invokes onShow when VS Code reveals the webview — that's
    // when the user clicked the activity-bar icon. What happens next depends
    // on which surface is active; onSidebarShown() decides.
    this.sidebar = new DashboardSidebar(() => {
      void this.onSidebarShown();
    }, context.extensionUri);

    context.subscriptions.push(
      this.output,
      vscode.window.registerWebviewViewProvider(DashboardSidebar.viewId, this.sidebar),
      vscode.commands.registerCommand("claudeUsage.open", () => this.openDashboard()),
      vscode.commands.registerCommand("claudeUsage.openInEditor", () => this.showEditorTab()),
      vscode.commands.registerCommand("claudeUsage.rescan", () => this.rescan()),
      vscode.commands.registerCommand("claudeUsage.restart", () => this.restart()),
      vscode.commands.registerCommand("claudeUsage.showLogs", () => this.output.show()),
      // A setting change only re-renders the sidebar. It never opens or closes
      // a tab behind the user's back; the derived rule keeps every combination
      // single-instance regardless.
      vscode.workspace.onDidChangeConfiguration((e) => {
        if (e.affectsConfiguration("claudeUsage.openLocation")) this.renderSidebar();
      }),
    );
  }

  /** Current value of claudeUsage.openLocation, normalized. */
  private openLocation(): OpenTarget {
    return resolveOpenTarget(vscode.workspace.getConfiguration("claudeUsage").get("openLocation"));
  }

  /** Which surface owns the dashboard right now. Derived, never stored. */
  private activeSurface(): OpenTarget {
    return deriveActiveSurface(this.panel !== undefined, this.openLocation());
  }

  /** The `claudeUsage.open` command: route by setting. */
  async openDashboard(): Promise<void> {
    if (this.openLocation() === "editor") {
      await this.showEditorTab();
      return;
    }
    await this.showSidebar();
  }

  /** Reveal the activity-bar container, then let onSidebarShown do the work. */
  private async showSidebar(): Promise<void> {
    await vscode.commands.executeCommand("workbench.view.extension.claudeUsageSidebar");
    // If the view was already resolved, revealing it fires no onShow — so drive
    // the same path explicitly. ensureServer()'s coalescing makes the possible
    // duplicate call free.
    await this.onSidebarShown();
  }

  /**
   * The user revealed the sidebar. If the editor tab owns the dashboard, hand
   * off to it; otherwise render live here and make sure the server is up.
   */
  private async onSidebarShown(): Promise<void> {
    if (this.activeSurface() === "editor") {
      await this.showEditorTab();
      return;
    }
    this.renderSidebar();
    const url = await this.ensureServer();
    if (url && !this.panel) this.sidebar.setUrl(url);
  }

  /** Create or focus the editor tab and point it at the server. */
  private async showEditorTab(): Promise<void> {
    this.panel = DashboardPanel.createOrShow(this.context.extensionUri, () => {
      this.panel = undefined;
      this.renderSidebar();
    });
    // The sidebar is no longer the active surface — swap it to a placeholder
    // before we start waiting on the server.
    this.renderSidebar();
    this.maybeCollapseSidebar();
    const url = await this.ensureServer();
    // The user can close the tab during a slow cold start, so re-check.
    if (url) this.panel?.setUrl(url);
  }

  /** Apply the derived render table to the sidebar. */
  private renderSidebar(): void {
    const decision = sidebarRenderFor(this.panel !== undefined, this.openLocation());
    if (decision.kind === "placeholder") {
      this.sidebar.setPlaceholder(decision.message, decision.action);
      return;
    }
    if (this.serverUrl) {
      this.sidebar.setUrl(this.serverUrl);
      return;
    }
    if (this.lastError) {
      this.sidebar.setError(this.lastError);
      return;
    }
    this.sidebar.setStatus("");
  }

  /**
   * Collapse the sidebar after opening a tab, if the user asked for that.
   * Task 6 declares the setting and swaps this condition for the shared
   * shouldCollapseSidebar() predicate; inlined here so this task compiles alone.
   */
  private maybeCollapseSidebar(): void {
    const enabled = vscode.workspace
      .getConfiguration("claudeUsage")
      .get<boolean>("collapseSidebarOnOpenInEditor", false);
    if (!enabled || !this.sidebar.isVisible()) return;
    void vscode.commands.executeCommand("workbench.action.closeSidebar");
  }

  /**
   * Make sure the dashboard server is running and return its URL, or undefined
   * if startup failed. Coalesces concurrent callers onto one spawn so two
   * surfaces opening at once can't produce two Python processes.
   */
  private async ensureServer(): Promise<string | undefined> {
    if (this.server && this.server.status === "ready" && this.serverUrl) {
      return this.serverUrl;
    }
    if (this.startupInFlight) {
      return this.startupInFlight;
    }
    this.startupInFlight = this.doStartup().finally(() => {
      this.startupInFlight = undefined;
    });
    return this.startupInFlight;
  }

  /** Push a non-error status to whichever surface the user is looking at. */
  private broadcastStatus(text: string): void {
    if (this.activeSurface() === "editor") this.panel?.setStatus(text);
    else this.sidebar.setStatus(text);
  }

  /** Record and show a startup failure on whichever surface is active. */
  private broadcastError(msg: string): void {
    this.lastError = msg;
    if (this.activeSurface() === "editor") this.panel?.setError(msg);
    else this.sidebar.setError(msg);
  }

  private async doStartup(): Promise<string | undefined> {
    const config = vscode.workspace.getConfiguration("claudeUsage");
    const configuredPython = config.get<string>("pythonPath", "");
    const configuredCli = config.get<string>("cliPath", "");
    // Hardcoded to localhost. We previously exposed a `host` setting but
    // 0.0.0.0 would have made the user's usage data visible on the LAN.
    // The Python dashboard accepts HOST/PORT env vars directly if someone
    // really needs to bind elsewhere; that's an out-of-extension config.
    const host = "127.0.0.1";
    const configuredPort = config.get<number>("port", 0);

    const workspaceFolders = (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
    const extensionDir = this.context.extensionUri.fsPath;
    // Bundled python sources live at <extensionDir>/python/cli.py — copied
    // there from the repo root by scripts/copy-python.js at package time.
    const bundledCliPath = path.join(extensionDir, "python", "cli.py");
    const mode = resolveInstallMode({
      configuredCliPath: configuredCli,
      bundledCliPath,
      extensionDir,
      workspaceFolders,
    });
    if (mode.kind === "none") {
      const msg = noInstallMessage();
      this.output.appendLine(msg);
      this.broadcastError(msg);
      vscode.window.showErrorMessage(msg);
      return undefined;
    }

    const python = mode.kind === "clone" ? locatePython(configuredPython) : undefined;
    if (mode.kind === "clone" && !python) {
      const msg = noPythonMessage();
      this.output.appendLine(msg);
      this.broadcastError(msg);
      vscode.window.showErrorMessage(
        "Claude Usage needs Python 3.8+ on PATH. See the dashboard panel for install links.",
      );
      return undefined;
    }

    // Reuse the last port when it's still free so the embedded dashboard's
    // localStorage (which is keyed by the iframe's http://host:port origin)
    // persists across window reloads instead of resetting every launch.
    const savedPort = this.context.workspaceState.get<number>(LAST_PORT_KEY);
    const port = await resolveStablePort(configuredPort, savedPort, host);
    void this.context.workspaceState.update(LAST_PORT_KEY, port);
    const url = `http://${host}:${port}/`;
    // Probe a dashboard-specific endpoint so we don't get fooled by some
    // other localhost service listening on the same port.
    const probeUrl = `http://${host}:${port}/api/data`;
    // --no-browser: the dashboard is embedded in the webview, so the bundled
    // cli.py must not also pop a system browser (it does by default for CLI users).
    // --surface vscode: tells the dashboard it's embedded so its footer shows the
    // version only — no "get the extension" promo (we're already in it) and no
    // GitHub update check (VS Code updates the extension itself).
    const spawnArgs = dashboardSpawnArgs(mode, python, ["--no-browser", "--host", host, "--port", String(port), "--surface", "vscode"]);
    if (!spawnArgs) {
      const msg = "Could not assemble a valid command to spawn the dashboard.";
      this.output.appendLine(msg);
      this.broadcastError(msg);
      return undefined;
    }

    this.broadcastStatus(`Starting dashboard at ${url}…`);
    this.output.appendLine(`[ext] install mode: ${describeMode(mode)}`);
    // Capture the manager into a local so the catch block can't dispose
    // a *different* manager that was created by a concurrent call.
    const manager = new ServerManager({
      command: spawnArgs.command,
      args: spawnArgs.args,
      url: probeUrl,
      output: this.toSink(),
      // Cold start can be slow the first time: spawning Python, opening the DB,
      // and (once, after upgrade) backfilling session topics across the whole
      // history before /api/data answers. Give it 20s before giving up.
      readinessTimeoutMs: 20_000,
    });
    this.server = manager;
    try {
      await manager.start();
      this.serverUrl = url;
      this.lastError = undefined;
      return url;
    } catch (err) {
      const msg = `Failed to start dashboard: ${(err as Error).message}`;
      this.output.appendLine(msg);
      this.broadcastError(msg);
      manager.dispose();
      if (this.server === manager) this.server = undefined;
      this.serverUrl = undefined;
      // Offer a one-click retry (and log access) rather than making the user
      // hunt for the command palette. Both surfaces also show a Retry button.
      void vscode.window.showErrorMessage(msg, "Retry", "Show Logs").then((choice) => {
        if (choice === "Retry") void this.openDashboard();
        else if (choice === "Show Logs") this.output.show();
      });
      return undefined;
    }
  }

  /**
   * Trigger a rescan against the running server, then refresh the active
   * surface. Currently just refreshes — the existing Python dashboard has a
   * Rescan button inside the UI; this is a placeholder for future host-driven
   * rescan if we add a POST endpoint dedicated to it.
   */
  rescan(): void {
    if (this.activeSurface() === "editor") this.panel?.refresh();
    else this.sidebar.refresh();
  }

  async restart(): Promise<void> {
    // If a startup is in flight, wait for it to settle so we don't dispose a
    // manager mid-spawn and leave an orphaned Python process.
    if (this.startupInFlight) {
      try { await this.startupInFlight; } catch { /* ignored — about to restart */ }
    }
    if (this.server) {
      this.server.dispose();
      this.server = undefined;
    }
    this.serverUrl = undefined;
    this.lastError = undefined;
    this.sidebar.setUrl(null);
    this.panel?.setUrl(null);
    // Restart into whichever surface currently owns the dashboard.
    if (this.activeSurface() === "editor") await this.showEditorTab();
    else await this.showSidebar();
  }

  dispose(): void {
    this.panel?.dispose();
    this.panel = undefined;
    if (this.server) {
      this.server.dispose();
      this.server = undefined;
    }
  }

  private toSink(): OutputSink {
    return { appendLine: (line) => this.output.appendLine(line) };
  }
}
```

- [ ] **Step 3: Verify it compiles and the suite still passes**

Run: `cd vscode-extension && npx tsc -p . --noEmit && npm test`
Expected: no TypeScript errors; all existing tests PASS (this task adds no new tests — `extension.ts` needs a full vscode host, which is what the Task 7 manual checklist covers).

- [ ] **Step 4: Smoke-test in the Extension Development Host**

Run: open `vscode-extension/` in VS Code and press F5. In the dev host:
1. Click the Claude Usage activity-bar icon → the dashboard loads in the sidebar as before.
2. Command palette → `Claude Usage: Open Dashboard in Editor Tab` → a tab opens with the dashboard, and the sidebar switches to `The dashboard is open in an editor tab.` with a **Show tab** button.
3. Close the tab → the sidebar goes back to the live iframe.
4. Set `"claudeUsage.openLocation": "editor"` in settings → click the activity-bar icon → a tab opens.
5. Click the `$(link-external)` button in the sidebar's title bar → a tab opens.

Expected: all five behave as described. If step 2 also pops the sidebar container open, the line-59 reveal was left on a shared path — move it into `showSidebar()`.

- [ ] **Step 5: Commit**

```bash
git add vscode-extension/src/extension.ts vscode-extension/package.json
git commit -m "feat(vscode): add claudeUsage.openLocation and the open-in-editor command

Splits openDashboard() into ensureServer() plus two reveal paths, so starting
the Python server is no longer welded to revealing the sidebar. The sidebar
container reveal now lives only in showSidebar() — leaving it on the shared
path would pop open the very panel the user is escaping.

The active surface is derived (panelExists ? editor : setting), so exactly one
dashboard instance is live in every combination of setting and tab state."
```

---

### Task 6: Optional sidebar collapse after opening the tab

**Files:**
- Modify: `vscode-extension/src/extension.ts` (`maybeCollapseSidebar`, imports)
- Modify: `vscode-extension/package.json` (`contributes.configuration.properties`)

**Interfaces:**
- Consumes: `shouldCollapseSidebar` from `src/open-target.ts` (Task 2); `DashboardSidebar.isVisible()` (Task 3).
- Produces: nothing new.

`shouldCollapseSidebar` and its unit tests already exist from Task 2. This task wires it up and declares the setting.

- [ ] **Step 1: Declare the setting**

Add to `contributes.configuration.properties` in `vscode-extension/package.json`, directly after `claudeUsage.openLocation`:

```json
        "claudeUsage.collapseSidebarOnOpenInEditor": {
          "type": "boolean",
          "default": false,
          "description": "After the dashboard opens in an editor tab, collapse the sidebar so the tab gets the full window width. Only collapses when the Claude Usage view is the one currently showing."
        },
```

- [ ] **Step 2: Use the shared predicate in `extension.ts`**

Extend the `open-target` import:

```typescript
import {
  OpenTarget,
  resolveOpenTarget,
  deriveActiveSurface,
  sidebarRenderFor,
  shouldCollapseSidebar,
} from "./open-target";
```

Replace the inlined condition from Task 5 with the predicate:

```typescript
  /** Collapse the sidebar after opening a tab, if the user asked for that. */
  private maybeCollapseSidebar(): void {
    const enabled = vscode.workspace
      .getConfiguration("claudeUsage")
      .get<boolean>("collapseSidebarOnOpenInEditor", false);
    // The visibility guard matters: workbench.action.closeSidebar closes
    // whichever container is showing, so without it, popping out while the
    // Explorer is open would close the Explorer.
    if (!shouldCollapseSidebar(enabled, this.sidebar.isVisible())) return;
    void vscode.commands.executeCommand("workbench.action.closeSidebar");
  }
```

- [ ] **Step 3: Verify compile and tests**

Run: `cd vscode-extension && npx tsc -p . --noEmit && npm test`
Expected: no TypeScript errors; all tests PASS.

- [ ] **Step 4: Verify the guard in the Extension Development Host**

Run: F5, then in the dev host set `"claudeUsage.collapseSidebarOnOpenInEditor": true`.
1. With the Claude Usage sidebar showing, run `Claude Usage: Open Dashboard in Editor Tab` → tab opens **and the sidebar collapses**.
2. Switch the sidebar to the Explorer, then run the same command → tab opens and **the Explorer stays open**.

Expected: both. Step 2 failing means the `isVisible()` guard isn't wired.

- [ ] **Step 5: Commit**

```bash
git add vscode-extension/src/extension.ts vscode-extension/package.json
git commit -m "feat(vscode): add claudeUsage.collapseSidebarOnOpenInEditor

Guarded on the Claude Usage view actually being the visible container, since
workbench.action.closeSidebar closes whatever is showing — without the guard,
popping out with the Explorer open would close the Explorer."
```

---

### Task 7: Docs, version bump, and full verification

**Files:**
- Modify: `vscode-extension/README.md:95-105` (settings table) and add a usage section
- Modify: `scanner.py:18` (`VERSION`)
- Modify: `vscode-extension/package.json:5` (`version`)
- Modify: `CHANGELOG.md:1-3` (new top heading)

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: a releasable v1.6.0.

`tests/test_version.py` asserts all three version sites agree, so this task's Python test run is the real gate.

- [ ] **Step 1: Add the CHANGELOG entry**

Insert directly below the `# Changelog` heading in `CHANGELOG.md`, above `## v1.5.5`:

```markdown
## v1.6.0 — TBD

### VS Code extension

- Added **`claudeUsage.openLocation`** (`sidebar` | `editor`, default `sidebar`) to host the dashboard in an editor tab instead of the narrow activity-bar panel, giving the charts and tables the full window width they were designed for.
- Added the **`Claude Usage: Open Dashboard in Editor Tab`** command and a matching button in the sidebar's title bar, so the dashboard can be popped out on demand without changing the setting.
- Added **`claudeUsage.collapseSidebarOnOpenInEditor`** (default `false`) to collapse the sidebar once the tab is up. Only collapses when the Claude Usage view is the one showing, so it never closes the Explorer.
- Exactly one dashboard instance is live at a time: the sidebar shows a short placeholder pointing at the tab whenever the tab owns the dashboard. The editor tab keeps its state across tab switches and closes on window reload.
```

- [ ] **Step 2: Bump all three version sites**

`scanner.py` line 18:

```python
VERSION = "1.6.0"
```

`vscode-extension/package.json` line 5:

```json
  "version": "1.6.0",
```

- [ ] **Step 3: Run the Python suite to confirm version parity**

Run: `python3 -m unittest discover -s tests -v`
Expected: all PASS. A failure in `TestVersion.test_matches_changelog_heading` or `test_matches_package_json` means one of the three sites was missed.

- [ ] **Step 4: Update the extension README**

In the settings table at `vscode-extension/README.md:98`, add two rows above `claudeUsage.pythonPath`:

```markdown
| `claudeUsage.openLocation` | `sidebar` | Where the dashboard opens: `sidebar` (activity-bar panel) or `editor` (a tab in the editor area, full window width). |
| `claudeUsage.collapseSidebarOnOpenInEditor` | `false` | Collapse the sidebar once the dashboard opens in an editor tab. Only collapses when the Claude Usage view is the one showing. |
```

Then add this section immediately after the settings table:

```markdown
### Open the dashboard in an editor tab

The sidebar is narrow, and the dashboard was designed for a full browser
window. To give it more room:

- **Once:** run `Claude Usage: Open Dashboard in Editor Tab` from the command
  palette, or click the pop-out button in the sidebar's title bar.
- **Always:** set `"claudeUsage.openLocation": "editor"`. The activity-bar icon
  and `Claude Usage: Open Dashboard` then both open the tab.

Only one dashboard runs at a time. While the tab is open, the sidebar shows a
short note with a **Show tab** button instead of a second copy. Close the tab
and the sidebar takes over again.

The tab keeps its state when you switch to another editor tab, and closes when
you reload the window.
```

- [ ] **Step 5: Run the complete verification suite**

Run:

```bash
cd vscode-extension && npm test && npx tsc -p . --noEmit && cd .. && python3 -m unittest discover -s tests -v
```

Expected: extension tests PASS, no TypeScript errors, Python tests PASS.

- [ ] **Step 6: Walk the full manual checklist in the Extension Development Host**

Run: F5 from `vscode-extension/`, then confirm each item:

1. Default settings: activity-bar icon → live sidebar iframe, as before.
2. `Claude Usage: Open Dashboard in Editor Tab` → tab opens; sidebar shows `The dashboard is open in an editor tab.` + **Show tab**.
3. Close the tab → sidebar reclaims the live iframe.
4. `"claudeUsage.openLocation": "editor"` → activity-bar icon opens the tab; sidebar shows `The dashboard opens in an editor tab.` + **Open editor tab**.
5. `"claudeUsage.collapseSidebarOnOpenInEditor": true` → sidebar collapses after the tab opens.
6. Explorer visible instead of Claude Usage → pop out → Explorer stays open.
7. Sidebar title-bar pop-out button opens the tab.
8. Switch to another editor tab and back → dashboard filters and scroll position are retained.
9. Reload the window → the tab is gone (expected; no serializer) and no Python process lingers.
10. Dashboard renders correctly at full editor width — charts and tables use the space.
11. `Claude Usage: Restart Server` with the tab open → the tab reloads, not the sidebar.
12. Flip `openLocation` back to `sidebar` while the tab is open → the tab stays open and the sidebar keeps its placeholder (still one instance).

- [ ] **Step 7: Commit**

```bash
git add CHANGELOG.md scanner.py vscode-extension/package.json vscode-extension/README.md
git commit -m "docs: document the editor-tab dashboard and bump to v1.6.0

New user-visible feature, so a minor bump. scanner.VERSION, the CHANGELOG
heading and package.json move together — tests/test_version.py enforces it.
The heading stays TBD until the maintainer merges DEV to main."
```

---

## Notes for the implementer

- **Do not reintroduce a stored active-surface flag.** `deriveActiveSurface` exists so single-instance is a property of the code shape, not of remembering to update a field in six places.
- **Do not re-export the render helpers from `sidebar.ts`.** One home per symbol; `test/sidebar.test.ts` imports what it needs directly.
- **The `↻` in `"↻ Retry"` is literal Unicode**, not the `&#8635;` entity the old template used. It passes through `escapeHtml` untouched, which is why the whole label can be escaped without a raw-HTML escape hatch.
- `CHANGELOG.md`'s `TBD` is intentional and stays until release. Do not substitute a date.
