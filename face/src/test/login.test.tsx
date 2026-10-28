import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { codeFromHash, telegramInitData, telegramStartApproval } from "@/lib/telegram";
import { LoginPage, safeNext } from "@/pages/login";

afterEach(() => {
  window.history.replaceState(null, "", "/");
  delete (window as unknown as { Telegram?: unknown }).Telegram;
});

describe("login helpers", () => {
  it("reads a 10-character code from the link's hash", () => {
    expect(codeFromHash("#abcdefgh23")).toBe("ABCDEFGH23");
    expect(codeFromHash("#tgWebAppData=x")).toBeNull();
    expect(codeFromHash("")).toBeNull();
    expect(codeFromHash("#%E0")).toBeNull(); // a malformed escape does not crash the page
  });

  it("finds Telegram init data in the hash or the WebApp object", () => {
    const initData = "query_id=AA&user=%7B%22id%22%3A1%7D&auth_date=1&hash=ff";
    expect(telegramInitData(window, `#tgWebAppData=${encodeURIComponent(initData)}&tgWebAppVersion=8.0`)).toBe(initData);
    (window as unknown as { Telegram: unknown }).Telegram = { WebApp: { initData: "from-api" } };
    expect(telegramInitData(window, "")).toBe("from-api");
    expect(telegramStartApproval("user=x&start_param=a_5f3a9c21&hash=1")).toBe("5f3a9c21");
    expect(telegramStartApproval("start_param=zzz")).toBeNull();
  });
});

describe("safeNext", () => {
  const origin = "https://app.example";
  it.each([
    ["/s/a1b2c3d4?tab=changes", "/s/a1b2c3d4?tab=changes"],
    ["/a/5f3a9c21", "/a/5f3a9c21"],
    [undefined, "/"],
    ["https://evil.com/", "/"],
    ["//evil.com", "/"],
    ["/\\evil.com", "/"],
    ["/%5Cevil.com", "/%5Cevil.com"],
    ["/%09/evil.com", "/%09/evil.com"],
    ["/\t/evil.com", "/"],
    ["/.//evil.com", "/"],
    ["/login?next=/x", "/"],
    ["javascript:alert(1)", "/"],
  ])("%s → %s", (next, want) => {
    const got = safeNext(next, origin);
    expect(got).toBe(want);
    expect(new URL(got, origin).origin).toBe(origin);
  });
});

describe("LoginPage", () => {
  it("sends the typed code and goes on", async () => {
    const loginCode = vi.fn().mockResolvedValue({ ok: true });
    const done = vi.fn();
    render(<LoginPage next="/s/a1b2c3d4" loginCode={loginCode} loginTelegram={vi.fn()} done={done} />);
    fireEvent.change(screen.getByPlaceholderText("XXXXXXXXXX"), { target: { value: "rtne7k2qxp" } });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    await waitFor(() => expect(done).toHaveBeenCalledWith("/s/a1b2c3d4"));
    expect(loginCode).toHaveBeenCalledWith("RTNE7K2QXP");
  });

  it("logs in from the link's #code and removes it from the URL", async () => {
    window.history.replaceState(null, "", "/login#RTNE7K2QXP");
    const loginCode = vi.fn().mockResolvedValue({ ok: true });
    const done = vi.fn();
    render(<LoginPage loginCode={loginCode} loginTelegram={vi.fn()} done={done} />);
    await waitFor(() => expect(done).toHaveBeenCalledWith("/"));
    expect(loginCode).toHaveBeenCalledWith("RTNE7K2QXP");
    expect(window.location.hash).toBe("");
  });

  it("says why a wrong code failed, and the rate limit as the server says it", async () => {
    const loginCode = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(401, "x"))
      .mockRejectedValueOnce(new ApiError(429, "Слишком много попыток — подожди 10 минут."));
    render(<LoginPage loginCode={loginCode} loginTelegram={vi.fn()} done={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText("XXXXXXXXXX"), { target: { value: "AAAAAAAAAA" } });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/одноразовый/);
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/Слишком много попыток/));
  });

  it("inside the Mini App posts initData and opens the card it was launched for", async () => {
    const initData = "user=%7B%22id%22%3A1%7D&start_param=a_5f3a9c21&auth_date=1&hash=ff";
    window.history.replaceState(null, "", `/login#tgWebAppData=${encodeURIComponent(initData)}`);
    const loginTelegram = vi.fn().mockResolvedValue({ ok: true });
    const done = vi.fn();
    render(<LoginPage loginCode={vi.fn()} loginTelegram={loginTelegram} done={done} />);
    await waitFor(() => expect(done).toHaveBeenCalledWith("/a/5f3a9c21"));
    expect(loginTelegram).toHaveBeenCalledWith(initData);
    expect(screen.queryByPlaceholderText("XXXXXXXXXX")).toBeNull();
  });

  it("never follows an outside `next`", async () => {
    const done = vi.fn();
    render(<LoginPage next="//evil.example" loginCode={vi.fn().mockResolvedValue({})} loginTelegram={vi.fn()} done={done} />);
    fireEvent.change(screen.getByPlaceholderText("XXXXXXXXXX"), { target: { value: "AAAAAAAAAA" } });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    await waitFor(() => expect(done).toHaveBeenCalledWith("/"));
  });
});
