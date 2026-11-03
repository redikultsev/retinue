// Talking to Retinue: the header's numbered answers («1 — yes, 2 — the second one»), the chat with the assistant beside
// every screen (her conversation, the same as Telegram's; open or closed as the device left it), the microphone on an
// Operator card's answer, and a project's own «Operator».
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Operator } from "@/components/operator";
import { ChatPane } from "@/components/talk";
import { Toaster } from "@/components/toaster";
import { api } from "@/lib/api";
import type { Feed, FeedRow } from "@/lib/feed";
import { live } from "@/lib/live";
import { NUMBERS_KEY } from "@/lib/operator";
import { forgetProjects, setProjects } from "@/lib/projects";
import type { ChatRow } from "@/lib/contract";
import { CHAT_KEY, chatWith, forgetChat, parseAnswers, reach, setChatOpen, takeDraft, takeRow } from "@/lib/talk";

const NOW = 1_000_000;

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const router = createRouter({ routeTree: root.addChildren([home, one]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

const chatRow = (id: string, over: Partial<ChatRow> = {}): ChatRow => ({ id, at: NOW, by: "her", text: `Row ${id}`, rev: 1, ...over });

const row = (key: string, over: Partial<FeedRow> = {}): FeedRow =>
  ({
    key, action: "reply", source: "session", session: key, track: "notes", sphere: null, sphere_title: null, sphere_icon: null,
    project: null, title: `Session ${key}`, since: NOW - 600, essence: "It waits", ref: key, ...over,
  }) as unknown as FeedRow;

beforeEach(() => {
  live.meta = { schema: 1, owner_ok: true, face_origin: "", tracks: [{ name: "notes", lane: "private", kind: "private" }], spheres: [], speech: true };
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
  it("is the assistant's conversation: his rows, hers, the system's card; what he sends is his row at once", async () => {
    vi.spyOn(api, "chat").mockResolvedValue({
      schema: 1,
      draft: null,
      rows: [
        chatRow("telegram:1", { at: NOW - 60, by: "owner", text: "What is left for Friday?", via: "telegram" }),
        chatRow("a:1", { at: NOW - 50, text: "Two things: **the tickets** and the flat.", files: [{ id: "f".repeat(32), name: "plan.pdf", size: 2048, mime: "application/pdf" }, { id: null, name: "big.zip", size: 1, mime: "application/zip", why: "over 20 MB: not kept in the app" }] }),
        chatRow("s:1", { at: NOW - 40, by: "notice", text: "Add it to your tasks?", buttons: [["Add", "b".repeat(16)], ["Not now", "c".repeat(16)]] }),
      ],
    });
    const say = vi.spyOn(api, "chatSay").mockImplementation(async (text, id) => ({ ok: true, row: chatRow(`app:${id}`, { by: "owner", text, pending: true, rev: 0 }) }));
    const press = vi.spyOn(api, "chatPress").mockResolvedValue({ ok: true });
    setChatOpen(true);
    expect(localStorage.getItem(CHAT_KEY)).toBe("1");
    mount(
      <>
        <ChatPane />
        <Toaster />
      </>,
    );
    const pane = await screen.findByRole("complementary", { name: "Talk to Retinue" });
    const log = await within(pane).findByRole("log", { name: "The conversation" });
    expect(within(log).getByText("What is left for Friday?")).toBeTruthy();
    expect(within(log).getByText("In Telegram")).toBeTruthy();
    expect(within(log).getByText("the tickets")).toBeTruthy();
    expect(log.textContent, "her words as Markdown").not.toContain("**");
    expect(within(log).getByRole("link", { name: /plan\.pdf/ }).getAttribute("href")).toBe(`/api/chat/files/${"f".repeat(32)}`);
    expect(within(log).getByText("over 20 MB: not kept in the app")).toBeTruthy();
    expect(within(pane).queryByRole("button", { name: "Attach" })).toBeNull();

    fireEvent.click(within(log).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(press).toHaveBeenCalledWith("b".repeat(16), "s:1"));
    takeRow(chatRow("s:1", { at: NOW - 40, by: "notice", text: "Add it to your tasks?\n\n_Chosen: Add_", buttons: [], toast: "Added.", rev: 2 }));
    await waitFor(() => expect(within(log).queryByRole("button", { name: "Add" })).toBeNull());
    expect(await screen.findByText("Added.")).toBeTruthy();

    fireEvent.change(within(pane).getByRole("textbox", { name: "Message to Retinue" }), { target: { value: "And on Saturday?" } });
    fireEvent.click(within(pane).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(say).toHaveBeenCalledWith("And on Saturday?", expect.stringMatching(/^face:/)));
    expect(await within(log).findByText("And on Saturday?")).toBeTruthy();
    expect(within(log).getByText("Sending…")).toBeTruthy();
    expect(within(pane).getByRole("button", { name: /Dictate|Say it in English|microphone/ })).toBeTruthy();
    fireEvent.click(within(pane).getByRole("button", { name: "Close the chat" }));
    expect(localStorage.getItem(CHAT_KEY)).toBe("0");
  });

  it("shows what she is writing, replaced whole each time, until her row comes", async () => {
    vi.spyOn(api, "chat").mockResolvedValue({ schema: 1, rows: [chatRow("o:1", { by: "owner", text: "Plan my day" })], draft: null });
    mount(<ChatPane />);
    await screen.findByText("Plan my day");
    takeDraft({ text: "Looking at", at: NOW });
    takeDraft({ text: "Looking at your calendar", at: NOW });
    const draft = await screen.findByTestId("chat-draft");
    await waitFor(() => expect(draft.textContent).toContain("Looking at your calendar"));
    expect(draft.textContent).not.toContain("Looking atLooking");
    takeRow(chatRow("a:2", { at: NOW + 1, text: "Your day is planned." }));
    await waitFor(() => expect(screen.queryByTestId("chat-draft")).toBeNull());
    expect(screen.getByText("Your day is planned.")).toBeTruthy();
  });

  it("takes the header's words in its box, to be read and sent by the owner", async () => {
    vi.spyOn(api, "chat").mockResolvedValue({ schema: 1, rows: [], draft: null });
    const say = vi.spyOn(api, "chatSay").mockResolvedValue({ ok: true, row: chatRow("app:x", { by: "owner", text: "x" }) });
    chatWith("Remind me about the tickets on Friday");
    mount(<ChatPane />);
    const box = (await screen.findByRole("textbox", { name: "Message to Retinue" })) as HTMLTextAreaElement;
    expect(box.value).toBe("Remind me about the tickets on Friday"); // in the box to read, not sent by itself
    expect(say).not.toHaveBeenCalled();
    expect(await screen.findByText(/the same conversation as with the bot in Telegram/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(say).toHaveBeenCalledWith("Remind me about the tickets on Friday", expect.stringMatching(/^face:/)));
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
    setProjects(["work", "work/acme", "work/acme/site", "travel"].map((name) => ({ name, title: name, parent: "", questions: 0, tasks: 0 })));
    mount(<Operator feed={feed} spheres={[]} now={NOW} project="work/acme" />);
    const cards = await screen.findAllByTestId("operator-card");
    expect(cards.map((c) => c.getAttribute("aria-label"))).toEqual(["A host for the site", "The new header"]);
    expect(within(cards[1]!).getByText("#3")).toBeTruthy(); // its number on «Today», not a new one
    expect(within(cards[0]!).getByRole("button", { name: "Dictate the answer" })).toBeTruthy();
    forgetProjects();
  });
});
