// The one input box of the app: a new task (`/new`, pages/new.tsx) and a session's replies (its screen, the list's
// peek). Made after Claude's apps (docs/face.md «The composer»): a rounded box; the text on top, at least three lines on
// a phone, growing with what is typed to 40 % of the visible height, then scrolling inside; under it «+» on the left —
// a menu «Фото · Камера · Файл» (the camera on a touch screen) — and on the right 🎙 «EN» and the send arrow, greyed
// while there is nothing to send. What is attached shows as chips above the text, each with its ×, within the
// platform's limits (lib/attach.ts); a message may be files alone. Enter sends on a desktop (Shift+Enter: a new line);
// on a phone Enter is a new line and the arrow sends. A tap anywhere on the box puts the cursor in the text. While a
// turn runs, «Стоп» stands beside the arrow. 🎙 dictates into the text at the cursor; «EN» sends the English at once,
// word for word, with what the box holds, as one message (lib/voice.ts).
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowUp, Camera, Close, FileIcon, ImageIcon, Plus, Stop } from "@/components/icons";
import { Textarea } from "@/components/ui/textarea";
import { MicButtons, RecordingBar } from "@/components/voice";
import { ApiError, api } from "@/lib/api";
import { type Encoded, encodeFile, overLimits, sizeWord } from "@/lib/attach";
import { live } from "@/lib/live";
import { useCoarsePointer } from "@/lib/media";
import { toast } from "@/lib/toast";
import { cn, inputId } from "@/lib/utils";
import { insertAt, useVoice } from "@/lib/voice";

/** How tall the text may grow before it scrolls inside: this share of the visible height (the keyboard taken off). */
export const GROW_TO = 0.4;

/** The visible height: the visual viewport's, which shrinks under the keyboard on iOS and in Telegram. */
function visibleHeight(): number {
  return window.visualViewport?.height ?? window.innerHeight;
}

function useVisibleHeight(): number {
  const [height, setHeight] = useState(visibleHeight);
  useEffect(() => {
    const update = () => setHeight(visibleHeight());
    const viewport = window.visualViewport;
    viewport?.addEventListener("resize", update);
    window.addEventListener("resize", update);
    return () => {
      viewport?.removeEventListener("resize", update);
      window.removeEventListener("resize", update);
    };
  }, []);
  return height;
}

/** The field as tall as its text, up to `max` px, then it scrolls. Done by hand: `field-sizing: content` is not in
 * Safari, and the cap follows the keyboard. A field not laid out (`scrollHeight` 0) keeps its CSS height. */
export function fitField(el: HTMLTextAreaElement, max: number) {
  el.style.height = "auto";
  const want = el.scrollHeight;
  if (!want) {
    el.style.height = "";
    return;
  }
  el.style.height = `${Math.min(want, max)}px`;
  el.style.overflowY = want > max ? "auto" : "hidden";
}

/** A round control whose tap area is 44 px whatever its face (`face`: the visible circle's classes, 40 px on a phone,
 * 36 on a desktop) — Apple's minimum hit target. */
function RoundButton({
  face,
  children,
  className,
  type = "button",
  ...props
}: React.ComponentProps<"button"> & { face: string }) {
  return (
    <button
      type={type}
      className={cn("group inline-flex size-11 shrink-0 items-center justify-center rounded-full outline-none disabled:pointer-events-none", className)}
      {...props}
    >
      <span
        className={cn(
          "inline-flex size-10 items-center justify-center rounded-full transition-colors group-focus-visible:ring-[3px] group-focus-visible:ring-ring/50 md:size-9 [&_svg]:size-4",
          face,
        )}
      >
        {children}
      </span>
    </button>
  );
}

/** «+»: one button, one menu — as Claude, ChatGPT and Gemini have it. «Камера» opens the camera itself, on a touch
 * screen only (a desktop has no `capture`). */
