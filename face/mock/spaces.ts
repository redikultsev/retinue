// «Spaces» in the mock (retinue/spaces.py's answers): a small synthetic Base — two Spaces, a few Records, and in
// «Work» a project three folders deep (`work/acme/site`) with a file of each kind.
import type { ProjectFile, ProjectNode, RecordView, SpaceRow, SpaceView } from "../src/lib/space-types.ts";

const HEAD = "0123456789abcdef0123456789abcdef01234567";

const travel: SpaceView = {
  name: "travel",
  path: "travel",
  parent: null,
  title: "Travel",
  passport: {
    why: "Trips without surprises: tickets, stays and documents for every trip in one place, checked against the carriers' own rules.",
    in_scope: "flights, trains, stays, transfers, travel documents and insurance",
    not_in_scope: "visas → Documents; spending on trips → Money",
  },
  rules: "## Rules of the domain\n\n- Live prices only, with a link.\n- Compare tickets on two sites.",
  projects: [{ name: "travel/lisbon", title: "Lisbon in May", questions: 1, tasks: 0 }],
  files: [
    { path: "AGENTS.md", size: 1240, kind: "markdown" },
    { path: "journal/2026-05-07-lisbon.md", size: 860, kind: "markdown" },
    { path: "knowledge/transit.md", size: 2100, kind: "markdown" },
    { path: "out/day-by-day.md", size: 640, kind: "markdown" },
  ],
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

const NO_RECORDS = { knowledge: [], profile: [], journal: [], artifacts: [] };
const showcase: ProjectFile = { path: "AGENTS.md", size: 240, kind: "markdown" };

/** A project of the mock: its Showcase alone, its sub-projects by folder and title; its passport when it has one. */
function project(path: string, title: string, parent: string | null, sub: [string, string][], files: ProjectFile[] = [showcase], why?: [string, string, string]): SpaceView {
  const projects = sub.map(([name, t]) => ({ name, title: t, questions: 0, tasks: 0 }));
  const passport = why ? { why: why[0], in_scope: why[1], not_in_scope: why[2] } : null;
  return { name: path, path, parent, title, passport, rules: "", marker: true, counts: { questions: 0, tasks: 0 }, sections: [], records: NO_RECORDS, files, projects };
}

const work = project("work", "Work", null, [["work/acme", "Acme"]], [showcase], [
  "Paid work: clients, their sites and the invoices, each client a project of its own.",
  "clients, contracts, their sites and launches",
  "the household's bills → Home",
]);
const reading = project("reading", "Reading", null, [], [showcase], [
  "What to read next and what stayed from what was read: a plan by month, notes per book.",
  "books, the club's plan, notes and quotes",
  "courses and lessons → Learning",
]);
const lisbon: SpaceView = {
  ...project("travel/lisbon", "Lisbon in May", "travel", [], [showcase], [
    "Five nights in Lisbon, 7–12 May: a place to stay, a route by day, the trains north.",
    "the stay, the day plan, day trips, trains in Portugal",
    "the flights there and back → Travel",
  ]),
  counts: { questions: 1, tasks: 0 },
  sections: [{ heading: "Open questions", kind: "questions", items: [{ text: "The old town or by the river?", list: true }] }],
};
const projects: Record<string, SpaceView> = {
  "travel/lisbon": lisbon,
  "work/acme": project("work/acme", "Acme", "work", [["work/acme/site", "Site"]], [showcase], [
    "The Acme company's site and its move to a new host, without the site going down.",
    "the site, its host, its launch post",
    "Acme's accounting → Money",
  ]),
  "work/acme/site": project("work/acme/site", "Site", "work/acme", [["work/acme/site/blog", "Blog"]], [
    showcase,
    { path: "brief.pdf", size: 0, kind: "pdf" },
    { path: "media/demo.mp4", size: 0, kind: "video" },
    { path: "media/jingle.wav", size: 0, kind: "audio" },
    { path: "mockup.png", size: 0, kind: "image" },
  ]),
  "work/acme/site/blog": project("work/acme/site/blog", "Blog", "work/acme/site", []),
};

// The files themselves, made here: a 1×1 picture, a one-page PDF, half a second of silence, a few bytes as a film.
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
const PDF = Buffer.from(
  "%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n" +
    "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n",
);
function silence(): Buffer {
  const data = 4000; // 0.5 s of 8 kHz 8-bit mono
  const h = Buffer.alloc(44);
  h.write("RIFF", 0); h.writeUInt32LE(36 + data, 4); h.write("WAVEfmt ", 8); h.writeUInt32LE(16, 16); h.writeUInt16LE(1, 20);
  h.writeUInt16LE(1, 22); h.writeUInt32LE(8000, 24); h.writeUInt32LE(8000, 28); h.writeUInt16LE(1, 32); h.writeUInt16LE(8, 34);
  h.write("data", 36); h.writeUInt32LE(data, 40);
  return Buffer.concat([h, Buffer.alloc(data, 128)]);
}
const FILES: Record<string, { type: string; body: Buffer }> = {
  "work/acme/site/AGENTS.md": { type: "text/markdown; charset=utf-8", body: Buffer.from("# Site\n\nThe company site: pages, blog, media.\n") },
  "work/acme/site/mockup.png": { type: "image/png", body: PNG },
  "work/acme/site/brief.pdf": { type: "application/pdf", body: PDF },
  "work/acme/site/media/jingle.wav": { type: "audio/wav", body: silence() },
  "work/acme/site/media/demo.mp4": { type: "video/mp4", body: Buffer.from("00000018667479706d703432", "hex") },
};
for (const [path, file] of Object.entries(FILES)) {
  const row = projects["work/acme/site"]!.files!.find((f) => `work/acme/site/${f.path}` === path);
  if (row) row.size = file.body.length;
}

/** `GET /api/base/…` and `/api/base-view/…`: a file of a project, or null. */
export function mockBase(path: string): { type: string; body: Buffer } | null {
  const m = /^\/api\/base(?:-view)?\/(.+)$/.exec(path);
  return m ? (FILES[decodeURIComponent(m[1]!)] ?? null) : null;
}

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

const views: Record<string, SpaceView> = { travel, reading, work };

/** `GET /api/spaces…`, or null when the path is not one of them. */
export function mockSpaces(path: string): { status: number; data: unknown } | null {
  if (path === "/api/spaces") {
    const spaces: SpaceRow[] = Object.values(views).map((v) => ({ name: v.name, title: v.title, ...v.counts, waiting: 0, working: 0 }));
    // every project at any depth, as retinue/spaces.py lists them beside the grid
    const every: ProjectNode[] = [...Object.values(views), ...Object.values(projects)]
      .map((v) => ({ name: v.path ?? v.name, title: v.title, parent: v.parent ?? "", ...v.counts }))
      .sort((a, b) => a.name.localeCompare(b.name));
    return { status: 200, data: { head: HEAD, spaces, projects: every } };
  }
  const p = /^\/api\/projects\/(.+)$/.exec(path);
  if (p) {
    const name = decodeURIComponent(p[1]!);
    const found = projects[name] ?? views[name];
    return found ? { status: 200, data: { head: HEAD, project: found, sessions: [] } } : { status: 404, data: { error: "No such project." } };
  }
  const m = /^\/api\/spaces\/([^/]+)(?:\/r\/(.+))?$/.exec(path);
  if (!m) return null;
  const view = views[decodeURIComponent(m[1]!)];
  if (!view) return { status: 404, data: { error: "No such Space." } };
  if (!m[2]) return { status: 200, data: { head: HEAD, space: view } };
  const record = records[`${view.name}/${decodeURIComponent(m[2])}`];
  return record ? { status: 200, data: { head: HEAD, record } } : { status: 404, data: { error: "No such Record." } };
}
