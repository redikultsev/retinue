// «Schedule»: the week as an hourly grid (FullCalendar v7; a day on a phone) — meetings grey, the plan's blocks in their
// goal's colour, the plan's window in the owner's zone as the lit hours — under the bar of presses: «Plan my day»,
// «Plan my week», «Undo» of the last plan, «Busy now», and what the last press came to. A block dragged or stretched
// is the owner's write of `blocks`, locked: a stretched one is the task's length from then on (the Router's layout
// counts it). Tasks get their time on «Tasks» («Plan») or from «Plan my day/week»; the bar counts the open ones with
// nothing ahead and opens «Tasks». The assistant's plans of the last quarter-hour are lit, and a new one is said once
// with its «Undo». Loaded lazily: FC is ~85 kB gzipped.
import FullCalendar, { type CalendarRef } from "@fullcalendar/react";
import interaction from "@fullcalendar/react/interaction";
import classic from "@fullcalendar/react/themes/classic";
import timeGrid from "@fullcalendar/react/timegrid";
import "@fullcalendar/react/skeleton.css";
import "@fullcalendar/react/themes/classic/theme.css";
import "./grid.css";
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { Close } from "@/components/icons";
import { useEditing } from "@/components/views/edit";
import { toast } from "@/lib/toast";
import {
  dayOf,
  dayWords,
  duration,
  hm,
  type Placed,
  type Plan,
  placed,
  planDay,
  shorts,
  windowOf,
} from "@/lib/plan";
import { cn } from "@/lib/utils";
import { Popover, press, useOwnBlock } from "./shared";

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

/** The assistant's plans of the last quarter-hour not taken back, newest first. */
function herPlans(plan: Plan) {
  return (plan.plan.recent ?? []).filter((p) => p.by === "assistant" && !p.undone && plan.now - p.at < LIT_S);
}

/** The owner's last plan not taken back, while it is the newest of all: its «Undo». */
function lastOwn(plan: Plan) {
  const newest = (plan.plan.recent ?? [])[0];
  return newest && newest.by === "owner" && !newest.undone && !newest.failed ? newest : null;
}

