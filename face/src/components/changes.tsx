// «Changes»: the lane's `git diff HEAD` of a code session's directory, rendered by @pierre/diffs. A tap on a line
// opens a comment box under it; the comments wait in the session's tray and go together with «Send»
// (components/remarks.tsx).
import { parsePatchFiles } from "@pierre/diffs";
import { useCallback, useEffect, useMemo, useState } from "react";
import { RemarkableFileDiff } from "@/components/remark-diff";
import { RemarkTray } from "@/components/remarks";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import type { DiffResponse } from "@/lib/types";

export { commentText } from "@/lib/remarks";

export function Changes({ session }: { session: string }) {
  const [data, setData] = useState<DiffResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await api.diff(session));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't load the changes.");
    }
  }, [session]);

  useEffect(() => {
    void load();
  }, [load]);

  const files = useMemo(() => {
    if (!data?.diff) return [];
    try {
      return parsePatchFiles(data.diff).flatMap((p) => p.files);
    } catch {
      return [];
    }
  }, [data]);

  if (error)
    return (
      <div className="flex flex-col items-start gap-2 p-4 text-sm">
        <p className="text-destructive">{error}</p>
        <Button variant="outline" onClick={load}>
          Try again
        </Button>
      </div>
    );
  if (!data) return <p className="p-4 text-sm text-muted-foreground">Loading the changes…</p>;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 truncate font-mono text-xs text-muted-foreground">{data.cwd}</div>
        <Button size="sm" variant="outline" onClick={load}>
          Refresh
        </Button>
      </div>
      {data.stat && (
        <pre className="overflow-x-auto rounded-md bg-muted p-2 font-mono text-xs whitespace-pre">{data.stat}</pre>
      )}
      {data.truncated && (
        <p className="text-sm text-warning-foreground">The diff is over 1 MB — showing the start.</p>
      )}
      {files.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">No changes.</p>
      ) : (
        <>
          <p className="text-xs text-muted-foreground">Tap a line to comment; the comments go to the session together.</p>
          {files.map((f) => (
            <RemarkableFileDiff key={f.name} session={session} file={f} />
          ))}
          <RemarkTray session={session} />
        </>
      )}
    </div>
  );
}
