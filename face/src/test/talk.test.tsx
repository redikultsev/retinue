// Talking to Retinue: the header's numbered answers («1 — yes, 2 — the second one»), the chat with the voice's main
// session beside every screen (open or closed as the device left it, «The voice is not configured» without one), the
// microphone on an Operator card's answer, and a project's own «Operator».
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Operator } from "@/components/operator";
import { ChatPane } from "@/components/talk";
import { api } from "@/lib/api";
import type { Feed, FeedRow } from "@/lib/feed";
import { live } from "@/lib/live";
import { NUMBERS_KEY } from "@/lib/operator";
import { rowWithin } from "@/lib/projects";
import { CHAT_KEY, chatWith, forgetChat, parseAnswers, reach, setChatOpen, voiceSession } from "@/lib/talk";
import type { Session } from "@/lib/types";

const NOW = 1_000_000;

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const router = createRouter({ routeTree: root.addChildren([home, one]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: NOW, title: `Session ${id}`, waiting: 0, ...over,
});

const row = (key: string, over: Partial<FeedRow> = {}): FeedRow =>
  ({
    key, action: "reply", source: "session", session: key, track: "notes", sphere: null, sphere_title: null, sphere_icon: null,
    project: null, title: `Session ${key}`, since: NOW - 600, essence: "It waits", ref: key, ...over,
  }) as unknown as FeedRow;

beforeEach(() => {
  live.meta = { schema: 1, owner_ok: true, face_origin: "", tracks: [{ name: "notes", lane: "private", kind: "private" }], spheres: [], voice: "life", speech: true };
});

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
  live.meta = null;
  localStorage.clear();
  forgetChat();
});

describe("numbered answers", () => {
  it("reads only the owner's leading «<number> —» addresses", () => {
    expect(parseAnswers("1 — the recommended one, 2 — yes")).toEqual([
      { n: 1, words: "the recommended one" },
      { n: 2, words: "yes" },
    ]);
    expect(parseAnswers("3 – wait for the quote. 4 - no")).toEqual([
      { n: 3, words: "wait for the quote" },
      { n: 4, words: "no" },
    ]);
    expect(parseAnswers("2 — take the 12 — the cheaper one"), "a number inside an answer stays in it").toEqual([
      { n: 2, words: "take the 12 — the cheaper one" },
    ]);
    expect(parseAnswers("1 — book it for 3-4 days"), "a hyphen with no spaces is no address").toEqual([{ n: 1, words: "book it for 3-4 days" }]);
    for (const message of ["What is left before the trip?", "Book 2 — no, 3 nights", "1—", "the 2 — second"]) expect(parseAnswers(message), message).toEqual([]);
  });

  it("finds each card by the number «Today» gave it: one to send, one decided on its own card, one no card has", () => {
    const rows = [row("r1"), row("c1", { action: "decide", approval: { id: "a1" } as FeedRow["approval"] })];
    const numbers = { r1: { n: 1 }, c1: { n: 2 }, gone: { n: 3, freed: NOW } };
    const out = reach(parseAnswers("1 — yes, 2 — allow it, 3 — and this"), numbers, rows);
    expect(out.map((a) => [a.n, a.how, a.row?.key ?? null])).toEqual([
      [1, "send", "r1"],
      [2, "card", "c1"],
      [3, "none", null],
    ]);
  });
});

describe("the chat with Retinue", () => {
  it("is the voice's main session, its words and its box; what is sent goes into it; the device keeps it open", async () => {
    live.dispatch({ type: "list", snapshot: { schema: 1, cursor: 1, sessions: [session("v1", { sphere: "life", main: true }), session("o1", { sphere: "life" })] } });
    expect(voiceSession(Object.values(live.getState().sessions), "life")?.id).toBe("v1");
    vi.spyOn(live, "watch").mockResolvedValue(undefined);
    const input = vi.spyOn(api, "input").mockResolvedValue({ say: "Accepted." });
    setChatOpen(true);
    expect(localStorage.getItem(CHAT_KEY)).toBe("1");
    mount(<ChatPane />);
    const pane = await screen.findByRole("complementary", { name: "Talk to Retinue" });
    expect(within(pane).getByRole("link", { name: "Open its session" }).getAttribute("href")).toBe("/s/v1");
    fireEvent.change(within(pane).getByRole("textbox", { name: "Message to Retinue" }), { target: { value: "What is left before the trip?" } });
    fireEvent.click(within(pane).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(input).toHaveBeenCalledWith("v1", "What is left before the trip?", expect.stringMatching(/^face:/), []));
    expect(within(pane).getByRole("button", { name: /Dictate|Say it in English|microphone/ })).toBeTruthy();
    fireEvent.click(within(pane).getByRole("button", { name: "Close the chat" }));
    expect(localStorage.getItem(CHAT_KEY)).toBe("0");
  });

  it("starts the voice's main session with the first message when there is none, and takes the header's words in its box", async () => {
    const toMain = vi.spyOn(api, "toMain").mockResolvedValue({ session: "v9" });
    vi.spyOn(live, "watch").mockResolvedValue(undefined);
    chatWith("Remind me about the tickets on Friday");
    mount(<ChatPane />);
    const box = (await screen.findByRole("textbox", { name: "Message to Retinue" })) as HTMLTextAreaElement;
    expect(box.value).toBe("Remind me about the tickets on Friday"); // in the box to read, not sent by itself
    expect(toMain).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(toMain).toHaveBeenCalledWith("life", "Remind me about the tickets on Friday", expect.stringMatching(/^face:/), []));
  });

  it("says the voice is not configured when the platform has none", async () => {
    live.meta = { ...live.meta!, voice: null };
    mount(<ChatPane />);
    expect(await screen.findByText("The voice is not configured")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

describe("a project's «Operator»", () => {
  it("shows only its own and its sub-projects' cards, numbered as on «Today», each with a microphone by its answer", async () => {
    localStorage.setItem(NUMBERS_KEY, JSON.stringify({ a: { n: 1 }, b: { n: 2 }, c: { n: 3 } }));
    const feed: Feed = {
      schema: 1, count: 3, bot: "", assistant_at: null, deadlines: [], chips: [],
      groups: [{ action: "reply", title: "Reply", rows: [
        row("a", { project: "work/acme", title: "A host for the site" }),
        row("b", { project: "travel", title: "A flat in Lisbon" }),
        row("c", { project: "work/acme/site", title: "The new header" }),
      ] }],
    };
    const known = new Set(["work", "work/acme", "work/acme/site", "travel"]);
    mount(<Operator feed={feed} spheres={[]} now={NOW} filter={(r) => rowWithin(r, "work/acme", known)} />);
    const cards = await screen.findAllByTestId("operator-card");
    expect(cards.map((c) => c.getAttribute("aria-label"))).toEqual(["A host for the site", "The new header"]);
    expect(within(cards[1]!).getByText("#3")).toBeTruthy(); // its number on «Today», not a new one
    expect(within(cards[0]!).getByRole("button", { name: "Dictate the answer" })).toBeTruthy();
  });
});
