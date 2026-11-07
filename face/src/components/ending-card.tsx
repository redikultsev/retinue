// A session's request drawn by how its turn ended (`ending` on the feed's row, lib/operator.ts) — on the operator's
// cards and in the inbox's pane alike: «You asked: …» first, then what it needs — an ask's options with what each
// changes, what an approval will do and where, the thing to look at opened in place, the owner's own step with its
// command to copy, what a finished turn came to and what is next. Every answer goes to the session as the owner's next
// message, and the card says it in place («You: …») instead of its buttons. A session on one of the owner's computers
// (`computer` on the row) is answered there: the card shows the same words, its options without buttons, and the way to
// the session on that computer.
import { useState } from "react";
import { FileRow, type Kind } from "@/components/files";
import { ArrowUpRight } from "@/components/icons";
import { Reply } from "@/components/reply";
import { Button, buttonVariants } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import type { EndingArtifact, FeedRow, TurnEnding } from "@/lib/feed";
import { thingName, thingsOf, WHY_OWNER } from "@/lib/operator";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

export const FRAME = "flex flex-col gap-4 rounded-lg border bg-card p-4";
const META = "text-[13px] text-muted-foreground";

const KINDS = new Set<string>(["image", "markdown", "text", "html", "pdf", "audio", "video"]);

/** A thing in place: a picture as itself, a PDF or a page in its frame, a page on the web by its link. */
export function ThingView({ thing, opened }: { thing: EndingArtifact; opened: boolean }) {
  const name = thingName(thing);
  if (thing.kind === "link" && thing.src)
    return (
      <a href={thing.src} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-sm text-primary hover:underline">
        {name}
        <ArrowUpRight className="size-3.5" />
      </a>
    );
  if (!thing.src || thing.kind === "link")
    return (
      <p className={META}>
        {name} — {thing.why || "the platform does not serve it to the app."}
      </p>
    );
  return (
    <ul aria-label={name}>
      <FileRow file={{ path: name, name, size: 0 }} kind={(KINDS.has(thing.kind) ? thing.kind : "other") as Kind} src={thing.src} frame={thing.src} opened={opened} />
    </ul>
  );
}

/** The owner's words to the session as its next message; the card says them once sent. */
function useSend(session: string | null, onSent: (words: string) => void) {
  const [busy, setBusy] = useState(false);
  return {
    busy,
    send: async (words: string) => {
      if (!session || busy) return;
      setBusy(true);
      try {
        await api.input(session, words);
        onSent(words);
      } catch (e) {
        toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
      } finally {
        setBusy(false);
      }
    },
  };
}

function Copy({ text, where }: { text: string; where?: string }) {
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied.");
    } catch {
      toast("Couldn't copy — select the text by hand.", "error");
    }
  }
  return (
    <div className="overflow-hidden rounded-md border">
      <div className="flex items-center gap-2 bg-muted py-1 pr-1 pl-3 text-xs text-muted-foreground">
        <span className="min-w-0 flex-1 truncate">{where || "To copy"}</span>
        <Button variant="ghost" size="sm" onClick={() => void copy()}>
          Copy
        </Button>
      </div>
      <pre className="overflow-x-auto px-3 py-2 font-mono text-[13px] break-words whitespace-pre-wrap">{text}</pre>
    </div>
  );
}

const REPLY: Record<TurnEnding["kind"], string> = {
  ask: "Or answer in your own words",
  approve: "Or write to the session",
  look: "What to fix",
  step: "What came out, or what is in the way",
  done: "Reply to go on with it",
};

/** The card's main line: what it asks or says, by its kind. */
export function mainOf(e: TurnEnding): string {
  if (e.kind === "ask") return e.context || e.why || "";
  if (e.kind === "approve") return e.will || "";
  return e.what || "";
}

/** «Answer on the computer ↗»: the session's own link on the computer it runs on, or where it is when there is none. */
export function OnComputer({ computer, verb = "Answer" }: { computer: NonNullable<FeedRow["computer"]>; verb?: "Answer" | "Open" }) {
  if (!computer.link) return <p className={META}>{verb} it on {computer.device}: the session runs there.</p>;
  return (
    <div>
      <a href={computer.link} className={buttonVariants({ variant: "outline" })}>
        {verb} on {computer.device}
        <ArrowUpRight />
      </a>
    </div>
  );
}

