// Talking to Retinue by voice and by text (docs/face.md «Talk to Retinue»):
// - `DictateButton`: one press records, the next transcribes; the words go where its owner puts them (an Operator
//   card's answer, the chat's box). Through the platform's speech (lib/voice.ts), dictation only.
// - `HeaderVoice` (the app's header): a phrase «1 — the recommended one, 2 — yes» answers the operator's cards by
//   their numbers — shown first, sent on «Send»; any other phrase opens the chat with it in the box.
// - `ChatToggle` and `ChatPane`: the assistant's conversation beside every screen — the same one as in Telegram — a
//   column at the right on a wide screen, a sheet on a phone, open or closed as the device left it.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ComposerBox } from "@/components/composer";
import { Close, FileIcon, MessageCircle, Mic, Stop } from "@/components/icons";
import { InputBubble } from "@/components/transcript/input";
import { Markdown } from "@/components/transcript/item";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ApiError, api } from "@/lib/api";
import { sizeWord } from "@/lib/attach";
import { type ChatRow, paths } from "@/lib/contract";
import { feedSource, useFeed } from "@/lib/feed";
import { live } from "@/lib/live";
import { keptNumbers, renumber } from "@/lib/operator";
import {
  chatRows,
  chatWith,
  loadChat,
  parseAnswers,
  pressChat,
  type Reach,
  reach,
  setChatOpen,
  takeDraft,
  takeRow,
  useChat,
  useChatDraft,
  useChatOpen,
} from "@/lib/talk";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { clock, UNAVAILABLE, useVoice } from "@/lib/voice";
import { requests, waitingKeys } from "@/lib/waiting";

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
      const rows = requests(feed);
      const numbers = renumber(keptNumbers(), waitingKeys(feed), Date.now() / 1000);
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

/** Where a message of his came from, when not from here. */
const VIA: Record<string, string> = { telegram: "In Telegram", matrix: "In Matrix" };

function OwnerRow({ row }: { row: ChatRow }) {
  const note = row.pending ? "Sending…" : row.via ? (VIA[row.via] ?? `In ${row.via}`) : "";
  return (
    <div className="flex flex-col items-end gap-1">
      <InputBubble input={{ session: "", input_id: row.id, text: row.text, at: row.at, turn: null, n: null, attachments: [] }} />
      {note && <span className="text-xs text-muted-foreground/70">{note}</span>}
    </div>
  );
}

