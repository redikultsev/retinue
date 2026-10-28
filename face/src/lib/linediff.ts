// A small line diff for the edits shown inline in a transcript (an Edit's old_string → new_string): LCS over
// lines, unchanged runs longer than 2·context folded. The «Изменения» tab renders `git diff` with @pierre/diffs.

export interface DiffRow {
  type: " " | "-" | "+" | "…";
  text: string;
}

const MAX_CELLS = 250_000; // past ~500×500 lines the table is not worth it: show all old, then all new

export function lineDiff(before: string, after: string, context = 3): DiffRow[] {
  const a = before === "" ? [] : before.split("\n");
  const b = after === "" ? [] : after.split("\n");
  let rows: DiffRow[];
  if (a.length * b.length > MAX_CELLS) {
    rows = [...a.map((text) => ({ type: "-" as const, text })), ...b.map((text) => ({ type: "+" as const, text }))];
  } else {
    // lcs[i][j]: the LCS length of a[i:] and b[j:]
    const lcs = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1));
    for (let i = a.length - 1; i >= 0; i--) {
      for (let j = b.length - 1; j >= 0; j--) {
        lcs[i]![j] = a[i] === b[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
      }
    }
    rows = [];
    let i = 0;
    let j = 0;
    while (i < a.length && j < b.length) {
      if (a[i] === b[j]) {
        rows.push({ type: " ", text: a[i]! });
        i++;
        j++;
      } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
        rows.push({ type: "-", text: a[i++]! });
      } else {
        rows.push({ type: "+", text: b[j++]! });
      }
    }
    while (i < a.length) rows.push({ type: "-", text: a[i++]! });
    while (j < b.length) rows.push({ type: "+", text: b[j++]! });
  }
  return fold(rows, context);
}

function fold(rows: DiffRow[], context: number): DiffRow[] {
  const changed = rows.map((r) => r.type !== " ");
  const keep = rows.map((_, k) => {
    for (let d = -context; d <= context; d++) if (changed[k + d]) return true;
    return false;
  });
  const out: DiffRow[] = [];
  let skipped = 0;
  rows.forEach((row, k) => {
    if (keep[k]) {
      if (skipped) out.push({ type: "…", text: `${skipped} без изменений` });
      skipped = 0;
      out.push(row);
    } else skipped++;
  });
  if (skipped && out.length) out.push({ type: "…", text: `${skipped} без изменений` });
  return out;
}

/** Counts for a header: +added −removed. */
export function diffCounts(rows: DiffRow[]): { added: number; removed: number } {
  return {
    added: rows.filter((r) => r.type === "+").length,
    removed: rows.filter((r) => r.type === "-").length,
  };
}
