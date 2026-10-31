// A Space's views in the mock (retinue/views.py's answers): the «work» Space has two made-up collections — deals on a
// board, follow-ups in a queue — plus a list too short for a board and a view whose field its form lacks; «travel»
// has none and shows its text alone. `POST /api/_mock/collection` moves a deal on, as a write would.
import { randomBytes } from "node:crypto";
import type { CollectionViews, OneView, SpaceViews, View, ViewRow } from "../src/lib/view-types.ts";

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
let lastMs = 0;

/** A ULID at `ms`, or just past the last one: they only grow. */
function ulid(ms = Date.now()): string {
  lastMs = Math.max(ms, lastMs + 1);
  let time = "";
  for (let n = lastMs, i = 0; i < 10; i++, n = Math.floor(n / 32)) time = CROCKFORD[n % 32] + time;
  return time + [...randomBytes(16)].map((b) => CROCKFORD[b % 32]).join("");
}

const DAY_MS = 86_400_000;
const iso = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const TZ = "Europe/Berlin";

let seq = 0;
const row = (fields: Record<string, unknown>, days: number | null, rot: number | null, over: Partial<ViewRow> = {}): ViewRow => ({
  id: ulid(Date.now() - 40 * DAY_MS + seq++),
  version: 1,
  fields,
  refs: {},
  src: null,
  days,
  rotten: rot !== null && days !== null && days > rot,
  ...over,
});

const STAGES = ["lead", "qualified", "proposal", "negotiation", "won", "lost"] as const;
const STUCK = 7;

const deals: Record<(typeof STAGES)[number], ViewRow[]> = {
  lead: [
    row({ company: "Globex", contact: "Anna Petrova", value: 12000 }, 11, STUCK, { src: "web:https://example.com/leads/globex" }),
    row({ company: "Vandelay Industries", value: 4000 }, 2, STUCK),
  ],
  qualified: [
    row({ company: "Initech", contact: "Ivan Example", value: 30000 }, 9, STUCK),
    row({ company: "Example Ltd", value: 8500 }, 4, STUCK),
    row({ company: "Soylent", value: 2000 }, 1, STUCK),
  ],
  proposal: [row({ company: "Acme", contact: "Jane Example", value: 54000 }, 3, STUCK)],
  negotiation: [],
  won: [row({ company: "Wonka Industries", value: 7000 }, 20, STUCK)],
  lost: [
    row({ company: "Umbrella Corp", reason: "went with a cheaper vendor" }, 30, STUCK),
    row({ company: "Hooli", reason: "no budget this year" }, 25, STUCK),
  ],
};

const companies = Object.values(deals).flat();
const refOf = (company: string) => companies.find((r) => r.fields.company === company)!;

const followups = [
  { company: "Initech", channel: "mail", state: "drafted", days: 4 },
  { company: "Globex", channel: "phone", state: "drafted", days: 3 },
  { company: "Acme", channel: "mail", state: "drafted", days: 1 },
  { company: "Example Ltd", channel: "chat", state: "drafted", days: 0 },
  { company: "Soylent", channel: "mail", state: "drafted", days: -2 },
].map((f) =>
  row({ deal: refOf(f.company).id, channel: f.channel, state: f.state, date: iso(Date.now() - f.days * DAY_MS) }, f.days, 2, {
    refs: { deal: f.company },
  }),
);

let dealsRev = ulid(Date.now() - 2 * 3600_000);
const followupsRev = ulid(Date.now() - 26 * 3600_000);

function week(now = new Date()) {
  const monday = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - ((now.getUTCDay() + 6) % 7)));
  return { first: iso(monday.getTime()), last: iso(monday.getTime() + 6 * DAY_MS) };
}

function dealViews(): View[] {
  const moved = deals.won.length + deals.proposal.length;
  return [
    { name: "moved", kind: "stat", title: "Deals moved on", value: moved, goal: null, previous: 1, window: "week", ...week() },
    { name: "revenue", kind: "stat", title: "Revenue", error: "field not found: 'amount'" },
    {
      name: "pipeline",
      kind: "board",
      title: "Pipeline",
      group: "stage",
      rot: STUCK,
      fields: ["company", "contact", "value", "reason"],
      columns: STAGES.map((value) => ({ value, count: deals[value].length, folded: value === "lost", rows: deals[value] })),
      dropped: 0,
    },
  ];
}

function followupViews(): View[] {
  const done = 3;
  return [
    { name: "made", kind: "stat", title: "Follow-ups sent myself", value: done, goal: 10, previous: 7, window: "week", ...week() },
    { name: "replied", kind: "stat", title: "Replies this month", value: 12, goal: 10, previous: 9, window: "month", first: `${iso(Date.now()).slice(0, 8)}01`, last: iso(Date.now()) },
    { name: "my_move", kind: "queue", title: "My move", rot: 2, fields: ["deal", "channel", "date"], count: followups.length, rows: followups, dropped: 0 },
  ];
}

const partners: View[] = [
  {
    name: "partners",
    kind: "board",
    title: "Partners",
    group: "status",
    rot: null,
    fields: ["name"],
    columns: [
      { value: "talking", count: 1, folded: false, rows: [row({ name: "Example Studio" }, 5, null)] },
      { value: "signed", count: 1, folded: false, rows: [row({ name: "Acme Hosting" }, 40, null)] },
    ],
    dropped: 0,
  },
];

function collections(space: string): CollectionViews[] {
  if (space !== "work") return [];
  return [
    { name: "deals", rev: dealsRev, views: dealViews() },
    { name: "followups", rev: followupsRev, views: followupViews() },
    { name: "partners", rev: ulid(Date.now() - 9 * DAY_MS), views: partners },
    { name: "suppliers", rev: null, views: [{ name: "open", kind: "queue", title: "Suppliers to call", rot: null, fields: ["name"], count: 0, rows: [], dropped: 0 }] },
  ];
}

const moment = () => ({ tz: TZ, today: iso(Date.now()), at: Date.now() / 1000 });

/** `GET /api/c/_space?space=` and `GET /api/c/<collection>?view=`, or null when the path is not one of them. */
export function mockCollections(path: string, query: URLSearchParams): { status: number; data: unknown } | null {
  if (path === "/api/c/_space") {
    const space = query.get("space") ?? "";
    return { status: 200, data: { space, ...moment(), collections: collections(space) } satisfies SpaceViews };
  }
  const m = /^\/api\/c\/([a-z][a-z0-9_]*)$/.exec(path);
  if (!m) return null;
  const collection = collections("work").find((c) => c.name === m[1]);
  const view = collection?.views.find((v) => v.name === query.get("view"));
  if (!collection || !view) return { status: 404, data: { error: "No such view or record." } };
  return { status: 200, data: { name: collection.name, rev: collection.rev, ...moment(), view } satisfies OneView };
}

/** A deal moves one column on (the last open one's first card comes back to the start): the stream's
 * `collection` event, `{name, rev}`. */
export function moveDeal(): { name: string; rev: string } {
  const open = STAGES.filter((s) => s !== "lost");
  const from = open.find((s) => deals[s].length) ?? "lead";
  const to = open[(open.indexOf(from) + 1) % open.length]!;
  const card = deals[from].shift()!;
  deals[to].unshift({ ...card, version: card.version + 1, days: 0, rotten: false });
  dealsRev = ulid();
  return { name: "deals", rev: dealsRev };
}
