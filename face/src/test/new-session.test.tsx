import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const navigate = vi.fn();
vi.mock("@tanstack/react-router", () => ({ useNavigate: () => navigate }));
const newSession = vi.fn();
vi.mock("@/lib/api", async (real) => ({ ...(await real<typeof import("@/lib/api")>()), api: { newSession: (...a: unknown[]) => newSession(...a) } }));

import { NewSession } from "@/components/new-session";
import { live } from "@/lib/live";
import type { Meta } from "@/lib/types";

describe("a new session from a sphere's «＋»", () => {
  beforeEach(() => {
    newSession.mockReset().mockResolvedValue({ session: "s9" });
    live.meta = {
      tracks: [
        { name: "base", lane: "private", kind: "private" },
        { name: "retinue", lane: "code", kind: "code" },
      ],
      spheres: [{ name: "travel", title: "Travel", track: "base", icon: "✈️" }],
      general: "base",
    } as unknown as Meta;
  });

  it("asks only what to do: no sphere, no track — the sphere pressed, on its own track", async () => {
    render(<NewSession open onClose={vi.fn()} sphere="travel" />);
    expect(screen.queryByText("Sphere")).toBeNull();
    expect(screen.queryByText("Track")).toBeNull();
    expect(screen.getByText(/New session · .*Travel/)).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText(/For example/), { target: { value: " find a flat " } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(newSession).toHaveBeenCalledTimes(1));
    expect(newSession.mock.calls[0]?.slice(0, 2)).toEqual(["base", "find a flat"]);
    expect(newSession.mock.calls[0]?.[3]).toBe("travel");
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/s/$id", params: { id: "s9" } }));
  });
});

describe("the platform's word to a session", () => {
  it("is a note under its own name, never the owner's bubble", async () => {
    const { InputBubble } = await import("@/components/transcript/input");
    const text = "Message from the platform, not from the owner. While you were editing the Base, its hub changed a.md.";
    render(<InputBubble input={{ input_id: "base:1", session: "s1", text, at: 0, turn: 0, n: 0, attachments: [], by: "platform" }} />);
    const note = screen.getByTestId("platform-note");
    expect(note.textContent).toBe("From the platformWhile you were editing the Base, its hub changed a.md.");
    expect(screen.queryByTestId("input")).toBeNull();
  });
});
