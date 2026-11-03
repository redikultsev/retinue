// «Results»: everything a session made (lib/results.ts), one list newest first — a row a thing, its type's icon first,
// what it is under its name, its own button at the right («Undo», «Push»), its time. A row opens its thing: a file or
// a page in the right pane (its own page on a phone), a commit's card, a report under its row, the thing a turn ending
// asked to look at, the diff beside the conversation (the «Changes» view on a phone), a PR on its host.
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import type * as React from "react";
import { BaseDialog, fetchBase, useUndo, wroteParts } from "@/components/base-card";
import { ThingView } from "@/components/ending-card";
import { size } from "@/components/files";
import { AppWindow, ArrowDownLeft, ArrowUp, ArrowUpRight, ChevronRight, Database, Eye, FileDiff, FileIcon, GitPullRequest } from "@/components/icons";
import { PushButton, prLink } from "@/components/push-card";
import { RelationIcon } from "@/components/relation";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { useFeed } from "@/lib/feed";
import { when } from "@/lib/format";
import { useLiveState } from "@/lib/live";
import { useWide } from "@/lib/media";
import { endingOf, thingName } from "@/lib/operator";
import { usePane } from "@/lib/pane";
import { type DiffStat, parseStat, type Result, resultsOf } from "@/lib/results";
import { orderedFiles, orderedInputs, orderedSummaries } from "@/lib/store";
import { cn, LIST } from "@/lib/utils";

const stats = new Map<string, DiffStat | null>();

/** A code session's diff in numbers, read again when its turn ends; kept per session for the visit. */
export function useDiffStat(id: string, code: boolean, state?: string): DiffStat | null {
  const [stat, setStat] = useState<DiffStat | null>(() => stats.get(id) ?? null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: a turn that ended may have changed the work tree
  useEffect(() => {
    if (!code) return;
    let gone = false;
    setStat(stats.get(id) ?? null);
    api
      .diff(id)
      .then((d) => {
        const s = parseStat(d.stat);
        stats.set(id, s);
        if (!gone) setStat(s);
      })
      .catch(() => {
        // the diff does not load now: the row waits for the next read
      });
    return () => {
      gone = true;
    };
  }, [id, code, state]);
  return code ? stat : null;
}

/** What the session made, as the list shows it. */
export function useResults(id: string): Result[] {
  const state = useLiveState();
  const feed = useFeed();
  const session = state.sessions[id];
  const stat = useDiffStat(id, session?.kind === "code", session?.state);
  const row = useMemo(() => feed?.groups.flatMap((g) => g.rows).find((r) => r.key === `ending:${id}`), [feed, id]);
  const ending = row ? endingOf(row) : null;
  const filesOf = state.files[id];
  const summariesOf = state.summaries[id];
  const inputsOf = state.inputs[id];
  return useMemo(
    () =>
      session
        ? resultsOf({
            session,
            files: orderedFiles(filesOf),
            bases: state.bases,
            summaries: orderedSummaries(summariesOf),
            inputs: orderedInputs(inputsOf),
            ending,
            endingAt: row?.since,
            pushes: state.pushes,
            diff: stat,
          })
        : [],
    [session, filesOf, state.bases, summariesOf, inputsOf, ending, row?.since, state.pushes, stat],
  );
}

const ICON = "size-4 shrink-0 text-muted-foreground";

/** One row: the icon, the name over what it is, and at the right its own button and its time. The name and the line
 * open the thing (`onOpen`), or a link does (`href`). */
function Row({
  icon,
  name,
  line,
  at,
  aside,
  onOpen,
  href,
  open,
  mono = false,
  children,
}: {
  icon: React.ReactNode;
  name: React.ReactNode;
  line?: React.ReactNode;
  at?: number;
  aside?: React.ReactNode;
  onOpen?: () => void;
  href?: string;
  /** It opens under its row: the chevron turns. */
  open?: boolean;
  mono?: boolean;
  children?: React.ReactNode;
}) {
  const body = (
    <>
      {icon}
      <span className="flex min-w-0 flex-1 flex-col">
        <span className={cn("truncate text-sm", mono && "font-mono text-[13px]")}>{name}</span>
        {line && <span className="truncate text-xs text-muted-foreground">{line}</span>}
      </span>
    </>
  );
  const main = "flex min-w-0 flex-1 items-center gap-3 self-stretch py-2 text-left outline-none";
  return (
    <li data-testid="result" className="flex flex-col">
      <div className="flex min-h-12 items-center gap-3 px-3 transition-colors hover:bg-accent/50 has-[:focus-visible]:bg-accent/50">
        {href ? (
          <a href={href} target="_blank" rel="noopener noreferrer" className={main}>
            {body}
            <ArrowUpRight className="size-3.5 shrink-0 text-muted-foreground" />
          </a>
        ) : onOpen ? (
          <button type="button" onClick={onOpen} aria-expanded={open} className={main}>
            {body}
          </button>
        ) : (
          <div className={main}>{body}</div>
        )}
        {aside}
        {at !== undefined && <span className="w-12 shrink-0 text-right font-mono text-xs text-muted-foreground tabular-nums">{when(at)}</span>}
        {onOpen && !href && (
          <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} />
        )}
      </div>
      {children}
    </li>
  );
}

/** A commit's files by its newest one: the path in mono, how many more in words. */
function BaseName({ card }: { card: NonNullable<Extract<Result, { kind: "base" }>["card"]> }) {
  const { first, more } = wroteParts(card);
  if (!first) return <>{card.said}</>;
  return (
    <>
      <span className="font-mono text-[13px]">{first}</span>
      {more > 0 && <span className="text-muted-foreground">{` and ${more} more`}</span>}
    </>
  );
}

