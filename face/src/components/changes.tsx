// «Изменения»: the lane's `git diff HEAD` of a code session's directory, rendered by @pierre/diffs. A tap on a line
// opens a comment box under it; the comment goes to the session as its next message:
// `Комментарий к <file>:<line>: <text>`.
import { parsePatchFiles } from "@pierre/diffs";
import { FileDiff, type FileDiffMetadata } from "@pierre/diffs/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { toast } from "@/lib/toast";
import type { DiffResponse } from "@/lib/types";

export function commentText(file: string, line: number, text: string): string {
  return `Комментарий к ${file}:${line}: ${text.trim()}`;
}

interface Target {
  file: string;
  side: "deletions" | "additions";
  line: number;
}

function CommentBox({
  target,
  onSend,
  onCancel,
}: {
  target: Target;
  onSend: (text: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="flex flex-col gap-2 border-y bg-background p-2 font-sans">
      <div className="text-xs text-muted-foreground">
        Комментарий к {target.file}:{target.line}
      </div>
      <Textarea
        autoFocus
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Что здесь не так?"
        rows={2}
      />
      <div className="flex gap-2">
        <Button
          disabled={!text.trim() || busy}
          onClick={async () => {
            setBusy(true);
            await onSend(text).finally(() => setBusy(false));
          }}
        >
          Отправить в сессию
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          Отмена
        </Button>
      </div>
    </div>
  );
}

function ChangedFile({
  file,
  target,
  setTarget,
  send,
}: {
  file: FileDiffMetadata;
  target: Target | null;
  setTarget: (t: Target | null) => void;
  send: (t: Target, text: string) => Promise<void>;
}) {
  const mine = target?.file === file.name ? target : null;
  return (
    <FileDiff<Target>
      fileDiff={file}
      className="overflow-hidden rounded-lg border"
      options={{
        theme: { dark: "pierre-dark", light: "pierre-light" },
        themeType: "system",
        diffStyle: "unified",
        overflow: "wrap",
        onLineClick: (props) => setTarget({ file: file.name, side: props.annotationSide, line: props.lineNumber }),
      }}
      lineAnnotations={mine ? [{ side: mine.side, lineNumber: mine.line, metadata: mine }] : []}
      renderAnnotation={(a) =>
        a.metadata ? (
          <CommentBox target={a.metadata} onCancel={() => setTarget(null)} onSend={(text) => send(a.metadata!, text)} />
        ) : null
      }
    />
  );
}

export function Changes({ session }: { session: string }) {
  const [data, setData] = useState<DiffResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [target, setTarget] = useState<Target | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await api.diff(session));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Не получилось загрузить изменения.");
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

  async function send(t: Target, text: string) {
    try {
      const res = await api.input(session, commentText(t.file, t.line, text));
      toast(res.say || "Отправлено.");
      setTarget(null);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Не отправилось.", "error");
    }
  }

  if (error)
    return (
      <div className="flex flex-col items-start gap-2 p-4 text-sm">
        <p className="text-destructive">{error}</p>
        <Button variant="outline" onClick={load}>
          Ещё раз
        </Button>
      </div>
    );
  if (!data) return <p className="p-4 text-sm text-muted-foreground">Загружаю изменения…</p>;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 truncate font-mono text-xs text-muted-foreground">{data.cwd}</div>
        <Button size="sm" variant="outline" onClick={load}>
          Обновить
        </Button>
      </div>
      {data.stat && (
        <pre className="overflow-x-auto rounded-md bg-muted p-2 font-mono text-xs whitespace-pre">{data.stat}</pre>
      )}
      {data.truncated && (
        <p className="text-sm text-warning-foreground">Дифф больше 1 МБ — показано начало.</p>
      )}
      {files.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">Изменений нет.</p>
      ) : (
        <>
          <p className="text-xs text-muted-foreground">Нажми на строку, чтобы оставить комментарий.</p>
          {files.map((f) => (
            <ChangedFile key={f.name} file={f} target={target} setTarget={setTarget} send={send} />
          ))}
        </>
      )}
    </div>
  );
}
