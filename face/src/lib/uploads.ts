// «Uploads»: any file the owner sends up from a device — a bank's statement, an export, a screenshot, a photo of a
// receipt — kept by the platform as it came, by its SHA-256 (`/api/c/_files`, retinue/collections.py). Nothing here
// opens or reads a file; the list is the platform's, so the phone and the desktop agree, and a file uploaded on one
// shows on the other at once (the stream's `upload` event).

import { useCallback, useEffect, useState } from "react";
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

/** The list, read on mount and whenever the app comes back to the front; a file uploaded anywhere comes in by the
 * stream. `put` adds the files this device just sent. */
export function useUploads() {
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
    const off = live.onUpload((file) => put(file));
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      off();
    };
  }, [load, put]);
  return { uploads, error, reload: load, put };
}
