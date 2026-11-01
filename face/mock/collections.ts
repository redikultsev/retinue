// A Space's views in the mock (retinue/views.py's answers), and the owner's writes to them: the «work» Space has two
// made-up collections — deals on a board, follow-ups in a queue with «Done» and «Add a follow-up» — plus a list too
// short for a board and a view whose field its form lacks; «travel» has none and shows its text alone. Deals and
// follow-ups are records kept here with their versions and changes, so a card moved, a row done, a record added and
// a change undone show as the platform would show them. `POST /api/_mock/collection` moves a deal on as another
// writer would — `?quiet=1` without telling the app, for the next write of it to meet a conflict.
import { randomBytes } from "node:crypto";
import type {
  Change,
  ChangesAnswer,
  CollectionProposal,
  CollectionRecord,
  CollectionViews,
  FieldSpec,
  FormAnswer,
  OneView,
  Op,
  RecordAnswer,
  SpaceChange,
  SpaceViews,
  View,
  ViewRow,
  WriteAnswer,
} from "../src/lib/view-types.ts";

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
const STUCK = 7;
const REPLY = 2;
const DELETED = "_deleted";

const STAGES = ["lead", "qualified", "proposal", "negotiation", "won", "lost"];
const FIELDS: Record<"deals" | "followups", Record<string, FieldSpec>> = {
  deals: {
    company: { type: "text", required: true },
    contact: { type: "text" },
    value: { type: "int" },
    stage: { type: "enum", values: STAGES, required: true, labels: { negotiation: "In negotiation" } },
    next: { type: "text" },
    call: { type: "date" },
    found: { type: "date" },
    reason: { type: "text" },
  },
  followups: {
    deal: { type: "ref", to: "deals", required: true },
    date: { type: "date", required: true },
    channel: { type: "enum", values: ["mail", "phone", "chat"], required: true },
    state: { type: "enum", values: ["draft", "done"], required: true, labels: { draft: "Not sent" } },
    note: { type: "text" },
  },
};
type Name = keyof typeof FIELDS;

interface Rec extends CollectionRecord {
  collection: Name;
}

const records = new Map<string, Rec>();
const changes: Change[] = []; // the oldest first
const revs: Record<Name, string> = { deals: "", followups: "" };

function change(
  collection: Name,
  id: string,
  op: Change["op"],
  before: Record<string, unknown> | null,
  after: Record<string, unknown> | null,
  ms: number,
  actor = "owner",
  extra: Partial<Change> = {},
): Change {
  const made: Change = {
    change_id: ulid(ms),
    ts: ms / 1000,
    as_of: null,
    batch: null,
    collection,
    id,
    op,
    before,
    after,
    actor,
    model: null,
    via: actor === "owner" ? "screen" : actor === "assistant" ? "assistant" : "import",
    src: null,
    foreign: [],
    reverts: null,
    idem: null,
    ...extra,
  };
  changes.push(made);
  revs[collection] = made.change_id;
  return made;
}

function seed(collection: Name, fields: Record<string, unknown>, ageDays: number): Rec {
  const ms = Date.now() - ageDays * DAY_MS;
  const id = ulid(ms);
  const rec: Rec = { collection, id, fields, version: 1, created: ms / 1000, updated: ms / 1000, deleted: null };
  records.set(id, rec);
  change(collection, id, "add", null, fields, ms, "import:01J8ZQ4Y7Z5G0000000000IMPT", { via: "import" });
  return rec;
}