export function EndingCard({ row, e }: { row: FeedRow; e: TurnEnding }) {
  const [said, setSaid] = useState<string | null>(null);
  // a computer's session: read here, answered on the computer
  const here = !row.computer;
  const { busy, send } = useSend(row.session, setSaid);
  const things = thingsOf(e);
  const main = mainOf(e) || (e.kind === "look" ? "" : row.essence);
  const options = e.kind === "ask" ? (e.options ?? []) : [];
  const answer = (words: string) => void send(words);
  const command = e.kind === "step" ? e.command || e.copy : undefined;
  return (
    <div className={FRAME}>
      {e.request && (
        <p className={META}>
          <span className="text-foreground">You asked:</span> {e.request}
        </p>
      )}
      {main && <p className="max-w-[68ch] text-[15px] font-medium text-balance break-words">{main}</p>}
      {e.kind === "ask" && e.context && e.why && <p className={cn(META, "max-w-[68ch]")}>{e.why}</p>}
      {e.kind === "approve" && e.items?.length ? (
        <ul aria-label="Where" className="flex list-disc flex-col gap-0.5 pl-5 text-sm">
          {e.items.map((it) => (
            <li key={it}>{it}</li>
          ))}
        </ul>
      ) : null}
      {e.kind === "step" && e.where && !command && <p className={META}>Where: {e.where}</p>}
      {command && <Copy text={command} where={e.where} />}
      {things.length > 0 && (
        <div className="flex flex-col gap-2">
          {things.map((t, i) => (
            <ThingView key={`${i}:${t.src ?? t.path ?? t.url}`} thing={t} opened={e.kind === "look"} />
          ))}
        </div>
      )}
      {e.kind === "look" && e.checks?.length ? (
        <div className="flex flex-col gap-1">
          <span className={META}>Look at</span>
          <ul className="flex list-disc flex-col gap-0.5 pl-5 text-sm">
            {e.checks.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {e.kind === "done" && e.next && (
        <p className="text-sm">
          <span className="text-muted-foreground">{e.next_by === "you" ? "Your move:" : "Next, the session:"}</span> {e.next}
        </p>
      )}
      {(e.why_owner || e.without) && (
        <p className={META}>
          {e.why_owner && `Yours to decide: ${WHY_OWNER[e.why_owner] ?? e.why_owner}`}
          {e.why_owner && e.without ? " · " : ""}
          {e.without && `With no answer: ${e.without}`}
        </p>
      )}
      {said !== null ? (
        <p className="rounded-md bg-muted px-3 py-2 text-sm">You: «{said}»</p>
      ) : (
        <>
          {options.length > 0 && (
            <fieldset aria-label="Options" className="flex min-w-0 flex-col gap-1.5">
              {options.map((o, i) => (
                <button
                  key={o.label}
                  type="button"
                  disabled={busy || !here}
                  onClick={() => answer(o.label)}
                  className={cn(
                    "flex flex-col items-start gap-0.5 rounded-md border px-3 py-2 text-left transition-colors disabled:opacity-50 max-md:min-h-11",
                    here ? "hover:bg-accent" : "disabled:opacity-100",
                    i === 0 && options.length > 1 && "border-primary/60",
                  )}
                >
                  <span className="text-sm font-medium">
                    {o.label}
                    {i === 0 && options.length > 1 && <span className="ml-2 text-xs font-normal text-primary">Recommended</span>}
                  </span>
                  {o.effect && <span className={META}>{o.effect}</span>}
                </button>
              ))}
            </fieldset>
          )}
          {here && e.kind === "ask" && !options.length && e.guesses?.length ? (
            <div className="flex flex-wrap gap-1.5">
              {e.guesses.map((g) => (
                <Button key={g} variant="outline" size="sm" disabled={busy} onClick={() => answer(g)}>
                  {g}
                </Button>
              ))}
            </div>
          ) : null}
          {here && e.kind === "approve" && (
            <div className="flex flex-wrap gap-2">
              <Button disabled={busy} onClick={() => answer("Allowed — go ahead.")}>
                Allow
              </Button>
              <Button variant="outline" disabled={busy} onClick={() => answer("Don't — leave it as it is.")}>
                Don't
              </Button>
            </div>
          )}
          {here && (e.kind === "look" || e.kind === "step") && (
            <div>
              <Button disabled={busy} onClick={() => answer(e.kind === "look" ? "Looks right." : "Done.")}>
                {e.kind === "look" ? "Looks right" : "Done"}
              </Button>
            </div>
          )}
          {row.computer && <OnComputer computer={row.computer} />}
          {row.session && (
            <Reply session={row.session} placeholder={e.kind === "step" && e.expect ? `Send back: ${e.expect}` : REPLY[e.kind]} onSent={setSaid} />
          )}
        </>
      )}
    </div>
  );
}
