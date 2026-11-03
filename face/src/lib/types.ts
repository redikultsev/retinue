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
  /** Its Base files not written yet (retinue/tobase.py): the platform commits them after its turn. */
  base?: number;
  /** Commits in the repositories it worked in that no branch of the remote has (retinue/topush.py): «Push · N». */
  push?: number;
  /** Why a repository it worked in cannot be pushed now («<repo>: <reason>», retinue/topush.py); absent: nothing. */
  push_why?: string;
  /** When its running turn began (a resume after a card is the same turn); absent from a platform before it. */
  turn_started?: number;
  /** Its run waits for room in the lane (a sub-session beyond the room's share, retinue/subs.py). */
  queued?: boolean;
  /** The owner archived it: out of every list until a turn of it begins again (its link still opens it). */
  archived?: boolean;
  /** The Base folder it runs in (`work/acme`); absent: none, or a platform before it. */
  project?: string;
  /** Its place on the map among its project's live sessions, kept for its whole life (retinue/sessions.py
   * `take_slot`); absent: a session from before the map, or a platform before it. */
  slot?: number;
}

/** What the face says of a session whose run waits for room. */
export const QUEUED = "waiting for room on the lane";

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
  /** A courier card the owner closed («Done»): done by hand, or not to be done. */
  | "done"
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
  /** A ping (retinue/pings.py): done, waits on the owner (a card of the session is shown), or broke. */
  kind?: PingKind | null;
  /** Seconds since the owner last acted on the session when the turn ended. */
  took_s?: number | null;
  /** The card of the platform's own commit after this turn (retinue/tobase.py), while it can be undone from here. */
  auto?: string | null;
  /** What the turn wrote to the Base, in one line — or why it did not and what came next (retinue/tobase.py). */
  base_line?: string | null;
}

export type PingKind = "done" | "decision" | "failed";

/** One file of a commit to the Base: A/M/D, its lines added and removed (null: not counted). */
export interface BaseFile {
  path: string;
  status: "A" | "M" | "D" | string;
  add: number | null;
  del: number | null;
}

export type BaseCardState = "committing" | "committed" | "failed" | "undone" | "expired";

/** The platform's own commit to the Base after a turn (retinue/tobase.py). */
export interface BaseCard {
  id: string;
  session: string;
  state: BaseCardState;
  files: BaseFile[];
  /** The commit's subject: `<Space>: <session title>`. */
  subject: string;
  sha: string | null;
  /** The line the owner reads: what went where, or why nothing did and what came next. */
  said: string;
  /** «Undo» can be pressed now (committed less than seven days ago). */
  undo: boolean;
  /** The commit that undid it. */
  undone: string | null;
  refused: string[];
  committed: string[];
  created: number;
  at: number;
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
  /** Not the owner's: `parent`, a parent session's message into its sub-session; `report`, its sub-sessions' reports;
   * `platform`, the platform's own word to the session (merge a file both sides edited, fix what the Base's check refused). */
  by?: "parent" | "report" | "platform";
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

/** A row of «Archived» (`GET /api/archived`): the session as listed, and when the owner archived it. */
export type ArchivedSession = Session & { archived_at: number };

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
  /** Its last few commits to the Base (retinue/tobase.py). */
  base_cards?: BaseCard[];
  /** Its «Push» cards, the same way; absent from a platform before the code. */
  push_cards?: PushCard[];
  /** The sub-sessions it proposed, oldest first, whatever became of them. */
  proposals?: Proposal[];
  /** What «Summary» says of it beyond its row; absent from a platform before it. */
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

/** «Push»: a repository's push card (retinue/topush.py), the same object as its Telegram copies. */
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
  /** Files under .github/workflows: the token cannot push them; «Push» refuses. */
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

/** A proposal past its week reads `expired`: it can no longer be started. */
export type ProposalState = "open" | "started" | "declined" | "expired";

/** A sub-session a session proposed instead of starting it (retinue/subs.py): «Start» or «Not now». The brief is the
 * model's text, shown whole before «Start». */
export interface Proposal {
  id: string;
  /** The session that proposed it. */
  session: string;
  /** The call it was proposed by: its card sits under it in the transcript. */
  tool_use_id: string;
  title: string;
  brief: string;
  created: number;
  /** Its last change: a copy older than the store's never replaces it. */
  at: number;
  state: ProposalState;
  /** The sub-session started for it. */
  child: string | null;
  /** Proposed in a turn a sub-session's report started: the brief may carry a page the child read. */
  from_report: boolean;
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
  | { kind: "push"; data: PushCard }
  | { kind: "proposal"; data: Proposal };

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
  "proposal",
];

/** The stream's `collection` event: a collection changed — its records, its form, its views or its rights — and
`rev` is the newest change it has now (a ULID: compare as strings), `space` the Space it is in. Not kept in the state:
a Space's screen that shows the collection reads its views again, and one that does not reads itself again only when
the collection is in its Space (lib/collections.ts). */
export interface CollectionEvent {
  name: string;
  space: string | null;
  rev: string;
}

/** One seven-day span of the platform's tally (`GET /api/stats`, signals.tally). */
export interface Tally {
  since: number;
  until: number;
  sessions: number;
  sub_sessions: number;
  runs: number;
  owner: { messages: number; stops: number };
  engine: { tool_errors: number; tool_errors_by_tool: Record<string, number>; compactions: number; run_errors: number };
  gate: { calls: number; by_decision: Record<string, number>; reasons: Record<string, number> };
  cards: {
    approval: CardTally;
    question: CardTally;
    courier: number;
  };
}

export interface CardTally {
  shown: number;
  granted: number;
  answered: number;
  rejected: number;
  open: number;
  lapsed: number;
}
