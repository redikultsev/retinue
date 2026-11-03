// Projects as places (docs/face.md «Projects»): every project of the Base at any depth (`/api/spaces`' `projects`),
// read once while the app is open; which project a session or a feed row belongs to — the nearest project above its
// folder, else its sphere's —, one rule for every screen, and a project's words on screen. Nothing here is kept: it
// is folded from what the app already reads.
import { useEffect, useMemo, useSyncExternalStore } from "react";
import type { ProjectNode } from "./space-types";
import { spacesApi } from "./spaces";
import type { Sphere } from "./types";

/** A project's path as the app compares it: no slash at either end. */
export const clean = (path: string | null | undefined): string => (path ?? "").trim().replace(/^\/+|\/+$/g, "");

/** Whether `path` is the project `project` or a folder below it. */
export function within(path: string | null | undefined, project: string): boolean {
  const p = clean(path);
  return !!project && (p === project || p.startsWith(`${project}/`));
}

/** The project a Base folder belongs to: the folder itself or the nearest project above it, among `known`; "" none. */
function nearest(folder: string | null | undefined, known: ReadonlySet<string>): string {
  const parts = clean(folder).split("/").filter(Boolean);
  for (let n = parts.length; n > 0; n--) {
    const above = parts.slice(0, n).join("/");
    if (known.has(above)) return above;
  }
  return "";
}

/** What a session and a feed row both say of where they are from: the Base folder (absent off the Base), the sphere. */
export interface Placed {
  project?: string | null;
  sphere?: string | null;
}

/** Where a session or a feed row belongs: the nearest project of its folder, else its folder's top, else its sphere
 * (a top project by name), else "" — «Other». */
export type ProjectOf = (x: Placed) => string;

/** The rule, over the projects known: the Base's list, the spheres (top ones), and those a project's page read. */
export function projectOf(known: ReadonlySet<string>): ProjectOf {
  return (x) => nearest(x.project, known) || clean(x.project).split("/")[0] || x.sphere || "";
}

// --- the list of projects ----------------------------------------------------------------------------------------

let nodes: ProjectNode[] | null = null;
// projects a project's page read before the list had them (or with no list at all): known to every screen alike
let learned: readonly string[] = [];
let asked = 0;
const subs = new Set<() => void>();
const AGAIN_MS = 5 * 60_000;
const notify = () => {
  for (const f of subs) f();
};
const subscribe = (f: () => void) => {
  subs.add(f);
  return () => subs.delete(f);
};

/** For tests: forget the list. */
export function forgetProjects(): void {
  nodes = null;
  learned = [];
  asked = 0;
}

/** For tests and the mock: the list as given. */
export function setProjects(list: ProjectNode[] | null): void {
  nodes = list;
  asked = Date.now();
  notify();
}

/** A project's page: itself and its sub-projects, as it read them — every screen matches against them from now on. */
export function learnProjects(paths: string[]): void {
  const fresh = paths.map(clean).filter((p) => p && !learned.includes(p));
  if (!fresh.length) return;
  learned = [...learned, ...fresh];
  notify();
}

function read(): void {
  if (Date.now() - asked < AGAIN_MS) return;
  asked = Date.now();
  spacesApi
    .list()
    .then((answer) => setProjects(answer.projects ?? []))
    .catch(() => undefined); // no Base: the spheres stand for the projects
}

/** Every project at any depth, or null until read (and without a Base). Read again at most every five minutes. */
export function useProjects(): ProjectNode[] | null {
  useEffect(read, []);
  return useSyncExternalStore(subscribe, () => nodes, () => nodes);
}

/** Where a session or a feed row belongs, by the one rule ({@link projectOf}) over every project known now. */
export function useProjectOf(spheres: Sphere[]): ProjectOf {
  const list = useProjects();
  const also = useSyncExternalStore(subscribe, () => learned, () => learned);
  return useMemo(() => projectOf(new Set([...(list ?? []).map((n) => n.name), ...spheres.map((s) => s.name), ...also])), [list, spheres, also]);
}

/** A project's title: its Showcase's, a top one's sphere's, else its folder's last name. */
export function projectName(path: string, list: ProjectNode[] | null, spheres: Sphere[]): string {
  const sphere = spheres.find((s) => s.name === path);
  if (sphere) return sphere.title;
  return list?.find((n) => n.name === path)?.title ?? path.split("/").pop() ?? path;
}

/** A project in a row's second line: «Work», a sub-project «Work / Site»; "" for none. */
export function projectLabel(path: string, list: ProjectNode[] | null, spheres: Sphere[]): string {
  const p = clean(path);
  if (!p) return "";
  const top = p.split("/")[0]!;
  return p === top ? projectName(top, list, spheres) : `${projectName(top, list, spheres)} / ${projectName(p, list, spheres)}`;
}
