// The face API's shapes (docs/face.md «How the app stays current», «API»). Transcript item data mirrors
// retinue/transcript.py `_call` and `_Fold`; the approval view mirrors retinue/courier.py `CardView`.

export const SCHEMA = 1;

export type SessionState = "active" | "waiting" | "idle" | "closed";

export interface Track {
  name: string;
  lane: string;
  kind: "code" | "private" | string;
}

/** A sphere of the owner's life (retinue/config.py `Sphere`): sessions are grouped by it. */
export interface Sphere {
  name: string;
  title: string;
  icon: string;
  /** The track a new session of this sphere runs on. */
  track: string;
}

export interface Meta {
  schema: number;
  owner_ok: boolean;
  tracks: Track[];
  /** The track «＋» starts a new general session on: the root of the Base. Absent from a platform before it. */
  general?: string | null;
  face_origin: string;
  /** Absent from a platform before spheres. */
  spheres?: Sphere[];
  /** The owner's voice works here: the platform has the key and a way out. Absent from a platform before it. */
  speech?: boolean;
}

export interface Session {
  id: string;
  track: string;
  lane: string;
  kind: "code" | "private" | string;
  state: SessionState;
  created: number;
  at: number;
  title: string;
  waiting: number;
  /** The owner keeps it on top of the list (stored by the platform, so every device agrees). */
  pinned?: boolean;
  /** A side question: the session its question is about (a session with `parent` runs read-only). */
  parent?: string | null;
  /** A sub-session: the session that started it with a brief; it is listed under that one. */
  sub_of?: string | null;
  /** Its open sub-sessions. Absent from a platform before them. */
  children?: number;
  /** A side question. Absent: a platform before sub-sessions, where `parent` marks one. */
  side?: boolean;
  /** The sphere it belongs to (`Sphere.name`); absent or null: none. */
  sphere?: string | null;
  /** Its sphere's main session: `/<sphere>` in Telegram writes into it. */
  main?: boolean;
  /** A fork: the session whose conversation it started from; it is listed under that one. */
  fork_of?: string | null;
  /** A summary or a card came after the owner last looked at it. */
  unread?: boolean;
  /** Its Base files not committed yet (retinue/tobase.py): «В Базу · N». Absent from a platform before the Base. */
  base?: number;
  /** Commits in the repositories it worked in that no branch of the remote has (retinue/topush.py): «Запушить · N». */
  push?: number;
  /** Why a repository it worked in cannot be pushed now («<repo>: <reason>», retinue/topush.py); absent: nothing. */
  push_why?: string;
  /** When its running turn began (a resume after a card is the same turn); absent from a platform before it. */
  turn_started?: number;
  /** Its run waits for room in the lane (a sub-session beyond the room's share, retinue/subs.py). */
  queued?: boolean;
}

/** What the face says of a session whose run waits for room. */
export const QUEUED = "ждёт места в комнате";

export type ApprovalState =
  | "pending"
  | "shown"
  | "granted"
  | "executing"
  | "executed"
  | "rejected"
  | "stale"
  | "expired"
  | "dropped"
  /** A question: every question has its answer; the session resumes with them. */
  | "answered";

/** courier.CardView as JSON: header lines, the verbatim body, a status line, an optional (words, url) link. */
export interface CardView {
  head: string[];
  body?: string;
  status?: string;
  link?: [string, string] | [];
}

/** One answer of a question card (retinue/questions.py): the labels picked, joined, or the owner's own words. */
export interface QuestionAnswer {
  value: string;
  other: boolean;
}

/** A question card's view: the answers so far and a multi-select's ticks not sent yet, by question number. */
export interface QuestionView {
  answers: Record<string, QuestionAnswer>;
  picks: Record<string, string[]>;
  status?: string;
}

export interface Approval {
  id: string;
  session: string;
  /** `question`: AskUserQuestion — its input holds the questions, its view what is answered (QuestionView). */
  kind: "approval" | "courier" | "question";
  /** The CLI's tool_use id of the call: the card is placed right after the transcript item of that call. */
  tool_use_id: string;
  state: ApprovalState;
  tool: string;
  input: Record<string, unknown>;
  cwd: string;
  digest: string;
  view: CardView;
  created: number;
  at: number;
}

