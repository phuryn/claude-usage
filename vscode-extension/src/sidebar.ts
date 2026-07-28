import * as vscode from "vscode";
import { renderHtml, makeNonce, WebviewAction } from "./webview-html";

/**
 * Retry button for the sidebar's error pane. Routes through claudeUsage.open
 * so retrying honors whatever claudeUsage.openLocation currently says.
 */
const RETRY_ACTION: WebviewAction = { label: "↻ Retry", command: "claudeUsage.open" };

export class DashboardSidebar implements vscode.WebviewViewProvider {
  public static readonly viewId = "claudeUsage.dashboard";

  private view: vscode.WebviewView | undefined;
  private currentUrl: string | null = null;
  private statusText = "";
  // The action button shown in the status pane, if any. Set by setError()
  // (Retry) and setPlaceholder(); cleared by setUrl() and setStatus().
  private action: WebviewAction | undefined;
  private readonly onShow: () => void;
  private readonly extensionUri: vscode.Uri | undefined;
  private iconUri = "";
  private cspSource = "";

  constructor(onShow: () => void = () => {}, extensionUri?: vscode.Uri) {
    this.onShow = onShow;
    this.extensionUri = extensionUri;
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    // enableCommandUris lets the status pane's "Retry" link invoke
    // claudeUsage.open via a command: URI. The pane renders only our own
    // trusted HTML (statusText is escaped), so allowing command URIs is safe.
    view.webview.options = { enableScripts: true, enableCommandUris: true };
    // Resolve a webview-safe URI for the bundled icon so the status pane shows
    // the same logo as the dashboard header. Guarded so node-only tests (whose
    // fake view has no asWebviewUri / no vscode.Uri) don't blow up.
    if (this.extensionUri && typeof view.webview.asWebviewUri === "function") {
      this.iconUri = view.webview
        .asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "resources", "icon.svg"))
        .toString();
      this.cspSource = view.webview.cspSource ?? "";
    }
    this.render();
    view.onDidDispose(() => {
      this.view = undefined;
    });
    // Kick the host to start the server now that the user has revealed the
    // panel. extension.ts wires this to openDashboard(); the in-flight
    // coalescing on that side means clicking the icon repeatedly is safe.
    this.onShow();
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

  /** Force the iframe to reload (e.g. after a rescan). */
  refresh(): void {
    // Re-render same URL — the iframe will reload because the HTML is regenerated.
    // render() itself no-ops when there is no view.
    this.render();
  }

  private render(): void {
    if (!this.view) return;
    this.view.webview.html = renderHtml(
      this.currentUrl, this.statusText, makeNonce(), this.iconUri, this.cspSource, this.action,
    );
  }
}
