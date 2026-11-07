// The operator's map (docs/face.md «The map»): every live session in a place of its own that it keeps for its whole
// life — the platform's `slot` in its project, so the phone and the laptop agree —, the projects as areas in a stable
// order, a sub-project as an area of its own under its project. Nothing moves when a state changes: a tile only says
// it. Flat on purpose: places that stay put are what spatial memory needs (Data Mountain, 1998); a third dimension
// does not help (Cockburn & McKenzie, 2002).
import { rowSession } from "./computers";
import type { FeedRow, TurnEnding } from "./feed";
import { endingOf, type Numbers } from "./operator";
import { RECENT_S } from "./store";
import type { Session, Sphere } from "./types";
import { waits as rowWaits } from "./waiting";

export interface MapRef {
  name: string;
  title: string;
  parent: string | null;
}

export interface Area {
  /** The project's key, as `where` gives it: its folder, a sphere's name, "" for «Other». */
  key: string;
  title: string;
  icon?: string;
  depth: number;
  /** The title of the project it is in, for a sub-project's area. */
  within?: string;
  /** Its places in order: a session, or null — a place free; `next` is where the next one will appear. */
  places: (Session | null)[];
  next: number;
}

const isWaiting = (s: Session) => s.state === "waiting" || s.waiting > 0;

/** The sessions on the map: the app's list, a closed one only for a week (as the left panel has them). */
export function onMap(sessions: Session[], now: number): Session[] {
  return sessions.filter((s) => s.state !== "closed" || isWaiting(s) || s.main || now - s.at < RECENT_S);
}

/** A project's places: each session at its `slot` (one whose place is taken by another — a project told apart only
 * here — the next free after it), one from before the slots after them by age; `next`: the lowest place free. */
export function placeAll(sessions: Session[]): { places: (Session | null)[]; next: number } {
  const places: (Session | null)[] = [];
  const slotted = sessions.filter((s) => s.slot !== undefined && s.slot !== null).sort((a, b) => a.slot! - b.slot! || a.created - b.created);
  for (const s of slotted) {
    let at = s.slot!;
    while (places[at]) at++;
    places[at] = s;
  }
  for (const s of sessions.filter((x) => x.slot === undefined || x.slot === null).sort((a, b) => a.created - b.created)) places[places.length] = s;
  for (let i = 0; i < places.length; i++) if (places[i] === undefined) places[i] = null;
  const hole = places.indexOf(null);
  return { places, next: hole >= 0 ? hole : places.length };
}

/** The map's areas: the projects with a session on the map and every project above them, in the left panel's order —
 * the spheres as the platform lists them, then the others A–Z, «Other» last; a sub-project right after its project.
 * `where(s)`: the project a session belongs to (lib/projects.ts `useProjectOf`). */
export function mapAreas(sessions: Session[], spheres: Sphere[], nodes: MapRef[], where: (s: Session) => string): Area[] {
  const byKey = new Map<string, Session[]>();
  for (const s of sessions) byKey.set(where(s), [...(byKey.get(where(s)) ?? []), s]);
  const node = new Map(nodes.map((n) => [n.name, n]));
  const sphere = new Map(spheres.map((s) => [s.name, s]));
  const up = (key: string): string | null => {
    const p = node.get(key)?.parent;
    return p && node.has(p) ? p : null;
  };
  const keys = new Set<string>();
  for (const key of byKey.keys()) for (let k: string | null = key; k !== null && !keys.has(k); k = k ? up(k) : null) keys.add(k);
  const at = (k: string) => (k === "" ? Number.MAX_SAFE_INTEGER : spheres.findIndex((s) => s.name === k) + 1 || spheres.length + 1);
  const out: Area[] = [];
  const walk = (key: string, depth: number, parent?: string) => {
    const known = sphere.get(key);
    const { places, next } = placeAll(byKey.get(key) ?? []);
    const title = key === "" ? "Other" : known ? known.title : (node.get(key)?.title ?? key.split("/").pop() ?? key);
    out.push({ key, title, icon: depth === 0 ? known?.icon : undefined, depth, ...(parent ? { within: parent } : {}), places, next });
    for (const k of [...keys].filter((k) => k && up(k) === key).sort((a, b) => a.localeCompare(b))) walk(k, depth + 1, title);
  };
  for (const key of [...keys].filter((k) => k === "" || up(k) === null).sort((a, b) => at(a) - at(b) || a.localeCompare(b))) walk(key, 0);
  return out;
}

export type TileState = "waits" | "working" | "done";

/** What a tile says: whose move it is, its operator number while it waits, and its one line. */
export interface Tile {
  state: TileState;
  /** Its request on the feed, when it has one: the card that opens. */
  row: FeedRow | null;
  n?: number;
  line: string;
}

const NEEDS: Record<TurnEnding["kind"], string> = { ask: "Answer", approve: "Allow", look: "Look", step: "Your step", done: "Done" };

/** A request's one line: what it needs, in a verb and its matter («Answer: which host», «Allow: publish the post»). */
export function needs(row: FeedRow): string {
  const e = endingOf(row);
  const matter = (row.essence || "").split("\n")[0]!.trim();
  const verb = e
    ? NEEDS[e.kind]
    : row.approval?.kind === "question" || row.action === "answer"
      ? "Answer"
      : row.approval
        ? "Allow"
        : row.proposal
          ? "Start"
          : row.collection_proposal
            ? "Apply"
            : row.source === "push"
              ? "Push"
              : row.action === "reply"
                ? "Reply"
                : "";
  return verb && matter ? `${verb}: ${matter}` : verb || matter;
}

/** A session's tile: by its request on the feed (the first that waits), else by its own state. */
export function tileOf(s: Session, rows: FeedRow[], numbers: Numbers): Tile {
  const mine = rows.filter((r) => rowSession(r) === s.id);
  const row = mine.find(rowWaits) ?? mine[0] ?? null;
  const waits = (row !== null && rowWaits(row)) || isWaiting(s);
  const state: TileState = waits ? "waits" : s.state === "active" ? "working" : "done";
  const n = waits && row ? numbers[row.key]?.n : undefined;
  return { state, row, n, line: row ? needs(row) : "" };
}
