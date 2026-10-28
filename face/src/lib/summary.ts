// «Сводка»: what a session's right panel (a wide screen) or sheet (a phone) shows, read from what the app already has —
// the session's row, its snapshot's `about`, its items, files and cards. Nothing here is fetched on its own.
import { useSyncExternalStore } from "react";
import type { Item, ItemData, Session } from "./types";

export type Todo = NonNullable<Extract<ItemData, { kind: "plan" }>["data"]["todos"]>[number];

/** The session's latest TodoWrite list — the plan Claude Code pins under its last message — or null. Top-level only: a
 * subagent's plan is its own. */
export function latestPlan(items: Item[]): Todo[] | null {
  for (let k = items.length - 1; k >= 0; k--) {
    const it = items[k]!;
    if (it.kind === "plan" && it.data.todos) return it.data.todos;
  }
  return null;
}

/** «2 из 5», and the step in work in its own words («Чиню …»), for a plan's one-line bar. */
export function planLine(todos: Todo[]): { done: number; total: number; now: string | null } {
  const doing = todos.find((t) => t.status === "in_progress");
  return {
    done: todos.filter((t) => t.status === "completed").length,
    total: todos.length,
    now: doing ? doing.activeForm || doing.content : null,
  };
}

/** What hangs under a session: its sub-sessions, the side questions about it and its forks — newest first. */
export function childrenOf(sessions: Record<string, Session>, id: string): { session: Session; how: string }[] {
  const out: { session: Session; how: string }[] = [];
  for (const s of Object.values(sessions)) {
    if (s.id === id) continue;
    if (s.sub_of === id) out.push({ session: s, how: "подсессия" });
    else if (s.parent === id) out.push({ session: s, how: "вопрос" });
    else if (s.fork_of === id) out.push({ session: s, how: "ветка" });
  }
  return out.sort((a, b) => b.session.at - a.session.at);
}

/** Tokens as the owner reads them: «840», «84 тыс.», «1,2 млн». */
export function tokensWord(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${Math.round(n / 1000)} тыс.`;
  return `${(n / 1_000_000).toFixed(1).replace(".", ",")} млн`;
}

// The wide screen's panel is open or folded away; the device remembers it. One value for every session.
const KEY = "retinue-summary-open";
const listeners = new Set<() => void>();
let open = (() => {
  try {
    return localStorage.getItem(KEY) !== "0";
  } catch {
    return true;
  }
})();

export function setSummaryOpen(value: boolean) {
  open = value;
  try {
    localStorage.setItem(KEY, value ? "1" : "0");
  } catch {
    // storage unavailable: the choice lasts this visit
  }
  for (const l of listeners) l();
}

export function useSummaryOpen(): boolean {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => open,
    () => open,
  );
}
