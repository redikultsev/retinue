// The owner's writes from a Space's screen (docs/collections.md#from-the-screen): a board's card moved to another
// column, a queue's row pressed done, a record added, a change taken back. Each is the operations of `apply` at the
// version the screen showed; one that moved on since comes back as a refusal for the screen to ask about. Here are
// the calls and the words — who changed what, from what to what — apart from the components that show them.
import { request } from "./api";
import { paths } from "./contract";
import { valueWords } from "./collections";
import { day } from "./spaces";
import type {
  Change,
  ChangesAnswer,
  CollectionProposal,
  FieldSpec,
  FormAnswer,
  Op,
  ProposalDecided,
  RecordAnswer,
  Refusal,
  WriteAnswer,
} from "./view-types";

export const editApi = {
  write: (collection: string, ops: Op[], idem: string) => request<WriteAnswer>(paths.collection(collection), { ops, idem }),
  record: (collection: string, id: string) => request<RecordAnswer>(paths.record(collection, id)),
  form: (collection: string) => request<FormAnswer>(paths.form(collection)),
  changes: (space: string, before?: string) => request<ChangesAnswer>(paths.changes(space, before)),
};

export type EditApi = typeof editApi;

/** What the agents proposed to write (retinue/colltools.py): those waiting, a Space's alone; the owner's press. */
export const proposalsApi = {
  waiting: (space?: string) => request<{ proposals: CollectionProposal[] }>(paths.proposals(space)),
  decide: (id: string, action: "apply" | "dismiss") => request<ProposalDecided>(paths.collectionProposal(id, action), {}),
};

export type ProposalsApi = typeof proposalsApi;

/** One operation of a write, and the record it is about — a revert names a change, not its record: a conflict's
 * «Apply mine» gives that record's new version to the operations about it. */
export interface Step {
  op: Op;
  target: string | null;
}

export const DELETED = "_deleted";

/** The steps of a write again, each about the record the refusal names at the version it has now. */
export function againAt(steps: Step[], refusal: Refusal): Step[] {
  const record = refusal.record;
  if (!record) return steps;
  return steps.map((s) => (s.target === record.id && "version" in s.op ? { ...s, op: { ...s.op, version: record.version } } : s));
}

/** The steps that take a write back: a revert of each change it made, at the version its record has after it. */
export function undoSteps(answer: WriteAnswer): Step[] {
  return answer.change_ids.flatMap((change, i) => {
    const record = answer.records[i];
    return change && record ? [{ op: { op: "revert", change_id: change, version: record.version } as Op, target: record.id }] : [];
  });
}

/** Who wrote a change, as the owner reads it. */
export function actorWords(actor: string): string {
  if (actor === "owner") return "you";
  if (actor === "assistant") return "the assistant";
  if (actor.startsWith("session:")) return "a session";
  if (actor.startsWith("mac:")) return "the Mac";
  if (actor.startsWith("code:")) return `the rule ${actor.slice(5)}`;
  if (actor.startsWith("import:")) return "an import";
  if (actor.startsWith("define:")) return "a form change";
  return actor;
}

const ULID = /^[0-9A-HJKMNP-TV-Z]{26}$/;

/** A field's value in words: an enum's by its words, a ref by its record's (or «another record»), a day as «21.09»,
 * a yes or a no; «—» for none. Without the field's type, a value is read by its look. */
export function valueText(value: unknown, spec?: FieldSpec, words: Record<string, string> = {}, now: Date = new Date()): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return value.toLocaleString("en-US");
  if (typeof value !== "string") return JSON.stringify(value);
  if (spec?.type === "ref" || (!spec && ULID.test(value))) return words[value] ?? "another record";
  if (spec?.type === "enum" || (!spec && /^[a-z][a-z0-9_]*$/.test(value))) return valueWords(value, spec?.labels);
  if (spec?.type === "date" || (!spec && /^\d{4}-\d{2}-\d{2}$/.test(value))) return day(value, now);
  if (spec?.type === "instant" || (!spec && INSTANT.test(value))) return moment(value, now);
  return value;
}

