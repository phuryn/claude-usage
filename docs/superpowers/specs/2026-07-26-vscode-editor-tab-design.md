# Open the dashboard as an editor tab

**Date:** 2026-07-26
**Branch:** `ashinn/open-in-workspace-tab`
**Status:** Approved, ready for implementation planning

## Problem

The VS Code extension only renders the dashboard in the primary sidebar, as a
`WebviewViewProvider` registered against the `claudeUsageSidebar` activity-bar
container. The sidebar is narrow, and the dashboard SPA was designed for a full
browser window — charts and tables are cramped there. Users need the option to
host the dashboard in an editor tab, which is closer to the layout the SPA was
built for.

## Goals

- A setting that chooses which surface hosts the dashboard.
- A command that opens the editor tab on demand, even when the setting says
  `sidebar`.
- Exactly one live dashboard instance at a time.
- No change to how or when the Python server starts.

## Non-goals

- Restoring the editor tab across window reloads (no `WebviewPanelSerializer`).
- Eager/warm server start at VS Code launch.
- Any CSS or layout work inside the Python dashboard.
- Multiple simultaneous dashboard instances.

## Background: what exists today

- `DashboardSidebar` (`src/sidebar.ts`) implements `vscode.WebviewViewProvider`
  for view id `claudeUsage.dashboard`. It renders an iframe pointing at the
  local server, or a status/error pane when there is no URL yet.
- `renderHtml(url, statusText, nonce, iconUri, cspSource, showRetry)` is already
  a pure function, so it is reusable by a second surface unchanged.
- `Extension.openDashboard()` (`src/extension.ts`) reveals the sidebar container,
  then resolves Python + install mode + port, spawns a `ServerManager`, and
  points the sidebar at the resulting URL. Concurrent calls are coalesced onto a
  single `startupInFlight` promise.
- There is one `ServerManager`, one port, one URL. The port is stashed in
  `workspaceState` and reused when free, so the iframe origin — and therefore
  the dashboard's `localStorage` — is stable across reloads.

## Server lifecycle (unchanged)

`activationEvents` is `[]`; VS Code auto-generates activation from the
contributed view and commands. `activate()` only constructs the `Extension` — it
does **not** spawn Python. The server starts lazily, on the first surface that
opens, via a shared coalesced `ensureServer()`. Opening the tab first starts the
server; revealing the sidebar afterward reuses the same process and URL.

Because both surfaces iframe the same `http://127.0.0.1:<port>/` origin, the
dashboard's `localStorage` (collapsed sections, the 24h update-check cache)
carries over when the user moves between surfaces.

## Settings

| Setting | Type | Default | Description |
|---|---|---|---|
| `claudeUsage.openLocation` | enum `"sidebar"` \| `"editor"` | `"sidebar"` | Which surface hosts the dashboard. |
| `claudeUsage.collapseSidebarOnOpenInEditor` | boolean | `false` | Collapse the primary sidebar once the editor tab is up. |

`collapseSidebarOnOpenInEditor` fires on **any** path that opens the editor tab,
not only the explicit pop-out command.

## Command and menu

- `claudeUsage.openInEditor` — title *"Claude Usage: Open Dashboard in Editor
  Tab"*. Always opens the tab, regardless of `openLocation`.
- Contributed to `menus.view/title` with icon `$(link-external)`, group
  `navigation`, `when: view == claudeUsage.dashboard`, so the pop-out is one
  click from the sidebar's title bar.

## Core rule: the active surface is derived, not stored

```
activeSurface = panelExists ? "editor" : openLocation
```

There is no mutable `activeSurface` field. Single-instance is guaranteed by
construction: the sidebar renders its live iframe only when no panel exists
*and* the setting is `sidebar`. In every other state it renders a placeholder.