function Files({ row }: { row: ChatRow }) {
  const files = row.files ?? [];
  if (!files.length) return null;
  return (
    <ul aria-label="Files" className="flex flex-col gap-1.5">
      {files.map((f, k) => (
        <li key={`${row.id}/${k}`} className="min-w-0">
          {f.id ? (
            <a href={paths.chatFile(f.id)} download={f.name} className="flex min-h-9 max-w-full items-center gap-2 rounded-lg border px-2.5 py-1.5 text-[13px] transition-colors hover:bg-accent max-md:min-h-11">
              <FileIcon className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 truncate font-mono">{f.name}</span>
              <span className="ml-auto shrink-0 font-mono text-xs text-muted-foreground tabular-nums">{sizeWord(f.size)}</span>
            </a>
          ) : (
            <span className="flex max-w-full items-center gap-2 px-2.5 py-1.5 text-[13px] text-muted-foreground">
              <FileIcon className="size-4 shrink-0" />
              <span className="min-w-0 truncate font-mono">{f.name}</span>
              <span className="shrink-0 text-xs">{f.why}</span>
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

/** The system's own word (a reminder, a refusal, a card): quieter than hers, its buttons under it. */
function Notice({ row }: { row: ChatRow }) {
  const [busy, setBusy] = useState<string | null>(null);
  const buttons = row.buttons ?? [];
  // the row came back rewritten: the press is over
  // biome-ignore lint/correctness/useExhaustiveDependencies: the row's rev is the signal
  useEffect(() => setBusy(null), [row.rev]);
  return (
    <div data-testid="chat-notice" className="flex min-w-0 flex-col gap-2 rounded-lg border border-dashed px-3 py-2.5 text-muted-foreground [&_.markdown]:text-[13px]">
      <Markdown text={row.text} />
      {buttons.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {buttons.map(([label, id]) => (
            <Button
              key={id}
              size="sm"
              variant="outline"
              className="max-md:h-11"
              disabled={busy !== null}
              onClick={() => {
                setBusy(id);
                void pressChat(row.id, id).then((ok) => ok || setBusy(null));
              }}
            >
              {label}
            </Button>
          ))}
        </div>
      )}
    </div>
  );
}

function Row({ row }: { row: ChatRow }) {
  if (row.by === "owner") return <OwnerRow row={row} />;
  if (row.by === "notice") return <Notice row={row} />;
  return (
    <div data-testid="chat-her" className="flex min-w-0 flex-col gap-2">
      <Markdown text={row.text} />
      <Files row={row} />
    </div>
  );
}

/** The assistant's conversation as a chat: the rows, what she is writing, then the box with the microphone. */
function Chat() {
  const chat = useChat();
  const rows = useMemo(() => chatRows(chat), [chat]);
  const draft = useChatDraft();
  useEffect(() => {
    void loadChat();
    const off = [live.onChat(takeRow), live.onChatDraft(takeDraft), live.onResync(() => void loadChat())];
    return () => {
      for (const f of off) f();
    };
  }, []);
  // stay at the bottom while new words come, unless the owner scrolled up
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });
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
        {!chat.loaded ? (
          <p className="text-sm text-muted-foreground">{chat.failed ?? "Loading…"}</p>
        ) : !rows.length && !chat.draft ? (
          <p className="text-sm text-muted-foreground">Ask about anything, in any project. This is the same conversation as with the bot in Telegram.</p>
        ) : (
          <div role="log" aria-label="The conversation" className="flex flex-col gap-4">
            {rows.map((row) => (
              <Row key={row.id} row={row} />
            ))}
            {chat.draft && (
              <div data-testid="chat-draft" className="flex min-w-0 flex-col gap-1.5">
                {chat.draft.text ? (
                  <Markdown text={chat.draft.text} streaming />
                ) : (
                  <span role="status" className="inline-flex items-center gap-2 text-[13px] text-muted-foreground">
                    <span aria-hidden className="size-1.5 animate-pulse rounded-full bg-primary" />
                    Writing…
                  </span>
                )}
              </div>
            )}
          </div>
        )}
      </div>
      <div className="shrink-0 px-3 pt-1 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
        <ComposerBox
          key={`chat:${draft.n}`}
          owner="chat"
          label="Message to Retinue"
          placeholder="Ask about anything, any project"
          initial={draft.text}
          autoFocus={draft.n > 0}
          compact
          noFiles="Files go to Retinue through the bot in Telegram for now."
          send={async (text, inputId) => {
            stick.current = true;
            const { row } = await api.chatSay(text, inputId);
            takeRow({ ...row, rev: row.rev ?? 0 });
            return undefined;
          }}
        />
      </div>
    </div>
  );
}

function ChatHead({ onClose }: { onClose: () => void }) {
  return (
    <header className="flex min-h-12 shrink-0 items-center gap-2 border-b pr-1 pl-4">
      <MessageCircle className="size-4 shrink-0 text-muted-foreground" />
      <h2 className="text-[15px] font-medium">Retinue</h2>
      <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">your assistant · also in Telegram</span>
      <Button variant="ghost" size="icon" aria-label="Close the chat" title="Close" onClick={onClose} className="size-8 md:size-8">
        <Close />
      </Button>
    </header>
  );
}

/** The chat as the right column of a wide screen. */
export function ChatPane() {
  return (
    <aside aria-label="Talk to Retinue" className="flex h-full w-full flex-col border-l bg-sidebar">
      <ChatHead onClose={() => setChatOpen(false)} />
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
