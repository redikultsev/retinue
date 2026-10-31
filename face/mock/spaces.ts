// «Spaces» in the mock (retinue/spaces.py's answers): a small synthetic Base — two Spaces, a few Records.
import type { RecordView, SpaceRow, SpaceView } from "../src/lib/space-types.ts";

const HEAD = "0123456789abcdef0123456789abcdef01234567";

const travel: SpaceView = {
  name: "travel",
  title: "Travel",
  rules: "## Rules of the domain\n\n- Live prices only, with a link.\n- Compare tickets on two sites.",
  marker: true,
  counts: { questions: 2, tasks: 1 },
  sections: [
    { heading: "Tasks", kind: "tasks", items: [{ text: "Recheck the [transfer](/spaces/travel/r/knowledge/transit.md) rules before 01.05.", list: true }] },
    {
      heading: "Open questions",
      kind: "questions",
      items: [
        { text: "Transfer through the hub or fly direct?", list: true },
        { text: "Is insurance covering active holidays needed?", list: true },
      ],
    },
  ],
  records: {
    knowledge: [
      { path: "knowledge/transit.md", folder: "", title: "Airport transfer", verified: "2026-09-21", practice: "diverges", stale_after: "2026-12-21", status: "current" },
      { path: "knowledge/airport-hotels.md", folder: "", title: "Airport hotels", verified: "2026-10-02", status: "current" },
    ],
    profile: [{ path: "profile/docs/passport.md", folder: "docs", title: "Passport", from: "2020-01-01", until: "2030-01-01" }],
    journal: [{ path: "journal/2026-10-01-baggage.md", folder: "", title: "Taking tickets with baggage included", type: "decision", date: "2026-10-01" }],
    artifacts: [],
  },
};

const work: SpaceView = {
  name: "work",
  title: "Work",
  rules: "",
  marker: true,
  counts: { questions: 0, tasks: 0 },
  sections: [],
  records: { knowledge: [], profile: [], journal: [], artifacts: [] },
};

const records: Record<string, RecordView> = {
  "travel/knowledge/transit.md": {
    space: "travel",
    path: "knowledge/transit.md",
    nature: "knowledge",
    title: "Airport transfer",
    head: {
      written_by: "Claude Code (Opus 5)", created: "2026-09-21", updated: "2026-10-02", id: "transit", title: "Airport transfer",
      type: "knowledge", source: "The carrier's rules, https://example.org/rules", verified: "2026-09-21",
      stale_after: "2026-12-21", status: "current", practice: "diverges",
    },
    bad_head: false,
    body: "# Airport transfer\n\n## In short\n\n- By the rules 40 minutes is enough for a transfer.\n- In practice it takes at least an hour and a half.\n\nSee the [passport](/spaces/travel/r/profile/docs/passport.md).",
  },
  "travel/profile/docs/passport.md": {
    space: "travel",
    path: "profile/docs/passport.md",
    nature: "profile",
    title: "Passport",
    head: { written_by: "Владелец", created: "2026-09-01", id: "passport", type: "profile", from: "2020-01-01", until: "2030-01-01", learned: "2026-09-01", source: "passport" },
    bad_head: false,
    body: "Valid until January 2030.",
  },
};

const views: Record<string, SpaceView> = { travel, work };

/** `GET /api/spaces…`, or null when the path is not one of them. */
export function mockSpaces(path: string): { status: number; data: unknown } | null {
  if (path === "/api/spaces") {
    const spaces: SpaceRow[] = Object.values(views).map((v) => ({ name: v.name, title: v.title, ...v.counts, waiting: 0, working: 0 }));
    return { status: 200, data: { head: HEAD, spaces } };
  }
  const m = /^\/api\/spaces\/([^/]+)(?:\/r\/(.+))?$/.exec(path);
  if (!m) return null;
  const view = views[decodeURIComponent(m[1]!)];
  if (!view) return { status: 404, data: { error: "No such Space." } };
  if (!m[2]) return { status: 200, data: { head: HEAD, space: view } };
  const record = records[`${view.name}/${decodeURIComponent(m[2])}`];
  return record ? { status: 200, data: { head: HEAD, record } } : { status: 404, data: { error: "No such Record." } };
}
