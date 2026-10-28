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
