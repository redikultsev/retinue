// «Changes»: a Space's newest changes to its records — the owner's from this screen, the sessions', the assistant's,
// an import's — each with who, when and what, and «Undo». Folded until opened; read when opened, and again when a
// collection of the Space moves on or the owner writes here. A record's name opens its sheet.
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import type { ChangesAnswer, SpaceChange } from "@/lib/collections";
import { actorWords, ago, changeWords, type EditApi } from "@/lib/edit";
import { LIST } from "@/lib/utils";
import { useEditing } from "./edit";

const UNDONE = new Set(["add", "set", "delete", "restore", "revert"]);

export function SpaceChanges({ space, api, revs }: { space: string; api: EditApi; revs: string }) {
  const editing = useEditing();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ChangesAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const read = useCallback(async () => {
    try {
      setData(await api.changes(space));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Did not load.");
    }
  }, [api, space]);
  const writes = editing?.writes ?? 0;
  // biome-ignore lint/correctness/useExhaustiveDependencies: `revs` and `writes` are what it reads again by
  useEffect(() => {
    if (open) void read();
  }, [open, read, revs, writes]);
  const older = async () => {
    if (!data?.next) return;
    try {
      const more = await api.changes(space, data.next);
      setData({ ...more, changes: [...data.changes, ...more.changes], words: { ...data.words, ...more.words } });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Did not load.");
    }
  };
  const undo = async (change: SpaceChange) => {
    if (!editing || change.version === null || !change.id) return;
    setBusy(true);
    await editing.write(change.collection, [{ op: { op: "revert", change_id: change.change_id, version: change.version }, target: change.id }], "Undone.");
    setBusy(false);
  };
  return (
    <details className="rounded-lg border px-3 py-2" onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)} data-testid="space-changes">
      <summary className="cursor-pointer text-sm font-medium">Changes</summary>
      <div className="flex flex-col gap-2 pt-2">
        {error && <p className="text-sm text-muted-foreground">{error}</p>}
        {!data && !error && <p className="text-sm text-muted-foreground">Loading…</p>}
        {data && data.changes.length === 0 && <p className="text-sm text-muted-foreground">No changes yet.</p>}
        {data && data.changes.length > 0 && (
          <ul className={LIST}>
            {data.changes.map((change) => {
              const what = changeWords(change, {}, data.words).join(" · ");
              return (
                <li key={change.change_id} className="flex items-start gap-2 px-3 py-2">
                  <span className="min-w-0 flex-1">
                    {editing && change.id ? (
                      <button type="button" className="block text-left text-sm font-medium break-words underline-offset-2 hover:underline" onClick={() => editing.open(change.collection, change.id!)}>
                        {change.title ?? "Untitled"}
                      </button>
                    ) : (
                      <span className="block text-sm font-medium break-words">{change.title ?? "Untitled"}</span>
                    )}
                    <span className="block text-sm break-words">{what}</span>
                    <span className="block text-xs text-muted-foreground">
                      {[actorWords(change.actor), ago(change.ts), change.collection, change.foreign.length > 0 && `after reading ${change.foreign.join(", ")}`]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </span>
                  {change.reverted ? (
                    <span className="shrink-0 py-1 text-xs text-muted-foreground">taken back</span>
                  ) : editing && UNDONE.has(change.op) && change.version !== null && (
                    <Button size="sm" variant="ghost" disabled={busy} aria-label={`Undo: ${change.title ?? "Untitled"}, ${what}`} onClick={() => void undo(change)}>
                      Undo
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {data?.next && (
          <Button size="sm" variant="ghost" className="self-start" onClick={() => void older()}>
            Show older
          </Button>
        )}
      </div>
    </details>
  );
}
