// «Archived» under the session list's filters: what the owner archived, the latest first (`GET /api/archived`) — a
// sub-session or side question archived with its session is not listed apart, «Restore» on that one brings it. A row's
// title opens the session where it stands; «Restore» puts it back in every list (its stopped turn stays stopped, and
// the main mark of a sphere is set again in its menu).
import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { when } from "@/lib/format";
import { live } from "@/lib/live";
import { sphereLabel } from "@/lib/spheres";
import { toast } from "@/lib/toast";
import type { ArchivedSession } from "@/lib/types";

interface Props {
  load?: typeof api.archived;
  restore?: typeof api.unarchive;
  /** The clock (a test's). */
  now?: number;
}

export function ArchivedList({ load = api.archived, restore = api.unarchive, now = Date.now() / 1000 }: Props) {
  const [rows, setRows] = useState<ArchivedSession[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const spheres = live.meta?.spheres ?? [];

  useEffect(() => {
    let gone = false;
    load()
      .then((res) => !gone && setRows(res.sessions))
      .catch(() => !gone && setRows([]));
    return () => {
      gone = true;
    };
  }, [load]);

  async function back(s: ArchivedSession) {
    setBusy(s.id);
    try {
      const res = await restore(s.id, false);
      live.dispatch({ type: "event", id: null, event: { kind: "session", data: res.session } });
      setRows((all) => (all ?? []).filter((r) => r.id !== s.id));
      toast("Restored.");
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That didn't work.", "error");
    } finally {
      setBusy(null);
    }
  }

  if (rows === null) return <p className="px-3 py-8 text-center text-sm text-muted-foreground">Loading…</p>;
  if (rows.length === 0) return <p className="px-3 py-8 text-center text-sm text-muted-foreground">Nothing archived.</p>;
  return (
    <ul aria-label="Archived" className="flex flex-col">
      {rows.map((s) => {
        const sphere = spheres.find((x) => x.name === s.sphere);
        return (
          <li key={s.id} data-testid="archived-row" className="group/row flex min-h-11 items-center gap-2 rounded-md px-2 py-1.5 hover:bg-accent/50">
            <div className="flex min-w-0 flex-1 flex-col">
              <Link to="/s/$id" params={{ id: s.id }} className="truncate text-sm hover:underline">
                {s.title}
              </Link>
              <span className="truncate text-xs text-muted-foreground">
                {sphere ? `${sphereLabel(sphere)} · ` : ""}archived {when(s.archived_at, now)}
              </span>
            </div>
            <Button variant="ghost" size="sm" className="hit shrink-0" disabled={busy !== null} onClick={() => void back(s)}>
              {busy === s.id ? "Restoring…" : "Restore"}
            </Button>
          </li>
        );
      })}
    </ul>
  );
}
