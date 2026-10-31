// The owner's own message in a session's transcript: his text, and what he attached — a picture as the platform's
// thumbnail of it (never the bytes he sent, never a URL from a model), a PDF or a file by its name and size.
import { FileIcon } from "@/components/icons";
import { api } from "@/lib/api";
import { sizeWord } from "@/lib/attach";
import type { Input } from "@/lib/types";

export function InputBubble({ input }: { input: Input }) {
  const files = input.attachments ?? [];
  return (
    <div data-testid="input" className="ml-auto flex max-w-[85%] flex-col items-end gap-1.5">
      {files.length > 0 && (
        <div className="flex flex-wrap justify-end gap-1.5">
          {files.map((f, n) =>
            f.kind === "image" && f.thumb ? (
              <img
                key={`${input.input_id}/${n}`}
                src={api.thumbUrl(input.session, input.input_id, n)}
                alt={f.name || "photo"}
                title={f.what}
                loading="lazy"
                className="max-h-40 max-w-full rounded-xl border object-cover"
              />
            ) : (
              <span
                key={`${input.input_id}/${n}`}
                title={f.what}
                className="flex max-w-full items-center gap-1.5 rounded-xl border bg-muted/50 px-2.5 py-1.5 text-xs"
              >
                <FileIcon className="size-4 shrink-0" />
                <span className="min-w-0 truncate">{f.name || f.what}</span>
                <span className="shrink-0 text-muted-foreground">{sizeWord(f.size)}</span>
              </span>
            ),
          )}
        </div>
      )}
      {input.text.trim() && (
        <div className="rounded-2xl bg-bubble px-3.5 py-2 text-[15px] whitespace-pre-wrap break-words md:text-sm">
          {input.text}
        </div>
      )}
    </div>
  );
}
