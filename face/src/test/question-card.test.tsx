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
      question: "Где жить?",
      header: "Жильё",
      multiSelect: false,
      options: [
        { label: "Центр", description: "старый город" },
        { label: "Окраина", description: "тихо" },
      ],
    },
    {
      question: "Что взять?",
      header: "Вещи",
      multiSelect: true,
      options: [
        { label: "Зонт", description: "" },
        { label: "Ноутбук", description: "" },
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
    expect(within(first!).getByText("Где жить?")).toBeTruthy();
    expect(within(first!).getByRole("button", { name: /Центр\s*старый город/ })).toBeTruthy();
    expect(within(second!).getByRole("button", { name: /Готово/ })).toHaveProperty("disabled", true);
    expect(screen.getAllByRole("button", { name: "Свой ответ…" })).toHaveLength(2);
  });

  it("a single choice is sent by its tap with the card's digest, and the card shows what came back", async () => {
    const live = fakeLive();
    const answered = question({ view: { answers: { "0": { value: "Окраина", other: false } }, picks: {} } as never, at: 2 });
    const answer = vi.fn().mockResolvedValue({ toast: "Принято. Осталось ответить: 1.", approval: answered });
    render(<ApprovalCard approval={question()} live={live} answer={answer} />);
    fireEvent.click(screen.getByRole("button", { name: /Окраина/ }));
    await waitFor(() => expect(live.getState().approvals["9c21aa01"]?.at).toBe(2));
    expect(answer).toHaveBeenCalledWith(expect.objectContaining({ id: "9c21aa01", digest: "digest-of-the-question" }), 0, {
      labels: ["Окраина"],
    });
  });

  it("a multi-select is ticked and sent by «Готово»; the owner's own words by «Свой ответ…»", async () => {
    const done = question({ state: "answered" });
    const answer = vi.fn().mockResolvedValue({ toast: "Ответ передан в сессию.", approval: done });
    const decided = vi.fn();
    render(<ApprovalCard approval={question()} live={fakeLive()} answer={answer} onDecided={decided} />);
    const [, second] = screen.getAllByTestId("question");
    fireEvent.click(within(second!).getByRole("button", { name: /Ноутбук/ }));
    fireEvent.click(within(second!).getByRole("button", { name: /Зонт/ }));
    expect(within(second!).getByRole("button", { name: /Зонт/ }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(within(second!).getByRole("button", { name: "Готово (2)" }));
    await waitFor(() => expect(decided).toHaveBeenCalledWith(done));
    expect(answer).toHaveBeenCalledWith(expect.anything(), 1, { labels: ["Ноутбук", "Зонт"] });

    const [first] = screen.getAllByTestId("question");
    fireEvent.click(within(first!).getByRole("button", { name: "Свой ответ…" }));
    fireEvent.change(within(first!).getByLabelText("Свой ответ"), { target: { value: "  у моря " } });
    fireEvent.click(within(first!).getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(answer).toHaveBeenCalledWith(expect.anything(), 0, { text: "у моря" }));
  });

  it("an answered question shows its answer, and a card no longer asking has no buttons", () => {
    const view = { answers: { "0": { value: "У моря", other: true } }, picks: {}, status: "Устарела: ты написал новое сообщение" };
    render(<ApprovalCard approval={question({ state: "stale", view: view as never })} live={fakeLive()} />);
    expect(screen.getByTestId("question-answer").textContent).toBe("Свой ответ: У моря");
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByText("Устарела: ты написал новое сообщение")).toBeTruthy();
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
    expect(screen.getAllByText("Где жить?")).toHaveLength(1);
    expect(screen.queryByText(/Ждёт ответа — в карточке вопроса/)).toBeNull();
  });
});
