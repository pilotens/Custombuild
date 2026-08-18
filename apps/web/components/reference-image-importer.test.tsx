import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { imageFileFromClipboard, ReferenceImageImporter } from "./reference-image-importer";

const ORIGINAL_CREATE_OBJECT_URL = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
const ORIGINAL_REVOKE_OBJECT_URL = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");

function syntheticLibraryPixels(): ImageData {
  const width = 360;
  const height = 260;
  const data = new Uint8ClampedArray(width * height * 4);
  const paint = (left: number, top: number, right: number, bottom: number, color: [number, number, number]) => {
    for (let y = top; y < bottom; y += 1) {
      for (let x = left; x < right; x += 1) {
        const offset = (y * width + x) * 4;
        data[offset] = color[0];
        data[offset + 1] = color[1];
        data[offset + 2] = color[2];
        data[offset + 3] = 255;
      }
    }
  };
  paint(0, 0, width, height, [246, 246, 242]);
  paint(30, 20, 330, 235, [194, 162, 111]);
  paint(30, 174, 330, 235, [111, 54, 28]);
  for (const x of [30, 90, 150, 210, 270, 330]) paint(x - 2, 20, x + 2, 235, [70, 45, 30]);
  for (const y of [20, 50, 80, 110, 140, 174, 235]) paint(30, y - 2, 330, y + 2, [74, 48, 30]);
  return { width, height, data, colorSpace: "srgb" } as ImageData;
}

beforeEach(() => {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:reference-preview"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
  vi.stubGlobal("Image", class {
    naturalWidth = 360;
    naturalHeight = 260;
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;

    set src(_value: string) {
      queueMicrotask(() => this.onload?.());
    }
  });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: vi.fn(),
    getImageData: vi.fn(() => syntheticLibraryPixels()),
  } as never);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (ORIGINAL_CREATE_OBJECT_URL) {
    Object.defineProperty(URL, "createObjectURL", ORIGINAL_CREATE_OBJECT_URL);
  } else {
    delete (URL as Partial<typeof URL>).createObjectURL;
  }
  if (ORIGINAL_REVOKE_OBJECT_URL) {
    Object.defineProperty(URL, "revokeObjectURL", ORIGINAL_REVOKE_OBJECT_URL);
  } else {
    delete (URL as Partial<typeof URL>).revokeObjectURL;
  }
});

const inspectReference = vi.fn(async () => ({
  import_id: "11111111-1111-4111-8111-111111111111",
  project_id: "22222222-2222-4222-8222-222222222222",
  image_sha256: "a".repeat(64),
  media_type: "image/png",
  size_bytes: 1_024,
}));

