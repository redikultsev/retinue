import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Files, kindOf, size } from "@/components/files";
import { initialState, orderedFiles, reduce, type Action } from "@/lib/store";
import type { OutFile, Session } from "@/lib/types";
import { FilePage } from "@/pages/file";

const file = (id: string, over: Partial<OutFile> = {}): OutFile => ({
  id,
  session: "s1",
  path: `${id}.md`,
  name: `${id}.md`,
  size: 2048,
  mime: "text/markdown; charset=utf-8",
  at: 100,
  ...over,
});

const session: Session = {
  id: "s1",
  track: "notes",
  lane: "private",
  kind: "private",
  state: "idle",
  created: 1,
  at: 1,
  title: "отчёт",
  waiting: 0,
};

const run = (actions: Action[]) => actions.reduce(reduce, initialState);

afterEach(() => vi.unstubAllGlobals());

describe("the files of a session in the store", () => {
  it("takes them from the snapshot and the stream, and keeps a file the stream brought after the snapshot", () => {
    const st = run([
      { type: "event", id: 12, event: { kind: "file", data: file("late", { at: 300 }) } },
      { type: "session", snapshot: { schema: 1, cursor: 10, session, items: [], approvals: [], summaries: [], files: [file("a")] } },
      { type: "event", id: 9, event: { kind: "file", data: file("old") } },
      { type: "event", id: 13, event: { kind: "file", data: file("b", { at: 200 }) } },
    ]);
    expect(orderedFiles(st.files.s1).map((f) => f.id)).toEqual(["late", "b", "a"]);
  });

  it("reads a snapshot from a platform without files as none", () => {
    const st = run([{ type: "session", snapshot: { schema: 1, cursor: 1, session, items: [], approvals: [], summaries: [] } }]);
    expect(orderedFiles(st.files.s1)).toEqual([]);
  });
});

describe("Files", () => {
  it("lists each file with its download, a picture as itself, and says when there is none", () => {
    const { rerender } = render(<Files files={[]} />);
    expect(screen.getByText("Сессия пока не отдала ни одного файла.")).toBeTruthy();
    rerender(<Files files={[file("chart", { path: "img/chart.png", name: "chart.png", mime: "image/png", size: 3 * 1024 * 1024 })]} />);
    expect(screen.getByText("img/chart.png")).toBeTruthy();
    expect(screen.getByText(/3,0 МБ/)).toBeTruthy();
    const link = screen.getByText("Скачать") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/api/files/chart");
    expect(link.getAttribute("download")).toBe("chart.png");
    expect((screen.getByAltText("chart.png") as HTMLImageElement).getAttribute("src")).toBe("/api/files/chart");
    expect(screen.queryByText("Открыть")).toBeNull();
  });

  it("opens an HTML page only in a frame sandboxed with nothing allowed", () => {
    render(<Files files={[file("page", { path: "page.html", name: "page.html", mime: "text/html; charset=utf-8" })]} />);
    expect(document.querySelector("iframe")).toBeNull();
    fireEvent.click(screen.getByText("Открыть"));
    const frame = document.querySelector("iframe")!;
    expect(frame.getAttribute("sandbox")).toBe("");
    expect(frame.getAttribute("src")).toBe("/api/files/page/view");
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it("renders Markdown with the transcript's renderer: no image loads from the network", async () => {
    const fetched = vi.fn(async () => new Response("# Итог\n\n![x](https://evil.example/leak.png)\n\n<img src=https://evil.example/a>"));
    vi.stubGlobal("fetch", fetched);
    render(<Files files={[file("report")]} />);
    fireEvent.click(screen.getByText("Открыть"));
    await waitFor(() => expect(screen.getByText("Итог")).toBeTruthy());
    expect(fetched).toHaveBeenCalledWith("/api/files/report", { credentials: "same-origin" });
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByTestId("image-as-text").textContent).toContain("https://evil.example/leak.png");
  });

  it("knows the kinds and says sizes in Russian", () => {
    expect(kindOf({ mime: "application/json" })).toBe("text");
    expect(kindOf({ mime: "application/pdf" })).toBe("other");
    expect(kindOf({ mime: "image/svg+xml" })).toBe("image");
    expect([size(10), size(2048), size(1536 * 1024)]).toEqual(["10 Б", "2 КБ", "1,5 МБ"]);
  });
});

describe("FilePage", () => {
  function mount(load: (id: string) => Promise<{ file: OutFile }>) {
    const root = createRootRoute();
    const page = createRoute({ getParentRoute: () => root, path: "/f/$id", component: () => <FilePage id="page" load={load} /> });
    const session = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => null });
    const index = createRoute({ getParentRoute: () => root, path: "/", component: () => null });
    const router = createRouter({
      routeTree: root.addChildren([page, session, index]),
      history: createMemoryHistory({ initialEntries: ["/f/page"] }),
    });
    return render(<RouterProvider router={router} />);
  }

  it("opens the page Telegram's «Открыть» names at once, in the sandboxed frame", async () => {
    mount(async () => ({ file: file("page", { path: "page.html", name: "page.html", mime: "text/html; charset=utf-8" }) }));
    expect(await screen.findByText("page.html")).toBeTruthy();
    const frame = document.querySelector("iframe")!;
    expect(frame.getAttribute("sandbox")).toBe("");
    expect(frame.getAttribute("src")).toBe("/api/files/page/view");
  });
});
