// «Tasks» by project (docs/face.md «Tasks and Schedule»): a group a project, in the tree's order, folded to its
// first rows; a row's «⋯» writes the task's `project`, and a task added in a group gets that group's project. Read
// as structure — the groups' keys, the rows' ids, the verbs and their fields —, never as the words on screen.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ScheduleContext } from "@/components/plan/shared";
import { AddTask, FOLDED, ProjectList, ProjectMenu, type Projects } from "@/components/plan/tasks-tab";
import { byProject, type Plan, type Task } from "@/lib/plan";
import type { Schedule } from "@/lib/schedule";

const NOW = Date.parse("2026-11-04T07:00:00Z") / 1000;
const TODAY = "2026-11-04";

const task = (id: string, project?: string): Task => ({
  id, fields: { title: `Task ${id}`, state: "open", ...(project ? { project } : {}) }, version: 2, created: NOW, updated: NOW,
});

function plan(rows: Task[]): Plan {
  return {
    now: NOW,
    tz: "Europe/Berlin",
    tasks: { rows, rev: null, space: "home", goals: [], missing: false, blocks: [] },
    calendar: { events: [], at: NOW, failed: 0, stale: false },
    reminders: [],
    plan: { at: null, plans: {}, holds: [], said: {}, window: { zone: "Europe/Berlin", opens: "09:00", closes: "23:00" } },
  };
}

const PROJECTS: Projects = {
  tree: [
    { key: "work", depth: 0 },
    { key: "work/acme", depth: 1 },
    { key: "travel", depth: 0 },
  ],
  of: (t) => t.fields.project ?? "",
  label: (key) => key,
  name: (key) => key,
};

/** A schedule that only records its verbs. */
function verbs(): Schedule & Record<keyof Schedule, ReturnType<typeof vi.fn>> {
  const keys: (keyof Schedule)[] = ["add", "set", "put", "move", "resize", "remove", "plan", "place", "undo", "busy", "now", "free", "off", "hours", "activities"];
  return Object.fromEntries(keys.map((k) => [k, vi.fn().mockResolvedValue(true)])) as never;
}

afterEach(() => vi.restoreAllMocks());

describe("Tasks by project", () => {
  it("shows a group a project in the tree's order, «No project» last, each folded to its first rows until opened", () => {
    const acme = Array.from({ length: FOLDED + 2 }, (_, n) => task(`a${n}`, "work/acme"));
    const rows = [task("n1"), task("t1", "travel"), ...acme];
    const groups = byProject(rows, PROJECTS.of, PROJECTS.tree);
    const { container } = render(
      <ScheduleContext value={verbs()}>
        <ProjectList plan={plan(rows)} groups={groups} today={TODAY} projects={PROJECTS} />
      </ScheduleContext>,
    );
    const sections = [...container.querySelectorAll<HTMLElement>("section[data-group]")];
    expect(sections.map((s) => s.dataset.group)).toEqual(["work/acme", "travel", ""]);
    const ids = (s: HTMLElement) => [...s.querySelectorAll<HTMLElement>("[data-task]")].map((r) => r.dataset.task);
    const [work] = sections;
    expect(ids(work!)).toEqual(acme.slice(0, FOLDED).map((t) => t.id));
    expect(sections.slice(1).map((s) => s.querySelector("[data-fold]"))).toEqual([null, null]);
    fireEvent.click(work!.querySelector("[data-fold]")!);
    expect(ids(work!)).toEqual(acme.map((t) => t.id));
  });

  it("a row's menu lists «No project», then the tree in its order, and a pick writes the task's project — none takes it off", async () => {
    const s = verbs();
    const t = task("r1", "work/acme");
    render(
      <ScheduleContext value={s}>
        <ProjectMenu task={t} projects={PROJECTS} />
      </ScheduleContext>,
    );
    fireEvent.click(screen.getByRole("button"));
    const items = screen.getAllByRole("menuitemradio");
    expect(items.map((i) => i.dataset.project)).toEqual(["", "work", "work/acme", "travel"]);
    expect(items.map((i) => i.getAttribute("aria-checked"))).toEqual(["false", "false", "true", "false"]);
    fireEvent.click(items[2]!); // where it is already: nothing to write
    expect(s.set).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button"));
    fireEvent.click(screen.getAllByRole("menuitemradio")[3]!);
    await waitFor(() => expect(s.set).toHaveBeenCalledWith(t, { project: "travel" }, expect.any(String)));
    fireEvent.click(screen.getByRole("button"));
    fireEvent.click(screen.getAllByRole("menuitemradio")[0]!);
    await waitFor(() => expect(s.set).toHaveBeenLastCalledWith(t, { project: null }, expect.any(String)));
  });

  it("a task added in a project's group gets that project; the box above the groups adds one of none", async () => {
    const s = verbs();
    const add = (node: React.ReactNode) => {
      const view = render(<ScheduleContext value={s}>{node}</ScheduleContext>);
      const box = screen.getByRole("textbox");
      fireEvent.change(box, { target: { value: "Call Acme" } });
      fireEvent.submit(box.closest("form")!);
      return view;
    };
    add(<AddTask project="work/acme" onClose={() => {}} />).unmount();
    await waitFor(() => expect(s.add).toHaveBeenCalledWith({ title: "Call Acme", project: "work/acme" }, expect.any(String)));
    add(<AddTask />);
    await waitFor(() => expect(s.add).toHaveBeenLastCalledWith({ title: "Call Acme" }, expect.any(String)));
  });
});
