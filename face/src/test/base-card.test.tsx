import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BaseCardView, filesWord } from "@/components/base-card";
import { FeedView } from "@/components/feed";
import { Transcript } from "@/components/transcript/transcript";
import { api } from "@/lib/api";
import type { FeedRow } from "@/lib/feed";
import { Live, live } from "@/lib/live";
import { initialState, openBaseCard, reduce } from "@/lib/store";
import type { BaseCard, SessionSnapshot, Summary } from "@/lib/types";
import { BasePage, CLOSE_AFTER_MS } from "@/pages/base";

const card = (over: Partial<BaseCard> = {}): BaseCard => ({
  id: "c0ffee00",
  session: "a1b2c3d4",
  state: "shown",
  place: "✈️ Путешествия",
  files: [
    { path: "travel/hotel.md", status: "A", add: 12, del: 0, on: true, shared: false },
    { path: "travel/route.md", status: "M", add: 3, del: 1, on: true, shared: true },
  ],
  subject: "travel: отель в Зурбагане",
  custom: false,
  others: { files: 2, sessions: 1 },
  sha: null,
  said: "",
  refused: [],
  committed: [],
  created: 10,
  at: 10,
  view: { head: ["В Базу · ✈️ Путешествия · сессия a1b2c3d4", "Тема: travel: отель в Зурбагане"], body: "", status: "" },
  ...over,
});

const fakeLive = () =>
  new Live({
    api: { meta: vi.fn(), sessions: vi.fn(), session: vi.fn() },
    openSource: vi.fn(),
    probe: vi.fn(),
    now: () => 0,
    reload: vi.fn(),
    onUnauthorized: vi.fn(),
  });

const calls = (answer: Partial<BaseCard> = {}) => {
  const res = { toast: "", card: card(answer) };
  return {
    baseToggle: vi.fn().mockResolvedValue(res),
    baseSubject: vi.fn().mockResolvedValue(res),
    baseCommit: vi.fn().mockResolvedValue({ toast: "Коммичу…", card: card({ ...answer, state: "committing", at: 11 }) }),
    baseCancel: vi.fn().mockResolvedValue(res),
    baseOpen: vi.fn().mockResolvedValue(res),
  };
};

function mount(node: React.ReactNode, at = "/") {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const base = createRoute({ getParentRoute: () => root, path: "/b/$id", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>открыта сессия</p> });
  const router = createRouter({
    routeTree: root.addChildren([home, base, one]),
    history: createMemoryHistory({ initialEntries: [at] }),
  });
  return render(<RouterProvider router={router} />);
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  live.stop();
});

