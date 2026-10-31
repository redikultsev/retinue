// Comments on a diff's lines (lib/remarks.ts): the box a tap on a line opens, a comment under its line, the session's
// tray («3 comments · Send») and a plain-text diff whose lines take a tap (a push's commits). No diff renderer here: the
// tray rides on cards that load @pierre/diffs only when their diff opens.
import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { type PlainLine, plainLines, quoteOf, type Remark, type Remarks, remarks, remarksMessage, type Side, useRemarks } from "@/lib/remarks";
import { toast } from "@/lib/toast";
import { cn, inputId } from "@/lib/utils";

export interface Target {
  file: string;
  line: number;
  side: Side;
  quote: string;
}

/** «2 comments» */
export function commentsWord(n: number): string {
  return `${n} ${n === 1 ? "comment" : "comments"}`;
}

/** The box under a tapped line: the comment, «Add», «Cancel». It goes to the tray, not yet to the session. */
export function RemarkBox({ target, initial = "", onAdd, onCancel }: { target: Target; initial?: string; onAdd: (text: string) => void; onCancel: () => void }) {
  const [text, setText] = useState(initial);
  return (
    <div data-testid="remark-box" className="flex flex-col gap-2 border-y bg-background p-2 font-sans">
      <div className="text-xs break-all text-muted-foreground">
        {target.file}:{target.line}
        {target.side === "deletions" ? " · removed line" : ""}
      </div>
      <Textarea autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder="What is wrong here?" rows={2} />
      <div className="flex gap-2">
        <Button disabled={!text.trim()} onClick={() => onAdd(text.trim())}>
          {initial ? "Save" : "Add comment"}
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

/** A comment waiting in the tray, under the line it is on: a tap on the line again edits it. */
export function RemarkNote({ remark, onRemove }: { remark: Remark; onRemove: () => void }) {
  return (
    <div data-testid="remark-note" className="flex items-start gap-2 border-y bg-primary/5 px-2 py-1.5 font-sans text-sm">
      <p className="min-w-0 flex-1 break-words whitespace-pre-wrap">{remark.text}</p>
      <Button size="sm" variant="ghost" onClick={onRemove}>
        Remove
      </Button>
    </div>
  );
}

/** The comments of one file's diff and the box being written: what to show under which line. */
export function useLineRemarks(session: string, file: string, from: Remarks = remarks) {
  const all = useRemarks(session, from);
  const [target, setTarget] = useState<Target | null>(null);
  const mine = all.filter((r) => r.file === file);
  const at = (side: Side, line: number) => mine.find((r) => r.side === side && r.line === line);
  return {
    mine,
    target,
    at,
    open: (side: Side, line: number, shown: string) => setTarget({ file, side, line, quote: quoteOf(shown) }),
    close: () => setTarget(null),
    add: (t: Target, text: string) => {
      from.add(session, { ...t, text });
      setTarget(null);
    },
    remove: (r: Remark) => from.remove(session, r.id),
  };
}

/** A diff as text — a push's commits, headers and messages included — every line of a hunk a tap away. Nothing is
 * rendered: each line is its own text. */
export function PlainDiff({ session, text, from = remarks, className }: { session: string; text: string; from?: Remarks; className?: string }) {
  const lines = plainLines(text);
  const all = useRemarks(session, from);
  const [target, setTarget] = useState<Target | null>(null);
  const on = (l: PlainLine) => all.find((r) => r.file === l.file && r.line === l.line && r.side === l.side);
  const editing = (l: PlainLine) => target !== null && target.file === l.file && target.line === l.line && target.side === l.side;
  return (
    <div data-testid="push-diff" className={cn("max-h-[60vh] overflow-auto rounded-lg border py-1 font-mono text-xs", className)}>
      {lines.map((l, k) => {
        const row = cn(
          "block w-full px-2 text-left break-words whitespace-pre-wrap",
          l.text.startsWith("+") && l.file && "bg-emerald-500/10",
          l.text.startsWith("-") && l.file && "bg-rose-500/10",
        );
        if (!l.file || l.line === undefined || !l.side) {
          return (
            <div key={k} className={row}>
              {l.text || " "}
            </div>
          );
        }
        const remark = on(l);
        const point = { file: l.file, line: l.line, side: l.side };
        return (
          <div key={k}>
            <button type="button" className={cn(row, "hover:bg-accent/60 active:bg-accent")} onClick={() => setTarget({ ...point, quote: quoteOf(l.text.slice(1)) })}>
              {l.text || " "}
            </button>
            {editing(l) && target ? (
              <RemarkBox
                target={target}
                initial={remark?.text ?? ""}
                onCancel={() => setTarget(null)}
                onAdd={(t) => {
                  from.add(session, { ...target, text: t });
                  setTarget(null);
                }}
              />
            ) : (
              remark && <RemarkNote remark={remark} onRemove={() => from.remove(session, remark.id)} />
            )}
          </div>
        );
      })}
    </div>
  );
}

/** The session's tray: «3 comments · Send», each comment with its line and «Remove». «Send» puts them to the session as
 * one message through its input — busy or archived, as a message typed there would go — and empties the tray. */
export function RemarkTray({ session, from = remarks, send = api.input, className }: { session: string; from?: Remarks; send?: typeof api.input; className?: string }) {
  const all = useRemarks(session, from);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  // The same comments again (a lost answer, a retry) go under the same key: the session gets them once.
  const tried = useRef<{ id: string; body: string } | null>(null);
  if (all.length === 0) return null;

  async function sendAll() {
    const sent = all;
    const body = remarksMessage(sent);
    const id = tried.current?.body === body ? tried.current.id : inputId();
    tried.current = { id, body };
    setBusy(true);
    try {
      const res = await send(session, body, id);
      from.clear(session, sent);
      tried.current = null;
      setOpen(false);
      toast(res?.say && res.say !== "Accepted." ? res.say : `Sent: ${commentsWord(sent.length)}.`);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Not sent — try again.", "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div data-testid="remark-tray" className={cn("sticky bottom-0 z-10 flex flex-col gap-2 rounded-lg border bg-background/95 p-2 shadow-md backdrop-blur", className)}>
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" aria-expanded={open} onClick={() => setOpen(!open)}>
          {commentsWord(all.length)}
        </Button>
        <Button size="sm" className="ml-auto" disabled={busy} onClick={() => void sendAll()}>
          {busy ? "Sending…" : "Send"}
        </Button>
      </div>
      {open && (
        <ul className="flex max-h-[40vh] flex-col divide-y overflow-auto text-sm">
          {all.map((r) => (
            <li key={r.id} className="flex items-start gap-2 py-1.5">
              <div className="min-w-0 flex-1">
                <div className="font-mono text-xs break-all text-muted-foreground">
                  {r.file}:{r.line}
                  {r.side === "deletions" ? " · removed line" : ""}
                </div>
                {r.quote && <code className="block font-mono text-xs break-words whitespace-pre-wrap text-muted-foreground">{r.quote}</code>}
                <p className="break-words whitespace-pre-wrap">{r.text}</p>
              </div>
              <Button size="sm" variant="ghost" onClick={() => from.remove(session, r.id)}>
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
