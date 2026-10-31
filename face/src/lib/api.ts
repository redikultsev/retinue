// The face API over fetch: the session cookie rides along (same origin), every POST carries `X-Retinue: 1`.
import type {
  Approval,
  BaseCard,
  BaseResponse,
  DecideResponse,
  QuestionReply,
  DiffResponse,
  Meta,
  OutFile,
  PushCard,
  PushResponse,
  Session,
  SessionSnapshot,
  SessionsSnapshot,
  Summary,
  Tally,
} from "./types";
import type { Encoded } from "./attach";
import { inputId } from "./utils";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

type Listener = () => void;
const unauthorized = new Set<Listener>();
/** Called on every 401: the app goes to the login screen. */
export function onUnauthorized(listener: Listener): () => void {
  unauthorized.add(listener);
  return () => unauthorized.delete(listener);
}

const MESSAGES: Record<number, string> = {
  401: "Please log in again.",
  403: "Request refused.",
  404: "Not found.",
  410: "The data is out of date — refreshing.",
  413: "The request is too large.",
  429: "Too many attempts — wait 10 minutes.",
};

export async function request<T>(path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.method = "POST";
    init.headers = { ...init.headers, "Content-Type": "application/json", "X-Retinue": "1" };
    init.body = JSON.stringify(body);
  }
  return call<T>(path, init);
}

/** A recording, as the browser made it, for its text (`/api/speech`, docs/face.md «Voice»). In English the answer
 * also carries `message`: the text as the session gets it, marked «(by voice, verbatim)». */
