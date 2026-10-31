// The app's frame around its tabs and sessions.
//
// - A phone: the page, and under it the tab bar «Today · Sessions · ＋ · Spaces · Read». «＋» is `/new`, an
//   empty new task with the composer at the bottom (pages/new.tsx), as a new chat in Claude's apps. The bar is there on
//   the tabs' own screens (a session's screen has its own input box and a way back), sits above the home indicator
//   (the safe area) and steps aside while the keyboard is up.
// - A wide screen (≥ 1024 px), three columns as in Claude's desktop app: on the left (272 px) «New task», what
//   waits («Today» with its count), the sessions by sphere and the Spaces; in the middle the page — `/new` too; on
//   the right one panel with tabs: a session's «Summary» (foldable, components/session-summary.tsx) and, when one is
//   open, a Record, a session's diff or a file (`?pane=`, lib/pane.ts). The pane's tab is the URL's; «Summary» closes it.
//   The columns are resized by dragging the lines between them (the left one folds away when dragged past its
//   minimum, and comes back when dragged out); each layout is remembered in the browser.
import { Link, useRouter, useRouterState } from "@tanstack/react-router";
import { lazy, Suspense, useEffect, useState } from "react";
import { Group, type Layout, type LayoutChangedMeta, Panel, Separator } from "react-resizable-panels";
import { AppHeader } from "@/components/app-header";
import { FileRow } from "@/components/files";
import { Book, Chart, Close, Grid, Home, List, Plus } from "@/components/icons";
import { SessionList } from "@/components/session-list";
import { SessionSummary } from "@/components/session-summary";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { useKeyboardUp } from "@/lib/keyboard";
import { useVeryWide, useWide } from "@/lib/media";
import { type Pane, usePane } from "@/lib/pane";
import { setSummaryOpen, useSummaryOpen } from "@/lib/summary";
import type { OutFile } from "@/lib/types";
import { cn } from "@/lib/utils";
import { TodayCount } from "@/pages/today";
import { RecordPage } from "@/pages/spaces";

const Changes = lazy(() => import("@/components/changes").then((m) => ({ default: m.Changes })));

/** The screens the tab bar stands under on a phone. */
export const TABBED = /^\/(today|sessions|new|read|spaces|week)(\/|$)/;

const TABS = [
  { to: "/today", label: "Today", Icon: Home },
  { to: "/sessions", label: "Sessions", Icon: List },
  null, // «＋»
  { to: "/spaces", label: "Spaces", Icon: Grid },
  { to: "/read", label: "Read", Icon: Book },
] as const;

export function TabBar({ pathname }: { pathname: string }) {
  return (
    <nav
      aria-label="Sections"
      className="grid shrink-0 grid-cols-[1fr_1fr_4rem_1fr_1fr] border-t bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur"
    >
      {TABS.map((tab) =>
        tab === null ? (
          <div key="plus" className="flex items-center justify-center">
            <Link
              to="/new"
              aria-label="New task"
              aria-current={pathname === "/new" ? "page" : undefined}
              className={cn(
                "inline-flex size-12 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-sm active:scale-95",
                pathname === "/new" && "ring-4 ring-primary/25",
              )}
            >
              <Plus className="size-6" />
            </Link>
          </div>
        ) : (
          <Link
            key={tab.to}
            to={tab.to}
            aria-current={pathname.startsWith(tab.to) ? "page" : undefined}
            className={cn(
              "relative flex min-h-14 flex-col items-center justify-center gap-0.5 text-[11px]",
              pathname.startsWith(tab.to) ? "font-medium text-primary" : "text-muted-foreground",
            )}
          >
            <tab.Icon className="size-5" />
            <span className="max-w-full truncate tracking-tight">{tab.label}</span>
            {tab.to === "/today" && <TodayCount className="absolute top-1 left-1/2 ml-2 h-4 min-w-4 rounded-full px-1 text-[10px]" />}
          </Link>
        ),
      )}
    </nav>
  );
}

const navClass = (on: boolean) =>
  cn(
    "flex h-8 items-center gap-2 rounded-md px-2 text-[13px] hover:bg-accent/70 pointer-coarse:min-h-11 [&_svg]:size-3.5",
    on && "bg-accent font-medium",
  );

