// «Пространства»: the grid with its badges, a Space as a screen, a Record to read, «Обсудить» — against a fake API.
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { live } from "@/lib/live";
import { QUOTE_FRAME, type RecordView, type SpaceView } from "@/lib/spaces";
import { BaseMarkdown } from "@/components/base-markdown";
import type { Session } from "@/lib/types";
import { Discuss, forgetRead, RecordPage, SpacePage, SpacesPage } from "@/pages/spaces";

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: Date.now() / 1000, title: `Сессия ${id}`,
  waiting: 0, ...over,
});

const SPACE: SpaceView = {
  name: "travel",
  title: "Путешествия",
  rules: "- Цены — только живые.",
  marker: true,
  counts: { questions: 1, tasks: 1 },
  sections: [
    { heading: "Задачи", kind: "tasks", items: [{ text: "Перепроверить [транзит](/spaces/travel/r/knowledge/transit.md)", list: true }] },
    { heading: "Открытые вопросы", kind: "questions", items: [{ text: "Брать ли транзит через хаб?", list: true }] },
    { heading: "Записи", kind: "records", items: [{ text: "**knowledge/**", list: false }] },
    { heading: "Сроки", kind: "other", items: [{ text: "паспорт до 2030", list: true }] },
  ],
  records: {
    knowledge: [
      { path: "knowledge/transit.md", folder: "", title: "Пересадка в аэропорту", verified: "2026-09-21", practice: "diverges" },
      { path: "knowledge/hotels.md", folder: "", title: "Отели у аэропорта" },
    ],
    profile: [{ path: "profile/docs/passport.md", folder: "docs", title: "Загранпаспорт", until: "2030-01-01" }],
    journal: [],
    artifacts: [],
  },
};

const RECORD: RecordView = {
  space: "travel",
  path: "knowledge/transit.md",
  nature: "knowledge",
  title: "Пересадка в аэропорту",
  head: {
    written_by: "Claude Code (Opus 5)", type: "knowledge", verified: "2026-09-21", practice: "diverges",
    stale_after: "2099-12-21", colour: "зелёный",
    source: "Норма — [x](javascript:alert(1)), ![i](https://evil.example/i.png), https://example.org/rules.",
  },
  bad_head: false,
  body: "Смотри [паспорт](/spaces/travel/r/profile/docs/passport.md) и [сайт](https://example.org).\n\n" +
    '<img src="https://evil.example/x.png"> ![x](https://evil.example/y.png)',
};

function mount(node: React.ReactNode, at = "/") {
  const root = createRootRoute();
  const page = createRoute({ getParentRoute: () => root, path: "/", component: () => node });
  const spaces = createRoute({ getParentRoute: () => root, path: "/spaces", component: () => <p>сетка</p> });
  const space = createRoute({ getParentRoute: () => root, path: "/spaces/$space", component: () => <p>пространство</p> });
  const record = createRoute({
    getParentRoute: () => root,
    path: "/spaces/$space/r/$",
    component: function R() {
      const { space: s, _splat } = record.useParams();
      return <p>запись {s}/{_splat}</p>;
    },
  });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>открыта сессия</p> });
  const router = createRouter({
    routeTree: root.addChildren([page, spaces, space, record, one]),
    history: createMemoryHistory({ initialEntries: [at] }),
  });
  render(<RouterProvider router={router} />);
  return router;
}

beforeEach(() => {
  forgetRead();
  live.meta = {
    schema: 1, owner_ok: true, face_origin: "", tracks: [{ name: "notes", lane: "private", kind: "private" }],
    spheres: [
      { name: "travel", title: "Путешествия", icon: "✈️", track: "notes" },
      { name: "work", title: "Работа", icon: "💼", track: "notes" },
    ],
  };
});

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
  live.meta = null;
});

