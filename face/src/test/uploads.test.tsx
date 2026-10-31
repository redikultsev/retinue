import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { live } from "@/lib/live";
import { merged, type Upload, type Uploads, upload, uploadsApi, uploadUrl } from "@/lib/uploads";
import { UploadsPage, UploadsView } from "@/pages/uploads";

const NOW = new Date(2026, 9, 28, 12, 0).getTime() / 1000;

const file = (n: number, over: Partial<Upload> = {}): Upload => ({
  sha256: String(n).repeat(64).slice(0, 64),
  name: `file-${n}.pdf`,
  size: 1536,
  mime: "application/pdf",
  created: NOW - n * 3600,
  ...over,
});

const LIST: Uploads = { schema: 1, max: 40 * 1024 * 1024, files: [file(1, { name: "acme-export.csv", mime: "text/csv" }), file(2)] };

const picked = (name: string, bytes = 10, type = "application/pdf") => new File([new Uint8Array(bytes)], name, { type });

function pick(files: File[]) {
  fireEvent.change(screen.getByLabelText("Files to upload"), { target: { files } });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("the list", () => {
  it("shows each file's name as its download, its date, size and short SHA-256, the newest first", () => {
    render(<UploadsView uploads={LIST} sending={[]} now={NOW} onPick={vi.fn()} onRetry={vi.fn()} onDismiss={vi.fn()} />);
    const rows = screen.getAllByTestId("upload");
    expect(rows.map((r) => within(r).getByRole("link").textContent)).toEqual(["acme-export.csv", "file-2.pdf"]);
    const link = within(rows[0]!).getByRole("link");
    expect(link.getAttribute("href")).toBe(uploadUrl(LIST.files[0]!));
    expect(link.getAttribute("download")).toBe("acme-export.csv");
    expect(rows[0]!.textContent).toContain("11:00 · 2 KB · 111111111111");
    expect(screen.getByText(/Any file up to 40 MB/)).toBeTruthy();
    const input = screen.getByLabelText("Files to upload") as HTMLInputElement;
    expect(input.type).toBe("file");
    expect(input.multiple).toBe(true);
    expect(input.accept).toBe(""); // any type: a phone offers its files, photos and camera
  });

  it("says when there is nothing, while it loads, and when it failed", () => {
    const props = { sending: [], onPick: vi.fn(), onRetry: vi.fn(), onDismiss: vi.fn() };
    const { unmount } = render(<UploadsView uploads={{ ...LIST, files: [] }} {...props} />);
    expect(screen.getByText("Nothing uploaded yet.")).toBeTruthy();
    unmount();
    render(<UploadsView uploads={null} {...props} />);
    expect(screen.getByText("Loading…")).toBeTruthy();
    document.body.innerHTML = "";
    const onReload = vi.fn();
    render(<UploadsView uploads={null} error="No connection to the server." onReload={onReload} {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onReload).toHaveBeenCalled();
  });

  it("keeps each file once, the newest first", () => {
    const again = file(2, { created: NOW });
    expect(merged(LIST.files, [again, file(3)]).map((f) => f.sha256[0])).toEqual(["2", "1", "3"]);
  });
});

describe("uploading", () => {
  it("sends the picked files one after another, with progress, and lists each as it lands", async () => {
    vi.spyOn(uploadsApi, "list").mockResolvedValue(LIST);
    const finish: ((value: { file: Upload; new: boolean }) => void)[] = [];
    const progress: ((sent: number, total: number) => void)[] = [];
    const send = vi.spyOn(uploadsApi, "upload").mockImplementation((_f, onProgress) => {
      progress.push(onProgress!);
      return new Promise((resolve) => finish.push(resolve));
    });
    render(<UploadsPage />);
    await screen.findByText("acme-export.csv");
    pick([picked("statement.pdf", 2048), picked("receipt.jpg", 10, "image/jpeg")]);
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    expect(send.mock.calls[0]![0].name).toBe("statement.pdf");
    act(() => progress[0]!(1024, 2048));
    expect(screen.getByRole("progressbar", { name: "Uploading statement.pdf" }).getAttribute("aria-valuenow")).toBe("50");
    expect(screen.getByText("Waiting · 10 B")).toBeTruthy();
    await act(async () => finish[0]!({ file: file(9, { name: "statement.pdf", created: NOW + 10 }), new: true }));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(screen.getAllByTestId("upload")[0]!.textContent).toContain("statement.pdf");
    await act(async () => finish[1]!({ file: LIST.files[0]!, new: false }));
    expect(await screen.findByText("Already uploaded — kept once.")).toBeTruthy();
    expect(screen.getAllByTestId("upload")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss receipt.jpg" }));
    expect(screen.queryByText("receipt.jpg")).toBeNull();
  });

  it("refuses an empty or too large file before sending, and a failed one stays with its reason and «Retry»", async () => {
    vi.spyOn(uploadsApi, "list").mockResolvedValue({ ...LIST, max: 100 });
    const send = vi
      .spyOn(uploadsApi, "upload")
      .mockRejectedValueOnce(new Error("The store is busy — try again in a moment."))
      .mockResolvedValueOnce({ file: file(5, { name: "ok.pdf", created: NOW + 1 }), new: true });
    render(<UploadsPage />);
    await screen.findByText("acme-export.csv");
    pick([picked("big.zip", 101), picked("empty.txt", 0), picked("ok.pdf", 50)]);
    expect(await screen.findByText("Too large: 101 B, 0 MB at most.")).toBeTruthy();
    expect(screen.getByText("The file is empty.")).toBeTruthy();
    expect(await screen.findByText("The store is busy — try again in a moment.")).toBeTruthy();
    expect(send).toHaveBeenCalledTimes(1);
    const big = screen.getAllByTestId("sending").find((li) => li.textContent?.includes("big.zip"))!;
    expect(within(big).queryByRole("button", { name: "Retry" })).toBeNull(); // the same file would be refused again
    const failed = screen.getAllByTestId("sending").find((li) => li.textContent?.includes("ok.pdf"))!;
    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Uploaded.")).toBeTruthy();
  });

  it("shows a file another device uploaded as the stream tells of it", async () => {
    vi.spyOn(uploadsApi, "list").mockResolvedValue(LIST);
    let told: ((f: Upload) => void) | undefined;
    vi.spyOn(live, "onUpload").mockImplementation((listener) => {
      told = listener;
      return () => undefined;
    });
    render(<UploadsPage />);
    await screen.findByText("acme-export.csv");
    act(() => told!(file(7, { name: "from-the-laptop.xlsx", created: NOW + 5 })));
    expect(screen.getAllByTestId("upload")[0]!.textContent).toContain("from-the-laptop.xlsx");
  });
});

describe("the request", () => {
  it("is the file's bytes with its type and its name percent-encoded, behind the app's header", async () => {
    const sent: { headers: Record<string, string>; body?: unknown; url?: string } = { headers: {} };
    class FakeXhr {
      upload = { onprogress: null as ((e: ProgressEvent) => void) | null };
      status = 0;
      responseText = "";
      withCredentials = false;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;
      open(_method: string, url: string) {
        sent.url = url;
      }
      setRequestHeader(name: string, value: string) {
        sent.headers[name] = value;
      }
      send(body: unknown) {
        sent.body = body;
        this.upload.onprogress?.({ loaded: 5, total: 10, lengthComputable: true } as ProgressEvent);
        this.status = 200;
        this.responseText = JSON.stringify({ file: file(1), new: true });
        this.onload?.();
      }
      abort() {}
    }
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    const blob = picked("Отчёт 1.pdf", 10);
    const onProgress = vi.fn();
    expect(await upload(blob, onProgress)).toEqual({ file: file(1), new: true });
    expect(sent.url).toBe("/api/c/_files");
    expect(sent.body).toBe(blob);
    expect(sent.headers).toMatchObject({ "X-Retinue": "1", "Content-Type": "application/pdf", "X-Name": encodeURIComponent("Отчёт 1.pdf") });
    expect(onProgress).toHaveBeenCalledWith(5, 10);
  });
});
