// One file of a diff rendered by @pierre/diffs whose lines take a tap: the box opens under the line, a comment waits
// under it until the tray sends it (components/remarks.tsx). Loaded only with a diff renderer that is open.
import type { DiffLineAnnotation } from "@pierre/diffs";
import { FileDiff, type FileDiffMetadata } from "@pierre/diffs/react";
import { RemarkBox, RemarkNote, type Target, useLineRemarks } from "@/components/remarks";
import type { Remark } from "@/lib/remarks";

type Note = { kind: "edit"; target: Target; initial: string } | { kind: "note"; remark: Remark };

export function RemarkableFileDiff({ session, file }: { session: string; file: FileDiffMetadata }) {
  const r = useLineRemarks(session, file.name);
  const editing = r.target;
  const annotations: DiffLineAnnotation<Note>[] = r.mine
    .filter((m) => !(editing && editing.side === m.side && editing.line === m.line))
    .map((m) => ({ side: m.side, lineNumber: m.line, metadata: { kind: "note", remark: m } }));
  if (editing) {
    const initial = r.at(editing.side, editing.line)?.text ?? "";
    annotations.push({ side: editing.side, lineNumber: editing.line, metadata: { kind: "edit", target: editing, initial } });
  }
  return (
    <FileDiff<Note>
      fileDiff={file}
      className="overflow-hidden rounded-lg border"
      options={{
        theme: { dark: "pierre-dark", light: "pierre-light" },
        themeType: "system",
        diffStyle: "unified",
        overflow: "wrap",
        onLineClick: (props) => r.open(props.annotationSide, props.lineNumber, props.lineElement.textContent ?? ""),
      }}
      lineAnnotations={annotations}
      renderAnnotation={(a) => {
        const m = a.metadata;
        if (!m) return null;
        if (m.kind === "note") return <RemarkNote remark={m.remark} onRemove={() => r.remove(m.remark)} />;
        return <RemarkBox key={`${m.target.side}:${m.target.line}`} target={m.target} initial={m.initial} onCancel={r.close} onAdd={(text) => r.add(m.target, text)} />;
      }}
    />
  );
}