| `openLocation` | Panel open? | Sidebar renders | Activity-bar icon |
|---|---|---|---|
| `sidebar` | no | live iframe | reveals sidebar (today's behavior) |
| `sidebar` | yes | placeholder + **[Show tab]** | reveals placeholder |
| `editor` | no | placeholder + **[Open editor tab]** | reveals, then auto-opens the tab |
| `editor` | yes | placeholder + **[Show tab]** | reveals, then focuses the tab |

Exact placeholder copy:

| Condition | Message | Action button |
|---|---|---|
| Panel open | `The dashboard is open in an editor tab.` | `Show tab` → `claudeUsage.openInEditor` |
| No panel, `openLocation: editor` | `The dashboard opens in an editor tab.` | `Open editor tab` → `claudeUsage.openInEditor` |

Both placeholders reuse the existing null-URL branch of `renderHtml` — the same
brand header, message paragraph, and action button already used by the status
and error panes. `setPlaceholder` is not a new template, just a new caller.

Consequences that fall out for free:

- Close the tab while `openLocation: sidebar` → the sidebar reclaims the live
  iframe on its next render.
- Close the tab while `openLocation: editor` → placeholder; clicking the icon
  reopens the tab.

## Routing

| Trigger | `openLocation: sidebar` | `openLocation: editor` |
|---|---|---|
| Activity-bar icon (sidebar `onShow`) | ensure server, render live iframe | open/focus the editor tab |
| `claudeUsage.open` | reveal sidebar | open/focus the editor tab |
| `claudeUsage.openInEditor` | open/focus the editor tab | open/focus the editor tab |

## Configuration changes

An `onDidChangeConfiguration` listener for `claudeUsage.openLocation` re-renders
the sidebar and nothing else. It never proactively opens or closes a tab.
Flipping the setting while a tab is open leaves the tab alone; the derived rule
keeps the result single-instance in every combination.

## Sidebar collapse

When `collapseSidebarOnOpenInEditor` is true and the extension opens the editor
tab, run `workbench.action.closeSidebar` — but **only** when the Claude Usage
view is the currently visible sidebar container, checked via
`WebviewView.visible`. That command closes whichever container is showing, so
without the guard, popping out while the Explorer is open would close the
Explorer.

Side effect worth keeping: with `openLocation: editor` and collapse enabled,
clicking the activity-bar icon opens the tab and collapses the sidebar in one
gesture, yielding a full-width dashboard.

## Editor panel behavior

- Singleton. Repeat opens call `reveal()` rather than creating a second panel.
- `vscode.ViewColumn.Active` — opening `Beside` would split the group and
  re-cramp the dashboard, defeating the purpose.
- `retainContextWhenHidden: true`, so switching to another tab and back does not
  reload the iframe or reset filters and scroll position.
- `enableScripts: true`, `enableCommandUris: true` (the status pane's action
  button uses a `command:` URI).
- `iconPath` from `resources/icon.svg`.
- No `WebviewPanelSerializer`: a window reload closes the tab and nothing
  restarts. Reopening is a cold start, which the existing 20s readiness timeout
  already accommodates.

## Module structure

### New

- **`src/webview-html.ts`** — `renderHtml`, `escapeHtml`, `makeNonce` move here
  from `sidebar.ts`, since both surfaces render identical HTML.
  One signature change: the `showRetry: boolean` parameter becomes
  `action?: { label: string; command: string }`. This covers *Retry* →
  `claudeUsage.open`, *Show tab* → `claudeUsage.openInEditor`, and *Open editor
  tab* → `claudeUsage.openInEditor`. It also fixes a latent bug: the status pane
  currently hardcodes `command:claudeUsage.open`, so a Retry click from an
  editor tab under `openLocation: sidebar` would retry into the sidebar.
- **`src/open-target.ts`** — `resolveOpenTarget(value: unknown): "sidebar" |
  "editor"`, mapping anything unrecognized to `"sidebar"`. Imports no vscode
  API, so it is directly unit-testable.
- **`src/editor-panel.ts`** — `DashboardPanel`, a module-level singleton
  wrapping `vscode.WebviewPanel`. Mirrors the sidebar's API (`setUrl`,
  `setStatus`, `setError`, `refresh`, `reveal`, `dispose`) so the host can treat
  both surfaces uniformly. `onDidDispose` clears the singleton and notifies the
  host to re-render the sidebar.

### Changed

- **`src/sidebar.ts`** — gains `setPlaceholder(message, action)` and
  `isVisible()`. The render *decision* lives in the host; the sidebar stays dumb
  and renders what it is told. It imports the render helpers from
  `webview-html.ts` but does **not** re-export them for backward compatibility;
  `test/sidebar.test.ts` updates its imports instead, so there is one home per
  symbol.
