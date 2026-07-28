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
  it("collapses on an explicit invocation when enabled AND our view is the visible container", () => {
    expect(shouldCollapseSidebar(true, true, false, "sidebar")).toBe(true);
    expect(shouldCollapseSidebar(true, true, false, "editor")).toBe(true);
  });

  it("does not collapse when the setting is off", () => {
    expect(shouldCollapseSidebar(false, true, false, "sidebar")).toBe(false);
    expect(shouldCollapseSidebar(false, false, false, "sidebar")).toBe(false);
  });

  it("does not collapse when our view is hidden — closing would hit the Explorer instead", () => {
    expect(shouldCollapseSidebar(true, false, false, "sidebar")).toBe(false);
  });

  it("on the sidebar-reveal handoff, collapses only when openLocation is editor", () => {
    expect(shouldCollapseSidebar(true, true, true, "editor")).toBe(true);
    expect(shouldCollapseSidebar(true, true, true, "sidebar")).toBe(false);
  });

  it("the handoff gate does not override the enabled/visible checks", () => {
    expect(shouldCollapseSidebar(false, true, true, "editor")).toBe(false);
    expect(shouldCollapseSidebar(true, false, true, "editor")).toBe(false);
  });
});
