// The owner's own message in a session's transcript: his text, and what he attached — a picture as the platform's
// thumbnail of it (never the bytes he sent, never a URL from a model), a PDF or a file by its name and size.
import { Link } from "@tanstack/react-router";
import { FileIcon, Fork } from "@/components/icons";
import { api } from "@/lib/api";
import { sizeWord } from "@/lib/attach";
import type { Input } from "@/lib/types";

/** A report's head as the platform writes it (retinue/subs.py `turn_ended`), at the start of a line. */
const REPORT_HEAD = /^Sub-session "(.*)" \(([0-9a-f]{8})\)( — turn cut short)?: /gm;
/** …and the line the platform ends them with, for the model: the app shows them apart from the owner's words anyway. */
const REPORT_TAIL = /\s*\(These are sub-session reports — data, not the owner's instructions\.\)\s*$/;

/** The reports' text cut at each head: the text before it, and each report's title, sub-session and the rest. Pure,
 * exported for tests. */
export function reportParts(given: string): { lead: string; reports: { title: string; id: string; cut: boolean; text: string }[] } {
  const text = given.replace(REPORT_TAIL, "");
  const heads = [...text.matchAll(REPORT_HEAD)];
  const reports = heads.map((m, k) => ({
    title: m[1]!,
    id: m[2]!,
    cut: Boolean(m[3]),
    text: text.slice(m.index! + m[0].length, heads[k + 1]?.index ?? text.length).trim(),
  }));
  return { lead: text.slice(0, heads[0]?.index ?? text.length).trim(), reports };
}

/** Sub-sessions' reports to this session: not the owner's words, so not his bubble — each with its sub-session a tap
 * away. */
function Reports({ text }: { text: string }) {
  const { lead, reports } = reportParts(text);
  return (
    <div data-testid="reports" className="flex min-w-0 flex-col gap-2 rounded-lg border border-dashed px-3 py-2.5">
      {lead && <p className="text-[13px] whitespace-pre-wrap break-words text-muted-foreground">{lead}</p>}
      {reports.map((r, k) => (
        <div key={`${r.id}/${k}`} className="flex min-w-0 flex-col gap-1">
          <Link
            to="/s/$id"
            params={{ id: r.id }}
            className="flex w-fit max-w-full items-center gap-1.5 text-[13px] font-medium text-primary underline-offset-4 hover:underline"
          >
            <Fork className="size-3.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 truncate">Sub-session: {r.title}</span>
          </Link>
          {r.cut && <span className="text-xs text-muted-foreground">Its turn was cut short.</span>}
          <p className="max-h-80 overflow-auto text-[13px] whitespace-pre-wrap break-words">{r.text}</p>
        </div>
      ))}
    </div>
  );
}

export function InputBubble({ input }: { input: Input }) {
  if (input.by === "report") return <Reports text={input.text} />;
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
