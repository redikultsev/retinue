// A question card decided in «Today»'s pane: the question, the session's brief for it, its options to pick, one button
// that answers. The brief is what the session wrote just before asking (the feed's `said`) when it is in the brief's
// shape — labelled lines, «**Recommend:** …» (deploy/lanes/instructions/common.md) — with its whole text a tap away;
// a text not in that shape shows a few lines and «Show all». The recommended option is picked at the start; 1–9 pick,
// Enter answers. Same rules as the card in a session (`QuestionCard`): each answer is bound to the card's digest.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Check, ChevronRight } from "@/components/icons";
import { questionsOf, RECOMMENDED, viewOf } from "@/components/question-card";
import { Markdown } from "@/components/transcript/item";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { type Live, live as defaultLive } from "@/lib/live";
import { toast } from "@/lib/toast";
import type { Approval, QuestionReply } from "@/lib/types";
import { cn } from "@/lib/utils";

const BRIEF = /^\s*(?:[-*]\s+)?\*\*(Recommend|Why|Cost|Risk|Skip|Not checked)\s*[:.]?\s*\*\*\s*[:.]?\s*(.+)$/i;

/** The brief's lines in the session's text, by their labels; empty when it has fewer than two. */
export function briefOf(said: string): [string, string][] {
  const lines = said.split("\n").flatMap((line): [string, string][] => {
    const m = BRIEF.exec(line);
    return m ? [[m[1]!.charAt(0).toUpperCase() + m[1]!.slice(1).toLowerCase(), m[2]!.trim()]] : [];
  });
  return lines.length >= 2 ? lines : [];
}

/** A text not in the brief's shape: a few lines at rest, the rest a tap away. */
function Said({ text }: { text: string }) {
  const box = useRef<HTMLDivElement>(null);
  const [whole, setWhole] = useState(false);
  const [long, setLong] = useState(false);
  useLayoutEffect(() => {
    const el = box.current;
    if (el && !whole) setLong(el.scrollHeight > el.clientHeight + 4);
  }, [whole]);
  return (
    <div data-testid="question-said" className="flex flex-col gap-1">
      <div
        ref={box}
        className={cn(
          "min-w-0 break-words text-muted-foreground",
          !whole && "max-h-32 overflow-hidden",
          !whole && long && "[mask-image:linear-gradient(to_bottom,black_55%,transparent)]", // ui-craft: allow — the cut text fades, no colour
        )}
      >
        <Markdown text={text} />
      </div>
      {(long || whole) && (
        <button type="button" aria-expanded={whole} className="hit w-fit text-sm text-primary underline-offset-4 hover:underline" onClick={() => setWhole((w) => !w)}>
          {whole ? "Show less" : "Show all"}
        </button>
      )}
    </div>
  );
}

/** Typing into a field, a button or a link holding the focus: the pane's keys stand aside. */
export const typing = (target: EventTarget | null) =>
  target instanceof HTMLElement &&
  (target.isContentEditable ||
    ["TEXTAREA", "SELECT"].includes(target.tagName) ||
    (target instanceof HTMLInputElement && !["radio", "checkbox", "button"].includes(target.type)));

