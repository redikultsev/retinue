// The session list by sphere (retinue/spheres.py): each sphere's main session first, then the others newest first,
// each followed by what hangs under it — its sub-sessions, forks and side questions — indented and foldable; and the
// left panel's tree of projects (`projectGroups`). The list has no state filter: what waits is its amber dot.
import { RECENT_S } from "./store";
import type { Session, Sphere } from "./types";

export type Filter = "all" | "waiting" | "active" | "done" | "pinned";

/** How deep a fork of a fork is indented at most: the phone's row keeps its width. */
export const MAX_DEPTH = 3;
export const NO_SPHERE = "No sphere";

export const isWaiting = (s: Session) => s.state === "waiting" || s.waiting > 0;

export function matches(s: Session, filter: Filter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "waiting":
      return isWaiting(s);
    case "active":
      return !isWaiting(s) && s.state === "active";
    case "done":
      return !isWaiting(s) && s.state !== "active";
    case "pinned":
      return !!s.pinned;
  }
}

export interface ListedRow {
  session: Session;
  /** 0 for a session of its own; under its parent, one more than the parent. */
  depth: number;
  /** A side question (`side`; a platform that does not send it: any `parent`), not a fork nor a sub-session. */
  side: boolean;
}

export interface SphereGroup {
  /** The sphere's name; "" for the sessions of no sphere. */
  key: string;
  /** Null for no sphere, and for a sphere no longer in the platform's settings. */
  sphere: Sphere | null;
  title: string;
  rows: ListedRow[];
}

/** A side question: the platform's `side`; one that does not send it yet has `parent` for side questions only. */
export const isSide = (s: Session) => s.side ?? !!s.parent;

/** The row a session hangs under: the session that started it (`sub_of`), the one a side question asks about
 * (`parent`), else the one it was forked from. */
const parentOf = (s: Session) => s.sub_of || (isSide(s) ? s.parent : null) || s.fork_of || null;

const newestFirst = (a: Session, b: Session) => b.at - a.at;

/** The sessions the list shows — not closed, closed in the last week, waiting, pinned or a sphere's main one — that
 * pass `filter`, grouped by sphere in the platform's order, then the spheres it no longer names, then «No sphere».
 * `empty`: the spheres with no session listed at all, for starting one there. */
export function sphereGroups(
  sessions: Session[],
  spheres: Sphere[],
  filter: Filter,
  now: number,
): { groups: SphereGroup[]; empty: Sphere[] } {
  const recent = (s: Session) => s.state !== "closed" || now - s.at < RECENT_S;
  const listed = sessions.filter((s) => isWaiting(s) || s.pinned || s.main || recent(s));
  const shown = listed.filter((s) => matches(s, filter));
  const byKey = new Map<string, Session[]>();
  for (const s of shown) byKey.set(s.sphere ?? "", [...(byKey.get(s.sphere ?? "") ?? []), s]);

  const known = new Map(spheres.map((s) => [s.name, s]));
  const keys = [
    ...spheres.map((s) => s.name).filter((k) => byKey.has(k)),
    ...[...byKey.keys()].filter((k) => k && !known.has(k)).sort(),
    ...(byKey.has("") ? [""] : []),
  ];
  const groups = keys.map((key) => {
    const members = byKey.get(key) ?? [];
    const ids = new Set(members.map((s) => s.id));
    const children = new Map<string, Session[]>();
    const tops: Session[] = [];
    for (const s of members) {
      const parent = parentOf(s);
      if (parent && ids.has(parent)) children.set(parent, [...(children.get(parent) ?? []), s]);
      else tops.push(s);
    }
    tops.sort((a, b) => Number(!!b.main) - Number(!!a.main) || newestFirst(a, b));
    const rows: ListedRow[] = [];
    const walk = (s: Session, depth: number) => {
      rows.push({ session: s, depth: Math.min(depth, MAX_DEPTH), side: isSide(s) });
      for (const child of (children.get(s.id) ?? []).sort(newestFirst)) walk(child, depth + 1);
    };
    for (const s of tops) walk(s, 0);
    const sphere = known.get(key) ?? null;
    return { key, sphere, title: key === "" ? NO_SPHERE : sphere ? sphereLabel(sphere) : key, rows };
  });
  const used = new Set(listed.map((s) => s.sphere ?? ""));
  return { groups, empty: spheres.filter((s) => !used.has(s.name)) };
}

