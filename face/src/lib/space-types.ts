// The shapes of retinue/spaces.py's answers (`/api/spaces…`). No imports: the mock server reads them too.
export const NATURES = ["knowledge", "profile", "journal", "artifacts"] as const;
export type Nature = (typeof NATURES)[number];

export const NATURE_WORDS: Record<Nature, string> = {
  knowledge: "Знание",
  profile: "Профиль",
  journal: "Журнал",
  artifacts: "Артефакты",
};

export interface SpaceRow {
  name: string;
  /** The Showcase's first heading. */
  title: string;
  questions: number;
  tasks: number;
  /** Sessions of its sphere waiting on the owner / working, as of the answer. */
  waiting: number;
  working: number;
}

export interface ShowcaseItem {
  /** Markdown; links to Records are the app's routes already. */
  text: string;
  /** A list item (a question, a task), or a paragraph between them. */
  list: boolean;
}

export interface ShowcaseSection {
  heading: string;
  kind: "questions" | "tasks" | "records" | "other";
  items: ShowcaseItem[];
}

export interface RecordRow {
  /** Inside the Space: `knowledge/x.md`, `profile/acme/y.md`. */
  path: string;
  /** The subfolder of its nature, "" for none. */
  folder: string;
  title: string;
  id?: string;
  type?: string;
  status?: string;
  date?: string;
  verified?: string;
  stale_after?: string;
  updated?: string;
  from?: string;
  until?: string;
  practice?: string;
  /** Too big for the app: listed, not opened. */
  big?: boolean;
}

export interface SpaceView {
  name: string;
  title: string;
  /** Markdown above the Showcase's marker, the title line left out. */
  rules: string;
  marker: boolean;
  sections: ShowcaseSection[];
  counts: { questions: number; tasks: number };
  records: Record<Nature, RecordRow[]>;
}

export type Head = Record<string, unknown>;

export interface RecordView {
  space: string;
  path: string;
  nature: string;
  title: string;
  head: Head;
  /** It starts like a head but does not parse: shown as the body. */
  bad_head: boolean;
  body: string;
}
