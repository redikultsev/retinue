// `/new`: a new task, as a new chat is in Claude's apps — the place where a session was becomes an empty one, the
// composer at the bottom and something calm in the middle: a greeting for the time of day and how things stand
// («ждут тебя N · работают N», a tap opens «Сегодня»), with the nearest deadline. No «Кому»: a new task is always a new
// general session in the root of the Base (`meta.general`), which finds its context — and its sphere — itself. Sent,
// the session opens. What was typed and not sent waits here for the next visit (this tab's sessionStorage).
import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo } from "react";
import { AppHeader } from "@/components/app-header";
import { ComposerBox } from "@/components/composer";
import { ChevronRight, Flag } from "@/components/icons";
import { api } from "@/lib/api";
import { type Deadline, useFeed } from "@/lib/feed";
import { live, useLiveState } from "@/lib/live";
import { useCoarsePointer, useWide } from "@/lib/media";
import { waitingCount } from "@/lib/store";
import { running } from "@/lib/today";
import type { Meta } from "@/lib/types";
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

/** «Доброе утро» from 5, «Добрый день» from 12, «Добрый вечер» from 18, «Доброй ночи» from 23 — the device's clock. */
export function greeting(hour: number): string {
  if (hour >= 5 && hour < 12) return "Доброе утро";
  if (hour >= 12 && hour < 18) return "Добрый день";
  if (hour >= 18 && hour < 23) return "Добрый вечер";
  return "Доброй ночи";
}

/** «ждут тебя 2 · работают 1»; nothing at all: «Тихо: ничего не ждёт». */
export function statusLine(waiting: number, working: number): string {
  if (!waiting && !working) return "Тихо: ничего не ждёт";
  return [waiting ? `ждут тебя ${waiting}` : "ничего не ждёт", working ? `работают ${working}` : null].filter(Boolean).join(" · ");
}

/** The track of a new general session: the platform names it (`general`, the root of the Base — null: it has none); an
 * older platform — its first track that is not a repository's. Null too before the platform has answered: nothing is
 * sent to no track. */
export function generalTrack(meta: Meta | null): string | null {
  if (!meta) return null;
  if ("general" in meta) return meta.general ?? null;
  return meta.tracks.find((t) => t.kind !== "code")?.name ?? null;
}

export const NO_TRACK = "Новую сессию начать негде: в platform.yaml нет трека в корне Базы.";

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
  const track = generalTrack(meta);
  const blocked = track ? null : meta ? NO_TRACK : "Подключаюсь…";

  async function send(text: string, id: string, files: Parameters<typeof api.newSession>[4]) {
    if (!track) throw new Error(NO_TRACK);
    const { session } = await newSession(track, text, id, null, files);
    writeDraft("");
    await navigate({ to: "/s/$id", params: { id: session } });
    return undefined;
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {!wide && <AppHeader />}
      <main className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        <div className="m-auto flex w-full max-w-xl flex-col items-center gap-4 px-6 py-10 text-center">
          <img src="/icons/icon.svg" alt="" className="size-12 rounded-2xl shadow-sm" />
          <h1 className="text-[1.75rem] leading-tight font-semibold tracking-tight md:text-3xl">{greeting(now.getHours())}</h1>
          <Link
            to="/today"
            aria-label={`Сегодня: ${statusLine(waiting, working)}`}
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
        </div>
      </main>
      <div className="mx-auto w-full max-w-3xl shrink-0 px-3 pt-2 pb-3 md:px-6 md:pb-6">
        {meta && !track && (
          <p role="alert" className="px-2 pb-2 text-sm text-destructive">
            {NO_TRACK}
          </p>
        )}
        <ComposerBox
          owner="new"
          label="Что сделать"
          placeholder="Что сделать?"
          send={send}
          transcribe={transcribe}
          autoFocus={!coarse}
          initial={readDraft()}
          onDraft={writeDraft}
          blocked={blocked}
        />
      </div>
    </div>
  );
}
