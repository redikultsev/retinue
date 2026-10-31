import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PlainDiff, RemarkTray } from "@/components/remarks";
import { ApiError } from "@/lib/api";
import { plainLines, quoteOf, Remarks, remarksMessage } from "@/lib/remarks";

class Memory implements Storage {
  data = new Map<string, string>();
  get length() {
    return this.data.size;
  }
  clear() {
    this.data.clear();
  }
  getItem(k: string) {
    return this.data.get(k) ?? null;
  }
  key(n: number) {
    return [...this.data.keys()][n] ?? null;
  }
  removeItem(k: string) {
    this.data.delete(k);
  }
  setItem(k: string, v: string) {
    this.data.set(k, v);
  }
}

const at = (file: string, line: number, text: string, side: "additions" | "deletions" = "additions", quote = "x = 1") => ({ file, line, side, quote, text });

const PUSH = `commit 1111111111111111111111111111111111111111
Parents: 2222222222222222222222222222222222222222
Author: Anna <anna@example.com>

    Fix the gate

diff --git a/acme/gate.py b/acme/gate.py
--- a/acme/gate.py
+++ b/acme/gate.py
@@ -10,3 +10,3 @@ def bind():
     keep = 1
-    old = 2
+    new = 2
diff --git a/acme/new.py b/acme/new.py
new file mode 100644
--- /dev/null
+++ b/acme/new.py
@@ -0,0 +1,1 @@
+print("hi")

commit 3333333333333333333333333333333333333333
    -- not a removed line: the next commit's message
`;

describe("comments on a diff", () => {
  it("are one message: each line, its quote as data, its comment", () => {
    const msg = remarksMessage([
      { id: "a", ...at("acme/gate.py", 12, "  use new here ") },
      { id: "b", ...at("acme/gate.py", 11, "why gone?", "deletions", "old = 2") },
    ]);
    expect(msg).toBe(
      "Comments on the diff (2):\n\nComment on acme/gate.py:12: use new here\n> x = 1\n\n" +
        "Comment on acme/gate.py:11: (the removed line) why gone?\n> old = 2",
    );
    expect(remarksMessage([{ id: "a", ...at("a.md", 1, "t", "additions", "") }])).toBe("A comment on the diff:\n\nComment on a.md:1: t");
    expect(quoteOf("a\nb   ")).toBe("a");
    expect(quoteOf("y".repeat(400))).toHaveLength(300);
  });

  it("outlive a reload in their session's tray, one per line, and a send takes only those it sent", () => {
    const store = new Memory();
    const one = new Remarks(store);
    one.add("s1", at("f.py", 3, "first"));
    one.add("s1", at("f.py", 3, "second"));
    one.add("s1", at("f.py", 4, "other"));
    one.add("s2", at("g.py", 1, "elsewhere"));
    const again = new Remarks(store);
    expect(again.list("s1").map((r) => r.text)).toEqual(["second", "other"]);
    expect(again.list("s2")).toHaveLength(1);
    const sent = again.list("s1").slice(0, 1);
    again.clear("s1", sent);
    expect(again.list("s1").map((r) => r.text)).toEqual(["other"]);
    again.clear("s1", again.list("s1"));
    expect(store.getItem("retinue.remarks.s1")).toBeNull();
  });

  it("live in memory when storage throws or holds junk", () => {
    const broken = new Memory();
    broken.setItem = () => {
      throw new Error("quota");
    };
    const r = new Remarks(broken);
    r.add("s", at("f", 1, "kept"));
    expect(r.list("s")).toHaveLength(1);
    const junk = new Memory();
    junk.data.set("retinue.remarks.s", "{not json");
    expect(new Remarks(junk).list("s")).toEqual([]);
    junk.data.set("retinue.remarks.t", JSON.stringify([{ id: 1 }, { id: "a", ...at("f", 2, "ok") }]));
    expect(new Remarks(junk).list("t").map((x) => x.text)).toEqual(["ok"]);
    expect(new Remarks(null).list("s")).toEqual([]);
  });

  it("point a push's lines at their file and number; headers and messages point nowhere", () => {
    const lines = plainLines(PUSH);
    const pointed = lines.filter((l) => l.file).map((l) => [l.text, l.file, l.line, l.side]);
    expect(pointed).toEqual([
      ["     keep = 1", "acme/gate.py", 10, "additions"],
      ["-    old = 2", "acme/gate.py", 11, "deletions"],
      ["+    new = 2", "acme/gate.py", 11, "additions"],
      ['+print("hi")', "acme/new.py", 1, "additions"],
    ]);
    expect(lines.find((l) => l.text.includes("not a removed line"))?.file).toBeUndefined();
  });
});

describe("the tray", () => {
  it("sends every comment as one message under one key, and empties", async () => {
    const r = new Remarks(new Memory());
    r.add("s1", at("f.py", 3, "fix"));
    r.add("s1", at("f.py", 9, "and this"));
    const send = vi.fn().mockRejectedValueOnce(new ApiError(503, "The platform is away.")).mockResolvedValue({ say: "Accepted." });
    render(<RemarkTray session="s1" from={r} send={send} />);
    expect(screen.getByRole("button", { name: "2 comments" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    expect(r.list("s1")).toHaveLength(2); // not sent: the comments stay
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toHaveProperty("disabled", false));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    const [first, second] = send.mock.calls as [string, string, string][];
    const [s, body, id] = first!;
    const [, again, id2] = second!;
    expect(s).toBe("s1");
    expect(body).toBe(again);
    expect(body).toContain("Comment on f.py:3: fix\n> x = 1");
    expect(id2).toBe(id);
    await waitFor(() => expect(screen.queryByTestId("remark-tray")).toBeNull());
    expect(r.list("s1")).toEqual([]);
  });

  it("lists the comments with their lines when opened, each removable", () => {
    const r = new Remarks(new Memory());
    r.add("s1", at("f.py", 3, "fix", "additions", "<b>not html</b>"));
    render(<RemarkTray session="s1" from={r} send={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "1 comment" }));
    expect(screen.getByText("<b>not html</b>")).toBeTruthy();
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    });
    expect(screen.queryByTestId("remark-tray")).toBeNull();
  });
});

describe("a push's diff", () => {
  it("takes a tap on a line: the box, then the comment under it and in the tray", () => {
    const r = new Remarks(new Memory());
    render(
      <>
        <PlainDiff session="s1" text={PUSH} from={r} />
        <RemarkTray session="s1" from={r} send={vi.fn()} />
      </>,
    );
    expect(screen.queryByRole("button", { name: /Fix the gate/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "- old = 2" }));
    fireEvent.change(screen.getByPlaceholderText("What is wrong here?"), { target: { value: "keep old" } });
    fireEvent.click(screen.getByRole("button", { name: "Add comment" }));
    expect(screen.getByTestId("remark-note").textContent).toContain("keep old");
    expect(r.list("s1")).toMatchObject([{ file: "acme/gate.py", line: 11, side: "deletions", quote: "    old = 2", text: "keep old" }]);
    expect(screen.getByRole("button", { name: "1 comment" })).toBeTruthy();
  });
});
