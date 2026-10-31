// A question card (AskUserQuestion, retinue/questions.py): each question with its options as full-width buttons, the
// description on a second line. A single choice is sent by its tap; a multi-select is ticked and sent by «Done»;
// «Own answer…» opens a field for the owner's own words. Every answer is bound to the card's digest, by the same rules
// as a Telegram press — an answer here retires that question's Telegram buttons, and one there shows here.
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { approvalStateWord } from "@/lib/format";
import { type Live, live as defaultLive } from "@/lib/live";
import { toast } from "@/lib/toast";
import type { Approval, QuestionReply, QuestionView } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Question {
  question: string;
  header: string;
  options: { label: string; description: string }[];
  multiSelect: boolean;
}

/** The call's questions as retinue/questions.py `of` reads them: whatever is not shaped so is left out. */
export function questionsOf(input: Record<string, unknown>): Question[] {
  const raw = Array.isArray(input.questions) ? input.questions : [];
  return raw.flatMap((q): Question[] => {
    if (!q || typeof q !== "object" || typeof (q as { question?: unknown }).question !== "string") return [];
    const { question, header, options, multiSelect } = q as Record<string, unknown>;
    return [
      {
        question: question as string,
        header: typeof header === "string" ? header : "",
        options: (Array.isArray(options) ? options : []).flatMap((o) =>
          o && typeof o === "object" && (o as { label?: unknown }).label
            ? [{ label: String((o as { label: unknown }).label), description: String((o as { description?: unknown }).description ?? "") }]
            : [],
        ),
        multiSelect: multiSelect === true,
      },
    ];
  });
}

const viewOf = (approval: Approval): QuestionView => {
  const view = approval.view as unknown as Partial<QuestionView>;
  return { answers: view.answers ?? {}, picks: view.picks ?? {}, status: view.status ?? "" };
};

interface Props {
  approval: Approval;
  live?: Live;
  answer?: typeof api.answer;
  className?: string;
  /** Called with the card once the last question is answered. */
  onDecided?: (approval: Approval) => void;
}

export function QuestionCard({ approval, live = defaultLive, answer = api.answer, className, onDecided }: Props) {
  const questions = questionsOf(approval.input);
  const view = viewOf(approval);
  const open = approval.state === "shown";
  const [busy, setBusy] = useState<number | null>(null);
  const [ticks, setTicks] = useState<Record<number, string[]>>({});
  const [writing, setWriting] = useState<Record<number, string | undefined>>({});

  async function send(n: number, reply: QuestionReply) {
    setBusy(n);
    try {
      const res = await answer(approval, n, reply);
      toast(res.toast);
      if (res.approval) live.dispatch({ type: "event", id: null, event: { kind: "approval", data: res.approval } });
      if (res.approval?.view && n in ((res.approval.view as unknown as QuestionView).answers ?? {}))
        setWriting((w) => ({ ...w, [n]: undefined }));
      if (res.approval && res.approval.state === "answered") onDecided?.(res.approval);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
    } finally {
      setBusy(null);
    }
  }

  const tick = (n: number, label: string) =>
    setTicks((t) => {
      const now = t[n] ?? [];
      return { ...t, [n]: now.includes(label) ? now.filter((l) => l !== label) : [...now, label] };
    });

  return (
    <Card data-testid="question-card" className={cn(open && "border-warning/70 bg-warning/5 ring-1 ring-warning/30", className)}>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-sm">Question · session {approval.session}</CardTitle>
          <Badge variant={open ? "warning" : "secondary"}>{open ? "Awaiting an answer" : approvalStateWord(approval.state)}</Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {questions.map((q, n) => {
          const answered = view.answers[String(n)];
          const picked = ticks[n] ?? [];
          const text = writing[n];
          return (
            <section key={n} data-testid="question" className="flex flex-col gap-2">
              {q.header && <div className="text-xs text-muted-foreground uppercase">{q.header}</div>}
              <div className="font-medium break-words whitespace-pre-wrap">{q.question}</div>
              {answered ? (
                <div data-testid="question-answer" className="text-sm">
                  <span className="text-muted-foreground">{answered.other ? "Own answer: " : "Answer: "}</span>
                  {answered.value}
                </div>
              ) : open ? (
                <>
                  <div className="flex flex-col gap-2">
                    {q.options.map((o) => {
                      const on = picked.includes(o.label);
                      return (
                        <Button
                          key={o.label}
                          variant={q.multiSelect && on ? "default" : "outline"}
                          aria-pressed={q.multiSelect ? on : undefined}
                          disabled={busy !== null}
                          className="h-auto min-h-11 w-full flex-col items-start gap-0.5 py-1.5 text-left whitespace-normal md:min-h-9"
                          onClick={() => (q.multiSelect ? tick(n, o.label) : send(n, { labels: [o.label] }))}
                        >
                          <span className="font-medium">
                            {q.multiSelect ? (on ? "☑ " : "☐ ") : ""}
                            {o.label}
                          </span>
                          {o.description && <span className="text-xs font-normal opacity-80">{o.description}</span>}
                        </Button>
                      );
                    })}
                    {q.multiSelect && (
                      <Button
                        size="lg"
                        disabled={busy !== null || picked.length === 0}
                        onClick={() => send(n, { labels: picked })}
                      >
                        {busy === n ? "Sending…" : `Done${picked.length ? ` (${picked.length})` : ""}`}
                      </Button>
                    )}
                  </div>
                  {text === undefined ? (
                    <Button variant="ghost" className="self-start" onClick={() => setWriting((w) => ({ ...w, [n]: "" }))}>
                      Own answer…
                    </Button>
                  ) : (
                    <form
                      className="flex flex-col gap-2"
                      onSubmit={(e) => {
                        e.preventDefault();
                        if (text.trim()) void send(n, { text: text.trim() });
                      }}
                    >
                      <Textarea
                        aria-label="Own answer"
                        autoFocus
                        value={text}
                        onChange={(e) => setWriting((w) => ({ ...w, [n]: e.target.value }))}
                      />
                      <Button type="submit" disabled={busy !== null || !text.trim()}>
                        {busy === n ? "Sending…" : "Send"}
                      </Button>
                    </form>
                  )}
                </>
              ) : (
                <ul className="flex flex-col gap-1 text-sm text-muted-foreground">
                  {q.options.map((o) => (
                    <li key={o.label}>
                      {o.label}
                      {o.description && <span> — {o.description}</span>}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          );
        })}
        {view.status && <p className="text-sm">{view.status}</p>}
      </CardContent>
    </Card>
  );
}
