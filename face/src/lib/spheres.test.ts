import { describe, expect, it } from "vitest";
import { sphereGroups } from "./spheres";
import type { Session, Sphere } from "./types";

const session = (id: string, over: Partial<Session> = {}): Session => ({
  id, track: "notes", lane: "private", kind: "private", state: "idle", created: 1, at: 1, title: id, waiting: 0,
  ...over,
});
const SPHERES: Sphere[] = [
  { name: "travel", title: "Путешествия", icon: "✈️", track: "notes" },
  { name: "work", title: "Работа", icon: "", track: "notes" },
  { name: "home", title: "Дом", icon: "", track: "notes" },
];
const NOW = 1_000_000;
const ids = (g: { rows: { session: Session; depth: number }[] }) => g.rows.map((r) => `${r.session.id}@${r.depth}`);

describe("sphereGroups", () => {
  it("puts each sphere's main session first, then the others newest first, forks and sides under theirs", () => {
    const { groups, empty } = sphereGroups(
      [
        session("loose", { at: 50 }),
        session("old", { sphere: "travel", at: 10 }),
        session("main", { sphere: "travel", at: 5, main: true }),
        session("new", { sphere: "travel", at: 30 }),
        session("fork", { sphere: "travel", at: 40, fork_of: "main" }),
        session("fork2", { sphere: "travel", at: 41, fork_of: "fork" }),
        session("side", { sphere: "travel", at: 42, parent: "old" }),
        session("gone", { sphere: "mars", at: 1 }),
        session("job", { sphere: "work", at: 2 }),
        session("orphan", { sphere: "work", at: 3, fork_of: "elsewhere" }),
      ],
      SPHERES,
      "all",
      NOW,
    );
    expect(groups.map((g) => [g.key, g.title])).toEqual([
      ["travel", "✈️ Путешествия"],
      ["work", "Работа"],
      ["mars", "mars"],
      ["", "Без сферы"],
    ]);
    expect(ids(groups[0]!)).toEqual(["main@0", "fork@1", "fork2@2", "new@0", "old@0", "side@1"]);
    expect(groups[0]!.rows.find((r) => r.session.id === "side")!.side).toBe(true);
    expect(ids(groups[1]!)).toEqual(["orphan@0", "job@0"]);
    expect(empty.map((s) => s.name)).toEqual(["home"]);
  });

  it("filters by state and keeps a closed main session listed however old", () => {
    const sessions = [
      session("waits", { sphere: "travel", state: "waiting" }),
      session("runs", { sphere: "travel", state: "active" }),
      session("card", { sphere: "travel", state: "active", waiting: 1 }),
      session("pinned", { pinned: true, state: "closed", at: 1 }),
      session("main", { sphere: "work", state: "closed", at: 1, main: true }),
      session("ancient", { sphere: "work", state: "closed", at: 1 }),
    ];
    const keys = (filter: Parameters<typeof sphereGroups>[2]) =>
      sphereGroups(sessions, SPHERES, filter, NOW).groups.flatMap((g) => g.rows.map((r) => r.session.id));
    expect(keys("all")).toEqual(["waits", "runs", "card", "main", "pinned"]);
    expect(keys("waiting")).toEqual(["waits", "card"]);
    expect(keys("active")).toEqual(["runs"]);
    expect(keys("done")).toEqual(["main", "pinned"]);
    expect(keys("pinned")).toEqual(["pinned"]);
  });
});
