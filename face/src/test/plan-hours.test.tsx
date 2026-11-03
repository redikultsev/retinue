// The plan's settings: the window's hours over the grid open a sheet; a save is one press of `hours`, refused on its
// face when the zone is not one.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HoursButton } from "@/components/plan/hours";
import { planApi } from "@/lib/plan";

const W = { zone: "Europe/Berlin", opens: "10:00", closes: "01:00" };

afterEach(() => vi.restoreAllMocks());

describe("plan settings", () => {
  it("shows the window's hours and saves a new one as one press", async () => {
    const sent = vi.spyOn(planApi, "press").mockResolvedValue({ ok: true });
    render(<HoursButton window={W} />);
    fireEvent.click(screen.getByRole("button", { name: "Plan settings" }));
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText("Closes"), { target: { value: "22:00" } });
    fireEvent.click(save);
    await waitFor(() => expect(sent).toHaveBeenCalledWith({ action: "hours", opens: "10:00", closes: "22:00", zone: "Europe/Berlin" }));
  });

  it("keeps a zone the browser's list leaves out, and refuses a window under two hours", async () => {
    vi.spyOn(Intl, "supportedValuesOf").mockReturnValue(["Europe/Berlin"]);
    const sent = vi.spyOn(planApi, "press").mockResolvedValue({ ok: true });
    render(<HoursButton window={{ zone: "UTC", opens: "09:00", closes: "23:00" }} />);
    fireEvent.click(screen.getByRole("button", { name: "Plan settings" }));
    expect(screen.queryByText("Not a zone: pick one from the list.")).toBeNull();
    fireEvent.change(screen.getByLabelText("Closes"), { target: { value: "10:00" } });
    expect(screen.getByText("A window is at least two hours.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Closes"), { target: { value: "22:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(sent).toHaveBeenCalledWith({ action: "hours", opens: "09:00", closes: "22:00", zone: "UTC" }));
  });

  it("does not send a zone that is not one", () => {
    const sent = vi.spyOn(planApi, "press").mockResolvedValue({ ok: true });
    render(<HoursButton window={W} />);
    fireEvent.click(screen.getByRole("button", { name: "Plan settings" }));
    fireEvent.change(screen.getByLabelText("Zone"), { target: { value: "Mars/Olympus" } });
    expect(screen.getByText("Not a zone: pick one from the list.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(sent).not.toHaveBeenCalled();
  });
});
