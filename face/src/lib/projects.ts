// Projects as places (docs/face.md «Projects»): every project of the Base at any depth (`/api/spaces`' `projects`),
// read once while the app is open; which project a session, a feed row or a task belongs to — the nearest project
// above its folder, else its sphere's —, and a project's words on screen. Nothing here is kept: it is folded from what
// the app already reads.
import { useEffect, useSyncExternalStore } from "react";
import type { FeedRow } from "./feed";
import type { ProjectNode } from "./space-types";
import { spacesApi } from "./spaces";
import type { Session, Sphere } from "./types";

/** A project's path as the app compares it: no slash at either end. */
export const clean = (path: string | null | undefined): string => (path ?? "").trim().replace(/^\/+|\/+$/g, "");

/** Whether `path` is the project `project` or a folder below it. */
export function within(path: string | null | undefined, project: string): boolean {
  const p = clean(path);
  return !!project && (p === project || p.startsWith(`${project}/`));
}

/** The project a Base folder belongs to: the folder itself or the nearest project above it, among `known`; "" none. */
export function nearest(folder: string | null | undefined, known: ReadonlySet<string>): string {
  const parts = clean(folder).split("/").filter(Boolean);
  for (let n = parts.length; n > 0; n--) {
    const above = parts.slice(0, n).join("/");
    if (known.has(above)) return above;
  }
  return "";
}

/** Where a session belongs: the nearest project of its folder, else its sphere (a top project by name), else "". */
export function sessionProject(s: Pick<Session, "project" | "sphere">, known: ReadonlySet<string>): string {
  return nearest(s.project, known) || clean(s.project).split("/")[0] || s.sphere || "";
}

/** Where a feed row belongs: its project's nearest, else its sphere. */
export function rowProject(row: Pick<FeedRow, "project" | "sphere">, known: ReadonlySet<string>): string {
  return nearest(row.project, known) || clean(row.project).split("/")[0] || row.sphere || "";
}

/** A feed row of the project `project` or of one of its sub-projects. */
export const rowWithin = (row: Pick<FeedRow, "project" | "sphere">, project: string, known: ReadonlySet<string>) =>
  within(rowProject(row, known), project);

/** What waits for the owner on a feed row: everything but a finished turn to read. */
export const rowWaits = (row: FeedRow) => row.action !== "read";

// --- the list of projects ----------------------------------------------------------------------------------------

let nodes: ProjectNode[] | null = null;
let asked = 0;
const subs = new Set<() => void>();
const AGAIN_MS = 5 * 60_000;

/** For tests: forget the list. */
export function forgetProjects(): void {
  nodes = null;
  asked = 0;
}

/** For tests and the mock: the list as given. */
export function setProjects(list: ProjectNode[] | null): void {
  nodes = list;
  asked = Date.now();
  for (const f of subs) f();
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
  return useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => subs.delete(f);
    },
    () => nodes,
    () => nodes,
  );
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

/** The projects a session's or a feed row's folder is matched against: the Base's, and the spheres (top ones). */
export function knownProjects(list: ProjectNode[] | null, spheres: Sphere[]): ReadonlySet<string> {
  return new Set([...(list ?? []).map((n) => n.name), ...spheres.map((s) => s.name)]);
}
