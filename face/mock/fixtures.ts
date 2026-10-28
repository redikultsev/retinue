// What the mock face API starts with: tracks, sessions, transcripts with an item of every kind, a shown approval
// card, a courier card, a long input, and a realistic `git diff HEAD`. Shapes follow docs/face.md «API» and
// retinue/transcript.py, retinue/approvals.py `card`, retinue/courier.py `CardView`.
import type { About, Approval, CardView, Input, Item, ItemData, OutFile, PushCard, Session, Sphere, Summary, Track } from "../src/lib/types.ts";

export const T0 = Math.floor(Date.now() / 1000);

export const tracks: Track[] = [
  { name: "retinue", lane: "code", kind: "code" },
  { name: "notes", lane: "private", kind: "private" },
  { name: "kb", lane: "code", kind: "code" },
];

export const spheres: Sphere[] = [
  { name: "travel", title: "Путешествия", icon: "✈️", track: "notes" },
  { name: "reading", title: "Чтение", icon: "📚", track: "notes" },
  { name: "work", title: "Дела", icon: "🗂️", track: "notes" },
  { name: "health", title: "Здоровье", icon: "🩺", track: "notes" },
  { name: "platform", title: "Платформа", icon: "🛠️", track: "retinue" },
];

export const sessions: Session[] = [
  { id: "a1b2c3d4", track: "retinue", lane: "code", kind: "code", state: "waiting", created: T0 - 3600, at: T0 - 120, title: "Почини падающий тест test_gate: карточка после рестарта не нажимается", waiting: 1, push: 2, children: 2, sphere: "platform", main: true },
  { id: "d4c3b2a1", track: "retinue", lane: "code", kind: "code", state: "active", created: T0 - 500, at: T0 - 40, title: "Ревью дизайна: дайджест после рестарта", waiting: 0, sphere: "platform", sub_of: "a1b2c3d4" },
  { id: "e1d2c3b4", track: "retinue", lane: "code", kind: "code", state: "active", created: T0 - 450, at: T0 - 35, title: "Прогон полного набора тестов", waiting: 0, sphere: "platform", sub_of: "a1b2c3d4", queued: true },
  { id: "aa11bb22", track: "retinue", lane: "code", kind: "code", state: "idle", created: T0 - 9000, at: T0 - 7000, title: "Лента «Сегодня»: чипы статуса хаба", waiting: 0, sphere: "platform", push: 0, unread: true },
  { id: "cc33dd44", track: "notes", lane: "private", kind: "private", state: "idle", created: T0 - 20000, at: T0 - 15000, title: "Заметки к прочитанной книге", waiting: 0, sphere: "reading", main: true, unread: true },
  { id: "ee55ff66", track: "notes", lane: "private", kind: "private", state: "idle", created: T0 - 30000, at: T0 - 25000, title: "План чтения на май", waiting: 0, sphere: "reading", base: 3 },
  { id: "1a2b3c4d", track: "notes", lane: "private", kind: "private", state: "idle", created: T0 - 40000, at: T0 - 36000, title: "Список дел на выходные", waiting: 0, sphere: "work", pinned: true },
  { id: "77889900", track: "notes", lane: "private", kind: "private", state: "waiting", created: T0 - 2000, at: T0 - 300, title: "Собери конспект созвона в заметку", waiting: 1, sphere: "work", main: true, base: 1 },
  { id: "e5f6a7b8", track: "notes", lane: "private", kind: "private", state: "active", created: T0 - 900, at: T0 - 20, title: "Набросай план поездки в Лиссабон", waiting: 0, sphere: "travel", main: true, unread: true, children: 2 },
  { id: "b1c2d3e4", track: "notes", lane: "private", kind: "private", state: "active", created: T0 - 800, at: T0 - 30, title: "Жильё в Лиссабоне на 5 ночей", waiting: 0, sphere: "travel", sub_of: "e5f6a7b8" },
  { id: "f1e2d3c4", track: "notes", lane: "private", kind: "private", state: "idle", created: T0 - 700, at: T0 - 200, title: "Маршрут по Португалии", waiting: 0, sphere: "travel", sub_of: "e5f6a7b8" },
  { id: "5566aabb", track: "kb", lane: "code", kind: "code", state: "active", created: T0 - 600, at: T0 - 10, title: "Проверь битые ссылки в Витринах", waiting: 0 },
  { id: "c9d0e1f2", track: "retinue", lane: "code", kind: "code", state: "closed", created: T0 - 86400, at: T0 - 80000, title: "Выпусти 0.4.0: changelog и тег", waiting: 0, sphere: "platform" },
  { id: "0a1b2c3d", track: "notes", lane: "private", kind: "private", state: "idle", created: T0 - 7200, at: T0 - 5400, title: "Как устроены лимиты на линии?", waiting: 0, sphere: "travel", fork_of: "e5f6a7b8" },
];

