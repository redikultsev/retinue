// The service worker caches the app shell only: /api/* is never answered by it (not even intercepted).
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

function load() {
  const listeners: Record<string, (e: unknown) => void> = {};
  const self = {
    location: { origin: "https://app.example" },
    addEventListener: (kind: string, fn: (e: unknown) => void) => {
      listeners[kind] = fn;
    },
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn() },
  };
  const cache = { match: vi.fn().mockResolvedValue(undefined), put: vi.fn(), addAll: vi.fn(), keys: vi.fn().mockResolvedValue([]), delete: vi.fn() };
  const caches = { open: vi.fn().mockResolvedValue(cache), match: vi.fn().mockResolvedValue(undefined), keys: vi.fn().mockResolvedValue([]), delete: vi.fn() };
  const source = readFileSync(resolve(process.cwd(), "public/sw.js"), "utf8");
  runInNewContext(source, { self, caches, fetch: vi.fn().mockResolvedValue(new Response("ok")), URL, Response, Promise });
  const fetchEvent = (url: string, init: { method?: string; mode?: string } = {}) => {
    const respondWith = vi.fn();
    listeners.fetch!({ request: { url, method: init.method ?? "GET", mode: init.mode ?? "cors" }, respondWith });
    return respondWith;
  };
  return { fetchEvent };
}

describe("service worker", () => {
  it("never answers /api/*", () => {
    const { fetchEvent } = load();
    expect(fetchEvent("https://app.example/api/sessions")).not.toHaveBeenCalled();
    expect(fetchEvent("https://app.example/api/events?since=1")).not.toHaveBeenCalled();
    expect(fetchEvent("https://app.example/api/approvals/x/decide", { method: "POST" })).not.toHaveBeenCalled();
  });

  it("answers navigations and hashed assets (the app shell)", () => {
    const { fetchEvent } = load();
    expect(fetchEvent("https://app.example/s/a1b2c3d4", { mode: "navigate" })).toHaveBeenCalled();
    expect(fetchEvent("https://app.example/assets/index-abc.js")).toHaveBeenCalled();
    expect(fetchEvent("https://elsewhere.example/x.js")).not.toHaveBeenCalled();
  });

  it("keeps only app routes as the shell, never a navigation to the manifest or an icon", () => {
    const { fetchEvent } = load();
    expect(fetchEvent("https://app.example/manifest.webmanifest", { mode: "navigate" })).not.toHaveBeenCalled();
    expect(fetchEvent("https://app.example/icons/icon-192.png", { mode: "navigate" })).not.toHaveBeenCalled();
    expect(fetchEvent("https://app.example/a/5f3a9c21", { mode: "navigate" })).toHaveBeenCalled();
    expect(fetchEvent("https://app.example/login", { mode: "navigate" })).toHaveBeenCalled();
  });

  it("knows the tabs and every page a link opens", () => {
    const { fetchEvent } = load();
    for (const path of ["/today", "/sessions", "/new", "/pages", "/read", "/week", "/spaces", "/spaces/travel", "/spaces/travel/r/knowledge/a.md",
      "/b/b1", "/p/p1", "/f/f1"])
      expect(fetchEvent(`https://app.example${path}`, { mode: "navigate" }), path).toHaveBeenCalled();
    for (const path of ["/todayx", "/newx", "/new/x", "/spaces/travel/x", "/s/a/b"])
      expect(fetchEvent(`https://app.example${path}`, { mode: "navigate" }), path).not.toHaveBeenCalled();
  });
});
