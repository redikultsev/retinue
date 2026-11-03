import { type RefCallback, useCallback, useRef, useState, useSyncExternalStore } from "react";

/** A CSS media query as a boolean that follows the window. */
export function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const m = window.matchMedia?.(query);
      m?.addEventListener("change", onChange);
      return () => m?.removeEventListener("change", onChange);
    },
    () => window.matchMedia?.(query).matches ?? false,
    () => false,
  );
}

/** A phone or tablet: Enter makes a new line there, the send button sends. */
export const useCoarsePointer = () => useMedia("(pointer: coarse)");
/** The desktop layout with the sidebar. */
export const useWide = () => useMedia("(min-width: 1024px)");
/** Room for two sessions side by side (E8). */
export const useVeryWide = () => useMedia("(min-width: 1280px)");

/** Whether an element has `min` px of width, as it changes: a screen beside the open chat has less than the window.
 * One not laid out yet (0 wide, as in jsdom) counts as having it. */
export function useRoomFor<T extends HTMLElement>(min: number): [RefCallback<T>, boolean] {
  const [roomy, setRoomy] = useState(true);
  const watch = useRef<ResizeObserver | null>(null);
  const ref = useCallback(
    (el: T | null) => {
      watch.current?.disconnect();
      watch.current = null;
      if (!el) return;
      const seen = (width: number) => setRoomy(width === 0 || width >= min);
      seen(el.getBoundingClientRect().width);
      if (typeof ResizeObserver === "undefined") return;
      watch.current = new ResizeObserver(([entry]) => entry && seen(entry.contentRect.width));
      watch.current.observe(el);
    },
    [min],
  );
  return [ref, roomy];
}