/** The left column on a wide screen: the app's places on top, fixed; under them the sessions, the one part that
 * scrolls. Starting a session in a sphere is the composer's sphere picker (pages/new.tsx); the Spaces are a page. */
export function Sidebar({ pathname, current, beside }: { pathname: string; current?: string; beside?: (id: string) => void }) {
  return (
    <aside className="flex h-full w-full flex-col border-r bg-sidebar">
      <AppHeader />
      <nav aria-label="Places" className="flex shrink-0 flex-col px-2 pt-2 pb-2">
        <Link to="/new" className={navClass(pathname === "/new")}>
          <Plus className="size-4" />
          New task
        </Link>
        <Link to="/today" className={navClass(pathname.startsWith("/today"))}>
          <Home className="size-4" />
          <span className="flex-1">Today</span>
          <TodayCount />
        </Link>
        <Link to="/read" className={navClass(pathname.startsWith("/read"))}>
          <Book className="size-4" />
          Read
        </Link>
        <Link to="/spaces" className={navClass(pathname.startsWith("/spaces"))}>
          <Grid className="size-4" />
          Spaces
        </Link>
        <Link to="/week" className={navClass(pathname.startsWith("/week"))}>
          <Chart className="size-4" />
          Week
        </Link>
      </nav>
      <SessionList current={current} beside={beside} fill />
    </aside>
  );
}

function FilePane({ id }: { id: string }) {
  const [file, setFile] = useState<OutFile | null>(null);
  const [missing, setMissing] = useState<string | null>(null);
  useEffect(() => {
    let gone = false;
    setFile(null);
    api
      .file(id)
      .then(({ file: found }) => !gone && setFile(found))
      .catch((e) => !gone && setMissing(e instanceof ApiError && e.status === 404 ? "There is no such file." : "The file did not load."));
    return () => {
      gone = true;
    };
  }, [id]);
  if (!file) return <p className="text-sm text-muted-foreground">{missing ?? "Loading…"}</p>;
  return (
    <ul>
      <FileRow file={file} opened />
    </ul>
  );
}

const PANE_TITLES: Record<Pane["kind"], string> = { record: "Record", diff: "Changes", file: "File" };

