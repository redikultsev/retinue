// A page's header, the same on every tab: a band across the column (sticky, the page scrolls under it) whose title
// stands on the same left edge as the page's content — the header's inner box has the content's width and padding.
import { cn } from "@/lib/utils";

/** The content column every page uses: its width is the page's, its padding is this. */
export const PAGE_X = "px-4 md:px-6";

export function PageHeader({
  title,
  back,
  actions,
  width = "max-w-3xl",
  children,
}: {
  title: React.ReactNode;
  /** A way up (a Space from its Record), before the title. */
  back?: React.ReactNode;
  /** Small controls on the title's line, at its right. */
  actions?: React.ReactNode;
  /** The page's content width (`max-w-*`), so the title lines up with what is under it. */
  width?: string;
  /** What belongs to the header under the title: a date range, the status chips, a search box. */
  children?: React.ReactNode;
}) {
  return (
    <header className="sticky top-0 z-10 shrink-0 border-b bg-background/95 pt-[env(safe-area-inset-top)] backdrop-blur">
      <div className={cn("mx-auto flex w-full flex-col gap-2 py-3", width, PAGE_X)}>
        <div className="flex min-h-8 items-center gap-2">
          {back}
          <h1 className="min-w-0 flex-1 truncate text-xl font-semibold">{title}</h1>
          {actions}
        </div>
        {children}
      </div>
    </header>
  );
}
