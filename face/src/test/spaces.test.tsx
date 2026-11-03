// «Spaces»: the grid with its badges, a Space as a screen, a Record to read, «Discuss» — against a fake API.
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "@/lib/api";
import { live } from "@/lib/live";
import { QUOTE_FRAME, type RecordView, type SpaceView } from "@/lib/spaces";
import { BaseMarkdown } from "@/components/base-markdown";
import type { Session } from "@/lib/types";
import { Discuss, forgetRead, ProjectPage, RecordPage, SpacePage, SpacesPage } from "@/pages/spaces";

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
  const project = createRoute({ getParentRoute: () => root, path: "/spaces/$space/p/$", component: () => <p>project</p> });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const router = createRouter({
    routeTree: root.addChildren([page, spaces, space, project, record, one]),
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
      "Travel1 waiting2 working1 question",
      "Work3 questions",
      "Home",
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
  const PASSPORTED: SpaceView = {
    ...SPACE,
    passport: {
      why: "Trips without surprises: tickets, stays and documents in one place.",
      in_scope: "flights, trains, stays",
      not_in_scope: "visas → Documents",
    },
    projects: [{ name: "travel/lisbon", title: "Lisbon in May", questions: 1, tasks: 0 }],
  };

  it("test_a_project_opens_on_its_passport_then_what_is_his_then_its_sessions", async () => {
    live.dispatch({
      type: "list",
      snapshot: { schema: 1, cursor: 1, sessions: [
        session("t1", { sphere: "travel", state: "waiting", title: "Tickets for June" }),
        session("c1", { sphere: "work", title: "Quarterly report" }),
        session("l1", { sphere: "travel", project: "travel/lisbon", title: "A flat in the old town" }),
      ] },
    });
    mount(<SpacePage space="travel" read={vi.fn().mockResolvedValue({ head: "h", space: PASSPORTED })} />);
    const passport = await screen.findByRole("region", { name: "Passport" });
    // the header is one line: its name, what it is, its tabs — «Overview» first and open
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Travel");
    expect(screen.getByText("project · 1 sub-project")).toBeTruthy();
    // «Operator N»: what waits in it — before the feed is in, its sessions that wait, as its row on the left says
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Overview", "Records3", "Operator1"]);
    expect(screen.getByRole("tab", { name: "Overview" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText("1 waiting")).toBeTruthy();

    expect(within(passport).getByText("Trips without surprises: tickets, stays and documents in one place.")).toBeTruthy();
    expect([...passport.querySelectorAll("dt")].map((d) => d.textContent)).toEqual(["In scope", "Not in scope", "Rules"]);
    expect(within(passport).getByText("visas → Documents")).toBeTruthy();
    expect(within(passport).getByText("Live prices only")).toBeTruthy();
    expect(within(passport).getByRole("button", { name: "Edit with a session" })).toBeTruthy();

    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["Yours", "Sessions"]);
    const yours = within(screen.getByRole("list", { name: "Yours" }));
    expect(yours.getAllByRole("listitem").map((li) => li.querySelector("button")?.textContent)).toEqual(["Discuss", "Plan"]);
    expect(yours.getByRole("link", { name: "transit" }).getAttribute("href")).toBe("/spaces/travel/r/knowledge/transit.md");
    const listed = within(screen.getByRole("list", { name: "Sessions" }));
    expect(listed.getByRole("link", { name: /Tickets for June/ })).toBeTruthy();
    expect(listed.getByRole("link", { name: /Lisbon in May/ }).getAttribute("href")).toBe("/spaces/travel/p/lisbon");
    expect(listed.getByRole("link", { name: /Lisbon in May/ }).textContent).toContain("sub-project · 1 session · 1 question");
    expect(screen.queryByText("Quarterly report")).toBeNull();
    expect(screen.queryByText("A flat in the old town"), "a sub-project's session is on its own page").toBeNull();
    expect(screen.getByText("Deadlines")).toBeTruthy();
    expect(screen.queryByText("**knowledge/**")).toBeNull();

    // «Records»: one list, its nature and its head on the second line, no folder lines
    fireEvent.click(screen.getByRole("tab", { name: "Records3" }));
    const records = within(screen.getByRole("list", { name: "Records" }));
    const transit = records.getByRole("link", { name: /Airport transfer/ });
    expect(transit.textContent).toBe("Airport transferKnowledge · verified 21 Sep · practice diverges");
    expect(transit.getAttribute("href")).toBe("/spaces/travel/r/knowledge/transit.md");
    expect(records.getByRole("link", { name: /Passport/ }).textContent).toBe("PassportProfile · until 1 Jan 2030");
    expect(screen.queryByText("docs/")).toBeNull();
    expect(screen.getByText("Knowledge 2 · Profile 1")).toBeTruthy();
  });

  it("finds a Record by its name once there are more than a few", async () => {
    const many = Array.from({ length: 6 }, (_, n) => ({ path: `knowledge/k${n}.md`, folder: "", title: `Note ${n}` }));
    const big: SpaceView = { ...SPACE, records: { ...SPACE.records, knowledge: [...SPACE.records.knowledge, ...many] } };
    mount(<SpacePage space="travel" read={vi.fn().mockResolvedValue({ head: "h", space: big })} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Records9" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Find a Record" }), { target: { value: "hotel" } });
    expect(screen.queryByRole("link", { name: /Airport transfer/ })).toBeNull();
    expect(screen.getByRole("link", { name: /Airport hotels/ })).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "Find a Record" }), { target: { value: "nothing like it" } });
    expect(screen.getByText("Nothing found.")).toBeTruthy();
  });

  it("«Discuss», «Plan» and «Edit with a session» open a session's first message on what they are about", async () => {
    mount(<SpacePage space="travel" read={vi.fn().mockResolvedValue({ head: "h", space: SPACE })} />);
    fireEvent.click(await screen.findByRole("button", { name: "Discuss the question" }));
    let dialog = await screen.findByRole("dialog", { name: "Discuss: open question" });
    expect(within(dialog).getByTestId("discuss-head").textContent).toBe(
      `Let's discuss an Open question from the Showcase. ${QUOTE_FRAME}\n> travel/AGENTS.md\n> Take the transit through the hub?`,
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Plan the task" }));
    dialog = await screen.findByRole("dialog", { name: "Plan a task" });
    expect(within(dialog).getByTestId("discuss-head").textContent).toBe(
      `Let's plan a Task from the Showcase: what it takes, the steps, by when. ${QUOTE_FRAME}\n> travel/AGENTS.md\n> Recheck the transit (travel/knowledge/transit.md)`,
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    // no passport yet: the card says so, and the same button writes one
    expect(screen.getByText(/No passport yet/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Write it with a session" }));
    dialog = await screen.findByRole("dialog", { name: "Edit the passport" });
    expect(within(dialog).getByTestId("discuss-head").textContent).toMatch(/^Let's edit this project's passport: the «## Why» section at the top of travel\/AGENTS\.md/);
  });
});

describe("ProjectPage", () => {
  const DEEP = "acme/clients/anna";
  const PROJECT: SpaceView = {
    name: DEEP, path: DEEP, parent: "acme/clients", title: "Anna Petrova", rules: "", marker: true,
    counts: { questions: 1, tasks: 0 },
    sections: [{ heading: "Open questions", kind: "questions", items: [{ text: "Renew the contract?", list: true }] }],
    records: { knowledge: [], profile: [], journal: [], artifacts: [] },
    files: [
      { path: "AGENTS.md", size: 120, kind: "markdown" },
      { path: "media/call.mp4", size: 2000, kind: "video" },
      { path: "media/voice.m4a", size: 500, kind: "audio" },
      { path: "photo.png", size: 1024, kind: "image" },
      { path: "scan.pdf", size: 80, kind: "pdf" },
    ],
    projects: [{ name: `${DEEP}/cases`, title: "Cases", questions: 2, tasks: 0 }],
  };

  it("test_a_projects_screen_lists_all_its_files_and_sub_projects", async () => {
    live.dispatch({
      type: "list",
      snapshot: { schema: 1, cursor: 1, sessions: [session("in1", { title: "Renewal letter" }), session("out1", { title: "Elsewhere" })] },
    });
    const read = vi.fn().mockResolvedValue({ head: "h", project: PROJECT, sessions: ["in1"] });
    const start = vi.spyOn(api, "newSession").mockResolvedValue({ session: "77889900" });
    mount(<ProjectPage path={DEEP} read={read} />);
    await screen.findByText("Renew the contract?");
    expect(read).toHaveBeenCalledWith(DEEP);
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Overview", "Files5", "Operator"]);
    expect(screen.getByRole("link", { name: /Cases/ }).getAttribute("href")).toBe(`/spaces/acme/p/clients/anna/cases`);
    expect(screen.getByRole("link", { name: "To the project above" }).getAttribute("href")).toBe("/spaces/acme/p/clients");
    expect(screen.getByRole("link", { name: /Renewal letter/ })).toBeTruthy();
    expect(screen.queryByText("Elsewhere")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Files5" }));
    const files = within(screen.getByRole("list", { name: "Project files" }));
    expect(files.getAllByRole("listitem").map((li) => li.querySelector(".font-medium")?.textContent)).toEqual(
      PROJECT.files!.map((f) => f.path),
    );
    expect(document.querySelector("img")?.getAttribute("src")).toBe(`/api/base/${DEEP}/photo.png`);
    expect(document.querySelector("video")?.getAttribute("src")).toBe(`/api/base/${DEEP}/media/call.mp4`);
    expect(document.querySelector("audio")?.getAttribute("src")).toBe(`/api/base/${DEEP}/media/voice.m4a`);
    const pdf = files.getAllByRole("listitem").find((li) => li.textContent?.includes("scan.pdf"))!;
    fireEvent.click(within(pdf).getByRole("button", { name: "Open" }));
    const frame = pdf.querySelector("iframe")!;
    expect(frame.getAttribute("src")).toBe(`/api/base-view/${DEEP}/scan.pdf`);
    expect(frame.hasAttribute("sandbox")).toBe(false);

    fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
    fireEvent.click(screen.getByRole("button", { name: "New session here" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "Draft the letter" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Start" }));
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    expect(start.mock.calls[0]![5]).toBe(DEEP);
  });
});

describe("RecordPage", () => {
  it("reads a Record: its head in words, its source apart, unknown fields at the bottom, links in the app", async () => {
    const read = vi.fn().mockResolvedValue({ head: "h", record: RECORD });
    const router = mount(<RecordPage space="travel" path="knowledge/transit.md" read={read} />);
    const words = await screen.findByTestId("head-words");
    expect(read).toHaveBeenCalledWith("travel", "knowledge/transit.md");
    expect(words.textContent).toBe("Knowledgeverified 21 Sep· practice diverges· stale after 21 Dec 2099· written by Claude Code");
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