const LONG_COMMAND = `cd /work/retinue && uv run --locked --with pytest pytest -q tests/test_gate.py tests/test_platformd.py \\
  -k "press or pressable or restart or digest" \\
  --maxfail=1 -o log_cli=true --log-cli-level=INFO \\
  2>&1 | tee /tmp/gate-run.log | grep -E "PASS|FAIL|ERROR|assert" | head -n 200 && \\
  python - <<'PY'
import json, sqlite3
db = sqlite3.connect("/work/retinue/.state/platform.sqlite")
rows = db.execute("SELECT id, state, digest FROM approvals ORDER BY created DESC LIMIT 5").fetchall()
print(json.dumps(rows, ensure_ascii=False, indent=2))
PY`;

const NOTE = Array.from(
  { length: 60 },
  (_, i) =>
    `- ${i + 1}. Acme: обсудили ${["формат выгрузки", "лимит строк в отчёте", "сроки релиза", "данные до запуска", "интеграцию с CRM"][i % 5]}; следующий шаг — ${["прислать выгрузку", "проверить на 10 тыс. строк", "назначить созвон", "оценить в днях"][i % 4]}.`,
).join("\n");

function card(session: string, track: string, tool: string, cwd: string, text: string, long = false): CardView {
  const head = [`Разрешение · ${track} · сессия ${session}`, `Инструмент: ${tool}`, `Папка: ${cwd}`, `Ввод, ${text.length} знаков:`];
  return long ? { head, body: "Ввод длинный — целиком он придёт файлом по кнопке «Открыть»." } : { head, body: text };
}

const writeInput = { file_path: "/work/notes/work/acme-call-2026-10-26.md", content: `# Созвон с Acme, 26 октября\n\n${NOTE}\n` };

export const approvals: Approval[] = [
  {
    id: "5f3a9c21",
    session: "a1b2c3d4",
    tool_use_id: "toolu_bash_2",
    kind: "approval",
    state: "shown",
    tool: "Bash",
    input: { command: LONG_COMMAND, description: "Прогнать тесты гейта и посмотреть последние карточки" },
    cwd: "/work/retinue",
    digest: "9d1c0f6e7a52b8c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0",
    view: card("a1b2c3d4", "retinue", "Bash", "/work/retinue", LONG_COMMAND),
    created: T0 - 130,
    at: T0 - 120,
  },
  {
    id: "b7e4d012",
    session: "77889900",
    tool_use_id: "toolu_write_1",
    kind: "approval",
    state: "shown",
    tool: "Write",
    input: writeInput,
    cwd: "/work/notes",
    digest: "0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
    view: card("77889900", "notes", "Write", "/work/notes", JSON.stringify(writeInput, null, 2), true),
    created: T0 - 310,
    at: T0 - 300,
  },
  {
    id: "c0ffee42",
    session: "c9d0e1f2",
    tool_use_id: "toolu_push",
    kind: "courier",
    state: "rejected",
    tool: "Bash",
    input: { command: "git push origin main --follow-tags" },
    cwd: "/work/retinue",
    digest: "c0ffee42c0ffee42c0ffee42c0ffee42c0ffee42c0ffee42c0ffee42c0ffee42",
    view: {
      head: ["Курьер · retinue · сессия c9d0e1f2", "Хочет: отправить коммиты (git push)", "Папка: /work/retinue"],
      body: "git push origin main --follow-tags",
      status: "Не выполняю: это делаешь ты сам — команда выше целиком.",
    },
    created: T0 - 80100,
    at: T0 - 80100,
  },
  {
    id: "d00dfeed",
    session: "c9d0e1f2",
    tool_use_id: "toolu_tag",
    kind: "approval",
    state: "executed",
    tool: "Bash",
    input: { command: 'git tag -a v0.4.0 -m "Retinue 0.4.0"' },
    cwd: "/work/retinue",
    digest: "d00dfeedd00dfeedd00dfeedd00dfeedd00dfeedd00dfeedd00dfeedd00dfeed",
    view: { ...card("c9d0e1f2", "retinue", "Bash", "/work/retinue", 'git tag -a v0.4.0 -m "Retinue 0.4.0"'), status: "Разрешено — выполняю." },
    created: T0 - 80300,
    at: T0 - 80200,
  },
];

