// The owner's voice (docs/face.md «Голос»): a recording made by the browser and sent as it is to the platform, which
// has ElevenLabs Scribe turn it into text (`/api/speech`). Voice is input only: the answers stay text.
//
// Two modes. Dictation (the default): the text lands in the field at the cursor, to be edited and sent by hand.
// English («EN»): word for word, the «uh» and the slips kept, and sent at once as the session's message — the session
// answers it with its corrections. Tap to start, tap to stop; 10 minutes at most.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, speech } from "@/lib/api";
import { toast } from "@/lib/toast";

export type Mode = "dictate" | "en";
export type VoiceState = "idle" | "starting" | "recording" | "sending";

export const MAX_MS = 10 * 60 * 1000;
export const UNAVAILABLE = "Микрофон здесь недоступен — надиктуй голосовым боту или открой приложение с экрана «Домой»";
export const NOT_SET_UP = "Голос не настроен на сервере: нет ключа ElevenLabs или выхода к нему.";
export const AT_CAP = "Запись остановлена: 10 минут — предел. Расшифровываю.";
/** What the browser may record in, best first: Chrome and Firefox take WebM/Opus, Safari MP4/AAC. Scribe takes both
 * (probed). */
export const TYPES = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4;codecs=mp4a.40.2"];
const BITRATE = 64_000; // a voice needs no more; 10 minutes stay under 5 MB where the browser honours it

/** The first type this browser records in; "" when it names none (its own default), null with no MediaRecorder. */
export function pickType(
  recorder: { isTypeSupported?: (t: string) => boolean } | null | undefined = globalThis.MediaRecorder,
): string | null {
  if (!recorder) return null;
  return TYPES.find((t) => recorder.isTypeSupported?.(t)) ?? "";
}

/** A microphone can be asked for here at all: a secure page with getUserMedia and MediaRecorder. Telegram's web view
 * on iOS may have neither, or refuse when asked; then the answer is the bot's voice notes or the installed app. */
export function micAvailable(nav: Navigator | null | undefined = globalThis.navigator): boolean {
  return typeof nav?.mediaDevices?.getUserMedia === "function" && pickType() !== null;
}

/** `piece` put in place of text[start, end), with a space either side where words would touch; the caret after it. */
export function insertAt(text: string, start: number, end: number, piece: string): { text: string; caret: number } {
  const before = text.slice(0, start);
  const after = text.slice(end);
  const lead = before && !/\s$/.test(before) ? " " : "";
  const trail = after && !/^\s/.test(after) ? " " : "";
  const head = before + lead + piece;
  return { text: head + trail + after, caret: head.length };
}

/** 83 000 ms → «1:23». */
export function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export interface Voice {
  state: VoiceState;
  elapsed: number;
  /** 0…1, the loudness now; stays 0 where the browser has no Web Audio. */
  level: number;
  en: boolean;
  setEn: (on: boolean) => void;
  /** No microphone here, or it was refused: the button says so instead of recording. */
  unavailable: boolean;
  toggle: () => void;
  cancel: () => void;
}

interface Options {
  /** Dictation: the text, for the field. */
  onText: (text: string) => void;
  /** English: the message, to be sent now. */
  onMessage: (message: string) => void | Promise<void>;
  /** Called as recording starts: the field remembers where its cursor was. */
  onStart?: () => void;
  /** False when the platform said it cannot hear (`meta.speech`): nothing is recorded in vain. */
  configured?: boolean;
  /** Whether the UI that shows the recording is on screen — «＋» open. Going false drops the recording. */
  active?: boolean;
  /** Whom the result is for — the session. A new one drops the recording: a result reaches only its own. */
  owner?: string;
  transcribe?: typeof speech;
}

interface Run {
  token: number;
  recorder: MediaRecorder;
  stream: MediaStream;
  chunks: Blob[];
  mode: Mode;
  timer?: ReturnType<typeof setInterval>;
  audio?: AudioContext;
  frame?: number;
  abort: AbortController;
}

/** Everything a recording holds on to — the microphone, the meter, the clock — let go. */
function release(r: { stream: MediaStream; timer?: ReturnType<typeof setInterval>; audio?: AudioContext; frame?: number }) {
  if (r.timer !== undefined) clearInterval(r.timer);
  if (r.frame !== undefined) cancelAnimationFrame(r.frame);
  for (const track of r.stream.getTracks()) track.stop();
  void r.audio?.close().catch(() => undefined);
}

/** The recording's whole life: idle → starting (the permission prompt) → recording → sending → idle. «Отмена» from
 * any of them, the screen going away, «＋» closing or another session goes straight back: the microphone is let go,
 * nothing is sent, nothing is kept. Each recording has a token; whatever comes back after an await is taken only
 * while its token is the current one — so a result reaches only the screen and session that started it, while they
 * still show it. The audio lives in memory only. */