- **`src/extension.ts`** — `openDashboard()` splits into:
  - `ensureServer(): Promise<string | undefined>` — all Python / install-mode /
    port / spawn logic, keeping the existing `startupInFlight` coalescing.
    Returns the URL, or `undefined` on failure after pushing the error to the
    active surface.
  - `showSidebar()` and `showEditorTab()` — thin reveal paths that both consume
    `ensureServer()`. The unconditional
    `executeCommand("workbench.view.extension.claudeUsageSidebar")` that
    currently opens `openDashboard()` moves into `showSidebar()` only;
    `showEditorTab()` must never reveal the sidebar container, or opening a tab
    would pop open the very panel the user is trying to escape.
  - `renderSidebar()` — applies the derived-state table above.
  - `openDashboard()` — routes via `resolveOpenTarget`.
  - `onDidChangeConfiguration` handler for `claudeUsage.openLocation`.
  - `restart()` and `rescan()` target the active surface.
  - `dispose()` disposes the panel as well as the server.

## Manifest

- Two `configuration.properties` entries.
- One `commands` entry.
- One `menus.view/title` entry.

## Versioning

A user-visible, non-breaking feature, so a **minor bump to v1.6.0**.
`tests/test_version.py` asserts that `scanner.VERSION`, the top `## vX.Y.Z`
CHANGELOG heading, and `vscode-extension/package.json`'s `version` all agree, so
all three move together:

- `scanner.py` → `VERSION = "1.6.0"`
- `vscode-extension/package.json` → `"version": "1.6.0"`
- `CHANGELOG.md` → `## v1.6.0 — TBD` with a `### VS Code extension` subsection

The `TBD` stays until the maintainer merges to `main`.

## Documentation

- Settings table in `vscode-extension/README.md` gains both new rows.
- A short "Open in an editor tab" section in the same README covering the
  setting, the command, and the title-bar button.
- The root `README.md` does not enumerate extension settings, so it needs no
  change.

## Testing

### Unit (vitest, `vscode` stubbed via `vi.mock("vscode", factory)`)

- **`test/open-target.test.ts`** (new) — `"sidebar"` and `"editor"` pass
  through; empty string, `undefined`, and unrecognized values fall back to
  `"sidebar"`.
- **`test/webview-html.test.ts`** (new) — the existing `renderHtml`,
  `escapeHtml`, and `makeNonce` tests relocate here, plus coverage for the
  parameterized action button (arbitrary label and command id) and the
  placeholder render.
- **`test/editor-panel.test.ts`** (new) — requires a richer `vscode` mock
  exposing `window.createWebviewPanel`, `ViewColumn`, and `Uri.joinPath`.
  Asserts: a second `createOrShow` reveals rather than creating a second panel;
  `retainContextWhenHidden: true` is passed; dispose clears the singleton so the
  next call creates a fresh panel; `setUrl` writes iframe HTML.
- **`test/sidebar.test.ts`** — keeps the `DashboardSidebar` tests, drops the
  relocated render tests, adds placeholder mode and `isVisible()`.

### Manual (F5 Extension Development Host)

Vitest cannot exercise real VS Code routing, so the implementation plan carries
an explicit checklist:

1. Default settings: activity-bar icon → live sidebar iframe, as today.
2. `claudeUsage.openInEditor` from the palette → tab opens, sidebar switches to
   the **[Show tab]** placeholder.
3. Close the tab → sidebar reclaims the live iframe.
4. Set `openLocation: editor` → activity-bar icon opens the tab; sidebar shows
   the placeholder.
5. Enable `collapseSidebarOnOpenInEditor` → sidebar collapses after the tab
   opens.
6. With the Explorer visible instead, pop out → the Explorer stays open.
7. Sidebar title-bar `$(link-external)` button opens the tab.
8. Switch to another editor tab and back → dashboard state is retained.
9. Reload the window → the tab is gone (expected; no serializer).
10. Confirm the dashboard renders correctly at full editor width.

## Risks and open items

- **Single-instance invariant** depends entirely on the derived rule. Any future
  code that stores an active-surface flag reintroduces the drift this design
  avoids.
- **Panel tests need a heavier `vscode` mock** than the existing suite uses.
  If the mock becomes unwieldy, the fallback is to keep `DashboardPanel` thin
  and push assertions into `webview-html.ts`, which needs no mock at all.
- **Cold start after reload** is the accepted cost of skipping the serializer.
