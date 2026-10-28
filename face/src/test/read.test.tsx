import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  changedSinceDone,
  continuing,
  type Library,
  type LibraryPage,
  libraryApi,
  NO_SECTION,
  safeUrl,
  sections,
} from "@/lib/library";
import { openInTelegram } from "@/lib/telegram";
import { ReadPage, ReadView } from "@/pages/read";

const HUB = "https://hub.in.example.test";
const NOW = new Date(2026, 9, 28, 12, 0).getTime() / 1000;

const page = (id: string, over: Partial<LibraryPage> = {}): LibraryPage => ({
  id,
  title: id,
  url: `${HUB}/pages/${id}.html`,
  updated: "2026-10-27",
  section: "education",
  section_title: "Учёба",
  section_icon: "🎓",
  minutes: 42,
  opened: null,
  done: null,
  ...over,
});

const LIBRARY: Library = {
  schema: 1,
  hub: HUB,
  at: NOW - 60,
  pages: [
    page("regex", { title: "Курс по Regex", opened: NOW - 3600 }),
    page("sql-cheatsheet", { title: "SQL: шпаргалка", updated: "2026-10-20", opened: NOW - 60 }),
    page("ёлки", { title: "Ёлки и сосны", section: null, section_title: null, section_icon: null, minutes: null }),
    page("travel-notes", { title: "Заметки к поездке", section: "travel", section_title: "Путешествия", section_icon: "✈️", done: NOW - 86400 * 3, opened: NOW - 86400 * 4 }),
  ],
};

afterEach(() => {
  vi.restoreAllMocks();
  delete (window as unknown as { Telegram?: unknown }).Telegram;
});

describe("the library's rules", () => {
  it("draws a link only to the hub's own /pages/ over https", () => {
    expect(safeUrl(`${HUB}/pages/regex.html`, HUB)).toBe(`${HUB}/pages/regex.html`);
    expect(safeUrl("http://hub.in.example.test/pages/regex.html", HUB)).toBeNull();
    expect(safeUrl("https://evil.example/pages/regex.html", HUB)).toBeNull();
    expect(safeUrl(`${HUB}/now/`, HUB)).toBeNull();
    expect(safeUrl("javascript:alert(1)", HUB)).toBeNull();
    expect(safeUrl(`${HUB}/pages/regex.html`, "")).toBeNull();
  });

  it("puts what was opened and not finished first, the last opened on top", () => {
    expect(continuing(LIBRARY.pages).map((p) => p.id)).toEqual(["sql-cheatsheet", "regex"]);
  });

  it("groups by section, the sectionless last, and searches title and section with ё as е", () => {
    expect(sections(LIBRARY.pages).map((s) => [s.title, s.pages.map((p) => p.id)])).toEqual([
      ["Путешествия", ["travel-notes"]],
      ["Учёба", ["regex", "sql-cheatsheet"]],
      [NO_SECTION, ["ёлки"]],
    ]);
    expect(sections(LIBRARY.pages, "елки").flatMap((s) => s.pages.map((p) => p.id))).toEqual(["ёлки"]);
    expect(sections(LIBRARY.pages, "учёба regex").flatMap((s) => s.pages.map((p) => p.id))).toEqual(["regex"]);
    expect(sections(LIBRARY.pages, "нет такого")).toEqual([]);
  });

  it("says a finished page changed when it was updated on a later day", () => {
    expect(changedSinceDone(page("a", { done: NOW, updated: "2026-10-28" }))).toBe(false);
    expect(changedSinceDone(page("a", { done: NOW - 86400 * 2, updated: "2026-10-28" }))).toBe(true);
    expect(changedSinceDone(page("a"))).toBe(false);
  });

  it("hands the link to Telegram inside the Mini App, and to the browser outside it", () => {
    expect(openInTelegram(`${HUB}/pages/regex.html`)).toBe(false);
    const openLink = vi.fn();
    (window as unknown as { Telegram: unknown }).Telegram = { WebApp: { openLink } };
    expect(openInTelegram(`${HUB}/pages/regex.html`)).toBe(true);
    expect(openLink).toHaveBeenCalledWith(`${HUB}/pages/regex.html`);
    const postEvent = vi.fn();
    const proxy = { TelegramWebviewProxy: { postEvent } } as unknown as Window;
    expect(openInTelegram("https://x/pages/a.html", proxy)).toBe(true);
    expect(postEvent).toHaveBeenCalledWith("web_app_open_link", JSON.stringify({ url: "https://x/pages/a.html" }));
  });
});

