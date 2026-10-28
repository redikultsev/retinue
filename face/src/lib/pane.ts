// The desktop's right pane: a Record of the Base, a session's diff or a handed-over file, open beside whatever is in
// the middle. It lives in the URL's `pane` search value (`r:<space>/<path>`, `d:<session>`, `f:<file id>`), so a
// reload or a copied link keeps it and «Назад» closes it. On a phone there is no pane: the same thing opens as its
// own page.
import { useRouter, useRouterState } from "@tanstack/react-router";
import { useWide } from "./media";

export type Pane =
  | { kind: "record"; space: string; path: string }
  | { kind: "diff"; session: string }
  | { kind: "file"; id: string };

const SPACE = /^[a-z][a-z0-9_-]{0,31}$/;
const ID = /^[A-Za-z0-9_-]{1,64}$/;

// A Record's path, as the platform's rule has it (retinue/feed.py RECORD): one to three parts inside its Space, each
// a letter, digit or `_` then letters, digits, `.`, `_` or `-` — no dot-led part (so no `..`), no space, `?`, `#` or
// `%` — and the last a `.md` file.
const PART = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$/;

/** A Record's path inside its Space. */
export function recordPathOk(path: string): boolean {
  const parts = path.split("/");
  return parts.length >= 1 && parts.length <= 3 && parts.every((p) => PART.test(p)) && path.endsWith(".md") && path.length > 3;
}

/** A Record's path in the base: its Space, then its path inside it. */
export function fullRecordOk(path: string): boolean {
  return splitRecord(path) !== null;
}

export function parsePane(value: unknown): Pane | null {
  if (typeof value !== "string") return null;
  const kind = value.slice(0, 2);
  const rest = value.slice(2);
  if (kind === "r:") {
    const cut = rest.indexOf("/");
    const space = rest.slice(0, cut);
    const path = rest.slice(cut + 1);
    return cut > 0 && SPACE.test(space) && recordPathOk(path) ? { kind: "record", space, path } : null;
  }
  if (kind === "d:" && ID.test(rest)) return { kind: "diff", session: rest };
  if (kind === "f:" && ID.test(rest)) return { kind: "file", id: rest };
  return null;
}

export function paneValue(pane: Pane): string {
  switch (pane.kind) {
    case "record":
      return `r:${pane.space}/${pane.path}`;
    case "diff":
      return `d:${pane.session}`;
    case "file":
      return `f:${pane.id}`;
  }
}

/** A Record's own page: `/spaces/<space>/r/<path inside the Space>`. */
export const recordHref = (space: string, path: string) =>
  `/spaces/${encodeURIComponent(space)}/r/${path.split("/").map(encodeURIComponent).join("/")}`;

/** A path of the Base (`personal/profile/gym.md`) as its Space and its path inside it; null if it is not one. */
export function splitRecord(full: string): { space: string; path: string } | null {
  const cut = full.indexOf("/");
  const space = full.slice(0, cut);
  const path = full.slice(cut + 1);
  return cut > 0 && SPACE.test(space) && recordPathOk(path) ? { space, path } : null;
}

/** The pane open now, and a way to open or close one in place: the page in the middle stays. */
export function usePane(): { pane: Pane | null; open: (pane: Pane) => void; close: () => void } {
  const location = useRouterState({ select: (s) => s.location });
  const router = useRouter();
  const search = location.search as Record<string, unknown>;
  const go = (value: string | undefined) => {
    const next: Record<string, unknown> = { ...search, pane: value };
    if (value === undefined) delete next.pane;
    router.history.push(`${location.pathname}${router.options.stringifySearch(next)}`);
  };
  return { pane: parsePane(search.pane), open: (pane) => go(paneValue(pane)), close: () => go(undefined) };
}

/** Open a Record: beside the page on a wide screen, as its own page on a phone. */
export function useOpenRecord(): (space: string, path: string) => void {
  const wide = useWide();
  const { open } = usePane();
  const router = useRouter();
  return (space, path) => {
    if (wide) open({ kind: "record", space, path });
    else router.history.push(recordHref(space, path));
  };
}
