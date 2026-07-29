import * as vscode from "vscode";
import * as path from "node:path";
import { locatePython } from "./python-locator";
import { resolveInstallMode, dashboardSpawnArgs, InstallMode } from "./install-mode";
import { resolveStablePort } from "./port-allocator";
import { ServerManager, OutputSink } from "./server-manager";
import { DashboardSidebar } from "./sidebar";
import { DashboardPanel } from "./editor-panel";
import {
  OpenTarget,
  resolveOpenTarget,
  deriveActiveSurface,
  sidebarRenderFor,
  shouldCollapseSidebar,
} from "./open-target";

/**
 * workspaceState key holding the last port the dashboard bound to. Reused on the
 * next launch so the iframe origin (and thus its localStorage: collapsed-section
 * state + the update-check cache) survives window reloads. Per-workspace so two
 * windows don't fight over one port.
 */
const LAST_PORT_KEY = "claudeUsage.lastPort";

/**
 * Lifecycle owner for the extension. Held as a module-level singleton so
 * deactivate() can find it.
 */
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

  /** The `claudeUsage.open` command: route by the derived active surface. */
  async openDashboard(): Promise<void> {
    if (this.activeSurface() === "editor") {
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
   * The user revealed the sidebar. Only hand off to the editor tab when the
   * *setting* says editor — checking activeSurface() here would also hand off
   * whenever a panel merely happens to exist (e.g. openLocation: "sidebar"
   * with the dashboard popped out), which would steal focus from whatever
   * file the user was on via DashboardPanel.reveal(). With openLocation:
   * "sidebar", falling through to renderSidebar() resolves to the "Show tab"
   * placeholder instead — see the spec's state table.
   */
  private async onSidebarShown(): Promise<void> {
    if (this.openLocation() === "editor") {
      await this.showEditorTab();
      return;
    }
    this.renderSidebar();
    const url = await this.ensureServer();
    // Re-derive rather than only guarding the sidebar: a panel may exist here
    // (openLocation: "sidebar" with the dashboard popped out), and if its own
    // startup had failed, this attempt is the one that succeeds. Handing the
    // URL to whichever surface is live keeps the tab from sitting on a stale
    // error pane over a running server.
    if (url) {
      if (this.panel) this.panel.setUrl(url);
      else this.sidebar.setUrl(url);
    }
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
    // The user can close the tab during a slow cold start, so re-derive which
    // surface should receive the result instead of assuming the panel survived.
    if (url) {
      if (this.panel) this.panel.setUrl(url);
      else this.renderSidebar();
    }
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

  /**
   * Push a non-error status to whichever surface actually exists. Checking
   * `this.panel` directly (rather than activeSurface()) matters when the
   * setting says "editor" but no tab has been created yet — activeSurface()
   * would still say "editor" and a `this.panel?.` call would silently no-op,
   * leaving the sidebar (the only surface that exists) without the update.
   */
  private broadcastStatus(text: string): void {
    if (this.panel) this.panel.setStatus(text);
    else this.sidebar.setStatus(text);
  }

  /**
   * Record and show a startup failure on whichever surface actually exists
   * (see broadcastStatus() for why that's `this.panel`, not activeSurface()).
   * Also clears serverUrl: every doStartup() exit that calls this means the
   * server is not usable, and renderSidebar() trusts serverUrl over lastError,
   * so a stale URL here would hide the error behind a dead iframe.
   */
  private broadcastError(msg: string): void {
    this.lastError = msg;
    this.serverUrl = undefined;
    if (this.panel) this.panel.setError(msg);
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

    // Everything from here on can fail asynchronously (resolveStablePort probes
    // a socket and can reject; manager.start() can reject) — one try/catch
    // covers both so a rejection always routes through broadcastError() and
    // resolves to undefined, matching this method's declared return type,
    // instead of escaping as an unhandled rejection out of ensureServer().
    let manager: ServerManager | undefined;
    try {
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
        vscode.window.showErrorMessage(msg);
        return undefined;
      }

      this.broadcastStatus(`Starting dashboard at ${url}…`);
      this.output.appendLine(`[ext] install mode: ${describeMode(mode)}`);
      // Capture the manager into a local so the catch block can't dispose
      // a *different* manager that was created by a concurrent call.
      manager = new ServerManager({
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
      await manager.start();
      this.serverUrl = url;
      this.lastError = undefined;
      return url;
    } catch (err) {
      const msg = `Failed to start dashboard: ${(err as Error).message}`;
      this.output.appendLine(msg);
      this.broadcastError(msg);
      if (manager) {
        manager.dispose();
        if (this.server === manager) this.server = undefined;
      }
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
    // Checking `this.panel` directly (rather than activeSurface()) matters
    // when the setting says "editor" but no tab has been created yet — see
    // broadcastStatus() for why activeSurface() would be wrong here too.
    if (this.panel) this.panel.refresh();
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

function describeMode(mode: InstallMode): string {
  if (mode.kind === "brew") return `brew (${mode.binary})`;
  if (mode.kind === "clone") return `clone (${mode.cliPy})`;
  return "none";
}

/**
 * Friendly "no install found" message. With the bundled Python sources this
 * should be virtually unreachable in a packaged extension — only fires when
 * the user explicitly sets claudeUsage.cliPath to a path that doesn't exist
 * AND no PATH/workspace/sibling fallback succeeds.
 */
export function noInstallMessage(): string {
  return [
    "Could not find a claude-usage install. This is unexpected for a marketplace install.",
    "Check your claudeUsage.cliPath setting (clear it to fall back to the bundled sources),",
    "and use Claude Usage: Show Logs to see what was tried.",
  ].join("\n");
}

/**
 * Friendly "no Python found" message. This is the most likely failure for
 * a fresh marketplace install on a machine without Python — common on
 * Windows. Points the user at python.org with concrete next steps.
 */
export function noPythonMessage(platform: NodeJS.Platform = process.platform): string {
  const installHint =
    platform === "win32"
      ? "Install Python 3.8+ from https://www.python.org/downloads/windows/ (make sure to check 'Add Python to PATH' during install)."
      : platform === "darwin"
      ? "Install Python 3.8+ with: brew install python  (or from https://www.python.org/downloads/macos/)."
      : "Install Python 3.8+ via your distro's package manager (e.g. apt install python3).";
  return [
    "Claude Usage needs Python 3.8 or newer on your PATH.",
    "",
    installHint,
    "",
    "After installing, reload this VS Code window (Cmd/Ctrl+Shift+P → Developer: Reload Window).",
    "If Python is already installed in a non-standard location, set claudeUsage.pythonPath in settings.",
  ].join("\n");
}

let extension: Extension | undefined;

export function activate(context: vscode.ExtensionContext): void {
  extension = new Extension(context);
}

export function deactivate(): void {
  extension?.dispose();
  extension = undefined;
}
