import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Inbox } from "@/components/inbox";
import { Toaster } from "@/components/toaster";
import { WaitingPane } from "@/components/waiting-sheet";
import { ApiError, api } from "@/lib/api";
import { DEBOUNCE_MS, type Feed, type FeedRow, FeedSource, feedSource, herLine, POLL_MS, type WaitingDetail, waited } from "@/lib/feed";
import { live } from "@/lib/live";
import { spacesApi } from "@/lib/spaces";
import { initialState, type State } from "@/lib/store";
import type { Approval } from "@/lib/types";

const NOW = 1_000_000;

const approval = (over: Partial<Approval> = {}): Approval => ({
  id: "5f3a9c21",
  session: "a1b2c3d4",
  kind: "approval",
  tool_use_id: "toolu_1",
  state: "shown",
  tool: "Bash",
  input: { command: "make test" },
  cwd: "/work/retinue",
  digest: "d",
  view: { head: ["Approval · retinue · session a1b2c3d4"] },
  created: NOW - 600,
  at: NOW - 600,
  ...over,
});

const row = (over: Partial<FeedRow>): FeedRow => ({
  key: "k",
  action: "read",
  source: "session",
  session: "a1b2c3d4",
  track: "notes",
  sphere: null,
  sphere_title: null,
  sphere_icon: null,
  title: "Anna's trip",
  since: NOW - 1200,
  essence: "Found three hotels",
  ref: "a1b2c3d4",
  ...over,
});

const question = approval({
  id: "q1",
  kind: "question",
  tool: "AskUserQuestion",
  input: { questions: [{ question: "Where to stay?", options: [{ label: "Centre" }, { label: "Outskirts" }] }] },
  view: { answers: {}, picks: {} } as unknown as Approval["view"],
});

const FEED: Feed = {
  schema: 1,
  count: 5,
  bot: "retinue_bot",
  assistant_at: NOW - 3 * 3600,
  groups: [
    { action: "answer", title: "Answer a question", rows: [row({ key: "q", action: "answer", source: "question", approval: question, essence: "Where to stay?" })] },
    {
      action: "decide",
      title: "Decide",
      rows: [row({ key: "a", action: "decide", source: "approval", approval: approval(), sphere: "travel", sphere_title: "Travel", sphere_icon: "✈️" })],
    },
    { action: "reply", title: "Reply", rows: [row({ key: "c", action: "reply" })] },
    { action: "read", title: "Read", rows: [row({ key: "r", session: "b2c3d4e5", title: "Post" })] },
    {
      action: "assistant",
      title: "From the assistant",
      rows: [row({ key: "w", action: "assistant", source: "chat", session: null, track: null, title: "Anna Petrova", essence: "reply: transfer" })],
    },
  ],
};

function mount(node: React.ReactNode) {
  const root = createRootRoute();
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>session opened</p> });
  const record = createRoute({ getParentRoute: () => root, path: "/spaces/$space/r/$", component: () => <p>record opened</p> });
  const fresh = createRoute({ getParentRoute: () => root, path: "/new", component: () => <p>new task</p> });
  const router = createRouter({ routeTree: root.addChildren([home, one, record, fresh]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

/** A desktop window: the list and the pane side by side. */
const wide = () =>
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("min-width"), addEventListener() {}, removeEventListener() {} }));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  live.stop();
});

const inbox = (feed: Feed | null) => <Inbox feed={feed} running={[]} done={[]} spheres={[]} now={NOW} />;
const rowNamed = (name: RegExp) => screen.findByRole("button", { name });

