// The shapes of retinue/spaces.py's answers (`/api/spaces…`). No imports: the mock server reads them too.
export const NATURES = ["knowledge", "profile", "journal", "artifacts"] as const;
export type Nature = (typeof NATURES)[number];

export const NATURE_WORDS: Record<Nature, string> = {
  knowledge: "Knowledge",
  profile: "Profile",
  journal: "Journal",
  artifacts: "Artifacts",
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

/** A file of a project (its own: none of a sub-project's), by its path inside the project. */
export interface ProjectFile {
  path: string;
  size: number;
  kind: "image" | "pdf" | "audio" | "video" | "markdown" | "text" | "html" | "other";
  /** Too big for the app: listed, not sent. */
  big?: boolean;
}

/** A sub-project: `name` is its folder in the Base. */
export interface ProjectRow {
  name: string;
  title: string;
  questions: number;
  tasks: number;
}

/** Every project at any depth, as `/api/spaces` lists it beside the grid: the left panel's projects. */
export interface ProjectNode extends ProjectRow {
  /** The project it is in: "" the Base's root, null none. */
  parent: string | null;
}

/** Why a project is and what is in its scope: the `## Why` section of its AGENTS.md, markdown. */
export interface Passport {
  why: string;
  in_scope: string;
  not_in_scope: string;
}

/** A project — a Space is a top-level one. The project fields are absent from a platform before projects. */
export interface SpaceView {
  /** Its passport; null or absent: its AGENTS.md has no `## Why`. */
  passport?: Passport | null;
  name: string;
  /** Its folder in the Base. */
  path?: string;
  /** The project it is a sub-project of; null: none. */
  parent?: string | null;
  files?: ProjectFile[];
  /** Files past the listing's cap. */
  more_files?: number;
  projects?: ProjectRow[];
  title: string;
  /** Markdown above the Showcase's marker, the title line and the passport left out. */
  rules: string;
  marker: boolean;
  sections: ShowcaseSection[];
  counts: { questions: number; tasks: number };
  records: Record<Nature, RecordRow[]>;
}

export type Head = Record<string, unknown>;

export interface RecordView {
  space: string;
  /** The project it belongs to: the Space, or a project below it. */
  project?: string;
  path: string;
  nature: string;
  title: string;
  head: Head;
  /** It starts like a head but does not parse: shown as the body. */
  bad_head: boolean;
  body: string;
}
