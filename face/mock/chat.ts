// «Talk to Retinue» in the mock (retinue/chat.py and the router's app channel): the assistant's conversation as rows,
// his message kept at once and answered a moment later — what she is writing streamed as `chat-draft`, her answer,
// then a card of hers with two buttons, which a press rewrites as the router does. Invented words only.
import { randomBytes } from "node:crypto";
import type { ChatDraft, ChatRow } from "../src/lib/contract.ts";

type Emit = (kind: string, key: string, data: unknown) => void;
type Draft = (draft: ChatDraft) => void;

const now = () => Date.now() / 1000;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const button = () => randomBytes(12).toString("base64url");

const LONG =
  "Here is the week as it stands:\n\n- **Monday** — the Acme call at 10:00, then the report draft\n- **Wednesday** — the dentist at 16:30; the flat in Ensk still waits for your yes\n- **Friday** — buy the tickets (the reminder is set)\n\nNothing else is due. The longest stretch of free time is Thursday afternoon, about four hours — a good slot for the review you put off.";

let rev = 0;
const rows = new Map<string, ChatRow>();

function seed(row: Omit<ChatRow, "rev">) {
  rows.set(row.id, { ...row, rev: ++rev });
}

const T = now();
seed({ id: "telegram:301", at: T - 7200, by: "owner", text: "What is left before the Ensk trip?", via: "telegram" });
seed({ id: "telegram:301:a", at: T - 7180, by: "her", text: "Two things: book the flat in the old town (the session waits for your yes) and recheck the transfer rules before 1 May. The tickets are not bought yet." });
seed({ id: "app:face:seed-1", at: T - 3600, by: "owner", text: "What does my week look like?" });
seed({ id: "app:face:seed-1:a", at: T - 3590, by: "her", text: LONG, files: [{ id: "0".repeat(32), name: "week-plan.pdf", size: 48_211, mime: "application/pdf" }] });
seed({ id: "system:302", at: T - 1800, by: "notice", text: "Reminder: call the bank about the card", buttons: [] });

export function mockChat(emit: Emit, draft: Draft) {
  function put(row: Omit<ChatRow, "rev">): ChatRow {
    const next = { ...rows.get(row.id), ...row, rev: ++rev } as ChatRow;
    rows.set(next.id, next);
    emit("chat", next.id, next);
    return next;
  }

  async function answer(text: string) {
    await sleep(500);
    const reply = `Noted: “${text.slice(0, 60)}”. I put it on your list for this week and will remind you on Friday morning.`;
    let shown = "";
    for (const word of reply.split(/(?<= )/)) {
      shown += word;
      draft({ text: shown, at: now() });
      await sleep(70);
    }
    put({ id: `app-row:${randomBytes(4).toString("hex")}`, at: now(), by: "her", text: reply });
    await sleep(200);
    put({ id: `system:${randomBytes(4).toString("hex")}`, at: now(), by: "notice", text: `Add «${text.slice(0, 60)}» to your tasks?`, buttons: [["Add", button()], ["Not now", button()]] });
  }

  return {
    snapshot: () => ({ schema: 1, rows: [...rows.values()].sort((a, b) => a.at - b.at), draft: null }),
    /** His message: his row at once, `pending` until «the router» takes it; once per input id. */
    say(inputId: string, text: string): { status: number; body: unknown } {
      if (!/^face:[0-9a-f-]{36}$/.test(inputId) || !text.trim()) return { status: 400, body: { error: "input_id (face:<uuid4>) and text" } };
      const id = `app:${inputId}`;
      const had = rows.get(id);
      if (had) return { status: 200, body: { ok: true, row: had } };
      const row = put({ id, at: now(), by: "owner", text, pending: true });
      setTimeout(() => {
        put({ id, at: row.at, by: "owner", text, pending: false });
        void answer(text);
      }, 300);
      return { status: 200, body: { ok: true, row } };
    },
    /** A press: the card rewritten as the router rewrites it — its choice under it, no buttons left. */
    press(buttonId: string, rowId: string): { status: number; body: unknown } {
      const row = rows.get(rowId);
      const pressed = row?.buttons?.find(([, id]) => id === buttonId);
      if (!row || !pressed) return { status: 410, body: { error: "This button is no longer on the card." } };
      setTimeout(() => put({ ...row, text: `${row.text}\n\n_Chosen: ${pressed[0]}_`, buttons: [], toast: pressed[0] === "Add" ? "Added to your tasks." : "Left as it is." }), 250);
      return { status: 200, body: { ok: true } };
    },
  };
}
