import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { commentText } from "@/components/changes";
import { TranscriptItem } from "@/components/transcript/item";
import { foldSteps, placeCards, placeInputs, placeSummaries, Transcript } from "@/components/transcript/transcript";
import type { Approval, Input, Item, ItemData, Summary } from "@/lib/types";

const item = (data: ItemData, n = 0): Item => ({ ...data, session: "s1", turn: 0, n }) as Item;

describe("TranscriptItem", () => {
  it("renders an assistant message as markdown and the owner's as plain text", async () => {
    const { container } = render(<TranscriptItem item={{ kind: "message", data: { role: "assistant", text: "This is **important**" } }} />);
    expect((await within(container).findByText("important")).tagName).toMatch(/STRONG|SPAN/);
    render(<TranscriptItem item={{ kind: "message", data: { role: "user", text: "**not markdown**" } }} />);
    expect(screen.getByText("**not markdown**")).toBeTruthy();
  });

  it("collapses a tool call to one line with its command, expandable to input and result", () => {
    const command = `uv run pytest -q ${"tests/test_gate.py ".repeat(20)}`.trim();
    const { container } = render(
      <TranscriptItem item={{ kind: "tool", data: { id: "t", tool: "Bash", input: { command }, result: "12 passed", error: false } }} />,
    );
    const details = container.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(within(details.querySelector("summary")!).getByText("Bash")).toBeTruthy();
    expect(details.querySelector("summary")!.textContent).toContain("uv run pytest -q");
    fireEvent.click(details.querySelector("summary")!);
    expect(within(details).getByText("12 passed")).toBeTruthy();
    expect(within(details).getByText(command)).toBeTruthy();
  });

  it("groups reads with a count and marks a failed one", () => {
    render(
      <TranscriptItem
        item={{
          kind: "reads",
          data: {
            calls: [
              { id: "1", tool: "Read", input: { file_path: "/work/a.py" }, result: "x", error: false },
              { id: "2", tool: "Read", input: { file_path: "/work/b.py" }, result: "y", error: false },
              { id: "3", tool: "Grep", input: { pattern: "gate" }, result: "File does not exist.", error: true },
            ],
          },
        }}
      />,
    );
    expect(screen.getByText("Read: 3 calls")).toBeTruthy();
    expect(screen.getByText("Read ×2, Grep")).toBeTruthy();
  });

  it("shows an Edit as a small diff with counts", () => {
    const { container } = render(
      <TranscriptItem
        item={{ kind: "edit", data: { id: "e", tool: "Edit", path: "/work/retinue/gate.py", ok: true, error: "", old: "a\nb", new: "a\nc" } }}
      />,
    );
    expect(screen.getByText("/work/retinue/gate.py")).toBeTruthy();
    expect(screen.getByText("+1")).toBeTruthy();
    expect(screen.getByText("−1")).toBeTruthy();
    const rows = [...container.querySelectorAll(".bg-diff-add, .bg-diff-del")].map((r) => r.textContent);
    expect(rows).toEqual(["-b", "+c"]);
  });

  it("shows a TodoWrite plan with each step's state", () => {
    render(
      <TranscriptItem
        item={{
          kind: "plan",
          data: {
            id: "p",
            tool: "TodoWrite",
            todos: [
              { content: "Find", status: "completed" },
              { content: "Fix", status: "in_progress", activeForm: "Fixing" },
              { content: "Check", status: "pending" },
            ],
          },
        }}
      />,
    );
    expect(screen.getByText("Find").className).toContain("line-through");
    expect(screen.getByText("Fixing")).toBeTruthy();
    expect(screen.getByText("Check")).toBeTruthy();
  });

  it("shows a subagent with its own items and its answer", () => {
    render(
      <TranscriptItem
        item={{
          kind: "subagent",
          data: {
            id: "s",
            tool: "Task",
            description: "Find the cause",
            agent: "Explore",
            prompt: "Trace the card's path",
            items: [{ kind: "tool", data: { id: "i", tool: "Bash", input: { command: "ls" }, result: "a", error: false } }],
            result: "The cause is in gate.py",
          },
        }}
      />,
    );
    expect(screen.getByText(/Subagent · Explore: Find the cause/)).toBeTruthy();
    expect(screen.getByText("1 step")).toBeTruthy();
    expect(screen.getByText("Trace the card's path")).toBeTruthy();
  });

  it("shows a question with its options and the answer", () => {
    render(
      <TranscriptItem
        item={{
          kind: "question",
          data: { id: "q", questions: [{ question: "Where to stay?", options: [{ label: "Centre" }, { label: "Outskirts" }] }], answer: "Centre" },
        }}
      />,
    );
    expect(screen.getByText("Where to stay?")).toBeTruthy();
    expect(screen.getByText("Answer:")).toBeTruthy();
  });

  it("marks the end of a turn, and an error", () => {
    render(<TranscriptItem item={{ kind: "result", data: { stop_reason: "", is_error: true, text: "limit", deferred: null } }} />);
    expect(screen.getByText("The turn broke off with an error: limit")).toBeTruthy();
  });
});

