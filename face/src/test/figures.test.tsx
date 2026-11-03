// Sums by month and a year's line drawn and said in words, money in its currency; a project's own view framed under the
// sandbox the platform names, its data posted to it.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CodeFrame, Months, sumWords, YearLine } from "@/components/views/figures";
import type { CodeView, CollectionViews, MonthsView, YearView } from "@/lib/collections";

afterEach(cleanup);

const MONTHS: MonthsView = {
  name: "spent", kind: "months", title: "Spent by month", date: "date", sum: "amount", unit: "EUR", group: "kind",
  values: ["rent", "food"],
  months: [
    { first: "2026-07-01", totals: [] },
    { first: "2026-08-01", totals: [{ value: "food", total: "12.50" }] },
    { first: "2026-09-01", totals: [{ value: "rent", total: "900.00" }, { value: "food", total: "0.30" }] },
  ],
};
const months = (n: number) => Array.from({ length: 12 }, (_, i) => ({ first: `2026-${String(i + 1).padStart(2, "0")}-01`, total: "0.00", running: String(n * (i + 1)) }));
const YEAR: YearView = {
  name: "year", kind: "year", title: "This year", date: "date", sum: "amount", unit: "EUR", year: 2026, through: 9,
  months: months(100), previous: months(80).map((m) => ({ ...m, first: m.first.replace("2026", "2025") })),
};
const LEDGER: CollectionViews = { name: "ledger", rev: "01J8ZQ4Y7Z5G0000000000000A", labels: { kind: { rent: "Rent" } }, views: [MONTHS, YEAR] };

describe("the figures", () => {
  it("test_sums_by_month_and_a_years_line_render", () => {
    render(<Months collection={LEDGER} view={MONTHS} />);
    const spent = screen.getByTestId("months-spent");
    expect(within(spent.querySelector("figcaption")!).getByText("€900.30")).toBeTruthy();
    expect(spent.querySelectorAll("rect")).toHaveLength(3);
    const rows = within(spent).getAllByRole("row").map((r) => r.textContent);
    expect(rows).toEqual(["MonthRentFoodTotal", "Jul€0.00€0.00€0.00", "Aug€0.00€12.50€12.50", "Sep€900.00€0.30€900.30"]);

    render(<YearLine view={YEAR} />);
    const year = screen.getByTestId("year-year");
    const caption = within(year.querySelector("figcaption")!);
    expect(caption.getByText("€900.00")).toBeTruthy();
    expect(caption.getByText("€720.00")).toBeTruthy();
    const [previous, now] = [...year.querySelectorAll("polyline")];
    expect(previous!.getAttribute("points")!.split(" ")).toHaveLength(12);
    expect(now!.getAttribute("points")!.split(" ")).toHaveLength(9);
    expect(sumWords(1234, null)).toBe("1,234");
  });

  it("frames a project's own view under the sandbox it is given and posts it its data", () => {
    const view: CodeView = {
      name: "burn", kind: "code", title: "Burn-down", src: "/api/c/invoices/_code/burn", sandbox: "allow-scripts allow-forms allow-modals",
      fields: { company: { type: "text" } }, labels: {}, count: 1, rows: [{ id: "01J8ZQ4Y7Z5G0000000000000A", version: 1, fields: { company: "Acme" }, refs: {}, src: null, days: null, rotten: false }], dropped: 0,
    };
    render(<CodeFrame view={view} />);
    const frame = screen.getByTitle("Burn-down") as HTMLIFrameElement;
    expect(frame.getAttribute("sandbox")).toBe(view.sandbox);
    expect(frame.getAttribute("src")).toBe(view.src);
    const post = vi.spyOn(frame.contentWindow!, "postMessage");
    fireEvent.load(frame);
    expect(post).toHaveBeenCalledWith({ view: expect.objectContaining({ rows: view.rows, count: 1 }) }, "*");
  });
});
