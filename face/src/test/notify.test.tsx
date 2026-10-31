import { act, render, screen } from "@testing-library/react";
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { describe, expect, it, vi } from "vitest";
import { Toaster } from "@/components/toaster";
import { Live, type LiveDeps } from "@/lib/live";
import { cardNotice, visibleSessions } from "@/lib/notify";
import { closeMiniApp, insideTelegram } from "@/lib/telegram";
import { toast } from "@/lib/toast";
import type { Approval, Session } from "@/lib/types";

const card = (over: Partial<Approval> = {}): Approval => ({
  id: "c1", session: "s2", tool_use_id: "t", kind: "approval", state: "shown", tool: "Bash", input: { command: "ls" },
  cwd: "/work", digest: "d", view: { head: ["Approval"] }, created: 1, at: 1, ...over,
});
const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "code", lane: "code", kind: "code", state: "waiting", created: 1, at: 1, title: "Fix the tests", waiting: 1,
  ...over,
});

describe("a card for another session", () => {
  it("is announced unless its session is on screen or its own page is open", () => {
    expect(visibleSessions("/s/s1", "?with=s3&tab=chat")).toEqual(["s1", "s3"]);
    expect(visibleSessions("/", "")).toEqual([]);
    expect(cardNotice(card(), session("s2"), ["s1"])).toEqual({ text: "Awaiting decision: code · Fix the tests", href: "/s/s2" });
    expect(cardNotice(card(), undefined, [])?.text).toBe("Awaiting decision: session s2");
    expect(cardNotice(card(), session("s2"), ["s1", "s2"])).toBeNull();
    expect(cardNotice(card(), session("s2"), [], "/a/c1")).toBeNull();
  });

  it("is told by the stream once, when it starts to wait — not from a snapshot, not when it is rewritten", async () => {
    const sources: { emit: (kind: string, data: unknown, id: number) => void }[] = [];
    const deps: LiveDeps = {
      api: {
        meta: vi.fn().mockResolvedValue({ schema: 1, owner_ok: true, tracks: [], face_origin: "" }),
        sessions: vi.fn().mockResolvedValue({ schema: 1, cursor: 10, sessions: [session("s2")] }),
        session: vi.fn().mockResolvedValue({ schema: 1, cursor: 10, session: session("s2"), items: [], summaries: [],
                                             approvals: [card({ id: "old" })] }),
      },
      openSource: () => {
        const listeners = new Map<string, (e: MessageEvent<string>) => void>();
        const source = { addEventListener: (k: string, fn: (e: MessageEvent<string>) => void) => listeners.set(k, fn),
                         close: vi.fn(), onopen: null, onerror: null };
        sources.push({ emit: (kind, data, id) => listeners.get(kind)?.({ data: JSON.stringify(data), lastEventId: String(id) } as MessageEvent<string>) });
        return source as unknown as EventSource;
      },
      probe: vi.fn(), now: () => 0, reload: vi.fn(), onUnauthorized: vi.fn(),
    };
    const live = new Live(deps);
    const told: string[] = [];
    live.onNewCard((a) => told.push(a.id));
    await live.start();
    expect(told).toEqual([]); // the snapshot's card was there before
    sources[0]!.emit("approval", card({ id: "new", state: "pending" }), 11);
    sources[0]!.emit("approval", card({ id: "new" }), 12);
    sources[0]!.emit("approval", card({ id: "new", view: { head: ["Approval", "Session idle for 55 min — …"] } }), 13);
    sources[0]!.emit("approval", card({ id: "courier", kind: "courier" }), 14);
    expect(told).toEqual(["new"]);
    live.stop();
  });

  it("is a toast that opens the session", async () => {
    const root = createRootRoute({ component: () => <Toaster /> });
    const router = createRouter({
      routeTree: root.addChildren([createRoute({ getParentRoute: () => root, path: "/", component: () => null })]),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });
    render(<RouterProvider router={router} />);
    await act(async () => toast("Awaiting decision: code · Fix the tests", "info", 8000, "/s/s2"));
    const link = await screen.findByRole("status");
    expect(link.tagName).toBe("A");
    expect(link.getAttribute("href")).toBe("/s/s2");
  });
});

describe("the Mini App", () => {
  it("is told by Telegram's web view and closed through it, without Telegram's script", () => {
    const postEvent = vi.fn();
    const tg = { TelegramWebviewProxy: { postEvent } } as unknown as Window;
    expect(insideTelegram(tg)).toBe(true);
    expect(closeMiniApp(tg)).toBe(true);
    expect(postEvent).toHaveBeenCalledWith("web_app_close", "{}");
    const close = vi.fn();
    expect(closeMiniApp({ Telegram: { WebApp: { close } } } as unknown as Window)).toBe(true);
    expect(close).toHaveBeenCalled();
    expect(insideTelegram({} as Window)).toBe(false);
    expect(closeMiniApp({} as Window)).toBe(false);
  });
});
