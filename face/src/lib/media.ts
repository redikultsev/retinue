import { useSyncExternalStore } from "react";

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
