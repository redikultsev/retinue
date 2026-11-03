// «Details»: where a session runs and how, for when the owner wants to know — its folder, track, model, Claude Code's
// version and mode, the context its last request carried, its connectors, when it started and its id; the session
// it hangs under and what hangs under it, each by its relation's icon. Only what the platform knows: a line with
// nothing known is left out. What it made is «Results», its plan the line above the input box.
import { Link } from "@tanstack/react-router";
import { useMemo } from "react";
import type * as React from "react";
import { SphereMark } from "@/components/icons";
import { RELATION_WORDS, RelationIcon, type Relation, upOf } from "@/components/relation";
import { sessionStateWord, when } from "@/lib/format";
import { live, useLiveState } from "@/lib/live";
import { sphereLabel } from "@/lib/spheres";
import { childrenOf, tokensWord } from "@/lib/summary";
import { QUEUED, type Session } from "@/lib/types";
import { cn } from "@/lib/utils";

function Section({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-1.5 border-b px-3 py-3 last:border-b-0">
      <h3 className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <span className="flex-1">{title}</span>
        {aside}
      </h3>
      {children}
    </section>
  );
}

function Field({ name, children, mono, title }: { name: string; children: React.ReactNode; mono?: boolean; title?: string }) {
  return (
    <div className="flex min-w-0 items-baseline gap-2 text-[13px]">
      <dt className="w-24 shrink-0 text-muted-foreground">{name}</dt>
      <dd title={title} className={cn("min-w-0 flex-1 truncate", mono && "font-mono text-xs")}>
        {children}
      </dd>
    </div>
  );
}

/** A dot for a state: waiting on the owner, working, done or idle. */
export function Dot({ s }: { s: Session }) {
  const waiting = s.state === "waiting" || s.waiting > 0;
  return (
    <span
      aria-hidden="true"
      className={cn(
        "size-1.5 shrink-0 rounded-full",
        waiting ? "bg-warning" : s.state === "active" ? "animate-pulse bg-primary" : "bg-muted-foreground/40",
      )}
    />
  );
}

interface Props {
  id: string;
  /** A link was followed (the phone's sheet closes). */
  onLeave?: () => void;
}

export function SessionSummary({ id, onLeave }: Props) {
  const state = useLiveState();
  const s = state.sessions[id];
  const about = state.about[id];
  const kids = useMemo(() => childrenOf(state.sessions, id), [state.sessions, id]);
  if (!s) return <p className="p-3 text-sm text-muted-foreground">Loading…</p>;
  const sphere = live.meta?.spheres?.find((x) => x.name === s.sphere);
  const up = upOf(s);
  const link = (to: string, how: Relation) => (
    <Link to="/s/$id" params={{ id: to }} onClick={onLeave} title={RELATION_WORDS[how]} className="inline-flex max-w-full items-center gap-1.5 text-primary hover:underline">
      <RelationIcon how={how} className="size-3.5 shrink-0" />
      <span className="sr-only">{RELATION_WORDS[how]}: </span>
      <span className="truncate">{state.sessions[to]?.title || "the session it came from"}</span>
    </Link>
  );

  return (
    <div data-testid="session-summary" className="flex flex-col">
      <Section title="Where it runs">
        <dl className="flex flex-col gap-1">
          <Field name="State">
            {sessionStateWord(s.state)}
            {s.queued && ` · ${QUEUED}`}
          </Field>
          {about?.cwd && (
            <Field name="Folder" mono title={about.cwd}>
              {about.cwd}
            </Field>
          )}
          <Field name="Track">
            {s.track} · {s.lane}
            {s.kind === "code" && " · code"}
          </Field>
          {about?.model && (
            <Field name="Model" mono>
              {about.model}
            </Field>
          )}
          {about?.permission_mode && (
            <Field name="Mode" mono>
              {about.permission_mode}
            </Field>
          )}
          {about?.context_tokens != null && (
            <Field name="Context" title="Tokens its last request carried">
              <span className="tabular-nums">{tokensWord(about.context_tokens)}</span> tokens
            </Field>
          )}
          {about?.cli_version && (
            <Field name="Claude Code" mono>
              {about.cli_version}
            </Field>
          )}
          <Field name="Sphere">
            {sphere && <SphereMark icon={sphere.icon} className="mr-1 inline size-3.5 align-[-2px] text-muted-foreground" />}
            {sphere ? sphereLabel(sphere) : s.sphere || "—"}
            {s.main && " · main"}
          </Field>
          <Field name="Started">{when(s.created)}</Field>
          <Field name="Session" mono>
            {s.id}
          </Field>
        </dl>
      </Section>

      {(up || kids.length > 0) && (
        <Section title="Related" aside={kids.length > 0 ? <span className="tabular-nums">{kids.length}</span> : undefined}>
          {up && <p className="min-w-0 text-[13px]">{link(up.id, up.how)}</p>}
          {kids.length > 0 && (
            <ul className="flex flex-col">
              {kids.map(({ session: k, how }) => (
                <li key={k.id}>
                  <Link
                    to="/s/$id"
                    params={{ id: k.id }}
                    onClick={onLeave}
                    className="flex h-7 items-center gap-2 rounded-md px-1 text-[13px] hover:bg-accent pointer-coarse:min-h-11"
                  >
                    <RelationIcon how={how as Relation} className="size-3.5 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate">{k.title || k.track}</span>
                    <Dot s={k} />
                    <span className="shrink-0 text-[11px] text-muted-foreground">{k.queued ? "queued" : sessionStateWord(k.state)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Section>
      )}

      {(about?.mcp.length ?? 0) > 0 && (
        <Section title="Connectors">
          <ul className="flex flex-col gap-1">
            {about!.mcp.map((m) => (
              <li key={m.name} className="flex items-center gap-2 text-[13px]">
                <span
                  aria-hidden="true"
                  className={cn(
                    "size-1.5 rounded-full",
                    m.status === "connected" ? "bg-success" : m.status === "failed" ? "bg-destructive" : "bg-muted-foreground/50",
                  )}
                />
                <span className="min-w-0 flex-1 truncate font-mono text-xs">{m.name}</span>
                <span className="text-[11px] text-muted-foreground">{m.status || "—"}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}