export function GridTab({ plan, today }: { plan: Plan; today: string }) {
  const tz = plan.tz;
  const wide = useWide();
  const dark = useDark();
  const own = useOwnBlock();
  const editing = useEditing();
  const calendar = useRef<CalendarRef>(null);
  const [title, setTitle] = useState("");
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

  // Her plans: lit, and one made while the grid is open said once with its «Undo».
  const hers = herPlans(plan);
  const ids = new Set(hers.map((p) => p.id));
  const lit = new Set(all.filter((p) => p.block.fields.plan && ids.has(p.block.fields.plan)).map((p) => p.block.id));
  const opened = useRef(plan.now);
  const seen = useRef(new Set<string>());
  useEffect(() => {
    for (const p of hers) {
      if (seen.current.has(p.id) || p.at < opened.current - 60) continue;
      seen.current.add(p.id);
      const n = all.filter((x) => x.block.fields.plan === p.id).length;
      toast(`The assistant changed the plan: ${n} ${n === 1 ? "block" : "blocks"}, lit on the grid.`, "info", 12000, undefined, {
        label: "Undo",
        run: () => void press({ action: "undo", plan: p.id }, "Taking it back…"),
      });
    }
  });

  // The tasks with nothing ahead on the grid: dragged from the list onto it.
  const unplanned = plan.tasks.rows.filter(
    (t) => t.fields.state === "open" && !all.some((p) => p.task?.id === t.id && p.end > plan.now && p.block.fields.state === "planned"),
  );
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


  const resize = (p: Placed, minutes: number, what: string) =>
    editing
      ? editing.write("blocks", [{ op: { op: "set", id: p.block.id, version: p.block.version, fields: { minutes, locked: true } }, target: p.block.id }], what)
      : Promise.resolve(false);
  const remove = (p: Placed) => void press({ action: "remove", block: p.block.id }, "Taken off the plan.");

  return (
    <div className="flex min-h-0 flex-1 gap-4 px-2 pt-3 pb-4 md:px-6">
      <div className={cn("rt-grid relative flex min-w-0 flex-1 flex-col gap-3", dark && "rt-dark")}>
        <PlanBar plan={plan} first={first} today={today} unplanned={unplanned.length}
          nav={{ title, go: (to) => calendar.current?.getApi()[to]() }}
        />
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
          ref={calendar}
          headerToolbar={false}
          datesSet={(info) => setTitle(info.view.title)}
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
          longPressDelay={300}
          eventLongPressDelay={300}
          eventMinHeight={20}
          slotMinHeight={wide ? 26 : 30}
          slotLaneClass={(info) => (info.isMinor ? "rt-minor" : "")}
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
          eventClick={(info) => {
            const p = info.event.extendedProps.placed as Placed | undefined;
            if (!p) return;
            const r = info.el.getBoundingClientRect();
            setMenu({ p, x: Math.min(r.left, window.innerWidth - 232), y: r.bottom + 4 });
          }}
          eventContent={(arg) => {
            const p = arg.event.extendedProps.placed as Placed | undefined;
            const { start, end } = arg.event;
            // Under 45 minutes there is room for one line: the title, then the time after it; longer, the title
            // wraps to as many lines as the block has room for, the time under it.
            const short = !!start && !!end && end.getTime() - start.getTime() < 45 * 60_000;
            const meta = `${arg.timeText}${p?.block.fields.locked ? " · yours" : ""}`;
            return short ? (
              <div className="flex h-full min-w-0 items-center gap-1.5 overflow-hidden px-1.5 leading-tight">
                <span className="max-w-full shrink-0 truncate text-[12px] font-medium">{arg.event.title}</span>
                <span className="min-w-0 truncate font-mono text-[11px] opacity-70 tabular-nums">{meta}</span>
              </div>
            ) : (
              <div className="flex h-full min-w-0 flex-col overflow-hidden px-1.5 py-1 leading-[1.25]">
                <span className="text-[12px] font-medium break-words text-pretty">{arg.event.title}</span>
                <span className="mt-0.5 truncate font-mono text-[11px] opacity-70 tabular-nums">{meta}</span>
              </div>
            );
          }}
        />
      </div>
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

/** The presses over the grid, and what the last one came to. */
function PlanBar({ plan, first, today, unplanned, nav }: {
  plan: Plan;
  first: string;
  today: string;
  unplanned: number;
  nav: { title: string; go: (to: "prev" | "next" | "today") => void };
}) {
  const own = lastOwn(plan);
  const short = [...shorts(plan, today)];
  const s = plan.plan.said;
  const said = s?.text && (s.at ?? 0) > plan.now - 600 ? s.text : null;
  const age = plan.calendar.at ? Math.round((plan.now - plan.calendar.at) / 60) : null;
  const button = "h-8 rounded-md border px-3 text-[13px] transition-colors hover:bg-accent max-md:h-10";
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void press({ action: "plan", day: first }, `Planning ${dayWords(first, today).toLowerCase()}…`)}
          className="h-8 rounded-md bg-primary px-3 text-[13px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 max-md:h-10"
        >
          Plan my day
        </button>
        <button type="button" onClick={() => void press({ action: "plan", day: first, days: 7 }, "Planning the week…")} className={button}>
          Plan my week
        </button>
        {own && (
          <button type="button" onClick={() => void press({ action: "undo", plan: own.id }, "Taking the plan back…")} className={button}>
            Undo plan
          </button>
        )}
        <BusyMenu plan={plan} />
        {unplanned > 0 && (
          <Link
            to="/today"
            search={{ tab: "tasks" }}
            className="inline-flex h-8 items-center gap-1.5 rounded-md bg-warning/15 px-2.5 text-[13px] text-warning-foreground transition-colors hover:bg-warning/25 max-md:h-10 dark:text-warning"
          >
            Not planned <span className="font-mono tabular-nums">{unplanned}</span>
          </Link>
        )}
        {plan.calendar.stale && (
          <span className="text-xs text-muted-foreground">
            {age === null ? "The calendar has not been read yet" : `Calendar read ${duration(Math.max(age, 1))} ago`}
          </span>
        )}
        <div className="ml-auto flex items-center gap-1">
          <span className="mr-1 text-[13px] font-medium tabular-nums">{nav.title}</span>
          <button type="button" aria-label="Back" onClick={() => nav.go("prev")} className="h-8 w-8 rounded-md border text-[13px] hover:bg-accent max-md:h-10 max-md:w-10">
            ‹
          </button>
          <button type="button" onClick={() => nav.go("today")} className={button}>
            Today
          </button>
          <button type="button" aria-label="On" onClick={() => nav.go("next")} className="h-8 w-8 rounded-md border text-[13px] hover:bg-accent max-md:h-10 max-md:w-10">
            ›
          </button>
        </div>
      </div>
      {short.length > 0 && (
        <Link to="/today" search={{ tab: "tasks" }} className="w-fit text-xs text-destructive hover:underline">
          Short before the deadline: {short.map(([, x]) => `${x.title} ${duration(x.minutes)}`).join(", ")}
        </Link>
      )}
      {said && <p className="text-xs text-muted-foreground">{said}</p>}
    </div>
  );
}

/** «Busy now»: the time from now held, and the plan's blocks it crosses laid again after it. */
function BusyMenu({ plan }: { plan: Plan }) {
  const held = plan.plan.holds.find((h) => h.from <= plan.now && h.to > plan.now);
  if (held)
    return (
      <button
        type="button"
        onClick={() => void press({ action: "free" }, "Freeing the time…")}
        className="inline-flex h-8 items-center gap-1.5 rounded-md border px-3 text-[13px] transition-colors hover:bg-accent max-md:h-10"
      >
        Busy till {hm(held.to, plan.tz)}
        <Close className="size-3.5" />
      </button>
    );
  return (
    <Popover label="Busy now" button="Busy now">
      {(close) => (
        <div className="flex flex-col text-[13px]">
          {([30, 60, 120] as const).map((m) => (
            <button
              key={m}
              type="button"
              role="menuitem"
              onClick={() => {
                close();
                void press({ action: "busy", minutes: m }, `Busy for ${duration(m)}: moving what it crosses…`);
              }}
              className="flex h-8 items-center rounded-md px-2 text-left transition-colors hover:bg-accent max-md:h-10"
            >
              {duration(m)}
            </button>
          ))}
        </div>
      )}
    </Popover>
  );
}

