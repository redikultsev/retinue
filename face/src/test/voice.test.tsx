import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Composer } from "@/components/composer";
import { live } from "@/lib/live";
import { toast } from "@/lib/toast";
import { AT_CAP, clock, insertAt, MAX_MS, NOT_SET_UP, pickType, UNAVAILABLE } from "@/lib/voice";
import { mountNew } from "./mount-new";

vi.mock("@/lib/toast", async (original) => ({ ...(await original<typeof import("@/lib/toast")>()), toast: vi.fn() }));

/** MediaRecorder as the browsers have it, enough for the app: the types it takes, start, and stop with its data. */
class FakeRecorder {
  static types = ["audio/webm;codecs=opus", "audio/webm"];
  static made: FakeRecorder[] = [];
  static isTypeSupported(type: string) {
    return FakeRecorder.types.includes(type);
  }
  state: "inactive" | "recording" = "inactive";
  mimeType: string;
  options: MediaRecorderOptions | undefined;
  private listeners: Record<string, ((e: unknown) => void)[]> = {};
  constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
    this.options = options;
    this.mimeType = options?.mimeType ?? "audio/mp4";
    FakeRecorder.made.push(this);
  }
  addEventListener(type: string, fn: (e: unknown) => void) {
    this.listeners[type] = [...(this.listeners[type] ?? []), fn];
  }
  start() {
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    for (const fn of this.listeners.dataavailable ?? []) fn({ data: new Blob(["voice"], { type: this.mimeType }) });
    for (const fn of this.listeners.stop ?? []) fn({});
  }
}

const track = { stop: vi.fn() };
const stream = { getTracks: () => [track] } as unknown as MediaStream;
let getUserMedia: ReturnType<typeof vi.fn>;

function microphone(given: unknown = { getUserMedia }) {
  Object.defineProperty(navigator, "mediaDevices", { value: given, configurable: true });
}

beforeEach(() => {
  FakeRecorder.made = [];
  FakeRecorder.types = ["audio/webm;codecs=opus", "audio/webm"];
  getUserMedia = vi.fn().mockResolvedValue(stream);
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  microphone();
  live.meta = { schema: 1, owner_ok: true, face_origin: "", tracks: [{ name: "notes", lane: "private", kind: "private" }], speech: true };
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.mocked(toast).mockClear();
  track.stop.mockClear();
  live.meta = null;
});

const field = () => screen.getByRole("textbox", { name: "Сообщение" }) as HTMLTextAreaElement;

function composer(answer: { text: string; message?: string } = { text: "KV cache" }) {
  const send = vi.fn().mockResolvedValue({ say: "Принято." });
  const transcribe = vi.fn().mockResolvedValue(answer);
  render(<Composer session="s1" running={false} send={send} transcribe={transcribe} />);
  return { send, transcribe };
}

async function record() {
  fireEvent.click(screen.getByRole("button", { name: /^(Диктовать|Сказать по-английски)$/ }));
  await screen.findByRole("status", { name: "Идёт запись" });
}

describe("dictation in a session", () => {
  it("records, shows the time and «Отмена», and puts the text at the cursor for him to send", async () => {
    const { send, transcribe } = composer();
    fireEvent.change(field(), { target: { value: "Проверь и сделай" } });
    field().setSelectionRange(7, 7);
    await record();
    expect(getUserMedia).toHaveBeenCalledWith({ audio: { echoCancellation: true, noiseSuppression: true } });
    expect(FakeRecorder.made[0]!.options).toEqual({ mimeType: "audio/webm;codecs=opus", audioBitsPerSecond: 64000 });
    expect(screen.getByTestId("voice-clock").textContent).toBe("0:00 / 10:00");
    expect(screen.getByRole("button", { name: "Отмена" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Остановить и расшифровать" }));
    await waitFor(() => expect(field().value).toBe("Проверь KV cache и сделай"));
    const [audio, mode] = transcribe.mock.calls[0]!;
    expect([(audio as Blob).type, (audio as Blob).size, mode]).toEqual(["audio/webm;codecs=opus", 5, "dictate"]);
    expect(send).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled();
    await waitFor(() => expect(field().selectionStart).toBe("Проверь KV cache".length));
  });

  it("«Отмена» drops the recording: nothing is sent anywhere", async () => {
    const { transcribe } = composer();
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
    expect(transcribe).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled();
    expect(field().value).toBe("");
  });

  it("stops by itself at 10 minutes, says so, and transcribes what it has", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    const { transcribe } = composer();
    await record();
    act(() => vi.advanceTimersByTime(4 * 60 * 1000));
    expect(screen.getByTestId("voice-clock").textContent).toBe("4:00 / 10:00");
    act(() => vi.advanceTimersByTime(MAX_MS));
    expect(toast).toHaveBeenCalledWith(AT_CAP);
    await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(field().value).toBe("KV cache"));
  });

  it("an error is a short line, and the field is back", async () => {
    const send = vi.fn();
    const { ApiError } = await import("@/lib/api");
    const transcribe = vi.fn().mockRejectedValue(new ApiError(502, "Не удалось расшифровать: кончились кредиты ElevenLabs — пополни баланс."));
    render(<Composer session="s1" running={false} send={send} transcribe={transcribe} />);
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Остановить и расшифровать" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith("Не удалось расшифровать: кончились кредиты ElevenLabs — пополни баланс.", "error"),
    );
    expect(field().hidden).toBe(false);
  });
});

