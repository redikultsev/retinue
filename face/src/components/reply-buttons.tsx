// A ping's suggested replies («Ответы:») as small chips: a press sends that reply into the session as the owner's
// message — once, whichever place pressed it, here or in Telegram (retinue/pings.py). Once one went in, or the owner
// wrote, the replies are gone in both places; the one that went in is shown as «→ …». 44 px to tap on a touch screen.
import { useState } from "react";
import { ApiError, api } from "@/lib/api";
import { type Live, live as defaultLive } from "@/lib/live";
import { toast } from "@/lib/toast";
import type { Summary } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props {
  summary: Summary;
  live?: Live;
  reply?: typeof api.reply;
  className?: string;
}

export function ReplyButtons({ summary, live = defaultLive, reply = api.reply, className }: Props) {
  const [busy, setBusy] = useState(false);
  const replies = summary.replies ?? [];
  if (summary.answered && replies.length === 0)
    return (
      <p data-testid="answered" className={cn("text-xs text-muted-foreground", className)}>
        → {summary.answered}
      </p>
    );
  if (replies.length === 0 || typeof summary.seq !== "number") return null;
  async function press(n: number) {
    setBusy(true);
    try {
      const res = await reply(summary.session, summary.seq!, n);
      // The ping as it now is; the stream brings the same, maybe later.
      if (res.summary) live.dispatch({ type: "event", id: null, event: { kind: "summary", data: res.summary } });
      toast(res.say);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Не получилось.", "error");
    } finally {
      setBusy(false);
    }
  }
  return (
    <fieldset aria-label="Ответы" className={cn("m-0 flex min-w-0 flex-wrap gap-1.5 border-0 p-0 pointer-coarse:gap-2", className)}>
      {replies.map((r, n) => (
        <button
          key={r}
          type="button"
          disabled={busy}
          onClick={() => press(n)}
          className={cn(
            "hit inline-flex min-h-7 max-w-full items-center rounded-full border px-3 py-1 text-left text-xs leading-snug transition-colors disabled:opacity-50",
            n === 0 ? "border-primary/40 bg-primary/10 text-foreground hover:bg-primary/20" : "hover:bg-accent",
          )}
        >
          {r}
        </button>
      ))}
    </fieldset>
  );
}