const msg = (role: "assistant" | "user", text: string): ItemData => ({ kind: "message", data: { role, text } });

const call = (id: string, tool: string, input: Record<string, unknown>, result: string | null, error = false): ItemData => ({
  kind: "tool",
  data: { id, tool, input, result, error },
});

const turns: Record<string, ItemData[][]> = {
  a1b2c3d4: [
    [
      msg("assistant", "Посмотрю историю `retinue/gate.py`, тесты и issues."),
      call("toolu_h1", "Bash", { command: "git log --oneline -12 -- retinue/gate.py retinue/platformd.py" }, "a03fc7d Sub-sessions loosened\n13f8596 Sub-sessions after re-review\nc626ff8 Sub-sessions after review\n…"),
      { kind: "reads", data: { calls: [
        { id: "toolu_h2", tool: "Read", input: { file_path: "/work/retinue/retinue/gate.py" }, result: "   1→\"\"\"The gate…\"\"\"", error: false },
        { id: "toolu_h3", tool: "Read", input: { file_path: "/work/retinue/tests/test_gate.py" }, result: "   1→import pytest", error: false },
        { id: "toolu_h4", tool: "Grep", input: { pattern: "def owner_press", path: "/work/retinue/retinue" }, result: "retinue/platformd.py:1204", error: false },
      ] } },
      call("toolu_h5", "mcp__github__search_issues", { query: "repo:owner/retinue restart digest is:open" }, "2 issues: #41 «карточка после рестарта», #38 «дайджест файла»"),
      call("toolu_h6", "WebFetch", { url: "https://docs.anthropic.com/en/docs/claude-code/hooks", prompt: "PreToolUse defer semantics" }, "defer: the call is held; a resume runs it once."),
      call("toolu_h7", "Bash", { command: "uv run --locked --with pytest pytest -q tests/test_gate.py -x" }, "..........F\n1 failed, 10 passed in 2.02s", true),
      call("toolu_h8", "Bash", { command: "rg -n \"file_hash\" retinue | head" }, "retinue/approvals.py:88:def file_hash(…)\nretinue/gate.py:88:    approvals.file_hash(…)"),
      msg("assistant", "За неделю гейт не менялся; падение воспроизводится. Два открытых issue про рестарт (#41, #38) — это оно. Дальше чиню."),
    ],
    [
      msg("user", "Почини падающий тест test_gate: карточка после рестарта не нажимается. Логи в /tmp/gate.log."),
      msg(
        "assistant",
        "Посмотрю, как гейт решает, можно ли нажать карточку, и что меняется после рестарта.\n\nПлан:\n1. найти `_pressable` и тест;\n2. воспроизвести;\n3. починить и прогнать тесты.",
      ),
      {
        kind: "plan",
        data: {
          id: "toolu_todo_1",
          tool: "TodoWrite",
          todos: [
            { content: "Найти _pressable и падающий тест", status: "completed", activeForm: "Ищу _pressable" },
            { content: "Воспроизвести падение после рестарта", status: "completed", activeForm: "Воспроизвожу" },
            { content: "Починить сравнение дайджеста", status: "in_progress", activeForm: "Чиню сравнение дайджеста" },
            { content: "Прогнать tests/test_gate.py и tests/test_platformd.py", status: "pending", activeForm: "Гоняю тесты" },
          ],
        },
      },
      {
        kind: "reads",
        data: {
          calls: [
            { id: "toolu_r1", tool: "Read", input: { file_path: "/work/retinue/retinue/platformd.py" }, result: '   186→    def _pressable(self, approval: dict, digest: str) -> bool:\n   187→        """The card still asks what the session waits on…"""\n   188→        session = self.store.session(approval["session"])', error: false },
            { id: "toolu_r2", tool: "Grep", input: { pattern: "_pressable|digest !=", path: "/work/retinue" }, result: "retinue/platformd.py:170\nretinue/platformd.py:194\ntests/test_platformd.py:412", error: false },
            { id: "toolu_r3", tool: "Glob", input: { pattern: "tests/test_gate*.py" }, result: "tests/test_gate.py", error: false },
            { id: "toolu_r4", tool: "Read", input: { file_path: "/work/retinue/tests/test_nope.py" }, result: "File does not exist.", error: true },
          ],
        },
      },
      {
        kind: "subagent",
        data: {
          id: "toolu_task_1",
          tool: "Task",
          description: "Найти, где карточка теряет дайджест при рестарте",
          agent: "Explore",
          prompt: "Пройди путь карточки от `_show` до `owner_press` и найди, где после рестарта платформы дайджест кнопки может не совпасть с дайджестом карточки. Ответь путями и строками.",
          items: [
            { kind: "reads", data: { calls: [{ id: "toolu_s1", tool: "Read", input: { file_path: "/work/retinue/retinue/sessions.py" }, result: "…", error: false }, { id: "toolu_s2", tool: "Grep", input: { pattern: "add_button" }, result: "retinue/sessions.py:301", error: false }] } },
            msg("assistant", "Кнопки хранят дайджест на момент показа; `file_hash` пересчитывается при рестарте из смонтированного `/work`."),
          ],
          result: "Расхождение в `retinue/gate.py:88`: после рестарта `file_hash` берётся из пути платформы, а не из пути линии, поэтому дайджест другой. Кнопка верная, карточка — нет.",
        },
      },
      {
        kind: "edit",
        data: {
          id: "toolu_e1",
          tool: "Edit",
          path: "/work/retinue/retinue/gate.py",
          ok: true,
          error: "",
          old: '    path = tool_input.get("file_path")\n    digest = approvals.digest(tool, tool_input, cwd, approvals.file_hash(tool, tool_input, platform_root))\n    return digest',
          new: '    path = tool_input.get("file_path")\n    # the file as the lane sees it: through the platform\'s mount of its /work\n    file_hash = approvals.file_hash(tool, tool_input, lane.work_mount)\n    digest = approvals.digest(tool, tool_input, cwd, file_hash)\n    return digest',
        },
      },
      {
        kind: "tool",
        data: { id: "toolu_bash_1", tool: "Bash", input: { command: "cd /work/retinue && uv run --locked --with pytest pytest -q tests/test_gate.py", description: "Тесты гейта" }, result: "..........F.\n=================================== FAILURES ===================================\n___________________ test_press_after_restart_runs_once ___________________\nE   AssertionError: assert 'shown' == 'executed'\n1 failed, 11 passed in 2.31s", error: true },
      },
      msg("assistant", "Один тест ещё падает: после рестарта карточка остаётся `shown`. Прогоню оба файла с логами — нужен твой ок."),
      {
        kind: "tool",
        data: { id: "toolu_bash_2", tool: "Bash", input: { command: LONG_COMMAND }, result: null, error: false },
      },
      { kind: "result", data: { stop_reason: "tool_deferred", is_error: false, text: "", deferred: { id: "toolu_bash_2", name: "Bash" } } },
    ],
  ],
  "77889900": [
    [
      msg("user", "Собери конспект созвона с Acme в заметку, по пунктам."),
      msg("assistant", "Записываю конспект в `work/acme-call-2026-10-26.md`."),
      { kind: "edit", data: { id: "toolu_write_1", tool: "Write", path: writeInput.file_path, ok: null, error: "", content: writeInput.content } },
      { kind: "result", data: { stop_reason: "tool_deferred", is_error: false, text: "", deferred: { id: "toolu_write_1", name: "Write" } } },
    ],
  ],
  e5f6a7b8: [
    [
      msg("user", "Набросай план поездки в Лиссабон."),
      {
        kind: "question",
        data: {
          id: "toolu_q1",
          questions: [
            {
              header: "Жильё",
              question: "Где жить в Лиссабоне?",
              multiSelect: false,
              options: [
                { label: "Центр", description: "старый город, пешком до центра" },
                { label: "Окраина", description: "тише, парки, дальше от центра" },
              ],
            },
          ],
          answer: "Центр",
        },
      },
      msg("assistant", "Хорошо, **Центр**. Собираю план по дням:\n\n| День | Что |\n|---|---|\n| 7 мая | прилёт, заселение, ужин |\n| 8 мая | старый город, музей |\n| 9 мая | музей азулежу |"),
    ],
  ],
  "5566aabb": [
    [
      msg("user", "Проверь битые ссылки в Витринах."),
      { kind: "tool", data: { id: "toolu_lc", tool: "Bash", input: { command: "just check" }, result: "ok: 412 records, 0 broken links", error: false } },
    ],
  ],
  c9d0e1f2: [
    [
      msg("user", "Выпусти 0.4.0: changelog и тег."),
      { kind: "edit", data: { id: "toolu_cl", tool: "MultiEdit", path: "/work/retinue/CHANGELOG.md", ok: true, error: "", edits: [{ old: "## Unreleased", new: "## 0.4.0 — 2026-10-25" }, { old: "- face: draft", new: "- face: the PWA (sessions, cards, diff)" }] } },
      { kind: "tool", data: { id: "toolu_tag", tool: "Bash", input: { command: 'git tag -a v0.4.0 -m "Retinue 0.4.0"' }, result: "", error: false } },
      { kind: "tool", data: { id: "toolu_push", tool: "Bash", input: { command: "git push origin main --follow-tags" }, result: "Отправку делаешь ты: команда пришла курьерской карточкой.", error: true } },
      msg("assistant", "Готово: changelog и тег `v0.4.0`. Отправку (`git push`) сделай сам — команда в курьерской карточке."),
      { kind: "result", data: { stop_reason: "end_turn", is_error: false, text: "Готово.", deferred: null } },
    ],
  ],
  "0a1b2c3d": [
    [
      msg("user", "Как устроены лимиты на линии?"),
      {
        kind: "plan",
        data: { id: "toolu_plan", tool: "ExitPlanMode", plan: "1. На линии одновременно идут **не больше 2** сессий, всего — не больше 5.\n2. Остальные ждут в очереди `queue`, старшие первыми.\n3. Сессия без движения 7 дней закрывается." },
      },
      msg("assistant", "Коротко: две сессии на линию, пять на всю платформу, остальное ждёт очереди."),
      { kind: "result", data: { stop_reason: "end_turn", is_error: false, text: "", deferred: null } },
    ],
  ],
};

