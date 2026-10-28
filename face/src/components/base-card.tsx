// «В Базу» (retinue/tobase.py): a session's commit card — the Base files its runs changed, each a toggle, the commit's
// subject, «Закоммитить», the diff, «Отмена». It is the same card as its Telegram copies: a change here rewrites them,
// and one there comes here on the stream. Nothing commits without a press; a press is taken once in either place.
import { lazy, Suspense, useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ApiError, api } from "@/lib/api";
import { type Live, live as defaultLive, useLiveState } from "@/lib/live";
import { toast } from "@/lib/toast";
import type { BaseCard as Card, BaseResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

// The diff renderer (@pierre/diffs and Shiki) is loaded only when the diff is opened.
const BaseDiff = lazy(() => import("@/components/base-diff").then((m) => ({ default: m.BaseDiff })));

type Calls = Pick<typeof api, "baseToggle" | "baseSubject" | "baseCommit" | "baseCancel" | "baseOpen">;

const STATE_WORDS: Record<string, string> = {
  committing: "Коммичу…",
  committed: "Закоммичено",
  failed: "Не закоммичено",
  cancelled: "Отменено",
  stale: "Устарела",
  expired: "Истекла",
};

/** «3 файла» */
export function filesWord(n: number): string {
  const tail = n % 100;
  const word =
    tail % 10 === 1 && tail !== 11 ? "файл" : tail % 10 >= 2 && tail % 10 <= 4 && !(tail >= 12 && tail <= 14) ? "файла" : "файлов";
  return `${n} ${word}`;
}

function put(live: Live, card: Card | null) {
  if (card) live.dispatch({ type: "event", id: null, event: { kind: "base", data: card } });
}

interface Props {
  card: Card;
  calls?: Calls;
  live?: Live;
  className?: string;
}

export function BaseCardView({ card, calls = api, live = defaultLive, className }: Props) {
  const [busy, setBusy] = useState(false);
  const [subject, setSubject] = useState(card.subject);
  const [diff, setDiff] = useState(false);
  useEffect(() => setSubject(card.subject), [card.subject]);
  const shown = card.state === "shown";
  const chosen = card.files.filter((f) => f.on).length;

  async function act(call: () => Promise<BaseResponse>, fallback: string): Promise<boolean> {
    setBusy(true);
    try {
      const res = await call();
      put(live, res.card);
      if (res.toast) toast(res.toast);
      return true;
    } catch (e) {
      toast(e instanceof ApiError ? e.message : fallback, "error");
      return false;
    } finally {
      setBusy(false);
    }
  }

  /** «Закоммитить» with the subject as the field shows it: an edit not saved yet is saved first. */
  async function commit() {
    const edited = subject.trim() && subject !== card.subject;
    if (edited && !(await act(() => calls.baseSubject(card.id, subject), "Тема не сохранилась."))) return;
    await act(() => calls.baseCommit(card.id), "Не закоммитилось.");
  }

  return (
    <section data-testid="base-card" aria-label={card.view.head[0] ?? "В Базу"} className={cn("flex flex-col gap-3", className)}>
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">{card.view.head[0]}</span>
        {!shown && <Badge variant={card.state === "committing" ? "warning" : "secondary"}>{STATE_WORDS[card.state] ?? card.state}</Badge>}
      </div>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (subject.trim() && subject !== card.subject) void act(() => calls.baseSubject(card.id, subject), "Тема не сохранилась.");
        }}
      >
        <Input aria-label="Тема коммита" value={subject} disabled={!shown || busy} onChange={(e) => setSubject(e.target.value)} />
        {shown && subject !== card.subject && subject.trim() && (
          <Button type="submit" variant="outline" disabled={busy}>
            Сохранить
          </Button>
        )}
      </form>
      <ul className="flex flex-col gap-1" aria-label="Файлы">
        {card.files.map((f) => (
          <li key={f.path}>
            <label className={cn("flex min-h-9 items-center gap-2 rounded-md px-1 text-sm", shown && "hover:bg-accent")}>
              <input
                type="checkbox"
                className="size-4 shrink-0"
                checked={f.on}
                disabled={!shown || busy}
                aria-label={f.path}
                onChange={(e) => void act(() => calls.baseToggle(card.id, f.path, e.target.checked), "Не переключилось.")}
              />
              <span className="w-4 shrink-0 font-mono text-xs text-muted-foreground">{f.status}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-[13px]" title={f.path}>
                {f.path}
              </span>
              {f.nobody && (
                <span className="shrink-0 text-xs text-muted-foreground" title="Ни одна сессия его не меняла">
                  ничей
                </span>
              )}
              {f.shared && (
                <span className="shrink-0 text-xs text-warning-foreground" title="Файл менялся и в другой сессии">
                  ⇄
                </span>
              )}
              {f.add !== null && (
                <span className="shrink-0 font-mono text-xs tabular-nums">
                  <span className="text-green-600 dark:text-green-400">+{f.add}</span>{" "}
                  <span className="text-red-600 dark:text-red-400">−{f.del}</span>
                </span>
              )}
            </label>
          </li>
        ))}
      </ul>
      {card.files.some((f) => f.shared) && (
        <p className="text-xs text-muted-foreground">⇄ — файл менялся и в другой сессии: реши, чей он.</p>
      )}
      {card.files.some((f) => f.nobody) && (
        <p className="text-xs text-muted-foreground">
          «ничей» — изменён, но ни одной сессией. Выключен: включи, чтобы закоммитить.
        </p>
      )}
      {card.others.files > 0 && (
        <p className="text-xs text-muted-foreground">
          Ещё {filesWord(card.others.files)} не закоммичено в других сессиях ({card.others.sessions}).
        </p>
      )}
      {card.said && <p role="status" className="text-sm whitespace-pre-line">{card.said}</p>}
      <div className="flex flex-wrap gap-2">
        {shown && (
          <Button disabled={busy || chosen === 0} onClick={() => void commit()}>
            Закоммитить · {chosen}
          </Button>
        )}
        <Button variant="outline" aria-expanded={diff} onClick={() => setDiff(!diff)}>
          {diff ? "Скрыть дифф" : "Открыть дифф"}
        </Button>
        {shown && (
          <Button variant="ghost" disabled={busy} onClick={() => void act(() => calls.baseCancel(card.id), "Не отменилось.")}>
            Отмена
          </Button>
        )}
      </div>
      {diff && (
        <Suspense fallback={<p className="text-sm text-muted-foreground">Загружаю дифф…</p>}>
          <BaseDiff id={card.id} on={card.files.filter((f) => f.on).map((f) => f.path).join("\n")} />
        </Suspense>
      )}
    </section>
  );
}

