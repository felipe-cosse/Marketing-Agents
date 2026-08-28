import type { Rect } from "./layout";

export const VIEWPORT = Object.freeze({
  fitPadding: 28,
  panOverscan: 64,
  selectionMargin: 24,
  minZoom: 0.35,
  maxZoom: 1.75,
  zoomFactor: 1.2,
  wheelZoomSensitivity: 0.002,
  maxWheelZoomDelta: 240,
  keyboardPanStep: 48,
  dragThreshold: 4,
} as const);

export interface ViewportSize {
  readonly width: number;
  readonly height: number;
}

export interface ViewportTransform {
  readonly zoom: number;
  readonly translateX: number;
  readonly translateY: number;
  readonly intent: "auto-fit" | "manual";
}

function finitePositive(value: number, label: string): number {
  if (!Number.isFinite(value) || value <= 0) {
    throw new RangeError(`${label} must be a finite positive number`);
  }
  return value;
}

function round(value: number): number {
  return Math.round(value * 10_000) / 10_000;
}

export function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function clampDimension(
  translation: number,
  viewportSize: number,
  boundsStart: number,
  boundsSize: number,
  zoom: number,
): number {
  const scaled = boundsSize * zoom;
  if (scaled <= viewportSize) {
    return round((viewportSize - scaled) / 2 - boundsStart * zoom);
  }
  const minimum =
    viewportSize - VIEWPORT.panOverscan - (boundsStart + boundsSize) * zoom;
  const maximum = VIEWPORT.panOverscan - boundsStart * zoom;
  return round(clamp(translation, minimum, maximum));
}

export function clampTransform(
  transform: ViewportTransform,
  viewport: ViewportSize,
  bounds: Rect,
): ViewportTransform {
  finitePositive(viewport.width, "viewport width");
  finitePositive(viewport.height, "viewport height");
  finitePositive(bounds.width, "bounds width");
  finitePositive(bounds.height, "bounds height");
  const zoom = round(clamp(transform.zoom, VIEWPORT.minZoom, VIEWPORT.maxZoom));
  return {
    zoom,
    translateX: clampDimension(
      transform.translateX,
      viewport.width,
      bounds.x,
      bounds.width,
      zoom,
    ),
    translateY: clampDimension(
      transform.translateY,
      viewport.height,
      bounds.y,
      bounds.height,
      zoom,
    ),
    intent: transform.intent,
  };
}

export function fitTransform(
  viewport: ViewportSize,
  bounds: Rect,
): ViewportTransform {
  finitePositive(viewport.width, "viewport width");
  finitePositive(viewport.height, "viewport height");
  finitePositive(bounds.width, "bounds width");
  finitePositive(bounds.height, "bounds height");
  const rawZoom = Math.min(
    (viewport.width - 2 * VIEWPORT.fitPadding) / bounds.width,
    (viewport.height - 2 * VIEWPORT.fitPadding) / bounds.height,
    1,
  );
  const zoom = round(clamp(rawZoom, VIEWPORT.minZoom, 1));
  return {
    zoom,
    translateX: round(
      (viewport.width - bounds.width * zoom) / 2 - bounds.x * zoom,
    ),
    translateY: round(
      (viewport.height - bounds.height * zoom) / 2 - bounds.y * zoom,
    ),
    intent: "auto-fit",
  };
}

export function zoomAt(
  transform: ViewportTransform,
  targetZoom: number,
  screenAnchor: { readonly x: number; readonly y: number },
  viewport: ViewportSize,
  bounds: Rect,
): ViewportTransform {
  const zoom = round(clamp(targetZoom, VIEWPORT.minZoom, VIEWPORT.maxZoom));
  const worldX = (screenAnchor.x - transform.translateX) / transform.zoom;
  const worldY = (screenAnchor.y - transform.translateY) / transform.zoom;
  return clampTransform(
    {
      zoom,
      translateX: round(screenAnchor.x - worldX * zoom),
      translateY: round(screenAnchor.y - worldY * zoom),
      intent: "manual",
    },
    viewport,
    bounds,
  );
}

export function panBy(
  transform: ViewportTransform,
  delta: { readonly x: number; readonly y: number },
  viewport: ViewportSize,
  bounds: Rect,
): ViewportTransform {
  return clampTransform(
    {
      ...transform,
      translateX: round(transform.translateX + delta.x),
      translateY: round(transform.translateY + delta.y),
      intent: "manual",
    },
    viewport,
    bounds,
  );
}

export function wheelZoomTarget(currentZoom: number, deltaY: number): number {
  finitePositive(currentZoom, "current zoom");
  if (!Number.isFinite(deltaY)) {
    throw new RangeError("wheel delta must be finite");
  }
  const boundedDelta = clamp(
    deltaY,
    -VIEWPORT.maxWheelZoomDelta,
    VIEWPORT.maxWheelZoomDelta,
  );
  return currentZoom * Math.exp(-boundedDelta * VIEWPORT.wheelZoomSensitivity);
}

export function preserveWorldCenter(
  transform: ViewportTransform,
  previousViewport: ViewportSize,
  nextViewport: ViewportSize,
  bounds: Rect,
): ViewportTransform {
  const worldCenterX =
    (previousViewport.width / 2 - transform.translateX) / transform.zoom;
  const worldCenterY =
    (previousViewport.height / 2 - transform.translateY) / transform.zoom;
  return clampTransform(
    {
      ...transform,
      translateX: nextViewport.width / 2 - worldCenterX * transform.zoom,
      translateY: nextViewport.height / 2 - worldCenterY * transform.zoom,
      intent: "manual",
    },
    nextViewport,
    bounds,
  );
}

export function revealRect(
  transform: ViewportTransform,
  viewport: ViewportSize,
  bounds: Rect,
  target: Rect,
): ViewportTransform {
  const left = target.x * transform.zoom + transform.translateX;
  const right =
    (target.x + target.width) * transform.zoom + transform.translateX;
  const top = target.y * transform.zoom + transform.translateY;
  const bottom =
    (target.y + target.height) * transform.zoom + transform.translateY;
  let deltaX = 0;
  let deltaY = 0;

  if (left < VIEWPORT.selectionMargin) {
    deltaX = VIEWPORT.selectionMargin - left;
  } else if (right > viewport.width - VIEWPORT.selectionMargin) {
    deltaX = viewport.width - VIEWPORT.selectionMargin - right;
  }
  if (top < VIEWPORT.selectionMargin) {
    deltaY = VIEWPORT.selectionMargin - top;
  } else if (bottom > viewport.height - VIEWPORT.selectionMargin) {
    deltaY = viewport.height - VIEWPORT.selectionMargin - bottom;
  }

  if (deltaX === 0 && deltaY === 0) {
    return transform;
  }
  return panBy(transform, { x: deltaX, y: deltaY }, viewport, bounds);
}
