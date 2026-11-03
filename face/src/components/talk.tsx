// Talking to Retinue by voice and by text (docs/face.md «Talk to Retinue»):
// - `DictateButton`: one press records, the next transcribes; the words go where its owner puts them (an Operator
//   card's answer, the chat's box). Through the platform's speech (lib/voice.ts), dictation only.
// - `HeaderVoice` (the app's header): a phrase «1 — the recommended one, 2 — yes» answers the operator's cards by
//   their numbers — shown first, sent on «Send»; any other phrase opens the chat with it in the box.
// - `ChatToggle` and `ChatPane`: the voice's main session beside every screen — a column at the right on a wide
//   screen, a sheet on a phone —, open or closed as the device left it.
import { Link } from "@tanstack/react-router";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ComposerBox } from "@/components/composer";
import { ArrowUpRight, Close, MessageCircle, Mic, Stop } from "@/components/icons";
import { Transcript } from "@/components/transcript/transcript";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ApiError, api } from "@/lib/api";
import { feedSource, useFeed } from "@/lib/feed";
import { live, useLiveState } from "@/lib/live";
import { keptNumbers, renumber, waits } from "@/lib/operator";
import { approvalsOf, listed, orderedInputs, orderedItems, orderedSummaries, proposalsOf } from "@/lib/store";
import { chatWith, parseAnswers, type Reach, reach, setChatOpen, useChatDraft, useChatOpen, voiceSession } from "@/lib/talk";
import { sessionRequests } from "@/lib/today";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { clock, UNAVAILABLE, useVoice } from "@/lib/voice";

/** The microphone beside an answer: dictation into it. While it records: the time, «Stop» and «Cancel» in its place. */
export function DictateButton({
  onText,
  owner,
  label = "Dictate",
  className,
}: {
  onText: (text: string) => void;
  /** Whom the words are for: a new one drops the recording. */
  owner: string;
  label?: string;
  className?: string;
}) {
  const voice = useVoice({ configured: live.meta?.speech !== false, owner, onText, onMessage: () => undefined });
  if (voice.state !== "idle")
    return (
      <span
        role="status"
        aria-label={voice.state === "sending" ? "Transcribing" : voice.state === "starting" ? "Waiting for the microphone" : "Recording"}
        className={cn("inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full border pr-1 pl-2.5 max-md:h-11", className)}
      >
        {voice.state === "recording" && <span aria-hidden className="size-2 animate-pulse rounded-full bg-destructive" />}
        <span className="font-mono text-xs text-muted-foreground tabular-nums">{voice.state === "recording" ? clock(voice.elapsed) : "…"}</span>
        {voice.state === "recording" && (
          <button type="button" aria-label="Stop and transcribe" title="Stop and transcribe" onClick={voice.toggle} className="hit inline-flex size-7 items-center justify-center rounded-full hover:bg-accent">
            <Stop className="size-3.5" />
          </button>
        )}
        <button type="button" aria-label="Cancel the recording" title="Cancel" onClick={voice.cancel} className="hit inline-flex size-7 items-center justify-center rounded-full text-muted-foreground hover:bg-accent">
          <Close className="size-3.5" />
        </button>
      </span>
    );
  return (
    <Button
      type="button"
      variant="outline"
      size="icon"
      aria-label={label}
      title={voice.unavailable ? UNAVAILABLE : label}
      onClick={voice.toggle}
      className={cn("shrink-0 rounded-full", voice.unavailable && "opacity-50", className)}
    >
      <Mic />
    </Button>
  );
}

// --- the header's microphone ------------------------------------------------------------------------------------