export interface Summary {
  session: string;
  /** The note's number (the event key is `<session>/<seq>`); absent, the summary is keyed by `at`. */
  seq?: number;
  head: string;
  text: string;
  at: number;
  /** The turn it was said at the end of: shown right after that turn's items. Absent on old summaries. */
  turn?: number | null;
  /** A ping (retinue/pings.py): done, waits on the owner's choice, or broke. Null on summaries from before pings. */
  kind?: PingKind | null;
  /** Seconds since the owner last acted on the session when the turn ended. */
  took_s?: number | null;
  /** The suggested answers still pressable (≤3); emptied once one went in, the owner wrote, or a newer ping came. */
  replies?: string[];
  /** The reply that went in, from here or from Telegram. */
  answered?: string | null;
  /** The replies line exactly as the model wrote it at the end of the turn's last message: the face hides it there. */
  line?: string | null;
}

export type PingKind = "done" | "decision" | "failed";

/** One file of a «В Базу» card: A/M/D, its lines added and removed (null: not counted), turned on, also changed in
 * another session's run. */
export interface BaseFile {
  path: string;
  status: "A" | "M" | "D" | string;
  add: number | null;
  del: number | null;
  on: boolean;
  shared: boolean;
  /** No session's run changed it, yet it is uncommitted: listed turned off, for the owner to include. */
  nobody?: boolean;
}

export type BaseCardState = "shown" | "committing" | "committed" | "failed" | "cancelled" | "stale" | "expired";

/** «В Базу»: a session's commit card (retinue/tobase.py), the same object as its Telegram copies. */
export interface BaseCard {
  id: string;
  session: string;
  state: BaseCardState;
  /** The session's sphere or track, as the card's header names it. */
  place: string;
  files: BaseFile[];
  subject: string;
  /** The owner set the subject; otherwise it is the default `<Space>: <title>`. */
  custom: boolean;
  /** How many files other sessions have uncommitted, and in how many sessions — never which. */
  others: { files: number; sessions: number };
  sha: string | null;
  /** The result in words: committed, what was refused and why, stale, expired. */
  said: string;
  refused: string[];
  committed: string[];
  created: number;
  at: number;
  view: CardView;
}

/** What came with an owner's message (retinue/inbox.py `Ready.meta`): a picture or a PDF went in as a block, a file
 * into the session's inbox/. A picture has a thumbnail at `/api/sessions/{session}/inputs/{input_id}/{n}`. */
export interface Attachment {
  kind: "image" | "pdf" | "file" | string;
  name: string;
  size: number;
  what: string;
  thumb?: boolean;
}

/** An owner's message to the session, placed before the item (`turn`, `n`) the session had not yet written. */
export interface Input {
  session: string;
  input_id: string;
  text: string;
  at: number;
  turn: number | null;
  n: number | null;
  attachments: Attachment[];
}

/** A file a session handed over from `out/` (retinue/outfiles.py): kept by value, served by `/api/files/{id}`. */
export interface OutFile {
  id: string;
  session: string;
  /** Under `out/`, with `/`. */
  path: string;
  name: string;
  size: number;
  /** From the platform's list, by extension: never what the file says it is. */
  mime: string;
  at: number;
}

// --- transcript items (retinue/transcript.py) -------------------------------------------------------------------

export interface ReadCall {
  id: string;
  tool: "Read" | "Grep" | "Glob" | string;
  input: Record<string, unknown>;
  result: string | null;
  error: boolean;
}

export type ItemData =
  | { kind: "message"; data: { role: "assistant" | "user"; text: string } }
  | { kind: "reads"; data: { calls: ReadCall[] } }
  | {
      kind: "edit";
      data: {
        id: string;
        tool: "Edit" | "Write" | "MultiEdit" | "NotebookEdit" | string;
        path: string;
        ok: boolean | null;
        error: string;
        old?: string;
        new?: string;
        edits?: { old: string; new: string }[];
        content?: string;
      };
    }
  | {
      kind: "plan";
      data: {
        id: string;
        tool: "ExitPlanMode" | "TodoWrite" | string;
        plan?: string;
        todos?: { content: string; status: "pending" | "in_progress" | "completed" | string; activeForm?: string }[];
      };
    }
  | {
      kind: "subagent";
      data: {
        id: string;
        tool: string;
        description: string;
        agent: string;
        prompt: string;
        items: ItemData[];
        result: string | null;
      };
    }
  | {
      kind: "question";
      data: {
        id: string;
        questions: {
          question: string;
          header?: string;
          options?: { label: string; description?: string }[];
          multiSelect?: boolean;
        }[];
        answer: string | null;
      };
    }
  | {
      kind: "tool";
      data: { id: string; tool: string; input: Record<string, unknown>; result: string | null; error: boolean };
    }
  | {
      kind: "result";
      data: { stop_reason: string; is_error: boolean; text: string; deferred: Record<string, unknown> | null };
    };

