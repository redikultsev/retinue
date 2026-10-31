// Files the owner attaches to a message, as the face API takes them (retinue/inbox.py `given`): each whole, in base64,
// with its SHA-256, and within the platform's own limits — checked here first so a phone does not upload 40 MB to be
// refused.

export const MAX_FILES = 10;
export const MAX_FILE = 20 * 1024 * 1024;
export const MAX_TOTAL = 30 * 1024 * 1024;

export interface Encoded {
  name: string;
  media_type: string;
  data: string;
  sha256: string;
}

const mb = (n: number) => `${Math.round(n / 2 ** 20)} MB`;

/** Why these files cannot go together, or null when they can. */
export function overLimits(files: File[]): string | null {
  if (files.length > MAX_FILES) return `No more than ${MAX_FILES} files in one message.`;
  const big = files.find((f) => f.size > MAX_FILE);
  if (big) return `“${big.name}”: ${mb(big.size)} — over ${mb(MAX_FILE)}.`;
  if (files.some((f) => f.size === 0)) return "An empty file can't be sent.";
  const total = files.reduce((n, f) => n + f.size, 0);
  if (total > MAX_TOTAL) return `The files together are over ${mb(MAX_TOTAL)}.`;
  return null;
}

function base64(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) out += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(out);
}

export async function encodeFile(file: File): Promise<Encoded> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return {
    name: file.name,
    media_type: file.type,
    data: base64(bytes),
    sha256: [...digest].map((b) => b.toString(16).padStart(2, "0")).join(""),
  };
}

/** «12 KB», «1.4 MB» */
export function sizeWord(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 2 ** 20) return `${Math.round(n / 1024)} KB`;
  return `${(n / 2 ** 20).toFixed(1)} MB`;
}
