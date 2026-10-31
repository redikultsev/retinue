// «Uploads»: any file the owner sends up from a device — a bank's statement, an export, a screenshot, a photo of a
// receipt — kept by the platform as it came, by its SHA-256 (`/api/c/_files`, retinue/collections.py). Nothing here
// opens or reads a file; the list is the platform's, so the phone and the desktop agree, and a file uploaded on one
// shows on the other at once (the stream's `upload` event).

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { ApiError, loggedOut, request } from "./api";
import { live } from "./live";

export interface Upload {
  sha256: string;
  name: string;
  size: number;
  /** As the device said; never sniffed. */
  mime: string;
  /** Seconds, the platform's clock: the first time these bytes came. */
  created: number;
}

export interface Uploads {
  schema: number;
  files: Upload[];
  /** The largest file the platform takes, in bytes. */
  max: number;
}

/** What the platform takes when it has not said yet: 40 MB. */
export const UPLOAD_MAX = 40 * 1024 * 1024;

export const uploadUrl = (file: Pick<Upload, "sha256">) => `/api/c/_files/${file.sha256}`;

/** One file's bytes to the platform, its progress told as they go (`fetch` cannot tell an upload's progress): `{file,
 * new}`, `new` false when the same bytes were there already. Rejects with an ApiError, as `request` does. */
export function upload(
  file: Blob & { name?: string },
  onProgress?: (sent: number, total: number) => void,
  signal?: AbortSignal,
): Promise<{ file: Upload; new: boolean }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/c/_files");
    xhr.withCredentials = true;
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("X-Retinue", "1");
    xhr.setRequestHeader("X-Name", encodeURIComponent(file.name ?? "file"));
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (e) => onProgress?.(e.loaded, e.lengthComputable ? e.total : file.size);
    xhr.onload = () => {
      let data: { file?: Upload; new?: boolean; error?: string } = {};
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        // not JSON: the generic message below
      }
      if (xhr.status === 200 && data.file) return resolve({ file: data.file, new: data.new === true });
      if (xhr.status === 401) loggedOut();
      reject(new ApiError(xhr.status, data.error ?? (xhr.status === 413 ? "The file is too large." : `Error ${xhr.status}.`)));
    };
    xhr.onerror = () => reject(new ApiError(0, "No connection to the server."));
    xhr.onabort = () => reject(new ApiError(0, "Cancelled."));
    signal?.addEventListener("abort", () => xhr.abort());
    xhr.send(file);
  });
}

export const uploadsApi = {
  list: () => request<Uploads>("/api/c/_files"),
  upload,
};

/** Newest first, each file once: a new or re-sent row replaces its old one. */
export function merged(files: Upload[], added: Upload[]): Upload[] {
  const by = new Map(files.map((f) => [f.sha256, f]));
  for (const f of added) by.set(f.sha256, f);
  return [...by.values()].sort((a, b) => b.created - a.created || a.name.localeCompare(b.name));
}

/** A file on its way: `sending` with its bytes so far, then `done` (`again`: those bytes were there already) or
 * `failed` with the reason — `refused` when it was never sent (empty, too large), so no «Retry». */
export interface Sending {
  key: string;
  file: File;
  sent: number;
  state: "waiting" | "sending" | "done" | "failed";
  again?: boolean;
  error?: string;
  refused?: boolean;
}

/** The files this device is sending, one after another in the order picked. It lives beside the app, not in the
 * page: leaving «Uploads» for «Today» goes on sending, and the page shows where each one is when it comes back. While
 * anything is still to go, closing the tab asks first. */
export class UploadQueue {
  private items: Sending[] = [];
  private listeners = new Set<() => void>();
  private landedListeners = new Set<(file: Upload) => void>();
  private busy = false;
  private counter = 0;

  constructor(private send: typeof upload = (...args) => uploadsApi.upload(...args)) {}

  getItems = (): Sending[] => this.items;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Told of each file this queue landed, for the list to show it. */
  onLanded(listener: (file: Upload) => void): () => void {
    this.landedListeners.add(listener);
    return () => this.landedListeners.delete(listener);
  }

  /** Picked files in, waiting their turn — but one `refuse` has a reason for (empty, too large), refused at once. */
  add(files: File[], refuse: (file: File) => string | null = () => null): void {
    const added = files.map((file): Sending => {
      const key = `u${++this.counter}`;
      const error = refuse(file);
      return error ? { key, file, sent: 0, state: "failed", refused: true, error } : { key, file, sent: 0, state: "waiting" };
    });
    this.set([...this.items, ...added]);
    this.next();
  }

  retry(key: string): void {
    if (!this.items.some((s) => s.key === key && s.state === "failed" && !s.refused)) return;
    this.change(key, { state: "waiting", sent: 0, error: undefined });
    this.next();
  }

  dismiss(key: string): void {
    this.set(this.items.filter((s) => s.key !== key || s.state === "waiting" || s.state === "sending"));
  }

  /** Anything not yet landed or failed. */
  pending(): boolean {
    return this.items.some((s) => s.state === "waiting" || s.state === "sending");
  }

  private set(items: Sending[]) {
    const was = this.pending();
    this.items = items;
    const is = this.pending();
    if (was !== is && typeof window !== "undefined") {
      if (is) window.addEventListener("beforeunload", onLeave);
      else window.removeEventListener("beforeunload", onLeave);
    }
    for (const l of this.listeners) l();
  }

  private change(key: string, over: Partial<Sending>) {
    this.set(this.items.map((s) => (s.key === key ? { ...s, ...over } : s)));
  }

  private next(): void {
    if (this.busy) return;
    const item = this.items.find((s) => s.state === "waiting");
    if (!item) return;
    this.busy = true;
    this.change(item.key, { state: "sending", sent: 0 });
    this.send(item.file, (sent) => this.change(item.key, { sent }))
      .then(
        (answer) => {
          for (const l of this.landedListeners) l(answer.file);
          this.change(item.key, { state: "done", sent: item.file.size, again: !answer.new });
        },
        (e: unknown) => this.change(item.key, { state: "failed", error: e instanceof Error ? e.message : "Not uploaded." }),
      )
      .finally(() => {
        this.busy = false;
        this.next();
      });
  }
}

function onLeave(e: BeforeUnloadEvent) {
  e.preventDefault();
}

export const uploadQueue = new UploadQueue();

export function useUploadQueue(queue: UploadQueue = uploadQueue): Sending[] {
  return useSyncExternalStore(queue.subscribe, queue.getItems);
}

/** The list, read on mount, whenever the app comes back to the front and after the stream resynced; a file uploaded
 * anywhere comes in by the stream, one this device sent as the queue lands it. `put` adds files to it. */
export function useUploads(queue: UploadQueue = uploadQueue) {
  const [uploads, setUploads] = useState<Uploads | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setUploads(await uploadsApi.list());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load.");
    }
  }, []);
  const put = useCallback((...files: Upload[]) => {
    setUploads((current) => (current ? { ...current, files: merged(current.files, files) } : current));
  }, []);
  useEffect(() => {
    void load();
    const onVisible = () => {
      if (!document.hidden) void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    const offs = [live.onUpload((file) => put(file)), live.onResync(() => void load()), queue.onLanded((file) => put(file))];
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      for (const off of offs) off();
    };
  }, [load, put, queue]);
  return { uploads, error, reload: load, put };
}
