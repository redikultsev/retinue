import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionList } from "@/components/session-list";
import { SessionPanel } from "@/components/session-panel";
import { SideQuestion } from "@/components/side-question";
import { live } from "@/lib/live";
import type { Session, SessionSnapshot } from "@/lib/types";

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "code", lane: "code", kind: "code", state: "idle", created: 1, at: 1, title: `Session ${id}`, waiting: 0,
  ...over,
});

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
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
    seed([session("a1b2c3d4", { sub_of: null, queued: true, title: "lodging" }), session("b2", { title: "another" })]);
    mount(<SessionList />);
    const row = (await screen.findByText("lodging")).closest("a") as HTMLElement;
    expect(row.textContent).toContain("waiting for room on the lane");
    expect((screen.getByText("another").closest("a") as HTMLElement).textContent).not.toContain("waiting for room");
    const snapshot: SessionSnapshot = {
      schema: 1, cursor: 6, session: session("a1b2c3d4", { queued: true, title: "lodging" }), approvals: [],
      summaries: [], items: [],
    };
    vi.spyOn(live, "watch").mockImplementation(async () => live.dispatch({ type: "session", snapshot }));
    mountPanel("a1b2c3d4");
    const header = (await screen.findAllByRole("banner")).at(-1) as HTMLElement;
    await waitFor(() => expect(header.textContent).toContain("waiting for room on the lane"));
  });

  it("opens a peek with the session's last words and a reply box, without leaving the list", async () => {
    seed([session("a1b2c3d4")]);
    const snapshot: SessionSnapshot = {
      schema: 1, cursor: 6, session: session("a1b2c3d4"), approvals: [], summaries: [],
      items: [{ session: "a1b2c3d4", turn: 0, n: 0, kind: "message", data: { role: "assistant", text: "The tests are **green**." } }],
    };
    const watch = vi.spyOn(live, "watch").mockImplementation(async () => live.dispatch({ type: "session", snapshot }));
    mount(<SessionList />);
    fireEvent.click(await screen.findByRole("button", { name: "Peek: Session a1b2c3d4" }));
    const peek = await screen.findByTestId("peek");
    expect(watch).toHaveBeenCalledWith("a1b2c3d4");
    expect(await within(peek).findByText("green")).toBeTruthy();
    expect(within(peek).getByRole("textbox", { name: "Message" })).toBeTruthy();
    expect(screen.queryByText("session opened")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Collapse: Session a1b2c3d4" }));
    expect(screen.queryByTestId("peek")).toBeNull();
  });

  it("never shows a ping's replies under the session's row: they are under its summary", async () => {
    seed([session("a1b2c3d4")]);
    mount(<SessionList />);
    await screen.findByText("Session a1b2c3d4");
    const summary = { session: "a1b2c3d4", seq: 3, head: "Result", text: "Done.", at: 2, turn: 0, kind: "decision" as const,
      took_s: 10, replies: ["Yes", "No"], answered: null };
    act(() => live.dispatch({ type: "event", id: 9, event: { kind: "summary", data: summary } }));
    await waitFor(() => expect(live.getState().summaries.a1b2c3d4?.["3"]).toBeTruthy());
    expect(screen.queryByRole("group", { name: "Replies" })).toBeNull();
  });

  it("pins on the server, the pinned filter shows it, and side sessions hang under theirs", async () => {
    seed([
      session("old", { at: 1 }),
      session("new", { at: 9 }),
      session("side", { at: 10, parent: "old", title: "why so?" }),
    ]);
    const pin = vi.fn().mockResolvedValue({ session: session("old", { at: 1, pinned: true }) });
    mount(<SessionList pin={pin} />);
    const titles = () => screen.getAllByRole("link").map((a) => a.textContent);
    await screen.findByRole("button", { name: "Pin: Session old" });
    expect(titles()[0]).toContain("Session new");
    expect(titles()[1]).toContain("Session old");
    expect(titles()[2]).toContain("side question: why so?");
    fireEvent.click(screen.getByRole("button", { name: "Pin: Session old" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Unpin: Session old" }).getAttribute("aria-pressed")).toBe("true"),
    );
    expect(pin).toHaveBeenCalledWith("old", true);
    fireEvent.click(screen.getByRole("button", { name: "Pinned" }));
    expect(titles()).toHaveLength(1);
    expect(titles()[0]).toContain("Session old");
    fireEvent.click(screen.getByRole("button", { name: "All" }));
  });
});

describe("SideQuestion", () => {
  it("asks once per question and lands in the side session", async () => {
    const ask = vi.fn().mockResolvedValue({ session: "77889900", say: "Session 77889900" });
    mount(<SideQuestion session="a1b2c3d4" ask={ask} />);
    fireEvent.click(await screen.findByRole("button", { name: "Side question" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Question" }), { target: { value: " what for? " } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("session opened")).toBeTruthy();
    expect(ask).toHaveBeenCalledWith("a1b2c3d4", "what for?", expect.stringMatching(/^face:/));
  });
});
