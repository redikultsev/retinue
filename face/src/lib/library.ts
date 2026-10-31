// «Read»: the life hub's pages (`GET /api/library`, retinue/library.py) — the owner's own HTML pages the hub serves
// under /pages/, as the router last pushed their list. A page is never fetched or framed here: it opens on the hub's
// own origin in a tab of its own (Telegram's openLink inside the Mini App), so its scripts stay away from the app's
// login. What the owner opened and finished is the platform's row, the same on every device.

import { useCallback, useEffect, useState } from "react";
import { request } from "./api";

export interface LibraryPage {
  id: string;
  title: string;
  /** Always `<hub>/pages/<id>.html` — the platform refuses any other address. */
  url: string;
  /** The day the page last changed, `YYYY-MM-DD`. */
  updated: string;
  /** The Space of the Base it lives in, or null. */
  section: string | null;
  section_title: string | null;
  section_icon: string | null;
  /** Reading time, or null. */
  minutes: number | null;
  /** When the owner last opened it (seconds), or null. */
  opened: number | null;
  /** When he said «Finished» (seconds), or null. */
  done: number | null;
}

export interface Library {
  schema: number;
  /** The hub's origin from the platform's config; empty when it is not set. */
  hub: string;
  /** When the router last pushed the list (seconds), or null: never. */
  at: number | null;
  pages: LibraryPage[];
}

export const libraryApi = {
  list: () => request<Library>("/api/library"),
  open: (id: string) => request<{ page: LibraryPage }>(`/api/library/${encodeURIComponent(id)}/open`, {}),
  done: (id: string, done: boolean) =>
    request<{ page: LibraryPage }>(`/api/library/${encodeURIComponent(id)}/done`, { done }),
};

/** The page's address when it is what the platform promised — https, the hub's origin, under /pages/ — else null:
 * the link is not drawn. The platform checks it already; this is the second look before an href. */
export function safeUrl(url: string, hub: string): string | null {
  try {
    const parsed = new URL(url);
    const origin = new URL(hub).origin;
    return parsed.protocol === "https:" && parsed.origin === origin && parsed.pathname.startsWith("/pages/")
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

/** «Continue»: opened and not finished, the last opened first. */
export function continuing(pages: LibraryPage[]): LibraryPage[] {
  return pages.filter((p) => p.opened !== null && p.done === null).sort((a, b) => (b.opened ?? 0) - (a.opened ?? 0));
}

export interface Section {
  key: string;
  title: string;
  icon: string | null;
  pages: LibraryPage[];
}

export const NO_SECTION = "No section";

const fold = (text: string) => text.toLocaleLowerCase("ru").replaceAll("ё", "е");

/** The pages that match `query` (title or section, any case, ё as е), by section: sections by title, «No section»
 * last; in each, the latest changed first. */
export function sections(pages: LibraryPage[], query = ""): Section[] {
  const words = fold(query).split(/\s+/).filter(Boolean);
  const out = new Map<string, Section>();
  for (const page of pages) {
    const haystack = fold(`${page.title} ${page.section_title ?? ""} ${page.section ?? ""}`);
    if (!words.every((w) => haystack.includes(w))) continue;
    const key = page.section ?? "";
    let section = out.get(key);
    if (!section) {
      section = { key, title: page.section_title ?? page.section ?? NO_SECTION, icon: page.section_icon, pages: [] };
      out.set(key, section);
    }
    section.pages.push(page);
  }
  const list = [...out.values()];
  for (const s of list) s.pages.sort((a, b) => b.updated.localeCompare(a.updated) || a.title.localeCompare(b.title, "ru"));
  return list.sort((a, b) => Number(a.key === "") - Number(b.key === "") || a.title.localeCompare(b.title, "ru"));
}

/** `YYYY-MM-DD` as a local day's start, in seconds; NaN when it is not a day. */
export function dayStart(day: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getTime() / 1000 : Number.NaN;
}

/** Finished, and changed on a later day than he finished it: worth a look again. */
export function changedSinceDone(page: LibraryPage): boolean {
  if (page.done === null) return false;
  const done = new Date(page.done * 1000);
  return dayStart(page.updated) > new Date(done.getFullYear(), done.getMonth(), done.getDate()).getTime() / 1000;
}

/** The list, read on mount and again whenever the app comes back to the front (the owner returns from a page);
 * `put` replaces one page after an action. */
export function useLibrary() {
  const [library, setLibrary] = useState<Library | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setLibrary(await libraryApi.list());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load.");
    }
  }, []);
  useEffect(() => {
    void load();
    const onVisible = () => {
      if (!document.hidden) void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [load]);
  const put = useCallback((page: LibraryPage) => {
    setLibrary((current) =>
      current ? { ...current, pages: current.pages.map((p) => (p.id === page.id ? page : p)) } : current,
    );
  }, []);
  return { library, error, reload: load, put };
}
