import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BaseCardView, BaseLine } from "@/components/base-card";
import { Transcript } from "@/components/transcript/transcript";
import { Live } from "@/lib/live";
import { initialState, reduce } from "@/lib/store";
import type { BaseCard, Summary } from "@/lib/types";

function card(over: Partial<BaseCard> = {}): BaseCard {
  return {
    id: "c0ffee00",
    session: "a1b2c3d4",
    state: "committed",
    files: [
      { path: "travel/hotel.md", status: "A", add: 12, del: 0 },
      { path: "travel/AGENTS.md", status: "M", add: 1, del: 0 },
    ],
    subject: "travel: a hotel",
    sha: "4f2a9c1e0b7d",
    said: "Written to the Base: travel/AGENTS.md, travel/hotel.md · 4f2a9c1.",
    undo: true,
    undone: null,
    refused: [],
    committed: ["A travel/hotel.md", "M travel/AGENTS.md"],
    created: 10,
    at: 10,
    ...over,
  };
}

const fakeLive = () =>
  new Live({
    api: { meta: vi.fn(), sessions: vi.fn(), session: vi.fn() },
    openSource: vi.fn(),
    probe: vi.fn(),
    now: () => 0,
    reload: vi.fn(),
    onUnauthorized: vi.fn(),
  });

const summary = (over: Partial<Summary> = {}): Summary =>
  ({ session: "a1b2c3d4", seq: 5, head: "Result", text: "Done.", at: 1, turn: 0, kind: "done", ...over }) as Summary;

describe("a commit to the Base", () => {
  it("is one line under the turn's summary: nothing to press to commit", () => {
    render(
      <Transcript
        items={[]}
        approvals={[]}
        running={false}
        summaries={[summary({ auto: "c0ffee00", base_line: "Written to the Base: travel/hotel.md · 4f2a9c1." })]}
      />,
    );
    expect(screen.getByRole("button", { name: "Written to the Base: travel/hotel.md · 4f2a9c1." })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /To the Base/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Commit/ })).toBeNull();
  });

  it("says why nothing went, as plain text when there is no commit to open", () => {
    render(<BaseLine line="Not written: just check: no Showcase row. The session was asked to fix it." />);
    expect(screen.getByText(/Not written: just check/).tagName).toBe("P");
  });

  it("opens its card at a tap: the line, the files, «Undo»", async () => {
    const live = fakeLive();
    const baseCard = vi.fn().mockResolvedValue({ card: card() });
    render(<BaseLine id="c0ffee00" line={card().said} calls={{ baseCard }} live={live} />);
    fireEvent.click(screen.getByRole("button", { name: card().said }));
    await waitFor(() => expect(baseCard).toHaveBeenCalledWith("c0ffee00"));
    expect(await screen.findByRole("dialog", { name: "In the Base" })).toBeTruthy();
  });

  it("is undone by «Undo», and the stream takes the card as the platform says it", async () => {
    const live = fakeLive();
    const undone = card({ state: "undone", undo: false, undone: "9e8d7c6b5a41", said: "Undone: 9e8d7c6 puts back 2 files.", at: 11 });
    const undo = vi.fn().mockResolvedValue({ toast: undone.said, card: undone });
    render(<BaseCardView card={card()} undo={undo} live={live} />);
    expect(screen.getByRole("status").textContent).toBe(card().said);
    expect(screen.getByText("Written")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(undo).toHaveBeenCalledWith("c0ffee00"));
    await waitFor(() => expect(live.getState().bases.c0ffee00?.state).toBe("undone"));
  });

  it("keeps the newest card of each id from the stream", () => {
    let state = initialState;
    act(() => {
      state = reduce(state, { type: "event", id: 3, event: { kind: "base", data: card({ at: 10 }) } });
      state = reduce(state, { type: "event", id: 4, event: { kind: "base", data: card({ state: "undone", at: 11 }) } });
    });
    expect(state.bases.c0ffee00?.state).toBe("undone");
  });
});