const ahead = (days: number) => iso(Date.now() + days * DAY_MS);
const DEALS: [Record<string, unknown>, number][] = [
  [{ company: "Globex", contact: "Anna Petrova", value: 12000, stage: "lead", next: "Send the price list", found: ahead(-40) }, 11],
  [{ company: "Vandelay Industries", value: 4000, stage: "lead", found: ahead(-12) }, 2],
  [{ company: "Initech", contact: "Ivan Example", value: 30000, stage: "qualified", next: "Demo call, agree the pilot", call: ahead(1), found: ahead(-20) }, 9],
  [{ company: "Example Ltd", value: 8500, stage: "qualified", found: ahead(-9) }, 4],
  [{ company: "Soylent", value: 2000, stage: "qualified", found: ahead(-3) }, 1],
  [{ company: "Acme", contact: "Jane Example", value: 54000, stage: "proposal", next: "Answer the legal questions", call: ahead(3), found: ahead(-25) }, 3],
  [{ company: "Wonka Industries", value: 7000, stage: "won" }, 20],
  [{ company: "Umbrella Corp", reason: "went with a cheaper vendor", stage: "lost" }, 30],
  [{ company: "Hooli", reason: "no budget this year", stage: "lost" }, 25],
];
const seeded = DEALS.map(([fields, days]) => seed("deals", fields, days));
const dealOf = (company: string) => seeded.find((r) => r.fields.company === company)!;
for (const [company, channel, days, note] of [
  ["Initech", "mail", 4, "Asked for the pilot's scope and dates"],
  ["Globex", "phone", 3, "Call back about the price list"],
  ["Acme", "mail", 1, "Answer the legal team's questions"],
  ["Example Ltd", "chat", 0, "Confirm the delivery window"],
  ["Soylent", "mail", -2, "Check in after their board meeting"],
] as const)
  seed("followups", { deal: dealOf(company).id, date: iso(Date.now() - days * DAY_MS), channel, state: "draft", note }, Math.max(days, 0));
{
  // the assistant moved one on from a mail an hour ago: its change shows with its mark
  const acme = dealOf("Acme");
  const at = Date.now() - 3600_000;
  acme.fields.stage = "proposal";
  acme.version += 1;
  acme.updated = at / 1000;
  change("deals", acme.id, "set", { stage: "qualified" }, { stage: "proposal" }, at, "assistant", { foreign: ["mail"], src: "mail:work/<msg-1@example.com>" });
}

/** Every enum of a collection's form, with its words. */
const labelsOf = (name: Name) =>
  Object.fromEntries(Object.entries(FIELDS[name]).flatMap(([f, spec]) => (spec.type === "enum" ? [[f, spec.labels ?? {}]] : [])));

const live = (collection: Name) => [...records.values()].filter((r) => r.collection === collection && r.deleted === null);

/** When a record's field last changed (its `add` when never since), in ms. */
function since(rec: Rec, field: string): number {
  for (let i = changes.length - 1; i >= 0; i--) {
    const c = changes[i]!;
    if (c.id === rec.id && (c.op === "add" || (c.after && field in c.after))) return c.ts * 1000;
  }
  return rec.created * 1000;
}

const daysSince = (ms: number) => Math.floor((Date.now() - ms) / DAY_MS);

function row(rec: Rec, fields: string[], days: number | null, rot: number | null): ViewRow {
  const refs: Record<string, string> = {};
  for (const f of fields) {
    const target = FIELDS[rec.collection][f]?.type === "ref" ? records.get(String(rec.fields[f])) : undefined;
    if (target && target.deleted === null) refs[f] = String(target.fields.company);
  }
  const shown = Object.fromEntries(Object.entries(rec.fields).filter(([k]) => fields.includes(k)));
  const last = [...changes].reverse().find((c) => c.id === rec.id && c.src);
  return { id: rec.id, version: rec.version, fields: shown, refs, src: last?.src ?? null, days, rotten: rot !== null && days !== null && days > rot };
}

function week(now = new Date()) {
  const monday = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - ((now.getUTCDay() + 6) % 7)));
  return { first: iso(monday.getTime()), last: iso(monday.getTime() + 6 * DAY_MS), start: monday.getTime() };
}

/** The changes a revert took back. */
const takenBack = () => new Set(changes.flatMap((c) => (c.reverts ? [c.reverts] : [])));

/** As `views._changed`: a change taken back, and the revert that took it, move nothing. */
const thisWeek = (collection: Name, test: (c: Change) => boolean) => {
  const taken = takenBack();
  return new Set(
    changes
      .filter((c) => c.collection === collection && c.op !== "add" && c.op !== "revert" && !taken.has(c.change_id) && c.ts * 1000 >= week().start && test(c))
      .map((c) => c.id),
  ).size;
};

