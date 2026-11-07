// «Today»'s tabs — «Today», «Tasks», «Schedule» (the week's grid) — and the operator's view: a card for each
// session's request, drawn by how its turn ended, answered in place, numbered while it waits, holding its place while
// the owner reads.
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Operator } from "@/components/operator";
import { api } from "@/lib/api";
import type { Feed, FeedRow, TurnEnding } from "@/lib/feed";
import { live } from "@/lib/live";
import { renumber } from "@/lib/operator";
import type { Plan } from "@/lib/plan";
import { todaySearch } from "@/lib/today";
import { TodayView } from "@/pages/today";

// «Schedule»'s grid is FullCalendar, which needs a real browser's layout: here it stands in as a mark.
vi.mock("@/components/plan/grid-tab", () => ({ GridTab: () => <p>the week's grid</p> }));

const NOW = 1_000_000;

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const router = createRouter({ routeTree: root.addChildren([home, one]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
  localStorage.clear();
});

const row = (key: string, action: string, ending: TurnEnding | null, over: Partial<FeedRow> = {}): FeedRow =>
  ({
    key, action, source: "session", session: key, track: "notes", sphere: null, sphere_title: null, sphere_icon: null, project: "work/acme",
    title: `Session ${key}`, since: NOW - 600, essence: "", ref: key, ending, ...over,
  }) as unknown as FeedRow;

const ASK: TurnEnding = {
  kind: "ask",
  request: "Find a host for the Acme site under €20 a month",
  why_owner: "money",
  context: "Three hosts fit; the cheapest has no backups.",
  options: [
    { label: "Example Cloud, €12", effect: "I order it and move the site on Friday." },
    { label: "Wait for the quote", effect: "Nothing is bought." },
  ],
  without: "the launch moves a week",
};
const ENDINGS: Record<string, [string, TurnEnding]> = {
  ask: ["answer", ASK],
  approve: ["decide", { kind: "approve", request: "Publish the launch post", why_owner: "external", will: "Publish «We moved» on the blog.", items: ["blog.example.com/we-moved"] }],
  step: ["step", { kind: "step", request: "Move the site", what: "Add the address record at the registrar.", where: "The registrar's DNS page", command: "example.com. 3600 IN A 203.0.113.10" }],
  picture: ["read", { kind: "look", request: "Redo the header", artifact: { path: "out/header.png", title: "header.png", src: "/api/files/f1", kind: "image" }, checks: ["The logo on a phone"] }],
  brief: ["read", { kind: "look", request: "One PDF for the designer", artifact: { path: "out/brief.pdf", title: "brief.pdf", src: "/api/files/f2/view", kind: "pdf" } }],
  done: ["read", { kind: "done", request: "Make the README say how to run the site", what: "The README says how to run and deploy.", next: "Read it.", next_by: "you" }],
};

function feedOf(rows: FeedRow[]): Feed {
  return { schema: 1, count: rows.length, bot: "", assistant_at: null, groups: [{ action: "read", title: "Read", rows }], deadlines: [], chips: [] };
}
const ALL = Object.entries(ENDINGS).map(([key, [action, e]]) => row(key, action, e));

describe("«Today»'s address", () => {
  it("lands an old link to the day, the week or the grid's «Plan» on «Schedule»", () => {
    expect(todaySearch({})).toStrictEqual({ tab: undefined });
    expect(todaySearch({ tab: "tasks", view: "week" })).toStrictEqual({ tab: "tasks" });
    expect(todaySearch({ tab: "day" })).toStrictEqual({ tab: "schedule" });
    expect(todaySearch({ tab: "week" })).toStrictEqual({ tab: "schedule" });
    expect(todaySearch({ tab: "plan" })).toStrictEqual({ tab: "schedule" });
    expect(todaySearch({ tab: "schedule", view: "week" })).toStrictEqual({ tab: "schedule" });
    expect(todaySearch({ tab: "waiting" })).toStrictEqual({ tab: undefined });
  });
});

function plan(): Plan {
  return {
    now: NOW,
    tz: "UTC",
    tasks: {
      rows: [{ id: "t1", fields: { state: "open", title: "Send Acme the draft", project: "work/acme" }, version: 1, created: NOW - 9000, updated: NOW - 9000 }],
      rev: null, space: "home", goals: [], missing: false, blocks: [],
    },
    calendar: { events: [], at: NOW, failed: 0, stale: false },
    reminders: [],
    plan: { at: null, plans: {}, holds: [], said: {}, window: { zone: "UTC", opens: "09:00", closes: "23:00" } },
  };
}

describe("«Today»'s tabs", () => {
  it("test_today_tasks_and_schedule_are_tabs_of_their_own", async () => {
    const onTab = vi.fn();
    const p = { plan: plan(), error: null, reload: () => undefined };
    const first = mount(<TodayView feed={feedOf([])} sessions={[]} spheres={[]} since={NOW} now={NOW} tab="waiting" onTab={onTab} plan={p} />);
    const tabs = await screen.findByRole("tablist", { name: "Today" });
    expect(within(tabs).getAllByRole("tab").map((t) => t.textContent)).toEqual(["Today", "Tasks1", "Schedule"]);
    expect(screen.queryByText("Send Acme the draft")).toBeNull(); // the tasks are not among what waits
    fireEvent.click(within(tabs).getByRole("tab", { name: /^Tasks/ }));
    expect(onTab).toHaveBeenCalledWith("tasks");
    first.unmount();

    // «Schedule» is the week's grid: no «Day · Week» switch over it any more
    mount(<TodayView feed={feedOf([])} sessions={[]} spheres={[]} since={NOW} now={NOW} tab="schedule" onTab={onTab} plan={p} />);
    const schedule = await screen.findByRole("tab", { name: "Schedule" });
    expect(schedule.getAttribute("aria-selected")).toBe("true");
    expect(await screen.findByText("the week's grid")).toBeTruthy();
    expect(screen.queryByRole("group", { name: "Schedule" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Week" })).toBeNull();
  });
});

describe("the operator", () => {
  it("test_operator_draws_each_card_by_how_its_turn_ended", async () => {
    mount(<Operator feed={feedOf(ALL)} spheres={[]} now={NOW} />);
    const cards = await screen.findAllByTestId("operator-card");
    expect(cards.map((c) => c.getAttribute("data-kind"))).toEqual(["ask", "approve", "step", "look", "look", "done"]);
    const [ask, approve, step, picture, brief, done] = cards as [HTMLElement, HTMLElement, HTMLElement, HTMLElement, HTMLElement, HTMLElement];

    // «You asked: …» comes first on every card
    for (const card of cards) expect(card.querySelector("p")?.textContent).toMatch(/^You asked: /);

    // short numbers on the cards that wait; a finished turn is only to read
    expect(within(ask).getByText("#1")).toBeTruthy();
    expect(within(picture).getByText("#4")).toBeTruthy();
    expect(within(done).queryByText(/^#\d/)).toBeNull();
    expect(ask.textContent).toMatch(/work \/ acme · Session ask · asks · 10 min/);
    // the session's title on the top line is the way into its own screen
    expect(within(ask).getByRole("link", { name: "Session ask" }).getAttribute("href")).toMatch(/^\/s\//);

    const options = within(within(ask).getByRole("group", { name: "Options" })).getAllByRole("button");
    expect(options.map((o) => o.textContent)).toEqual([
      "Example Cloud, €12RecommendedI order it and move the site on Friday.",
      "Wait for the quoteNothing is bought.",
    ]);
    expect(within(ask).getByText("Yours to decide: money · With no answer: the launch moves a week")).toBeTruthy();
    expect(within(ask).getByRole("textbox", { name: "Your message" })).toBeTruthy();

    expect(within(approve).getByText("Publish «We moved» on the blog.")).toBeTruthy();
    expect(within(within(approve).getByRole("list", { name: "Where" })).getByText("blog.example.com/we-moved")).toBeTruthy();
    expect(within(approve).getByRole("button", { name: "Allow" })).toBeTruthy();

    expect(within(step).getByText("example.com. 3600 IN A 203.0.113.10").tagName).toBe("PRE");
    expect(within(step).getByText("The registrar's DNS page")).toBeTruthy();
    expect(within(step).getByRole("button", { name: "Copy" })).toBeTruthy();

    // the thing to look at is open in place: the picture itself, the PDF in its frame
    expect(within(picture).getByRole("img", { name: "header.png" }).getAttribute("src")).toBe("/api/files/f1");
    expect(within(picture).getByText("The logo on a phone")).toBeTruthy();
    expect(within(brief).getByTitle("brief.pdf").getAttribute("src")).toBe("/api/files/f2/view");

    expect(within(done).getByText("The README says how to run and deploy.")).toBeTruthy();
    expect(within(done).getByText("Your move:").parentElement?.textContent).toBe("Your move: Read it.");
  });

  it("test_an_ask_is_answered_by_its_option_from_the_card", async () => {
    const input = vi.spyOn(api, "input").mockResolvedValue({ say: "Accepted." } as never);
    mount(<Operator feed={feedOf([row("ask", "answer", ASK)])} spheres={[]} now={NOW} />);
    const card = await screen.findByTestId("operator-card");
    fireEvent.click(within(card).getByRole("button", { name: /^Wait for the quote/ }));
    expect(await within(card).findByText("You: «Wait for the quote»")).toBeTruthy();
    expect(input).toHaveBeenCalledWith("ask", "Wait for the quote");
    expect(within(card).queryByRole("group", { name: "Options" })).toBeNull();
  });

  it("test_operator_cards_hold_their_place_while_he_reads", async () => {
    let setFeed: (f: Feed) => void = () => undefined;
    function Harness() {
      const [feed, set] = useState(feedOf(ALL.slice(0, 3)));
      setFeed = set;
      return <Operator feed={feed} spheres={[]} now={NOW} />;
    }
    mount(<Harness />);
    const titles = async () => (await screen.findAllByTestId("operator-card")).map((c) => c.getAttribute("aria-label"));
    expect(await titles()).toEqual(["Session ask", "Session approve", "Session step"]);

    // the ask is answered and leaves the feed; a new card comes before the others
    act(() => setFeed(feedOf([row("new", "answer", { ...ASK, request: "Another" }), ...ALL.slice(1, 3)])));
    expect(await titles()).toEqual(["Session ask", "Session approve", "Session step"]);
    const ask = screen.getAllByTestId("operator-card")[0]!;
    expect(ask.textContent).toContain("answered or gone");
    expect(within(ask).queryByText("#1")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "New: 1 — show" }));
    expect(await titles()).toEqual(["Session new", "Session approve", "Session step"]);
    // its number is a new one: «one» was the ask's a moment ago
    expect(within(screen.getAllByTestId("operator-card")[0]!).getByText("#4")).toBeTruthy();
  });

  it("keeps a number while its card waits, and a freed one for half an hour", () => {
    const first = renumber({}, ["a", "b"], NOW);
    expect(first).toEqual({ a: { n: 1 }, b: { n: 2 } });
    const later = renumber(first, ["b", "c"], NOW + 60);
    expect(later).toEqual({ a: { n: 1, freed: NOW + 60 }, b: { n: 2 }, c: { n: 3 } });
    expect(renumber(later, ["b", "c", "d"], NOW + 31 * 60)).toEqual({ b: { n: 2 }, c: { n: 3 }, d: { n: 1 } });
  });

  it("test_a_computers_session_is_answered_on_its_card_to_that_computer", async () => {
    const answer = vi.spyOn(api, "answerComputer").mockResolvedValue({ answer: "a1" });
    const input = vi.spyOn(api, "input");
    const computer = { device: "laptop", id: "local_q", link: "claude://claude.ai/epitaxy/local_q" };
    const ask: TurnEnding = {
      kind: "ask",
      request: "Pick a plan",
      why_owner: "goal",
      context: "Two found",
      options: [
        { label: "A", effect: "cheaper" },
        { label: "B", effect: "faster" },
      ],
    };
    const rows = [
      row("ending:laptop/local_q", "answer", ask, { source: "computer", session: null, title: "Plan", computer }),
      row("reply:laptop/local_w", "reply", null, { source: "computer", session: null, title: "Waits", essence: "", computer: { device: "laptop", id: "local_w" } }),
      row("ending:laptop/local_d", "answer", ask, { source: "computer", session: null, title: "Done", computer: { device: "laptop", id: "local_d" }, answered: "B" }),
    ];
    mount(<Operator feed={feedOf(rows)} spheres={[]} now={NOW} />);
    const [first, second, third] = await screen.findAllByTestId("operator-card");
    // no way out to the computer: the card itself takes the answer
    expect(within(first!).queryByRole("link", { name: /on laptop/ })).toBeNull();
    expect(within(first!).queryByRole("link", { name: /Open the session/ })).toBeNull();
    for (const option of within(first!).getAllByRole("button", { name: /^[AB]/ })) expect((option as HTMLButtonElement).disabled).toBe(false);
    expect(within(first!).getByRole("textbox", { name: "Your message" })).toBeTruthy();
    fireEvent.click(within(first!).getByRole("button", { name: /^A/ }));
    expect(await within(first!).findByText("You: «A»")).toBeTruthy();
    expect(answer).toHaveBeenCalledWith("laptop", "local_q", "A");
    expect(within(first!).queryByRole("group", { name: "Options" })).toBeNull();

    fireEvent.change(within(second!).getByRole("textbox", { name: "Your message" }), { target: { value: "Go on" } });
    fireEvent.click(within(second!).getByRole("button", { name: "Send" }));
    expect(await within(second!).findByText("You: «Go on»")).toBeTruthy();
    expect(answer).toHaveBeenCalledWith("laptop", "local_w", "Go on");

    // answered before: it says so and asks nothing again
    expect(within(third!).getByText("You: «B»")).toBeTruthy();
    expect(within(third!).queryByRole("group", { name: "Options" })).toBeNull();
    expect(within(third!).queryByRole("textbox")).toBeNull();
    expect(input).not.toHaveBeenCalled();
  });
});
