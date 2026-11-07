// A session's row opened whole: its top line (where from, how long, the way to the session) and the box whose words go
// into it as the owner's next message — the same in the inbox's pane and on the operator's cards.
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { ArrowUpRight } from "@/components/icons";
import { DictateButton } from "@/components/talk";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import type { FeedRow } from "@/lib/feed";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { insertAt } from "@/lib/voice";

const OPEN_LINK = "inline-flex shrink-0 items-center gap-1 text-xs text-muted-foreground hover:text-foreground [&_svg]:size-3.5";

/** The pane's top line: where it is from and how long, and the way to its session. */
export function Crumb({ children, session }: { children: React.ReactNode; session?: string | null }) {
  return (
    <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
      <span className="min-w-0 truncate">{children}</span>
      {session && (
        <Link to="/s/$id" params={{ id: session }} className={cn(OPEN_LINK, "ml-auto")}>
          Open the session
          <ArrowUpRight />
        </Link>
      )}
    </div>
  );
}

export type Deliver = (words: string) => Promise<unknown>;

/** Where the owner's words for a row go: a room session's next message, or the answer one of his computers takes on its
 * next report and puts into its session there. */
export function deliverTo(row: FeedRow): Deliver | null {
  if (row.computer) {
    const { device, id } = row.computer;
    return (words) => api.answerComputer(device, id, words);
  }
  const session = row.session;
  return session ? (words) => api.input(session, words) : null;
}

/** A session's text box: what the owner writes goes into it as his next message (`onSent` told the words) — by
 * `deliver` when it is not a room session's (`session` then only names the box). */
export function Reply({
  session,
  placeholder,
  onSent,
  deliver,
}: {
  session: string;
  placeholder: string;
  onSent?: (words: string) => void;
  deliver?: Deliver;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  async function send() {
    const words = text.trim();
    if (!words || busy) return;
    setBusy(true);
    try {
      await (deliver ? deliver(words) : api.input(session, words));
      setText("");
      toast("Sent");
      onSent?.(words);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-start gap-2">
        <Textarea
          aria-label="Your message"
          placeholder={placeholder}
          rows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void send();
          }}
          className="min-w-0 flex-1 resize-none"
        />
        {/* the answer by voice: dictated into the field, to read and send */}
        <DictateButton owner={`reply:${session}`} label="Dictate the answer" onText={(piece) => setText((now) => insertAt(now, now.length, now.length, piece).text)} />
      </div>
      <div>
        <Button disabled={busy || !text.trim()} onClick={() => void send()}>
          {busy ? "Sending…" : "Send"}
        </Button>
      </div>
    </div>
  );
}
