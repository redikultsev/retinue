// «Summary»: a session at a glance, as the right panel of Claude's desktop app has it — where it runs, its context, its
// plan, what hangs under it, what it changed and handed over, its connectors. Only what the platform knows: a section
// with nothing known is left out, and what the platform cannot know is said once at the bottom.
import { Link } from "@tanstack/react-router";
import { useMemo } from "react";
import type * as React from "react";
import { BaseButton } from "@/components/base-card";
import { size } from "@/components/files";
import { FileIcon, GitBranch } from "@/components/icons";
import { PushButton, prLink } from "@/components/push-card";
import { TodoList } from "@/components/transcript/item";
import { sessionStateWord, when } from "@/lib/format";
import { live, useLiveState } from "@/lib/live";
import { sphereLabel } from "@/lib/spheres";
import { orderedFiles, orderedItems } from "@/lib/store";
import { childrenOf, latestPlan, planLine, tokensWord } from "@/lib/summary";
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
  /** Open the session's diff: the right pane on a wide screen, the «Changes» tab on a phone. */
  onDiff?: () => void;
  /** Open a handed-over file: the right pane, or its own page. */
  onFile?: (id: string) => void;
  /** A link was followed (the phone's sheet closes). */
  onLeave?: () => void;
}

export function SessionSummary({ id, onDiff, onFile, onLeave }: Props) {
  const state = useLiveState();
  const s = state.sessions[id];
  const about = state.about[id];
  const itemsOf = state.items[id];
  const plan = useMemo(() => latestPlan(orderedItems(itemsOf)), [itemsOf]);
  const filesOf = state.files[id];
  const files = useMemo(() => orderedFiles(filesOf), [filesOf]);
  const kids = useMemo(() => childrenOf(state.sessions, id), [state.sessions, id]);
  const prs = useMemo(
    () =>
      Object.values(state.pushes)
        .filter((p) => p.session === id && p.url && prLink(p.url))
        .sort((a, b) => b.at - a.at),
    [state.pushes, id],
  );
  if (!s) return <p className="p-3 text-sm text-muted-foreground">Loading…</p>;
  const sphere = live.meta?.spheres?.find((x) => x.name === s.sphere);
  const up = s.sub_of ?? (s.side || s.parent ? s.parent : null) ?? s.fork_of ?? null;
  const upWord = s.sub_of ? "sub-session of" : s.parent ? "side question on" : "fork of";
  const upTitle = up ? state.sessions[up]?.title || up : "";
  const step = plan ? planLine(plan) : null;
  const go = (to: string) => (
    <Link to="/s/$id" params={{ id: to }} onClick={onLeave} className="text-primary hover:underline">
      {state.sessions[to]?.title || to}
    </Link>
  );

  return (
    <div data-testid="session-summary" className="flex flex-col">
      <Section title="Environment">
        <dl className="flex flex-col gap-1">
          <Field name="State">
            {sessionStateWord(s.state)}
            {s.queued && ` · ${QUEUED}`}
          </Field>
          <Field name="Track">
            {s.track} · {s.lane}
            {s.kind === "code" && " · code"}
          </Field>
          {about?.cwd && (
            <Field name="Folder" mono title={about.cwd}>
              {about.cwd}
            </Field>
          )}
          {about?.model && (
            <Field name="Model" mono>
              {about.model}
            </Field>
          )}
          {about?.cli_version && (
            <Field name="Claude Code" mono>
              {about.cli_version}
            </Field>
          )}
          {about?.permission_mode && (
            <Field name="Mode" mono>
              {about.permission_mode}
            </Field>
          )}
          <Field name="Sphere">
            {sphere ? sphereLabel(sphere) : s.sphere || "—"}
            {s.main && " · main"}
          </Field>
          {up && (
            <Field name="Under" title={upTitle}>
              {upWord} {go(up)}
            </Field>
          )}
          <Field name="Started">{when(s.created)}</Field>
        </dl>
      </Section>

      {about?.context_tokens != null && (
        <Section title="Context">
          <p className="text-[13px]">
            <span className="font-medium tabular-nums">{tokensWord(about.context_tokens)}</span> tokens in the last
            request
          </p>
        </Section>
      )}

      {plan && step && (
        <Section title="Plan" aside={<span className="tabular-nums">{`${step.done} of ${step.total}`}</span>}>
          <TodoList todos={plan} small />
        </Section>
      )}

      {kids.length > 0 && (
        <Section title="Sub-sessions" aside={<span className="tabular-nums">{kids.length}</span>}>
          <ul className="flex flex-col">
            {kids.map(({ session: k, how }) => (
              <li key={k.id}>
                <Link
                  to="/s/$id"
                  params={{ id: k.id }}
                  onClick={onLeave}
                  className="flex h-7 items-center gap-2 rounded-md px-1 text-[13px] hover:bg-accent pointer-coarse:min-h-11"
                >
                  <Dot s={k} />
                  <span className="min-w-0 flex-1 truncate">{k.title || k.track}</span>
                  <span className="shrink-0 text-[11px] text-muted-foreground">
                    {k.queued ? "queued" : how === "sub-session" ? sessionStateWord(k.state) : `${how} · ${sessionStateWord(k.state)}`}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {(s.kind === "code" || (s.base ?? 0) > 0 || (s.push ?? 0) > 0 || s.push_why || prs.length > 0) && (
        <Section title="Changes">
          <div className="flex flex-wrap gap-1.5">
            {s.kind === "code" && onDiff && (
              <button
                type="button"
                onClick={onDiff}
                className="inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-xs hover:bg-accent pointer-coarse:h-9 [&_svg]:size-3.5"
              >
                <GitBranch />
                Session diff
              </button>
            )}
            {(s.base ?? 0) > 0 && <BaseButton session={id} count={s.base!} />}
            {(s.push ?? 0) > 0 && <PushButton session={id} count={s.push!} />}
          </div>
          {s.push_why && <p className="text-xs text-muted-foreground">Cannot push — {s.push_why}</p>}
          {prs.map((p) => (
            <a
              key={p.id}
              href={prLink(p.url)!}
              target="_blank"
              rel="noopener noreferrer"
              className="truncate text-[13px] text-primary hover:underline"
            >
              PR · {p.repo} → {p.base}
            </a>
          ))}
        </Section>
      )}

      {files.length > 0 && (
        <Section title="Files" aside={<span className="tabular-nums">{files.length}</span>}>
          <ul className="flex flex-col">
            {files.map((f) => (
              <li key={f.id}>
                <button
                  type="button"
                  onClick={() => onFile?.(f.id)}
                  className="flex h-7 w-full items-center gap-2 rounded-md px-1 text-left text-[13px] hover:bg-accent pointer-coarse:min-h-11"
                >
                  <FileIcon className="size-3.5 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1 truncate font-mono text-xs">{f.path}</span>
                  <span className="shrink-0 text-[11px] text-muted-foreground">{size(f.size)}</span>
                </button>
              </li>
            ))}
          </ul>
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

      <p className="px-3 py-3 text-[11px] leading-snug text-muted-foreground">
        The platform does not know the background tasks or the context window size — they are not shown here.
        {!about && " The model, folder and connectors appear once the platform sends them."}
      </p>
    </div>
  );
}
