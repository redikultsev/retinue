import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Composer, fitField, GROW_TO, messageId } from "@/components/composer";
import { MAX_FILE, overLimits } from "@/lib/attach";

function touch(coarse: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("pointer: coarse") ? coarse : false,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const box = () => screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;

const file = (name: string, text: string, type = "text/plain") => new File([text], name, { type });

describe("Composer attachments", () => {
  it("sends files alone, whole, in base64 with their SHA-256, and clears them", async () => {
    const send = vi.fn().mockResolvedValue({ say: "Accepted." });
    render(<Composer session="s1" running={false} send={send} />);
    const send_ = screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
    expect(send_.disabled).toBe(true);
    fireEvent.change(screen.getByTestId("attach-input"), { target: { files: [file("a.txt", "abc")] } });
    expect(screen.getByText("a.txt")).toBeTruthy();
    expect(send_.disabled).toBe(false);
    fireEvent.click(send_);
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    const [session, text, id, attachments] = send.mock.calls[0]!;
    expect([session, text, String(id).startsWith("face:")]).toEqual(["s1", "", true]);
    expect(attachments).toEqual([
      {
        name: "a.txt",
        media_type: "text/plain",
        data: "YWJj",
        sha256: "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
      },
    ]);
    await waitFor(() => expect(screen.queryByText("a.txt")).toBeNull());
  });

  it("removes a file before sending and refuses what the platform would refuse", () => {
    render(<Composer session="s1" running={false} send={vi.fn()} />);
    fireEvent.change(screen.getByTestId("attach-input"), { target: { files: [file("a.txt", "a"), file("b.txt", "b")] } });
    fireEvent.click(screen.getByRole("button", { name: "Remove a.txt" }));
    expect(screen.queryByText("a.txt")).toBeNull();
    expect(screen.getByText("b.txt")).toBeTruthy();
    const big = { name: "big.iso", size: MAX_FILE + 1 } as File;
    expect(overLimits([big])).toBe("“big.iso”: 20 MB — over 20 MB.");
    expect(overLimits(Array.from({ length: 11 }, (_, k) => ({ name: `${k}`, size: 1 }) as File))).toBe(
      "No more than 10 files in one message.",
    );
    expect(overLimits([{ name: "a", size: 16 * 2 ** 20 } as File, { name: "b", size: 16 * 2 ** 20 } as File])).toBe(
      "The files together are over 30 MB.",
    );
  });
});

describe("Composer, the one box", () => {
  it("starts three lines tall (two in the list's peek) and grows with the text to 40 % of the visible height, then scrolls", () => {
    render(<Composer session="s1" running={false} send={vi.fn()} />);
    expect(box().rows).toBe(3);
    expect(box().className).toContain("min-h-[5.5rem]");
    const el = document.createElement("textarea");
    let tall = 120;
    Object.defineProperty(el, "scrollHeight", { get: () => tall });
    fitField(el, Math.round(1000 * GROW_TO));
    expect([el.style.height, el.style.overflowY]).toEqual(["120px", "hidden"]);
    tall = 900;
    fitField(el, 400);
    expect([el.style.height, el.style.overflowY]).toEqual(["400px", "auto"]);
    tall = 0; // not laid out: its CSS height stands
    fitField(el, 400);
    expect(el.style.height).toBe("");
  });

  it("the list's peek is the same box, smaller", () => {
    render(<Composer session="s1" running={false} send={vi.fn()} compact />);
    expect(box().rows).toBe(2);
  });

  it("«+» opens one menu — Photo, File, and Camera on a touch screen; a pick goes as a removable chip", () => {
    touch(true);
    render(<Composer session="s1" running={false} send={vi.fn()} />);
    expect(screen.queryByRole("menu")).toBeNull();
    const plus = screen.getByRole("button", { name: "Attach" });
    fireEvent.click(plus);
    const menu = screen.getByRole("menu", { name: "Attach" });
    expect(plus.getAttribute("aria-expanded")).toBe("true");
    expect(Array.from(menu.querySelectorAll("[role=menuitem]")).map((b) => b.textContent)).toEqual(["Photo", "Camera", "File"]);
    expect(screen.queryByText("from the Base…")).toBeNull();
    const camera = screen.getByTestId("attach-camera") as HTMLInputElement;
    expect([camera.accept, camera.getAttribute("capture")]).toEqual(["image/*", "environment"]);
    expect((screen.getByTestId("attach-photo") as HTMLInputElement).accept).toBe("image/*");
    const click = vi.spyOn(screen.getByTestId("attach-photo") as HTMLInputElement, "click");
    fireEvent.click(screen.getByRole("menuitem", { name: "Photo" }));
    expect(click).toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.change(screen.getByTestId("attach-photo"), { target: { files: [file("IMG_1.jpg", "jpeg", "image/jpeg")] } });
    expect(screen.getByRole("list", { name: "Attachments" }).textContent).toContain("IMG_1.jpg");
    fireEvent.click(screen.getByRole("button", { name: "Remove IMG_1.jpg" }));
    expect(screen.queryByRole("list", { name: "Attachments" })).toBeNull();
  });

  it("no camera on a desktop; Esc and a tap beside close the menu", () => {
    touch(false);
    render(<Composer session="s1" running={false} send={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Attach" }));
    expect(screen.queryByRole("menuitem", { name: "Camera" })).toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Attach" }));
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("on a desktop Enter sends and Shift+Enter is a new line; the arrow is grey while there is nothing to send", async () => {
    touch(false);
    const send = vi.fn().mockResolvedValue({ say: "Accepted." });
    render(<Composer session="s1" running={false} send={send} />);
    const arrow = screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
    expect(arrow.disabled).toBe(true);
    fireEvent.change(box(), { target: { value: "   " } });
    expect(arrow.disabled).toBe(true);
    fireEvent.change(box(), { target: { value: "hello" } });
    expect(arrow.disabled).toBe(false);
    fireEvent.keyDown(box(), { key: "Enter", shiftKey: true });
    expect(send).not.toHaveBeenCalled();
    fireEvent.keyDown(box(), { key: "Enter" });
    await waitFor(() => expect(send).toHaveBeenCalledWith("s1", "hello", expect.stringMatching(/^face:/), []));
    await waitFor(() => expect(box().value).toBe(""));
  });

  it("on a phone Enter is a new line: only the arrow sends", async () => {
    touch(true);
    const send = vi.fn().mockResolvedValue({ say: "Accepted." });
    render(<Composer session="s1" running={false} send={send} />);
    fireEvent.change(box(), { target: { value: "two\nlines" } });
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(send).not.toHaveBeenCalled();
    expect(box().getAttribute("enterkeyhint")).toBe("enter");
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(send).toHaveBeenCalledWith("s1", "two\nlines", expect.any(String), []));
  });

  it("keeps the voice where it was — 🎙 and «EN» on the right — and «Stop» while a turn runs", () => {
    const stop = vi.fn().mockResolvedValue({ ok: true });
    render(<Composer session="s1" running send={vi.fn()} stop={stop} />);
    const row = screen.getByRole("button", { name: "Send" }).parentElement!;
    const names = Array.from(row.querySelectorAll("button")).map((b) => b.getAttribute("aria-label"));
    expect(names).toEqual(["Attach", expect.stringMatching(/^(Dictate|The microphone is not available here.*)$/), "EN", "Stop", "Send"]);
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(stop).toHaveBeenCalledWith("s1");
  });

  it("a tap on the box beside the text puts the cursor in it", () => {
    render(<Composer session="s1" running={false} send={vi.fn()} />);
    fireEvent.click(screen.getByTestId("composer"));
    expect(document.activeElement).toBe(box());
  });

  it("a retry of the same message keeps its id; anything else is a new message", () => {
    const a = file("a.txt", "a");
    const tried = { id: "face:1", body: "hello", files: [a] };
    expect(messageId(tried, "hello", [a])).toBe("face:1");
    expect(messageId(tried, "hello\n\nHi", [a])).not.toBe("face:1");
    expect(messageId(tried, "hello", [])).not.toBe("face:1");
    expect(messageId(null, "hello", [a])).toMatch(/^face:/);
  });

  it("the menu takes the focus, moves with the arrows, and gives it back to «+» on Esc", () => {
    touch(true);
    render(<Composer session="s1" running={false} send={vi.fn()} />);
    const plus = screen.getByRole("button", { name: "Attach" });
    fireEvent.click(plus);
    const items = screen.getAllByRole("menuitem");
    expect(document.activeElement).toBe(items[0]);
    fireEvent.keyDown(items[0]!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
    fireEvent.keyDown(items[1]!, { key: "End" });
    expect(document.activeElement).toBe(items[2]);
    fireEvent.keyDown(items[2]!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[0]);
    fireEvent.keyDown(items[0]!, { key: "ArrowUp" });
    expect(document.activeElement).toBe(items[2]);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(plus);
  });

  it("every control is a 44 px target, whatever its face", () => {
    render(<Composer session="s1" running send={vi.fn()} />);
    fireEvent.change(screen.getByTestId("attach-input"), { target: { files: [file("a.txt", "a")] } });
    for (const name of ["Attach", "Stop", "Send", "Remove a.txt"])
      expect(screen.getByRole("button", { name }).className, name).toMatch(/(^|\s)size-11(\s|$)/);
  });
});
