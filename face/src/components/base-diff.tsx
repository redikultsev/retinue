// A «To the Base» card's diff: the files turned on, as the Base's clone has them now (`GET /api/base/{id}/diff`), rendered by
// @pierre/diffs as a diff — never as the Markdown the files are. Read again when the files turned on change. A tap on
// a line comments on it; the comments go to the card's session together (components/remarks.tsx).
import { parsePatchFiles } from "@pierre/diffs";
import { useEffect, useMemo, useState } from "react";
import { RemarkableFileDiff } from "@/components/remark-diff";
import { RemarkTray } from "@/components/remarks";
import { ApiError, api } from "@/lib/api";

export function BaseDiff({ id, on, session, load = api.baseDiff }: { id: string; on: string; session: string; load?: typeof api.baseDiff }) {
  const [data, setData] = useState<{ diff: string; truncated: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `on` is what the diff is of: a change reads it again
  useEffect(() => {
    let gone = false;
    setError(null);
    load(id)
      .then((d) => !gone && setData(d))
      .catch((e) => !gone && setError(e instanceof ApiError ? e.message : "The diff did not load."));
    return () => {
      gone = true;
    };
  }, [id, on, load]);

  const files = useMemo(() => {
    if (!data?.diff) return [];
    try {
      return parsePatchFiles(data.diff).flatMap((p) => p.files);
    } catch {
      return [];
    }
  }, [data]);

  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (!data) return <p className="text-sm text-muted-foreground">Loading the diff…</p>;
  return (
    <div data-testid="base-diff" className="flex flex-col gap-2">
      {data.truncated && <p className="text-sm text-warning-foreground">The diff is over 1 MB — showing the start.</p>}
      {files.length === 0 ? (
        <p className="text-sm text-muted-foreground">No changes.</p>
      ) : (
        <>
          {files.map((f) => (
            <RemarkableFileDiff key={f.name} session={session} file={f} />
          ))}
          <RemarkTray session={session} />
        </>
      )}
    </div>
  );
}
