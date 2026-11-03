// The plan's settings: the window's hours over the grid open a sheet; a save is one verb of the schedule — `hours`,
// `activities` —, refused on its face when the zone is not one.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActivitiesForm, HoursButton } from "@/components/plan/hours";
import { ScheduleContext } from "@/components/plan/shared";
import type { Schedule } from "@/lib/schedule";

const W = { zone: "Europe/Berlin", opens: "10:00", closes: "01:00" };

afterEach(() => vi.restoreAllMocks());

/** `ui` over a schedule that records its saves. */
function under(ui: ReactElement) {
  const s = { hours: vi.fn().mockResolvedValue(true), activities: vi.fn().mockResolvedValue(true) };
  render(<ScheduleContext value={s as unknown as Schedule}>{ui}</ScheduleContext>);
  return s;
}

describe("plan settings", () => {
  it("shows the window's hours and saves a new one as one press", async () => {
    const { hours } = under(<HoursButton window={W} />);
    fireEvent.click(screen.getByRole("button", { name: "Plan settings" }));
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText("Closes"), { target: { value: "22:00" } });
    fireEvent.click(save);
    await waitFor(() => expect(hours).toHaveBeenCalledWith({ opens: "10:00", closes: "22:00", zone: "Europe/Berlin" }, "Plan window: 10:00–22:00"));
  });

  it("keeps a zone the browser's list leaves out, and refuses a window under two hours", async () => {
    vi.spyOn(Intl, "supportedValuesOf").mockReturnValue(["Europe/Berlin"]);
    const { hours } = under(<HoursButton window={{ zone: "UTC", opens: "09:00", closes: "23:00" }} />);
    fireEvent.click(screen.getByRole("button", { name: "Plan settings" }));
    expect(screen.queryByText("Not a zone: pick one from the list.")).toBeNull();
    fireEvent.change(screen.getByLabelText("Closes"), { target: { value: "10:00" } });
    expect(screen.getByText("A window is at least two hours.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Closes"), { target: { value: "22:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(hours).toHaveBeenCalledWith({ opens: "09:00", closes: "22:00", zone: "UTC" }, "Plan window: 09:00–22:00"));
  });

  it("does not send a zone that is not one", () => {
    const { hours } = under(<HoursButton window={W} />);
    fireEvent.click(screen.getByRole("button", { name: "Plan settings" }));
    fireEvent.change(screen.getByLabelText("Zone"), { target: { value: "Mars/Olympus" } });
    expect(screen.getByText("Not a zone: pick one from the list.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(hours).not.toHaveBeenCalled();
  });
});

const nth = (label: string, n: number): HTMLElement => screen.getAllByLabelText(label)[n] as HTMLElement;

describe("activities", () => {
  const LUNCH = { title: "Lunch", minutes: 45, from: "13:00", to: "16:00" };

  it("adds one and saves the list as one press, a range only with both ends", async () => {
    const { activities } = under(<ActivitiesForm activities={[LUNCH]} done={() => {}} />);
    const save = screen.getByRole("button", { name: "Save activities" });
    expect(save).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: "Add an activity" }));
    fireEvent.change(nth("Name", 1), { target: { value: "Rest" } });
    fireEvent.change(nth("Starts from", 1), { target: { value: "15:00" } });
    expect(save).toHaveProperty("disabled", true);
    fireEvent.change(nth("Starts from", 1), { target: { value: "" } });
    fireEvent.click(save);
    await waitFor(() =>
      expect(activities).toHaveBeenCalledWith([LUNCH, { title: "Rest", minutes: 30 }], "Activities saved"),
    );
  });

  it("refuses a name used twice", () => {
    render(<ActivitiesForm activities={[LUNCH]} done={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Add an activity" }));
    fireEvent.change(nth("Name", 1), { target: { value: "lunch" } });
    expect(screen.getAllByText("Named twice.").length).toBe(2);
    expect(screen.getByRole("button", { name: "Save activities" })).toHaveProperty("disabled", true);
  });
});
