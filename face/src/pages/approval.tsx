// `/a/<approval id>`: one card on its own — what the Telegram Mini App button opens. The card comes from
// `GET /api/approvals/{id}`; its session's snapshot is then watched, so the stream keeps the card current. Once a
// press decides it, the Mini App closes itself a second later, the result shown till then; opened anywhere else, the
// page goes on to the card's session.
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { ApprovalCard } from "@/components/approval-card";
import { ArrowLeft } from "@/components/icons";
import { ApiError, api } from "@/lib/api";
import { live, useLiveState } from "@/lib/live";
import { closeMiniApp, insideTelegram } from "@/lib/telegram";
import type { Approval } from "@/lib/types";

export const CLOSE_AFTER_MS = 1000;

interface Props {
  id: string;
  load?: typeof api.approval;
  decide?: typeof api.decide;
  /** Where the page runs: the Telegram web view or not. */
  telegram?: () => boolean;
  close?: () => boolean;
}

export function ApprovalPage({ id, load = api.approval, decide, telegram = insideTelegram, close = closeMiniApp }: Props) {
  const { approvals, sessions } = useLiveState();
  const approval = approvals[id];
  const [missing, setMissing] = useState<string | null>(null);
  const [closing, setClosing] = useState(false);
  const navigate = useNavigate();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  useEffect(() => {
    let gone = false;
    setMissing(null);
    load(id)
      .then(({ approval: found }) => {
        if (gone) return;
        live.dispatch({ type: "event", id: null, event: { kind: "approval", data: found } });
        live.watch(found.session).catch(() => undefined);
      })
      .catch((e) => {
        if (!gone) setMissing(e instanceof ApiError && e.status === 404 ? "Такой карточки нет." : "Карточка не загрузилась.");
      });
    return () => {
      gone = true;
    };
  }, [id, load]);

  function decided(card: Approval) {
    if (telegram()) {
      setClosing(true);
      timer.current = setTimeout(() => {
        if (!close()) setClosing(false);
      }, CLOSE_AFTER_MS);
      return;
    }
    void navigate({ to: "/s/$id", params: { id: card.session } });
  }

  const owner = approval ? sessions[approval.session] : undefined;
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="flex min-h-14 items-center gap-2 border-b px-2 pt-[env(safe-area-inset-top)]">
        <Link to="/" aria-label="К списку" className="inline-flex size-11 items-center justify-center rounded-md hover:bg-accent">
          <ArrowLeft className="size-5" />
        </Link>
        <span className="font-semibold">Карточка {id}</span>
      </header>
      <main className="mx-auto flex w-full max-w-2xl flex-col gap-3 p-3 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {approval ? (
          <>
            <ApprovalCard approval={approval} onDecided={decided} {...(decide ? { decide } : {})} />
            {closing && (
              <p role="status" className="text-center text-sm text-muted-foreground">
                {approval.view.status || "Готово."} Закрываю…
              </p>
            )}
            <Link to="/s/$id" params={{ id: approval.session }} className="text-sm text-primary underline">
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