/** What a numbered phrase will do, card by card, and one «Send» for all that can be sent. */
function Answers({ answers, onClose }: { answers: Reach[]; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const sendable = answers.filter((a) => a.how === "send");
  async function send() {
    setBusy(true);
    let sent = 0;
    for (const a of sendable) {
      try {
        await api.input(a.row!.session!, a.words);
        sent += 1;
      } catch (e) {
        toast(e instanceof ApiError ? `#${a.n}: ${e.message}` : `#${a.n}: not sent.`, "error");
      }
    }
    setBusy(false);
    if (sent) toast(`Sent ${sent} ${sent === 1 ? "answer" : "answers"}`);
    void feedSource.refresh();
    onClose();
  }
  return (
    <Dialog open onClose={onClose} title="Answer the cards">
      <ul className="flex flex-col divide-y rounded-lg border" aria-label="Answers">
        {answers.map((a) => (
          <li key={`${a.n}:${a.words}`} className="flex flex-col gap-0.5 px-3 py-2">
            <span className="flex min-w-0 items-baseline gap-2 text-xs text-muted-foreground">
              <span className="font-mono font-medium text-foreground tabular-nums">#{a.n}</span>
              <span className="truncate">{a.row ? a.row.title : "no card has this number now"}</span>
            </span>
            <span className={cn("text-sm break-words", a.how !== "send" && "text-muted-foreground")}>
              «{a.words}»{a.how === "card" ? " — decide it on its card, by its buttons" : ""}
            </span>
          </li>
        ))}
      </ul>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button disabled={busy || !sendable.length} onClick={() => void send()}>
          {busy ? "Sending…" : sendable.length === 1 ? "Send 1 answer" : `Send ${sendable.length} answers`}
        </Button>
      </div>
    </Dialog>
  );
}

/** The header's microphone: one press to speak, one to stop. «1 — the recommended one, 2 — yes» answers the operator's
 * cards by their numbers (shown first, sent on «Send»); anything else opens the chat with the words in its box. */
export function HeaderVoice() {
  const feed = useFeed();
  const [answers, setAnswers] = useState<Reach[] | null>(null);
  const voice = useVoice({
    configured: live.meta?.speech !== false,
    owner: "header",
    onMessage: () => undefined,
    onText: (text) => {
      const numbered = parseAnswers(text);
      // the numbers «Today», a project's «Operator» and the map show: kept on the device, given now to a card none
      // of them has drawn yet
      const rows = sessionRequests(feed);
      const numbers = renumber(keptNumbers(), rows.filter(waits).map((r) => r.key), Date.now() / 1000);
      if (numbered.length) setAnswers(reach(numbered, numbers, rows));
      else chatWith(text);
    },
  });
  const recording = voice.state !== "idle";
  const label = "Speak: «1 — yes, 2 — the second one» answers the cards; anything else goes to Retinue";
  return (
    <>
      {recording ? (
        <span role="status" aria-label={voice.state === "sending" ? "Transcribing" : "Recording"} className="inline-flex h-8 items-center gap-1 rounded-md border pr-0.5 pl-2">
          {voice.state === "recording" && <span aria-hidden className="size-2 animate-pulse rounded-full bg-destructive" />}
          <span className="font-mono text-xs text-muted-foreground tabular-nums">{voice.state === "recording" ? clock(voice.elapsed) : "…"}</span>
          {voice.state === "recording" && (
            <button type="button" aria-label="Stop and transcribe" title="Stop and transcribe" onClick={voice.toggle} className="hit inline-flex size-7 items-center justify-center rounded hover:bg-accent">
              <Stop className="size-3.5" />
            </button>
          )}
          <button type="button" aria-label="Cancel the recording" title="Cancel" onClick={voice.cancel} className="hit inline-flex size-7 items-center justify-center rounded text-muted-foreground hover:bg-accent">
            <Close className="size-3.5" />
          </button>
        </span>
      ) : (
        <Button size="icon" variant="ghost" aria-label="Speak" title={voice.unavailable ? UNAVAILABLE : label} onClick={voice.toggle} className={cn(voice.unavailable && "opacity-50")}>
          <Mic />
        </Button>
      )}
      {answers && <Answers answers={answers} onClose={() => setAnswers(null)} />}
    </>
  );
}

/** «Talk to Retinue»: opens and closes the chat; the device keeps which. */
export function ChatToggle() {
  const open = useChatOpen();
  return (
    <Button size="icon" variant="ghost" aria-label="Talk to Retinue" title="Talk to Retinue" aria-pressed={open} onClick={() => setChatOpen(!open)} className={cn(open && "bg-accent")}>
      <MessageCircle />
    </Button>
  );
}

// --- the chat -------------------------------------------------------------------------------------------------------

/** The voice's main session as a chat: its conversation, then its box with the microphone. No voice on this platform:
 * says so. */
function Chat() {
  const state = useLiveState();
  const sphere = live.meta?.voice;
  const main = useMemo(() => voiceSession(listed(state.sessions), sphere), [state.sessions, sphere]);
  const [started, setStarted] = useState<string | null>(null);
  const id = main?.id ?? started ?? "";
  const draft = useChatDraft();
  const loaded = !!id && id in state.floors;
  const itemsOf = id ? state.items[id] : undefined;
  const items = useMemo(() => orderedItems(itemsOf), [itemsOf]);
  const approvals = useMemo(() => (id ? approvalsOf(state.approvals, id) : []), [state.approvals, id]);
  const proposals = useMemo(() => (id ? proposalsOf(state.proposals, id) : []), [state.proposals, id]);
  const summaries = useMemo(() => orderedSummaries(id ? state.summaries[id] : undefined), [state.summaries, id]);
  const inputs = useMemo(() => orderedInputs(id ? state.inputs[id] : undefined), [state.inputs, id]);
  const running = id ? state.sessions[id]?.state === "active" : false;
  useEffect(() => {
    if (id && !live.isWatched(id)) live.watch(id).catch(() => undefined);
  }, [id]);
  // stay at the bottom while new words come, unless the owner scrolled up
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });
  if (!sphere)
    return (
      <div className="flex flex-col gap-2 p-4 text-sm">
        <p className="font-medium">The voice is not configured</p>
        <p className="text-muted-foreground">
          Retinue talks here once the platform has a voice: a <code className="font-mono text-xs">voice:</code> section in platform.yaml
          (docs/proactive.md, «The voice»). Until then, write to a session or to the bot in Telegram.
        </p>
      </div>
    );
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-3"
      >
        {!id ? (
          <p className="text-sm text-muted-foreground">Ask about anything, in any project: Retinue sees them all and answers here.</p>
        ) : !loaded ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : (
          <Transcript items={items} approvals={approvals} delta={state.deltas[id]} running={running} summaries={summaries} inputs={inputs} proposals={proposals} />
        )}
      </div>
      <div className="shrink-0 px-3 pt-1 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
        <ComposerBox
          key={`chat:${draft.n}`}
          owner={`chat:${id}`}
          label="Message to Retinue"
          placeholder="Talk to Retinue — about anything, any project"
          initial={draft.text}
          autoFocus={draft.n > 0}
          compact
          running={running}
          onStop={id ? () => void api.stop(id).catch(() => undefined) : undefined}
          send={async (text, inputId, files) => {
            if (id) return api.input(id, text, inputId, files);
            const made = await api.toMain(sphere, text, inputId, files);
            setStarted(made.session);
            return undefined;
          }}
        />
      </div>
    </div>
  );
}

