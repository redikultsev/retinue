// A pane's room: «Today» and the map lay two columns only where both fit — the open chat takes its width, not the window's.
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useRoomFor } from "@/lib/media";

function Probe({ min }: { min: number }) {
  const [ref, roomy] = useRoomFor<HTMLDivElement>(min);
  return <div ref={ref}>{roomy ? "two" : "one"}</div>;
}

describe("useRoomFor", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("says whether the element is as wide as asked, and follows it as it changes", () => {
    let told: ResizeObserverCallback = () => undefined;
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(cb: ResizeObserverCallback) {
          told = cb;
        }
        observe() {}
        disconnect() {}
      },
    );
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 500 } as DOMRect);
    render(<Probe min={820} />);
    expect(screen.getByText("one")).toBeTruthy();
    act(() => told([{ contentRect: { width: 1200 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(screen.getByText("two")).toBeTruthy();
    rect.mockRestore();
  });

  it("takes an element not laid out yet as roomy", () => {
    render(<Probe min={820} />);
    expect(screen.getByText("two")).toBeTruthy();
  });
});