function BaseRow({ r }: { r: Extract<Result, { kind: "base" }> }) {
  const { bases } = useLiveState();
  const card = bases[r.id] ?? r.card;
  const [open, setOpen] = useState(false);
  const { busy, press } = useUndo(card);
  const files = card?.files.length ?? 0;
  return (
    <Row
      icon={<Database className={ICON} />}
      name={card ? <BaseName card={card} /> : (r.line ?? "Written to the Base")}
      line={card ? (card.state === "undone" ? "In the Base · undone" : `In the Base · ${files} ${files === 1 ? "file" : "files"}`) : "In the Base"}
      at={r.at}
      onOpen={async () => {
        if (card || (await fetchBase(r.id))) setOpen(true);
      }}
      aside={
        card?.undo && card.state === "committed" ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void press()}>
            Undo
          </Button>
        ) : null
      }
    >
      <BaseDialog id={open ? r.id : null} onClose={() => setOpen(false)} />
    </Row>
  );
}

function ReportRow({ r }: { r: Extract<Result, { kind: "report" }> }) {
  const [open, setOpen] = useState(false);
  const first = r.text.split("\n").find((l) => l.trim()) ?? "";
  return (
    <Row
      icon={<ArrowDownLeft className={ICON} />}
      name={r.title}
      line={`Sub-session's report${r.cut ? " · its turn was cut short" : ""} · ${first}`}
      at={r.at}
      open={open}
      onOpen={() => setOpen(!open)}
    >
      {open && (
        <div data-testid="report" className="mx-3 mb-3 ml-10 flex flex-col gap-2 rounded-lg border border-dashed px-3 py-2.5">
          <Link
            to="/s/$id"
            params={{ id: r.child }}
            className="flex w-fit max-w-full items-center gap-1.5 text-[13px] font-medium text-primary underline-offset-4 hover:underline"
          >
            <RelationIcon how="sub-session" className="size-3.5 shrink-0" />
            <span className="min-w-0 truncate">Sub-session: {r.title}</span>
          </Link>
          <p className="max-h-80 overflow-auto text-[13px] break-words whitespace-pre-wrap">{r.text}</p>
        </div>
      )}
    </Row>
  );
}

function LookRow({ r }: { r: Extract<Result, { kind: "look" }> }) {
  const [open, setOpen] = useState(false);
  const name = thingName(r.thing);
  if (r.thing.kind === "link" && r.thing.src)
    return <Row icon={<Eye className={ICON} />} name={name} line="To look at · a page on the web" at={r.at} href={r.thing.src} />;
  return (
    <Row icon={<Eye className={ICON} />} name={name} line={r.thing.src ? "To look at" : `To look at · ${r.thing.why || "not served to the app"}`} at={r.at} onOpen={() => setOpen(true)}>
      <Dialog open={open} onClose={() => setOpen(false)} title={name} className="max-w-3xl">
        <div className="flex flex-col gap-3 p-4">
          <ThingView thing={r.thing} opened />
          <div className="flex justify-end">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Close
            </Button>
          </div>
        </div>
      </Dialog>
    </Row>
  );
}

function ResultRow({ r, session, onChanges }: { r: Result; session: string; onChanges: () => void }) {
  const wide = useWide();
  const { open } = usePane();
  const navigate = useNavigate();
  switch (r.kind) {
    case "file":
    case "page":
      return (
        <Row
          icon={r.kind === "page" ? <AppWindow className={ICON} /> : <FileIcon className={ICON} />}
          name={r.file.path}
          mono
          line={`${r.kind === "page" ? "Page" : "File"} · ${size(r.file.size)}`}
          at={r.at}
          onOpen={() => (wide ? open({ kind: "file", id: r.file.id }) : void navigate({ to: "/f/$id", params: { id: r.file.id } }))}
        />
      );
    case "base":
      return <BaseRow r={r} />;
    case "report":
      return <ReportRow r={r} />;
    case "look":
      return <LookRow r={r} />;
    case "diff":
      return (
        <Row
          icon={<FileDiff className={ICON} />}
          name={
            <span className="font-mono text-[13px] tabular-nums">
              <span className="text-success">+{r.stat.added}</span> <span className="text-destructive">−{r.stat.removed}</span>
            </span>
          }
          line={`Code changes · ${r.stat.files} ${r.stat.files === 1 ? "file" : "files"}`}
          onOpen={() => (wide ? open({ kind: "diff", session }) : onChanges())}
        />
      );
    case "push":
      return (
        <Row
          icon={<ArrowUp className={ICON} />}
          name={r.count > 0 ? `${r.count} ${r.count === 1 ? "commit" : "commits"} not pushed` : "Not pushed"}
          line={r.why ? `Cannot push — ${r.why}` : "Not on the remote yet"}
          aside={r.count > 0 ? <PushButton session={session} count={r.count} /> : null}
        />
      );
    case "pr":
      return (
        <Row
          icon={<GitPullRequest className={ICON} />}
          name={r.card.title || `PR · ${r.card.repo}`}
          line={`Pull request · ${r.card.repo} → ${r.card.base}`}
          at={r.at}
          href={prLink(r.card.url) ?? undefined}
        />
      );
  }
}

export function Results({ session, results, onChanges }: { session: string; results: Result[]; onChanges: () => void }) {
  if (!results.length) return <p className="py-8 text-center text-sm text-muted-foreground">Nothing made yet.</p>;
  return (
    <ul aria-label="Results" className={LIST}>
      {results.map((r) => (
        <ResultRow key={r.key} r={r} session={session} onChanges={onChanges} />
      ))}
    </ul>
  );
}
