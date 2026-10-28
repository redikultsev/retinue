import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReplyButtons } from "@/components/reply-buttons";
import { Transcript } from "@/components/transcript/transcript";
import { live } from "@/lib/live";
import { lastWords } from "@/lib/store";
import { hideReplyLines, openPing, withoutLine } from "@/lib/replies";
import type { Item, Summary } from "@/lib/types";

const ping = (over: Partial<Summary> = {}): Summary => ({
  session: "s1", seq: 7, head: "Итог · notes · сессия s1", text: "Нашёл три варианта.", at: 5, turn: 0,
  kind: "decision", took_s: 600, replies: ["Бери первый", "Покажи ещё"], answered: null,
  line: "Ответы: «Бери первый» · «Покажи ещё»", ...over,
});

const LAST = "Нашёл три варианта.\n\nОтветы: «Бери первый» · «Покажи ещё»";

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
});

describe("a ping's replies", () => {
  it("sends the pressed reply by the seq and its number, and shows the ping as the platform answers", async () => {
    const dispatch = vi.spyOn(live, "dispatch");
    const closed = ping({ replies: [], answered: "Покажи ещё" });
    const reply = vi.fn().mockResolvedValue({ say: "Принято.", summary: closed });
    render(<ReplyButtons summary={ping()} live={live} reply={reply} />);
    const group = screen.getByRole("group", { name: "Ответы" });
    expect(within(group).getAllByRole("button").map((b) => b.textContent)).toEqual(["Бери первый", "Покажи ещё"]);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Покажи ещё" })));
    expect(reply).toHaveBeenCalledWith("s1", 7, 1);
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({ type: "event", id: null, event: { kind: "summary", data: closed } }),
    );
  });

  it("shows the reply that went in instead of buttons, and nothing for a ping without replies", () => {
    const { rerender, container } = render(<ReplyButtons summary={ping({ replies: [], answered: "Бери первый" })} />);
    expect(screen.getByTestId("answered").textContent).toBe("→ Бери первый");
    rerender(<ReplyButtons summary={ping({ replies: [], kind: "done" })} />);
    expect(container.textContent).toBe("");
  });

  it("hides the replies line in the turn's last message, and the summary that repeats it shows only its header", () => {
    const items: Item[] = [{ session: "s1", turn: 0, n: 0, kind: "message", data: { role: "assistant", text: LAST } }];
    render(<Transcript items={items} approvals={[]} running={false} summaries={[ping()]} />);
    expect(screen.queryByText(/Ответы:/)).toBeNull();
    expect(screen.getAllByText("Нашёл три варианта.")).toHaveLength(1);
    expect(within(screen.getByTestId("summary")).getByRole("button", { name: "Бери первый" })).toBeTruthy();
  });

  it("takes off only the exact line the platform parsed, at the end of the turn's last message", () => {
    const line = "Ответы: «Бери первый» · «Покажи ещё»";
    expect(withoutLine(`${LAST}\n  `, line)).toBe("Нашёл три варианта.");
    expect(withoutLine(`${line}\nи дальше текст`, line)).toBe(`${line}\nи дальше текст`);
    expect(withoutLine(LAST, null)).toBe(LAST);
    const msg = (n: number, text: string, turn = 0): Item =>
      ({ session: "s1", turn, n, kind: "message", data: { role: "assistant", text } }) as Item;
    const items = [msg(0, LAST), msg(1, LAST), msg(0, LAST, 1)];
    const shown = hideReplyLines(items, [ping()]);
    expect(shown.map((it) => (it.kind === "message" ? it.data.text : ""))).toEqual([LAST, "Нашёл три варианта.", LAST]);
    expect(hideReplyLines(items, [ping({ line: null })])).toBe(items);
    expect(lastWords([msg(0, LAST)], "", [ping()])).toBe("Нашёл три варианта.");
    expect(lastWords([msg(0, LAST)], "", [])).toBe(LAST);
    expect(openPing([ping(), ping({ seq: 8, replies: [] })])).toBeNull();
    expect(openPing([ping({ seq: 6, replies: [] }), ping()])?.seq).toBe(7);
  });
});
