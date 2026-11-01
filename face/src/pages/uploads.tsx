// `/uploads`, «Uploads»: any file sent up from this device — statements, exports, screenshots, photos of receipts —
// kept by the platform as it came, by its SHA-256 (lib/uploads.ts). «Upload» takes several at once, from the phone's
// files, photos or camera; they go one after another, each with its progress, and a failed one stays with the reason
// and «Retry». The list is the platform's, newest first, the same on every device. Nothing here opens a file: a row is
// its download.
import { useRef, useState } from "react";
import { size } from "@/components/files";
import { AlertCircle, Check, Close, UploadIcon } from "@/components/icons";
import { PageHeader, PAGE_X } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { when } from "@/lib/format";
import {
  type Sending,
  type Upload,
  UPLOAD_MAX,
  type UploadQueue,
  uploadQueue,
  type Uploads,
  uploadUrl,
  useUploadQueue,
  useUploads,
} from "@/lib/uploads";
import { cn, LIST } from "@/lib/utils";

/** «12 Oct · 1.4 MB · 3f9a2c41d0e7». */
function details(file: Upload, now: number): string {
  return `${when(file.created, now)} · ${size(file.size)} · ${file.sha256.slice(0, 12)}`;
}

function FileRow({ file, now }: { file: Upload; now: number }) {
  return (
    <li data-testid="upload" className="flex min-w-0 flex-col gap-0.5 px-3 py-2.5">
      <a
        href={uploadUrl(file)}
        download={file.name}
        className="text-[15px] font-medium break-words underline-offset-4 hover:underline md:text-sm"
      >
        {file.name}
      </a>
      <span className="text-xs text-muted-foreground tabular-nums" title={`SHA-256 ${file.sha256}`}>
        {details(file, now)}
      </span>
    </li>
  );
}

function SendingRow({ item, onRetry, onDismiss }: { item: Sending; onRetry: () => void; onDismiss: () => void }) {
  const total = item.file.size || 1;
  const percent = Math.min(100, Math.round((item.sent / total) * 100));
  return (
    <li data-testid="sending" className="flex items-start gap-2 px-3 py-2.5">
      <span
        aria-hidden="true"
        className={cn(
          "mt-0.5 inline-flex size-5 shrink-0 items-center justify-center rounded-full [&_svg]:size-3.5",
          item.state === "failed" ? "text-destructive" : item.state === "done" ? "bg-success/15 text-success" : "text-muted-foreground",
        )}
      >
        {item.state === "failed" ? <AlertCircle /> : item.state === "done" ? <Check /> : <UploadIcon />}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-[15px] break-words md:text-sm">{item.file.name}</span>
        {item.state === "sending" || item.state === "waiting" ? (
          <>
            <div
              role="progressbar"
              aria-label={`Uploading ${item.file.name}`}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
              className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
            >
              <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${percent}%` }} />
            </div>
            <span className="text-xs text-muted-foreground tabular-nums">
              {item.state === "waiting" ? `Waiting · ${size(item.file.size)}` : `${size(item.sent)} of ${size(item.file.size)}`}
            </span>
          </>
        ) : item.state === "done" ? (
          <span className="text-xs text-muted-foreground">{item.again ? "Already uploaded — kept once." : "Uploaded."}</span>
        ) : (
          <span role="alert" className="text-xs text-destructive">
            {item.error}
          </span>
        )}
      </div>
      {item.state === "failed" && !item.refused && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          Retry
        </Button>
      )}
      {(item.state === "failed" || item.state === "done") && (
        <Button size="icon" variant="ghost" aria-label={`Dismiss ${item.file.name}`} onClick={onDismiss} className="size-9 md:size-8">
          <Close />
        </Button>
      )}
    </li>
  );
}

export function UploadsView({
  uploads,
  error,
  sending,
  now = Date.now() / 1000,
  onPick,
  onRetry,
  onDismiss,
  onReload,
}: {
  uploads: Uploads | null;
  error?: string | null;
  sending: Sending[];
  now?: number;
  onPick: (files: File[]) => void;
  onRetry: (key: string) => void;
  onDismiss: (key: string) => void;
  onReload?: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const max = uploads?.max ?? UPLOAD_MAX;
  const files = uploads?.files ?? [];
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <PageHeader
        title="Uploads"
        actions={
          <Button size="sm" onClick={() => input.current?.click()}>
            <UploadIcon />
            Upload
          </Button>
        }
      />
      <input
        ref={input}
        type="file"
        multiple
        hidden
        aria-label="Files to upload"
        onChange={(e) => {
          const picked = [...(e.target.files ?? [])];
          e.target.value = ""; // the same file picked again is a change too
          if (picked.length) onPick(picked);
        }}
      />
      <main
        className={cn(
          "mx-auto flex w-full max-w-3xl min-h-0 flex-1 flex-col gap-5 overflow-y-auto pt-4 pb-[calc(env(safe-area-inset-bottom)+1rem)]",
          PAGE_X,
          over && "bg-accent/40",
        )}
        onDragOver={(e) => {
          if (![...e.dataTransfer.types].includes("Files")) return;
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOver(false); // not when it only moved onto a row
        }}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          const dropped = [...e.dataTransfer.files];
          if (dropped.length) onPick(dropped);
        }}
      >
        <p className="text-sm text-muted-foreground">
          Any file up to {Math.round(max / 1024 / 1024)} MB — a statement, an export, a screenshot, a photo of a receipt.
          It is kept on the server as it came; nothing reads it here.
        </p>
        {sending.length > 0 && (
          <section aria-label="Uploading" className="flex flex-col gap-2">
            <ul className={LIST}>
              {sending.map((item) => (
                <SendingRow key={item.key} item={item} onRetry={() => onRetry(item.key)} onDismiss={() => onDismiss(item.key)} />
              ))}
            </ul>
          </section>
        )}
        {!uploads ? (
          error ? (
            <div className="flex flex-col items-start gap-2">
              <p className="text-sm text-destructive">{error}</p>
              {onReload && (
                <Button size="sm" variant="outline" onClick={onReload}>
                  Try again
                </Button>
              )}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Loading…</p>
          )
        ) : files.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing uploaded yet.</p>
        ) : (
          <section aria-label="Uploaded" className="flex flex-col gap-2">
            <h2 className="text-[13px] font-medium">
              Uploaded <span className="tabular-nums">({files.length})</span>
            </h2>
            <ul className={LIST}>
              {files.map((file) => (
                <FileRow key={file.sha256} file={file} now={now} />
              ))}
            </ul>
          </section>
        )}
      </main>
    </div>
  );
}

/** Why a picked file is not sent at all, or null: the platform would refuse it the same way. */
export function refusal(file: File, max: number): string | null {
  if (file.size === 0) return "The file is empty.";
  if (file.size > max) return `Too large: ${size(file.size)}, ${Math.round(max / 1024 / 1024)} MB at most.`;
  return null;
}

/** The page over the app's one queue (`lib/uploads.ts`): what is on its way goes on when the page is left. */
export function UploadsPage({ queue = uploadQueue }: { queue?: UploadQueue }) {
  const { uploads, error, reload } = useUploads(queue);
  const sending = useUploadQueue(queue);
  const max = uploads?.max ?? UPLOAD_MAX;
  return (
    <UploadsView
      uploads={uploads}
      error={error}
      sending={sending}
      onPick={(files) => queue.add(files, (file) => refusal(file, max))}
      onRetry={(key) => queue.retry(key)}
      onDismiss={(key) => queue.dismiss(key)}
      onReload={() => void reload()}
    />
  );
}