function dealViews(): View[] {
  const { first, last } = week();
  const fields = ["company", "contact", "value", "reason"];
  return [
    { name: "moved", kind: "stat", title: "Deals moved on", value: thisWeek("deals", (c) => !!c.after && "stage" in c.after), goal: null, previous: 1, window: "week", first, last },
    { name: "revenue", kind: "stat", title: "Revenue", error: "field not found: 'amount'" },
    {
      name: "calls",
      kind: "agenda",
      title: "Calls",
      date: "call",
      fields: ["company", "contact"],
      first: iso(Date.now()),
      last: ahead(6),
      count: live("deals").filter((r) => r.fields.call).length,
      rows: live("deals")
        .filter((r) => r.fields.call && String(r.fields.call) >= iso(Date.now()) && String(r.fields.call) <= ahead(6))
        .sort((a, b) => String(a.fields.call).localeCompare(String(b.fields.call)))
        .map((r) => row(r, ["company", "contact", "call"], -Math.round((Date.parse(String(r.fields.call)) - Date.parse(iso(Date.now()))) / DAY_MS), null)),
      dropped: 0,
    },
    {
      name: "weekly",
      kind: "chart",
      title: "New deals by week",
      tab: "Numbers",
      date: "found",
      group: "stage",
      values: STAGES,
      weeks: Array.from({ length: 8 }, (_, k) => {
        const start = week().start - (7 - k) * 7 * DAY_MS;
        const inWeek = live("deals").filter((r) => {
          const at = Date.parse(String(r.fields.found ?? ""));
          return at >= start && at < start + 7 * DAY_MS;
        });
        const counts = STAGES.map((value) => ({ value, count: inWeek.filter((r) => r.fields.stage === value).length })).filter((c) => c.count);
        return { first: iso(start), counts };
      }),
    },
    {
      name: "talking",
      kind: "queue",
      title: "In talks",
      mark: "wait",
      rot: STUCK,
      fields: ["company", "contact", "value"],
      count: talking().length,
      rows: talking(),
      dropped: 0,
    },
    {
      name: "pipeline",
      kind: "board",
      title: "Pipeline",
      group: "stage",
      rot: STUCK,
      fields,
      columns: STAGES.map((value) => {
        const rows = live("deals")
          .filter((r) => r.fields.stage === value)
          .map((r) => row(r, fields, daysSince(since(r, "stage")), STUCK))
          .sort((a, b) => (b.days ?? 0) - (a.days ?? 0));
        return { value, count: rows.length, folded: value === "won" || value === "lost", rows };
      }),
      dropped: 0,
      add: { label: "Add a deal", fields: ["company", "contact", "value", "stage"], values: { stage: "lead" } },
    },
  ];
}

/** The deals in talks, each with its newest follow-up — which way it went — as a queue's `latest` gives it. */
function talking(): ViewRow[] {
  return live("deals")
    .filter((r) => r.fields.stage === "qualified" || r.fields.stage === "proposal")
    .map((r) => {
      const newest = live("followups")
        .filter((f) => f.fields.deal === r.id)
        .sort((a, b) => String(b.fields.date).localeCompare(String(a.fields.date)))[0];
      const state = newest ? String(newest.fields.state) : null;
      const latest = newest && { date: String(newest.fields.date), value: state, label: state === "draft" ? "Not sent" : null, arrow: state === "done" ? ("out" as const) : null };
      return { ...row(r, ["company", "contact", "value"], daysSince(since(r, "stage")), STUCK), ...(latest ? { latest } : {}) };
    });
}

function followupViews(): View[] {
  const { first, last } = week();
  const fields = ["deal", "channel", "note", "date"];
  const rows = live("followups")
    .filter((r) => r.fields.state === "draft")
    .map((r) => row(r, fields, Math.round((Date.now() - Date.parse(String(r.fields.date))) / DAY_MS), REPLY))
    .sort((a, b) => (b.days ?? 0) - (a.days ?? 0));
  return [
    { name: "made", kind: "stat", title: "Follow-ups done", value: 3 + live("followups").filter((r) => r.fields.state === "done" && since(r, "state") >= week().start).length, goal: 10, previous: 7, window: "week", first, last },
    { name: "replied", kind: "stat", title: "Replies this month", tab: "Numbers", value: 12, goal: 10, previous: 9, window: "month", ...month() },
    {
      name: "waiting",
      kind: "queue",
      title: "Waiting on me",
      rot: REPLY,
      fields,
      count: rows.length,
      rows,
      dropped: 0,
      done: { field: "state", value: "done", label: "Done" },
      add: { label: "Add a follow-up", fields: ["deal", "date", "channel", "state", "note"], values: { state: "draft" } },
    },
  ];
}