function ChatHead({ id, onClose }: { id?: string; onClose: () => void }) {
  return (
    <header className="flex min-h-12 shrink-0 items-center gap-2 border-b pr-1 pl-4">
      <MessageCircle className="size-4 shrink-0 text-muted-foreground" />
      <h2 className="text-[15px] font-medium">Retinue</h2>
      <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">the voice · sees every project</span>
      {id && (
        <Link to="/s/$id" params={{ id }} title="Open its session" aria-label="Open its session" className="hit inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground">
          <ArrowUpRight className="size-4" />
        </Link>
      )}
      <Button variant="ghost" size="icon" aria-label="Close the chat" title="Close" onClick={onClose} className="size-8 md:size-8">
        <Close />
      </Button>
    </header>
  );
}

/** The chat as the right column of a wide screen. */
export function ChatPane() {
  const state = useLiveState();
  const main = voiceSession(listed(state.sessions), live.meta?.voice);
  return (
    <aside aria-label="Talk to Retinue" className="flex h-full w-full flex-col border-l bg-sidebar">
      <ChatHead id={main?.id} onClose={() => setChatOpen(false)} />
      <Chat />
    </aside>
  );
}

/** The chat as a sheet over a phone's screen. */
export function ChatSheet() {
  const open = useChatOpen();
  return (
    <Dialog open={open} onClose={() => setChatOpen(false)} title="Talk to Retinue" sheet>
      <div className="flex h-[70dvh] flex-col">
        <Chat />
      </div>
    </Dialog>
  );
}
