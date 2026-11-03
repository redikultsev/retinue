// One session on screen: a header of one line — its state, its title, the session it hangs under (an icon and that
// session's title), its tabs «Chat» and «Results N», and at the right a side question, its menu and «Details» (ⓘ) —
// then the tab. «Chat» is the conversation (components/transcript) over «Waiting for you» (what in it waits on the
// owner, with its buttons), the plan Claude Code pins under its last message, and the input box. «Results» is
// everything the session made (components/results.tsx); a finished session opens on it, one working or waiting on the
// conversation. «Details» — where it runs, its model, mode, context, connectors, its id — is the right panel on a wide
// screen, closed until asked for, and a sheet on a phone. Two of these stand side by side on a very wide screen, both
// fed by the app's one stream.
import { Link, useRouter } from "@tanstack/react-router";
import { lazy, type ReactNode, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Composer } from "@/components/composer";
import { ArrowLeft, ChevronRight, Close, Info, ListTodo } from "@/components/icons";
import { RELATION_WORDS, RelationIcon, upOf } from "@/components/relation";
import { Results, useResults } from "@/components/results";
import { SessionMenu } from "@/components/session-menu";
import { SessionSummary } from "@/components/session-summary";
import { SideQuestion } from "@/components/side-question";
import { TodoList } from "@/components/transcript/item";
import { Transcript } from "@/components/transcript/transcript";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { WaitingDock, useWaiting } from "@/components/waiting-dock";
import { sessionStateWord } from "@/lib/format";
import { useKeyboardUp } from "@/lib/keyboard";
import { live, useLiveState } from "@/lib/live";
import { useWide } from "@/lib/media";
import { defaultTab } from "@/lib/results";
import { useMarkSeen } from "@/lib/seen";
import { approvalsOf, orderedInputs, orderedItems, orderedSummaries, proposalsOf } from "@/lib/store";
import { latestPlan, planLine, setSummaryOpen, useSummaryOpen } from "@/lib/summary";
import { QUEUED } from "@/lib/types";
import { cn } from "@/lib/utils";

// The diff renderer (@pierre/diffs and Shiki) is loaded only when the changes are opened.
const Changes = lazy(() => import("@/components/changes").then((m) => ({ default: m.Changes })));

/** «changes»: a code session's diff on a phone, opened from «Results» (the right pane has it on a wide screen). */
export type Tab = "chat" | "results" | "changes";

interface Props {
  id: string;
  /** Absent: the tab the session opens on (lib/results.ts `defaultTab`). */
  tab?: Tab;
  onTab: (tab: Tab) => void;
  /** Mobile: a back arrow to the list. */
  back?: boolean;
  /** Side by side: closes this panel. */
  onClose?: () => void;
  extra?: ReactNode;
  /** The right panel can show this session's «Details» (a wide screen, one session in the middle). */
  panel?: boolean;
  /** Open the chat at this turn's summary, marked, instead of at the bottom. */
  turn?: number;
}

/** The latest plan, pinned above the input box as Claude Code pins its task list: one line, open on a tap. */
function PinnedPlan({ items }: { items: ReturnType<typeof orderedItems> }) {
  const plan = useMemo(() => latestPlan(items), [items]);
  if (!plan || plan.length === 0) return null;
  const { done, total, now } = planLine(plan);
  if (done === total) return null;
  return (
    <details data-testid="pinned-plan" className="group/plan mx-auto mb-1.5 w-full max-w-[46rem] rounded-lg border bg-card">
      <summary className="flex h-8 cursor-pointer list-none items-center gap-2 px-2.5 text-[13px] select-none pointer-coarse:min-h-11 [&_svg]:size-3.5">
        <ListTodo className="text-muted-foreground" />
        <span className="shrink-0 tabular-nums text-muted-foreground">{`${done} of ${total}`}</span>
        <span className="min-w-0 flex-1 truncate">{now ?? "Plan"}</span>
        <ChevronRight className="fold-chevron size-3 text-muted-foreground transition-transform" />
      </summary>
      <div className="max-h-48 overflow-y-auto border-t px-2.5 py-2">
        <TodoList todos={plan} small />
      </div>
    </details>
  );
}