describe("English, word for word", () => {
  it("is sent at once as the session's message, unedited", async () => {
    const { send, transcribe } = composer({ text: "So, um, yes", message: "(голосом, дословно) So, um, yes" });
    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    expect(screen.getByRole("button", { name: "EN" }).getAttribute("aria-pressed")).toBe("true");
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Остановить и расшифровать" }));
    await waitFor(() => expect(send).toHaveBeenCalledWith("s1", "(голосом, дословно) So, um, yes", expect.stringMatching(/^face:/), []));
    expect(transcribe.mock.calls[0]![1]).toBe("en");
    expect(field().value).toBe(""); // nothing put into the field
  });

  it("in «＋» it is the new session's first message, and the session opens", async () => {
    const d = newDeps({ text: "Hello there", message: "(голосом, дословно) Hello there" });
    const { router } = await mountNew(d);
    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Остановить и расшифровать" }));
    await waitFor(() =>
      expect(d.newSession).toHaveBeenCalledWith("notes", "(голосом, дословно) Hello there", expect.stringMatching(/^face:/), null, []),
    );
    await waitFor(() => expect(router.state.location.pathname).toBe("/s/n1"));
  });

  it("in «＋» dictation goes into the text", async () => {
    const d = newDeps({ text: "найди рейсы" });
    await mountNew(d);
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Остановить и расшифровать" }));
    await waitFor(() => expect(task().value).toBe("найди рейсы"));
    expect(d.newSession).not.toHaveBeenCalled();
  });
});

const task = () => screen.getByRole("textbox", { name: "Что сделать" }) as HTMLTextAreaElement;

function newDeps(answer: { text: string; message?: string }) {
  return {
    newSession: vi.fn().mockResolvedValue({ session: "n1" }),
    transcribe: vi.fn().mockResolvedValue(answer),
  };
}

/** A promise the test settles itself. */
function later<T>() {
  let settle!: (value: T) => void;
  const promise = new Promise<T>((resolve) => {
    settle = resolve;
  });
  return { promise, settle };
}