/** A row as the list shows it: how many rows hang under it, whether they are folded away, and how many of those
 * hidden wait on the owner. */
export interface FoldedRow extends ListedRow {
  kids: number;
  folded: boolean;
  waiting: number;
}

/** The rows of a group with the folded ones' sub-rows taken out — a main session folds its sub-sessions, forks and side
 * questions, as Claude Code's sidebar folds a session's subagents. `folded`: the ids the owner folded. A fold never
 * hides `current` (the session on screen): a row with it underneath stays open. What waits on the owner is counted on
 * the folded row (`waiting`), not lost. */
export function foldRows(rows: ListedRow[], folded: ReadonlySet<string>, current?: string): FoldedRow[] {
  const out: FoldedRow[] = [];
  let hideBelow: number | null = null;
  rows.forEach((row, k) => {
    if (hideBelow !== null) {
      if (row.depth > hideBelow) return;
      hideBelow = null;
    }
    const under = [];
    for (let j = k + 1; j < rows.length && rows[j]!.depth > row.depth; j += 1) under.push(rows[j]!.session);
    const shut = under.length > 0 && folded.has(row.session.id) && !under.some((s) => s.id === current);
    out.push({ ...row, kids: under.length, folded: shut, waiting: shut ? under.filter(isWaiting).length : 0 });
    if (shut) hideBelow = row.depth;
  });
  return out;
}

export const sphereLabel = (s: Pick<Sphere, "title">) => s.title;

// --- by project: the left panel's places -------------------------------------------------------------------------

export interface ProjectGroup {
  /** The project's folder (`work/acme`), a sphere's name for one with no folder, or "" for «Other». */
  key: string;
  title: string;
  /** A top project's sphere mark. */
  icon?: string;
  /** 0 for a top project; one more per project it is in. */
  depth: number;
  /** The project it is in, among the groups; null for a top one. */
  parent: string | null;
  /** Its own sessions, each with what hangs under it. */
  rows: ListedRow[];
}

/** A project node as `projectGroups` needs it. */
export interface ProjectRef {
  name: string;
  title: string;
  parent: string | null;
}

/** The sessions the list shows (as `sphereGroups`), under the projects they run in — the nearest project of a
 * session's folder —, the Base's tree: its Spaces on top, every project inside its project at any depth, sessions or
 * not, «Other» last for a session of none. A sphere is a place only as a Base folder; with no Base read (`nodes`
 * empty) the spheres stand for the Spaces. A session that hangs under another (a sub-session, a fork, a side question)
 * stays under it, in that one's project. The order: the Spaces as the Spaces page has them (the spheres' order, then
 * A–Z); in a project its main session, the pinned, the others newest first, then its sub-projects A–Z. `where(s)`: the
 * project a session belongs to (lib/projects.ts `useProjectOf`). */
