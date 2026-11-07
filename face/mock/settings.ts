// «Settings» in the mock (retinue/settings.py and the host's deploy/settings/retinue-settings.py): the view the
// platform answers, each change taken by «the host» a moment later — set, its time, the services it waits on — and
// a restart that clears them after a few seconds. A value is checked for its shape and then forgotten, as the real
// one: nothing here keeps or answers it. `MOCK_SETTINGS_OFF=1`: the host's service not installed. Invented values only.
import { randomBytes } from "node:crypto";
import type { SettingsView } from "../src/lib/contract.ts";

const now = () => Date.now() / 1000;
const DAY = 86400;
const TELEGRAM = /^[0-9]{5,16}:[A-Za-z0-9_-]{30,64}$/;
const VALUE = /^[A-Za-z0-9._~+/=:@%,-]{8,4096}$/;
const SERVICES = ["router", "assistant", "platform", "tg-business"];

type Key = SettingsView["keys"][number] & { services: string[]; shape: RegExp };

const keys: Key[] = [
  {
    name: "CLAUDE_CODE_OAUTH_TOKEN", group: "keys", title: "Claude subscription",
    what: "Runs the assistant and every session on your Claude plan",
    where: "On a computer with Claude Code, run claude setup-token and paste what it prints. Not an API key: one would bill the API instead of your plan.",
    hint: "sk-ant-oat01-…", clear: false, on: true, set: true, changed: now() - 12 * DAY,
    services: ["assistant", "platform"], shape: /^[A-Za-z0-9._~+/=:@%,-]{20,1024}$/,
  },
  {
    name: "ELEVENLABS_API_KEY", group: "keys", title: "ElevenLabs",
    what: "Turns voice notes and dictation into text. Without it, voice is refused",
    where: "elevenlabs.io → Developers → API keys: a key with speech to text only and a credit limit.",
    hint: "sk_…", clear: true, on: true, set: false, changed: null,
    services: ["router", "platform"], shape: /^[A-Za-z0-9._~+/=:@%,-]{16,256}$/,
  },
  {
    name: "TELEGRAM_BOT_TOKEN", group: "channels", title: "Telegram bot",
    what: "Your chat with Retinue and its cards in Telegram, and the app's sign-in from Telegram",
    where: "@BotFather → /mybots → your bot → API Token. Revoking it there gives a new one.",
    hint: "123456789:AA…", clear: false, on: true, set: true, changed: null,
    services: ["router", "platform"], shape: TELEGRAM,
  },
  {
    name: "TELEGRAM_BUSINESS_TOKEN", group: "channels", title: "Telegram Business",
    what: "Reads the chats you chose and sends the replies you approve",
    where: "@BotFather: a second bot of its own with Business Mode on, never the main bot's token.",
    hint: "123456789:AA…", clear: true, on: false, set: false, changed: null,
    services: [], shape: TELEGRAM,
  },
];
const git = [
  { key: "acme", repos: ["acme/site", "acme/api"], set: true, changed: now() - 3 * DAY },
  { key: "example_gitlab", repos: ["example-group/notes"], set: false, changed: null },
];
const projectKeys = [{ name: "BOOKING", project: "travel", set: true, changed: null as number | null }];
const lane = {
  name: "private", engine: "claude-code", engine_label: "Claude Code",
  model: "claude-opus-5-5[1m]", effort: "xhigh",
  running: { model: "claude-opus-5-5[1m]", effort: "xhigh" },
};
const pending = new Set<string>();
const restarting: string[] = [];
let restarted: number | null = null;
const done: SettingsView["done"] = {};

