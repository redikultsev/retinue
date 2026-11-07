// The owner's computers' own sessions (docs/face.md «From the computers», retinue/computers.py): each device reports
// the Claude Code sessions it runs, and the list shows them under their projects beside the room's, as rows that open
// nothing here — on the computer, the row's link opens the session there. Read every `EVERY_MS` while the app is open
// and on screen; nothing is kept on the device.
import { useEffect, useSyncExternalStore } from "react";
import { request } from "./api";
import { paths } from "./contract";
import type { Session } from "./types";

/** One session as its device reported it. */
export interface ComputerSession {
  id: string;
  title: string;
  /** The folder it runs in, on that computer: the row's tooltip only. */
  folder: string;
  /** The Base folder the computer files it under (`work/acme`); "" none: «Other». */
  project: string;
  state: "working" | "waiting" | "done";
  /** Its last activity, in seconds. */
  at: number;
  /** `claude://…`: the session on that computer. */
  link?: string;
}

export interface Computer {
  device: string;
  /** When it last reported, in seconds. */
  at: number;
  /** No report for longer than the platform waits: its rows are greyed and count as neither waiting nor working. */
  off: boolean;
  sessions: ComputerSession[];
}

/** A computer's session as the list's rows take one: `computer` marks it, its id never meets a room session's. */
export interface ComputerRow extends Session {
  computer: { device: string; off: boolean; seen: number; folder: string; link?: string };
}

export const isComputer = (s: Session): s is ComputerRow => "computer" in s && !!(s as ComputerRow).computer;

const EVERY_MS = 15_000;
let computers: Computer[] = [];
const subs = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let users = 0;

/** For tests and the mock: the computers as given. */
export function setComputers(list: Computer[]): void {
  computers = list;
  for (const f of subs) f();
}

function read(): void {
  if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
  request<{ computers?: Computer[] }>(paths.computers)
    .then((answer) => setComputers(answer.computers ?? []))
    .catch(() => undefined); // a platform before it, or offline: the list as it was
}

/** Every computer that reported, read now and every `EVERY_MS` while a list is on screen. */
export function useComputers(): Computer[] {
  useEffect(() => {
    users += 1;
    if (users === 1) {
      read();
      timer = setInterval(read, EVERY_MS);
    }
    return () => {
      users -= 1;
      if (users === 0 && timer) {
        clearInterval(timer);
        timer = null;
      }
    };
  }, []);
  return useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => subs.delete(f);
    },
    () => computers,
    () => computers,
  );
}

/** The computers' sessions as rows of the list: working → active, waiting → waiting, done → closed; an off
 * computer's rows → closed, so the filters «Waiting» and «Working» leave them out. */
export function computerRows(list: Computer[]): ComputerRow[] {
  return list.flatMap((c) =>
    c.sessions.map((s) => ({
      id: `computer:${c.device}:${s.id}`,
      track: c.device,
      lane: "",
      kind: "computer",
      state: c.off ? "closed" : s.state === "working" ? "active" : s.state === "waiting" ? "waiting" : "closed",
      created: s.at,
      at: s.at,
      title: s.title,
      waiting: 0,
      sphere: null,
      project: s.project,
      computer: { device: c.device, off: c.off, seen: c.at, folder: s.folder, ...(s.link ? { link: s.link } : {}) },
    })),
  );
}
