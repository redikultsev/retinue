import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionList } from "@/components/session-list";
import { SessionPanel } from "@/components/session-panel";
import { SideQuestion } from "@/components/side-question";
import { live } from "@/lib/live";
import type { Session, SessionSnapshot } from "@/lib/types";

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "code", lane: "code", kind: "code", state: "idle", created: 1, at: 1, title: `Сессия ${id}`, waiting: 0,
  ...over,
});

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>открыта сессия</p> });
  const router = createRouter({
    routeTree: root.addChildren([home, one]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(<RouterProvider router={router} />);
}

function mountPanel(id: string) {
  const root = createRootRoute();
  const page = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <SessionPanel id={id} tab="chat" onTab={() => {}} /> });
  const router = createRouter({
    routeTree: root.addChildren([page]),
    history: createMemoryHistory({ initialEntries: [`/s/${id}`] }),
  });
  return render(<RouterProvider router={router} />);
}

function seed(sessions: Session[]) {
  live.dispatch({ type: "list", snapshot: { schema: 1, cursor: 5, sessions } });
}

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
});

describe("SessionList", () => {
  it("says when a session waits for room in the lane, and in its own header too", async () => {
    seed([session("a1b2c3d4", { sub_of: null, queued: true, title: "жильё" }), session("b2", { title: "другая" })]);
    mount(<SessionList />);
    const row = (await screen.findByText("жильё")).closest("a") as HTMLElement;
    expect(row.textContent).toContain("ждёт места в комнате");
    expect((screen.getByText("другая").closest("a") as HTMLElement).textContent).not.toContain("ждёт места");
    const snapshot: SessionSnapshot = {
      schema: 1, cursor: 6, session: session("a1b2c3d4", { queued: true, title: "жильё" }), approvals: [],
      summaries: [], items: [],
    };
    vi.spyOn(live, "watch").mockImplementation(async () => live.dispatch({ type: "session", snapshot }));
    mountPanel("a1b2c3d4");
    const header = (await screen.findAllByRole("banner")).at(-1) as HTMLElement;
    await waitFor(() => expect(header.textContent).toContain("ждёт места в комнате"));
  });

  it("opens a peek with the session's last words and a reply box, without leaving the list", async () => {
    seed([session("a1b2c3d4")]);
    const snapshot: SessionSnapshot = {
      schema: 1, cursor: 6, session: session("a1b2c3d4"), approvals: [], summaries: [],
      items: [{ session: "a1b2c3d4", turn: 0, n: 0, kind: "message", data: { role: "assistant", text: "Тесты **зелёные**." } }],
    };
    const watch = vi.spyOn(live, "watch").mockImplementation(async () => live.dispatch({ type: "session", snapshot }));
    mount(<SessionList />);
    fireEvent.click(await screen.findByRole("button", { name: "Заглянуть: Сессия a1b2c3d4" }));
    const peek = await screen.findByTestId("peek");
    expect(watch).toHaveBeenCalledWith("a1b2c3d4");
    expect(await within(peek).findByText("зелёные")).toBeTruthy();
    expect(within(peek).getByRole("textbox", { name: "Сообщение" })).toBeTruthy();
    expect(screen.queryByText("открыта сессия")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Свернуть: Сессия a1b2c3d4" }));
    expect(screen.queryByTestId("peek")).toBeNull();
  });

  it("shows the last ping's replies under the session's row once the app has seen it", async () => {
    seed([session("a1b2c3d4")]);
    mount(<SessionList />);
    await screen.findByText("Сессия a1b2c3d4");
    expect(screen.queryByRole("group", { name: "Ответы" })).toBeNull();
    const summary = { session: "a1b2c3d4", seq: 3, head: "Итог", text: "Готово.", at: 2, turn: 0, kind: "decision" as const,
      took_s: 10, replies: ["Да", "Нет"], answered: null };
    act(() => live.dispatch({ type: "event", id: 9, event: { kind: "summary", data: summary } }));
    const group = await screen.findByRole("group", { name: "Ответы" });
    expect(within(group).getAllByRole("button").map((b) => b.textContent)).toEqual(["Да", "Нет"]);
    act(() => live.dispatch({ type: "event", id: 10, event: { kind: "summary", data: { ...summary, replies: [], answered: "Да" } } }));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Ответы" })).toBeNull());
  });

  it("pins on the server, the pinned filter shows it, and side sessions hang under theirs", async () => {
    seed([
      session("old", { at: 1 }),
      session("new", { at: 9 }),
      session("side", { at: 10, parent: "old", title: "почему так?" }),
    ]);
    const pin = vi.fn().mockResolvedValue({ session: session("old", { at: 1, pinned: true }) });
    mount(<SessionList pin={pin} />);
    const titles = () => screen.getAllByRole("link").map((a) => a.textContent);
    await screen.findByRole("button", { name: "Закрепить: Сессия old" });
    expect(titles()[0]).toContain("Сессия new");
    expect(titles()[1]).toContain("Сессия old");
    expect(titles()[2]).toContain("вопрос: почему так?");
    fireEvent.click(screen.getByRole("button", { name: "Закрепить: Сессия old" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Открепить: Сессия old" }).getAttribute("aria-pressed")).toBe("true"),
    );
    expect(pin).toHaveBeenCalledWith("old", true);
    fireEvent.click(screen.getByRole("button", { name: "Закреплены" }));
    expect(titles()).toHaveLength(1);
    expect(titles()[0]).toContain("Сессия old");
    fireEvent.click(screen.getByRole("button", { name: "Все" }));
  });
});

describe("SideQuestion", () => {
  it("asks once per question and lands in the side session", async () => {
    const ask = vi.fn().mockResolvedValue({ session: "77889900", say: "Сессия 77889900" });
    mount(<SideQuestion session="a1b2c3d4" ask={ask} />);
    fireEvent.click(await screen.findByRole("button", { name: "Вопрос в сторону" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Вопрос" }), { target: { value: " зачем? " } });
    fireEvent.click(screen.getByRole("button", { name: "Спросить" }));
    expect(await screen.findByText("открыта сессия")).toBeTruthy();
    expect(ask).toHaveBeenCalledWith("a1b2c3d4", "зачем?", expect.stringMatching(/^face:/));
  });
});
