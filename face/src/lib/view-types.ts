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

export interface BoardView extends Named {
  kind: "board";
  group: string;
  rot: number | null;
  fields: string[];
  columns: Column[];
  dropped: number;
}

export interface QueueView extends Named {
  kind: "queue";
  rot: number | null;
  fields: string[];
  count: number;
  rows: ViewRow[];
  dropped: number;
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
