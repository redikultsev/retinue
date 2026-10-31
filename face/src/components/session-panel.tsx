// One session on screen: its header, «Chat» (the transcript and the input box), for a code session
// «Changes», and «Files» once it has handed any over. Two of these stand side by side on a wide screen, both fed by
// the app's one stream. The transcript is a reading column (~720 px), the plan Claude Code pins under its last message
// sits above the input box, and «Summary» (components/session-summary.tsx) opens from the header: the right panel on a
// wide screen, a sheet on a phone.
import { Link, useNavigate, useRouter } from "@tanstack/react-router";
import { lazy, type ReactNode, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Composer } from "@/components/composer";
import { Files } from "@/components/files";
import { SessionMenu } from "@/components/session-menu";
import { SideQuestion } from "@/components/side-question";
import { ArrowLeft, ChevronRight, Close, ListTodo, Panel } from "@/components/icons";
import { SessionSummary } from "@/components/session-summary";
import { TodoList } from "@/components/transcript/item";
import { Transcript } from "@/components/transcript/transcript";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { TabsList } from "@/components/ui/tabs";
import { sessionStateWord } from "@/lib/format";
import { useKeyboardUp } from "@/lib/keyboard";
import { live, useLiveState } from "@/lib/live";
import { useWide } from "@/lib/media";
import { usePane } from "@/lib/pane";
import { latestPlan, planLine, setSummaryOpen, useSummaryOpen } from "@/lib/summary";
import { useMarkSeen } from "@/lib/seen";
import { cn } from "@/lib/utils";
import { approvalsOf, orderedFiles, orderedInputs, orderedItems, orderedSummaries, proposalsOf } from "@/lib/store";
import { QUEUED } from "@/lib/types";

// The diff renderer (@pierre/diffs and Shiki) is loaded only when «Changes» is opened.
const Changes = lazy(() => import("@/components/changes").then((m) => ({ default: m.Changes })));

export type Tab = "chat" | "changes" | "files";

