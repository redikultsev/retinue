// A session's menu: its sphere, whether it is the sphere's main session, «To the Base» while its runs left Base files
// uncommitted (retinue/tobase.py), and «Fork» — a new session from a copy of this one's conversation, with the
// track's full rights (retinue/spheres.py); «Pin» too, since a phone's row shows its pin only once pinned; «Archive»
// ends it and its kin and takes them out of every list until a message brings it back (asked once first). Each
// change is the platform's: the answer's row goes into the store as the session's event, and every device shows it.
import { useNavigate, useRouterState } from "@tanstack/react-router";
import { useState } from "react";
import { BaseDialog, openBase } from "@/components/base-card";
import { PushDialog, openPush } from "@/components/push-card";
import { Archive, Database, Fork, GitBranch, More, Pin, Star } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { live } from "@/lib/live";
import { NO_SPHERE, sphereLabel } from "@/lib/spheres";
import { toast } from "@/lib/toast";
import type { Session } from "@/lib/types";
import { cn, inputId } from "@/lib/utils";

type Api = Pick<typeof api, "setSphere" | "setMain" | "fork"> & Partial<Pick<typeof api, "pin" | "archive">>;

export function SessionMenu({ s, calls = api, className }: { s: Session; calls?: Api; className?: string }) {
  const pin = calls.pin ?? api.pin;
  const archive = calls.archive ?? api.archive;
  const [archiving, setArchiving] = useState(false);
  const here = useRouterState({ select: (r) => r.location.pathname });
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
    setArchiving(false);
  }

  async function archiveIt() {
    await change(() => archive(s.id));
    close();
    if (here.startsWith(`/s/${s.id}`)) await navigate({ to: "/sessions" });
  }

  async function change(call: () => Promise<{ session: Session }>) {
    setBusy(true);
    try {
      const res = await call();
      live.dispatch({ type: "event", id: null, event: { kind: "session", data: res.session } });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
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
      toast(e instanceof ApiError ? e.message : "The fork did not start.", "error");
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
        aria-label={`Menu: ${s.title}`}
        title="Sphere, pin, fork, archive"
        className={className}
        onClick={() => setOpen(true)}
      >
        <More />
      </Button>
      <Dialog open={open} onClose={close} title={forking ? "Fork" : archiving ? "Archive this session?" : s.title || "Session"}>
        {archiving ? (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-muted-foreground">
              It stops, and its cards and sub-sessions go with it. Writing to it brings it back.
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setArchiving(false)}>
                Back
              </Button>
              <Button disabled={busy} onClick={() => void archiveIt()}>
                {busy ? "Archiving…" : "Archive"}
              </Button>
            </div>
          </div>
        ) : forking ? (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              void fork();
            }}
          >
            <p className="text-sm text-muted-foreground">
              A new session starts from a copy of this conversation and goes its own way, with the same rights. This session
              does not change.
            </p>
            <Textarea
              aria-label="First message"
              value={text}
              onChange={(e) => {
                setText(e.target.value);
                setId(inputId());
              }}
              rows={3}
              placeholder="For example: what if we go by train?"
            />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setForking(false)}>
                Back
              </Button>
              <Button type="submit" disabled={busy || !text.trim()}>
                {busy ? "Forking…" : "Fork"}
              </Button>
            </div>
          </form>
        ) : (
          <div className="flex flex-col gap-4">
            {spheres.length > 0 && (
              <fieldset className="flex flex-col gap-2">
                <legend className="mb-1 text-sm font-medium">Sphere</legend>
                <div className="flex flex-wrap gap-2">
                  {spheres.map((sphere) => choice(sphere.name, sphereLabel(sphere)))}
                  {choice(null, NO_SPHERE)}
                </div>
              </fieldset>
            )}
            <Button variant="outline" disabled={busy} onClick={() => change(() => pin(s.id, !s.pinned))}>
              <Pin className={cn(s.pinned && "fill-current")} />
              {s.pinned ? "Unpin" : "Pin"}
            </Button>
            {s.sphere && (
              <Button variant="outline" disabled={busy} onClick={() => change(() => calls.setMain(s.id, !s.main))}>
                <Star className={cn(s.main && "fill-current")} />
                {s.main ? "No longer main" : "Make main in the sphere"}
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
                <Database />To the Base · {s.base}
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
                Push · {s.push}
              </Button>
            )}
            {!s.parent && (
              <Button variant="outline" onClick={() => setForking(true)}>
                <Fork />
                Fork…
              </Button>
            )}
            <div className="flex items-center justify-between gap-2">
              <Button variant="ghost" className="text-muted-foreground" onClick={() => setArchiving(true)}>
                <Archive />
                Archive
              </Button>
              <Button variant="ghost" onClick={close}>
                Close
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