describe("ReferenceImageImporter", () => {
  it("starts with a clear, validated upload step", () => {
    const onClose = vi.fn();
    render(<ReferenceImageImporter open onClose={onClose} onInspect={inspectReference} onApply={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Skapa från referensbild" })).toBeVisible();
    expect(screen.getByLabelText("Välj referensbild")).toHaveAttribute("accept", "image/jpeg,image/png,image/webp");
    expect(screen.getByLabelText("Välj referensbild")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByText(/Rak framifrån/)).toBeVisible();
    expect(screen.getByText(/dolda infästningar kan inte avläsas säkert/)).toBeVisible();
    expect(screen.getByText("Klistra in skärmklipp")).toBeVisible();
    expect(screen.getByLabelText("Uppladdningsruta för referensbild")).toHaveAttribute("aria-keyshortcuts", "Control+V Meta+V");
    fireEvent.click(screen.getByRole("button", { name: "Stäng bildimporten" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("renders nothing while closed", () => {
    const { container } = render(<ReferenceImageImporter open={false} onClose={vi.fn()} onInspect={inspectReference} onApply={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("extracts supported screenshots from clipboard files and items", () => {
    const screenshot = new File(["pixels"], "skärmklipp.png", { type: "image/png" });
    expect(imageFileFromClipboard({ files: [screenshot], items: [] })).toBe(screenshot);
    expect(imageFileFromClipboard({
      files: [],
      items: [{ kind: "file", type: "image/png", getAsFile: () => screenshot }],
    })).toBe(screenshot);
    expect(imageFileFromClipboard({
      files: [],
      items: [{ kind: "string", type: "text/plain", getAsFile: () => null }],
    })).toBeUndefined();
  });

  it("explains when Ctrl+V does not contain an image", () => {
    render(<ReferenceImageImporter open onClose={vi.fn()} onInspect={inspectReference} onApply={vi.fn()} />);
    fireEvent.paste(screen.getByLabelText("Uppladdningsruta för referensbild"), {
      clipboardData: {
        files: [],
        items: [{ kind: "string", type: "text/plain", getAsFile: () => null }],
      },
    });
    expect(screen.getByRole("alert")).toHaveTextContent(/Urklippet innehåller ingen/);
  });

  it("shows replacement failures and starts clean when reopened", async () => {
    const { rerender } = render(<ReferenceImageImporter open onClose={vi.fn()} onInspect={inspectReference} onApply={vi.fn()} />);
    const input = screen.getByLabelText("Välj referensbild");
    fireEvent.change(input, {
      target: { files: [new File(["gif"], "fel.gif", { type: "image/gif" })] },
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Bilden kunde inte användas/);
    expect(alert).toHaveTextContent(/tidigare tolkningen har inte ändrats/);
    await waitFor(() => expect(alert).toHaveFocus());

    fireEvent.click(screen.getByRole("button", { name: "Stäng bildimporten" }));
    rerender(<ReferenceImageImporter open={false} onClose={vi.fn()} onInspect={inspectReference} onApply={vi.fn()} />);
    rerender(<ReferenceImageImporter open onClose={vi.fn()} onInspect={inspectReference} onApply={vi.fn()} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("Ladda upp en möbelbild")).toBeVisible();
  });

  it("offers the full confirmed B1 envelope while keeping image inference explicitly limited", async () => {
    const onApply = vi.fn();
    render(<ReferenceImageImporter open onClose={vi.fn()} onInspect={inspectReference} onApply={onApply} />);
    fireEvent.change(screen.getByLabelText("Välj referensbild"), {
      target: { files: [new File(["pixels"], "bibliotek.png", { type: "image/png" })] },
    });

    await screen.findByRole("heading", { name: "Kontrollera tolkningen" });
    const width = screen.getByRole("spinbutton", { name: /^Bredd/ });
    const height = screen.getByRole("spinbutton", { name: "Höjdmm" });
    const depth = screen.getByRole("spinbutton", { name: /^Djup/ });
    const shelves = screen.getByRole("spinbutton", { name: "Hyllor" });
    const dividers = screen.getByRole("spinbutton", { name: "Avdelare" });
    const baseHeight = screen.getByRole("spinbutton", { name: "Höjd underskåpmm" });
    const baseCount = screen.getByRole("spinbutton", { name: "Skåpsmoduler" });

    expect(width).toHaveAttribute("min", "250");
    expect(width).toHaveAttribute("max", "6000");
    expect(height).toHaveAttribute("min", "300");
    expect(height).toHaveAttribute("max", "4000");
    expect(depth).toHaveAttribute("min", "100");
    expect(depth).toHaveAttribute("max", "1200");
    expect(shelves).toHaveAttribute("max", "40");
    expect(dividers).toHaveAttribute("max", "16");
    expect(baseCount).toHaveAttribute("max", "17");
    expect(screen.getByText(/högst 20 hyllor och 12 avdelare/)).toBeVisible();

    for (const [input, value] of [
      [width, "6000"],
      [height, "4000"],
      [depth, "1200"],
      [shelves, "20"],
      [dividers, "16"],
      [baseHeight, "2000"],
      [baseCount, "17"],
    ] as const) {
      fireEvent.change(input, { target: { value } });
      fireEvent.blur(input);
    }
    fireEvent.click(screen.getByRole("button", { name: "Skapa konceptmodell" }));

    expect(onApply).toHaveBeenCalledWith(expect.objectContaining({
      patch: expect.objectContaining({
        width_mm: 6_000,
        height_mm: 4_000,
        depth_mm: 1_200,
        shelf_count: 20,
        divider_count: 16,
        base_cabinet_height_mm: 2_000,
        base_cabinet_depth_mm: 1_200,
        base_cabinet_count: 17,
      }),
      metadata: expect.objectContaining({ verification_status: "concept" }),
    }));
  });
});
