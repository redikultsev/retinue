import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApprovalCard } from "@/components/approval-card";
import { ApiError } from "@/lib/api";
import { Live } from "@/lib/live";
import type { Approval } from "@/lib/types";

const longCommand = `${"echo line && ".repeat(400)}true`; // > 4096 characters: Telegram would only offer «Open»

const approval = (over: Partial<Approval> = {}): Approval => ({
  id: "5f3a9c21",
  session: "a1b2c3d4",
  kind: "approval",
  tool_use_id: "toolu_1",
  state: "shown",
  tool: "Bash",
  input: { command: longCommand },
  cwd: "/work/retinue",
  digest: "digest-of-the-call",
  view: {
    head: ["Approval · retinue · session a1b2c3d4", "Tool: Bash", "Folder: /work/retinue", "Input, 6404 characters:"],
    body: "The input is long — the whole of it comes as a file under «Open».",
  },
  created: 1,
  at: 1,
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

describe("ApprovalCard", () => {
  it("shows the header code built and the FULL input, never cut", () => {
    render(<ApprovalCard approval={approval()} live={fakeLive()} />);
    expect(screen.getByText("Approval · retinue · session a1b2c3d4")).toBeTruthy();
    expect(screen.getByText("Folder: /work/retinue")).toBeTruthy();
    expect(screen.getByTestId("approval-input").textContent).toBe(longCommand);
    expect(screen.getByRole("button", { name: "Allow" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Deny" })).toBeTruthy();
  });

  it("shows JSON input of a non-Bash tool indented, as approvals.input_text does", () => {
    const input = { file_path: "/work/notes/a.md", content: "text" };
    render(<ApprovalCard approval={approval({ tool: "Write", input })} live={fakeLive()} />);
    expect(screen.getByTestId("approval-input").textContent).toBe(JSON.stringify(input, null, 2));
  });

  it("writes invisible characters as their codes", () => {
    render(<ApprovalCard approval={approval({ input: { command: "rm‮fdp.exe" } })} live={fakeLive()} />);
    expect(screen.getByTestId("approval-input").textContent).toBe("rm⟦U+202E⟧fdp.exe");
    expect(screen.getByText(/invisible characters/)).toBeTruthy();
  });

  it("approve posts the action with the card's digest and applies the returned approval", async () => {
    const live = fakeLive();
    const decided = approval({ state: "granted", at: 2, view: { head: [], status: "Allowed — running it." } });
    const decide = vi.fn().mockResolvedValue({ toast: "Allowed.", approval: decided });
    render(<ApprovalCard approval={approval()} live={live} decide={decide} />);
    fireEvent.click(screen.getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(live.getState().approvals["5f3a9c21"]?.state).toBe("granted"));
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ id: "5f3a9c21", digest: "digest-of-the-call" }), "approve");
  });

  it("reject posts reject", async () => {
    const decide = vi.fn().mockResolvedValue({ toast: "Denied.", approval: approval({ state: "rejected" }) });
    render(<ApprovalCard approval={approval()} live={fakeLive()} decide={decide} />);
    fireEvent.click(screen.getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(decide).toHaveBeenCalledWith(expect.anything(), "reject"));
  });

  it("keeps the buttons usable after a failed request", async () => {
    const decide = vi.fn().mockRejectedValue(new ApiError(0, "No connection to the server."));
    render(<ApprovalCard approval={approval()} live={fakeLive()} decide={decide} />);
    fireEvent.click(screen.getByRole("button", { name: "Allow" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Allow" }) as HTMLButtonElement).disabled).toBe(false));
  });

  it("a decided card and a courier card have no approve/reject buttons", () => {
    const { unmount } = render(<ApprovalCard approval={approval({ state: "executed" })} live={fakeLive()} />);
    expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
    unmount();
    render(
      <ApprovalCard
        approval={approval({
          kind: "courier",
          input: { command: "git push origin main" },
          view: { head: ["Courier · retinue · session a1b2c3d4"], status: "Not running it: you do this yourself — the whole command is above." },
        })}
        live={fakeLive()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
    expect(screen.getByRole("button", { name: "Copy command" })).toBeTruthy();
    expect(screen.getByText(/you do this yourself/)).toBeTruthy();
  });
});
