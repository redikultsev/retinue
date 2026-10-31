// «Spaces»: the grid with its badges, a Space as a screen, a Record to read, «Discuss» — against a fake API.
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { live } from "@/lib/live";
import { QUOTE_FRAME, type RecordView, type SpaceView } from "@/lib/spaces";
import { BaseMarkdown } from "@/components/base-markdown";
import type { Session } from "@/lib/types";
import { Discuss, forgetRead, RecordPage, SpacePage, SpacesPage } from "@/pages/spaces";

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: Date.now() / 1000, title: `Session ${id}`,
  waiting: 0, ...over,
});

const SPACE: SpaceView = {
  name: "travel",
  title: "Travel",
  rules: "- Live prices only.",
  marker: true,
  counts: { questions: 1, tasks: 1 },
  sections: [
    { heading: "Tasks", kind: "tasks", items: [{ text: "Recheck the [transit](/spaces/travel/r/knowledge/transit.md)", list: true }] },
    { heading: "Open questions", kind: "questions", items: [{ text: "Take the transit through the hub?", list: true }] },
    { heading: "Records", kind: "records", items: [{ text: "**knowledge/**", list: false }] },
    { heading: "Deadlines", kind: "other", items: [{ text: "passport until 2030", list: true }] },
  ],
  records: {
    knowledge: [
      { path: "knowledge/transit.md", folder: "", title: "Airport transfer", verified: "2026-09-21", practice: "diverges" },
      { path: "knowledge/hotels.md", folder: "", title: "Airport hotels" },
    ],
    profile: [{ path: "profile/docs/passport.md", folder: "docs", title: "Passport", until: "2030-01-01" }],
    journal: [],
    artifacts: [],
  },
};

const RECORD: RecordView = {
  space: "travel",
  path: "knowledge/transit.md",
  nature: "knowledge",
  title: "Airport transfer",
  head: {
    written_by: "Claude Code (Opus 5)", type: "knowledge", verified: "2026-09-21", practice: "diverges",
    stale_after: "2099-12-21", colour: "green",
    source: "The rule — [x](javascript:alert(1)), ![i](https://evil.example/i.png), https://example.org/rules.",
  },
  bad_head: false,
  body: "See the [passport](/spaces/travel/r/profile/docs/passport.md) and the [site](https://example.org).\n\n" +
    '<img src="https://evil.example/x.png"> ![x](https://evil.example/y.png)',
};

function mount(node: React.ReactNode, at = "/") {
  const root = createRootRoute();
  const page = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const spaces = createRoute({ getParentRoute: () => root, path: "/spaces", component: () => <p>grid</p> });
  const space = createRoute({ getParentRoute: () => root, path: "/spaces/$space", component: () => <p>space</p> });
  const record = createRoute({
    getParentRoute: () => root,
    path: "/spaces/$space/r/$",
    component: function R() {
      const { space: s, _splat } = record.useParams();
      return <p>record {s}/{_splat}</p>;
    },
  });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const router = createRouter({
    routeTree: root.addChildren([page, spaces, space, record, one]),
    history: createMemoryHistory({ initialEntries: [at] }),
  });
  render(<RouterProvider router={router} />);
  return router;
}

beforeEach(() => {
  forgetRead();
  live.meta = {
    schema: 1, owner_ok: true, face_origin: "", tracks: [{ name: "notes", lane: "private", kind: "private" }],
    spheres: [
      { name: "travel", title: "Travel", icon: "✈️", track: "notes" },
      { name: "work", title: "Work", icon: "💼", track: "notes" },
    ],
  };
});

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
  live.meta = null;
});