/** «В Базу»: the session's card, opened or brought up to date by the platform. Its id, or null when there is
 * nothing to commit (said as a toast). */
export async function openBase(session: string, calls: Pick<Calls, "baseOpen"> = api, live: Live = defaultLive) {
  try {
    const res = await calls.baseOpen(session);
    put(live, res.card);
    if (!res.card) toast(res.toast || "Нечего коммитить.");
    return res.card?.id ?? null;
  } catch (e) {
    toast(e instanceof ApiError ? e.message : "Карточка не открылась.", "error");
    return null;
  }
}

/** The card in a dialog, as the stream keeps it. */
export function BaseDialog({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { bases } = useLiveState();
  const card = id ? bases[id] : undefined;
  return (
    <Dialog open={id !== null} onClose={onClose} title="В Базу" className="max-w-2xl">
      {card && <BaseCardView card={card} />}
      <div className="flex justify-end">
        <Button variant="ghost" onClick={onClose}>
          Закрыть
        </Button>
      </div>
    </Dialog>
  );
}

/** «В Базу · N»: under a session's last summary, in its feed row. */
export function BaseButton({ session, count, calls = api, className }: { session: string; count: number; calls?: Pick<Calls, "baseOpen">; className?: string }) {
  const [id, setId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <>
      <Button
        variant="outline"
        size="sm"
        className={cn("w-fit", className)}
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setId(await openBase(session, calls));
          setBusy(false);
        }}
      >
        В Базу · {count}
      </Button>
      <BaseDialog id={id} onClose={() => setId(null)} />
    </>
  );
}