describe("Inbox", () => {
  it("lists what waits on one grid — what to do, who, how long — and opens each row whole in the pane", async () => {
    wide();
    vi.spyOn(live, "watch").mockResolvedValue(undefined);
    mount(inbox(FEED));
    const rows = await screen.findAllByTestId("inbox-row");
    expect(rows.map((r) => r.firstChild?.nextSibling?.textContent)).toEqual(["Answer", "Allow", "Reply", "Read", "Reply"]);
    expect(rows[4]!.textContent).toContain("transfer");
    expect(rows[4]!.textContent).not.toContain("reply:");

    fireEvent.click(rows[0]!);
    expect(await screen.findByRole("radio", { name: /Centre/ })).toBeTruthy();
    fireEvent.click(rows[1]!);
    await screen.findByTestId("approval-input");
    const decide = screen.getByRole("region", { name: "Anna's trip" });
    expect(within(decide).getByText(/Travel · waits 20 min/)).toBeTruthy();
    expect(within(decide).getByRole("button", { name: "Allow" })).toBeTruthy();
    expect(within(decide).getByTestId("approval-input").textContent).toBe("make test");
    fireEvent.click(rows[2]!);
    await screen.findByPlaceholderText("It waits for your message");
    const reply = screen.getByRole("region", { name: "Anna's trip" });
    expect(within(reply).getByRole("link", { name: /Open the session/ }).getAttribute("href")).toBe("/s/a1b2c3d4");
    expect(within(reply).getByPlaceholderText("It waits for your message")).toBeTruthy();
    expect(live.watch).toHaveBeenCalledWith("a1b2c3d4");
  });

  it("on a narrow screen opens a row over the list, and «All» brings the list back", async () => {
    mount(inbox(FEED));
    fireEvent.click((await screen.findAllByTestId("inbox-row"))[1]!);
    expect(await screen.findByTestId("approval-input")).toBeTruthy();
    expect(screen.queryAllByTestId("inbox-row")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    expect(await screen.findAllByTestId("inbox-row")).toHaveLength(5);
  });

  it("goes down and up the list by J and K", async () => {
    wide();
    vi.spyOn(live, "watch").mockResolvedValue(undefined);
    mount(inbox(FEED));
    await screen.findAllByTestId("inbox-row");
    fireEvent.keyDown(window, { key: "j" });
    await waitFor(() => expect(screen.getAllByTestId("inbox-row")[1]!.getAttribute("aria-current")).toBe("true"));
    fireEvent.keyDown(window, { key: "k" });
    await waitFor(() => expect(screen.getAllByTestId("inbox-row")[0]!.getAttribute("aria-current")).toBe("true"));
  });

  it("says when nothing waits, and waits for the first read", async () => {
    const { unmount } = mount(inbox(null));
    expect(await screen.findByText("Loading…")).toBeTruthy();
    unmount();
    mount(inbox({ ...FEED, count: 0, groups: [] }));
    expect(await screen.findByText("Nothing is waiting for you.")).toBeTruthy();
  });

  it("reads her title in its parts: the verb, the words, a due the router did not mask", () => {
    expect(herLine("pay: Hosting is blocked · deadline Fri 13.11 (+3)")).toEqual({ verb: "Pay", text: "Hosting is blocked", due: "Fri 13.11" });
    expect(herLine("do: the invoice · deadline ‹number› (+1)")).toEqual({ verb: "Do", text: "the invoice", due: "" });
    expect(herLine("a reply draft waits for «Send»")).toEqual({ verb: "", text: "a reply draft waits for «Send»", due: "" });
  });
});

describe("Her row", () => {
  const T = "0123456789abcdef0123456789abcdef";
  const GMAIL = "https://mail.google.com/mail/?authuser=me@example.com#all/18c2f0a1b2c3d4e5";
  const hers = (rows: FeedRow[], over: Partial<Feed> = {}): Feed => ({
    ...FEED,
    count: rows.length,
    groups: [{ action: "assistant", title: "From the assistant", rows }],
    ...over,
  });
  const pane = (r: FeedRow) => <WaitingPane row={r} bot="" now={NOW} />;
  const letter = row({ key: "w", action: "assistant", source: "letter", session: null, track: null, title: "Acme Hosting", essence: "pay: the invoice ‹number›", ref: "letter:1", t: T, sphere_title: "Home" });
  const detail: WaitingDetail = {
    kind: "letter",
    who: "Acme Hosting",
    account: "me@example.com",
    asks: [
      { need: "pay", text: "The invoice for May.\nBy Friday.", deadline: "Friday", at: NOW - 3600, open: { url: GMAIL, label: "Open in Gmail" } },
      { need: "pay", text: "A reminder.", deadline: "", at: NOW - 7200, open: { url: GMAIL, label: "Open in Gmail" } },
    ],
    more: 1,
    open: { url: GMAIL, label: "Open in Gmail" },
  };

  it("closes from its pane: the row's token goes with «Done», and the toast takes it back", async () => {
    vi.spyOn(api, "waitingDetail").mockResolvedValue({ detail });
    const press = vi.spyOn(api, "waitingPress").mockResolvedValue({ ok: true });
    mount(
      <>
        {pane(letter)}
        <Toaster />
      </>,
    );
    const open = await screen.findByRole("region", { name: "Acme Hosting" });
    fireEvent.click(within(open).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(press).toHaveBeenCalledWith(T, "done"));
    const toast = await screen.findByRole("status");
    expect(toast.textContent).toBe("DoneUndo");
    fireEvent.click(within(toast).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(press).toHaveBeenLastCalledWith(T, "open"));
  });

  it("leaves a person's row to Tasks: the queue shows the draft, not the ask", async () => {
    const draft = row({ key: "d", action: "assistant", source: "draft", session: null, track: null, title: "Anna Petrova", essence: "a reply draft waits for «Send»", ref: "draft:0000000c" });
    mount(inbox(hers([letter, draft])));
    expect(await rowNamed(/Anna Petrova/)).toBeTruthy();
    expect(screen.queryAllByTestId("inbox-row").map((r) => r.textContent).join()).not.toContain("Acme Hosting");
  });

  it("counts what closed and what ran out under the list", async () => {
    mount(inbox(hers([letter], { closed_today: 5, expired_week: 2, assistant_at: NOW - 60 })));
    expect(await screen.findByText("Closed today 5 · ran out unmarked this week 2")).toBeTruthy();
  });

  it("opens with what the list says, then fills in what only the router has, and goes where the letter is", async () => {
    let answer: (value: { detail: WaitingDetail }) => void = () => undefined;
    const load = vi.spyOn(api, "waitingDetail").mockReturnValue(new Promise((done) => (answer = done)));
    mount(pane(letter));
    const open = await screen.findByRole("region", { name: "Acme Hosting" });
    expect(load).toHaveBeenCalledWith("letter:1", T);
    expect(within(open).getByText("the invoice ‹number›")).toBeTruthy();
    expect(within(open).getByRole("status").textContent).toBe("Asking the assistant…");
    answer({ detail });
    const gmail = await within(open).findByRole("link", { name: /Open in Gmail/ });
    expect(gmail.getAttribute("href")).toBe(GMAIL);
    expect(gmail.getAttribute("rel")).toBe("noopener noreferrer");
    expect(within(open).getByText("What they ask (3)")).toBeTruthy();
    expect(within(open).getByText(/The invoice for May\./).textContent).toBe("The invoice for May.\nBy Friday.");
    expect(within(open).getByText("And 1 older — in Telegram.")).toBeTruthy();
    expect(within(open).getByText(/to me@example.com/)).toBeTruthy();
  });

  it("asks the assistant in a session of its own: the row's context waits in a new task, in the row's sphere", async () => {
    vi.spyOn(api, "waitingDetail").mockResolvedValue({ detail });
    sessionStorage.clear();
    mount(pane({ ...letter, sphere: "home" }));
    const open = await screen.findByRole("region", { name: "Acme Hosting" });
    await within(open).findByRole("link", { name: /Open in Gmail/ });
    fireEvent.click(within(open).getByRole("button", { name: "Ask the assistant" }));
    expect(await screen.findByText("new task")).toBeTruthy();
    expect(sessionStorage.getItem("retinue-new-draft")).toBe(
      "About a letter from Acme Hosting in «Waiting for you»:\n> pay: the invoice ‹number›\n> The invoice for May. By Friday.\n" +
        `> A reminder.\n> ${GMAIL}\n\n`,
    );
    expect(sessionStorage.getItem("retinue-new-sphere")).toBe("home");
  });

  it("puts it off from the pane too", async () => {
    vi.spyOn(api, "waitingDetail").mockResolvedValue({ detail });
    const press = vi.spyOn(api, "waitingPress").mockResolvedValue({ ok: true });
    mount(pane(letter));
    fireEvent.click(await screen.findByRole("button", { name: "Tomorrow" }));
    await waitFor(() => expect(press).toHaveBeenCalledWith(T, "later"));
  });

  it("draws no link of a shape the router does not make", async () => {
    const odd = { ...detail, open: { url: "javascript:alert(1)", label: "Open in Gmail" }, asks: [{ ...detail.asks[0]!, open: { url: "https://evil.example", label: "Open the chat" } }] };
    vi.spyOn(api, "waitingDetail").mockResolvedValue({ detail: odd as WaitingDetail });
    mount(pane(letter));
    const open = await screen.findByRole("region", { name: "Acme Hosting" });
    await within(open).findByText(/The invoice for May/);
    expect(within(open).queryByRole("link", { name: /Gmail|chat/ })).toBeNull();
  });

  it("says a silent router plainly and asks again on «Try again»; an outdated row refreshes the list", async () => {
    const load = vi
      .spyOn(api, "waitingDetail")
      .mockRejectedValueOnce(new ApiError(504, "The assistant's side did not answer in time."))
      .mockRejectedValueOnce(new ApiError(410, "The assistant no longer has this row."));
    const refresh = vi.spyOn(feedSource, "refresh").mockResolvedValue();
    mount(pane(letter));
    const open = await screen.findByRole("region", { name: "Acme Hosting" });
    expect((await within(open).findByRole("alert")).textContent).toContain("did not answer");
    fireEvent.click(within(open).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    expect((await within(open).findByText(/out of date/)).textContent).toContain("refreshed");
    expect(within(open).queryByRole("button", { name: "Done" })).toBeNull();
    expect(refresh).toHaveBeenCalled();
  });

  it("shows a draft's text with «Send in Telegram» first", async () => {
    const load = vi.spyOn(api, "waitingDetail").mockResolvedValue({
      detail: { kind: "draft", who: "Anna Petrova", account: "me@example.com", asks: [], more: 0, open: { url: GMAIL, label: "Open in Gmail" },
        draft: { text: "Hello Anna,\n\nYes, Friday.", channel: "mail", subject: "Re: the trip", expires: NOW + 3600, state: "shown" } },
    });
    const draft = row({ key: "d", action: "assistant", source: "draft", session: null, track: null, title: "Anna Petrova", essence: "a reply draft waits for «Send»", ref: "draft:0000000c" });
    mount(inbox(hers([draft])));
    fireEvent.click(await rowNamed(/Anna Petrova/));
    const open = await screen.findByRole("region", { name: "Anna Petrova" });
    expect(load).toHaveBeenCalledWith("draft:0000000c", undefined);
    expect((await within(open).findByText(/Hello Anna/)).textContent).toBe("Hello Anna,\n\nYes, Friday.");
    expect(within(open).getAllByRole("link").map((a) => a.textContent)).toEqual(["Send in Telegram", "Open the thread"]);
    expect(within(open).queryByRole("button", { name: "Done" })).toBeNull();
  });

  it("opens a base's date with its Record in the pane, asking the router nothing", async () => {
    const date = row({ key: "dl", action: "assistant", source: "deadline", session: null, track: null, title: "The Base", essence: "tomorrow: Pay for the internet — due", ref: "deadline:1" });
    const load = vi.spyOn(api, "waitingDetail");
    const read = vi.spyOn(spacesApi, "record").mockResolvedValue({
      head: "h",
      record: { space: "home", path: "journal/2026-05-01-internet.md", nature: "journal", title: "Pay for the internet", head: {}, bad_head: false, body: "The bill comes on the 1st." },
    });
    mount(inbox(hers([date], { deadlines: [{ id: "deadline:1", when: "1970-01-13", title: "tomorrow: Pay for the internet — due", sphere: "home", sphere_title: "Home", sphere_icon: null, path: "home/journal/2026-05-01-internet.md" }] })));
    fireEvent.click(await rowNamed(/Pay for the internet/));
    expect(await screen.findByText("The bill comes on the 1st.")).toBeTruthy();
    expect(read).toHaveBeenCalledWith("home", "journal/2026-05-01-internet.md");
    expect(screen.getByText(/Home · due ·/)).toBeTruthy();
    expect(load).not.toHaveBeenCalled();
  });
});

describe("FeedSource", () => {
  function fakeLive() {
    let state: State = initialState;
    const listeners = new Set<() => void>();
    return {
      getState: () => state,
      subscribe: (l: () => void) => {
        listeners.add(l);
        return () => listeners.delete(l);
      },
      set(next: Partial<State>) {
        state = { ...state, ...next };
        for (const l of listeners) l();
      },
    };
  }

  it("reads on start, again once after a burst of session, card or summary changes, and every minute", async () => {
    vi.useFakeTimers();
    const load = vi.fn().mockResolvedValue(FEED);
    const source = new FeedSource({ load, setTimer: setTimeout, clearTimer: clearTimeout });
    const fake = fakeLive();
    source.start(fake);
    await vi.waitFor(() => expect(source.feed).toBe(FEED));
    expect(load).toHaveBeenCalledTimes(1);

    fake.set({ sessions: {} });
    fake.set({ approvals: {} });
    fake.set({ summaries: {} });
    fake.set({ deltas: { a: "writing…" } });
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS + 10);
    expect(load).toHaveBeenCalledTimes(2);
    fake.set({ deltas: { a: "writing more…" } });
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS + 10);
    expect(load).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(load).toHaveBeenCalledTimes(3);

    load.mockRejectedValueOnce(new Error("offline"));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(source.feed).toBe(FEED);
    source.stop();
    await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    expect(load).toHaveBeenCalledTimes(4);
    expect(source.feed).toBeNull();
  });
});

describe("waited", () => {
  it("says minutes, hours, days", () => {
    expect([waited(NOW - 30, NOW), waited(NOW - 3 * 3600, NOW), waited(NOW - 2 * 86400, NOW)]).toEqual(["1 min", "3 h", "2 d"]);
  });
});