export function items(): Item[] {
  return Object.entries(turns).flatMap(([session, list]) =>
    list.flatMap((turn, t) => turn.map((it, n) => ({ ...it, session, turn: t, n }) as Item)),
  );
}

export const summaries: Summary[] = [
  { session: "a1b2c3d4", seq: 1, head: "Итог · retinue · сессия a1b2c3d4", text: "За неделю гейт не менялся; падение воспроизводится. Два открытых issue про рестарт (#41, #38) — это оно. Дальше чиню.", at: T0 - 3000, turn: 0, kind: "decision", replies: [], answered: "Чини" },
  { session: "e5f6a7b8", seq: 5, head: "Итог · notes · сессия e5f6a7b8", text: "Хорошо, **Центр**. Собираю план по дням:\n\n| День | Что |\n|---|---|\n| 7 мая | прилёт, заселение, ужин |\n| 8 мая | старый город, музей |\n| 9 мая | музей азулежу |", at: T0 - 25, turn: 0, kind: "decision", replies: ["Добавь Синтру", "Бронируй жильё", "Пока так"], answered: null },
  { session: "c9d0e1f2", seq: 3, head: "Итог · retinue · сессия c9d0e1f2", text: "Готово: changelog и тег `v0.4.0`. Отправку (`git push`) сделай сам — команда в курьерской карточке.", at: T0 - 80000 },
  { session: "0a1b2c3d", seq: 4, head: "Итог · notes · сессия 0a1b2c3d", text: "Ход закончен без текста.", at: T0 - 5400 },
];