export function QuestionDecide({
  approval,
  said = "",
  live = defaultLive,
  answer = api.answer,
  onDecided,
}: {
  approval: Approval;
  said?: string;
  live?: Live;
  answer?: typeof api.answer;
  onDecided?: (approval: Approval) => void;
}) {
  const questions = questionsOf(approval.input);
  const view = viewOf(approval);
  const n = Math.max(0, questions.findIndex((_, i) => !view.answers[String(i)]));
  const q = questions[n];
  const recommended = q ? q.options.findIndex((o) => RECOMMENDED.test(o.label)) : -1;
  const [picks, setPicks] = useState<number[]>(() => (recommended >= 0 && q && !q.multiSelect ? [recommended] : []));
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  // a new question of the same card starts from its own recommendation
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset once per question
  useEffect(() => {
    setPicks(recommended >= 0 && q && !q.multiSelect ? [recommended] : []);
    setText("");
  }, [approval.id, n]);

  const brief = briefOf(said);
  const own = text.trim();
  const chosen = q ? picks.map((i) => q.options[i]?.label).filter((l): l is string => !!l) : [];
  const ready = !busy && approval.state === "shown" && (own ? true : chosen.length > 0);

  async function send() {
    if (!q || !ready) return;
    const reply: QuestionReply = own ? { text: own } : { labels: chosen };
    setBusy(true);
    try {
      const res = await answer(approval, n, reply);
      toast(res.toast);
      if (res.approval) live.dispatch({ type: "event", id: null, event: { kind: "approval", data: res.approval } });
      if (res.approval && res.approval.state === "answered") onDecided?.(res.approval);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
    } finally {
      setBusy(false);
    }
  }

  const pick = (i: number) => {
    if (!q || i < 0 || i >= q.options.length) return;
    setPicks((now) => (q.multiSelect ? (now.includes(i) ? now.filter((x) => x !== i) : [...now, i]) : [i]));
  };

  // 1–9 pick, Enter answers — while nothing is being typed
  const keys = useRef({ pick, send });
  keys.current = { pick, send };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (/^[1-9]$/.test(e.key)) {
        e.preventDefault();
        keys.current.pick(Number(e.key) - 1);
      } else if (e.key === "Enter" && !(e.target instanceof HTMLButtonElement)) {
        e.preventDefault();
        void keys.current.send();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!q) return null;
  const label = own ? "Send your answer" : chosen.length === 1 ? `Answer · ${chosen[0]!.replace(RECOMMENDED, "")}` : chosen.length ? `Answer · ${chosen.length} picked` : "Answer";
  return (
    <div data-testid="question-decide" className="flex flex-col gap-6">
      <h2 className="text-xl font-medium text-balance">{q.question}</h2>
      {questions.length > 1 && (
        <p className="-mt-4 text-xs text-muted-foreground tabular-nums">
          Question {n + 1} of {questions.length}
        </p>
      )}
      {brief.length > 0 ? (
        <div className="flex flex-col gap-1">
          <dl data-testid="question-brief" className="grid grid-cols-[88px_minmax(0,1fr)] gap-x-4 gap-y-1 rounded-lg border bg-card px-4 py-3 max-sm:grid-cols-1">
            {brief.map(([k, v], i) => (
              <div key={k} className="contents">
                <dt className="text-xs leading-5 text-muted-foreground">{k}</dt>
                <dd className={cn("min-w-0 break-words max-sm:mb-2", i === 0 ? "text-foreground" : "text-muted-foreground")}>{v}</dd>
              </div>
            ))}
          </dl>
          <details className="group">
            <summary className="hit inline-flex h-8 cursor-pointer list-none items-center gap-1 text-sm text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden">
              <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" />
              The session's full reasoning
            </summary>
            <div className="max-w-[68ch] pt-2 text-muted-foreground">
              <Markdown text={said} />
            </div>
          </details>
        </div>
      ) : said ? (
        <Said key={said} text={said} />
      ) : null}
      <fieldset className="flex min-w-0 flex-col gap-2">
        <legend className="mb-2 text-xs text-muted-foreground">{q.multiSelect ? "Pick any" : "Your answer"}</legend>
        {q.options.map((o, i) => {
          const on = picks.includes(i) && !own;
          return (
            <label
              key={o.label}
              className={cn(
                "grid w-full cursor-pointer grid-cols-[16px_minmax(0,1fr)_20px] items-start gap-x-3 rounded-lg border px-3.5 py-3 text-left transition-colors has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/50",
                on ? "border-primary bg-primary/10" : "hover:bg-accent/40",
              )}
            >
              <input
                type={q.multiSelect ? "checkbox" : "radio"}
                name={`question-${approval.id}-${n}`}
                className="sr-only"
                checked={on}
                disabled={busy}
                onChange={() => {
                  setText("");
                  pick(i);
                }}
              />
              <span
                aria-hidden
                className={cn(
                  "mt-0.5 grid size-4 place-items-center border",
                  q.multiSelect ? "rounded-[4px]" : "rounded-full",
                  on ? "border-primary" : "border-muted-foreground",
                )}
              >
                {on && (q.multiSelect ? <Check className="size-3 text-primary" /> : <span className="size-2 rounded-full bg-primary" />)}
              </span>
              <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1 font-medium">
                {o.label.replace(RECOMMENDED, "")}
                {RECOMMENDED.test(o.label) && (
                  <span className="rounded border px-1.5 text-xs font-normal text-muted-foreground">Recommended</span>
                )}
              </span>
              {i < 9 ? (
                <span aria-hidden className="rounded border text-center font-mono text-xs leading-4 text-muted-foreground max-md:hidden">
                  {i + 1}
                </span>
              ) : (
                <span />
              )}
              {o.description && <span className="col-start-2 mt-0.5 text-[13px] text-muted-foreground">{o.description}</span>}
            </label>
          );
        })}
        <Textarea
          aria-label="Your own answer"
          placeholder="Or answer in your own words"
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void send();
          }}
          className="min-h-10 resize-none border-dashed"
        />
      </fieldset>
      <div>
        <Button size="lg" disabled={!ready} onClick={() => void send()}>
          {busy ? "Sending…" : label}
        </Button>
      </div>
    </div>
  );
}
