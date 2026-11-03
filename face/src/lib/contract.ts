// The face API's contract (docs/face.md «API»): every path the app calls, the presses the plan takes and the fields
// each takes, and the shapes of the answers — what the app's calls are built from and what the mock (face/mock/)
// answers by, so the two cannot drift apart unseen. Type imports only: the mock runs this file under Node as it is.
import type { Press as PlanPress } from "./plan";

export type { Feed, FeedGroup, FeedRow, Press as RowPress, TurnEnding, WaitingDetail } from "./feed";
export type { Library, LibraryPage } from "./library";
export type { Activity, Block, Plan, PlanReminder, PlanWindow, Press, Task, TaskFields } from "./plan";

const seg = (s: string) => encodeURIComponent(s);

/** Every path of the face API the app calls. A segment given as `at` is one encoded already (lib/spaces.ts `segs`). */
export const paths = {
  meta: "/api/meta",
  stats: "/api/stats",
  speech: (mode: string) => `/api/speech?mode=${mode}`,
  events: (since: number) => `/api/events?since=${since}`,
  feed: "/api/feed",
  waitingPress: "/api/waiting/press",
  waitingDetail: "/api/waiting/detail",
  plan: "/api/plan",
  planPress: "/api/plan/press",
  sessions: "/api/sessions",
  archived: "/api/archived",
  session: (id: string) => `/api/sessions/${seg(id)}`,
  /** A session's own route: `input`, `pin`, `archive`, `diff`, `push`, … */
  sessionDo: (id: string, what: string) => `/api/sessions/${seg(id)}/${what}`,
  thumb: (session: string, input: string, n: number) => `/api/sessions/${seg(session)}/inputs/${seg(input)}/${n}`,
  approval: (id: string) => `/api/approvals/${seg(id)}`,
  approvalDo: (id: string, what: "decide" | "done" | "answer") => `/api/approvals/${seg(id)}/${what}`,
  file: (id: string) => `/api/files/${seg(id)}`,
  base: (id: string) => `/api/base/${seg(id)}`,
  push: (id: string) => `/api/push/${seg(id)}`,
  proposal: (id: string, action: "start" | "dismiss") => `/api/proposals/${seg(id)}/${action}`,
  auth: (how: "code" | "telegram" | "logout") => `/api/auth/${how}`,
  library: "/api/library",
  libraryPage: (id: string, what: "open" | "done") => `/api/library/${seg(id)}/${what}`,
  spaces: "/api/spaces",
  space: (name: string) => `/api/spaces/${seg(name)}`,
  spaceRecord: (name: string, at: string) => `/api/spaces/${seg(name)}/r/${at}`,
  project: (at: string) => `/api/projects/${at}`,
  baseFile: (at: string) => `/api/base/${at}`,
  baseView: (at: string) => `/api/base-view/${at}`,
  /** A collection: its writes (POST), one view of it (`?view=`). */
  collection: (name: string) => `/api/c/${seg(name)}`,
  collectionView: (name: string, view: string) => `/api/c/${seg(name)}?view=${seg(view)}`,
  record: (collection: string, id: string) => `/api/c/${seg(collection)}/${seg(id)}`,
  form: (collection: string) => `/api/c/${seg(collection)}/_form`,
  spaceViews: (space: string) => `/api/c/_space?space=${seg(space)}`,
  changes: (space: string, before?: string) => `/api/c/_changes?space=${seg(space)}${before ? `&before=${seg(before)}` : ""}`,
  proposals: (space?: string) => `/api/c/_proposals${space ? `?space=${seg(space)}` : ""}`,
  collectionProposal: (id: string, action: "apply" | "dismiss") => `/api/c/_proposals/${seg(id)}/${action}`,
} as const;

/** A press on the plan (`POST /api/plan/press`) by its action, and the fields that action takes — retinue/plan.py's
 * `PRESSES` and `clean_press`, kept equal to them by tests/test_planner.py. «Place» takes a task, or a person's row
 * (`t`) or a reminder (`ref`) for the Router to make its task first. */
export const PRESS_FIELDS: { readonly [A in PlanPress["action"]]: readonly string[] } = {
  plan: ["day", "days"],
  place: ["day", "task", "t", "ref"],
  undo: ["plan"],
  busy: ["minutes"],
  free: [],
  remove: ["block"],
  hours: ["opens", "closes", "zone"],
  now: ["activity"],
  activities: ["activities"],
  off: [],
};

export const PRESSES = Object.keys(PRESS_FIELDS) as PlanPress["action"][];