describe("BaseCardView", () => {
  it("lists the files with A/M/D and their lines, marks a shared one and counts the others without naming them", () => {
    render(<BaseCardView card={card()} calls={calls()} live={fakeLive()} />);
    const files = screen.getByRole("list", { name: "Файлы" });
    expect(within(files).getAllByRole("checkbox").map((c) => (c as HTMLInputElement).checked)).toEqual([true, true]);
    expect(within(files).getByText("travel/hotel.md")).toBeTruthy();
    expect(within(files).getByText("+12")).toBeTruthy();
    expect(within(files).getByTitle("Файл менялся и в другой сессии")).toBeTruthy();
    expect(screen.getByText("Ещё 2 файла не закоммичено в других сессиях (1).")).toBeTruthy();
    expect((screen.getByLabelText("Тема коммита") as HTMLInputElement).value).toBe("travel: отель в Зурбагане");
    expect(screen.getByRole("button", { name: "Закоммитить · 2" })).toBeTruthy();
  });

  it("a toggle, a subject and the commit go to the platform, and its card goes into the store", async () => {
    const c = calls({ files: [card().files[0]!, { ...card().files[1]!, on: false }] });
    const store = fakeLive();
    render(<BaseCardView card={card()} calls={c} live={store} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "travel/route.md" }));
    await waitFor(() => expect(c.baseToggle).toHaveBeenCalledWith("c0ffee00", "travel/route.md", false));
    await waitFor(() => expect(store.getState().bases.c0ffee00?.files[1]?.on).toBe(false));

    fireEvent.change(screen.getByLabelText("Тема коммита"), { target: { value: "travel: отель" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(c.baseSubject).toHaveBeenCalledWith("c0ffee00", "travel: отель"));

    fireEvent.click(screen.getByRole("button", { name: "Закоммитить · 2" }));
    await waitFor(() => expect(c.baseCommit).toHaveBeenCalledWith("c0ffee00"));
    await waitFor(() => expect(store.getState().bases.c0ffee00?.state).toBe("committing"));
  });

  it("«Закоммитить» saves an edited subject first, with the commit after it", async () => {
    const c = calls();
    render(<BaseCardView card={card()} calls={c} live={fakeLive()} />);
    fireEvent.change(screen.getByLabelText("Тема коммита"), { target: { value: "travel: другое" } });
    fireEvent.click(screen.getByRole("button", { name: "Закоммитить · 2" }));
    await waitFor(() => expect(c.baseCommit).toHaveBeenCalledWith("c0ffee00"));
    expect(c.baseSubject).toHaveBeenCalledWith("c0ffee00", "travel: другое");
    expect(c.baseSubject.mock.invocationCallOrder[0]!).toBeLessThan(c.baseCommit.mock.invocationCallOrder[0]!);
  });

  it("marks nobody's file and says what it is", () => {
    render(<BaseCardView card={card({ files: [{ path: "MAP.md", status: "M", add: 1, del: 0, on: false, shared: false, nobody: true }] })} calls={calls()} live={fakeLive()} />);
    expect(screen.getByTitle("Ни одна сессия его не меняла")).toBeTruthy();
    expect(screen.getByText(/«ничей» — изменён, но ни одной сессией/)).toBeTruthy();
  });

  it("a decided card shows its result and no commit; nothing turned on, nothing to commit", () => {
    const { unmount } = render(
      <BaseCardView
        card={card({ state: "committed", sha: "abc1234def", said: "Закоммичено: abc1234 · 1 файл." })}
        calls={calls()}
        live={fakeLive()}
      />,
    );
    expect(screen.getByRole("status").textContent).toBe("Закоммичено: abc1234 · 1 файл.");
    expect(screen.queryByRole("button", { name: /Закоммитить/ })).toBeNull();
    expect(screen.getAllByRole("checkbox").every((c) => (c as HTMLInputElement).disabled)).toBe(true);
    unmount();
    render(<BaseCardView card={card({ files: card().files.map((f) => ({ ...f, on: false })) })} calls={calls()} live={fakeLive()} />);
    expect((screen.getByRole("button", { name: "Закоммитить · 0" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("says Russian plurals of files", () => {
    expect([1, 2, 5, 11, 21, 22, 112].map(filesWord)).toEqual([
      "1 файл",
      "2 файла",
      "5 файлов",
      "11 файлов",
      "21 файл",
      "22 файла",
      "112 файлов",
    ]);
  });
});

describe("the store", () => {
  it("keeps a card by its event, merges a snapshot's cards like approvals and finds the open one", () => {
    let state = reduce(initialState, { type: "event", id: 5, event: { kind: "base", data: card({ at: 20, state: "committing" }) } });
    const snapshot = {
      schema: 1,
      cursor: 4,
      session: { id: "a1b2c3d4", track: "base", lane: "private", kind: "private", state: "idle", created: 1, at: 1, title: "t", waiting: 0, base: 2 },
      items: [],
      approvals: [],
      summaries: [],
      base_cards: [card({ at: 10 }), card({ id: "0ld0ld00", state: "committed", created: 1 })],
    } as SessionSnapshot;
    state = reduce(state, { type: "session", snapshot });
    expect(state.bases.c0ffee00?.state).toBe("committing"); // the stream's newer copy wins
    expect(state.bases["0ld0ld00"]?.state).toBe("committed");
    expect(openBaseCard(state.bases, "a1b2c3d4")?.id).toBe("c0ffee00");
    const older = reduce(state, { type: "event", id: null, event: { kind: "base", data: card({ at: 15 }) } });
    expect(older.bases.c0ffee00?.state).toBe("committing"); // an answer older than the stream's copy never wins
  });
});

describe("where the button is", () => {
  const summary = (seq: number): Summary => ({ session: "a1b2c3d4", seq, head: `Итог ${seq}`, text: `Готово ${seq}.`, at: seq, turn: null });

  it("under the session's last summary only, while it has Base files", () => {
    const { rerender } = render(
      <Transcript items={[]} approvals={[]} running={false} summaries={[summary(1), summary(2)]} base={{ session: "a1b2c3d4", count: 2 }} />,
    );
    const notes = screen.getAllByTestId("summary");
    expect(notes).toHaveLength(1); // only the latest of summaries without a turn is shown
    expect(within(notes[0]!).getByRole("button", { name: "В Базу · 2" })).toBeTruthy();
    rerender(<Transcript items={[]} approvals={[]} running={false} summaries={[summary(1), summary(2)]} base={{ session: "a1b2c3d4", count: 0 }} />);
    expect(screen.queryByRole("button", { name: /В Базу/ })).toBeNull();
  });

  it("in the feed's «Решить» row, and a press opens the card", async () => {
    const opened = vi.spyOn(api, "baseOpen").mockResolvedValue({ toast: "", card: card() });
    const row: FeedRow = {
      key: "base:a1b2c3d4",
      action: "decide",
      source: "base",
      session: "a1b2c3d4",
      track: "base",
      sphere: null,
      sphere_title: null,
      sphere_icon: null,
      title: "добавь отель",
      since: 0,
      essence: "В Базу: 2 файла",
      ref: "a1b2c3d4",
      base: 2,
    };
    mount(<FeedView feed={{ schema: 1, count: 1, bot: "", assistant_at: null, groups: [{ action: "decide", title: "Решить", rows: [row] }] }} now={100} />);
    const decide = await screen.findByRole("region", { name: "Решить" });
    expect(within(decide).getByText("В Базу: 2 файла")).toBeTruthy();
    fireEvent.click(within(decide).getByRole("button", { name: "В Базу · 2" }));
    await waitFor(() => expect(opened).toHaveBeenCalledWith("a1b2c3d4"));
    expect(await screen.findByTestId("base-card")).toBeTruthy();
  });
});

describe("BasePage", () => {
  it("loads the card, and inside Telegram closes once it is decided", async () => {
    vi.spyOn(live, "watch").mockResolvedValue();
    const load = vi.fn().mockResolvedValue({ card: card() });
    const close = vi.fn().mockReturnValue(true);
    mount(<BasePage id="c0ffee00" load={load} telegram={() => true} close={close} />, "/b/c0ffee00");
    expect(await screen.findByTestId("base-card")).toBeTruthy();
    expect(load).toHaveBeenCalledWith("c0ffee00");
    vi.useFakeTimers();
    act(() => live.dispatch({ type: "event", id: 9, event: { kind: "base", data: card({ state: "committed", at: 20, said: "Закоммичено." }) } }));
    expect(screen.getByText("Закрываю…")).toBeTruthy();
    act(() => vi.advanceTimersByTime(CLOSE_AFTER_MS));
    expect(close).toHaveBeenCalledOnce();
  });
});