export function useVoice({
  onText,
  onMessage,
  onStart,
  configured = true,
  active = true,
  owner = "",
  transcribe = speech,
}: Options): Voice {
  const [state, setState] = useState<VoiceState>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [en, setEn] = useState(false);
  const [unavailable, setUnavailable] = useState(() => !micAvailable());
  const run = useRef<Run | null>(null);
  const token = useRef(0); // the current recording's; a cancel moves it on, so nothing older is taken
  const handlers = useRef({ onText, onMessage, onStart });
  handlers.current = { onText, onMessage, onStart };

  const cancel = useCallback(() => {
    token.current += 1;
    const r = run.current;
    run.current = null;
    setState("idle");
    setLevel(0);
    if (!r) return; // still at the permission prompt: start() sees the token moved and lets the microphone go
    r.chunks = [];
    r.abort.abort();
    if (r.recorder.state !== "inactive") {
      try {
        r.recorder.stop();
      } catch {
        // already stopped: released below anyway
      }
    }
    release(r);
  }, []);

  const send = useCallback(
    async (r: Run) => {
      const audio = new Blob(r.chunks, { type: r.recorder.mimeType || r.chunks[0]?.type || "audio/webm" });
      r.chunks = [];
      if (audio.size === 0) {
        toast("Запись пустая — попробуй ещё раз.", "error");
        if (token.current === r.token) setState("idle");
        return;
      }
      try {
        const answer = await transcribe(audio, r.mode, r.abort.signal);
        if (token.current !== r.token) return;
        if (r.mode === "en") await handlers.current.onMessage(answer.message ?? `(голосом, дословно) ${answer.text}`);
        else handlers.current.onText(answer.text);
      } catch (e) {
        if (token.current === r.token) toast(e instanceof ApiError ? e.message : "Не удалось расшифровать — попробуй ещё раз.", "error");
      } finally {
        if (token.current === r.token) {
          run.current = null;
          setState("idle");
        }
      }
    },
    [transcribe],
  );

  const stop = useCallback(() => {
    const r = run.current;
    if (!r || r.recorder.state === "inactive") return;
    setState("sending");
    r.recorder.stop(); // its `stop` event sends what was recorded
  }, []);

  const start = useCallback(async () => {
    if (!configured) {
      toast(NOT_SET_UP, "error");
      return;
    }
    if (!micAvailable()) {
      setUnavailable(true);
      toast(UNAVAILABLE, "error", 8000);
      return;
    }
    const mode: Mode = en ? "en" : "dictate";
    const t = ++token.current;
    setState("starting");
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch {
      if (token.current !== t) return;
      // refused, no microphone, or a web view that never asks: the same way round it
      setUnavailable(true);
      setState("idle");
      toast(UNAVAILABLE, "error", 8000);
      return;
    }
    if (token.current !== t) {
      // cancelled, closed or gone while the prompt was up: the microphone it just gave is let go at once
      for (const track of stream.getTracks()) track.stop();
      return;
    }
    let r: Run | undefined;
    try {
      const type = pickType() ?? "";
      const recorder = new MediaRecorder(stream, type ? { mimeType: type, audioBitsPerSecond: BITRATE } : { audioBitsPerSecond: BITRATE });
      const made: Run = { token: t, recorder, stream, chunks: [], mode, abort: new AbortController() };
      r = made;
      recorder.addEventListener("dataavailable", (e: BlobEvent) => {
        if (e.data?.size) made.chunks.push(e.data);
      });
      recorder.addEventListener("stop", () => {
        release(made);
        setLevel(0);
        if (token.current === made.token) void send(made);
      });
      meter(made, setLevel);
      recorder.start(1000);
      const began = Date.now();
      made.timer = setInterval(() => {
        const now = Date.now() - began;
        setElapsed(now);
        if (now >= MAX_MS && recorder.state === "recording") {
          toast(AT_CAP);
          stop();
        }
      }, 250);
      run.current = made;
      handlers.current.onStart?.();
      setElapsed(0);
      setState("recording");
    } catch {
      release(r ?? { stream });
      run.current = null;
      setLevel(0);
      setUnavailable(true);
      setState("idle");
      toast(UNAVAILABLE, "error", 8000);
    }
  }, [configured, en, send, stop]);

  const toggle = useCallback(() => {
    if (state === "idle") void start();
    else if (state === "recording") stop();
  }, [state, start, stop]);

  // Off screen, «＋» closed, another session: the recording is dropped, never sent.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `owner` is the point — a new one drops the recording
  useEffect(() => cancel, [cancel, owner]);
  useEffect(() => {
    if (!active) cancel();
  }, [active, cancel]);

  return { state, elapsed, level, en, setEn, unavailable, toggle: active ? toggle : () => undefined, cancel };
}

/** The level meter: Web Audio's analyser, read once a frame. Optional — a browser without it shows the time alone. */
function meter(r: Run, set: (level: number) => void) {
  const Context = globalThis.AudioContext ?? (globalThis as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Context || typeof requestAnimationFrame !== "function") return;
  try {
    const audio = new Context();
    const analyser = audio.createAnalyser();
    analyser.fftSize = 512;
    audio.createMediaStreamSource(r.stream).connect(analyser);
    const samples = new Uint8Array(analyser.fftSize);
    r.audio = audio;
    const read = () => {
      analyser.getByteTimeDomainData(samples);
      let sum = 0;
      for (const v of samples) sum += ((v - 128) / 128) ** 2;
      set(Math.min(1, Math.sqrt(sum / samples.length) * 4));
      r.frame = requestAnimationFrame(read);
    };
    r.frame = requestAnimationFrame(read);
  } catch {
    // no meter: the recording goes on
  }
}