describe("SpacesPage", () => {
  it("shows the Spaces in the spheres' order with their icons and badges from the live sessions", async () => {
    live.dispatch({
      type: "list",
      snapshot: { schema: 1, cursor: 1, sessions: [
        session("w", { sphere: "travel", state: "waiting" }),
        session("a", { sphere: "travel", state: "active" }),
        session("b", { sphere: "travel", state: "active" }),
      ] },
    });
    const read = vi.fn().mockResolvedValue({
      head: "h",
      spaces: [
        { name: "home", title: "Дом", questions: 0, tasks: 2, waiting: 0, working: 0 },
        { name: "work", title: "Работа", questions: 3, tasks: 1, waiting: 5, working: 0 },
        { name: "travel", title: "Путешествия", questions: 1, tasks: 1, waiting: 0, working: 0 },
      ],
    });
    const router = mount(<SpacesPage read={read} />);
    const links = await screen.findAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual([
      "✈️Путешествия1 ждёт2 работают1 вопрос",
      "💼Работа3 вопроса",
      "📁Дом",
    ]);
    fireEvent.click(links[0]!);
    await waitFor(() => expect(router.state.location.pathname).toBe("/spaces/travel"));
  });

  it("says why it could not load", async () => {
    mount(<SpacesPage read={vi.fn().mockRejectedValue(new ApiError(404, "База не подключена."))} />);
    expect(await screen.findByText("База не подключена.")).toBeTruthy();
  });
});

describe("SpacePage", () => {
  it("shows the Space's sessions, Open questions, Tasks and Records by nature, with a search", async () => {
    live.dispatch({
      type: "list",
      snapshot: { schema: 1, cursor: 1, sessions: [
        session("t1", { sphere: "travel", state: "waiting", title: "Билеты на июнь" }),
        session("c1", { sphere: "work", title: "Квартальный отчёт" }),
      ] },
    });
    mount(<SpacePage space="travel" read={vi.fn().mockResolvedValue({ head: "h", space: SPACE })} />);
    await screen.findByText("Брать ли транзит через хаб?");
    expect(screen.getByText("1 ждёт · 1 Открытый вопрос")).toBeTruthy();
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["Сессии (1)", "Открытые вопросы (1)", "Задачи (1)", "Записи (3)"]);
    expect(screen.getByRole("link", { name: /Билеты на июнь/ })).toBeTruthy();
    expect(screen.queryByText("Квартальный отчёт")).toBeNull();
    expect(screen.getByRole("link", { name: "транзит" }).getAttribute("href")).toBe("/spaces/travel/r/knowledge/transit.md");
    expect(screen.getByText("Знание 2 · Профиль 1")).toBeTruthy();
    const transit = screen.getByRole("link", { name: /Пересадка в аэропорту/ });
    expect(transit.textContent).toContain("проверено 21.09");
    expect(transit.textContent).toContain("практика расходится");
    expect(screen.getByText("docs/")).toBeTruthy();
    expect(screen.queryByText("**knowledge/**")).toBeNull();
    expect(screen.getByText("Сроки")).toBeTruthy();

    fireEvent.change(screen.getByRole("searchbox", { name: "Найти Запись" }), { target: { value: "отел" } });
    expect(screen.queryByRole("link", { name: /Пересадка в аэропорту/ })).toBeNull();
    expect(screen.getByRole("link", { name: /Отели у аэропорта/ })).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "Найти Запись" }), { target: { value: "ничего такого" } });
    expect(screen.getByText("Ничего не нашлось.")).toBeTruthy();
  });

  it("«Обсудить» on an Open question opens the dialog with the question quoted", async () => {
    mount(<SpacePage space="travel" read={vi.fn().mockResolvedValue({ head: "h", space: SPACE })} />);
    fireEvent.click(await screen.findByRole("button", { name: "Обсудить вопрос" }));
    const dialog = await screen.findByRole("dialog", { name: "Обсудить: открытый вопрос" });
    expect(within(dialog).getByTestId("discuss-head").textContent).toBe(
      `Обсудим Открытый вопрос из Витрины. ${QUOTE_FRAME}\n> travel/AGENTS.md\n> Брать ли транзит через хаб?`,
    );
  });
});

