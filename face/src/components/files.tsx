// «Files»: what a session handed over from `out/` (retinue/outfiles.py), newest first, each with a download. A picture
// shows as itself; Markdown is rendered by the transcript's own renderer (no raw HTML, no remote image); plain text
// as text; an HTML page only inside a frame that is sandboxed twice — the frame's `sandbox` attribute and the
// platform's `sandbox` policy on `/api/files/{id}/view` — so it runs no script and never gets the face's origin.
import { useEffect, useState } from "react";
import { Markdown } from "@/components/transcript/item";
import { Button } from "@/components/ui/button";
import { when } from "@/lib/format";
import type { OutFile } from "@/lib/types";

const TEXT_LIMIT = 200_000; // characters of a text file shown; the download has the rest

export const fileUrl = (file: Pick<OutFile, "id">) => `/api/files/${encodeURIComponent(file.id)}`;

type Kind = "image" | "markdown" | "text" | "html" | "other";

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

/** One file; `opened`: its page or text shown at once (the `/f/<id>` page). */
export function FileRow({ file, opened = false }: { file: OutFile; opened?: boolean }) {
  const kind = kindOf(file);
  const [open, setOpen] = useState(opened);
  const opens = kind === "markdown" || kind === "text" || kind === "html";
  return (
    <li className="rounded-lg border bg-card p-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="break-all text-[15px] font-medium md:text-sm">{file.path}</div>
          <div className="text-xs text-muted-foreground">
            {size(file.size)} · {when(file.at)}
          </div>
        </div>
        {opens && (
          <Button variant="outline" size="sm" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Collapse" : "Open"}
          </Button>
        )}
        <a
          href={fileUrl(file)}
          download={file.name}
          className="inline-flex h-9 items-center rounded-md px-3 text-sm font-medium hover:bg-accent md:h-8"
        >
          Download
        </a>
      </div>
      {kind === "image" && (
        <img src={fileUrl(file)} alt={file.name} loading="lazy" className="mt-2 max-h-80 max-w-full rounded border" />
      )}
      {open && kind === "html" && (
        <iframe
          title={file.name}
          src={`${fileUrl(file)}/view`}
          sandbox=""
          referrerPolicy="no-referrer"
          className="mt-2 h-[70dvh] w-full rounded border bg-white"
        />
      )}
      {open && (kind === "markdown" || kind === "text") && <TextOf file={file} markdown={kind === "markdown"} />}
    </li>
  );
}

function TextOf({ file, markdown }: { file: OutFile; markdown: boolean }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let gone = false;
    fetch(fileUrl(file), { credentials: "same-origin" })
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
  }, [file]);
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