function AttachMenu({ onFiles, disabled, camera }: { onFiles: (files: File[]) => void; disabled: boolean; camera: boolean }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const photo = useRef<HTMLInputElement>(null);
  const shot = useRef<HTMLInputElement>(null);
  const file = useRef<HTMLInputElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  /** Closed: by Esc, a pick or Tab the focus goes back to «+»; by a tap beside, it stays where the tap put it. */
  function close(refocus: boolean) {
    setOpen(false);
    if (refocus) trigger.current?.focus();
  }
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLElement>("[role=menuitem]")?.focus(); // as a menu button does (WAI-ARIA APG)
    const away = (e: PointerEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  /** ↑ ↓ Home End move between the items, round; Tab leaves the menu closed. */
  function onMenuKey(e: React.KeyboardEvent) {
    const all = [...(menu.current?.querySelectorAll<HTMLElement>("[role=menuitem]") ?? [])];
    const at = all.indexOf(document.activeElement as HTMLElement);
    const to =
      e.key === "ArrowDown" ? (at + 1) % all.length
        : e.key === "ArrowUp" ? (at - 1 + all.length) % all.length
          : e.key === "Home" ? 0
            : e.key === "End" ? all.length - 1
              : null;
    if (to !== null) {
      e.preventDefault();
      all[to]?.focus();
    } else if (e.key === "Tab") close(false);
  }
  const items = [
    { input: photo, label: "Фото", Icon: ImageIcon, accept: "image/*", testid: "attach-photo", capture: undefined },
    ...(camera ? [{ input: shot, label: "Камера", Icon: Camera, accept: "image/*", testid: "attach-camera", capture: "environment" as const }] : []),
    { input: file, label: "Файл", Icon: FileIcon, accept: undefined, testid: "attach-input", capture: undefined },
  ];
  return (
    <div ref={box} className="relative">
      {items.map(({ input, accept, testid, capture }) => (
        <input
          key={testid}
          ref={input}
          type="file"
          multiple={!capture}
          hidden
          accept={accept}
          capture={capture}
          data-testid={testid}
          onChange={(e) => {
            onFiles([...(e.target.files ?? [])]);
            e.target.value = "";
          }}
        />
      ))}
      <RoundButton
        ref={trigger}
        aria-label="Прикрепить"
        title="Фото, камера или файл"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen(!open)}
        face="border hover:bg-accent"
      >
        <Plus />
      </RoundButton>
      {open && (
        <div
          ref={menu}
          role="menu"
          aria-label="Прикрепить"
          onKeyDown={onMenuKey}
          className="absolute bottom-full left-0 z-30 mb-2 flex min-w-44 flex-col rounded-2xl border bg-background p-1 shadow-lg"
        >
          {items.map(({ input, label, Icon }) => (
            <button
              key={label}
              type="button"
              role="menuitem"
              tabIndex={-1}
              onClick={() => {
                close(true);
                input.current?.click();
              }}
              className="flex min-h-11 items-center gap-3 rounded-xl px-3 text-left text-[15px] hover:bg-accent md:min-h-9 md:text-sm"
            >
              <Icon className="size-5 text-muted-foreground md:size-4" />
              {label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Chips({ files, onRemove }: { files: File[]; onRemove: (k: number) => void }) {
  if (files.length === 0) return null;
  return (
    <ul className="flex gap-1.5 overflow-x-auto px-3 pt-3" aria-label="Вложения">
      {files.map((f, k) => {
        const Icon = f.type.startsWith("image/") ? ImageIcon : FileIcon;
        return (
          <li key={`${f.name}/${k}`} className="flex max-w-56 shrink-0 items-center gap-1.5 rounded-xl border bg-muted/50 py-1 pr-1 pl-2.5 text-xs">
            <Icon className="size-3.5 shrink-0" />
            <span className="min-w-0 truncate">{f.name}</span>
            <span className="shrink-0 text-muted-foreground">{sizeWord(f.size)}</span>
            <button
              type="button"
              aria-label={`Убрать ${f.name}`}
              className="group -my-3 -mr-2.5 -ml-2 inline-flex size-11 shrink-0 items-center justify-center rounded-full"
              onClick={() => onRemove(k)}
            >
              <span className="inline-flex size-6 items-center justify-center rounded-full group-hover:bg-accent">
                <Close className="size-3.5" />
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** The id of a message: the one tried last when it is the same message again (a retry), else a new one. */
export function messageId(tried: { id: string; body: string; files: File[] } | null, body: string, files: File[]): string {
  const same = tried && tried.body === body && tried.files.length === files.length && tried.files.every((f, k) => f === files[k]);
  return same ? tried.id : inputId();
}

export interface ComposerBoxProps {
  /** Whom a recording is for: a new one drops it (lib/voice.ts) — the session's id, or `new`. */
  owner: string;
  /** The field's accessible name. */
  label: string;
  placeholder: string;
  /** One message out: its text, its id (kept across retries of the same message), its files. Throws when it did not
   * go; the platform's word, when there is one, comes back. */
  send: (text: string, id: string, files: Encoded[]) => Promise<{ say?: string } | undefined>;
  running?: boolean;
  onStop?: () => void;
  transcribe?: typeof api.speech;
  /** The list's peek: two lines to start with, not three. */
  compact?: boolean;
  autoFocus?: boolean;
  /** The text as it was when the screen was left (the new task keeps its draft). */
  initial?: string;
  onDraft?: (text: string) => void;
  /** Why nothing can be sent here now (no track for a new session yet): the arrow is off and says it. */
  blocked?: string | null;
}

export function ComposerBox({
  owner,
  label,
  placeholder,
  send,
  running = false,
  onStop,
  transcribe = api.speech,
  compact = false,
  autoFocus = false,
  initial = "",
  onDraft,
  blocked = null,
}: ComposerBoxProps) {
  const [text, setTextState] = useState(initial);
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  // One id per message, kept across retries of the same message so a retried POST is not a second message — and only of
  // the same: a failed send that did reach the platform must not swallow a different message sent after it.
  const tried = useRef<{ id: string; body: string; files: File[] } | null>(null);
  const coarse = useCoarsePointer();
  const height = useVisibleHeight();
  const field = useRef<HTMLTextAreaElement>(null);
  const cursor = useRef<{ start: number; end: number } | null>(null); // where the dictation goes, kept at its start

  function setText(next: string) {
    setTextState(next);
    onDraft?.(next);
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: the text is why it measures again
  useLayoutEffect(() => {
    if (field.current) fitField(field.current, Math.round(height * GROW_TO));
  }, [text, height]);

  /** The English transcript, put into the field when it could not be sent: never lost. */
  function keep(message: string, why: string) {
    setText(insertAt(text, text.length, text.length, message).text);
    toast(`${why} Запись — в поле: отправь её сам.`, "error", 8000);
  }

  /** What the box holds, `spoken` (an English transcript, word for word) after its text, as one message. Null when it
   * went, else why not. */
  async function submit(spoken?: string): Promise<string | null> {
    const body = [text.trim(), spoken ?? ""].filter(Boolean).join("\n\n");
    if (!body && files.length === 0) return null;
    if (busy) return "Предыдущее сообщение ещё отправляется.";
    if (blocked) return blocked;
    const id = messageId(tried.current, body, files);
    tried.current = { id, body, files };
    setBusy(true);
    try {
      const encoded = await Promise.all(files.map(encodeFile));
      const res = await send(body, id, encoded);
      setTextState("");
      onDraft?.("");
      setFiles([]);
      tried.current = null;
      if (res?.say && res.say !== "Принято.") toast(res.say);
      return null;
    } catch (e) {
      return e instanceof ApiError ? e.message : "Не отправилось — попробуй ещё раз.";
    } finally {
      setBusy(false);
    }
  }

  const voice = useVoice({
    configured: live.meta?.speech !== false,
    owner,
    transcribe,
    onStart: () => {
      const el = field.current;
      cursor.current = el ? { start: el.selectionStart, end: el.selectionEnd } : null;
    },
    onText: (piece) => {
      const at = cursor.current ?? { start: text.length, end: text.length };
      const next = insertAt(text, Math.min(at.start, text.length), Math.min(at.end, text.length), piece);
      setText(next.text);
      // the caret after the dictated words, once the field has them: the next dictation or the keyboard goes on there
      setTimeout(() => field.current?.setSelectionRange(next.caret, next.caret), 0);
    },
    onMessage: async (message) => {
      if (busy) return keep(message, "Предыдущее сообщение ещё отправляется.");
      const why = await submit(message);
      if (why) keep(message, why);
    },
  });
  const recording = voice.state !== "idle";

  function attach(added: File[]) {
    if (added.length === 0) return;
    const next = [...files, ...added];
    const why = overLimits(next);
    if (why) {
      toast(why, "error");
      return;
    }
    setFiles(next);
  }

  async function typed() {
    const why = await submit();
    if (why) toast(why, "error");
  }

  const empty = !text.trim() && files.length === 0;
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void typed();
      }}
    >
      {/* a bigger target for the field, which has its own keys */}
      <div
        data-testid="composer"
        onClick={(e) => {
          if (!(e.target as Element).closest("button, input, textarea, [role=menu]")) field.current?.focus();
        }}
        className="flex cursor-text flex-col rounded-3xl border bg-background shadow-sm transition-[border-color,box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/25"
      >
        <Chips
          files={files}
          onRemove={(k) => {
            setFiles(files.filter((_, j) => j !== k));
          }}
        />
        <Textarea
          ref={field}
          readOnly={recording} // the cursor the dictation goes to stays where it was
          aria-label={label}
          placeholder={placeholder}
          value={text}
          rows={compact ? 2 : 3}
          autoFocus={autoFocus}
          enterKeyHint={coarse ? "enter" : "send"}
          className={cn(
            "resize-none rounded-none border-0 bg-transparent px-4 pt-3 pb-1 text-base shadow-none focus-visible:ring-0 md:text-[15px]",
            compact ? "min-h-14" : "min-h-[5.5rem]",
          )}
          onChange={(e) => setText(e.target.value)}
          onPaste={(e) => {
            const pasted = [...e.clipboardData.files];
            if (pasted.length) {
              e.preventDefault();
              attach(pasted);
            }
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !coarse && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void typed();
            }
          }}
        />
        <div className="flex min-h-12 items-center gap-0.5 px-1.5 pb-1.5">
          {recording ? (
            <RecordingBar voice={voice} />
          ) : (
            <>
              <AttachMenu onFiles={attach} disabled={busy} camera={coarse} />
              <span className="flex-1" />
              <MicButtons voice={voice} disabled={busy} compact />
            </>
          )}
          {/* «Стоп» stays in reach while dictating: a turn that runs away is stopped without dropping the recording */}
          {running && onStop && (
            <RoundButton aria-label="Стоп" title="Стоп" onClick={onStop} face="border bg-background hover:bg-accent">
              <Stop />
            </RoundButton>
          )}
          {!recording && (
            <RoundButton
              type="submit"
              aria-label="Отправить"
              title={blocked ?? (coarse ? "Отправить" : "Отправить (Enter)")}
              disabled={empty || busy || !!blocked}
              face={cn(
                "[&_svg]:size-5",
                empty || busy || blocked ? "bg-muted-foreground/30 text-background" : "bg-primary text-primary-foreground hover:bg-primary/90",
              )}
            >
              <ArrowUp />
            </RoundButton>
          )}
        </div>
      </div>
    </form>
  );
}

interface Props {
  session: string;
  running: boolean;
  send?: typeof api.input;
  stop?: typeof api.stop;
  transcribe?: typeof api.speech;
  compact?: boolean;
}

/** The box under a session: its messages go into it. */
export function Composer({ session, running, send = api.input, stop = api.stop, transcribe = api.speech, compact }: Props) {
  async function onStop() {
    try {
      await stop(session);
      toast("Останавливаю.");
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Не остановилось.", "error");
    }
  }
  return (
    <ComposerBox
      key={session}
      owner={session}
      label="Сообщение"
      placeholder="Написать в сессию…"
      send={(text, id, files) => send(session, text, id, files)}
      running={running}
      onStop={onStop}
      transcribe={transcribe}
      compact={compact}
    />
  );
}
