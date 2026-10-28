import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApprovalCard } from "@/components/approval-card";
import { ApiError } from "@/lib/api";
import { Live } from "@/lib/live";
import type { Approval } from "@/lib/types";

const longCommand = `${"echo строка && ".repeat(400)}true`; // > 4096 characters: Telegram would only offer «Открыть»

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
    head: ["Разрешение · retinue · сессия a1b2c3d4", "Инструмент: Bash", "Папка: /work/retinue", "Ввод, 6404 знаков:"],
    body: "Ввод длинный — целиком он придёт файлом по кнопке «Открыть».",
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
    expect(screen.getByText("Разрешение · retinue · сессия a1b2c3d4")).toBeTruthy();
    expect(screen.getByText("Папка: /work/retinue")).toBeTruthy();
    expect(screen.getByTestId("approval-input").textContent).toBe(longCommand);
    expect(screen.getByRole("button", { name: "Разрешить" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Отказать" })).toBeTruthy();
  });

  it("shows JSON input of a non-Bash tool indented, as approvals.input_text does", () => {
    const input = { file_path: "/work/notes/a.md", content: "текст" };
    render(<ApprovalCard approval={approval({ tool: "Write", input })} live={fakeLive()} />);
    expect(screen.getByTestId("approval-input").textContent).toBe(JSON.stringify(input, null, 2));
  });

  it("writes invisible characters as their codes", () => {
    render(<ApprovalCard approval={approval({ input: { command: "rm‮fdp.exe" } })} live={fakeLive()} />);
    expect(screen.getByTestId("approval-input").textContent).toBe("rm⟦U+202E⟧fdp.exe");
    expect(screen.getByText(/невидимые символы/)).toBeTruthy();
  });

  it("approve posts the action with the card's digest and applies the returned approval", async () => {
    const live = fakeLive();
    const decided = approval({ state: "granted", at: 2, view: { head: [], status: "Разрешено — выполняю." } });
    const decide = vi.fn().mockResolvedValue({ toast: "Разрешено.", approval: decided });
    render(<ApprovalCard approval={approval()} live={live} decide={decide} />);
    fireEvent.click(screen.getByRole("button", { name: "Разрешить" }));
    await waitFor(() => expect(live.getState().approvals["5f3a9c21"]?.state).toBe("granted"));
    expect(decide).toHaveBeenCalledWith(expect.objectContaining({ id: "5f3a9c21", digest: "digest-of-the-call" }), "approve");
  });

  it("reject posts reject", async () => {
    const decide = vi.fn().mockResolvedValue({ toast: "Отказано.", approval: approval({ state: "rejected" }) });
    render(<ApprovalCard approval={approval()} live={fakeLive()} decide={decide} />);
    fireEvent.click(screen.getByRole("button", { name: "Отказать" }));
    await waitFor(() => expect(decide).toHaveBeenCalledWith(expect.anything(), "reject"));
  });

  it("keeps the buttons usable after a failed request", async () => {
    const decide = vi.fn().mockRejectedValue(new ApiError(0, "Нет связи с сервером."));
    render(<ApprovalCard approval={approval()} live={fakeLive()} decide={decide} />);
    fireEvent.click(screen.getByRole("button", { name: "Разрешить" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Разрешить" }) as HTMLButtonElement).disabled).toBe(false));
  });

  it("a decided card and a courier card have no approve/reject buttons", () => {
    const { unmount } = render(<ApprovalCard approval={approval({ state: "executed" })} live={fakeLive()} />);
    expect(screen.queryByRole("button", { name: "Разрешить" })).toBeNull();
    unmount();
    render(
      <ApprovalCard
        approval={approval({
          kind: "courier",
          input: { command: "git push origin main" },
          view: { head: ["Курьер · retinue · сессия a1b2c3d4"], status: "Не выполняю: это делаешь ты сам — команда выше целиком." },
        })}
        live={fakeLive()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Разрешить" })).toBeNull();
    expect(screen.getByRole("button", { name: "Скопировать команду" })).toBeTruthy();
    expect(screen.getByText(/это делаешь ты сам/)).toBeTruthy();
  });
});
