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
});
