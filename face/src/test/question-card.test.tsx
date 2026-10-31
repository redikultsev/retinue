import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApprovalCard } from "@/components/approval-card";
import { Transcript } from "@/components/transcript/transcript";
import { Live } from "@/lib/live";
import { shownApprovals } from "@/lib/store";
import type { Approval, Item } from "@/lib/types";

const ASK = {
  questions: [
    {
      question: "Where to stay?",
      header: "Stay",
      multiSelect: false,
      options: [
        { label: "Centre", description: "the old town" },
        { label: "Outskirts", description: "quiet" },
      ],
    },
    {
      question: "What to take?",
      header: "Things",
      multiSelect: true,
      options: [
        { label: "Umbrella", description: "" },
        { label: "Laptop", description: "" },
      ],
    },
  ],
};

const question = (over: Partial<Approval> = {}): Approval =>
  ({
    id: "9c21aa01",
    session: "a1b2c3d4",
    kind: "question",
    tool_use_id: "toolu_q",
    state: "shown",
    tool: "AskUserQuestion",
    input: ASK,
    cwd: "/work",
    digest: "digest-of-the-question",
    view: { answers: {}, picks: {}, status: "" },
    created: 1,
    at: 1,
    ...over,
  }) as Approval;

const fakeLive = () =>
  new Live({
    api: { meta: vi.fn(), sessions: vi.fn(), session: vi.fn() },
    openSource: vi.fn(),
    probe: vi.fn(),
    now: () => 0,
    reload: vi.fn(),
    onUnauthorized: vi.fn(),
  });

describe("QuestionCard", () => {
  it("draws each question with its options as buttons, the description on a second line", () => {
    render(<ApprovalCard approval={question()} live={fakeLive()} />);
    const [first, second] = screen.getAllByTestId("question");
    expect(within(first!).getByText("Where to stay?")).toBeTruthy();
    expect(within(first!).getByRole("button", { name: /Centre\s*the old town/ })).toBeTruthy();
    expect(within(second!).getByRole("button", { name: /Done/ })).toHaveProperty("disabled", true);
    expect(screen.getAllByRole("button", { name: "Own answer…" })).toHaveLength(2);
  });

  it("a single choice is sent by its tap with the card's digest, and the card shows what came back", async () => {
    const live = fakeLive();
    const answered = question({ view: { answers: { "0": { value: "Outskirts", other: false } }, picks: {} } as never, at: 2 });
    const answer = vi.fn().mockResolvedValue({ toast: "Accepted. Still to answer: 1.", approval: answered });
    render(<ApprovalCard approval={question()} live={live} answer={answer} />);
    fireEvent.click(screen.getByRole("button", { name: /Outskirts/ }));
    await waitFor(() => expect(live.getState().approvals["9c21aa01"]?.at).toBe(2));
    expect(answer).toHaveBeenCalledWith(expect.objectContaining({ id: "9c21aa01", digest: "digest-of-the-question" }), 0, {
      labels: ["Outskirts"],
    });
  });

  it("a multi-select is ticked and sent by «Done»; the owner's own words by «Own answer…»", async () => {
    const done = question({ state: "answered" });
    const answer = vi.fn().mockResolvedValue({ toast: "The answer went to the session.", approval: done });
    const decided = vi.fn();
    render(<ApprovalCard approval={question()} live={fakeLive()} answer={answer} onDecided={decided} />);
    const [, second] = screen.getAllByTestId("question");
    fireEvent.click(within(second!).getByRole("button", { name: /Laptop/ }));
    fireEvent.click(within(second!).getByRole("button", { name: /Umbrella/ }));
    expect(within(second!).getByRole("button", { name: /Umbrella/ }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(within(second!).getByRole("button", { name: "Done (2)" }));
    await waitFor(() => expect(decided).toHaveBeenCalledWith(done));
    expect(answer).toHaveBeenCalledWith(expect.anything(), 1, { labels: ["Laptop", "Umbrella"] });

    const [first] = screen.getAllByTestId("question");
    fireEvent.click(within(first!).getByRole("button", { name: "Own answer…" }));
    fireEvent.change(within(first!).getByLabelText("Own answer"), { target: { value: "  by the sea " } });
    fireEvent.click(within(first!).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(answer).toHaveBeenCalledWith(expect.anything(), 0, { text: "by the sea" }));
  });

  it("an answered question shows its answer, and a card no longer asking has no buttons", () => {
    const view = { answers: { "0": { value: "By the sea", other: true } }, picks: {}, status: "Expired: you wrote a new message" };
    render(<ApprovalCard approval={question({ state: "stale", view: view as never })} live={fakeLive()} />);
    expect(screen.getByTestId("question-answer").textContent).toBe("Own answer: By the sea");
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByText("Expired: you wrote a new message")).toBeTruthy();
  });

  it("waits on the owner like a card, and takes its item's place in the transcript", () => {
    expect(shownApprovals({ q: question() })).toHaveLength(1);
    const item: Item = {
      session: "a1b2c3d4",
      turn: 0,
      n: 0,
      kind: "question",
      data: { id: "toolu_q", questions: ASK.questions, answer: null },
    };
    render(<Transcript items={[item]} approvals={[question()]} running={false} />);
    expect(screen.getAllByText("Where to stay?")).toHaveLength(1);
    expect(screen.queryByText(/Awaiting an answer — in the question card/)).toBeNull();
  });
});
