import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionList } from "@/components/session-list";
import { type Computer, computerRows, setComputers } from "@/lib/computers";
import { live } from "@/lib/live";
import { forgetProjects, setProjects } from "@/lib/projects";

const now = Date.now() / 1000;

const laptop = (over: Partial<Computer> = {}): Computer => ({
  device: "laptop",
  at: now - 5,
  off: false,
  sessions: [
    { id: "local_a1", title: "compare two stays", folder: "/home/anna/base/travel/lisbon", project: "travel/lisbon/notes", state: "working", at: now - 20, link: "claude://claude.ai/epitaxy/local_a1" },
    { id: "local_b2", title: "packing list", folder: "/home/anna/base/travel", project: "travel", state: "waiting", at: now - 600 },
    { id: "local_c3", title: "scratch", folder: "/tmp", project: "nowhere/at/all", state: "done", at: now - 60 },
  ],
  ...over,
});

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const router = createRouter({ routeTree: root.addChildren([home]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

/** What the app reads: the computers as given, every other call refused (no platform behind the test). */
function answer(computers: Computer[]) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
    String(input).endsWith("/api/computers")
      ? new Response(JSON.stringify({ computers, off_s: 120 }), { status: 200, headers: { "Content-Type": "application/json" } })
      : new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } }),
  );
}

beforeEach(() => {
  live.meta = { schema: 1, owner_ok: true, face_origin: "", tracks: [], spheres: [] };
  setProjects([
    { name: "travel", title: "Travel", parent: "", questions: 0, tasks: 0 },
    { name: "travel/lisbon", title: "Lisbon in May", parent: "travel", questions: 0, tasks: 0 },
  ]);
  live.dispatch({ type: "list", snapshot: { schema: 1, cursor: 1, sessions: [] } });
});

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
  live.meta = null;
  forgetProjects();
  setComputers([]);
});

describe("a computer's own sessions in the list", () => {
  it("puts each under the nearest project of the folder its computer gave, else «Other», with its state", async () => {
    answer([laptop()]);
    mount(<SessionList />);
    const lisbon = (await screen.findByRole("heading", { name: /Lisbon in May/ })).closest("section") as HTMLElement;
    fireEvent.click(within(lisbon).getByRole("button", { name: "Expand: Lisbon in May" })); // a sub-project: folded until opened
    const working = within(lisbon).getByText("compare two stays").closest("a") as HTMLElement;
    expect(working.getAttribute("href")).toBe("claude://claude.ai/epitaxy/local_a1"); // opens it on the computer
    expect(working.title).toContain("on laptop");
    expect(working.title).toContain("/home/anna/base/travel/lisbon"); // the folder: its tooltip only
    expect(working.querySelector(".animate-pulse")).toBeTruthy(); // at work, as a room session's dot

    const travel = screen.getByRole("heading", { name: /^Open the project Travel/ }).closest("section") as HTMLElement;
    const waiting = within(travel).getByText("packing list").closest("div") as HTMLElement;
    expect(waiting.textContent).toContain("waiting");
    expect(waiting.closest("a")).toBeNull(); // no link given: a row that opens nothing

    // a Base folder no project holds: «Other», never a project of its own made up from its top folder
    const other = screen.getByRole("heading", { name: "Other" }).closest("section") as HTMLElement;
    expect(within(other).getByText("scratch")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: /nowhere/ })).toBeNull();
  });

  it("greys an off computer's rows, none of them working or waiting", async () => {
    answer([laptop({ off: true, at: now - 3600 })]);
    mount(<SessionList />);
    fireEvent.click(await screen.findByRole("button", { name: "Expand: Lisbon in May" }));
    const row = (await screen.findByText("compare two stays")).closest("a") as HTMLElement;
    expect(row.title).toContain("laptop off · seen");
    expect(row.textContent).toContain("off");
    expect(computerRows([laptop({ off: true })]).every((s) => s.state === "closed")).toBe(true);
  });
});
