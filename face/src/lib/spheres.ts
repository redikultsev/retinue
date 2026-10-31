// The session list by sphere (retinue/spheres.py): each sphere's main session first, then the others newest first,
// each followed by what hangs under it — its sub-sessions, forks and side questions — indented and foldable. The state
// groups of before are filters now.
import { RECENT_S } from "./store";
import type { Session, Sphere } from "./types";

export type Filter = "all" | "waiting" | "active" | "done" | "pinned";

export const FILTERS: readonly { value: Filter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "waiting", label: "Waiting" },
  { value: "active", label: "Working" },
  { value: "done", label: "Done" },
  { value: "pinned", label: "Pinned" },
];

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

export const sphereLabel = (s: Pick<Sphere, "icon" | "title">) => (s.icon ? `${s.icon} ${s.title}` : s.title);