// Files handed over from `out/` (retinue/outfiles.py), with their bytes: the mock serves them as the platform does.
const PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);
// The owner's messages, placed before what the session had not yet written.
export const inputs: Input[] = [
  { session: "a1b2c3d4", input_id: "tg:1", text: "Сначала посмотри, что вообще менялось в гейте за неделю, и есть ли открытые issues про рестарт.", at: T0 - 3600, turn: 0, n: 0, attachments: [] },
];

// «Сводка»: what each session's init line said (retinue/sessions.py `face_about`).
export function about(s: Session): About {
  const code = s.kind === "code";
  return {
    cwd: `/work/${s.track}`,
    context_tokens: code ? 84_200 : 21_500,
    model: "claude-opus-5-5",
    cli_version: "2.1.304",
    permission_mode: code ? "default" : "auto",
    mcp: [
      { name: "retinue", status: "connected" },
      ...(code ? [{ name: "github", status: "connected" }, { name: "playwright", status: "failed" }] : []),
    ],
  };
}

// A push that went out and opened a PR (retinue/topush.py): the closed release session's.
export const pushCards: PushCard[] = [
  {
    id: "p0a1b2c3", session: "c9d0e1f2", repo: "retinue", state: "pushed", place: "retinue", branch: "room/c9d0e1f2",
    target: "release-0.4.0", base: "main", custom: false, title: "Release 0.4.0", commits: [], files: [], ahead: 2,
    workflows: [], dirty: [], partial: false, url: "https://github.com/owner/retinue/pull/42", said: "Запушено.",
    created: T0 - 80000, at: T0 - 79900, view: { head: [], body: "", status: "" },
  },
];