export function settingsView(): SettingsView {
  const ready = process.env.MOCK_SETTINGS_OFF !== "1";
  return {
    schema: 1, ready, at: ready ? now() : null,
    pending: SERVICES.filter((s) => pending.has(s)), restarting: [...restarting], restarted,
    lanes: [lane],
    models: [
      { id: "claude-opus-5-5[1m]", label: "Opus 5.5 · 1M context" }, { id: "claude-opus-5-5", label: "Opus 5.5" },
      { id: "claude-sonnet-5-5[1m]", label: "Sonnet 5.5 · 1M context" }, { id: "claude-sonnet-5-5", label: "Sonnet 5.5" },
    ],
    efforts: [
      { id: "low", label: "Low" }, { id: "medium", label: "Medium" }, { id: "high", label: "High" },
      { id: "xhigh", label: "Extra high" }, { id: "max", label: "Max" },
    ],
    keys: keys.map(({ services: _s, shape: _p, ...k }) => k),
    git, project_keys: projectKeys, done,
  };
}

/** The host a moment later: the change written, or refused with its words. */
function later(id: string, apply: () => string | null, ms = 700) {
  setTimeout(() => {
    const why = apply();
    done[id] = why ? { ok: false, at: now(), why } : { ok: true, at: now() };
  }, ms);
}

const BAD = "This does not look like the right key: check you copied all of it, with nothing around it.";

/** A POST to `/api/settings/<op>`: 202 with the request's id, or 400 with why. */
export function settingsPost(op: string, b: Record<string, unknown>): [number, unknown] {
  if (process.env.MOCK_SETTINGS_OFF === "1")
    return [503, { error: "The server's settings service is not installed yet: run deploy/setup.sh once more on the server." }];
  const id = randomBytes(16).toString("hex");
  const value = typeof b.value === "string" ? b.value.trim() : "";
  if (op === "key") {
    const key = keys.find((k) => k.name === b.name);
    if (!key) return [400, { error: "Settings does not set this key." }];
    if (b.clear === true) {
      if (!key.clear) return [400, { error: "This key cannot be removed: Retinue needs it." }];
    } else if (!key.shape.test(value)) return [400, { error: BAD }];
    const clear = b.clear === true;
    later(id, () => {
      key.set = !clear;
      key.changed = now();
      for (const s of key.services) pending.add(s);
      return null;
    });
  } else if (op === "git") {
    const g = git.find((x) => x.key === b.key);
    if (!g) return [400, { error: "An account's key is a-z, 0-9 and _, as its token: in platform.yaml." }];
    if (!/^[^\s=]{1,512}$/.test(value)) return [400, { error: BAD }];
    // The host's set-git-token.sh refuses what is not a token's shape; «nope» shows that refusal coming back.
    later(id, () => {
      if (value.startsWith("nope")) return "The token was not written: this does not look like a token: empty, has spaces or too long — nothing written";
      g.set = true;
      g.changed = now();
      pending.add("platform");
      return null;
    });
  } else if (op === "project_key") {
    const p = projectKeys.find((x) => x.name === b.name);
    if (!p) return [400, { error: "A project key's name is A-Z, 0-9 and _." }];
    if (!VALUE.test(value)) return [400, { error: BAD }];
    later(id, () => {
      p.set = true;
      p.changed = now();
      pending.add("platform");
      return null;
    });
  } else if (op === "lane") {
    if (b.lane !== lane.name) return [400, { error: "There is no such lane." }];
    const models = settingsView().models.map((m) => m.id);
    const efforts = settingsView().efforts.map((e) => e.id);
    if ("model" in b && !models.includes(String(b.model))) return [400, { error: `Model is one of ${models.join(", ")}.` }];
    if ("effort" in b && !efforts.includes(String(b.effort))) return [400, { error: `Effort is one of ${efforts.join(", ")}.` }];
    later(id, () => {
      if (typeof b.model === "string") lane.model = b.model;
      if (typeof b.effort === "string") lane.effort = b.effort;
      pending.add("platform");
      return null;
    });
  } else if (op === "restart") {
    // «the host» says it restarts, restarts, then answers; the request waits until it is over
    setTimeout(() => restarting.push(...SERVICES.filter((s) => pending.has(s))), 200);
    setTimeout(() => {
      restarting.length = 0;
      pending.clear();
      lane.running = { model: lane.model, effort: lane.effort };
      restarted = now();
      done[id] = { ok: true, at: now() };
    }, 3500);
  } else return [404, { error: "Not found." }];
  return [202, { id }];
}
