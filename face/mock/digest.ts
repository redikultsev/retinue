// The assistant's digest in the mock (retinue/feed.py `digest`): what the Router last pushed — a person's rows with
// their tokens, a draft waiting for «Send», a date of the Base, the reminders — read by «Waiting for you»
// (mock/server.ts) and by «Today»'s plan (mock/plan.ts) alike, as the platform reads its one digest for both. A press on
// a person's row takes it out of the digest until «Undo», as the Router's next push would. Invented names only.

const now = () => Date.now() / 1000;
const day = (days: number) => new Date((now() + 3600 + days * 86400) * 1000).toISOString().slice(0, 10);
/** A moment `days` from today at a wall time in Berlin (+1 in winter). */
function wall(days: number, h: number, m = 0): number {
  const d = new Date(Date.now() + 3600_000);
  d.setUTCHours(h - 1, m, 0, 0);
  return d.getTime() / 1000 + days * 86400;
}

export interface DigestItem {
  id: string;
  kind: "chat" | "letter" | "draft" | "deadline" | "reminder";
  /** Who it is about; the Base for a date. */
  who: string;
  title: string;
  since: number;
  sphere: string | null;
  /** A person's row's token. */
  t?: string;
  /** A reminder's day, and how it repeats. */
  when?: string | null;
  repeat?: string | null;
}

const DIGEST: DigestItem[] = [
  { id: "chat:1", kind: "chat", who: "Anna Petrova", title: "reply: is an airport transfer needed", since: now() - 6 * 3600, sphere: "travel", t: "a".repeat(32) },
  { id: "letter:2", kind: "letter", who: "Acme Hosting", title: "pay: the hosting invoice for May · deadline Friday (+2)", since: now() - 2 * 86400, sphere: "home", t: "b".repeat(32) },
  { id: "chat:3", kind: "chat", who: "Ivan Example", title: "decide: which of the two venues to book for the team dinner, the one by the river or the one near the office, before both stop holding the date", since: now() - 9 * 86400, sphere: null, t: "c".repeat(32) },
  { id: "letter:4", kind: "letter", who: "Example Bank", title: "decide: confirm a sign-in from a new device · deadline today", since: now() - 4 * 86400, sphere: null, t: "d".repeat(32) },
  { id: "letter:5", kind: "letter", who: "Example Studio", title: "reply: a second call next week · deadline Monday", since: now() - 86400, sphere: "home", t: "e".repeat(32) },
  { id: "draft:0000000c", kind: "draft", who: "Anna Petrova", title: "a reply draft waits for «Send»", since: now() - 1800, sphere: null },
  { id: "deadline:1", kind: "deadline", who: "Base", title: "tomorrow: Pay for the internet — due", since: now() - 86400, sphere: "home" },
  { id: "reminder:7", kind: "reminder", who: "", title: "Send the report draft to Acme", since: wall(3, 19, 0), sphere: null, when: day(3), repeat: null },
  { id: "reminder:8", kind: "reminder", who: "", title: "Water the plants", since: wall(1, 16, 0), sphere: null, when: day(1), repeat: "weekly" },
];

/** The tokens of the rows pressed «Done», «Not a task» or «Tomorrow», until «Undo». */
const pressed = new Set<string>();
let closedToday = 1;

/** The digest as the Router last pushed it: without the rows pressed since. */
export const digest = (): DigestItem[] => DIGEST.filter((i) => !(i.t && pressed.has(i.t)));

/** The digest's count of the asks closed today. */
export const closed = () => closedToday;

/** A press on a person's row (`POST /api/waiting/press`), done at once here as the Router does it a moment later. */
export function pressRow(t: string, action: "done" | "dismissed" | "later" | "open"): void {
  if (action === "open") {
    if (pressed.delete(t)) closedToday -= 1;
  } else if (!pressed.has(t)) {
    pressed.add(t);
    closedToday += 1;
  }
}
