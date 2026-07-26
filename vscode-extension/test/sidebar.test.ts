import { describe, it, expect, vi } from "vitest";
import { DashboardSidebar } from "../src/sidebar";

// Minimal vscode mock so we can instantiate DashboardSidebar in node-only tests.
vi.mock("vscode", () => ({}), { virtual: true });

function makeFakeView() {
  let html = "";
  const disposeListeners: Array<() => void> = [];
  return {
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
