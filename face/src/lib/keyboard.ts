// Whether the on-screen keyboard is up: the tab bar steps aside for it. A phone gives no event for the keyboard, so it
// is read as: a text field has the focus AND the visual viewport is much shorter than the page was with no keyboard.
// Focus alone is not enough — Android's back gesture closes the keyboard and leaves the field focused — and a pinch
// zoom shrinks the visual viewport too, so a zoomed page (`scale` ≠ 1) never counts. The height with no keyboard is
// the tallest seen while no field had the focus: iOS and Telegram's web view shrink only the visual viewport, Android
// Chrome (`interactive-widget=resizes-content`) shrinks the layout too, and both read against it.
import { useEffect, useState } from "react";
import { useCoarsePointer } from "./media";

export const KEYBOARD_PX = 150;

export function isEditable(el: Element | null): boolean {
  if (!el) return false;
  if (el instanceof HTMLTextAreaElement) return !el.readOnly && !el.disabled;
  if (el instanceof HTMLInputElement)
    return !el.readOnly && !el.disabled && !["button", "checkbox", "radio", "file", "submit", "reset", "range", "color"].includes(el.type);
  return el instanceof HTMLElement && el.isContentEditable;
}

/** The rule itself: a field focused, the page not zoomed, and the visible height well below the keyboard-less one. */
export function keyboardUp(focused: boolean, scale: number, visible: number, full: number): boolean {
  return focused && Math.abs(scale - 1) < 0.01 && full - visible > KEYBOARD_PX;
}

export function useKeyboardUp(): boolean {
  const coarse = useCoarsePointer();
  const [up, setUp] = useState(false);
  useEffect(() => {
    if (!coarse) return;
    const viewport = window.visualViewport;
    let full = Math.max(window.innerHeight, viewport?.height ?? 0);
    let focused = isEditable(document.activeElement);
    const check = () => {
      const visible = viewport?.height ?? window.innerHeight;
      const scale = viewport?.scale ?? 1;
      if (!focused && Math.abs(scale - 1) < 0.01) full = Math.max(full, window.innerHeight, visible);
      setUp(keyboardUp(focused, scale, visible, full));
    };
    // focusout: the focus goes to `relatedTarget` (null: nowhere), and activeElement is not it yet.
    const onFocus = (e: FocusEvent) => {
      focused = isEditable(e.target as Element | null);
      check();
    };
    const onBlur = (e: FocusEvent) => {
      focused = isEditable(e.relatedTarget as Element | null);
      check();
    };
    const onTurn = () => {
      full = 0; // another orientation: its height is learnt again
      check();
    };
    document.addEventListener("focusin", onFocus);
    document.addEventListener("focusout", onBlur);
    viewport?.addEventListener("resize", check);
    window.addEventListener("resize", check);
    window.addEventListener("orientationchange", onTurn);
    check();
    return () => {
      document.removeEventListener("focusin", onFocus);
      document.removeEventListener("focusout", onBlur);
      viewport?.removeEventListener("resize", check);
      window.removeEventListener("resize", check);
      window.removeEventListener("orientationchange", onTurn);
    };
  }, [coarse]);
  return coarse && up;
}
