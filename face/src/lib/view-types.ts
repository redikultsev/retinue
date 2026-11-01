// What the platform answers of a Space's views (docs/collections.md#views), apart from lib/collections.ts so the
// mock can use the same types without React.

/** A record as a board or a queue shows it. */
export interface ViewRow {
  id: string;
  version: number;
  /** Only the view's fields, those the record has a value in. */
  fields: Record<string, unknown>;
  /** A `ref` field by the words of the record it names. */
  refs: Record<string, string>;
  src: string | null;
  /** Since the group's field last changed (a board), since its date or last change (a queue); below zero ahead. */
  days: number | null;
  /** Past the form's threshold. */
  rotten: boolean;
}

interface Named {
  name: string;
  title: string;
  error?: undefined;
}

export interface StatView extends Named {
  kind: "stat";
  value: number;
  goal: number | null;
  /** The same number for the week or month before. */
  previous: number | null;
  window: "week" | "month" | null;
  first: string | null;
  last: string | null;
}

export interface Column {
  /** The enum's value; null for the records it is unknown in. */
  value: string | null;
  count: number;
  folded: boolean;
  rows: ViewRow[];
}

/** A queue's one press on a row: its `field` set to `value` — «Sent» — which takes the row off the queue. */
export interface Done {
  field: string;
  value: string | number | boolean;
  label: string;
}

/** A view's «add»: the button's words, the fields its sheet asks for, in order, and what they start as. */
export interface AddSpec {
  label: string;
  fields: string[];
  values: Record<string, unknown>;
}

export interface BoardView extends Named {
  kind: "board";
  group: string;
  rot: number | null;
  fields: string[];
  columns: Column[];
  dropped: number;
  add?: AddSpec | null;
}

export interface QueueView extends Named {
  kind: "queue";
  rot: number | null;
  fields: string[];
  count: number;
  rows: ViewRow[];
  dropped: number;
  done?: Done | null;
  add?: AddSpec | null;
}

/** A view the platform could not make: a field its form lacks, a goal or a threshold it does not have. */
export interface BrokenView {
  name: string;
  kind: "stat" | "board" | "queue";
  title: string;
  error: string;
}

export type View = StatView | BoardView | QueueView | BrokenView;

export interface CollectionViews {
  name: string;
  /** Its newest change, a ULID; null for a collection nothing was ever written to. */
  rev: string | null;
  views: View[];
}

/** The moment the views are of: the zone their days are counted in, its today, the server's clock. */
export interface Moment {
  tz: string;
  today: string;
  at: number;
}

export interface SpaceViews extends Moment {
  space: string;
  collections: CollectionViews[];
}

export interface OneView extends Moment {
  name: string;
  rev: string | null;
  view: View;
}

// --- writing (docs/collections.md#from-the-screen) ------------------------------------------------------------------

export interface FieldSpec {
  type: "text" | "int" | "enum" | "date" | "bool" | "ref";
  values?: string[];
  /** A ref's collection. */
  to?: string;
  required?: boolean;
}

export interface CollectionRecord {
  id: string;
  fields: Record<string, unknown>;
  version: number;
  created: number;
  updated: number;
  /** When it was deleted (the platform's clock, in seconds); null for a live record. */
  deleted: number | null;
}

/** One change of a record, as the platform keeps it: `_deleted` in `before`/`after` is its deleted state. */
export interface Change {
  change_id: string;
  ts: number;
  as_of: string | null;
  batch: string | null;
  collection: string;
  id: string | null;
  op: "add" | "set" | "delete" | "restore" | "revert" | "define";
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  actor: string;
  model: string | null;
  via: string;
  src: string | null;
  foreign: string[];
  reverts: string | null;
  idem: string | null;
}

export interface RecordAnswer {
  name: string;
  rev: string | null;
  title: string | null;
  fields: Record<string, FieldSpec>;
  record: CollectionRecord;
  /** The newest first. */
  changes: Change[];
  /** The words of the records its refs name, by id. */
  words: Record<string, string>;
}

export interface FormAnswer {
  name: string;
  rev: string | null;
  fields: Record<string, FieldSpec>;
  /** For each ref field, the records it may name. */
  options: Record<string, { id: string; title: string }[]>;
}

export interface SpaceChange extends Change {
  /** The record's words now; null for one with none. */
  title: string | null;
  /** The record's version now, which its «Undo» goes by. */
  version: number | null;
  deleted: number | null;
  /** A revert took it back already: no «Undo» for it. */
  reverted: boolean;
}

export interface ChangesAnswer {
  space: string;
  changes: SpaceChange[];
  /** The change to read on from; null at the oldest. */
  next: string | null;
  words: Record<string, string>;
}

export type Op =
  | { op: "add"; fields: Record<string, unknown>; distinct?: boolean }
  | { op: "set"; id: string; version: number; fields: Record<string, unknown>; distinct?: boolean }
  | { op: "delete" | "restore"; id: string; version: number }
  | { op: "revert"; change_id: string; version: number };

export interface WriteAnswer {
  name: string;
  rev: string | null;
  /** Per operation: the change it made, null when the record was already so. */
  change_ids: (string | null)[];
  records: CollectionRecord[];
}

/** A write refused: `stale` the record moved on since it was shown, `after` a revert's change was changed after,
 * `duplicate` the form's key matches `record`, `taken` a unique value `record` holds. */
export interface Refusal {
  error: string;
  kind: "stale" | "after" | "duplicate" | "taken" | null;
  record: CollectionRecord | null;
  change: Change | null;
}

export type CollectionProposalState = "open" | "applying" | "applied" | "dismissed" | "expired";

/** A write to a collection an agent proposed instead of making it (retinue/colltools.py): «Apply» writes it as the
 * owner's own, «Dismiss» sets it aside. `changes` is what each operation would change, tried when it was proposed;
 * `needs` the rights' words when its writer could not make it itself, `why` the writer's own. */
export interface CollectionProposal {
  id: string;
  /** The call it was proposed by. */
  tool_use_id: string;
  /** The session it came from, if a session's. */
  session: string | null;
  actor: string;
  collection: string;
  space: string;
  changes: (Pick<Change, "op" | "id" | "before" | "after"> & { title: string | null })[];
  fields: Record<string, FieldSpec>;
  words: Record<string, string>;
  why: string | null;
  needs: string | null;
  created: number;
  /** Its last change: a copy older than another never replaces it. */
  at: number;
  state: CollectionProposalState;
  /** What «Apply» wrote. */
  change_ids: (string | null)[] | null;
}

export interface ProposalDecided {
  proposal: CollectionProposal;
  answer?: WriteAnswer;
}