const plain = (fields: Record<string, unknown>, days: number): ViewRow => ({ id: ulid(Date.now() - 40 * DAY_MS), version: 1, fields, refs: {}, src: null, days, rotten: false });
const partners: View[] = [
  {
    name: "partners",
    kind: "board",
    title: "Partners",
    tab: "Partners",
    group: "status",
    rot: null,
    fields: ["name"],
    columns: [
      { value: "talking", count: 1, folded: false, rows: [plain({ name: "Example Studio" }, 5)] },
      { value: "signed", count: 1, folded: false, rows: [plain({ name: "Acme Hosting" }, 40)] },
    ],
    dropped: 0,
  },
];
const partnersRev = ulid(Date.now() - 9 * DAY_MS);

function collections(space: string): CollectionViews[] {
  if (space !== "work") return [];
  return [
    { name: "deals", rev: revs.deals, labels: labelsOf("deals"), views: dealViews() },
    { name: "followups", rev: revs.followups, labels: labelsOf("followups"), views: followupViews() },
    { name: "partners", rev: partnersRev, views: partners },
    { name: "suppliers", rev: null, views: [{ name: "open", kind: "queue", title: "Suppliers to call", rot: null, fields: ["name"], count: 0, rows: [], dropped: 0 }] },
  ];
}

const moment = () => ({ tz: TZ, today: iso(Date.now()), at: Date.now() / 1000 });

function title(rec: Rec): string {
  if (rec.collection === "deals") return [rec.fields.company, rec.fields.contact].filter(Boolean).join(" · ");
  const deal = records.get(String(rec.fields.deal));
  return [deal?.deleted === null ? deal.fields.company : null, rec.fields.note].filter(Boolean).join(" · ") || "Untitled";
}

function words(values: (Record<string, unknown> | null)[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const v of values) {
    const deal = v && typeof v.deal === "string" ? records.get(v.deal) : undefined;
    if (deal && deal.deleted === null) out[deal.id] = String(deal.fields.company);
  }
  return out;
}

const shown = ({ collection: _, ...rec }: Rec): CollectionRecord => rec;

