import { describe, it, expect, vi } from "vitest";
import { DashboardSidebar } from "../src/sidebar";

// Minimal vscode mock so we can instantiate DashboardSidebar in node-only tests.
vi.mock("vscode", () => ({}), { virtual: true });

function makeFakeView(visible = true) {
  let html = "";
  const disposeListeners: Array<() => void> = [];
  return {
    visible,
    webview: {
      get html() { return html; },
      set html(v: string) { html = v; },
      options: undefined as unknown,
    },
    onDidDispose(listener: () => void) {
      disposeListeners.push(listener);
      return { dispose: () => {} };
    },
    _triggerDispose() { disposeListeners.forEach((l) => l()); },
    _html: () => html,
  };
}

describe("DashboardSidebar onShow auto-start", () => {
  it("invokes the onShow callback when resolveWebviewView is called", () => {
    const onShow = vi.fn();
    const sidebar = new DashboardSidebar(onShow);
    const fakeView = makeFakeView() as any;
    sidebar.resolveWebviewView(fakeView);
    expect(onShow).toHaveBeenCalledTimes(1);
  });

  it("doesn't throw without a callback (default no-op)", () => {
    const sidebar = new DashboardSidebar();
    const fakeView = makeFakeView() as any;
    expect(() => sidebar.resolveWebviewView(fakeView)).not.toThrow();
  });

  it("renders HTML into the webview on resolve", () => {
    const sidebar = new DashboardSidebar();
    const fakeView = makeFakeView() as any;
    sidebar.resolveWebviewView(fakeView);
    expect(fakeView._html()).toContain("<html");
  });

  it("re-fires onShow on every resolveWebviewView (e.g. user collapses+reopens)", () => {
    const onShow = vi.fn();
    const sidebar = new DashboardSidebar(onShow);
    const fakeView1 = makeFakeView() as any;
    sidebar.resolveWebviewView(fakeView1);
    fakeView1._triggerDispose();
    const fakeView2 = makeFakeView() as any;
    sidebar.resolveWebviewView(fakeView2);
    expect(onShow).toHaveBeenCalledTimes(2);
  });
});

describe("DashboardSidebar placeholder mode", () => {
  const ACTION = { label: "Show tab", command: "claudeUsage.openInEditor" };

  it("renders the placeholder message and its action button", () => {
    const sidebar = new DashboardSidebar();
    const view = makeFakeView() as any;
    sidebar.resolveWebviewView(view);
    sidebar.setPlaceholder("The dashboard is open in an editor tab.", ACTION);
    expect(view._html()).toContain("The dashboard is open in an editor tab.");
    expect(view._html()).toContain('href="command:claudeUsage.openInEditor"');
    expect(view._html()).toContain("Show tab");
  });

  it("drops the iframe when switching from a live URL to a placeholder", () => {
    const sidebar = new DashboardSidebar();
    const view = makeFakeView() as any;
    sidebar.resolveWebviewView(view);
    sidebar.setUrl("http://127.0.0.1:9000/");
    expect(view._html()).toContain("<iframe");
    sidebar.setPlaceholder("The dashboard is open in an editor tab.", ACTION);
    expect(view._html()).not.toContain("<iframe");
  });

  it("drops the placeholder action when switching back to a live URL", () => {
    const sidebar = new DashboardSidebar();
    const view = makeFakeView() as any;
    sidebar.resolveWebviewView(view);
    sidebar.setPlaceholder("The dashboard is open in an editor tab.", ACTION);
    sidebar.setUrl("http://127.0.0.1:9000/");
    expect(view._html()).toContain("<iframe");
    expect(view._html()).not.toContain("claudeUsage.openInEditor");
  });

  it("does not throw when no view has resolved yet", () => {
    const sidebar = new DashboardSidebar();
    expect(() => sidebar.setPlaceholder("x", ACTION)).not.toThrow();
  });
});

describe("DashboardSidebar isVisible", () => {
  it("is false before any view resolves", () => {
    expect(new DashboardSidebar().isVisible()).toBe(false);
  });

  it("reflects the resolved view's visible flag", () => {
    const sidebar = new DashboardSidebar();
    sidebar.resolveWebviewView(makeFakeView(true) as any);
    expect(sidebar.isVisible()).toBe(true);
  });

  it("is false when the resolved view is hidden", () => {
    const sidebar = new DashboardSidebar();
    sidebar.resolveWebviewView(makeFakeView(false) as any);
    expect(sidebar.isVisible()).toBe(false);
  });

  it("is false again after the view is disposed", () => {
    const sidebar = new DashboardSidebar();
    const view = makeFakeView(true) as any;
    sidebar.resolveWebviewView(view);
    view._triggerDispose();
    expect(sidebar.isVisible()).toBe(false);
  });
});