describe("Transcript", () => {
  const card = (over: Partial<Approval>): Approval => ({
    id: "a",
    session: "s1",
    kind: "approval",
    tool_use_id: "",
    state: "shown",
    tool: "Bash",
    input: { command: "make test" },
    cwd: "/work",
    digest: "d",
    view: { head: ["Approval · code · session s1"] },
    created: 1,
    at: 1,
    ...over,
  });
  const items = [
    item({ kind: "tool", data: { id: "t1", tool: "Bash", input: { command: "make test" }, result: null, error: false } }, 0),
    item({ kind: "message", data: { role: "assistant", text: "next" } }, 1),
  ];

  it("folds consecutive calls into one line, never a call with a card, and breaks at an owner's message or a summary", () => {
    const bash = (n: number, id = `b${n}`) =>
      item({ kind: "tool", data: { id, tool: "Bash", input: { command: `step ${n}` }, result: "ok", error: n === 2 } }, n);
    const run = [
      bash(0), bash(1), bash(2),
      item({ kind: "message", data: { role: "assistant", text: "between" } }, 3),
      bash(4), bash(5, "carded"), bash(6), bash(7),
    ];
    const none = new Map<number, unknown>();
    expect(foldSteps(run, none, none, none).map((r) => [r.steps, r.at])).toEqual([
      [true, [0, 1, 2]], [false, [3]], [true, [4, 5, 6, 7]],
    ]);
    const cards = new Map([[5, [card({})]]]);
    expect(foldSteps(run, cards, none, new Map([[7, []]])).map((r) => r.at)).toEqual([[0, 1, 2], [3], [4], [5], [6], [7]]);
    expect(foldSteps(run, none, new Map([[1, []]]), none).map((r) => r.at)).toEqual([[0, 1], [2], [3], [4, 5, 6, 7]]);

    const { container } = render(<Transcript items={run.slice(0, 4)} approvals={[]} running={false} />);
    const fold = container.querySelector("details")!;
    expect(fold.open).toBe(false);
    expect(fold.querySelector("summary")!.textContent).toContain("3 steps");
    expect(fold.querySelector("summary")!.textContent).toContain("Bash 3");
    expect(fold.querySelector("summary")!.textContent).toContain("1 error");
    expect(within(screen.getByTestId("step-run")).getAllByText("Bash")).toHaveLength(3);
  });

  it("keeps the last run open while the turn runs it", () => {
    const calls = [0, 1].map((n) =>
      item({ kind: "tool", data: { id: `t${n}`, tool: "Read", input: { file_path: `/w/${n}` }, result: null, error: false } }, n),
    );
    const { container } = render(<Transcript items={calls} approvals={[]} running />);
    expect(container.querySelector("details")!.open).toBe(true);
  });

  it("places a card after the call it is about: by tool_use_id, else by tool and input, else at the end", () => {
    expect(placeCards(items, [card({ tool_use_id: "t1" })]).get(0)?.[0]?.id).toBe("a");
    expect(placeCards(items, [card({})]).get(0)?.[0]?.id).toBe("a");
    expect(placeCards(items, [card({ input: { command: "other" } })]).get(-1)?.[0]?.id).toBe("a");
  });

  it("shows the live delta while a turn runs", () => {
    render(<Transcript items={items} approvals={[]} delta="Typing an answer" running />);
    expect(screen.getByTestId("delta").textContent).toContain("Typing an answer");
  });

  it("the comment on a diff line goes out in the agreed form", () => {
    expect(commentText("retinue/gate.py", 84, " remove this ")).toBe("Comment on retinue/gate.py:84: remove this");
  });
});