/** The GETs of `/api/c/…`, or null when the path is not one of them. */
export function mockCollections(path: string, query: URLSearchParams): { status: number; data: unknown } | null {
  if (path === "/api/c/_space") {
    const space = query.get("space") ?? "";
    return { status: 200, data: { space, ...moment(), collections: collections(space) } satisfies SpaceViews };
  }
  if (path === "/api/c/_changes") {
    const before = query.get("before");
    const all = changes.filter((c) => query.get("space") === "work" && (!before || c.change_id < before)).reverse();
    const taken = takenBack();
    const page = all.slice(0, 50).map((c): SpaceChange => {
      const rec = records.get(c.id ?? "");
      return { ...c, title: rec ? title(rec) : null, version: rec?.version ?? null, deleted: rec?.deleted ?? null, reverted: taken.has(c.change_id) };
    });
    const data: ChangesAnswer = {
      space: query.get("space") ?? "",
      changes: page,
      next: all.length > 50 ? page[page.length - 1]!.change_id : null,
      words: words(page.flatMap((c) => [c.before, c.after])),
    };
    return { status: 200, data };
  }
  const form = /^\/api\/c\/(deals|followups)\/_form$/.exec(path);
  if (form) {
    const name = form[1] as Name;
    const deals = live("deals").sort((a, b) => b.updated - a.updated);
    const data: FormAnswer = { name, rev: revs[name], fields: FIELDS[name], options: name === "followups" ? { deal: deals.map((r) => ({ id: r.id, title: title(r) })) } : {} };
    return { status: 200, data };
  }
  const one = /^\/api\/c\/(deals|followups)\/([0-9A-HJKMNP-TV-Z]{26})$/.exec(path);
  if (one) {
    const rec = records.get(one[2] ?? "");
    if (!rec || rec.collection !== one[1]) return { status: 404, data: { error: "No such view or record." } };
    const mine = changes.filter((c) => c.id === rec.id).reverse();
    const data: RecordAnswer = {
      name: rec.collection,
      rev: revs[rec.collection],
      title: title(rec),
      fields: FIELDS[rec.collection],
      record: shown(rec),
      changes: mine,
      words: words([rec.fields, ...mine.flatMap((c) => [c.before, c.after])]),
      related:
        rec.collection === "deals"
          ? [
              {
                name: "followups",
                fields: FIELDS.followups,
                date: "date",
                ref: "deal",
                records: live("followups")
                  .filter((f) => f.fields.deal === rec.id)
                  .sort((a, b) => String(b.fields.date).localeCompare(String(a.fields.date)))
                  .map(shown),
              },
            ].filter((r) => r.records.length)
          : [],
    };
    return { status: 200, data };
  }
  const m = /^\/api\/c\/([a-z][a-z0-9_]*)$/.exec(path);
  if (!m) return null;
  const collection = collections("work").find((c) => c.name === m[1]);
  const view = collection?.views.find((v) => v.name === query.get("view"));
  if (!collection || !view) return { status: 404, data: { error: "No such view or record." } };
  return { status: 200, data: { name: collection.name, rev: collection.rev, ...moment(), view } satisfies OneView };
}

/** This month's first day and its last, as the server's `window()` gives them. */
function month(): { first: string; last: string } {
  const now = new Date();
  const first = Date.UTC(now.getFullYear(), now.getMonth(), 1);
  return { first: iso(first), last: iso(Date.UTC(now.getFullYear(), now.getMonth() + 1, 0)) };
}

class Refused extends Error {
  readonly status: number;
  readonly data: Record<string, unknown>;
  constructor(status: number, data: Record<string, unknown>) {
    super(String(data.error));
    this.status = status;
    this.data = data;
  }
}

const refuse = (error: string, kind: string | null = null, record: Rec | null = null, last: Change | null = null) =>
  new Refused(kind === "stale" || kind === "after" ? 409 : 400, { error, kind, record: record ? shown(record) : null, change: last });

function at(collection: Name, id: string, version: number): Rec {
  const rec = records.get(id);
  if (!rec || rec.collection !== collection) throw refuse(`${collection} has no record '${id}'`);
  if (rec.version !== version) {
    const last = [...changes].reverse().find((c) => c.id === id) ?? null;
    throw refuse(`${id} is at version ${rec.version}, not ${version}: read it again, it may have changed since`, "stale", rec, last);
  }
  return rec;
}

function check(collection: Name, fields: Record<string, unknown>) {
  for (const [name, value] of Object.entries(fields)) {
    const spec = FIELDS[collection][name];
    if (!spec) throw refuse(`${collection} has no field '${name}'`);
    if (spec.type === "enum" && value !== null && !spec.values?.includes(String(value))) throw refuse(`'${name}' is one of ${spec.values?.join(", ")}: not '${value}'`);
  }
}

function put(rec: Rec, after: Record<string, unknown>, op: Change["op"], extra: Partial<Change> = {}): Change | null {
  const before: Record<string, unknown> = {};
  const changed: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(after)) {
    const was = k === DELETED ? rec.deleted : (rec.fields[k] ?? null);
    if (was !== v) {
      before[k] = was;
      changed[k] = v;
    }
  }
  if (!Object.keys(changed).length) return null;
  for (const [k, v] of Object.entries(changed)) {
    if (k === DELETED) rec.deleted = v as number | null;
    else if (v === null) delete rec.fields[k];
    else rec.fields[k] = v;
  }
  rec.version += 1;
  rec.updated = Date.now() / 1000;
  return change(rec.collection, rec.id, op, before, changed, Date.now(), "owner", extra);
}