describe("SpacesPage", () => {
  it("shows the Spaces in the spheres' order with their icons and badges from the live sessions", async () => {
    live.dispatch({
      type: "list",
      snapshot: { schema: 1, cursor: 1, sessions: [
        session("w", { sphere: "travel", state: "waiting" }),
        session("a", { sphere: "travel", state: "active" }),
        session("b", { sphere: "travel", state: "active" }),
      ] },
    });
    const read = vi.fn().mockResolvedValue({
      head: "h",
      spaces: [
        { name: "home", title: "Home", questions: 0, tasks: 2, waiting: 0, working: 0 },
        { name: "work", title: "Work", questions: 3, tasks: 1, waiting: 5, working: 0 },
        { name: "travel", title: "Travel", questions: 1, tasks: 1, waiting: 0, working: 0 },
      ],
    });
    const router = mount(<SpacesPage read={read} />);
    const links = await screen.findAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual([
      "✈️Travel1 waiting2 working1 question",
      "💼Work3 questions",
      "📁Home",
    ]);
    fireEvent.click(links[0]!);
    await waitFor(() => expect(router.state.location.pathname).toBe("/spaces/travel"));
  });

  it("says why it could not load", async () => {
    mount(<SpacesPage read={vi.fn().mockRejectedValue(new ApiError(404, "The Base is not connected."))} />);
    expect(await screen.findByText("The Base is not connected.")).toBeTruthy();
  });
});

describe("SpacePage", () => {
  it("shows the Space's sessions, Open questions, Tasks and Records by nature, with a search", async () => {
    live.dispatch({
      type: "list",
      snapshot: { schema: 1, cursor: 1, sessions: [
        session("t1", { sphere: "travel", state: "waiting", title: "Tickets for June" }),
        session("c1", { sphere: "work", title: "Quarterly report" }),
      ] },
    });
    mount(<SpacePage space="travel" read={vi.fn().mockResolvedValue({ head: "h", space: SPACE })} />);
    await screen.findByText("Take the transit through the hub?");
    expect(screen.getByText("1 waiting · 1 Open question")).toBeTruthy();
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["Sessions (1)", "Open questions (1)", "Tasks (1)", "Records (3)"]);
    expect(screen.getByRole("link", { name: /Tickets for June/ })).toBeTruthy();
    expect(screen.queryByText("Quarterly report")).toBeNull();
    expect(screen.getByRole("link", { name: "transit" }).getAttribute("href")).toBe("/spaces/travel/r/knowledge/transit.md");
    expect(screen.getByText("Knowledge 2 · Profile 1")).toBeTruthy();
    const transit = screen.getByRole("link", { name: /Airport transfer/ });
    expect(transit.textContent).toContain("verified 21.09");
    expect(transit.textContent).toContain("practice diverges");
    expect(screen.getByText("docs/")).toBeTruthy();
    expect(screen.queryByText("**knowledge/**")).toBeNull();
    expect(screen.getByText("Deadlines")).toBeTruthy();

    fireEvent.change(screen.getByRole("searchbox", { name: "Find a Record" }), { target: { value: "hotel" } });
    expect(screen.queryByRole("link", { name: /Airport transfer/ })).toBeNull();
    expect(screen.getByRole("link", { name: /Airport hotels/ })).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "Find a Record" }), { target: { value: "nothing like it" } });
    expect(screen.getByText("Nothing found.")).toBeTruthy();
  });

  it("«Discuss» on an Open question opens the dialog with the question quoted", async () => {
    mount(<SpacePage space="travel" read={vi.fn().mockResolvedValue({ head: "h", space: SPACE })} />);
    fireEvent.click(await screen.findByRole("button", { name: "Discuss the question" }));
    const dialog = await screen.findByRole("dialog", { name: "Discuss: open question" });
    expect(within(dialog).getByTestId("discuss-head").textContent).toBe(
      `Let's discuss an Open question from the Showcase. ${QUOTE_FRAME}\n> travel/AGENTS.md\n> Take the transit through the hub?`,
    );
  });
});