describe("«Читать»", () => {
  const view = (library: Library | null, extra: Partial<Parameters<typeof ReadView>[0]> = {}) => {
    const onOpen = vi.fn();
    const onDone = vi.fn();
    render(<ReadView library={library} now={NOW} onOpen={onOpen} onDone={onDone} {...extra} />);
    return { onOpen, onDone };
  };

  it("shows «Продолжить» above the sections, each page a link to the hub in a tab of its own", () => {
    view(LIBRARY);
    const [one, ...rest] = screen.getAllByRole("region");
    const first = one!;
    expect(first.getAttribute("aria-label")).toBe("Продолжить");
    expect(within(first).getAllByTestId("library-page").map((li) => li.textContent)).toEqual([
      expect.stringContaining("SQL: шпаргалка"),
      expect.stringContaining("Курс по Regex"),
    ]);
    expect(rest.map((r) => r.getAttribute("aria-label"))).toEqual(["Путешествия", "Учёба", NO_SECTION]);
    const link = within(first).getByRole("link", { name: "Курс по Regex" });
    expect(link.getAttribute("href")).toBe(`${HUB}/pages/regex.html`);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(within(first).getAllByTestId("library-page")[1]!.textContent).toContain("42 мин · обновлена");
    expect(screen.getByText(/изменилась после прочтения/)).toBeTruthy(); // finished on the 25th, updated on the 27th
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("does not draw a link the hub did not give", () => {
    view({ ...LIBRARY, pages: [page("bad", { title: "Чужая", url: "https://evil.example/pages/bad.html" })] });
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Чужая")).toBeTruthy();
  });

  it("searches, and hides «Продолжить» while it does", () => {
    view(LIBRARY);
    fireEvent.change(screen.getByLabelText("Найти страницу"), { target: { value: "поездк" } });
    expect(screen.queryByRole("region", { name: "Продолжить" })).toBeNull();
    expect(screen.getAllByTestId("library-page").map((li) => li.textContent)).toEqual([expect.stringContaining("Заметки к поездке")]);
    fireEvent.change(screen.getByLabelText("Найти страницу"), { target: { value: "quantum" } });
    expect(screen.getByText("Ничего не нашлось.")).toBeTruthy();
  });

  it("reports an open and a «Дочитал», and the button says which it is", () => {
    const { onOpen, onDone } = view(LIBRARY);
    const regex = screen.getAllByTestId("library-page").find((li) => li.textContent?.includes("Regex"))!;
    fireEvent.click(within(regex).getByRole("link"));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: "regex" }));
    const button = within(regex).getByRole("button", { name: /Дочитал/ });
    expect(button.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(button);
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ id: "regex" }), true);
    const notes = screen.getAllByTestId("library-page").find((li) => li.textContent?.includes("поездке"))!;
    fireEvent.click(within(notes).getByRole("button", { name: /Дочитал/ }));
    expect(onDone).toHaveBeenLastCalledWith(expect.objectContaining({ id: "travel-notes" }), false);
  });

  it("inside Telegram the page goes to openLink, not to the web view", () => {
    const openLink = vi.fn();
    (window as unknown as { Telegram: unknown }).Telegram = { WebApp: { openLink } };
    const { onOpen } = view(LIBRARY);
    const link = screen.getAllByRole("link", { name: "Курс по Regex" })[0]!;
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    link.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(openLink).toHaveBeenCalledWith(`${HUB}/pages/regex.html`);
    expect(onOpen).toHaveBeenCalled();
  });

  it("says when the list never came, when it is empty, and when it failed", () => {
    view({ ...LIBRARY, at: null, pages: [] });
    expect(screen.getByText("Список страниц ещё не пришёл с хаба.")).toBeTruthy();
    expect(screen.queryByLabelText("Найти страницу")).toBeNull();
    document.body.innerHTML = "";
    view({ ...LIBRARY, pages: [] });
    expect(screen.getByText(/Страниц нет/)).toBeTruthy();
    document.body.innerHTML = "";
    const onRetry = vi.fn();
    view(null, { error: "Нет связи с сервером.", onRetry });
    fireEvent.click(screen.getByRole("button", { name: "Ещё раз" }));
    expect(onRetry).toHaveBeenCalled();
  });

  it("the page reads the list, marks an open at once and keeps what the platform answers", async () => {
    vi.spyOn(libraryApi, "list").mockResolvedValue(LIBRARY);
    const open = vi.spyOn(libraryApi, "open").mockResolvedValue({ page: page("ёлки", { title: "Ёлки и сосны", section: null, section_title: null, opened: 5 }) });
    const done = vi.spyOn(libraryApi, "done").mockResolvedValue({ page: { ...LIBRARY.pages[0]!, done: NOW } });
    render(<ReadPage />);
    const link = await screen.findByRole("link", { name: "Ёлки и сосны" });
    fireEvent.click(link);
    expect(open).toHaveBeenCalledWith("ёлки");
    await waitFor(() => expect(screen.getByRole("region", { name: "Продолжить" }).textContent).toContain("Ёлки"));
    const regex = within(screen.getByRole("region", { name: "Продолжить" }))
      .getAllByTestId("library-page")
      .find((li) => li.textContent?.includes("Regex"))!;
    fireEvent.click(within(regex).getByRole("button", { name: /Дочитал/ }));
    expect(done).toHaveBeenCalledWith("regex", true);
    await waitFor(() => expect(within(screen.getByRole("region", { name: "Продолжить" })).queryByText("Курс по Regex")).toBeNull());
  });
});
