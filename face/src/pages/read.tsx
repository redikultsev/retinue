// `/pages`, «Pages» (once «Read», `/read`): the life hub's pages by section with search, «Continue» (opened, not finished) on top. A page
// opens on the hub's own origin, never inside the app (lib/library.ts); what was opened and «Finished» are kept by the
// platform, so the phone and the desktop agree.
import { PageHeader, PAGE_X } from "@/components/page-header";
import { useMemo, useState } from "react";
import { Check, Circle } from "@/components/icons";
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
import { cn, LIST } from "@/lib/utils";

/** «42 min · updated 27 Oct · opened 14:05». */
function details(page: LibraryPage, now: number): string {
  const parts: string[] = [];
  if (page.minutes) parts.push(`${page.minutes} min`);
  const updated = dayStart(page.updated);
  if (!Number.isNaN(updated)) parts.push(`updated ${when(updated, now)}`);
  if (page.done !== null) parts.push(changedSinceDone(page) ? "changed since you finished it" : "finished");
  else if (page.opened !== null) parts.push(`opened ${when(page.opened, now)}`);
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
    <li data-testid="library-page" className="group/row flex items-start gap-2 px-3 py-2.5">
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
          <span className="text-[15px] md:text-sm" title="The page's address is not on the hub: not opening it">
            {title}
          </span>
        )}
        <span className="text-xs text-muted-foreground">{details(page, now)}</span>
      </div>
      {/* a toggle, not a button per row: a ring when unread, a filled check once finished */}
      <button
        type="button"
        aria-pressed={done}
        aria-label={done ? `Finished: ${page.title}. Mark as unread` : `Mark as finished: ${page.title}`}
        title={done ? "Finished — mark as unread" : "Mark as finished"}
        onClick={() => onDone(page, !done)}
        className={cn(
          "hit inline-flex size-8 shrink-0 items-center justify-center rounded-full transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none [&_svg]:size-4",
          done ? "bg-success/15 text-success hover:bg-success/25" : "text-muted-foreground hover:bg-accent hover:text-foreground group-hover/row:text-foreground",
        )}
      >
        {done ? <Check /> : <Circle />}
      </button>
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
      <PageHeader title="Pages">
        {pages.length > 0 && (
          <Input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Find a page"
            aria-label="Find a page"
          />
        )}
      </PageHeader>
      <main className={cn("mx-auto flex w-full max-w-3xl min-h-0 flex-1 flex-col gap-5 overflow-y-auto pt-4 pb-[calc(env(safe-area-inset-bottom)+1rem)]", PAGE_X)}>
        {!library ? (
          error ? (
            <div className="flex flex-col items-start gap-2">
              <p className="text-sm text-destructive">{error}</p>
              {onRetry && (
                <Button size="sm" variant="outline" onClick={onRetry}>
                  Try again
                </Button>
              )}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Loading…</p>
          )
        ) : pages.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {library.at === null
              ? "The list of pages has not come from the hub yet."
              : "No pages. A page shows up here once it is published to the hub."}
          </p>
        ) : (
          <>
            {going.length > 0 && (
              <section aria-label="Continue" className="flex flex-col gap-2">
                <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                  Continue <span className="tabular-nums">({going.length})</span>
                </h2>
                <ul className={LIST}>{going.map(row)}</ul>
              </section>
            )}
            {grouped.length === 0 ? (
              <p className="text-sm text-muted-foreground">Nothing found.</p>
            ) : (
              grouped.map((section) => (
                <section key={section.key || "-"} aria-label={section.title} className="flex flex-col gap-2">
                  <h2 className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                    {section.icon && <span aria-hidden="true">{section.icon}</span>}
                    <span>{section.title}</span>
                    <span className="tabular-nums">({section.pages.length})</span>
                  </h2>
                  <ul className={LIST}>{section.pages.map(row)}</ul>
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
      () => undefined, // «opened» is a convenience: the page opened either way
    );
  };
  const onDone = (page: LibraryPage, done: boolean) => {
    libraryApi.done(page.id, done).then(
      (r) => put(r.page),
      (e: unknown) => toast(e instanceof Error ? e.message : "Not saved.", "error"),
    );
  };
  return <ReadView library={library} error={error} onOpen={onOpen} onDone={onDone} onRetry={() => void reload()} />;
}
