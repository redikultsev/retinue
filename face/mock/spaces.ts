// «Пространства» in the mock (retinue/spaces.py's answers): a small synthetic Base — two Spaces, a few Records.
import type { RecordView, SpaceRow, SpaceView } from "../src/lib/space-types.ts";

const HEAD = "0123456789abcdef0123456789abcdef01234567";

const travel: SpaceView = {
  name: "travel",
  title: "Путешествия",
  rules: "## Правила домена\n\n- Цены — только живые, со ссылкой.\n- Билеты сравниваем на двух сайтах.",
  marker: true,
  counts: { questions: 2, tasks: 1 },
  sections: [
    { heading: "Задачи", kind: "tasks", items: [{ text: "Перепроверить правила [пересадки](/spaces/travel/r/knowledge/transit.md) до 01.05.", list: true }] },
    {
      heading: "Открытые вопросы",
      kind: "questions",
      items: [
        { text: "Брать ли пересадку через хаб или лететь прямым?", list: true },
        { text: "Нужна ли страховка с покрытием активного отдыха?", list: true },
      ],
    },
  ],
  records: {
    knowledge: [
      { path: "knowledge/transit.md", folder: "", title: "Пересадка в аэропорту", verified: "2026-09-21", practice: "diverges", stale_after: "2026-12-21", status: "current" },
      { path: "knowledge/airport-hotels.md", folder: "", title: "Отели у аэропорта", verified: "2026-10-02", status: "current" },
    ],
    profile: [{ path: "profile/docs/passport.md", folder: "docs", title: "Загранпаспорт", from: "2020-01-01", until: "2030-01-01" }],
    journal: [{ path: "journal/2026-10-01-baggage.md", folder: "", title: "Беру билеты с багажом в цене", type: "decision", date: "2026-10-01" }],
    artifacts: [],
  },
};

const work: SpaceView = {
  name: "work",
  title: "Дела",
  rules: "",
  marker: true,
  counts: { questions: 0, tasks: 0 },
  sections: [],
  records: { knowledge: [], profile: [], journal: [], artifacts: [] },
};

const records: Record<string, RecordView> = {
  "travel/knowledge/transit.md": {
    space: "travel",
    path: "knowledge/transit.md",
    nature: "knowledge",
    title: "Пересадка в аэропорту",
    head: {
      written_by: "Claude Code (Opus 5)", created: "2026-09-21", updated: "2026-10-02", id: "transit", title: "Пересадка в аэропорту",
      type: "knowledge", source: "Правила перевозчика, https://example.org/rules", verified: "2026-09-21",
      stale_after: "2026-12-21", status: "current", practice: "diverges",
    },
    bad_head: false,
    body: "# Пересадка в аэропорту\n\n## Коротко\n\n- По правилам на пересадку хватает 40 минут.\n- На деле нужно не меньше полутора часов.\n\nСм. [загранпаспорт](/spaces/travel/r/profile/docs/passport.md).",
  },
  "travel/profile/docs/passport.md": {
    space: "travel",
    path: "profile/docs/passport.md",
    nature: "profile",
    title: "Загранпаспорт",
    head: { written_by: "Владелец", created: "2026-09-01", id: "passport", type: "profile", from: "2020-01-01", until: "2030-01-01", learned: "2026-09-01", source: "паспорт" },
    bad_head: false,
    body: "Действует до января 2030.",
  },
};

const views: Record<string, SpaceView> = { travel, work };

/** `GET /api/spaces…`, or null when the path is not one of them. */
export function mockSpaces(path: string): { status: number; data: unknown } | null {
  if (path === "/api/spaces") {
    const spaces: SpaceRow[] = Object.values(views).map((v) => ({ name: v.name, title: v.title, ...v.counts, waiting: 0, working: 0 }));
    return { status: 200, data: { head: HEAD, spaces } };
  }
  const m = /^\/api\/spaces\/([^/]+)(?:\/r\/(.+))?$/.exec(path);
  if (!m) return null;
  const view = views[decodeURIComponent(m[1]!)];
  if (!view) return { status: 404, data: { error: "Нет такого Пространства." } };
  if (!m[2]) return { status: 200, data: { head: HEAD, space: view } };
  const record = records[`${view.name}/${decodeURIComponent(m[2])}`];
  return record ? { status: 200, data: { head: HEAD, record } } : { status: 404, data: { error: "Нет такой Записи." } };
}
