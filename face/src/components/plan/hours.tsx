// The plan's settings: its window — the hours a plan lays tasks into, on one zone's clock wherever the owner is — as a
// quiet button with those hours over the grid, opening a sheet where he sets them; a save is a press (`hours`) the
// Router keeps.
import { useState } from "react";
import { Clock } from "@/components/icons";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { PlanWindow } from "@/lib/plan";
import { press } from "./shared";

/** A zone the browser knows: the list has canonical names only (no «UTC» in Chrome, no old aliases), so any name it
 * can format a date in is one too. */
function isZone(zone: string): boolean {
  try {
    new Intl.DateTimeFormat("en", { timeZone: zone });
    return zone.length > 0;
  } catch {
    return false;
  }
}

/** Minutes from `opens` to `closes`, past midnight when the close is earlier. */
function span(opens: string, closes: string): number {
  const m = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
  return (m(closes) - m(opens) + 1440) % 1440;
}

const ZONES: string[] = (() => {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
})();

/** The plan's window, as a quiet button with its hours: it opens the settings, where the owner sets them. */
export function HoursButton({ window: w }: { window: PlanWindow }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        aria-label="Plan settings"
        title="Plan settings"
        onClick={() => setOpen(true)}
        className="mr-1 inline-flex h-8 items-center gap-1.5 rounded-md px-2 font-mono text-xs text-muted-foreground tabular-nums transition-colors hover:bg-accent hover:text-foreground max-md:h-10"
      >
        <Clock className="size-3.5" />
        {w.opens}–{w.closes}
      </button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Plan settings" sheet side>
        {open && <HoursForm window={w} done={() => setOpen(false)} />}
      </Dialog>
    </>
  );
}

export function HoursForm({ window: w, done }: { window: PlanWindow; done: () => void }) {
  const [opens, setOpens] = useState(w.opens);
  const [closes, setCloses] = useState(w.closes);
  const [zone, setZone] = useState(w.zone);
  const known = zone === w.zone || isZone(zone);
  const same = opens === w.opens && closes === w.closes && zone === w.zone;
  const times = /^\d\d:\d\d$/.test(opens) && /^\d\d:\d\d$/.test(closes);
  const long = times && span(opens, closes) >= 120;
  const ok = long && known;
  const label = "flex flex-col gap-1.5 text-[13px] font-medium";
  return (
    <form
      className="flex flex-col gap-4 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (ok && !same) void press({ action: "hours", opens, closes, zone }, `Plan window: ${opens}–${closes}`).then((sent) => sent && done());
      }}
    >
      <p className="text-[13px] text-muted-foreground">
        The hours a plan lays tasks into, on this zone&apos;s clock wherever you are. A close before the open is after midnight.
      </p>
      <div className="grid grid-cols-2 gap-3">
        <label className={label}>
          Opens
          <Input type="time" value={opens} onChange={(e) => setOpens(e.target.value)} required />
        </label>
        <label className={label}>
          Closes
          <Input type="time" value={closes} onChange={(e) => setCloses(e.target.value)} required />
        </label>
      </div>
      {times && !long && <span className="-mt-2 text-xs text-destructive">A window is at least two hours.</span>}
      <label className={label}>
        Zone
        <Input list="plan-zones" value={zone} onChange={(e) => setZone(e.target.value.trim())} spellCheck={false} autoComplete="off" required />
        <datalist id="plan-zones">
          {ZONES.map((z) => <option key={z} value={z} />)}
        </datalist>
        {!known && <span className="text-xs font-normal text-destructive">Not a zone: pick one from the list.</span>}
      </label>
      <p className="text-xs text-muted-foreground">The next plan uses it; blocks already on the grid are not moved for it.</p>
      <div className="flex justify-end gap-2">
        <button type="button" onClick={done} className="h-9 rounded-md border px-3 text-[13px] transition-colors hover:bg-accent max-md:h-11">
          Cancel
        </button>
        <button
          type="submit"
          disabled={!ok || same}
          className="h-9 rounded-md bg-primary px-3 text-[13px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50 max-md:h-11"
        >
          Save
        </button>
      </div>
    </form>
  );
}