/** The right column: one panel, its tabs a session's «Summary» and what is open beside the page (`pane`). */
export function PaneView({ pane, session, onClose }: { pane: Pane | null; session?: string; onClose: () => void }) {
  const { open } = usePane();
  const active = pane ? pane.kind : "summary";
  const tabs = [
    ...(session ? [{ key: "summary", label: "Summary" }] : []),
    ...(pane ? [{ key: pane.kind, label: PANE_TITLES[pane.kind] }] : []),
  ];
  return (
    <aside
      aria-label={pane ? PANE_TITLES[pane.kind] : "Summary"}
      className="flex h-full w-full flex-col border-l bg-sidebar"
    >
      <header className="flex min-h-12 shrink-0 items-center gap-1 border-b px-2">
        <div role="tablist" className="flex min-w-0 flex-1 items-center gap-0.5">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              role="tab"
              aria-selected={t.key === active}
              onClick={() => {
                if (t.key === "summary" && pane) {
                  setSummaryOpen(true);
                  onClose();
                }
              }}
              className={cn(
                "inline-flex h-7 items-center rounded-md px-2 text-[13px]",
                t.key === active ? "bg-accent font-medium text-foreground" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Close"
          onClick={() => (pane ? onClose() : setSummaryOpen(false))}
          className="size-8 md:size-8"
        >
          <Close />
        </Button>
      </header>
      <div className={cn("min-h-0 flex-1 overflow-y-auto", pane && "p-3")}>
        {!pane && session ? (
          <SessionSummary
            id={session}
            onDiff={() => open({ kind: "diff", session })}
            onFile={(id) => open({ kind: "file", id })}
          />
        ) : pane?.kind === "record" ? (
          <RecordPage space={pane.space} path={pane.path} pane />
        ) : pane?.kind === "diff" ? (
          <Suspense fallback={<p className="text-sm text-muted-foreground">Loading the changes…</p>}>
            <Changes session={pane.session} />
          </Suspense>
        ) : pane ? (
          <FilePane id={pane.id} />
        ) : null}
      </div>
    </aside>
  );
}

/** The columns' widths in pixels, one per column whatever else is open, kept in the browser; a private window or
 * blocked storage just forgets them. */
const SIZES_KEY = "retinue-columns";

function readSizes(): Record<string, number> {
  try {
    const sizes = JSON.parse(localStorage.getItem(SIZES_KEY) ?? "{}");
    return sizes && typeof sizes === "object" ? sizes : {};
  } catch {
    return {};
  }
}

/** Keeps what the owner dragged (the library's layout is in percent of the window), never a layout it worked out. */
function saveSizes(layout: Layout, meta: LayoutChangedMeta) {
  if (!meta.isUserInteraction) return;
  const sizes = readSizes();
  for (const [id, percent] of Object.entries(meta.requestedLayout ?? layout))
    if (id !== "main") sizes[id] = Math.round((percent * window.innerWidth) / 100);
  try {
    localStorage.setItem(SIZES_KEY, JSON.stringify(sizes));
  } catch {
    // nothing to keep it in
  }
}

const separatorClass =
  "w-1 shrink-0 bg-transparent transition-colors hover:bg-primary/30 focus-visible:bg-primary/40 data-[separator=active]:bg-primary/50";

/** The three columns of a wide screen; the right one only while it has something to show. */
function Columns({ sidebar, main, right, rightId }: { sidebar: React.ReactNode; main: React.ReactNode; right: React.ReactNode; rightId: "summary" | "pane" | null }) {
  const sizes = readSizes();
  return (
    <Group orientation="horizontal" className="h-dvh" onLayoutChanged={saveSizes}>
      <Panel
        id="sidebar"
        defaultSize={sizes.sidebar ?? 272}
        minSize={200}
        maxSize={440}
        collapsible
        collapsedSize={0}
        groupResizeBehavior="preserve-pixel-size"
      >
        {sidebar}
      </Panel>
      <Separator aria-label="Left column width" className={separatorClass} />
      <Panel id="main" minSize={360}>
        {main}
      </Panel>
      {rightId && (
        <>
          <Separator aria-label="Right panel width" className={separatorClass} />
          <Panel
            key={rightId}
            id={rightId}
            defaultSize={sizes[rightId] ?? (rightId === "pane" ? 576 : 320)}
            minSize={260}
            maxSize="60%"
            groupResizeBehavior="preserve-pixel-size"
          >
            {right}
          </Panel>
        </>
      )}
    </Group>
  );
}

export function Shell({ children }: { children: React.ReactNode }) {
  const wide = useWide();
  const veryWide = useVeryWide();
  const keyboard = useKeyboardUp();
  const router = useRouter();
  const location = useRouterState({ select: (s) => s.location });
  const { pane, close } = usePane();
  const summaryOpen = useSummaryOpen();

  const current = /^\/s\/([^/]+)/.exec(location.pathname)?.[1];
  const search = location.search as Record<string, unknown>;
  const beside =
    veryWide && current
      ? (other: string) => {
          const tab = typeof search.tab === "string" ? search.tab : undefined;
          const pane = typeof search.pane === "string" ? search.pane : undefined;
          router.history.push(`/s/${current}${router.options.stringifySearch({ tab, with: other, pane })}`);
        }
      : undefined;

  // «Summary» beside one session (two sessions side by side take that room).
  const summarized = current && !(veryWide && typeof search.with === "string") ? current : undefined;
  if (wide) {
    const rightId = pane ? "pane" : summarized && summaryOpen ? "summary" : null;
    return (
      <Columns
        sidebar={<Sidebar pathname={location.pathname} current={current} beside={beside} />}
        // The middle scrolls by itself: a page that scrolls its own column and one that just grows both fit.
        main={<main className="h-full min-h-0 min-w-0 overflow-y-auto">{children}</main>}
        right={rightId && <PaneView pane={pane} session={summarized} onClose={close} />}
        rightId={rightId}
      />
    );
  }
  const tabbed = TABBED.test(location.pathname);
  return (
    <div className="flex h-dvh flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      {tabbed && !keyboard && <TabBar pathname={location.pathname} />}
    </div>
  );
}
