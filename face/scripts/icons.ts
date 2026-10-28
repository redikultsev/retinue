// Draws the app icon — three dots in procession (a lead and its retinue) on an indigo square — as SVG and as PNGs
// for the manifest and iOS. No image library: shapes are sampled 4×4 per pixel and the PNG is encoded with zlib.
// Run: node scripts/icons.ts (writes public/icons/).
import { writeFileSync } from "node:fs";
import { deflateSync } from "node:zlib";

const BG: [number, number, number] = [79, 70, 229];
const FG: [number, number, number] = [255, 255, 255];
// In a 0..1 square: [cx, cy, r]
const DOTS: [number, number, number][] = [
  [0.68, 0.5, 0.13],
  [0.38, 0.33, 0.095],
  [0.38, 0.67, 0.095],
];

function inside(x: number, y: number, radius: number, safe: number): "bg" | "fg" | "none" {
  // radius: the corner radius of the square (0 for a full-bleed maskable icon); safe: shrink the dots toward the
  // centre so they stay in a maskable icon's safe zone.
  const r = radius;
  const cx = Math.min(Math.max(x, r), 1 - r);
  const cy = Math.min(Math.max(y, r), 1 - r);
  if ((x - cx) ** 2 + (y - cy) ** 2 > r * r) return "none";
  for (const [dx, dy, dr] of DOTS) {
    const px = 0.5 + (dx - 0.5) * safe;
    const py = 0.5 + (dy - 0.5) * safe;
    if ((x - px) ** 2 + (y - py) ** 2 <= (dr * safe) ** 2) return "fg";
  }
  return "bg";
}

function crc32(buf: Buffer): number {
  let c = ~0;
  for (const b of buf) {
    c ^= b;
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return ~c >>> 0;
}

function chunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}

function png(size: number, radius: number, safe: number, opaque: boolean): Buffer {
  const S = 4;
  const raw = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y++) {
    raw[y * (size * 4 + 1)] = 0;
    for (let x = 0; x < size; x++) {
      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;
      for (let sy = 0; sy < S; sy++)
        for (let sx = 0; sx < S; sx++) {
          const hit = inside((x + (sx + 0.5) / S) / size, (y + (sy + 0.5) / S) / size, radius, safe);
          if (hit === "none") continue;
          const c = hit === "fg" ? FG : BG;
          r += c[0];
          g += c[1];
          b += c[2];
          a += 1;
        }
      const o = y * (size * 4 + 1) + 1 + x * 4;
      const n = S * S;
      if (opaque) {
        // iOS shows transparency as black: blend over the background colour instead
        raw[o] = Math.round((r + (n - a) * BG[0]) / n);
        raw[o + 1] = Math.round((g + (n - a) * BG[1]) / n);
        raw[o + 2] = Math.round((b + (n - a) * BG[2]) / n);
        raw[o + 3] = 255;
      } else {
        raw[o] = a ? Math.round(r / a) : 0;
        raw[o + 1] = a ? Math.round(g / a) : 0;
        raw[o + 2] = a ? Math.round(b / a) : 0;
        raw[o + 3] = Math.round((a / n) * 255);
      }
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8;
  ihdr[9] = 6;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const out = new URL("../public/icons/", import.meta.url);
const hex = (c: number[]) => `#${c.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><title>Retinue</title><rect width="100" height="100" rx="22" fill="${hex(BG)}"/>${DOTS.map(
  ([x, y, r]) => `<circle cx="${x * 100}" cy="${y * 100}" r="${r * 100}" fill="${hex(FG)}"/>`,
).join("")}</svg>\n`;
writeFileSync(new URL("icon.svg", out), svg);
writeFileSync(new URL("icon-192.png", out), png(192, 0.22, 1, false));
writeFileSync(new URL("icon-512.png", out), png(512, 0.22, 1, false));
writeFileSync(new URL("maskable-512.png", out), png(512, 0, 0.7, true));
writeFileSync(new URL("apple-touch-icon.png", out), png(180, 0, 0.85, true));
console.log("icons written to", out.pathname);
