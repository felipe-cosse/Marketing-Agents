// WEB-01 viewport algebra is bounded and reproducible without browser measurements.
import { describe, expect, it } from "vitest";

import {
  clampTransform,
  fitTransform,
  panBy,
  preserveWorldCenter,
  revealRect,
  wheelZoomTarget,
  zoomAt,
  type ViewportTransform,
} from "./viewport";

const bounds = { x: 0, y: 0, width: 1480, height: 754 } as const;

describe("WEB-01 bounded chart viewport", () => {
  it("fits the complete hierarchy at the accepted desktop viewport", () => {
    expect(fitTransform({ width: 1536, height: 856 }, bounds)).toEqual({
      zoom: 1,
      translateX: 28,
      translateY: 51,
      intent: "auto-fit",
    });
    expect(fitTransform({ width: 1206, height: 856 }, bounds)).toEqual({
      zoom: 0.777,
      translateX: 28.02,
      translateY: 135.071,
      intent: "auto-fit",
    });
  });

  it("zooms around the requested screen anchor and restores the baseline", () => {
    const size = { width: 1536, height: 856 };
    const baseline = fitTransform(size, bounds);
    const zoomed = zoomAt(baseline, 1.2, { x: 768, y: 428 }, size, bounds);
    expect(zoomed.zoom).toBe(1.2);
    const restored = zoomAt(zoomed, 1, { x: 768, y: 428 }, size, bounds);
    expect(restored).toEqual({ ...baseline, intent: "manual" });
  });

  it("derives smooth bounded wheel-zoom targets from the wheel delta", () => {
    expect(wheelZoomTarget(1, 0)).toBe(1);
    expect(wheelZoomTarget(1, -50)).toBeCloseTo(Math.exp(0.1), 8);
    expect(wheelZoomTarget(1, 50)).toBeCloseTo(Math.exp(-0.1), 8);
    expect(wheelZoomTarget(1, -10_000)).toBeCloseTo(Math.exp(0.48), 8);
    expect(() => wheelZoomTarget(1, Number.NaN)).toThrow(RangeError);
  });

  it("clamps extreme pan to the exact overscan bounds", () => {
    const transform: ViewportTransform = {
      zoom: 1.5,
      translateX: -99_999,
      translateY: -99_999,
      intent: "manual",
    };
    expect(
      clampTransform(transform, { width: 1000, height: 700 }, bounds),
    ).toEqual({
      zoom: 1.5,
      translateX: -1284,
      translateY: -495,
      intent: "manual",
    });
    expect(
      panBy(
        { ...transform, translateX: 99_999, translateY: 99_999 },
        { x: 1, y: 1 },
        { width: 1000, height: 700 },
        bounds,
      ),
    ).toEqual({ zoom: 1.5, translateX: 64, translateY: 64, intent: "manual" });
  });

  it("preserves a manual world center during resize", () => {
    const original: ViewportTransform = {
      zoom: 1.2,
      translateX: -300,
      translateY: -100,
      intent: "manual",
    };
    const resized = preserveWorldCenter(
      original,
      { width: 1000, height: 700 },
      { width: 1200, height: 800 },
      bounds,
    );
    expect(resized).toEqual({
      zoom: 1.2,
      translateX: -200,
      translateY: -50,
      intent: "manual",
    });
  });

  it("reveals a selected card by the smallest translation without changing zoom", () => {
    const transform: ViewportTransform = {
      zoom: 1,
      translateX: -900,
      translateY: 0,
      intent: "manual",
    };
    const revealed = revealRect(
      transform,
      { width: 1000, height: 700 },
      bounds,
      { x: 10, y: 230, width: 104, height: 80 },
    );
    expect(revealed.zoom).toBe(1);
    expect(revealed.translateX).toBe(14);
    expect(revealed.translateY).toBe(0);
  });

  it.each([
    { width: 0, height: 10 },
    { width: Number.NaN, height: 10 },
    { width: 10, height: Number.POSITIVE_INFINITY },
  ])("rejects invalid viewport dimensions", (size) => {
    expect(() => fitTransform(size, bounds)).toThrow(RangeError);
  });
});
