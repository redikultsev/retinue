// `/new`: a new task, as a new chat is in Claude's apps. On a wide screen the greeting, how things stand («N waiting ·
// N working», a tap opens Today), the nearest deadline and the composer stand together in the middle of the page; on a
// phone the composer stays at the bottom, under the thumb and above the keyboard. Under the composer, the sphere
// picker: «Auto» — a general session in the root of the Base (`meta.general`), which finds its context and its sphere
// itself — or one sphere, whose track and sphere the session starts with. Sent, the session opens. What was typed and
// not sent waits here for the next visit (this tab's sessionStorage).
import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { AppHeader } from "@/components/app-header";
import { ComposerBox } from "@/components/composer";
import { ChevronRight, Flag } from "@/components/icons";
import { api } from "@/lib/api";
import { type Deadline, useFeed } from "@/lib/feed";
import { live, useLiveState } from "@/lib/live";
import { useCoarsePointer, useWide } from "@/lib/media";
import { waitingCount } from "@/lib/store";
import { sphereLabel } from "@/lib/spheres";
import { running } from "@/lib/today";
import type { Meta, Sphere } from "@/lib/types";
import { cn } from "@/lib/utils";

const DRAFT_KEY = "retinue-new-draft";

function readDraft(): string {
  try {
    return sessionStorage.getItem(DRAFT_KEY) ?? "";
  } catch {
    return "";
  }
}

function writeDraft(text: string) {
  try {
    if (text) sessionStorage.setItem(DRAFT_KEY, text);
    else sessionStorage.removeItem(DRAFT_KEY);
  } catch {
    // storage unavailable: the draft lasts while the screen is open
  }
}

/** «Good morning» from 5, «Good afternoon» from 12, «Good evening» from 18, «Good night» from 23 — the device's clock. */
export function greeting(hour: number): string {
  if (hour >= 5 && hour < 12) return "Good morning";
  if (hour >= 12 && hour < 18) return "Good afternoon";
  if (hour >= 18 && hour < 23) return "Good evening";
  return "Good night";
}

/** «2 waiting · 1 working»; nothing at all: «Quiet: nothing waiting». */
export function statusLine(waiting: number, working: number): string {
  if (!waiting && !working) return "Quiet: nothing waiting";
  return [waiting ? `${waiting} waiting` : "nothing waiting", working ? `${working} working` : null].filter(Boolean).join(" · ");
}

/** The track of a new general session: the platform names it (`general`, the root of the Base — null: it has none); an
 * older platform — its first track that is not a repository's. Null too before the platform has answered: nothing is
 * sent to no track. */
export function generalTrack(meta: Meta | null): string | null {
  if (!meta) return null;
  if ("general" in meta) return meta.general ?? null;
  return meta.tracks.find((t) => t.kind !== "code")?.name ?? null;
}

export const NO_TRACK = "There is nowhere to start a new session: platform.yaml has no track at the root of the Base.";

/** «Auto» and the spheres, one of them chosen: where the new session starts. */
export function SpherePicker({
  spheres,
  value,
  onChange,
  wrap,
}: {
  spheres: Sphere[];
  value: string | null;
  onChange: (name: string | null) => void;
  /** Wrap the chips into rows (a wide screen) instead of one row that scrolls sideways (a phone). */
  wrap?: boolean;
}) {
  const chip = (on: boolean) =>
    cn(
      "inline-flex h-8 shrink-0 items-center gap-1 rounded-full border px-3 text-[13px] whitespace-nowrap pointer-coarse:h-9",
      on ? "border-primary bg-primary/10 font-medium text-foreground" : "text-muted-foreground hover:bg-accent hover:text-foreground",
    );
  return (
    <fieldset
      aria-label="Sphere"
      // a fieldset is as wide as its content unless told otherwise: `min-w-0` keeps the chips inside the composer's width
      className={cn(
        "flex min-w-0 gap-1.5 px-1 pt-2 pb-1",
        wrap ? "flex-wrap justify-center" : "overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
      )}
    >
      <button type="button" aria-pressed={value === null} onClick={() => onChange(null)} className={chip(value === null)}>
        Auto
      </button>
      {spheres.map((s) => (
        <button key={s.name} type="button" aria-pressed={value === s.name} onClick={() => onChange(s.name)} className={chip(value === s.name)}>
          {sphereLabel(s)}
        </button>
      ))}
    </fieldset>
  );
}

