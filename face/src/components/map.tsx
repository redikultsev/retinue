// The operator's map (docs/face.md «The map», lib/map.ts): the projects as areas, each live session a tile in the
// place it keeps for its whole life — its number while it waits, how it hangs under another, «waits for you»,
// «working» or «done», its title and one line: what it needs, or its plan's step with a thin bar. A press on a tile
// opens its card — the operator's own, with its answer and the microphone — in a pane beside the map, which stays put;
// on a phone the card is a sheet over it. The arrows go from tile to tile, Enter opens, Esc closes.
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { Layers, SphereMark } from "@/components/icons";
import { OperatorCard, useOperatorNumbers } from "@/components/operator";
import { RelationIcon, upOf } from "@/components/relation";
import { OnComputer } from "@/components/ending-card";
import { Crumb, Reply } from "@/components/reply";
import { Dialog } from "@/components/ui/dialog";
import { computerRows, computersLine, isComputer, useComputers } from "@/lib/computers";
import type { Feed } from "@/lib/feed";
import { live, useLiveState } from "@/lib/live";
import { type Area, mapAreas, onMap, type Tile, tileOf } from "@/lib/map";
import { useRoomFor, useWide } from "@/lib/media";
import { useProjectOf, useProjects, within } from "@/lib/projects";
import { listed, orderedItems } from "@/lib/store";
import { latestPlan, planLine } from "@/lib/summary";
import type { Session, Sphere } from "@/lib/types";
import { cn } from "@/lib/utils";
import { requests } from "@/lib/waiting";

/** A column of tiles (18.75rem) and the card's pane (20rem at least) side by side, with their gaps. */
const MAP_AND_CARD_PX = 720;

const WORDS = { waits: "waits for you", working: "working", done: "done" } as const;

/** A working session's plan: «3 of 5 · the step», for its line and its bar; null without a plan the app has read. */
function usePlanOf(s: Session): { done: number; total: number; now: string | null } | null {
  const state = useLiveState();
  const itemsOf = state.items[s.id];
  return useMemo(() => {
    const todos = latestPlan(orderedItems(itemsOf));
    return todos?.length ? planLine(todos) : null;
  }, [itemsOf]);
}