describe("RecordPage", () => {
  it("reads a Record: its head in words, its source apart, unknown fields at the bottom, links in the app", async () => {
    const read = vi.fn().mockResolvedValue({ head: "h", record: RECORD });
    const router = mount(<RecordPage space="travel" path="knowledge/transit.md" read={read} />);
    const words = await screen.findByTestId("head-words");
    expect(read).toHaveBeenCalledWith("travel", "knowledge/transit.md");
    expect(words.textContent).toBe("Знаниепроверено 21.09· практика расходится· устареет 21.12.2099· написал Claude Code");
    expect(screen.getByText("Источник")).toBeTruthy();
    const source = screen.getByTestId("source");
    expect(source.textContent).toBe(RECORD.head.source);
    // only http(s) addresses are links (an image's address too, as a link, never loaded); markdown is not parsed
    expect([...source.querySelectorAll("a")].map((a) => a.getAttribute("href"))).toEqual([
      "https://evil.example/i.png",
      "https://example.org/rules",
    ]);
    expect(source.querySelectorAll("img")).toHaveLength(0);
    expect(within(screen.getByTestId("head-extra")).getByText("colour").nextSibling?.textContent).toBe("зелёный");
    expect(document.querySelectorAll("main img, main [src]")).toHaveLength(0);
    expect(screen.getByText(/\[изображение: x\]/)).toBeTruthy();
    fireEvent.click(await screen.findByRole("link", { name: "паспорт" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/spaces/travel/r/profile/docs/passport.md"));
    expect(await screen.findByText("запись travel/profile/docs/passport.md")).toBeTruthy();
  });

  it("«Обсудить» starts a session in the Space's sphere naming the Record, then opens it", async () => {
    const start = vi.fn().mockResolvedValue({ session: "77889900" });
    const router = mount(
      <Discuss space="travel" topic={{ record: { path: "knowledge/transit.md", title: "Пересадка в аэропорту" } }} onClose={() => undefined} start={start} />,
    );
    const dialog = await screen.findByRole("dialog", { name: "Обсудить: запись" });
    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "Это ещё правда?" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Начать" }));
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    const [track, text, id, sphere] = start.mock.calls[0]!;
    expect([track, sphere]).toEqual(["notes", "travel"]);
    expect(text).toBe(`Обсудим Запись из Базы. ${QUOTE_FRAME}\n> travel/knowledge/transit.md\n> Пересадка в аэропорту\n\nЭто ещё правда?`);
    expect(id).toMatch(/^face:/);
    await waitFor(() => expect(router.state.location.pathname).toBe("/s/77889900"));
  });

  it("says when a Record is too big or not there", async () => {
    mount(<RecordPage space="travel" path="knowledge/big.md" read={vi.fn().mockRejectedValue(new ApiError(413, "Запись большая (600 КБ) — открой её на компьютере."))} />);
    expect(await screen.findByText("Запись большая (600 КБ) — открой её на компьютере.")).toBeTruthy();
  });
});

describe("BaseMarkdown links", () => {
  it("opens only plain app routes in place; anything else is not an app link", async () => {
    const router = mount(
      <BaseMarkdown
        text={[
          "[вверх](/spaces/x/r/../../../p/1)",
          "[js](javascript:alert(1))",
          "[data](data:text/html,<script>alert(1)</script>)",
          "[ссылкой][r]",
          "",
          "[r]: /spaces/x/r/../../p/2",
        ].join(" ")}
      />,
    );
    await screen.findByText("вверх");
    for (const el of document.querySelectorAll("a, button")) {
      const href = el.getAttribute("href") ?? "";
      expect(href.startsWith("javascript:") || href.startsWith("data:"), href).toBe(false);
      if (el.textContent === "вверх" || el.textContent === "ссылкой") fireEvent.click(el);
    }
    await new Promise((r) => setTimeout(r, 20));
    expect(router.state.location.pathname).toBe("/");
  });

  it("opens a plain app route in place", async () => {
    const router = mount(<BaseMarkdown text="[паспорт](/spaces/travel/r/profile/p.md)" />);
    fireEvent.click(await screen.findByRole("link", { name: "паспорт" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/spaces/travel/r/profile/p.md"));
  });
});