const fieldWords = (name: string) => valueWords(name);

const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

/** A moment in this device's own time: «21 Sep, 18:30». */
function moment(value: string, now: Date): string {
  const t = new Date(value);
  if (Number.isNaN(t.getTime())) return value;
  const hm = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
  const local = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
  return `${day(local, now)}, ${hm}`;
}

/** What a change did, in a few words each: «Stage: Applied → Interview», «added», «deleted», «restored». A revert
 * says what it set back, and that it was one. */
export function changeWords(change: Pick<Change, "op" | "before" | "after">, fields: Record<string, FieldSpec> = {}, words: Record<string, string> = {}): string[] {
  if (change.op === "add") return ["added"];
  const before = change.before ?? {};
  const after = change.after ?? {};
  const out: string[] = [];
  if (DELETED in after) out.push(after[DELETED] === null ? "restored" : "deleted");
  for (const name of Object.keys(after)) {
    if (name === DELETED) continue;
    const spec = fields[name];
    const was = before[name];
    out.push(`${fieldWords(name)}: ${was === undefined || was === null ? "" : `${valueText(was, spec, words)} → `}${valueText(after[name], spec, words)}`);
  }
  if (change.op === "revert") out.unshift("undone");
  return out.length ? out : [change.op];
}

/** How long ago, in a few words: «just now», «5 min ago», «3 h ago», «2 days ago». */
export function ago(ts: number, now: number = Date.now() / 1000): string {
  const s = Math.max(0, now - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  return `${d} ${d === 1 ? "day" : "days"} ago`;
}

/** A conflict in one sentence: «Changed by the assistant 2 min ago: Stage: Interview → Closed.» */
export function conflictWords(refusal: Refusal, now: number = Date.now() / 1000): string {
  const change = refusal.change;
  if (!change) return "It changed since you saw it.";
  const what = changeWords(change).join(", ");
  const by = `${refusal.kind === "after" ? "Changed after it" : "Changed"} by ${actorWords(change.actor)} ${ago(change.ts, now)}`;
  return `${by}: ${what}.`;
}

/** A record's fields in a few words, for a refusal that names another: its text values, the first two. */
export function recordWords(fields: Record<string, unknown>): string {
  const words = Object.values(fields).filter((v): v is string => typeof v === "string" && !ULID.test(v) && !/^\d{4}-\d{2}-\d{2}$/.test(v));
  return words.slice(0, 2).join(" · ") || "another record";
}

/** Whether a refusal is one of the screen's own questions, not an error to show. */
export function refusalOf(data: unknown): Refusal | null {
  if (!data || typeof data !== "object" || !("kind" in data)) return null;
  return data as Refusal;
}

/** The fields an add sends: those given a value, an int as a number; a field left empty is not sent. */
export function addFields(given: Record<string, string | boolean>, fields: Record<string, FieldSpec>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [name, value] of Object.entries(given)) {
    const spec = fields[name];
    if (!spec) continue;
    if (spec.type === "bool") {
      if (value === true) out[name] = true;
      continue;
    }
    if (typeof value !== "string" || value.trim() === "") continue;
    out[name] = spec.type === "int" ? Number(value) : spec.type === "text" ? value.trim()
      : spec.type === "instant" ? new Date(value).toISOString().replace(/\.\d{3}Z$/, "Z") : value;
  }
  return out;
}

/** The values an add's sheet starts with: the view's, and today in every required date it leaves empty. */
export function startValues(fields: string[], specs: Record<string, FieldSpec>, values: Record<string, unknown>, today: string): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (const name of fields) {
    const spec = specs[name];
    const given = values[name];
    if (spec?.type === "bool") out[name] = given === true;
    else if (given !== undefined && given !== null) out[name] = String(given);
    else out[name] = spec?.type === "date" && spec.required && today ? today : "";
  }
  return out;
}
