// The week as an hourly grid (FullCalendar v7): meetings grey, the plan's blocks in their goal's colour, the plan's
// window in the owner's zone as the lit hours. A block dragged or stretched is the owner's write of `blocks` — moved
// blocks are locked, a stretched one keeps its new length; a task from the list dropped on the grid is a new locked
// block. The assistant's last plans are lit, and a new one says «was → now» with its «Undo». Loaded lazily: FC is
// ~80 kB gzipped. A prototype for the owner to try (spec: assistant/journal/2026-11-02-plan-grid.md in the Base).
import FullCalendar from "@fullcalendar/react";
import interaction, { Draggable } from "@fullcalendar/react/interaction";
import classic from "@fullcalendar/react/themes/classic";
import timeGrid from "@fullcalendar/react/timegrid";
import "@fullcalendar/react/skeleton.css";
import "@fullcalendar/react/themes/classic/theme.css";
import "./grid.css";
import { useEffect, useMemo, useRef, useState } from "react";
import { useEditing } from "@/components/views/edit";
import { toast } from "@/lib/toast";
import {
  dayOf,
  dayWords,
  duration,
  goalLabel,
  hm,

  minutesOf,
  moment,
  type Placed,
  type Plan,
  placed,
  planDay,
  type Task,
  windowOf,
} from "@/lib/plan";
import { cn } from "@/lib/utils";
import { press, useOwnBlock } from "./shared";

const SWATCH = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"];
const LIT_S = 15 * 60;

