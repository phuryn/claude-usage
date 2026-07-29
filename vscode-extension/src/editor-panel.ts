import * as vscode from "vscode";
import { makeNonce, renderHtml } from "./webview-html";
import type { WebviewAction } from "./webview-html";

/**
 * Retry button for the panel's error pane. Points at claudeUsage.openInEditor
 * rather than claudeUsage.open so retrying from a tab reopens the tab. With
 * claudeUsage.open and openLocation="sidebar" it would retry into the sidebar.
 */
const RETRY_ACTION: WebviewAction = { label: "↻ Retry", command: "claudeUsage.openInEditor" };

/**
 * Icon id registered in package.json's `contributes.icons`. Referenced as a
 * ThemeIcon so the tab icon follows the theme's icon.foreground token.
 * Exported so test/manifest.test.ts can assert the manifest and the code agree.
 */
export const TAB_ICON_ID = "claude-usage";

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
  // Set once the panel is disposed (host-initiated or user closed the tab).
  // Guards the public surface so a stale reference held across an await
  // (e.g. `const panel = createOrShow(...); await server.start(); panel.setUrl(...)`)
  // no-ops instead of throwing — VS Code's real WebviewPanel throws on
  // .reveal() / .webview.html once disposed.
  private disposed = false;

  /** Accessor for the current panel, if one is open. Used only by tests. */
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

    // The tab icon is a ThemeIcon referencing our `contributes.icons` glyph, so
    // VS Code paints it with the active theme's `icon.foreground`. An SVG passed
    // as a Uri is drawn as a plain image and keeps whatever ink it was authored
    // with, which goes invisible on roughly half the themes.
    if (typeof vscode.ThemeIcon === "function") {
      this.panel.iconPath = new vscode.ThemeIcon(TAB_ICON_ID);
    }

    // The status pane's brand header is a different mechanism: it applies the
    // SVG as a CSS mask over a colored box, which does recolor, so it keeps
    // using a webview Uri. Guarded so node-only tests (whose fake panel has no
    // asWebviewUri) don't blow up.
    if (typeof vscode.Uri?.joinPath === "function"
        && typeof this.panel.webview.asWebviewUri === "function") {
      const icon = vscode.Uri.joinPath(extensionUri, "resources", "icon.svg");
      this.iconUri = this.panel.webview.asWebviewUri(icon).toString();
      this.cspSource = this.panel.webview.cspSource ?? "";
    }

    this.panel.onDidDispose(() => {
      this.disposed = true;
      if (DashboardPanel.current === this) DashboardPanel.current = undefined;
      onDispose();
    });

    this.render();
  }

  /** Bring the tab to the front. No-op once the panel is disposed. */
  reveal(): void {
    if (this.disposed) return;
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

  /** Force the iframe to reload (e.g. after a rescan). No-op once disposed. */
  refresh(): void {
    this.render();
  }

  /** Idempotent: disposing an already-disposed panel is a safe no-op. */
  dispose(): void {
    if (this.disposed) return;
    this.panel.dispose();
  }

  private render(): void {
    if (this.disposed) return;
    this.panel.webview.html = renderHtml(
      this.currentUrl, this.statusText, makeNonce(), this.iconUri, this.cspSource, this.action,
    );
  }
}
