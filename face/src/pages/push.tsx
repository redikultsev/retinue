// `/p/<card id>`: one «Запушить» card on its own — what a Telegram copy's Mini App button opens. The card comes from
// `GET /api/push/{id}`; its session's snapshot is then watched, so the stream keeps it current. Once the card is
// decided (pushed, cancelled), the Mini App closes itself a second later, the result shown till then;
// opened anywhere else, the page stays with a link to the session.
import { Link } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { PushCardView } from "@/components/push-card";
import { ArrowLeft } from "@/components/icons";
import { ApiError, api } from "@/lib/api";
import { live, useLiveState } from "@/lib/live";
import { closeMiniApp, insideTelegram } from "@/lib/telegram";

export const CLOSE_AFTER_MS = 1000;
const DECIDED = ["pushed", "cancelled"];

interface Props {
  id: string;
  load?: typeof api.pushCard;
  telegram?: () => boolean;
  close?: () => boolean;
}

export function PushPage({ id, load = api.pushCard, telegram = insideTelegram, close = closeMiniApp }: Props) {
  const { pushes, sessions } = useLiveState();
  const card = pushes[id];
  const [missing, setMissing] = useState<string | null>(null);
  const [closing, setClosing] = useState(false);
  const was = useRef<string | undefined>(undefined);

  useEffect(() => {
    let gone = false;
    setMissing(null);
    load(id)
      .then(({ card: found }) => {
        if (gone) return;
        live.dispatch({ type: "event", id: null, event: { kind: "push", data: found } });
        live.watch(found.session).catch(() => undefined);
      })
      .catch((e) => {
        if (!gone) setMissing(e instanceof ApiError && e.status === 404 ? "Такой карточки нет." : "Карточка не загрузилась.");
      });
    return () => {
      gone = true;
    };
  }, [id, load]);

  // Decided while open here: the Mini App goes back to the chat, where the Telegram copy says the same.
  useEffect(() => {
    const before = was.current;
    was.current = card?.state;
    if (!card || !before || before === card.state || !DECIDED.includes(card.state) || !telegram()) return;
    setClosing(true);
    const timer = setTimeout(() => {
      if (!close()) setClosing(false);
    }, CLOSE_AFTER_MS);
    return () => clearTimeout(timer);
  }, [card, telegram, close]);

  const owner = card ? sessions[card.session] : undefined;
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="flex min-h-14 items-center gap-2 border-b px-2 pt-[env(safe-area-inset-top)]">
        <Link to="/" aria-label="К списку" className="inline-flex size-11 items-center justify-center rounded-md hover:bg-accent">
          <ArrowLeft className="size-5" />
        </Link>
        <span className="font-semibold">Запушить</span>
      </header>
      <main className="mx-auto flex w-full max-w-2xl flex-col gap-3 p-3 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {card ? (
          <>
            <PushCardView card={card} />
            {closing && (
              <p role="status" className="text-center text-sm text-muted-foreground">
                Закрываю…
              </p>
            )}
            <Link to="/s/$id" params={{ id: card.session }} className="text-sm text-primary underline">
              Открыть сессию{owner ? ` «${owner.title}»` : ""}
            </Link>
          </>
        ) : (
          <p className="py-8 text-center text-sm text-muted-foreground">{missing ?? "Загружаю карточку…"}</p>
        )}
      </main>
    </div>
  );
}
