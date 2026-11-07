// «Settings» (docs/face.md «Settings»): each row says what it is for and whether it is set; a key typed is sent once
// and gone from the page at once, never rendered back; a change that waits for a restart says so, and the
// restart is the owner's press. Invented values only.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { requestState, type SettingsView } from "@/lib/settings";
import { SettingsBody } from "@/pages/settings";

const TYPED = "sk_invented_example_value_0123456789";

function view(over: Partial<SettingsView> = {}): SettingsView {
  return {
    schema: 1,
    ready: true,
    at: 1_800_000_000,
    pending: [],
    restarting: [],
    restarted: null,
    lanes: [
      {
        name: "private",
        engine: "claude-code",
        engine_label: "Claude Code",
        model: "claude-opus-5-5[1m]",
        effort: "xhigh",
        running: { model: "claude-opus-5-5[1m]", effort: "xhigh" },
      },
    ],
    models: [
      { id: "claude-opus-5-5[1m]", label: "Opus 5.5 · 1M context" },
      { id: "claude-sonnet-5-5", label: "Sonnet 5.5" },
    ],
    efforts: [
      { id: "high", label: "High" },
      { id: "xhigh", label: "Extra high" },
    ],
    keys: [
      {
        name: "CLAUDE_CODE_OAUTH_TOKEN",
        group: "keys",
        title: "Claude subscription",
        what: "Runs the assistant and every session on your Claude plan",
        where: "Run claude setup-token.",
        hint: "sk-ant-oat01-…",
        clear: false,
        on: true,
        set: true,
        changed: null,
      },
      {
        name: "ELEVENLABS_API_KEY",
        group: "keys",
        title: "ElevenLabs",
        what: "Turns voice notes into text",
        where: "elevenlabs.io → API keys.",
        hint: "sk_…",
        clear: true,
        on: true,
        set: false,
        changed: null,
      },
      {
        name: "TELEGRAM_BUSINESS_TOKEN",
        group: "channels",
        title: "Telegram Business",
        what: "Reads the chats you chose",
        where: "@BotFather.",
        hint: "123456789:AA…",
        clear: true,
        on: false,
        set: false,
        changed: null,
      },
    ],
    git: [{ key: "example_gitlab", repos: ["example-group/notes"], set: false, changed: null }],
    project_keys: [],
    done: {},
    ...over,
  };
}

function row(title: string) {
  const name = screen.getByText(title, { selector: "span" });
  const li = name.closest("li");
  if (!li) throw new Error(`no row ${title}`);
  return within(li);
}

describe("Settings", () => {
  it("offers Set or Replace by whether a key is set, and hides a section with nothing in it", () => {
    render(<SettingsBody view={view()} waiting={{}} send={vi.fn()} />);
    expect(row("Claude subscription").getByRole("button", { name: "Replace" })).toBeTruthy();
    expect(row("ElevenLabs").getByRole("button", { name: "Set" })).toBeTruthy();
    expect(row("example_gitlab").getByText(/example-group\/notes/)).toBeTruthy(); // its repositories
    expect(screen.queryByRole("region", { name: "Projects" })).toBeNull(); // a section with nothing in it is not shown
  });

  it("sends a typed key once and drops it from the page, whatever comes back", async () => {
    const send = vi.fn().mockResolvedValue("a".repeat(32));
    const { container } = render(<SettingsBody view={view()} waiting={{}} send={send} />);
    fireEvent.click(row("ElevenLabs").getByRole("button", { name: "Set" }));
    const field = screen.getByLabelText("New ElevenLabs") as HTMLInputElement;
    expect(field.type).toBe("password");
    expect(field.autocomplete).toBe("new-password");
    fireEvent.change(field, { target: { value: `  ${TYPED}\n` } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(send).toHaveBeenCalledOnce());
    expect(send).toHaveBeenCalledWith("key", "ELEVENLABS_API_KEY", { name: "ELEVENLABS_API_KEY", value: TYPED });
    await waitFor(() => expect(screen.queryByLabelText("New ElevenLabs")).toBeNull());
    expect(container.innerHTML).not.toContain(TYPED);
  });

  it("keeps a refused key's words on the row and still drops the value", async () => {
    const send = vi.fn().mockRejectedValue(new Error("This does not look like the right key."));
    const { container } = render(<SettingsBody view={view()} waiting={{}} send={send} />);
    fireEvent.click(row("ElevenLabs").getByRole("button", { name: "Set" }));
    const field = screen.getByLabelText("New ElevenLabs") as HTMLInputElement;
    fireEvent.change(field, { target: { value: TYPED } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText("This does not look like the right key.")).toBeTruthy());
    expect(field.value).toBe("");
    expect(container.innerHTML).not.toContain(TYPED);
  });

  it("shows a host's refusal of a request, and «Saving…» while it waits", () => {
    const id = "b".repeat(32);
    const waiting = { [id]: { about: "git:example_gitlab", since: Date.now() } };
    expect(requestState(id, waiting, view())).toBe("waiting");
    const refused = view({ done: { [id]: { ok: false, at: 1, why: "The token was not written: not a token" } } });
    expect(requestState(id, {}, refused)).toBe("refused");
    expect(requestState(id, {}, view())).toBe("lost");
    expect(requestState(null, {}, view())).toBe("none");
  });

  it("offers Remove only for an optional key that is set, and asks first", async () => {
    const send = vi.fn().mockResolvedValue("c".repeat(32));
    const keys = view().keys.map((k) => (k.name === "ELEVENLABS_API_KEY" ? { ...k, set: true } : k));
    render(<SettingsBody view={view({ keys })} waiting={{}} send={send} />);
    fireEvent.click(row("Claude subscription").getByRole("button", { name: "Replace" }));
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull(); // the stack needs it
    fireEvent.click(row("Claude subscription").getByRole("button", { name: "Cancel" }));
    fireEvent.click(row("ElevenLabs").getByRole("button", { name: "Replace" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(send).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Keep it" })).toBeTruthy(); // asked first
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(send).toHaveBeenCalledWith("key", "ELEVENLABS_API_KEY", { name: "ELEVENLABS_API_KEY", clear: true }),
    );
  });

  it("sends a lane's model, and offers Restart once a change waits on it", async () => {
    const send = vi.fn().mockResolvedValue("d".repeat(32));
    const { rerender } = render(<SettingsBody view={view()} waiting={{}} send={send} />);
    expect(screen.queryByRole("button", { name: "Restart" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "claude-sonnet-5-5" } });
    await waitFor(() =>
      expect(send).toHaveBeenCalledWith("lane", "lane:private:model", { lane: "private", model: "claude-sonnet-5-5" }),
    );
    const [lane] = view().lanes;
    if (!lane) throw new Error("no lane");
    const lanes = [{ ...lane, model: "claude-sonnet-5-5" }];
    rerender(<SettingsBody view={view({ lanes, pending: ["platform"] })} waiting={{}} send={send} />);
    fireEvent.click(screen.getByRole("button", { name: "Restart" }));
    await waitFor(() => expect(send).toHaveBeenCalledWith("restart", "restart", {}));
  });

  it("disables every control while the server's service is not installed", () => {
    render(<SettingsBody view={view({ ready: false })} waiting={{}} send={vi.fn()} />);
    expect(screen.getByText(/deploy\/setup\.sh/)).toBeTruthy(); // the command to run
    expect((screen.getByLabelText("Model") as HTMLSelectElement).disabled).toBe(true);
    expect((row("ElevenLabs").getByRole("button", { name: "Set" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
