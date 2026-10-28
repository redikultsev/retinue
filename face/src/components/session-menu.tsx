// A session's menu: its sphere, whether it is the sphere's main session, «В Базу» while its runs left Base files
// uncommitted (retinue/tobase.py), and «Ответвить» — a new session from a copy of this one's conversation, with the
// track's full rights (retinue/spheres.py); «Закрепить» too, since a phone's row shows its pin only once pinned. Each
// change is the platform's: the answer's row goes into the store as the session's event, and every device shows it.
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { BaseDialog, openBase } from "@/components/base-card";
import { PushDialog, openPush } from "@/components/push-card";
import { Database, Fork, GitBranch, More, Pin, Star } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { live } from "@/lib/live";
import { NO_SPHERE, sphereLabel } from "@/lib/spheres";
import { toast } from "@/lib/toast";
import type { Session } from "@/lib/types";
import { cn, inputId } from "@/lib/utils";

type Api = Pick<typeof api, "setSphere" | "setMain" | "fork"> & Partial<Pick<typeof api, "pin">>;

export function SessionMenu({ s, calls = api, className }: { s: Session; calls?: Api; className?: string }) {
  const pin = calls.pin ?? api.pin;
  const spheres = live.meta?.spheres ?? [];
  const [open, setOpen] = useState(false);
  const [forking, setForking] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [id, setId] = useState(inputId);
  const [base, setBase] = useState<string | null>(null);
  const [push, setPush] = useState<string | null>(null);
  const navigate = useNavigate();

  function close() {
    setOpen(false);
    setForking(false);
  }

  async function change(call: () => Promise<{ session: Session }>) {
    setBusy(true);
    try {
      const res = await call();
      live.dispatch({ type: "event", id: null, event: { kind: "session", data: res.session } });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Не получилось.", "error");
    } finally {
      setBusy(false);
    }
  }

  async function fork() {
    if (!text.trim() || busy) return;
    setBusy(true);
    try {
      const res = await calls.fork(s.id, text.trim(), id);
      setText("");
      setId(inputId());
      close();
      await navigate({ to: "/s/$id", params: { id: res.session } });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Не ответвилось.", "error");
    } finally {
      setBusy(false);
    }
  }

  const choice = (name: string | null, label: string) => (
    <button
      key={name ?? ""}
      type="button"
      aria-pressed={(s.sphere ?? null) === name}
      disabled={busy}
      onClick={() => change(() => calls.setSphere(s.id, name))}
      className={cn(
        "min-h-11 rounded-md border px-3 text-sm",
        (s.sphere ?? null) === name ? "border-primary bg-accent" : "hover:bg-accent",
      )}
    >
      {label}
    </button>
  );

  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        aria-label={`Меню: ${s.title}`}
        title="Сфера, главная, ответвить"
        className={className}
        onClick={() => setOpen(true)}
      >
        <More />
      </Button>
      <Dialog open={open} onClose={close} title={forking ? "Ответвить" : s.title || "Сессия"}>
        {forking ? (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              void fork();
            }}
          >
            <p className="text-sm text-muted-foreground">
              Новая сессия начнёт с копии этого разговора и пойдёт своей дорогой, с теми же правами. Эта сессия не
              изменится.
            </p>
            <Textarea
              aria-label="Первое сообщение"
              value={text}
              onChange={(e) => {
                setText(e.target.value);
                setId(inputId());
              }}
              rows={3}
              placeholder="Например: а если ехать поездом?"
            />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setForking(false)}>
                Назад
              </Button>
              <Button type="submit" disabled={busy || !text.trim()}>
                {busy ? "Ответвляю…" : "Ответвить"}
              </Button>
            </div>
          </form>
        ) : (
          <div className="flex flex-col gap-4">
            {spheres.length > 0 && (
              <fieldset className="flex flex-col gap-2">
                <legend className="mb-1 text-sm font-medium">Сфера</legend>
                <div className="flex flex-wrap gap-2">
                  {spheres.map((sphere) => choice(sphere.name, sphereLabel(sphere)))}
                  {choice(null, NO_SPHERE)}
                </div>
              </fieldset>
            )}
            <Button variant="outline" disabled={busy} onClick={() => change(() => pin(s.id, !s.pinned))}>
              <Pin className={cn(s.pinned && "fill-current")} />
              {s.pinned ? "Открепить" : "Закрепить"}
            </Button>
            {s.sphere && (
              <Button variant="outline" disabled={busy} onClick={() => change(() => calls.setMain(s.id, !s.main))}>
                <Star className={cn(s.main && "fill-current")} />
                {s.main ? "Больше не главная" : "Сделать главной в сфере"}
              </Button>
            )}
            {(s.base ?? 0) > 0 && (
              <Button
                variant="outline"
                disabled={busy}
                onClick={async () => {
                  close();
                  setBase(await openBase(s.id));
                }}
              >
                <Database />В Базу · {s.base}
              </Button>
            )}
            {(s.push ?? 0) > 0 && (
              <Button
                variant="outline"
                disabled={busy}
                onClick={async () => {
                  close();
                  setPush(await openPush(s.id));
                }}
              >
                <GitBranch />
                Запушить · {s.push}
              </Button>
            )}
            {!s.parent && (
              <Button variant="outline" onClick={() => setForking(true)}>
                <Fork />
                Ответвить…
              </Button>
            )}
            <div className="flex justify-end">
              <Button variant="ghost" onClick={close}>
                Закрыть
              </Button>
            </div>
          </div>
        )}
      </Dialog>
      <BaseDialog id={base} onClose={() => setBase(null)} />
      <PushDialog id={push} onClose={() => setPush(null)} />
    </>
  );
}