export function projectGroups(
  sessions: Session[],
  spheres: Sphere[],
  nodes: ProjectRef[],
  now: number,
  where: (s: Session) => string,
): ProjectGroup[] {
  const recent = (s: Session) => s.state !== "closed" || now - s.at < RECENT_S;
  const shown = sessions.filter((s) => isWaiting(s) || s.pinned || s.main || recent(s));
  const ids = new Set(shown.map((s) => s.id));
  const children = new Map<string, Session[]>();
  const tops: Session[] = [];
  for (const s of shown) {
    const parent = parentOf(s);
    if (parent && ids.has(parent)) children.set(parent, [...(children.get(parent) ?? []), s]);
    else tops.push(s);
  }
  tops.sort((a, b) => Number(!!b.main) - Number(!!a.main) || Number(!!b.pinned) - Number(!!a.pinned) || newestFirst(a, b));
  const node = new Map(nodes.map((n) => [n.name, n]));
  // a place of the tree: a Base project; without a Base, a sphere
  const place = (key: string) => (nodes.length ? node.has(key) : spheres.some((s) => s.name === key));
  const byKey = new Map<string, Session[]>();
  for (const s of tops) {
    const key = place(where(s)) ? where(s) : "";
    byKey.set(key, [...(byKey.get(key) ?? []), s]);
  }
  const sphere = new Map(spheres.map((s) => [s.name, s]));
  const up = (key: string): string | null => {
    const p = node.get(key)?.parent;
    return p && node.has(p) ? p : null;
  };
  // every place of the tree, sessions or not, and «Other» when a session is of none
  const keys = new Set<string>([...(nodes.length ? nodes.map((n) => n.name) : spheres.map((s) => s.name)), ...byKey.keys()]);
  const order = (a: string, b: string) => {
    const at = (k: string) => (k === "" ? Number.MAX_SAFE_INTEGER : (spheres.findIndex((s) => s.name === k) + 1 || spheres.length + 1));
    return at(a) - at(b) || a.localeCompare(b);
  };
  const out: ProjectGroup[] = [];
  const walk = (key: string, depth: number) => {
    const rows: ListedRow[] = [];
    const add = (s: Session, d: number) => {
      rows.push({ session: s, depth: Math.min(d, MAX_DEPTH), side: isSide(s) });
      for (const child of (children.get(s.id) ?? []).sort(newestFirst)) add(child, d + 1);
    };
    for (const s of byKey.get(key) ?? []) add(s, 0);
    const known = sphere.get(key);
    const title = key === "" ? OTHER : known ? sphereLabel(known) : (node.get(key)?.title ?? key.split("/").pop() ?? key);
    out.push({ key, title, icon: depth === 0 ? known?.icon : undefined, depth, parent: key ? up(key) : null, rows });
    for (const k of [...keys].filter((k) => k && up(k) === key).sort((a, b) => a.localeCompare(b))) walk(k, depth + 1);
  };
  for (const key of [...keys].filter((k) => k === "" || up(k) === null).sort(order)) walk(key, 0);
  return out;
}

export const OTHER = "Other";

/** The key a project's fold is kept under: a Space opens by itself and folds by `project:<key>`; a sub-project is
 * folded until opened, by `open:<key>`. */
export const foldKey = (g: Pick<ProjectGroup, "key" | "depth">) => (g.depth > 0 ? `open:${g.key}` : `project:${g.key}`);

/** Whether a project is folded by what the owner did ({@link foldKey}). */
export const isShut = (g: Pick<ProjectGroup, "key" | "depth">, folded: ReadonlySet<string>) => folded.has(foldKey(g)) !== g.depth > 0;

/** The groups as the list shows them: a folded project keeps its heading and hides its sessions and its sub-projects —
 * never the session on screen. */
export function foldProjects(groups: ProjectGroup[], folded: ReadonlySet<string>, current?: string): (ProjectGroup & { folded: boolean })[] {
  const parentOf = new Map(groups.map((g) => [g.key, g.parent]));
  const inside = (g: ProjectGroup, key: string) => {
    for (let k: string | null = g.key; k !== null && k !== undefined; k = parentOf.get(k) ?? null) if (k === key) return true;
    return false;
  };
  const holds = (key: string) => !!current && groups.some((g) => inside(g, key) && g.rows.some((r) => r.session.id === current));
  const shut = new Set(groups.filter((g) => isShut(g, folded) && !holds(g.key)).map((g) => g.key));
  return groups
    .filter((g) => {
      for (let p = g.parent; p !== null; p = parentOf.get(p) ?? null) if (shut.has(p)) return false;
      return true;
    })
    .map((g) => ({ ...g, folded: shut.has(g.key) }));
}