interface Props {
  id: string;
  tab: Tab;
  onTab: (tab: Tab) => void;
  /** Mobile: a back arrow to the list. */
  back?: boolean;
  /** Side by side: closes this panel. */
  onClose?: () => void;
  extra?: ReactNode;
  /** The right panel can show this session's «Summary» (a wide screen, one session in the middle). */
  panel?: boolean;
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

export function SessionPanel({ id, tab, onTab, back, onClose, extra, panel = false }: Props) {
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
  const filesOf = state.files[id];
  const files = useMemo(() => orderedFiles(filesOf), [filesOf]);
  const running = session?.state === "active";
  const wide = useWide();
  const summaryOpen = useSummaryOpen();
  const [sheet, setSheet] = useState(false);
  const { open: openPane } = usePane();
  const navigate = useNavigate();
  useMarkSeen(id, !!session?.unread && loaded);

  useEffect(() => {
    setError(null);
    live.watch(id).catch(() => setError("The session did not load."));
  }, [id]);

  // Stay at the bottom while new text comes, unless the owner scrolled up to read.
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current && tab === "chat") el.scrollTop = el.scrollHeight;
  });

  const code = session?.kind === "code";
  const tabs: { value: Tab; label: string }[] = [
    { value: "chat", label: "Chat" },
    ...(code ? [{ value: "changes" as const, label: "Changes" }] : []),
    ...(files.length || tab === "files" ? [{ value: "files" as const, label: `Files · ${files.length}` }] : []),
  ];
  return (
    <section className="flex h-full min-h-0 min-w-0 flex-col" aria-label={session?.title ?? id}>
      <header className="flex min-h-12 shrink-0 items-center gap-1 border-b px-2 pt-[env(safe-area-inset-top)] md:px-3">
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
        <div className="min-w-0 flex-1 px-1">
          <div className="flex min-w-0 items-center gap-2">
            {session && (
              <span
                role="img"
                aria-label={sessionStateWord(session.state)}
                title={sessionStateWord(session.state)}
                className={cn(
                  "size-1.5 shrink-0 rounded-full",
                  session.state === "waiting" ? "bg-warning" : session.state === "active" ? "animate-pulse bg-primary" : "bg-muted-foreground/40",
                )}
              />
            )}
            <span className="truncate text-[15px] font-medium md:text-sm">{session?.title ?? "Session"}</span>
          </div>
          <div className="flex min-w-0 items-center gap-1.5 truncate text-[11px] text-muted-foreground">
            <span>{session?.track}</span>
            <span>·</span>
            <span className="font-mono">{id}</span>
            {session && (
              <>
                <span>·</span>
                <span>{sessionStateWord(session.state)}</span>
              </>
            )}
            {session?.queued && (
              <>
                <span>·</span>
                <span>{QUEUED}</span>
              </>
            )}
            {session?.parent && (
              <>
                <span>·</span>
                <Link to="/s/$id" params={{ id: session.parent }} className="underline">
                  side question on {session.parent}
                </Link>
              </>
            )}
            {session?.fork_of && (
              <>
                <span>·</span>
                <Link to="/s/$id" params={{ id: session.fork_of }} className="underline">
                  fork of {session.fork_of}
                </Link>
              </>
            )}
          </div>
        </div>
        {session && <SideQuestion session={id} />}
        {session && <SessionMenu s={session} />}
        {extra}
        {session && (
          <Button
            variant="ghost"
            size="icon"
            aria-label="Summary"
            title="Summary"
            aria-pressed={panel && wide ? summaryOpen : sheet}
            onClick={() => (panel && wide ? setSummaryOpen(!summaryOpen) : setSheet(true))}
          >
            <Panel />
          </Button>
        )}
        {onClose && (
          <Button variant="ghost" size="icon" aria-label="Close panel" onClick={onClose}>
            <Close />
          </Button>
        )}
      </header>
      {tabs.length > 1 && (
        <div className="shrink-0 border-b px-3 py-1.5">
          <TabsList className="w-full md:w-auto" value={tab} onChange={onTab} tabs={tabs} />
        </div>
      )}
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
        ) : tab === "changes" && code ? (
          <Suspense fallback={<p className="text-sm text-muted-foreground">Loading the changes…</p>}>
            <Changes session={id} />
          </Suspense>
        ) : tab === "files" ? (
          <Files files={files} />
        ) : (
          <div className="mx-auto w-full max-w-[46rem]">
          <Transcript
            items={items}
            approvals={approvals}
            delta={delta}
            running={running}
            summaries={summaries}
            inputs={inputs}
            base={session?.base ? { session: id, count: session.base } : undefined}
            push={session?.push || session?.push_why ? { session: id, count: session.push ?? 0, why: session.push_why } : undefined}
            proposals={proposals}
          />
          </div>
        )}
      </div>
      {tab === "chat" && (
        // the home indicator's inset only while the keyboard is down: iOS keeps reporting it above the keyboard
        <div className={cn("shrink-0 bg-background px-3 pt-1 md:px-6", keyboard ? "pb-2" : "pb-[max(0.5rem,env(safe-area-inset-bottom))]")}>
          <PinnedPlan items={items} />
          <div className="mx-auto w-full max-w-[46rem]">
            <Composer session={id} running={running} />
          </div>
        </div>
      )}
      <Dialog open={sheet} onClose={() => setSheet(false)} title="Summary" sheet>
        <SessionSummary
          id={id}
          onLeave={() => setSheet(false)}
          onDiff={() => {
            setSheet(false);
            if (wide) openPane({ kind: "diff", session: id });
            else onTab("changes");
          }}
          onFile={(file) => {
            setSheet(false);
            void navigate({ to: "/f/$id", params: { id: file } });
          }}
        />
      </Dialog>
    </section>
  );
}
