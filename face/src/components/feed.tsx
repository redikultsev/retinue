// «Waiting for you» on the header: how many things wait for the owner, a tap from «Today», where they are listed and
// opened (`components/inbox.tsx`).
import { Link } from "@tanstack/react-router";
import { useTodayCount } from "@/lib/waiting";

/** The count in the header: what waits for the owner («Today N»), a tap away. Nothing while nothing waits. */
export function FeedCount() {
  const count = useTodayCount();
  if (count === 0) return null;
  return (
    <Link
      to="/today"
      aria-label={`Waiting for you: ${count}`}
      title="Waiting for you"
      className="inline-flex h-6 min-w-6 items-center justify-center rounded-full bg-warning px-2 text-xs font-semibold text-warning-foreground tabular-nums"
    >
      {count}
    </Link>
  );
}
