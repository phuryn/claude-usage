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

describe("DashboardPanel after disposal", () => {
  // A stale reference can outlive the tab: e.g. `const panel = createOrShow(...);
  // await server.start(); panel.setUrl(url)` — if the user closes the tab while
  // the await is pending, the later calls must no-op rather than throw (real
  // VS Code's WebviewPanel throws on .reveal() / .webview.html once disposed).
  it("setUrl, setStatus, setError, refresh and reveal do not throw once disposed", () => {
    const panel = DashboardPanel.createOrShow(EXT_URI, () => {});
    panel.dispose();

    expect(() => panel.setUrl("http://127.0.0.1:9000/")).not.toThrow();
    expect(() => panel.setStatus("Starting…")).not.toThrow();
    expect(() => panel.setError("Failed to start dashboard")).not.toThrow();
    expect(() => panel.refresh()).not.toThrow();
    expect(() => panel.reveal()).not.toThrow();
  });

  it("does not re-render the webview html after disposal", () => {
    const panel = DashboardPanel.createOrShow(EXT_URI, () => {});
    const fakePanel = h.created[0].panel;
    const htmlBeforeDispose = fakePanel._html();
    panel.dispose();

    panel.setUrl("http://127.0.0.1:9000/");
    panel.setStatus("Starting…");
    panel.setError("Failed to start dashboard");
    panel.refresh();

    expect(fakePanel._html()).toBe(htmlBeforeDispose);
  });

  it("does not call the underlying panel's reveal() again after disposal", () => {
    const panel = DashboardPanel.createOrShow(EXT_URI, () => {});
    const fakePanel = h.created[0].panel;
    panel.dispose();

    panel.reveal();

    expect(fakePanel.reveal).not.toHaveBeenCalled();
  });

  it("dispose() is idempotent — calling it again does not re-invoke the underlying panel's dispose", () => {
    const onDispose = vi.fn();
    const panel = DashboardPanel.createOrShow(EXT_URI, onDispose);
    panel.dispose();
    expect(onDispose).toHaveBeenCalledTimes(1);

    expect(() => panel.dispose()).not.toThrow();
    expect(onDispose).toHaveBeenCalledTimes(1);
  });
});