export type Item = ItemData & { session: string; turn: number; n: number };

// --- endpoints --------------------------------------------------------------------------------------------------

export interface SessionsSnapshot {
  schema: number;
  cursor: number;
  sessions: Session[];
}

export interface SessionSnapshot {
  schema: number;
  cursor: number;
  session: Session;
  items: Item[];
  approvals: Approval[];
  summaries: Summary[];
  /** The owner's messages; absent from a platform before attachments. */
  inputs?: Input[];
  /** The files the session handed over, oldest first; absent from a platform before them. */
  files?: OutFile[];
  /** Its «В Базу» cards: the open one and the last few decided; absent from a platform before the Base. */
  base_cards?: BaseCard[];
  /** Its «Запушить» cards, the same way; absent from a platform before the code. */
  push_cards?: PushCard[];
  /** What «Сводка» says of it beyond its row; absent from a platform before it. */
  about?: About;
}

/** A session's environment as the platform was told it (retinue/sessions.py `face_about`): null where nothing said. */
export interface About {
  cwd: string | null;
  /** What its main conversation's last request carried: input and cache tokens. */
  context_tokens: number | null;
  model: string | null;
  cli_version: string | null;
  permission_mode: string | null;
  /** The MCP servers of its newest run, as its init line named them. */
  mcp: { name: string; status: string }[];
}

export interface BaseResponse {
  toast: string;
  card: BaseCard | null;
}

export type PushCardState = "shown" | "pushing" | "pushed" | "cancelled" | "stale" | "expired";

/** «Запушить»: a repository's push card (retinue/topush.py), the same object as its Telegram copies. */
export interface PushCard {
  id: string;
  session: string;
  repo: string;
  state: PushCardState;
  place: string;
  /** The room's branch, the remote branch it goes to, the default branch the PR goes into. */
  branch: string;
  target: string;
  base: string;
  /** The owner named the branch. */
  custom: boolean;
  title: string;
  /** What leaves: every commit the remote has nowhere, newest first, with its whole message and its own files. */
  commits: {
    sha: string;
    subject: string;
    message?: string;
    /** «Name <email>, 2026-10-27 21:30 +0000», as the commit object says it. */
    author?: string;
    committer?: string;
    parents?: string[];
    files?: { path: string; status: string }[];
  }[];
  /** The PR's whole change: merge-base → the tip. */
  files: { path: string; status: string; add: number | null; del: number | null }[];
  ahead: number;
  /** The commits, objects and bytes a push carries. */
  unpushed?: number;
  objects?: number;
  bytes?: number;
  /** Files under .github/workflows: the token cannot push them; «Отправить» refuses. */
  workflows: string[];
  /** What in the work tree is not committed: it does not go. */
  dirty: string[];
  partial: boolean;
  /** The pull or merge request, once pushed. */
  url: string;
  said: string;
  created: number;
  at: number;
  view: { head: string[]; body: string; status: string };
}

export interface PushResponse {
  toast: string;
  card: PushCard | null;
}

export interface DiffResponse {
  cwd: string;
  stat: string;
  diff: string;
  truncated: boolean;
}

export interface DecideResponse {
  toast: string;
  approval: Approval;
}

/** What the face sends for one question: the options picked, or the owner's own text. */
export type QuestionReply = { labels: string[] } | { text: string };

// --- the stream -------------------------------------------------------------------------------------------------

export type StreamEvent =
  | { kind: "session"; data: Session }
  | { kind: "item"; data: Item }
  | { kind: "items_reset"; data: { session: string; turn: number; count: number } }
  | { kind: "approval"; data: Approval }
  | { kind: "summary"; data: Summary }
  | { kind: "input"; data: Input }
  | { kind: "file"; data: OutFile }
  | { kind: "base"; data: BaseCard }
  | { kind: "push"; data: PushCard };

export type EventKind = StreamEvent["kind"];
export const EVENT_KINDS: readonly EventKind[] = [
  "session",
  "item",
  "items_reset",
  "approval",
  "summary",
  "input",
  "file",
  "base",
  "push",
];
