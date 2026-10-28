// The app's one-line notices (the toast of a decision, an error). A tiny external store.
import { useSyncExternalStore } from "react";

export interface Toast {
  id: number;
  text: string;
  tone: "info" | "error";
  /** A path of this app the toast opens when tapped. */
  href?: string;
}

let toasts: Toast[] = [];
let next = 1;
const listeners = new Set<() => void>();
const emit = () => {
  for (const l of listeners) l();
};

export function toast(text: string, tone: Toast["tone"] = "info", ms = 4000, href?: string): void {
  const id = next++;
  toasts = [...toasts, { id, text, tone, ...(href ? { href } : {}) }];
  emit();
  setTimeout(() => {
    toasts = toasts.filter((t) => t.id !== id);
    emit();
  }, ms);
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => toasts,
  );
}