function apply(collection: Name, op: Op): [Rec, Change | null] {
  if (op.op === "add") {
    check(collection, op.fields);
    const missing = Object.entries(FIELDS[collection]).filter(([k, s]) => s.required && op.fields[k] === undefined);
    if (missing.length) throw refuse(`${missing.map(([k]) => `'${k}'`).join(", ")} required`);
    const company = (r: Rec) => String(r.fields.company).trim().toLowerCase();
    const same = collection === "deals" ? live("deals").find((r) => company(r) === String(op.fields.company).trim().toLowerCase()) : undefined;
    if (same && !op.distinct) throw refuse(`possible duplicate of ${same.id}: the same company`, "duplicate", same);
    const id = ulid();
    const rec: Rec = { collection, id, fields: { ...op.fields }, version: 1, created: Date.now() / 1000, updated: Date.now() / 1000, deleted: null };
    records.set(id, rec);
    return [rec, change(collection, id, "add", null, op.fields, Date.now())];
  }
  if (op.op === "revert") {
    const target = changes.find((c) => c.change_id === op.change_id);
    if (!target || target.collection !== collection || !target.id) throw refuse(`change not found: '${op.change_id}'`);
    const rec = at(collection, target.id, op.version);
    const goal = target.op === "add" ? { [DELETED]: Date.now() / 1000 } : (target.before ?? {});
    const touched = target.op === "add" ? null : Object.keys(target.after ?? {});
    const later = changes
      .slice(changes.indexOf(target) + 1)
      .find((c) => c.id === rec.id && (!touched || Object.keys(c.after ?? {}).some((k) => touched.includes(k))));
    if (later) throw refuse(`changed after it: ${Object.keys(later.after ?? {}).join(", ")} by ${later.actor} (${later.op}, ${later.change_id})`, "after", rec, later);
    return [rec, put(rec, goal, "revert", { reverts: target.change_id })];
  }
  const rec = at(collection, op.id, op.version);
  if (op.op === "set") {
    check(collection, op.fields);
    return [rec, put(rec, op.fields, "set")];
  }
  return [rec, put(rec, { [DELETED]: op.op === "delete" ? Date.now() / 1000 : null }, op.op)];
}

/** `POST /api/c/<collection>`: the owner's operations, each at the version shown — `{status, data, moved}`, `moved`
 * the collection's event when something changed — or null when the path is not one. Not one transaction, as the
 * platform's is: a refused operation leaves those before it written. */
export function mockWrite(
  path: string,
  body: Record<string, unknown>,
): { status: number; data: unknown; moved?: { name: string; space: string; rev: string } } | null {
  const m = /^\/api\/c\/([a-z][a-z0-9_]*)$/.exec(path);
  if (!m) return null;
  const collection = m[1] as Name;
  const refused = (error: string) => ({ status: 400, data: { error, kind: null, record: null, change: null } });
  if (!(collection in FIELDS)) return refused(`there is no collection '${collection}' to write to here`);
  const ops = Array.isArray(body.ops) ? (body.ops as Op[]) : [];
  if (!ops.length) return refused("ops is a list of 1 to 200 operations");
  const answer: WriteAnswer = { name: collection, rev: null, change_ids: [], records: [] };
  for (const [n, op] of ops.entries()) {
    try {
      const [rec, made] = apply(collection, op);
      answer.change_ids.push(made?.change_id ?? null);
      answer.records.push(shown(rec));
    } catch (e) {
      if (e instanceof Refused) return { status: e.status, data: { ...e.data, error: `ops[${n}]: ${e.data.error}` } };
      throw e;
    }
  }
  answer.rev = revs[collection];
  return { status: 200, data: answer, ...(answer.change_ids.some(Boolean) ? { moved: { name: collection, space: "work", rev: revs[collection] } } : {}) };
}

/** A deal moves one column on, as a session would (the first open column's oldest card): the stream's `collection`
 * event, `{name, space, rev}`. */
