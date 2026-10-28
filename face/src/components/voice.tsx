// The owner's voice in the session's composer and in «＋» (lib/voice.ts): 🎙 with «EN» beside it while idle; while
// recording, the time, a level meter, «Отмена» and the stop button in place of the field; «Расшифровываю…» after.
import { Mic, Stop } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { clock, MAX_MS, UNAVAILABLE, type Voice } from "@/lib/voice";

/** 🎙 and «EN». Without a microphone here the button stays tappable and says why, rather than vanish. */
export function MicButtons({ voice, disabled = false, compact = false }: { voice: Voice; disabled?: boolean; compact?: boolean }) {
  const label = voice.unavailable ? UNAVAILABLE : voice.en ? "Сказать по-английски" : "Диктовать";
  return (
    <span className="flex shrink-0 items-center">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label={label}
        title={label}
        disabled={disabled || voice.state !== "idle"}
        onClick={voice.toggle}
        className={cn("rounded-full", voice.unavailable && "opacity-50")}
      >
        <Mic />
      </Button>
      <button
        type="button"
        aria-pressed={voice.en}
        aria-label="EN"
        title={voice.en ? "Английский дословно: текст уйдёт сразу" : "По-английски дословно — сразу в сессию"}
        disabled={disabled || voice.state !== "idle"}
        onClick={() => voice.setEn(!voice.en)}
        className={cn(
          "inline-flex h-7 items-center rounded-full border px-1.5 text-[11px] font-semibold tracking-wide disabled:opacity-50",
          voice.en ? "border-primary bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent",
          compact && "-ml-1",
        )}
      >
        EN
      </button>
    </span>
  );
}

/** While asking for the microphone, recording or sending: what stands where the field was, «Отмена» always in it. */
export function RecordingBar({ voice }: { voice: Voice }) {
  const sending = voice.state === "sending";
  const starting = voice.state === "starting";
  return (
    <div
      role="status"
      aria-label={sending ? "Расшифровываю" : starting ? "Жду микрофон" : "Идёт запись"}
      className="flex min-h-11 flex-1 items-center gap-3 rounded-2xl border bg-muted/40 px-3"
    >
      {sending || starting ? (
        <span className="flex-1 text-sm text-muted-foreground">{sending ? "Расшифровываю…" : "Жду микрофон…"}</span>
      ) : (
        <>
          <span className="size-2.5 shrink-0 animate-pulse rounded-full bg-destructive" aria-hidden="true" />
          <span className="shrink-0 font-mono text-sm tabular-nums" data-testid="voice-clock">
            {clock(voice.elapsed)} / {clock(MAX_MS)}
          </span>
          {voice.en && <span className="shrink-0 rounded-full border px-1.5 text-[11px] font-semibold">EN</span>}
          <span className="h-1.5 min-w-8 flex-1 overflow-hidden rounded-full bg-muted" aria-hidden="true">
            <span
              data-testid="voice-level"
              className="block h-full rounded-full bg-primary transition-[width] duration-75"
              style={{ width: `${Math.round(voice.level * 100)}%` }}
            />
          </span>
        </>
      )}
      <Button type="button" variant="ghost" size="sm" onClick={voice.cancel}>
        Отмена
      </Button>
      {voice.state === "recording" && (
        <Button
          type="button"
          size="icon"
          aria-label="Остановить и расшифровать"
          title="Остановить и расшифровать"
          onClick={voice.toggle}
          className="rounded-full"
        >
          <Stop />
        </Button>
      )}
    </div>
  );
}
