// A card that arrives for a session the owner is not looking at: one quiet line that opens it. The tab title and the
// app badge already count the cards; this says which one came.
import type { Approval, Session } from "./types";

export interface CardNotice {
  text: string;
  href: string;
}

/** The sessions on screen: the one in the path (`/s/<id>`) and the one beside it (`?with=<id>`). */
export function visibleSessions(pathname: string, search: string): string[] {
  const out: string[] = [];
  const match = /^\/s\/([^/]+)/.exec(pathname);
  if (match?.[1]) out.push(decodeURIComponent(match[1]));
  const beside = new URLSearchParams(search).get("with");
  if (beside) out.push(beside);
  return out;
}

/** «Ждёт решения: <track> · <title>», linking to the session — or nothing when that session is on screen, where the
 * card already shows inline, or on the card's own page. */
export function cardNotice(approval: Approval, session: Session | undefined, visible: string[], pathname = ""): CardNotice | null {
  if (visible.includes(approval.session) || pathname === `/a/${approval.id}`) return null;
  const where = session ? `${session.track} · ${session.title || session.track}` : `сессия ${approval.session}`;
  return { text: `Ждёт решения: ${where}`, href: `/s/${encodeURIComponent(approval.session)}` };
}
