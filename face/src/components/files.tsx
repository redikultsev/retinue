// «Files»: what a session handed over from `out/` (retinue/outfiles.py), newest first, each with a download. A picture
// shows as itself; Markdown is rendered by the transcript's own renderer (no raw HTML, no remote image); plain text
// as text; an HTML page only inside a frame that is sandboxed twice — the frame's `sandbox` attribute and the
// platform's `sandbox` policy on `/api/files/{id}/view` — so it runs no script and never gets the face's origin.
// A project's files (retinue/spaces.py) use the same row: a recording or a film in the browser's player, a PDF in a
// frame of the browser's own viewer (`/api/base-view/…`).
import { useEffect, useState } from "react";
import { Markdown } from "@/components/transcript/item";
import { Button } from "@/components/ui/button";
import { when } from "@/lib/format";
import type { OutFile } from "@/lib/types";

const TEXT_LIMIT = 200_000; // characters of a text file shown; the download has the rest

export const fileUrl = (file: Pick<OutFile, "id">) => `/api/files/${encodeURIComponent(file.id)}`;

export type Kind = "image" | "markdown" | "text" | "html" | "pdf" | "audio" | "video" | "other";

/** What a row shows of a file: a session's handed-over one, or a project's (no time). */
export type Shown = Pick<OutFile, "path" | "name" | "size"> & { at?: number };

export function kindOf(file: Pick<OutFile, "mime">): Kind {
  const mime = file.mime.split(";")[0]!.trim();
  if (mime.startsWith("image/")) return "image";
  if (mime === "text/markdown") return "markdown";
  if (mime === "text/html") return "html";
  if (mime === "text/plain" || mime === "text/csv" || mime === "application/json") return "text";
  return "other";
}

export function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function Files({ files }: { files: OutFile[] }) {
  if (!files.length)
    return <p className="text-sm text-muted-foreground">The session has not handed over any files yet.</p>;
  return (
    <ul className="flex flex-col gap-3" aria-label="Session files">
      {files.map((file) => (
        <FileRow key={file.id} file={file} />
      ))}
    </ul>
  );
}

/** One file; `opened`: its page or text shown at once (the `/f/<id>` page). `src`: where its bytes are; `frame`: where
 * its page or PDF is for the frame. */
export function FileRow({
  file,
  opened = false,
  kind = kindOf(file as OutFile),
  src = fileUrl(file as OutFile),
  frame = `${src}/view`,
}: {
  file: Shown;
  opened?: boolean;
  kind?: Kind;
  src?: string;
  frame?: string;
}) {
  const [open, setOpen] = useState(opened);
  const opens = kind === "markdown" || kind === "text" || kind === "html" || kind === "pdf";
  return (
    <li className="rounded-lg border bg-card p-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="break-all text-[15px] font-medium md:text-sm">{file.path}</div>
          <div className="text-xs text-muted-foreground">
            {/* a thing a session shows on a card comes without its size */}
            {[file.size ? size(file.size) : "", file.at ? when(file.at) : ""].filter(Boolean).join(" · ")}
          </div>
        </div>
        {opens && (
          <Button variant="outline" size="sm" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Collapse" : "Open"}
          </Button>
        )}
        <a
          href={src}
          download={file.name}
          className="inline-flex h-9 items-center rounded-md px-3 text-sm font-medium hover:bg-accent md:h-8"
        >
          Download
        </a>
      </div>
      {kind === "image" && (
        <img src={src} alt={file.name} loading="lazy" className="mt-2 max-h-80 max-w-full rounded border" />
      )}
      {kind === "video" && (
        <video src={src} controls preload="metadata" className="mt-2 max-h-80 w-full rounded border">
          <track kind="captions" />
        </video>
      )}
      {kind === "audio" && (
        <audio src={src} controls preload="metadata" className="mt-2 w-full">
          <track kind="captions" />
        </audio>
      )}
      {open && kind === "html" && (
        <iframe
          title={file.name}
          src={frame}
          sandbox=""
          referrerPolicy="no-referrer"
          className="mt-2 h-[70dvh] w-full rounded border bg-white"
        />
      )}
      {/* a PDF in the browser's own viewer, which a sandboxed frame refuses; the platform lets only the face frame it */}
      {open && kind === "pdf" && <iframe title={file.name} src={frame} referrerPolicy="no-referrer" className="mt-2 h-[70dvh] w-full rounded border bg-white" />}
      {open && (kind === "markdown" || kind === "text") && <TextOf src={src} markdown={kind === "markdown"} />}
    </li>
  );
}

function TextOf({ src, markdown }: { src: string; markdown: boolean }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let gone = false;
    fetch(src, { credentials: "same-origin" })
      .then(async (res) => {
        if (!res.ok) throw new Error(String(res.status));
        const body = await res.text();
        if (!gone) setText(body);
      })
      .catch(() => {
        if (!gone) setError("The file did not load.");
      });
    return () => {
      gone = true;
    };
  }, [src]);
  if (error) return <p className="mt-2 text-sm text-destructive">{error}</p>;
  if (text === null) return <p className="mt-2 text-sm text-muted-foreground">Loading…</p>;
  const cut = text.length > TEXT_LIMIT;
  const shown = cut ? text.slice(0, TEXT_LIMIT) : text;
  return (
    <div className="mt-2 max-h-[70dvh] overflow-y-auto rounded border p-3">
      {markdown ? (
        <Markdown text={shown} />
      ) : (
        <pre className="whitespace-pre-wrap break-words font-mono text-xs">{shown}</pre>
      )}
      {cut && <p className="mt-2 text-xs text-muted-foreground">Showing the start — the whole file is under “Download”.</p>}
    </div>
  );
}
