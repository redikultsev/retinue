// The face API over fetch: the session cookie rides along (same origin), every POST carries `X-Retinue: 1`.
import type {
  Approval,
  ArchivedSession,
  BaseCard,
  BaseResponse,
  DecideResponse,
  QuestionReply,
  DiffResponse,
  Meta,
  OutFile,
  Proposal,
  PushCard,
  PushResponse,
  Session,
  SessionSnapshot,
  SessionsSnapshot,
  Tally,
} from "./types";
import type { Encoded } from "./attach";
import type { Press, WaitingDetail } from "./feed";
import { type ChatRow, type ChatSnapshot, paths } from "./contract";
import { inputId } from "./utils";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** The answer's body, when it was JSON: a refusal's own words beside its message. */
    readonly data?: unknown,
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

/** Tells them: a call was refused for want of a login — `request`'s own, or an upload's (lib/uploads.ts). */
export function loggedOut(): void {
  for (const l of unauthorized) l();
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
  return call(paths.speech(mode), {
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
  if (res.status === 401 && !path.startsWith("/api/auth/")) loggedOut();
  if (!res.ok) {
    let message = MESSAGES[res.status] ?? `Error ${res.status}.`;
    let data: { error?: string; toast?: string } | undefined;
    try {
      data = (await res.json()) as { error?: string; toast?: string };
      message = data.error ?? data.toast ?? message;
    } catch {
      // not JSON: keep the generic message
    }
    throw new ApiError(res.status, message, data);
  }
  return (await res.json()) as T;
}

export const api = {
  meta: () => request<Meta>(paths.meta),
  /** «Got it» under «What's new»: the notes up to `version` were seen. */
  whatsNewSeen: (version: string) => request<{ ok: boolean }>(paths.whatsNewSeen, { version }),
  /** The last eight seven-day spans of the platform's tally, the newest first («Numbers»). */
  stats: () => request<{ now: number; weeks: Tally[] }>(paths.stats),
  sessions: () => request<SessionsSnapshot>(paths.sessions),
  /** «Archived»: what the owner archived, the latest first. */
  archived: () => request<{ sessions: ArchivedSession[] }>(paths.archived),
  session: (id: string) => request<SessionSnapshot>(paths.session(id)),
  approval: (id: string) => request<{ approval: Approval }>(paths.approval(id)),
  file: (id: string) => request<{ file: OutFile }>(`${paths.file(id)}/meta`),
  diff: (id: string) => request<DiffResponse>(paths.sessionDo(id, "diff")),
  /** `project`: a project's folder in the Base — the session runs there. */
  newSession: (track: string, text: string, id = inputId(), sphere: string | null = null, attachments: Encoded[] = [], project?: string) =>
    request<{ session: string }>(paths.sessions, {
      track,
      ...(sphere ? { sphere } : {}),
      ...(project !== undefined ? { project } : {}),
      text,
      input_id: id,
      ...(attachments.length ? { attachments } : {}),
    }),
  /** Into the sphere's main session, as `/<sphere> text` in Telegram: a new one, made main, when it has none. */
  toMain: (sphere: string, text: string, id = inputId(), attachments: Encoded[] = []) =>
    request<{ session: string }>(paths.sessions, {
      sphere,
      main: true,
      text,
      input_id: id,
      ...(attachments.length ? { attachments } : {}),
    }),
  input: (session: string, text: string, id = inputId(), attachments: Encoded[] = []) =>
    request<{ say: string }>(
      paths.sessionDo(session, "input"),
      attachments.length ? { input_id: id, text, attachments } : { input_id: id, text },
    ),
  /** His answer to a session on one of his computers, at the turn its card showed (`at`, the session's last activity):
   * it waits on the platform until that computer takes it. 409: the session moved on since. */
  answerComputer: (device: string, id: string, text: string, at: number) =>
    request<{ answer: string }>(paths.computerAnswer(device, id), { text, at }),
  /** A picture the owner sent: the platform's own thumbnail of it. */
  thumbUrl: (session: string, input: string, n: number) =>
    paths.thumb(session, input, n),
  pin: (session: string, pinned: boolean) =>
    request<{ session: Session }>(paths.sessionDo(session, "pin"), { pinned }),
  /** «Archive»: the session and its kin end and leave the list; a message to it brings it back. */
  archive: (session: string) =>
    request<{ session: Session }>(paths.sessionDo(session, "archive"), {}),
  /** «Undo» under «Archived»: back in the list (its stopped turn stays stopped); `main` — it was the sphere's main one. */
  unarchive: (session: string, main: boolean) =>
    request<{ session: Session }>(paths.sessionDo(session, "unarchive"), { main }),
  side: (session: string, text: string, id = inputId()) =>
    request<{ session: string; say: string }>(paths.sessionDo(session, "side"), {
      input_id: id,
      text,
    }),
  setSphere: (session: string, sphere: string | null) =>
    request<{ session: Session }>(paths.sessionDo(session, "sphere"), { sphere }),
  setMain: (session: string, main: boolean) =>
    request<{ session: Session }>(paths.sessionDo(session, "main"), { main }),
  seen: (session: string) => request<{ session: Session }>(paths.sessionDo(session, "seen"), {}),
  fork: (session: string, text: string, id = inputId()) =>
    request<{ session: string; say: string }>(paths.sessionDo(session, "fork"), {
      input_id: id,
      text,
    }),
  stop: (session: string) => request<{ ok: boolean }>(paths.sessionDo(session, "stop"), {}),
  decide: (approval: Pick<Approval, "id" | "digest">, action: "approve" | "reject") =>
    request<DecideResponse>(paths.approvalDo(approval.id, "decide"), {
      action,
      digest: approval.digest,
    }),
  /** «Done» on a courier card: the owner did it himself or will not; it leaves «Waiting for you». */
  courierDone: (id: string) => request<DecideResponse>(paths.approvalDo(id, "done"), {}),
  answer: (approval: Pick<Approval, "id" | "digest">, question: number, reply: QuestionReply) =>
    request<DecideResponse>(paths.approvalDo(approval.id, "answer"), {
      digest: approval.digest,
      question,
      ...reply,
    }),
  /** The platform's own commit to the Base after a turn: its card, its diff, «Undo». */
  baseCard: (id: string) => request<{ card: BaseCard }>(paths.base(id)),
  baseDiff: (id: string) => request<{ diff: string; truncated: boolean }>(`${paths.base(id)}/diff`),
  baseUndo: (id: string) => request<BaseResponse>(`${paths.base(id)}/undo`, {}),
  /** «Push»: a repository's push card, opened or brought up to date (the same one Telegram shows). */
  pushOpen: (session: string, repo?: string) =>
    request<PushResponse>(paths.sessionDo(session, "push"), repo ? { repo } : {}),
  pushCard: (id: string) => request<{ card: PushCard }>(paths.push(id)),
  pushDiff: (id: string) => request<{ diff: string; truncated: boolean }>(`${paths.push(id)}/diff`),
  pushTarget: (id: string, target: string) =>
    request<PushResponse>(`${paths.push(id)}/target`, { target }),
  pushTitle: (id: string, title: string) => request<PushResponse>(`${paths.push(id)}/title`, { title }),
  pushSend: (id: string) => request<PushResponse>(`${paths.push(id)}/send`, {}),
  pushCancel: (id: string) => request<PushResponse>(`${paths.push(id)}/cancel`, {}),
  /** A sub-session a session proposed: «Start» starts it with its brief, «Not now» sets it aside. */
  proposal: (id: string, action: "start" | "dismiss") =>
    request<{ proposal: Proposal; say?: string }>(paths.proposal(id, action), {}),
  /** A press on her row of «Waiting for you» (`t`, the row's token): the router applies it, and the next digest has it. */
  waitingPress: (t: string, action: Press) => request<{ ok: boolean }>(paths.waitingPress, { t, action }),
  /** A row of hers opened: what it is about and where it is, asked of the router — a few seconds at most. */
  waitingDetail: (ref: string, t?: string) =>
    request<{ detail: WaitingDetail }>(paths.waitingDetail, t ? { ref, t } : { ref }),
  /** «Talk to Retinue»: the assistant's conversation, its newest rows. */
  chat: () => request<ChatSnapshot>(paths.chat),
  /** His message into it: his row back at once, `pending` until the router has it; the same id is the same message. */
  chatSay: (text: string, id = inputId()) => request<{ ok: boolean; row: ChatRow }>(paths.chat, { input_id: id, text }),
  /** A button of a row of hers: the router decides, and the row comes back rewritten. */
  chatPress: (id: string, row: string) => request<{ ok: boolean }>(paths.chatPress, { id, row }),
  loginCode: (code: string) => request<{ ok?: boolean }>(paths.auth("code"), { code }),
  loginTelegram: (initData: string) => request<{ ok?: boolean }>(paths.auth("telegram"), { init_data: initData }),
  logout: () => request<{ ok?: boolean }>(paths.auth("logout"), {}),
  speech,
};