export const files: { file: OutFile; body: Buffer }[] = [
  {
    file: { id: "f3a1b2c3d4e5f607", session: "a1b2c3d4", path: "gate-trace.md", name: "gate-trace.md", size: 0, mime: "text/markdown; charset=utf-8", at: T0 - 200 },
    body: Buffer.from("# Путь карточки\n\n1. `_show`\n2. `owner_press`\n3. `_pressable`\n"),
  },
  {
    file: { id: "f0a1b2c3d4e5f607", session: "c9d0e1f2", path: "report.md", name: "report.md", size: 0, mime: "text/markdown; charset=utf-8", at: T0 - 80000 },
    body: Buffer.from("# Релиз v0.4.0\n\n- changelog собран\n- тег поставлен\n\n![график](https://example.com/x.png)\n"),
  },
  {
    file: { id: "f1a1b2c3d4e5f607", session: "c9d0e1f2", path: "pages/summary.html", name: "summary.html", size: 0, mime: "text/html; charset=utf-8", at: T0 - 79990 },
    body: Buffer.from("<!doctype html><meta charset=utf-8><style>body{font:16px system-ui;padding:1rem}</style><h1>Итог</h1><p>Страница в песочнице.</p><script>document.body.append('скрипт выполнился — так быть не должно')</script>"),
  },
  {
    file: { id: "f2a1b2c3d4e5f607", session: "c9d0e1f2", path: "chart.png", name: "chart.png", size: 0, mime: "image/png", at: T0 - 79980 },
    body: PIXEL,
  },
].map(({ file, body }) => ({ file: { ...file, size: body.length }, body }));

