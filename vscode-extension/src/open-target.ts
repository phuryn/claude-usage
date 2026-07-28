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
