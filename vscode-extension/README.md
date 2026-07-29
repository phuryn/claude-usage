# Claude Code Usage — VS Code extension

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE.txt)

**See your Claude Code usage — tokens, costs, sessions, projects — right inside VS Code.**

![Daily usage view](https://raw.githubusercontent.com/phuryn/claude-usage/main/docs/usage1.png)

The extension reads your local Claude Code JSONL transcripts (the ones Claude Code already writes regardless of plan) and renders the same dashboard the Python tool ships, embedded as a VS Code sidebar. No API calls, no telemetry — all data stays on your machine.

Works on **API, Pro, and Max plans**. Captures usage from the Claude Code CLI, the official VS Code extension, and dispatched Code sessions.

---

## What it shows

- **Daily token usage** and **average hourly distribution** charts (with peak-hour shading)
- **Cost by model, project, and project + branch** tables, plus **Recent Sessions** — sortable, paged, and CSV-exportable
- **Subagent attribution** — a Subagent Tokens by Type chart and a Top Subagent Dispatches table that break dispatched Task/Agent usage out from your main sessions
- **Model multi-select** and a **date-range** dropdown to scope everything at once
- A sticky **section nav** for jumping between sections, and **collapsible** chart/table cards that remember what you've folded away across reloads

Cost estimates use Anthropic's published API pricing (actual Max/Pro costs differ).

---

## Install

### From the VS Code Marketplace

Search the Extensions sidebar for **"Claude Code Usage"** (publisher: `PawelHuryn`), or open it directly:

[**Install from the VS Code Marketplace →**](https://marketplace.visualstudio.com/items?itemName=PawelHuryn.claude-usage-phuryn)

[**See in Open VSX Registry →**](https://open-vsx.org/extension/PawelHuryn/claude-usage-phuryn)

### From a prebuilt `.vsix` (no build step)

Every [GitHub Release](https://github.com/phuryn/claude-usage/releases/latest) attaches a ready-built `.vsix`. Download it, then either drag it onto the VS Code **Extensions** view, or run:

```
code --install-extension claude-usage-phuryn-<version>.vsix
```

### Build and install from source

Clone the repo and run the install script for your platform. Each script **builds** the `.vsix` (`npm install` + `vsce package`) and then installs it via `code --install-extension` — you don't need an existing `.vsix`, the script produces one.

**macOS / Linux / WSL** (bash):

```bash
git clone https://github.com/phuryn/claude-usage
cd claude-usage/vscode-extension
./scripts/install.sh
```

**Windows** — run the script *in PowerShell*. Invoking `.\scripts\install.ps1` from Git Bash (or double-clicking it) just opens the file in an editor, because Windows maps `.ps1` to "Edit", not "Run". The line below runs it regardless of which shell you're in or your execution-policy setting:

```powershell
git clone https://github.com/phuryn/claude-usage
cd claude-usage/vscode-extension
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

---

## Requirements

- **Python 3.8 or newer on your `PATH`.** Almost everyone running Claude Code already has Python installed; if not, see [python.org/downloads](https://www.python.org/downloads/). On Windows make sure to check **"Add Python to PATH"** during the installer.
Optional, not required: on **VS Code 1.110 or newer** the editor tab gets a theme-colored icon, which needs `ThemeIcon` support on webview panels ([microsoft/vscode#90616](https://github.com/microsoft/vscode/issues/90616)). On older builds the tab simply appears without an icon.

That's the only dependency. The Python sources (`cli.py`, `scanner.py`, `dashboard.py`) are bundled inside the extension — no separate clone or Homebrew install needed.

---

## Usage

1. Click the **gauge icon** in the activity bar (left sidebar of VS Code).
2. The extension starts the dashboard server on a free local port and embeds it in a sidebar webview, or in an editor tab if you set `claudeUsage.openLocation` to `editor`.
3. Filter by model, range, or project — same UI as the standalone web dashboard.

![Hourly + project breakdown](https://raw.githubusercontent.com/phuryn/claude-usage/main/docs/usage2.png)

### Commands

Open the Command Palette (`Ctrl+Shift+P` / `Cmd+Shift+P`):

| Command | What it does |
|---|---|
| **Claude Usage: Open Dashboard** | Open the dashboard on whichever surface owns it and start the server. The sidebar by default, or the editor tab if one is already open or `openLocation` is `editor` |
| **Claude Usage: Open Dashboard in Editor Tab** | Open (or focus) the dashboard in an editor tab, regardless of the `openLocation` setting |
| **Claude Usage: Rescan Transcripts** | Refresh the iframe; the dashboard's own Rescan button runs an incremental scan that adds new usage without touching existing history |
| **Claude Usage: Restart Server** | Kill and respawn the Python process (use after changing settings) |
| **Claude Usage: Show Logs** | Open the extension's output channel — useful when something doesn't work |

### Settings

| Setting | Default | Description |
|---|---|---|
| `claudeUsage.openLocation` | `sidebar` | Where the dashboard opens: `sidebar` (activity-bar panel) or `editor` (a tab in the editor area, full window width). |
| `claudeUsage.collapseSidebarOnOpenInEditor` | `false` | Collapse the sidebar whenever the dashboard opens in an editor tab, via **Claude Usage: Open Dashboard in Editor Tab** (and its title-bar button), **Claude Usage: Open Dashboard**, **Claude Usage: Restart Server**, or the activity-bar icon when `openLocation` is `editor`. Only collapses while the Claude Usage view is the one currently showing in the sidebar, so it never closes the Explorer. |
| `claudeUsage.pythonPath` | _(auto-discover)_ | Path to a Python 3.8+ interpreter. Leave empty to auto-detect (`claude-usage` on PATH first, then `python3`, then `python`). |
| `claudeUsage.cliPath` | _(bundled)_ | Path to a custom `cli.py` (or its parent directory). Empty = use the bundled copy that ships with the extension. |
| `claudeUsage.port` | `0` | Port for the local dashboard server. `0` = OS picks a free one. |

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

---

## How discovery works

When you click the icon, the extension resolves how to run the dashboard in this order:

1. **`claudeUsage.cliPath` setting** if you've set one
2. **The bundled `python/cli.py`** that ships inside this `.vsix` (most installs hit this)
3. The `claude-usage` shim on `PATH` (if you installed via Homebrew)
4. A `cli.py` in any open VS Code workspace folder (the legacy "open the cloned repo" path)
5. A sibling `cli.py` from the extension dir (dev mode, when running from source via F5)

If none of those find anything, you'll get a friendly message in the sidebar — most often "Python 3.8+ is required" with a platform-specific install hint.

---

## Privacy

The extension only:
- Reads local JSONL transcripts from `~/.claude/projects/` (and the Xcode coding-assistant directory on macOS, if present)
- Runs a small HTTP server bound to `127.0.0.1` (localhost-only — never `0.0.0.0`) on a port the OS picks for you
- Embeds that server's dashboard in a VS Code webview

No data leaves your machine. No API calls. No telemetry.

---

## Troubleshooting

- **"Python 3.8 or newer required"** — install from [python.org](https://www.python.org/downloads/) and reload VS Code (`Ctrl+Shift+P` → `Developer: Reload Window`). On Windows make sure "Add Python to PATH" is checked in the installer.
- **Sidebar stays blank or shows "starting…"** — run `Claude Usage: Show Logs`. The extension logs the resolved Python path, the install mode, the spawn command, and any stdout/stderr from the server.
- **Dashboard renders but shows "No usage recorded"** — Claude Code hasn't written transcripts to `~/.claude/projects/` yet. Run a Claude Code session first.

---

## Source

The Python tool, this extension, and a Homebrew formula all live at [github.com/phuryn/claude-usage](https://github.com/phuryn/claude-usage). Bug reports and feature discussions: [Issues](https://github.com/phuryn/claude-usage/issues), [Discussions](https://github.com/phuryn/claude-usage/discussions).

**Made by** [The Product Compass Newsletter](https://www.productcompass.pm).
