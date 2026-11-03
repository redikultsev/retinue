// How one session hangs under another, drawn the same everywhere — the sidebar, a session's header, «Details», the
// cards: one icon per relation, never a word or an id. A sub-session (started with a brief) branches off, a fork
// turns off a copy of the conversation, a side question asks about it.
import type * as React from "react";
import { CornerDownRight, GitBranch, MessageQuestion } from "@/components/icons";
import type { Session } from "@/lib/types";

export type Relation = "sub-session" | "fork" | "side question";

const ICONS: Record<Relation, (props: React.SVGProps<SVGSVGElement>) => React.ReactElement> = {
  "sub-session": GitBranch,
  fork: CornerDownRight,
  "side question": MessageQuestion,
};

/** What the relation is called where a word is needed: a tooltip, a screen reader. */
export const RELATION_WORDS: Record<Relation, string> = {
  "sub-session": "Sub-session of",
  fork: "Forked from",
  "side question": "Side question on",
};

export function RelationIcon({ how, ...props }: { how: Relation } & React.SVGProps<SVGSVGElement>) {
  const Icon = ICONS[how];
  return <Icon data-relation={how} {...props} />;
}

/** The session it hangs under, and how; null for one of its own. */
export function upOf(s: Session): { id: string; how: Relation } | null {
  if (s.sub_of) return { id: s.sub_of, how: "sub-session" };
  if ((s.side || s.parent) && s.parent) return { id: s.parent, how: "side question" };
  if (s.fork_of) return { id: s.fork_of, how: "fork" };
  return null;
}