export const DIFF = `diff --git a/retinue/gate.py b/retinue/gate.py
index 3f2a1c9..8b7d0e4 100644
--- a/retinue/gate.py
+++ b/retinue/gate.py
@@ -80,10 +80,12 @@ def bind(tool: str, tool_input: dict, cwd: str, lane: LaneRef) -> tuple[str, str]:
     """The digest a card is bound to: the call, where it runs, and the file it would change as the lane sees it."""
     if tool not in transcript.EDITS:
         return approvals.digest(tool, tool_input, cwd, ""), ""
     path = tool_input.get("file_path")
-    digest = approvals.digest(tool, tool_input, cwd, approvals.file_hash(tool, tool_input, platform_root))
-    return digest
+    # the file as the lane sees it: through the platform's mount of its /work
+    file_hash = approvals.file_hash(tool, tool_input, lane.work_mount)
+    digest = approvals.digest(tool, tool_input, cwd, file_hash)
+    return digest, file_hash
 
 
 def stale(approval: dict, lane: LaneRef) -> bool:
-    return approval["file_hash"] != approvals.file_hash(approval["tool"], approval["input"], platform_root)
+    return approval["file_hash"] != approvals.file_hash(approval["tool"], approval["input"], lane.work_mount)
diff --git a/tests/test_gate.py b/tests/test_gate.py
index 1d4e5f6..2a3b4c5 100644
--- a/tests/test_gate.py
+++ b/tests/test_gate.py
@@ -210,6 +210,22 @@ def test_press_runs_once(platform, lane):
     assert platform.store.approval(card)["state"] == "executed"
 
 
+def test_press_after_restart_runs_once(tmp_path, platform_factory, lane):
+    """E4: a card pressed an hour later, after a restart of the stack, runs the call once."""
+    platform = platform_factory(tmp_path)
+    card = ask(platform, lane, "Edit", {"file_path": "/work/app/main.py", "old_string": "a", "new_string": "b"})
+    platform = platform_factory(tmp_path)  # the restart: a new process over the same SQLite
+    clock.advance(3600)
+    assert press(platform, card, "approve")["toast"] == "Разрешено."
+    assert press(platform, card, "approve")["toast"] == "Карточка устарела — решение не принято."
+    assert executions(lane, card) == 1
+
+
+def test_stale_when_file_changed(platform, lane):
+    card = ask(platform, lane, "Edit", {"file_path": "/work/app/main.py", "old_string": "a", "new_string": "b"})
+    (lane.work / "app" / "main.py").write_text("changed")
+    assert gate.stale(platform.store.approval(card), lane.ref)
+
 def test_never_list_is_never_granted(platform, lane):
     card = ask(platform, lane, "Bash", {"command": "git push origin main"})
     assert platform.store.approval(card)["kind"] == "courier"
diff --git a/docs/face.md b/docs/face.md
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/docs/face.md
@@ -0,0 +1,6 @@
+# The face
+
+The PWA the owner opens on the phone over WireGuard: sessions by track, a live transcript, approval cards,
+the changes of a code session, and two sessions side by side on a wide screen.
+
+It is served by nginx from the image; \`/api/*\` goes to the platform's face API on :9600.
`;

export const STAT = ` docs/face.md       |  6 ++++++
 retinue/gate.py    |  8 +++++---
 tests/test_gate.py | 16 ++++++++++++++++
 3 files changed, 27 insertions(+), 3 deletions(-)`;

export const STREAM_TEXT = [
  "Проверяю ",
  "Витрины ",
  "по очереди: ",
  "`work/AGENTS.md`, ",
  "`personal/AGENTS.md`, ",
  "`travel/AGENTS.md`.\n\n",
  "Пока нашёл **одну** ",
  "битую ссылку: ",
  "`travel/journal/2026-04-23-ams-lis-may7.md` ",
  "ссылается на файл, ",
  "которого нет.",
];