describe("RecordPage", () => {
  it("reads a Record: its head in words, its source apart, unknown fields at the bottom, links in the app", async () => {
    const read = vi.fn().mockResolvedValue({ head: "h", record: RECORD });
    const router = mount(<RecordPage space="travel" path="knowledge/transit.md" read={read} />);
    const words = await screen.findByTestId("head-words");
    expect(read).toHaveBeenCalledWith("travel", "knowledge/transit.md");
    expect(words.textContent).toBe("Knowledgeverified 21.09· practice diverges· stale after 21.12.2099· written by Claude Code");
    expect(screen.getByText("Source")).toBeTruthy();
    const source = screen.getByTestId("source");
    expect(source.textContent).toBe(RECORD.head.source);
    // only http(s) addresses are links (an image's address too, as a link, never loaded); markdown is not parsed
    expect([...source.querySelectorAll("a")].map((a) => a.getAttribute("href"))).toEqual([
      "https://evil.example/i.png",
      "https://example.org/rules",
    ]);
    expect(source.querySelectorAll("img")).toHaveLength(0);
    expect(within(screen.getByTestId("head-extra")).getByText("colour").nextSibling?.textContent).toBe("green");
    expect(document.querySelectorAll("main img, main [src]")).toHaveLength(0);
    expect(screen.getByText(/\[image: x\]/)).toBeTruthy();
    fireEvent.click(await screen.findByRole("link", { name: "passport" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/spaces/travel/r/profile/docs/passport.md"));
    expect(await screen.findByText("record travel/profile/docs/passport.md")).toBeTruthy();
  });

  it("«Discuss» starts a session in the Space's sphere naming the Record, then opens it", async () => {
    const start = vi.fn().mockResolvedValue({ session: "77889900" });
    const router = mount(
      <Discuss space="travel" topic={{ record: { path: "knowledge/transit.md", title: "Airport transfer" } }} onClose={() => undefined} start={start} />,
    );
    const dialog = await screen.findByRole("dialog", { name: "Discuss: record" });
    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "Is it still true?" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Start" }));
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    const [track, text, id, sphere] = start.mock.calls[0]!;
    expect([track, sphere]).toEqual(["notes", "travel"]);
    expect(text).toBe(`Let's discuss a Record from the Base. ${QUOTE_FRAME}\n> travel/knowledge/transit.md\n> Airport transfer\n\nIs it still true?`);
    expect(id).toMatch(/^face:/);
    await waitFor(() => expect(router.state.location.pathname).toBe("/s/77889900"));
  });

  it("says when a Record is too big or not there", async () => {
    mount(<RecordPage space="travel" path="knowledge/big.md" read={vi.fn().mockRejectedValue(new ApiError(413, "The Record is large (600 KB) — open it on a computer."))} />);
    expect(await screen.findByText("The Record is large (600 KB) — open it on a computer.")).toBeTruthy();
  });
});

describe("BaseMarkdown links", () => {
  it("opens only plain app routes in place; anything else is not an app link", async () => {
    const router = mount(
      <BaseMarkdown
        text={[
          "[up](/spaces/x/r/../../../p/1)",
          "[js](javascript:alert(1))",
          "[data](data:text/html,<script>alert(1)</script>)",
          "[by reference][r]",
          "",
          "[r]: /spaces/x/r/../../p/2",
        ].join(" ")}
      />,
    );
    await screen.findByText("up");
    for (const el of document.querySelectorAll("a, button")) {
      const href = el.getAttribute("href") ?? "";
      expect(href.startsWith("javascript:") || href.startsWith("data:"), href).toBe(false);
      if (el.textContent === "up" || el.textContent === "by reference") fireEvent.click(el);
    }
    await new Promise((r) => setTimeout(r, 20));
    expect(router.state.location.pathname).toBe("/");
  });

  it("opens a plain app route in place", async () => {
    const router = mount(<BaseMarkdown text="[passport](/spaces/travel/r/profile/p.md)" />);
    fireEvent.click(await screen.findByRole("link", { name: "passport" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/spaces/travel/r/profile/p.md"));
  });
});
