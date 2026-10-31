// shadcn/ui Dialog's look (MIT) on the native <dialog> element instead of Radix: focus trap, Esc and the top layer
// come from the browser, one dependency fewer. `sheet`: a bottom sheet on a phone (a centred panel on a wide screen),
// its own header with «Close», its body scrolling above the home indicator. Rendered into <body>: a dialog opened from
// a control shown only on hover (a session row's menu) would otherwise go when the hover does — its ancestor hidden,
// the top layer shows nothing.
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type * as React from "react";
import { cn } from "@/lib/utils";

interface DialogProps extends Omit<React.ComponentProps<"dialog">, "open"> {
  open: boolean;
  onClose: () => void;
  title: string;
  sheet?: boolean;
}

function Dialog({ open, onClose, title, sheet = false, className, children, ...props }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal?.() ?? dialog.setAttribute("open", "");
    if (!open && dialog.open) dialog.close?.() ?? dialog.removeAttribute("open");
  }, [open]);
  return createPortal(
    <dialog
      ref={ref}
      aria-label={title}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && onClose()}
      className={cn(
        "m-auto w-[calc(100%-2rem)] max-w-lg rounded-xl border bg-background p-0 text-foreground shadow-lg backdrop:bg-black/50",
        sheet &&
          "mx-0 mt-auto mb-0 max-h-[85dvh] w-full max-w-none rounded-t-2xl rounded-b-none md:m-auto md:max-h-[80dvh] md:w-[28rem] md:rounded-xl",
        className,
      )}
      {...props}
    >
      {open && sheet && (
        <div className="flex max-h-[inherit] flex-col">
          <div className="flex min-h-12 shrink-0 items-center gap-2 border-b pr-1 pl-4">
            <h2 className="flex-1 text-sm font-semibold">{title}</h2>
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="inline-flex size-11 items-center justify-center rounded-md text-muted-foreground hover:bg-accent"
            >
              <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden="true">
                <path d="M18 6 6 18M6 6l12 12" />
              </svg>
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto pb-[env(safe-area-inset-bottom)]">{children}</div>
        </div>
      )}
      {open && !sheet && (
        <div className="flex flex-col gap-4 p-5">
          <h2 className="text-lg font-semibold leading-none">{title}</h2>
          {children}
        </div>
      )}
    </dialog>,
    document.body,
  );
}

export { Dialog };