describe("summaries", () => {
  const msg = (turn: number, n: number, text: string): Item =>
    ({ kind: "message", data: { role: "assistant", text }, session: "s1", turn, n }) as Item;
  const sum = (seq: number, turn: number | undefined, text: string): Summary => ({
    session: "s1", seq, head: `Result · code · session s1 #${seq}`, text, at: seq, ...(turn === undefined ? {} : { turn }),
  });

  it("are placed at the end of the turn they were said for", () => {
    const items = [msg(0, 0, "a"), msg(0, 1, "done"), msg(2, 0, "b")];
    const placed = placeSummaries(items, [sum(1, 0, "done"), sum(2, 1, "limit"), sum(3, 2, "b")]);
    expect(placed.get(1)?.map((s) => s.seq)).toEqual([1, 2]); // turn 1 has no items: after turn 0's last
    expect(placed.get(-1)?.map((s) => s.seq)).toEqual([3]); // after the last item: the end
    const old = placeSummaries(items, [sum(1, undefined, "x"), sum(2, undefined, "y")]);
    expect([...old.entries()]).toEqual([[-1, [sum(2, undefined, "y")]]]); // no turn: only the latest, at the end
  });

  it("show each turn's end inline: a header when it repeats the last message, whole when it says more", () => {
    const items = [msg(0, 0, "Done, the tests are green."), msg(1, 0, "Next.")];
    render(
      <Transcript
        items={items}
        approvals={[]}
        running={false}
        summaries={[sum(1, 0, "Done, the tests are green."), sum(2, 1, "The turn broke off with an error: overloaded")]}
      />,
    );
    const [first, second] = screen.getAllByTestId("summary");
    expect(first!.textContent).toBe("Result · code · session s1 #1");
    expect(second!.textContent).toContain("The turn broke off with an error: overloaded");
    expect(screen.getAllByText("Done, the tests are green.")).toHaveLength(1);
  });
});

describe("the owner's messages in the transcript", () => {
  const at = (turn: number, n: number, text = `${turn}/${n}`): Item =>
    ({ kind: "message", data: { role: "assistant", text }, session: "s1", turn, n }) as Item;
  const input = (id: string, turn: number | null, n: number | null, over: Partial<Input> = {}): Input => ({
    session: "s1",
    input_id: id,
    text: `text ${id}`,
    at: 1,
    turn,
    n,
    attachments: [],
    ...over,
  });

  it("go before what the session had not yet written when they came, or at the end", () => {
    const items = [at(0, 0), at(0, 1), at(1, 0)];
    const placed = placeInputs(items, [input("a", 0, 0), input("b", 0, 2), input("c", 2, 0), input("old", null, null)]);
    expect([...placed].map(([k, xs]) => [k, xs.map((x) => x.input_id)])).toEqual([
      [0, ["a"]],
      [2, ["b"]],
      [-1, ["c"]],
    ]);
  });

  it("shows a picture as the platform's thumbnail and a file by its name and size", () => {
    const attachments = [
      { kind: "image", name: "IMG_1.png", size: 2048, what: "image “IMG_1.png”, 40×20", thumb: true },
      { kind: "file", name: "logs.zip", size: 3 * 1024 * 1024, what: "file “logs.zip”", thumb: false },
    ];
    render(
      <Transcript
        items={[at(0, 0, "Looking.")]}
        approvals={[]}
        running={false}
        inputs={[input("face:x", 0, 0, { text: "what is here?", attachments })]}
      />,
    );
    const bubble = screen.getByTestId("input");
    const img = within(bubble).getByAltText("IMG_1.png") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("/api/sessions/s1/inputs/face%3Ax/0");
    expect(within(bubble).getByText("logs.zip")).toBeTruthy();
    expect(within(bubble).getByText("3.0 MB")).toBeTruthy();
    expect(within(bubble).getByText("what is here?")).toBeTruthy();
    expect(bubble.compareDocumentPosition(screen.getByText("Looking.")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("puts a message written after the last summary below it and above the running turn", () => {
    const summary: Summary = { session: "s1", seq: 1, head: "Result · notes", text: "Done, here is the result.", at: 5, turn: 0 };
    render(
      <Transcript
        items={[at(0, 0, "Answer.")]}
        approvals={[]}
        running
        summaries={[summary]}
        inputs={[input("tg:2", 1, 0, { text: "and more", at: 6 })]}
      />,
    );
    const order = (a: Element, b: Element) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    const note = screen.getByTestId("summary");
    const mine = screen.getByTestId("input");
    expect(order(note, mine)).toBe(true);
    expect(order(mine, screen.getByText("Working…"))).toBe(true);
  });
});