describe("a recording never outlives what shows it", () => {
  it("leaving «＋» (another tab, back) drops it: the microphone off, nothing sent, even past 10 minutes", async () => {
    const d = newDeps({ text: "room noise", message: "(голосом, дословно) room noise" });
    const { router } = await mountNew(d);
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    await record();
    expect(task().readOnly).toBe(true);
    await act(async () => {
      await router.navigate({ to: "/today" });
    });
    expect(track.stop).toHaveBeenCalled();
    expect(FakeRecorder.made[0]!.state).toBe("inactive");
    act(() => vi.advanceTimersByTime(MAX_MS + 1000));
    await act(async () => undefined);
    expect(d.transcribe).not.toHaveBeenCalled();
    expect(d.newSession).not.toHaveBeenCalled();
  });

  it("gone while the permission prompt is up: the microphone it then gives is let go, no recorder starts", async () => {
    const asked = later<MediaStream>();
    getUserMedia.mockReturnValue(asked.promise);
    const { transcribe } = composer();
    fireEvent.click(screen.getByRole("button", { name: "Диктовать" }));
    expect(await screen.findByRole("status", { name: "Жду микрофон" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Отмена" })).toBeTruthy();
    cleanup();
    await act(async () => asked.settle(stream));
    expect(track.stop).toHaveBeenCalled();
    expect(FakeRecorder.made).toEqual([]);
    expect(transcribe).not.toHaveBeenCalled();
  });

  it("«Отмена» at the permission prompt: the same", async () => {
    const asked = later<MediaStream>();
    getUserMedia.mockReturnValue(asked.promise);
    composer();
    fireEvent.click(screen.getByRole("button", { name: "Диктовать" }));
    fireEvent.click(await screen.findByRole("button", { name: "Отмена" }));
    expect(field().hidden).toBe(false);
    await act(async () => asked.settle(stream));
    expect(track.stop).toHaveBeenCalled();
    expect(FakeRecorder.made).toEqual([]);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("another session on the same screen drops it: a result reaches only the session that started it", async () => {
    const send = vi.fn();
    const transcribe = vi.fn().mockResolvedValue({ text: "x", message: "(голосом, дословно) x" });
    const view = render(<Composer session="s1" running={false} send={send} transcribe={transcribe} />);
    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    await record();
    view.rerender(<Composer session="s2" running={false} send={send} transcribe={transcribe} />);
    expect(track.stop).toHaveBeenCalled();
    expect(screen.queryByRole("status")).toBeNull();
    expect(transcribe).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });

  it("«Отмена» while it is transcribed: what comes back later is dropped", async () => {
    const answer = later<{ text: string }>();
    const send = vi.fn();
    const transcribe = vi.fn().mockReturnValue(answer.promise);
    render(<Composer session="s1" running={false} send={send} transcribe={transcribe} />);
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Остановить и расшифровать" }));
    await screen.findByRole("status", { name: "Расшифровываю" });
    const signal = transcribe.mock.calls[0]![2] as AbortSignal;
    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    expect(signal.aborted).toBe(true);
    await act(async () => answer.settle({ text: "поздно" }));
    expect(field().value).toBe("");
  });

  it("a recorder that fails to start lets everything go and says why", async () => {
    vi.spyOn(FakeRecorder.prototype, "start").mockImplementation(() => {
      throw new Error("NotSupportedError");
    });
    composer();
    fireEvent.click(screen.getByRole("button", { name: "Диктовать" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(UNAVAILABLE, "error", 8000));
    expect(track.stop).toHaveBeenCalled();
    expect(screen.queryByRole("status")).toBeNull();
    expect(field().hidden).toBe(false);
  });
});

describe("an English transcript is never lost", () => {
  it("in a session: not sent, it goes into the field with the reason", async () => {
    const { ApiError } = await import("@/lib/api");
    const send = vi.fn().mockRejectedValue(new ApiError(0, "Нет связи с сервером."));
    const transcribe = vi.fn().mockResolvedValue({ text: "So, um", message: "(голосом, дословно) So, um" });
    render(<Composer session="s1" running={false} send={send} transcribe={transcribe} />);
    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Остановить и расшифровать" }));
    await waitFor(() => expect(field().value).toBe("(голосом, дословно) So, um"));
    expect(toast).toHaveBeenCalledWith("Нет связи с сервером. Запись — в поле: отправь её сам.", "error", 8000);
  });

  it("in «＋» it goes with what the box holds — its text, the files — as one message", async () => {
    const d = newDeps({ text: "Hello", message: "(голосом, дословно) Hello" });
    await mountNew(d);
    fireEvent.change(task(), { target: { value: "Проверь мой английский" } });
    fireEvent.change(screen.getByTestId("attach-input"), { target: { files: [new File(["abc"], "notes.txt", { type: "text/plain" })] } });
    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Остановить и расшифровать" }));
    await waitFor(() => expect(d.newSession).toHaveBeenCalledTimes(1));
    const [, body, , , attachments] = d.newSession.mock.calls[0]!;
    expect(body).toBe("Проверь мой английский\n\n(голосом, дословно) Hello");
    expect((attachments as { name: string }[]).map((a) => a.name)).toEqual(["notes.txt"]);
  });

  it("in «＋» when the session could not start: kept in the text with the reason, nothing opened", async () => {
    const { ApiError } = await import("@/lib/api");
    const d = newDeps({ text: "Hello", message: "(голосом, дословно) Hello" });
    d.newSession.mockRejectedValue(new ApiError(503, "Комната занята."));
    const { router } = await mountNew(d);
    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Остановить и расшифровать" }));
    await waitFor(() => expect(task().value).toBe("(голосом, дословно) Hello"));
    expect(toast).toHaveBeenCalledWith("Комната занята. Запись — в поле: отправь её сам.", "error", 8000);
    expect(router.state.location.pathname).toBe("/new");
  });
});

describe("the box while it records", () => {
  it("keeps «Стоп» in reach while a turn runs: stopping the turn does not drop the dictation", async () => {
    const stop = vi.fn().mockResolvedValue({ ok: true });
    render(<Composer session="s1" running send={vi.fn()} stop={stop} transcribe={vi.fn()} />);
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Стоп" }));
    expect(stop).toHaveBeenCalledWith("s1");
    expect(screen.getByRole("status", { name: "Идёт запись" })).toBeTruthy();
  });

  it("an English message after a failed send is a message of its own: a new id, not the failed one's", async () => {
    const { ApiError } = await import("@/lib/api");
    const send = vi.fn().mockRejectedValueOnce(new ApiError(0, "Нет связи с сервером.")).mockResolvedValue({ say: "Принято." });
    const transcribe = vi.fn().mockResolvedValue({ text: "Hi", message: "(голосом, дословно) Hi" });
    render(<Composer session="s1" running={false} send={send} transcribe={transcribe} />);
    fireEvent.change(field(), { target: { value: "привет" } });
    fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    await record();
    fireEvent.click(screen.getByRole("button", { name: "Остановить и расшифровать" }));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    const [first, second] = send.mock.calls;
    expect(second![1]).toBe("привет\n\n(голосом, дословно) Hi");
    expect(second![2]).not.toBe(first![2]);
  });
});

describe("where there is no microphone", () => {
  it("no getUserMedia (Telegram's web view): the button says so and records nothing", () => {
    microphone(null);
    const { transcribe } = composer();
    const button = screen.getByRole("button", { name: UNAVAILABLE });
    fireEvent.click(button);
    expect(toast).toHaveBeenCalledWith(UNAVAILABLE, "error", 8000);
    expect(FakeRecorder.made).toEqual([]);
    expect(transcribe).not.toHaveBeenCalled();
  });

  it("refused when asked: the same line, no crash", async () => {
    getUserMedia.mockRejectedValue(Object.assign(new Error("denied"), { name: "NotAllowedError" }));
    composer();
    fireEvent.click(screen.getByRole("button", { name: "Диктовать" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(UNAVAILABLE, "error", 8000));
    expect(screen.getByRole("button", { name: UNAVAILABLE })).toBeTruthy();
    expect(field().hidden).toBe(false);
  });

  it("no MediaRecorder at all: the same", () => {
    vi.stubGlobal("MediaRecorder", undefined);
    composer();
    expect(screen.getByRole("button", { name: UNAVAILABLE })).toBeTruthy();
  });

  it("the platform cannot hear: said before anything is recorded", () => {
    live.meta = { ...live.meta!, speech: false };
    composer();
    fireEvent.click(screen.getByRole("button", { name: "Диктовать" }));
    expect(toast).toHaveBeenCalledWith(NOT_SET_UP, "error");
    expect(getUserMedia).not.toHaveBeenCalled();
  });
});

describe("its pieces", () => {
  it("the recording's type: the first the browser takes, Safari's MP4 when WebM is not", () => {
    expect(pickType(FakeRecorder)).toBe("audio/webm;codecs=opus");
    expect(pickType({ isTypeSupported: (t) => t === "audio/mp4" })).toBe("audio/mp4");
    expect(pickType({ isTypeSupported: () => false })).toBe(""); // none named: the browser's own
    expect(pickType(null)).toBeNull();
  });

  it("text at the cursor, spaced where words would touch", () => {
    expect(insertAt("", 0, 0, "привет")).toEqual({ text: "привет", caret: 6 });
    expect(insertAt("ab", 1, 1, "X")).toEqual({ text: "a X b", caret: 3 });
    expect(insertAt("a ", 2, 2, "X")).toEqual({ text: "a X", caret: 3 });
    expect(insertAt("a old b", 2, 5, "new")).toEqual({ text: "a new b", caret: 5 });
  });

  it("the clock", () => {
    expect([clock(0), clock(83_000), clock(MAX_MS)]).toEqual(["0:00", "1:23", "10:00"]);
  });
});