/** «HH:MM» of a count of minutes from the day's midnight, past 24:00 when the window crosses it. */
const clock = (m: number) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}:00`;
const minutesOfDay = (ts: number, tz: string) => {
  const [h = 0, m = 0] = hm(ts, tz).split(":").map(Number);
  return h * 60 + m;
};

function useWide() {
  const query = "(min-width: 768px)";
  const [wide, setWide] = useState(() => typeof matchMedia === "undefined" || matchMedia(query).matches);
  useEffect(() => {
    const m = matchMedia(query);
    const on = () => setWide(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return wide;
}

function useDark() {
  const query = "(prefers-color-scheme: dark)";
  const [dark, setDark] = useState(() => typeof matchMedia !== "undefined" && matchMedia(query).matches);
  useEffect(() => {
    const m = matchMedia(query);
    const on = () => setDark(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return dark;
}

/** The assistant's plans of the last quarter-hour, newest first. */
function herPlans(plan: Plan) {
  return Object.values(plan.plan.plans)
    .filter((p) => p.by === "assistant" && !p.undone && plan.now - p.at < LIT_S)
    .sort((a, b) => b.at - a.at);
}

export function GridTab({ plan, today }: { plan: Plan; today: string }) {
  const tz = plan.tz;
  const wide = useWide();
  const dark = useDark();
  const own = useOwnBlock();
  const editing = useEditing();
  const list = useRef<HTMLUListElement>(null);
  const [menu, setMenu] = useState<{ p: Placed; x: number; y: number } | null>(null);
  const all = useMemo(() => placed(plan), [plan]);
  const goals = plan.tasks.goals;
  const first = planDay(plan.now, plan.plan.window);

  // The hours shown: the window in his zone, widened to any meeting of the week outside it.
  const [open, close] = windowOf(first, plan.plan.window);
  const opens = minutesOfDay(open, tz);
  const closes = opens + Math.round((close - open) / 60);
  let lo = opens;
  let hi = closes;
  for (const e of plan.calendar.events) {
    if (e.all_day || e.end < plan.now - 86400 || e.start > plan.now + 8 * 86400) continue;
    const s = minutesOfDay(e.start, tz);
    lo = Math.min(lo, s);
    hi = Math.max(hi, s + Math.round((e.end - e.start) / 60));
  }
  lo = Math.max(0, Math.floor(lo / 60) * 60 - 60);
  hi = Math.min(48 * 60, Math.ceil(hi / 60) * 60 + 60);

  // Her plans: lit, and a new one said once with its «Undo».
  const hers = herPlans(plan);
  const lit = new Set(hers.flatMap((p) => p.blocks));
  const seen = useRef(new Set<string>());
  useEffect(() => {
    for (const p of hers) {
      if (seen.current.has(p.id)) continue;
      seen.current.add(p.id);
      const words = (p.moves ?? []).map((m) => {
        const from = moment(m.from) ?? 0;
        const to = moment(m.to) ?? 0;
        return `${m.title ?? "A block"}: ${dayWords(dayOf(from, tz), today, true)} ${hm(from, tz)} → ${dayWords(dayOf(to, tz), today, true)} ${hm(to, tz)}, ${duration(m.minutes)}`;
      });
      toast(`The assistant moved ${words.join("; ") || "the plan"}`, "info", 12000, undefined, {
        label: "Undo",
        run: () => void press({ action: "undo", plan: p.id }, "Taken back."),
      });
    }
  });

  const events = [
    ...plan.calendar.events.map((e, n) => ({
      id: `e${n}`,
      title: e.title,
      start: new Date(e.start * 1000),
      end: new Date(e.end * 1000),
      allDay: e.all_day,
      editable: false,
      className: cn("rt-meeting", !e.busy && "rt-free"),
      extendedProps: { kind: "meeting" },
    })),
    ...plan.plan.holds.map((h, n) => ({
      id: `h${n}`,
      title: h.label,
      start: new Date(h.from * 1000),
      end: new Date(h.to * 1000),
      display: "background",
    })),
    ...all.map((p) => {
      const n = goals.findIndex((g) => g.value === p.task?.fields.goal);
      const passed = p.block.fields.state === "passed" || p.end <= plan.now;
      return {
        id: p.block.id,
        title: p.task?.fields.title ?? "A task",
        start: new Date(p.start * 1000),
        end: new Date(p.end * 1000),
        color: n < 0 ? "var(--muted-foreground)" : SWATCH[n % SWATCH.length],
        // The running block's end can still be pulled on — «half an hour more»; the past is fixed.
        startEditable: !passed && p.start > plan.now,
        durationEditable: !passed,
        className: cn("rt-block", passed && "rt-passed", lit.has(p.block.id) && "rt-lit", p.block.fields.locked && "rt-locked"),
        extendedProps: { kind: "block", placed: p },
      };
    }),
  ];

  // The tasks with nothing ahead on the grid: dragged from the list onto it.
  const unplanned = plan.tasks.rows.filter(
    (t) => t.fields.state === "open" && !all.some((p) => p.task?.id === t.id && p.end > plan.now && p.block.fields.state === "planned"),
  );
  useEffect(() => {
    if (!list.current || !wide) return;
    const d = new Draggable(list.current, {
      itemSelector: "[data-task]",
      longPressDelay: 300,
      eventData: (el: HTMLElement) => ({
        title: el.dataset.title,
        duration: { minutes: Number(el.dataset.minutes) },
        create: false,
        extendedProps: { task: el.dataset.task },
      }),
    });
    return () => d.destroy();
  }, [wide]);

  const resize = (p: Placed, minutes: number, what: string) =>
    editing
      ? editing.write("blocks", [{ op: { op: "set", id: p.block.id, version: p.block.version, fields: { minutes, locked: true } }, target: p.block.id }], what)
      : Promise.resolve(false);
  const remove = (p: Placed) => void press({ action: "remove", block: p.block.id }, "Taken off the plan.");

  return (
    <div className="flex min-h-0 flex-1 gap-4 px-2 pb-4 md:px-6">
      <div className={cn("rt-grid min-w-0 flex-1", dark && "rt-dark")}>
        <FullCalendar
          plugins={[timeGrid, interaction, classic]}
          colorScheme={dark ? "dark" : "light"}
          initialView={wide ? "timeGridWeek" : "timeGridDay"}
          key={wide ? "week" : "day"}
          initialDate={first}
          firstDay={new Date(`${first}T12:00:00Z`).getUTCDay()}
          timeZone={tz}
          now={new Date(plan.now * 1000)}
          nowIndicator
          headerToolbar={{ start: "title", end: "today prev,next" }}
          toolbarTitleClass="text-[15px] font-medium tracking-normal"
          height="auto"
          allDaySlot
          allDayText=""
          slotMinTime={clock(lo)}
          slotMaxTime={clock(hi)}
          slotDuration="00:30:00"
          slotHeaderFormat={{ hour: "2-digit", minute: "2-digit", hour12: false }}
          eventTimeFormat={{ hour: "2-digit", minute: "2-digit", hour12: false }}
          dayHeaderFormat={{ weekday: "short", day: "numeric" }}
          locale="en-GB"
          snapDuration="00:15:00"
          businessHours={{ daysOfWeek: [0, 1, 2, 3, 4, 5, 6], startTime: clock(opens), endTime: clock(closes) }}
          scrollTime={clock(opens)}
          editable
          droppable
          longPressDelay={300}
          eventLongPressDelay={300}
          eventMinHeight={18}
          events={events}
          eventAllow={(span) => span.end.getTime() / 1000 > plan.now}
          eventDrop={(info) => {
            const p = info.event.extendedProps.placed as Placed | undefined;
            const start = info.event.start;
            if (!p || !start) return info.revert();
            const at = start.getTime() / 1000;
            void own.move(p, at, `Moved to ${dayWords(dayOf(at, tz), today, true)} ${hm(at, tz)}.`).then((ok) => ok || info.revert());
          }}
          eventResize={(info) => {
            const p = info.event.extendedProps.placed as Placed | undefined;
            const { start, end } = info.event;
            if (!p || !start || !end) return info.revert();
            const minutes = Math.round((end.getTime() - start.getTime()) / 60000);
            void resize(p, minutes, `${p.task?.fields.title ?? "The block"}: ${duration(minutes)}.`).then((ok) => ok || info.revert());
          }}
          drop={(info) => {
            // `create: false` on the list's items: nothing lands on the grid but the block the write makes.
            const task = plan.tasks.rows.find((t) => t.id === info.draggedEl.dataset.task) as Task | undefined;
            if (!task) return;
            const at = info.date.getTime() / 1000;
            if (at < plan.now) return;
            void own.put(task, at, minutesOf(task), `Put on ${dayWords(dayOf(at, tz), today, true)} ${hm(at, tz)}.`);
          }}
          eventClick={(info) => {
            const p = info.event.extendedProps.placed as Placed | undefined;
            if (!p) return;
            const r = info.el.getBoundingClientRect();
            setMenu({ p, x: Math.min(r.left, window.innerWidth - 232), y: r.bottom + 4 });
          }}
          eventContent={(arg) => {
            const p = arg.event.extendedProps.placed as Placed | undefined;
            const { start, end } = arg.event;
            // Under 45 minutes there is room for one line: the title, then the time after it.
            const short = !!start && !!end && end.getTime() - start.getTime() < 45 * 60_000;
            const meta = `${arg.timeText}${p?.block.fields.locked ? " · yours" : ""}`;
            return (
              <div className={cn("flex h-full min-w-0 overflow-hidden px-1.5 leading-tight", short ? "items-center gap-1.5" : "flex-col py-0.5")}>
                <span className="max-w-full shrink-0 truncate text-[12px] font-medium">{arg.event.title}</span>
                <span className="min-w-0 truncate font-mono text-[11px] opacity-70 tabular-nums">{meta}</span>
              </div>
            );
          }}
        />
      </div>
      {wide && (
        <aside className="hidden w-60 shrink-0 flex-col gap-2 lg:flex">
          <h2 className="px-1 text-[13px] text-muted-foreground">Not on the grid · {unplanned.length}</h2>
          <ul ref={list} className="flex flex-col gap-1">
            {unplanned.map((t) => (
              <li
                key={t.id}
                data-task={t.id}
                data-title={t.fields.title}
                data-minutes={minutesOf(t)}
                className="cursor-grab rounded-md border bg-card px-2.5 py-1.5 text-[13px] active:cursor-grabbing"
              >
                <span className="block truncate">{t.fields.title}</span>
                <span className="font-mono text-[11px] text-muted-foreground tabular-nums">
                  {duration(minutesOf(t))}
                  {goalLabel(goals, t.fields.goal) ? ` · ${goalLabel(goals, t.fields.goal)}` : ""}
                  {t.fields.due ? ` · by ${dayWords(t.fields.due, today, true)}` : ""}
                </span>
              </li>
            ))}
          </ul>
          <p className="px-1 text-xs text-muted-foreground">Drag one onto the grid. A block you move or stretch stays where you put it.</p>
        </aside>
      )}
      {menu && <BlockMenu menu={menu} close={() => setMenu(null)} resize={resize} remove={remove} tz={tz} />}
    </div>
  );
}

/** A block's menu — the keyboard's and the phone's way to what the mouse does on the grid: its length, off the plan. */
function BlockMenu({
  menu,
  close,
  resize,
  remove,
  tz,
}: {
  menu: { p: Placed; x: number; y: number };
  close: () => void;
  resize: (p: Placed, minutes: number, what: string) => Promise<boolean>;
  remove: (p: Placed) => void;
  tz: string;
}) {
  const { p } = menu;
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.querySelector("button")?.focus();
    const key = (e: KeyboardEvent) => e.key === "Escape" && close();
    const away = (e: PointerEvent) => !ref.current?.contains(e.target as Node) && close();
    addEventListener("keydown", key);
    addEventListener("pointerdown", away);
    return () => {
      removeEventListener("keydown", key);
      removeEventListener("pointerdown", away);
    };
  }, [close]);
  const title = p.task?.fields.title ?? "A task";
  return (
    <div
      ref={ref}
      role="menu"
      aria-label={title}
      style={{ left: Math.max(8, menu.x), top: Math.min(menu.y, window.innerHeight - 180) }}
      className="fixed z-50 flex w-56 flex-col gap-1 rounded-lg border bg-card p-1.5 text-sm shadow-md"
    >
      <p className="truncate px-2 pt-1 text-[13px] font-medium">{title}</p>
      <p className="px-2 pb-1 font-mono text-[11px] text-muted-foreground tabular-nums">
        {hm(p.start, tz)}–{hm(p.end, tz)} · {duration(p.block.fields.minutes)}
      </p>
      <div className="grid grid-cols-4 gap-1 px-1">
        {[30, 60, 90, 120].map((m) => (
          <button
            key={m}
            type="button"
            role="menuitem"
            onClick={() => void resize(p, m, `${title}: ${duration(m)}.`).then(close)}
            className={cn(
              "rounded-md px-1 py-1.5 font-mono text-xs tabular-nums hover:bg-accent focus-visible:bg-accent focus-visible:outline-none",
              p.block.fields.minutes === m && "bg-accent",
            )}
          >
            {m < 60 ? `${m}m` : `${m / 60}h`}
          </button>
        ))}
      </div>
      <button
        type="button"
        role="menuitem"
        onClick={() => {
          remove(p);
          close();
        }}
        className="rounded-md px-2 py-1.5 text-left hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
      >
        Take off the plan
      </button>
    </div>
  );
}