export function speech(audio: Blob, mode: "dictate" | "en", signal?: AbortSignal): Promise<{ text: string; message?: string }> {
  return call(`/api/speech?mode=${mode}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json", "Content-Type": audio.type || "audio/webm", "X-Retinue": "1" },
    body: audio,
    ...(signal ? { signal } : {}),
  });
}

async function call<T>(path: string, init: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, init.signal?.aborted ? "Cancelled." : "No connection to the server.");
  }
  if (res.status === 401 && !path.startsWith("/api/auth/")) for (const l of unauthorized) l();
  if (!res.ok) {
    let message = MESSAGES[res.status] ?? `Error ${res.status}.`;
    try {
      const data = (await res.json()) as { error?: string; toast?: string };
      message = data.error ?? data.toast ?? message;
    } catch {
      // not JSON: keep the generic message
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

export const api = {
  meta: () => request<Meta>("/api/meta"),
  /** The last eight seven-day spans of the platform's tally, the newest first («Week»). */
  stats: () => request<{ now: number; weeks: Tally[] }>("/api/stats"),
  sessions: () => request<SessionsSnapshot>("/api/sessions"),
  session: (id: string) => request<SessionSnapshot>(`/api/sessions/${encodeURIComponent(id)}`),
  approval: (id: string) => request<{ approval: Approval }>(`/api/approvals/${encodeURIComponent(id)}`),
  file: (id: string) => request<{ file: OutFile }>(`/api/files/${encodeURIComponent(id)}/meta`),
  diff: (id: string) => request<DiffResponse>(`/api/sessions/${encodeURIComponent(id)}/diff`),
  newSession: (track: string, text: string, id = inputId(), sphere: string | null = null, attachments: Encoded[] = []) =>
    request<{ session: string }>("/api/sessions", {
      track,
      ...(sphere ? { sphere } : {}),
      text,
      input_id: id,
      ...(attachments.length ? { attachments } : {}),
    }),
  input: (session: string, text: string, id = inputId(), attachments: Encoded[] = []) =>
    request<{ say: string }>(
      `/api/sessions/${encodeURIComponent(session)}/input`,
      attachments.length ? { input_id: id, text, attachments } : { input_id: id, text },
    ),
  /** A ping's suggested reply pressed: the platform sends that reply once, whichever place pressed it. */
  reply: (session: string, seq: number, n: number) =>
    request<{ say: string; summary: Summary | null }>(`/api/sessions/${encodeURIComponent(session)}/input`, {
      reply: { seq, n },
    }),
  /** A picture the owner sent: the platform's own thumbnail of it. */
  thumbUrl: (session: string, input: string, n: number) =>
    `/api/sessions/${encodeURIComponent(session)}/inputs/${encodeURIComponent(input)}/${n}`,
  pin: (session: string, pinned: boolean) =>
    request<{ session: Session }>(`/api/sessions/${encodeURIComponent(session)}/pin`, { pinned }),
  side: (session: string, text: string, id = inputId()) =>
    request<{ session: string; say: string }>(`/api/sessions/${encodeURIComponent(session)}/side`, {
      input_id: id,
      text,
    }),
  setSphere: (session: string, sphere: string | null) =>
    request<{ session: Session }>(`/api/sessions/${encodeURIComponent(session)}/sphere`, { sphere }),
  setMain: (session: string, main: boolean) =>
    request<{ session: Session }>(`/api/sessions/${encodeURIComponent(session)}/main`, { main }),
  seen: (session: string) => request<{ session: Session }>(`/api/sessions/${encodeURIComponent(session)}/seen`, {}),
  fork: (session: string, text: string, id = inputId()) =>
    request<{ session: string; say: string }>(`/api/sessions/${encodeURIComponent(session)}/fork`, {
      input_id: id,
      text,
    }),
  stop: (session: string) => request<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(session)}/stop`, {}),
  decide: (approval: Pick<Approval, "id" | "digest">, action: "approve" | "reject") =>
    request<DecideResponse>(`/api/approvals/${encodeURIComponent(approval.id)}/decide`, {
      action,
      digest: approval.digest,
    }),
  answer: (approval: Pick<Approval, "id" | "digest">, question: number, reply: QuestionReply) =>
    request<DecideResponse>(`/api/approvals/${encodeURIComponent(approval.id)}/answer`, {
      digest: approval.digest,
      question,
      ...reply,
    }),
  /** «To the Base»: the session's commit card, opened or brought up to date (the same one Telegram shows). */
  baseOpen: (session: string) => request<BaseResponse>(`/api/sessions/${encodeURIComponent(session)}/base`, {}),
  baseCard: (id: string) => request<{ card: BaseCard }>(`/api/base/${encodeURIComponent(id)}`),
  baseDiff: (id: string) => request<{ diff: string; truncated: boolean }>(`/api/base/${encodeURIComponent(id)}/diff`),
  baseToggle: (id: string, path: string, on: boolean) =>
    request<BaseResponse>(`/api/base/${encodeURIComponent(id)}/toggle`, { path, on }),
  baseSubject: (id: string, subject: string) =>
    request<BaseResponse>(`/api/base/${encodeURIComponent(id)}/subject`, { subject }),
  baseCommit: (id: string) => request<BaseResponse>(`/api/base/${encodeURIComponent(id)}/commit`, {}),
  baseCancel: (id: string) => request<BaseResponse>(`/api/base/${encodeURIComponent(id)}/cancel`, {}),
  /** «Push»: a repository's push card, opened or brought up to date (the same one Telegram shows). */
  pushOpen: (session: string, repo?: string) =>
    request<PushResponse>(`/api/sessions/${encodeURIComponent(session)}/push`, repo ? { repo } : {}),
  pushCard: (id: string) => request<{ card: PushCard }>(`/api/push/${encodeURIComponent(id)}`),
  pushDiff: (id: string) => request<{ diff: string; truncated: boolean }>(`/api/push/${encodeURIComponent(id)}/diff`),
  pushTarget: (id: string, target: string) =>
    request<PushResponse>(`/api/push/${encodeURIComponent(id)}/target`, { target }),
  pushTitle: (id: string, title: string) => request<PushResponse>(`/api/push/${encodeURIComponent(id)}/title`, { title }),
  pushSend: (id: string) => request<PushResponse>(`/api/push/${encodeURIComponent(id)}/send`, {}),
  pushCancel: (id: string) => request<PushResponse>(`/api/push/${encodeURIComponent(id)}/cancel`, {}),
  loginCode: (code: string) => request<{ ok?: boolean }>("/api/auth/code", { code }),
  loginTelegram: (initData: string) => request<{ ok?: boolean }>("/api/auth/telegram", { init_data: initData }),
  logout: () => request<{ ok?: boolean }>("/api/auth/logout", {}),
  speech,
};
