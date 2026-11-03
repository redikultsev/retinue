// The plan's settings: its window — the hours a plan lays tasks into, on one zone's clock wherever the owner is — and
// his activities (meals with a range of starts, the rest for «Now»), as a quiet button with the hours over the grid,
// opening a sheet where he sets them; each save is a press (`hours`, `activities`) the Router keeps.
import { useState } from "react";
import { Clock } from "@/components/icons";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { Activity, PlanWindow } from "@/lib/plan";
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
export function HoursButton({ window: w, activities = [] }: { window: PlanWindow; activities?: Activity[] }) {
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
        {open && (
          <>
            <HoursForm window={w} done={() => setOpen(false)} />
            <ActivitiesForm activities={activities} done={() => setOpen(false)} />
          </>
        )}
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

interface Row extends Activity {
  key: number;
}

const HM = /^\d\d:\d\d$/;

/** His activities: a name and minutes each; a range of starts makes it a meal whose place the plan keeps. */
export function ActivitiesForm({ activities, done }: { activities: Activity[]; done: () => void }) {
  const [rows, setRows] = useState<Row[]>(() => activities.map((a, key) => ({ ...a, key })));
  const [next, setNext] = useState(activities.length);
  const set = (key: number, change: Partial<Activity>) => setRows((r) => r.map((x) => (x.key === key ? { ...x, ...change } : x)));
  const name = (t: string) => t.trim().replace(/\s+/g, " ");
  const names = rows.map((r) => name(r.title).toLowerCase());
  const wrong = (r: Row) =>
    !r.title.trim() ||
    r.title.trim().length > 40 ||
    !Number.isInteger(r.minutes) ||
    r.minutes < 5 ||
    r.minutes > 720 ||
    !!r.from !== !!r.to ||
    (!!r.from && !(HM.test(r.from) && HM.test(r.to ?? "")));
  const twice = new Set(names.filter((n, i) => names.indexOf(n) !== i));
  const ok = rows.length <= 12 && !rows.some(wrong) && twice.size === 0;
  const shaped = (): Activity[] =>
    rows.map((r) => ({ title: name(r.title), minutes: r.minutes, ...(r.from && r.to ? { from: r.from, to: r.to } : {}) }));
  const same = JSON.stringify(shaped()) === JSON.stringify(activities.map((a) => ({ title: a.title, minutes: a.minutes, ...(a.from && a.to ? { from: a.from, to: a.to } : {}) })));
  return (
    <form
      className="flex flex-col gap-3 border-t p-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (ok && !same) void press({ action: "activities", activities: shaped() }, "Activities saved").then((sent) => sent && done());
      }}
    >
      <h3 className="text-[13px] font-medium">Activities</h3>
      <p className="text-[13px] text-muted-foreground">
        What takes your time and is not a task. «Now» holds it from the moment you press. With a range of starts it is a meal: each day the
        plan keeps its minutes free somewhere in that range.
      </p>
      <ul className="flex flex-col gap-2">
        {rows.map((r) => (
          <li key={r.key} className="grid grid-cols-[1fr_4.5rem_auto] items-center gap-2 rounded-md border p-2 max-sm:grid-cols-[1fr_4.5rem]">
            <Input aria-label="Name" value={r.title} maxLength={40} onChange={(e) => set(r.key, { title: e.target.value })} />
            <Input
              aria-label="Minutes"
              type="number"
              min={5}
              max={720}
              value={Number.isNaN(r.minutes) ? "" : r.minutes}
              onChange={(e) => set(r.key, { minutes: e.target.valueAsNumber })}
            />
            <button
              type="button"
              aria-label={`Remove ${r.title || "activity"}`}
              onClick={() => setRows((x) => x.filter((y) => y.key !== r.key))}
              className="h-9 rounded-md px-2 text-[13px] text-muted-foreground hover:bg-accent max-sm:col-span-2 max-sm:justify-self-end max-md:h-11"
            >
              Remove
            </button>
            <div className="col-span-3 flex items-center gap-2 text-xs text-muted-foreground max-sm:col-span-2">
              <span className="shrink-0">Starts between</span>
              <Input aria-label="Starts from" type="time" value={r.from ?? ""} onChange={(e) => set(r.key, { from: e.target.value || undefined })} className="w-28" />
              <span>and</span>
              <Input aria-label="Starts by" type="time" value={r.to ?? ""} onChange={(e) => set(r.key, { to: e.target.value || undefined })} className="w-28" />
            </div>
            {wrong(r) && (
              <span className="col-span-3 text-xs text-destructive max-sm:col-span-2">
                A name, 5 min to 12 h{r.from || r.to ? ", and both ends of the range" : ""}.
              </span>
            )}
            {twice.has(name(r.title).toLowerCase()) && <span className="col-span-3 text-xs text-destructive">Named twice.</span>}
          </li>
        ))}
      </ul>
      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          disabled={rows.length >= 12}
          onClick={() => {
            setRows((r) => [...r, { key: next, title: "", minutes: 30 }]);
            setNext((n) => n + 1);
          }}
          className="h-9 rounded-md border px-3 text-[13px] transition-colors hover:bg-accent disabled:opacity-50 max-md:h-11"
        >
          Add an activity
        </button>
        <button
          type="submit"
          disabled={!ok || same}
          className="h-9 rounded-md bg-primary px-3 text-[13px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50 max-md:h-11"
        >
          Save activities
        </button>
      </div>
    </form>
  );
}