export function moveDeal(): { name: string; space: string; rev: string } {
  const open = STAGES.filter((s) => s !== "lost");
  const deals = live("deals");
  const from = open.find((s) => deals.some((r) => r.fields.stage === s)) ?? "lead";
  const to = open[(open.indexOf(from) + 1) % open.length]!;
  const rec = deals.find((r) => r.fields.stage === from)!;
  rec.fields.stage = to;
  rec.version += 1;
  rec.updated = Date.now() / 1000;
  change("deals", rec.id, "set", { stage: from }, { stage: to }, Date.now(), "session:mock", { via: "session" });
  return { name: "deals", space: "work", rev: revs.deals };
}

// --- what the agents proposed (retinue/colltools.py) --------------------------------------------------------------

const proposals = new Map<string, CollectionProposal & { ops: Op[] }>();

function propose(session: string, collection: Name, ops: Op[], shownChanges: CollectionProposal["changes"], why: string | null, needs: string | null, ageMs: number) {
  const id = randomBytes(6).toString("hex");
  const created = (Date.now() - ageMs) / 1000;
  proposals.set(id, {
    id, tool_use_id: `toolu_${id}`, session, actor: `session:${session}`, collection, space: "work", changes: shownChanges,
    fields: FIELDS[collection], words: words(shownChanges.flatMap((c) => [c.before, c.after])), why, needs, created, at: created,
    state: "open", change_ids: null, ops,
  });
}

/** Two proposals a session of `session` made: a delete its rights leave to the owner, and a stage with its words. */
export function seedProposals(session: string) {
  if (proposals.size) return;
  const hooli = live("deals").find((r) => r.fields.company === "Hooli")!;
  propose(session, "deals", [{ op: "delete", id: hooli.id, version: hooli.version }],
    [{ op: "delete", id: hooli.id, before: { [DELETED]: null }, after: { [DELETED]: Date.now() / 1000 }, title: title(hooli) }],
    null, "a delete by session in deals is the owner's to make", 2 * 3600_000);
  const initech = dealOf("Initech");
  propose(session, "deals", [{ op: "set", id: initech.id, version: initech.version, fields: { stage: "proposal" } }],
    [{ op: "set", id: initech.id, before: { stage: initech.fields.stage }, after: { stage: "proposal" }, title: title(initech) }],
    "Their reply asks for a quote by Friday: the deal is at the proposal now.", null, 20 * 60_000);
}

const faceProposal = ({ ops: _, ...p }: CollectionProposal & { ops: Op[] }): CollectionProposal => p;

/** The open proposals, a Space's alone when one is named. */
export function waitingProposals(space?: string | null): CollectionProposal[] {
  return [...proposals.values()].filter((p) => p.state === "open" && (!space || p.space === space)).map(faceProposal);
}

/** `GET /api/c/_proposals`, `POST /api/c/_proposals/<id>/<apply|dismiss>` — `{status, data, moved?, proposal?}`, or
 * null when the path is not one. «Apply» writes the operations as the owner's, at the versions proposed. */
export function mockProposals(path: string, method: string, query: URLSearchParams) {
  if (path === "/api/c/_proposals" && method === "GET") return { status: 200, data: { proposals: waitingProposals(query.get("space")) } };
  const m = /^\/api\/c\/_proposals\/([0-9a-f]{12})\/(apply|dismiss)$/.exec(path);
  if (!m || method !== "POST") return null;
  const p = proposals.get(m[1]!);
  if (!p) return { status: 404, data: { error: "No such proposal." } };
  if (p.state !== "open") return { status: 409, data: { error: p.state === "applied" ? "Applied already." : "You dismissed it.", kind: null, proposal: faceProposal(p) } };
  if (m[2] === "dismiss") {
    Object.assign(p, { state: "dismissed", at: Date.now() / 1000 });
    return { status: 200, data: { proposal: faceProposal(p) }, proposal: faceProposal(p) };
  }
  const wrote = mockWrite(`/api/c/${p.collection}`, { ops: p.ops })!;
  if (wrote.status !== 200) return { status: wrote.status, data: { ...(wrote.data as object), proposal: faceProposal(p) } };
  Object.assign(p, { state: "applied", at: Date.now() / 1000, change_ids: (wrote.data as WriteAnswer).change_ids });
  return { status: 200, data: { proposal: faceProposal(p), answer: wrote.data }, moved: wrote.moved, proposal: faceProposal(p) };
}

