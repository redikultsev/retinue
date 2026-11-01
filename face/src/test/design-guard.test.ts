// The look's few hard rules (docs/design.md), held by a test so a change that breaks one fails the build: no emoji
// as icons, no tracked capitals, no `transition-all`, no fractional pixels, no text under 11 px. A line that breaks
// one on purpose says why with `design: allow`.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..");
const SKIP = /(^|\/)(test|mock)\/|\.test\.|(^|\/)icons\.tsx$/;
const COMMENT = /^\s*(\/\/|\/\*|\*)/;

const RULES: [string, RegExp][] = [
  ["an emoji — a line icon of components/icons.tsx, or nothing", /[\u{1F300}-\u{1FAFF}\u{1F000}-\u{1F2FF}\u{2600}-\u{27BF}\u{2B50}]/u],
  ["tracked capitals — sentence case", /\buppercase\b.*\btracking-|\btracking-.*\buppercase\b/],
  ["transition-all — name what moves", /\btransition-all\b/],
  ["a fractional pixel — a step of the scale", /-\[\d+\.\d+px\]/],
  ["text under 11 px", /\btext-\[(?:[0-9]|10)(?:\.\d+)?px\]/],
];

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(tsx?|css)$/.test(name) && !SKIP.test(path.slice(SRC.length)) ? [path] : [];
  });
}

describe("the look's hard rules", () => {
  it("holds in every source file of the face", () => {
    const broken: string[] = [];
    for (const file of sources(SRC))
      readFileSync(file, "utf8")
        .split("\n")
        .forEach((line, i) => {
          if (COMMENT.test(line) || line.includes("design: allow")) return;
          for (const [why, rule] of RULES) if (rule.test(line)) broken.push(`${file.slice(SRC.length + 1)}:${i + 1} ${why}`);
        });
    expect(broken).toEqual([]);
  });
});
