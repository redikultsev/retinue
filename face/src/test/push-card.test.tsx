import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { commitsWord, PushCardView } from "@/components/push-card";
import { Live, live } from "@/lib/live";
import { initialState, reduce } from "@/lib/store";
import type { PushCard, SessionSnapshot } from "@/lib/types";

const card = (over: Partial<PushCard> = {}): PushCard => ({
  id: "f00dbabe",
  session: "a1b2c3d4",
  repo: "retinue",
  state: "shown",
  place: "code",
  branch: "fix-login",
  target: "retinue/a1b2c3d4-fix-login",
  base: "main",
  custom: false,
  title: "Fix login",
  commits: [
    { sha: "b".repeat(40), subject: "Add test", message: "Add test\n\nWhy it matters.", author: "A <a@e.x>, 2026-10-27 21:30 +0000", parents: ["a".repeat(40)], files: [{ path: "tests/t.py", status: "A" }] },
    { sha: "a".repeat(40), subject: "Fix login", message: "Fix login", parents: ["c".repeat(40)], files: [] },
  ],
  unpushed: 2,
  objects: 6,
  bytes: 3300,
  files: [{ path: "src/login.py", status: "M", add: 3, del: 1 }],
  ahead: 2,
  workflows: [],
  dirty: [],
  partial: false,
  url: "",
  said: "",
  created: 10,
  at: 10,
  view: { head: ["Push · retinue · code · session a1b2c3d4"], body: "", status: "" },
  ...over,
});

const fakeLive = () =>
  new Live({
    api: { meta: vi.fn(), sessions: vi.fn(), session: vi.fn() },
    openSource: vi.fn(),
    probe: vi.fn(),
    now: () => 0,
    reload: vi.fn(),
    onUnauthorized: vi.fn(),
  });

const calls = (answer: Partial<PushCard> = {}) => {
  const res = { toast: "", card: card(answer) };
  return {
    pushTarget: vi.fn().mockResolvedValue(res),
    pushTitle: vi.fn().mockResolvedValue(res),
    pushSend: vi.fn().mockResolvedValue({ toast: "Pushing…", card: card({ ...answer, state: "pushing", at: 11 }) }),
    pushCancel: vi.fn().mockResolvedValue(res),
    pushOpen: vi.fn().mockResolvedValue(res),
    pushDiff: vi.fn().mockResolvedValue({ diff: "", truncated: false }),
  };
};

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
});

describe("PushCardView", () => {
  it("shows the branch it goes to, the commits and files, and sends with an edited branch saved first", async () => {
    const c = calls();
    const store = fakeLive();
    render(<PushCardView card={card()} calls={c} live={store} />);
    expect((screen.getByLabelText("Branch") as HTMLInputElement).value).toBe("retinue/a1b2c3d4-fix-login");
    const commits = screen.getByRole("list", { name: "Commits" });
    expect(within(commits).getByText("Add test")).toBeTruthy();
    expect(within(commits).getByText(/Why it matters/)).toBeTruthy();
    expect(within(commits).getByText("A tests/t.py")).toBeTruthy();
    expect(within(commits).getByText(/author: A <a@e.x>/)).toBeTruthy();
    expect(screen.getByText(/Going out: 2 commits, 3 KB/)).toBeTruthy();
    expect(within(screen.getByRole("list", { name: "Files" })).getByText("+3")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Branch"), { target: { value: "fix/login" } });
    fireEvent.click(screen.getByRole("button", { name: "Push · 2 commits" }));
    await waitFor(() => expect(c.pushSend).toHaveBeenCalledWith("f00dbabe"));
    expect(c.pushTarget).toHaveBeenCalledWith("f00dbabe", "fix/login");
    expect(c.pushTarget.mock.invocationCallOrder[0]).toBeLessThan(c.pushSend.mock.invocationCallOrder[0]!);
    expect(store.getState().pushes.f00dbabe?.state).toBe("pushing");
  });

  it("refuses to send workflows, says what is not committed, and links the PR only on GitHub or GitLab", () => {
    const { rerender } = render(
      <PushCardView card={card({ workflows: [".github/workflows/ci.yml"], dirty: ["README.md"] })} calls={calls()} live={fakeLive()} />,
    );
    expect((screen.getByRole("button", { name: "Push · 2 commits" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/token cannot push/)).toBeTruthy();
    expect(screen.getByText(/Not committed \(1\): README.md/)).toBeTruthy();
    rerender(<PushCardView card={card({ state: "pushed", url: "https://github.com/o/r/pull/7" })} calls={calls()} live={fakeLive()} />);
    expect(screen.getByRole("link", { name: "Open PR" }).getAttribute("href")).toBe("https://github.com/o/r/pull/7");
    expect(screen.queryByRole("button", { name: /^Push/ })).toBeNull();
    rerender(<PushCardView card={card({ state: "pushed", url: "https://evil.example/pull/7" })} calls={calls()} live={fakeLive()} />);
    expect(screen.queryByRole("link", { name: "Open PR" })).toBeNull();
  });

  it("counts commits in English", () => {
    expect([0, 1, 2, 5, 11, 21].map(commitsWord)).toEqual(["0 commits", "1 commit", "2 commits", "5 commits", "11 commits", "21 commits"]);
  });
});

describe("the store", () => {
  it("keeps a push card from the stream and from a session's snapshot, the newer one winning", () => {
    let state = reduce(initialState, { type: "event", id: 5, event: { kind: "push", data: card({ at: 20 }) } });
    state = reduce(state, { type: "event", id: null, event: { kind: "push", data: card({ at: 15, title: "old" }) } });
    expect(state.pushes.f00dbabe?.title).toBe("Fix login");
    const snapshot = { cursor: 9, session: { id: "a1b2c3d4" }, items: [], approvals: [], summaries: [], push_cards: [card({ state: "pushed", at: 30 })] };
    state = reduce(state, { type: "session", snapshot: snapshot as unknown as SessionSnapshot });
    expect(state.pushes.f00dbabe?.state).toBe("pushed");
  });
});
