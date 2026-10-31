import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/ui's class joiner. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** An idempotency key for a POST that may be retried (`face:<uuid4>`). */
export function inputId(): string {
  return `face:${crypto.randomUUID()}`;
}

/** A list of rows as one surface with dividers, not a card per row: the app's one list style (Today, Pages, the feed). */
export const LIST = "flex flex-col divide-y overflow-hidden rounded-lg border bg-card";
/** A row of such a list that is itself a link or a button. */
export const LIST_ROW = "flex w-full text-left px-3 py-2.5 transition-colors hover:bg-accent/50 focus-visible:bg-accent/50 focus-visible:outline-none";