/** «Chat» · «Results N», underlined as the other screens' header tabs. */
function Tabs({ tab, onTab, results }: { tab: Tab; onTab: (tab: Tab) => void; results: number }) {
  const tabs: [Tab, string, number?][] = [
    ["chat", "Chat"],
    ["results", "Results", results],
  ];
  return (
    <div role="tablist" aria-label="Session" className="flex shrink-0 items-stretch self-stretch max-md:order-last max-md:-mx-2 max-md:h-11 max-md:w-[calc(100%+1rem)] max-md:border-t max-md:px-2">
      {tabs.map(([value, label, count]) => {
        const on = tab === value || (value === "results" && tab === "changes");
        return (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={on}
            onClick={() => onTab(value)}
            className={cn(
              "-mb-px inline-flex items-center gap-1.5 border-b-2 px-2.5 text-[13px] whitespace-nowrap transition-colors max-md:flex-1 max-md:justify-center",
              on ? "border-foreground text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {label}
            {count ? <span className="font-mono text-xs text-muted-foreground tabular-nums">{count}</span> : null}
          </button>
        );
      })}
    </div>
  );
}

export function SessionPanel({ id, tab: asked, onTab, back, onClose, extra, panel = false, turn }: Props) {
  const router = useRouter();
  const state = useLiveState();
  const keyboard = useKeyboardUp();
  const [error, setError] = useState<string | null>(null);
  const session = state.sessions[id];
  const loaded = id in state.floors;
  const itemsOf = state.items[id];
  const items = useMemo(() => orderedItems(itemsOf), [itemsOf]);
  const allApprovals = state.approvals;
  const approvals = useMemo(() => approvalsOf(allApprovals, id), [allApprovals, id]);
  const allProposals = state.proposals;
  const proposals = useMemo(() => proposalsOf(allProposals, id), [allProposals, id]);
  const delta = state.deltas[id];
  const summariesOf = state.summaries[id];
  const summaries = useMemo(() => orderedSummaries(summariesOf), [summariesOf]);
  const inputsOf = state.inputs[id];
  const inputs = useMemo(() => orderedInputs(inputsOf), [inputsOf]);
  const results = useResults(id);
  const waiting = useWaiting(id);
  const [dock, setDock] = useState<string | null>(null);
  const running = session?.state === "active";
  const wide = useWide();
  const detailsOpen = useSummaryOpen();
  const [sheet, setSheet] = useState(false);
  useMarkSeen(id, !!session?.unread && loaded);
  // the tab it opens on is chosen once its snapshot is in, and kept while the owner reads
  const first = useRef<{ id: string; tab: Tab } | null>(null);
  if (loaded && first.current?.id !== id) first.current = { id, tab: turn !== undefined ? "chat" : defaultTab(session, results.length) };
  const tab: Tab = asked ?? (first.current?.id === id ? first.current.tab : "chat");

  useEffect(() => {
    setError(null);
    setDock(null);
    live.watch(id).catch(() => setError("The session did not load."));
  }, [id]);

  // Stay at the bottom while new text comes, unless the owner scrolled up to read.
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  // A link to a turn opens there once its summary is in: the bottom no longer pulls.
  const shown = useRef<string | null>(null);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && turn !== undefined && tab === "chat" && shown.current !== `${id}/${turn}`) {
      const note = el.querySelector(`[data-turn="${turn}"]`);
      if (note) {
        shown.current = `${id}/${turn}`;
        stick.current = false;
        note.scrollIntoView({ block: "start" });
        return;
      }
    }
    if (el && stick.current && tab === "chat") el.scrollTop = el.scrollHeight;
  });

  const up = session ? upOf(session) : null;
  const upTitle = up ? state.sessions[up.id]?.title || "the session it came from" : "";
  const status = session ? (session.queued ? QUEUED : sessionStateWord(session.state)) : "";
  return (
    <section className="flex h-full min-h-0 min-w-0 flex-col" aria-label={session?.title ?? id}>
      <header className="flex min-h-12 shrink-0 flex-wrap items-center gap-x-1 border-b px-2 pt-[env(safe-area-inset-top)] md:flex-nowrap md:gap-x-2 md:px-3">
        {back && (
          <Link
            to="/sessions"
            aria-label="Back"
            // back where the owner came from (Today, a Space, the list); opened by a link, to the list
            onClick={(e) => {
              if (router.history.canGoBack()) {
                e.preventDefault();
                router.history.back();
              }
            }}
            className="inline-flex size-11 items-center justify-center rounded-md hover:bg-accent"
          >
            <ArrowLeft className="size-5" />
          </Link>
        )}
        <div className="flex min-h-12 min-w-0 flex-1 flex-col justify-center px-1 md:flex-initial md:shrink">
          <div className="flex min-w-0 items-center gap-2">
            {session && (
              <span
                role="img"
                aria-label={status}
                title={status}
                className={cn(
                  "size-1.5 shrink-0 rounded-full",
                  session.state === "waiting" ? "bg-warning" : session.state === "active" ? "animate-pulse bg-primary" : "bg-muted-foreground/40",
                )}
              />
            )}
            <h1 className="truncate text-[15px] font-medium md:text-sm">{session?.title ?? "Session"}</h1>
          </div>
          {up && (
            <Link
              to="/s/$id"
              params={{ id: up.id }}
              title={`${RELATION_WORDS[up.how]} «${upTitle}»`}
              className="flex w-fit max-w-full min-w-0 items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            >
              <RelationIcon how={up.how} className="size-3 shrink-0" />
              <span className="sr-only">{RELATION_WORDS[up.how]}: </span>
              <span className="truncate">{upTitle}</span>
            </Link>
          )}
        </div>
        {results.length > 0 && <Tabs tab={tab} onTab={onTab} results={results.length} />}
        <span className="hidden min-w-0 flex-1 truncate text-right text-xs text-muted-foreground md:block" aria-hidden="true">
          {status}
        </span>
        {session && <SideQuestion session={id} />}
        {session && <SessionMenu s={session} />}
        {extra}
        {session && (
          <Button
            variant="ghost"
            size="icon"
            aria-label="Details"
            title="Details: folder, model, context, connectors"
            aria-pressed={panel && wide ? detailsOpen : sheet}
            onClick={() => (panel && wide ? setSummaryOpen(!detailsOpen) : setSheet(true))}
          >
            <Info />
          </Button>
        )}
        {onClose && (
          <Button variant="ghost" size="icon" aria-label="Close panel" onClick={onClose}>
            <Close />
          </Button>
        )}
      </header>
      <div
        ref={scroller}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 md:px-6"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
      >
        {error ? (
          <p className="text-sm text-destructive">{error}</p>
        ) : !loaded ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : tab === "changes" && session?.kind === "code" ? (
          <div className="mx-auto flex w-full max-w-[46rem] flex-col gap-3">
            <button type="button" onClick={() => onTab("results")} className="flex w-fit items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground pointer-coarse:min-h-11">
              <ChevronRight className="size-3.5 rotate-180" />
              Results
            </button>
            <Suspense fallback={<p className="text-sm text-muted-foreground">Loading the changes…</p>}>
              <Changes session={id} />
            </Suspense>
          </div>
        ) : tab === "results" ? (
          <div className="mx-auto w-full max-w-[46rem]">
            <Results session={id} results={results} onChanges={() => onTab("changes")} />
          </div>
        ) : (
          <div className="mx-auto w-full max-w-[46rem]">
            <Transcript
              items={items}
              approvals={approvals}
              delta={delta}
              running={running}
              summaries={summaries}
              inputs={inputs}
              proposals={proposals}
              focus={turn}
              onWaiting={setDock}
            />
          </div>
        )}
      </div>
      {tab === "chat" && (
        // the home indicator's inset only while the keyboard is down: iOS keeps reporting it above the keyboard
        <div className={cn("shrink-0 bg-background px-3 pt-1 md:px-6", keyboard ? "pb-2" : "pb-[max(0.5rem,env(safe-area-inset-bottom))]")}>
          <WaitingDock waiting={waiting} open={dock} onOpen={setDock} />
          <PinnedPlan items={items} />
          <div className="mx-auto w-full max-w-[46rem]">
            <Composer session={id} running={running} />
          </div>
        </div>
      )}
      <Dialog open={sheet} onClose={() => setSheet(false)} title="Details" sheet>
        <SessionSummary id={id} onLeave={() => setSheet(false)} />
      </Dialog>
    </section>
  );
}
