// `/read`, «Читать»: the life hub's pages by section with search, «Продолжить» (opened, not finished) on top. A page
// opens on the hub's own origin, never inside the app (lib/library.ts); what was opened and «Дочитал» are kept by the
// platform, so the phone and the desktop agree.
import { useMemo, useState } from "react";
import { Check } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { when } from "@/lib/format";
import {
  changedSinceDone,
  continuing,
  dayStart,
  type Library,
  type LibraryPage,
  libraryApi,
  safeUrl,
  sections,
  useLibrary,
} from "@/lib/library";
import { openInTelegram } from "@/lib/telegram";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

/** «42 мин · обновлена 27 окт. · открыта 14:05». */
function details(page: LibraryPage, now: number): string {
  const parts: string[] = [];
  if (page.minutes) parts.push(`${page.minutes} мин`);
  const updated = dayStart(page.updated);
  if (!Number.isNaN(updated)) parts.push(`обновлена ${when(updated, now)}`);
  if (page.done !== null) parts.push(changedSinceDone(page) ? "изменилась после прочтения" : "дочитана");
  else if (page.opened !== null) parts.push(`открыта ${when(page.opened, now)}`);
  return parts.join(" · ");
}

function PageRow({
  page,
  hub,
  now,
  onOpen,
  onDone,
}: {
  page: LibraryPage;
  hub: string;
  now: number;
  onOpen: (page: LibraryPage) => void;
  onDone: (page: LibraryPage, done: boolean) => void;
}) {
  const href = safeUrl(page.url, hub);
  const done = page.done !== null;
  const title = <span className={cn("font-medium break-words", done && "text-muted-foreground")}>{page.title}</span>;
  return (
    <li data-testid="library-page" className="flex items-start gap-2 rounded-lg border bg-background p-3">
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[15px] underline-offset-4 hover:underline md:text-sm"
            onClick={(event) => {
              if (openInTelegram(href)) event.preventDefault();
              onOpen(page);
            }}
          >
            {title}
          </a>
        ) : (
          <span className="text-[15px] md:text-sm" title="Адрес страницы не с хаба: не открываю">
            {title}
          </span>
        )}
        <span className="text-xs text-muted-foreground">{details(page, now)}</span>
      </div>
      <Button
        size="sm"
        variant={done ? "secondary" : "outline"}
        aria-pressed={done}
        title={done ? "Вернуть в непрочитанные" : "Отметить, что дочитал"}
        onClick={() => onDone(page, !done)}
      >
        {done && <Check />}
        Дочитал
      </Button>
    </li>
  );
}

export function ReadView({
  library,
  error,
  now = Date.now() / 1000,
  onOpen,
  onDone,
  onRetry,
}: {
  library: Library | null;
  error?: string | null;
  now?: number;
  onOpen: (page: LibraryPage) => void;
  onDone: (page: LibraryPage, done: boolean) => void;
  onRetry?: () => void;
}) {
  const [query, setQuery] = useState("");
  const pages = library?.pages ?? [];
  const grouped = useMemo(() => sections(pages, query), [pages, query]);
  const going = useMemo(() => (query.trim() ? [] : continuing(pages)), [pages, query]);
  const row = (page: LibraryPage) => (
    <PageRow key={page.id} page={page} hub={library?.hub ?? ""} now={now} onOpen={onOpen} onDone={onDone} />
  );
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <header className="sticky top-0 z-10 flex shrink-0 flex-col gap-2 border-b bg-background px-3 pt-[calc(env(safe-area-inset-top)+0.75rem)] pb-3">
        <h1 className="text-xl font-semibold">Читать</h1>
        {pages.length > 0 && (
          <Input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Найти страницу"
            aria-label="Найти страницу"
          />
        )}
      </header>
      <main className="mx-auto flex w-full max-w-3xl min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-3 pt-3 pb-[calc(env(safe-area-inset-bottom)+1rem)]">
        {!library ? (
          error ? (
            <div className="flex flex-col items-start gap-2">
              <p className="text-sm text-destructive">{error}</p>
              {onRetry && (
                <Button size="sm" variant="outline" onClick={onRetry}>
                  Ещё раз
                </Button>
              )}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Загружаю…</p>
          )
        ) : pages.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {library.at === null
              ? "Список страниц ещё не пришёл с хаба."
              : "Страниц нет. Страница появится здесь, когда её опубликуют в хаб."}
          </p>
        ) : (
          <>
            {going.length > 0 && (
              <section aria-label="Продолжить" className="flex flex-col gap-2">
                <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                  Продолжить <span className="tabular-nums">({going.length})</span>
                </h2>
                <ul className="flex flex-col gap-2">{going.map(row)}</ul>
              </section>
            )}
            {grouped.length === 0 ? (
              <p className="text-sm text-muted-foreground">Ничего не нашлось.</p>
            ) : (
              grouped.map((section) => (
                <section key={section.key || "-"} aria-label={section.title} className="flex flex-col gap-2">
                  <h2 className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                    {section.icon && <span aria-hidden="true">{section.icon}</span>}
                    <span>{section.title}</span>
                    <span className="tabular-nums">({section.pages.length})</span>
                  </h2>
                  <ul className="flex flex-col gap-2">{section.pages.map(row)}</ul>
                </section>
              ))
            )}
          </>
        )}
      </main>
    </div>
  );
}

export function ReadPage() {
  const { library, error, reload, put } = useLibrary();
  const onOpen = (page: LibraryPage) => {
    put({ ...page, opened: Date.now() / 1000 }); // at once: the tab may take the app to the background
    libraryApi.open(page.id).then(
      (r) => put(r.page),
      () => undefined, // «открыта» is a convenience: the page opened either way
    );
  };
  const onDone = (page: LibraryPage, done: boolean) => {
    libraryApi.done(page.id, done).then(
      (r) => put(r.page),
      (e: unknown) => toast(e instanceof Error ? e.message : "Не сохранилось.", "error"),
    );
  };
  return <ReadView library={library} error={error} onOpen={onOpen} onDone={onDone} onRetry={() => void reload()} />;
}
