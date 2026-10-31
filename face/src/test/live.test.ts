import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { HIDDEN_RECONNECT_MS, Live, type LiveDeps } from "@/lib/live";
import type { Session, SessionsSnapshot } from "@/lib/types";

class FakeSource {
  static all: FakeSource[] = [];
  readyState = 0;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  constructor(readonly url: string) {
    FakeSource.all.push(this);
  }
  addEventListener(kind: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(kind, fn);
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  emit(kind: string, data: unknown, id?: number) {
    this.listeners.get(kind)?.({ data: JSON.stringify(data), lastEventId: id === undefined ? "" : String(id) } as MessageEvent<string>);
  }
}

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "code", lane: "code", kind: "code", state: "active", created: 1, at: 1, title: id, waiting: 0, ...over,
});

function setup(list: SessionsSnapshot = { schema: 1, cursor: 10, sessions: [session("s1"), session("s2")] }) {
  FakeSource.all = [];
  let clock = 0;
  const deps: LiveDeps = {
    api: {
      meta: vi.fn().mockResolvedValue({ schema: 1, owner_ok: true, tracks: [], face_origin: "" }),
      sessions: vi.fn().mockResolvedValue(list),
      session: vi.fn().mockImplementation(async (id: string) => ({ schema: 1, cursor: 10, session: session(id), items: [], approvals: [], summaries: [] })),
    },
    openSource: (url) => new FakeSource(url) as unknown as EventSource,
    probe: vi.fn().mockResolvedValue(200),
    now: () => clock,
    reload: vi.fn(),
    onUnauthorized: vi.fn(),
  };
  return { live: new Live(deps), deps, tick: (ms: number) => (clock += ms) };
}

describe("Live", () => {
  it("opens ONE stream for all sessions, from the list's cursor (E8)", async () => {
    const { live } = setup();
    await live.start();
    await live.watch("s1");
    await live.watch("s2");
    expect(FakeSource.all).toHaveLength(1);
    expect(FakeSource.all[0]!.url).toBe("/api/events?since=10");
    FakeSource.all[0]!.emit("item", { session: "s1", turn: 0, n: 0, kind: "message", data: { role: "assistant", text: "a" } }, 11);
    FakeSource.all[0]!.emit("item", { session: "s2", turn: 0, n: 0, kind: "message", data: { role: "assistant", text: "b" } }, 12);
    FakeSource.all[0]!.emit("delta", { session: "s2", text: "…" });
    const st = live.getState();
    expect(Object.keys(st.items.s1 ?? {})).toEqual(["0/0"]);
    expect(Object.keys(st.items.s2 ?? {})).toEqual(["0/0"]);
    expect(st.deltas.s2).toBe("…");
    expect(st.cursor).toBe(12);
  });

  it("on 410 reloads the snapshots and resumes from the new cursor", async () => {
    const { live, deps } = setup();
    await live.start();
    await live.watch("s1");
    (deps.probe as ReturnType<typeof vi.fn>).mockResolvedValue(410);
    (deps.api.sessions as ReturnType<typeof vi.fn>).mockResolvedValue({ schema: 1, cursor: 900, sessions: [session("s1", { state: "idle" })] });
    const first = FakeSource.all[0]!;
    first.readyState = 2;
    first.onerror?.();
    await vi.waitFor(() => expect(FakeSource.all).toHaveLength(2));
    expect(FakeSource.all[1]!.url).toBe("/api/events?since=900");
    expect(first.closed).toBe(true);
    expect(deps.api.session).toHaveBeenCalledTimes(2); // the watched session again
    // the session snapshot (cursor 10) is older than the new list (cursor 900): the list's row stays
    expect(live.getState().sessions.s1?.state).toBe("idle");
  });

  it("on 401 hands over to the login screen", async () => {
    const { live, deps } = setup();
    (deps.api.meta as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(401, "x"));
    await live.start();
    expect(deps.onUnauthorized).toHaveBeenCalled();
  });

  it("reopens the stream when the tab comes back after more than 45 s hidden, not sooner", async () => {
    const { live, tick } = setup();
    await live.start();
    live.visibility(true);
    tick(10_000);
    live.visibility(false);
    expect(FakeSource.all).toHaveLength(1);
    live.visibility(true);
    tick(HIDDEN_RECONNECT_MS + 1);
    live.visibility(false);
    expect(FakeSource.all).toHaveLength(2);
    expect(FakeSource.all[0]!.closed).toBe(true);
  });

  it("reloads the page when the server speaks another schema", async () => {
    const { live, deps } = setup({ schema: 2, cursor: 1, sessions: [] });
    await live.start();
    expect(deps.reload).toHaveBeenCalled();
    expect(live.connection).toBe("outdated");
  });

  it("loads the snapshot of a session that starts waiting, so its card is shown", async () => {
    const { live, deps } = setup();
    await live.start();
    FakeSource.all[0]!.emit("session", session("s2", { state: "waiting", waiting: 1 }), 11);
    await vi.waitFor(() => expect(deps.api.session).toHaveBeenCalledWith("s2"));
  });

  it("retries a failed start with backoff, and at once when the tab comes back", async () => {
    vi.useFakeTimers();
    try {
      const { live, deps } = setup();
      const meta = deps.api.meta as ReturnType<typeof vi.fn>;
      meta.mockRejectedValueOnce(new ApiError(0, "No connection to the server.")).mockRejectedValueOnce(new ApiError(502, "x"));
      await live.start();
      expect(live.connection).toBe("offline");
      expect(FakeSource.all).toHaveLength(0);
      await vi.advanceTimersByTimeAsync(1_000); // the first retry, fails again
      expect(meta).toHaveBeenCalledTimes(2);
      live.visibility(true);
      live.visibility(false); // back on the tab: no waiting for the 2 s backoff
      await vi.waitFor(() => expect(FakeSource.all).toHaveLength(1));
      expect(meta).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(60_000);
      expect(meta).toHaveBeenCalledTimes(3); // no stray retry once started
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not retry after a 401", async () => {
    vi.useFakeTimers();
    try {
      const { live, deps } = setup();
      (deps.api.meta as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(401, "x"));
      await live.start();
      await vi.advanceTimersByTimeAsync(60_000);
      expect(deps.api.meta).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
