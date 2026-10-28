import { Link } from "@tanstack/react-router";
import { useToasts } from "@/lib/toast";
import { cn } from "@/lib/utils";

export function Toaster() {
  const toasts = useToasts();
  return (
    <div
      aria-live="polite"
      className="pointer-events-none fixed inset-x-0 bottom-[max(5.5rem,calc(env(safe-area-inset-bottom)+5rem))] z-50 flex flex-col items-center gap-2 px-4"
    >
      {toasts.map((t) => {
        const className = cn(
          "pointer-events-auto max-w-md rounded-lg px-4 py-2.5 text-sm shadow-lg",
          t.tone === "error" ? "bg-destructive text-white" : "bg-foreground text-background",
        );
        return t.href ? (
          <Link key={t.id} to={t.href} role="status" className={cn(className, "underline-offset-2 hover:underline")}>
            {t.text}
          </Link>
        ) : (
          <div key={t.id} role="status" className={className}>
            {t.text}
          </div>
        );
      })}
    </div>
  );
}