function TileView({ s, tile, on, onOpen }: { s: Session; tile: Tile; on: boolean; onOpen: () => void }) {
  const plan = usePlanOf(s);
  const up = upOf(s);
  const line =
    tile.state === "working" && plan ? `${plan.done + (plan.now ? 1 : 0)} of ${plan.total}${plan.now ? ` · ${plan.now}` : ""}` : tile.line || (tile.state === "working" ? "at work" : "");
  return (
    <button
      type="button"
      data-testid="map-tile"
      data-session={s.id}
      aria-pressed={on}
      aria-label={`${tile.n !== undefined ? `#${tile.n} ` : ""}${s.title || s.track}, ${WORDS[tile.state]}`}
      onClick={onOpen}
      className={cn(
        "flex min-h-[5.5rem] min-w-0 flex-col gap-1 rounded-lg border bg-card px-2.5 py-2 text-left transition-[background-color,opacity] focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
        tile.state === "waits" && "border-warning/70",
        tile.state === "working" && "border-primary/60",
        tile.state === "done" && "opacity-60",
        on ? "bg-accent" : "hover:bg-accent/50",
      )}
    >
      <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
        {tile.n !== undefined && <span className="font-mono font-medium text-warning-foreground tabular-nums dark:text-warning">#{tile.n}</span>}
        {up && <RelationIcon how={up.how} className="size-3 shrink-0" aria-label={up.how} />}
        {tile.state === "working" && <span aria-hidden className="size-1.5 shrink-0 animate-pulse rounded-full bg-primary" />}
        <span className="truncate">{WORDS[tile.state]}</span>
      </span>
      <span className="line-clamp-2 text-[13px] leading-[18px] break-words text-foreground">{s.title || s.track}</span>
      {line && <span className="mt-auto truncate text-xs text-muted-foreground">{line}</span>}
      {tile.state === "working" && plan && plan.total > 0 && (
        <span aria-hidden className="h-1 overflow-hidden rounded-full bg-muted">
          <span className="block h-full rounded-full bg-primary" style={{ width: `${Math.round((plan.done / plan.total) * 100)}%` }} />
        </span>
      )}
    </button>
  );
}

function AreaView({
  area,
  tiles,
  open,
  onOpen,
}: {
  area: Area;
  tiles: Map<string, Tile>;
  open: string | null;
  onOpen: (id: string) => void;
}) {
  const waiting = area.places.filter((s) => s && tiles.get(s.id)?.state === "waits").length;
  return (
    <section aria-label={area.title} data-testid="map-area" className="flex min-w-0 flex-col gap-2">
      <h2 className="flex items-center gap-2 px-0.5 text-[13px] font-medium">
        {area.icon ? <SphereMark icon={area.icon} className="size-3.5 shrink-0 text-muted-foreground" /> : <Layers className="size-3.5 shrink-0 text-muted-foreground" />}
        <span className="min-w-0 truncate">
          {area.within && <span className="font-normal text-muted-foreground">{area.within} / </span>}
          {area.title}
        </span>
        {waiting > 0 && <span className="font-mono text-xs text-warning-foreground tabular-nums dark:text-warning">{waiting}</span>}
      </h2>
      <div className="grid grid-cols-2 gap-2">
        {area.places.map((s, i) =>
          s ? (
            <TileView key={s.id} s={s} tile={tiles.get(s.id)!} on={open === s.id} onOpen={() => onOpen(s.id)} />
          ) : i === area.next ? (
            <span key={`free:${i}`} aria-hidden data-testid="map-next" className="min-h-[5.5rem] rounded-lg border border-dashed" />
          ) : (
            <span key={`free:${i}`} aria-hidden className="min-h-[5.5rem]" />
          ),
        )}
        {area.next === area.places.length && <span aria-hidden data-testid="map-next" className="min-h-[5.5rem] rounded-lg border border-dashed" />}
      </div>
    </section>
  );
}

/** A session with no request on the feed: what it is doing, a way to it and a box to write into it. */
function SessionCard({ s, tile }: { s: Session; tile: Tile }) {
  if (isComputer(s))
    return (
      <article aria-label={s.title || "Untitled"} className="flex flex-col gap-3">
        <Crumb>
          {WORDS[tile.state]} · on {s.computer.device}
        </Crumb>
        <h2 className="text-[15px] font-medium text-balance">{s.title || "Untitled"}</h2>
        <OnComputer computer={{ device: s.computer.device, id: s.id, ...(s.computer.link ? { link: s.computer.link } : {}) }} verb="Open" />
      </article>
    );
  return (
    <article aria-label={s.title || s.track} className="flex flex-col gap-3">
      <Crumb session={s.id}>{WORDS[tile.state]}</Crumb>
      <h2 className="text-[15px] font-medium text-balance">
        <Link to="/s/$id" params={{ id: s.id }} className="underline-offset-4 hover:underline">
          {s.title || s.track}
        </Link>
      </h2>
      {tile.line && <p className="text-sm text-muted-foreground">{tile.line}</p>}
      <Reply session={s.id} placeholder={tile.state === "working" ? "Write to it while it works" : "Reply to go on with it"} />
    </article>
  );
}

export function MapView({
  feed,
  spheres,
  now,
  head,
  project,
}: {
  feed: Feed | null;
  spheres: Sphere[];
  now: number;
  head?: React.ReactNode;
  /** A project's page: only its own and its sub-projects' areas. */
  project?: string;
}) {
  const state = useLiveState();
  const nodes = useProjects();
  const numbers = useOperatorNumbers(feed, now);
  // the card's pane beside the map only where both fit: the open chat takes its room from «Today», not the window's
  const [room, roomy] = useRoomFor<HTMLDivElement>(MAP_AND_CARD_PX);
  const wide = useWide() && roomy;
  const [open, setOpen] = useState<string | null>(null);
  const grid = useRef<HTMLElement>(null);
  const of = useProjectOf(spheres);
  const machines = useComputers();
  // the room's sessions and the computers' (an off computer's are not on the map: nothing there is live)
  const sessions = useMemo(() => {
    const all = [...onMap(listed(state.sessions), now), ...computerRows(machines.filter((m) => !m.off))];
    return project ? all.filter((s) => within(of(s), project)) : all;
  }, [state.sessions, machines, now, project, of]);
  const areas = useMemo(() => mapAreas(sessions, spheres, nodes ?? [], of), [sessions, spheres, nodes, of]);
  const rows = useMemo(() => requests(feed), [feed]);
  const tiles = useMemo(() => new Map(sessions.map((s) => [s.id, tileOf(s, rows, numbers)])), [sessions, rows, numbers]);
  const picked = open ? sessions.find((s) => s.id === open) : undefined;
  const pickedTile = open ? tiles.get(open) : undefined;

  // working sessions' plans: their snapshot read once, then the stream keeps them
  const busy = sessions.filter((s) => s.state === "active" && !isComputer(s)).map((s) => s.id).slice(0, 8).join(","); // a computer's is not in the room
  useEffect(() => {
    for (const id of busy.split(",").filter(Boolean)) if (!(id in live.getState().floors) && !live.isWatched(id)) live.watch(id).catch(() => undefined);
  }, [busy]);

  // the arrows go from tile to tile (two to a row), Enter opens (the button's own), Esc closes the card
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && open) {
        setOpen(null);
        return;
      }
      const all = [...(grid.current?.querySelectorAll<HTMLButtonElement>("[data-testid=map-tile]") ?? [])];
      const at = all.indexOf(document.activeElement as HTMLButtonElement);
      if (at < 0) return;
      const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 2, ArrowUp: -2 }[e.key];
      if (!step) return;
      e.preventDefault();
      all[Math.max(0, Math.min(all.length - 1, at + step))]?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const card =
    picked && pickedTile ? (
      pickedTile.row ? (
        <OperatorCard key={picked.id} row={pickedTile.row} n={pickedTile.n} spheres={spheres} now={now} />
      ) : (
        <SessionCard key={picked.id} s={picked} tile={pickedTile} />
      )
    ) : null;
  return (
    <div ref={room} className="min-h-0 flex-1 overflow-y-auto">
      <div className={cn("grid gap-5 px-4 pt-3 pb-16 md:px-6", wide && "lg:grid-cols-[minmax(0,1fr)_minmax(20rem,24rem)]")}>
        <div className="flex min-w-0 flex-col gap-4">
          {head}
          {sessions.length === 0 ? (
            <p data-testid="empty-why" className="text-sm text-muted-foreground">
              {[project ? "No session of this project is on the map now." : "No session is on the map now: none runs in the room.", computersLine(machines)].filter(Boolean).join(" ")}
            </p>
          ) : (
            <section ref={grid} className="grid grid-cols-[repeat(auto-fill,minmax(18.75rem,1fr))] items-start gap-4" aria-label="The map">
              {areas.map((a) => (
                <AreaView key={a.key || "-"} area={a} tiles={tiles} open={open} onOpen={(id) => setOpen(open === id ? null : id)} />
              ))}
            </section>
          )}
          {sessions.length > 0 && (
            <p className="text-xs text-muted-foreground">
              Every session keeps its place while it lives; the dashed one is where the next appears. Say «3 — book it» to answer #3.
            </p>
          )}
        </div>
        {wide && (
          // the pane is always there, so the map never moves when a card opens
          <aside aria-label="The card" className="flex min-w-0 flex-col gap-3 self-start lg:sticky lg:top-3">
            {card ?? <p className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">Press a tile to open its card here</p>}
          </aside>
        )}
      </div>
      {!wide && (
        <Dialog open={!!card} onClose={() => setOpen(null)} title={picked?.title || "The card"} sheet>
          <div className="p-4">{card}</div>
        </Dialog>
      )}
    </div>
  );
}
