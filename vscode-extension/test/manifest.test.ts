import { describe, it, expect, vi } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

// sidebar.ts imports vscode at module load; stub it so we can import viewId
// without pulling in the real API (same pattern as test/sidebar.test.ts).
vi.mock("vscode", () => ({}), { virtual: true });

// Static JSON.parse of the manifest — no vscode API needed here, but the
// DashboardSidebar import below requires the stub above regardless.
const manifestPath = path.join(__dirname, "..", "package.json");
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));

import { DashboardSidebar } from "../src/sidebar";
import { TAB_ICON_ID } from "../src/editor-panel";

describe("package.json manifest", () => {
  it("declares claudeUsage.openInEditor with the exact title and icon", () => {
    const command = manifest.contributes.commands.find(
      (c: { command: string }) => c.command === "claudeUsage.openInEditor",
    );
    expect(command).toBeDefined();
    expect(command.title).toBe("Claude Usage: Open Dashboard in Editor Tab");
    expect(command.icon).toBe("$(link-external)");
  });

  it("wires the view/title menu entry to openInEditor and the sidebar's real view id", () => {
    const menuEntry = manifest.contributes.menus["view/title"].find(
      (m: { command: string }) => m.command === "claudeUsage.openInEditor",
    );
    expect(menuEntry).toBeDefined();
    expect(menuEntry.when).toBe(`view == ${DashboardSidebar.viewId}`);
  });

  it("declares claudeUsage.openLocation with the sidebar/editor enum and default sidebar", () => {
    const prop = manifest.contributes.configuration.properties["claudeUsage.openLocation"];
    expect(prop).toBeDefined();
    expect(prop.enum).toEqual(["sidebar", "editor"]);
    expect(prop.default).toBe("sidebar");
  });

  it("declares claudeUsage.collapseSidebarOnOpenInEditor as boolean, default false", () => {
    const prop = manifest.contributes.configuration.properties["claudeUsage.collapseSidebarOnOpenInEditor"];
    expect(prop).toBeDefined();
    expect(prop.type).toBe("boolean");
    expect(prop.default).toBe(false);
  });

  it("registers the tab icon glyph under the id editor-panel.ts references", () => {
    const icon = manifest.contributes.icons[TAB_ICON_ID];
    expect(icon).toBeDefined();
    expect(icon.default.fontCharacter).toBe("\\E001");
  });

  it("ships the icon font the glyph points at", () => {
    const fontPath = manifest.contributes.icons[TAB_ICON_ID].default.fontPath;
    const resolved = path.join(__dirname, "..", fontPath);
    expect(fs.existsSync(resolved)).toBe(true);
    // WOFF magic number: 'wOFF'. Catches a truncated or mis-generated build.
    expect(fs.readFileSync(resolved).subarray(0, 4).toString("latin1")).toBe("wOFF");
  });

  it("declares an engines floor new enough for ThemeIcon on WebviewPanel.iconPath", () => {
    // ThemeIcon support for WebviewPanel.iconPath landed in VS Code 1.110
    // (microsoft/vscode#90616). Below that the tab icon silently disappears.
    const floor = manifest.engines.vscode.replace(/^[^\d]*/, "");
    const [major, minor] = floor.split(".").map(Number);
    expect(major * 1000 + minor).toBeGreaterThanOrEqual(1 * 1000 + 110);
  });
});