/** The deadline nearest in time, the overdue first. */
function nearest(deadlines: Deadline[] | undefined): Deadline | null {
  return [...(deadlines ?? [])].sort((a, b) => a.when.localeCompare(b.when))[0] ?? null;
}

interface Props {
  newSession?: typeof api.newSession;
  transcribe?: typeof api.speech;
  /** The clock, for the greeting (a test's). */
  now?: Date;
}

export function NewPage({ newSession = api.newSession, transcribe = api.speech, now = new Date() }: Props) {
  const wide = useWide();
  const coarse = useCoarsePointer();
  const navigate = useNavigate();
  const feed = useFeed();
  const { sessions } = useLiveState();
  const list = useMemo(() => Object.values(sessions), [sessions]);
  const waiting = feed ? feed.count : waitingCount(list);
  const working = useMemo(() => running(list).length, [list]);
  const deadline = nearest(feed?.deadlines);
  const meta = live.meta; // read again on every change of the live state, the first snapshot's included
  const spheres = meta?.spheres ?? [];
  const [sphereName, setSphere] = useState<string | null>(null);
  const sphere = spheres.find((s) => s.name === sphereName) ?? null;
  const track = sphere ? sphere.track : generalTrack(meta);
  const blocked = track ? null : meta ? NO_TRACK : "Connecting…";

  async function send(text: string, id: string, files: Parameters<typeof api.newSession>[4]) {
    if (!track) throw new Error(NO_TRACK);
    const { session } = await newSession(track, text, id, sphere?.name ?? null, files);
    writeDraft("");
    await navigate({ to: "/s/$id", params: { id: session } });
    return undefined;
  }

  const composer = (
    <div className="w-full">
      {meta && !track && (
        <p role="alert" className="px-2 pb-2 text-sm text-destructive">
          {NO_TRACK}
        </p>
      )}
      <ComposerBox
        owner="new"
        label="What should I do?"
        placeholder="What should I do?"
        send={send}
        transcribe={transcribe}
        autoFocus={!coarse}
        initial={readDraft()}
        onDraft={writeDraft}
        blocked={blocked}
      />
      {spheres.length > 0 && <SpherePicker spheres={spheres} value={sphere?.name ?? null} onChange={setSphere} wrap={wide} />}
    </div>
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      {!wide && <AppHeader />}
      <main className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        <div className={cn("m-auto flex w-full flex-col items-center gap-4 px-6 py-10 text-center", wide ? "max-w-2xl -translate-y-[6vh]" : "max-w-xl")}>
          <img src="/icons/icon.svg" alt="" className="size-12 rounded-2xl shadow-sm" />
          <h1 className="text-[1.75rem] leading-tight font-semibold tracking-tight md:text-3xl">{greeting(now.getHours())}</h1>
          <Link
            to="/today"
            aria-label={`Today: ${statusLine(waiting, working)}`}
            className="inline-flex min-h-10 items-center gap-2 rounded-full border bg-muted/40 px-4 text-sm text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            <span
              aria-hidden="true"
              className={cn(
                "size-2 shrink-0 rounded-full",
                waiting ? "bg-warning" : working ? "animate-pulse bg-primary" : "bg-success",
              )}
            />
            <span>{statusLine(waiting, working)}</span>
            <ChevronRight className="size-4 shrink-0" />
          </Link>
          {deadline && (
            <Link
              to="/today"
              data-testid="next-deadline"
              className="flex max-w-full items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
            >
              <Flag className="size-4 shrink-0" />
              <span className="truncate">
                {deadline.sphere_icon ? `${deadline.sphere_icon} ` : ""}
                {deadline.title}
              </span>
            </Link>
          )}
          {wide && <div className="w-full pt-4 text-left">{composer}</div>}
        </div>
      </main>
      {!wide && <div className="mx-auto w-full max-w-3xl shrink-0 px-3 pt-2 pb-3">{composer}</div>}
    </div>
  );
}
