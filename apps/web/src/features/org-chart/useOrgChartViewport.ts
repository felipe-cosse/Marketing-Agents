import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";

import type { Rect } from "./layout";
import {
  VIEWPORT,
  clampTransform,
  fitTransform,
  panBy,
  preserveWorldCenter,
  revealRect,
  wheelZoomTarget,
  zoomAt,
  type ViewportSize,
  type ViewportTransform,
} from "./viewport";

interface DragState {
  readonly pointerId: number;
  readonly startX: number;
  readonly startY: number;
  readonly origin: ViewportTransform;
  moved: boolean;
}

interface UseOrgChartViewportOptions {
  readonly bounds: Rect;
  readonly structuralKey: string;
  readonly selectedRect: Rect | null;
  readonly onClearSelection: () => void;
}

interface OrgChartViewportApi {
  readonly containerRef: RefObject<HTMLDivElement | null>;
  readonly transform: ViewportTransform;
  readonly fit: () => void;
  readonly zoomIn: () => void;
  readonly zoomOut: () => void;
  readonly pointerHandlers: {
    readonly onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
    readonly onPointerMove: (event: ReactPointerEvent<HTMLDivElement>) => void;
    readonly onPointerUp: (event: ReactPointerEvent<HTMLDivElement>) => void;
    readonly onPointerCancel: (
      event: ReactPointerEvent<HTMLDivElement>,
    ) => void;
    readonly onKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
  };
}

function usableSize(width: number, height: number): ViewportSize | null {
  if (
    !Number.isFinite(width) ||
    !Number.isFinite(height) ||
    width <= 0 ||
    height <= 0
  ) {
    return null;
  }
  return { width, height };
}

function isInteractiveTarget(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    target.closest("button, a, input, select, textarea") !== null
  );
}

export function useOrgChartViewport({
  bounds,
  structuralKey,
  selectedRect,
  onClearSelection,
}: UseOrgChartViewportOptions): OrgChartViewportApi {
  const containerRef = useRef<HTMLDivElement>(null);
  const sizeRef = useRef<ViewportSize | null>(null);
  const keyRef = useRef<string | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const [transform, setTransform] = useState<ViewportTransform>(() =>
    fitTransform({ width: 1536, height: 856 }, bounds),
  );

  const fit = useCallback(() => {
    const size = sizeRef.current;
    if (size !== null) {
      setTransform(fitTransform(size, bounds));
    }
  }, [bounds]);

  const changeZoom = useCallback(
    (factor: number) => {
      const size = sizeRef.current;
      if (size === null) {
        return;
      }
      setTransform((current) =>
        zoomAt(
          current,
          current.zoom * factor,
          { x: size.width / 2, y: size.height / 2 },
          size,
          bounds,
        ),
      );
    },
    [bounds],
  );

  const zoomIn = useCallback(
    () => changeZoom(VIEWPORT.zoomFactor),
    [changeZoom],
  );
  const zoomOut = useCallback(
    () => changeZoom(1 / VIEWPORT.zoomFactor),
    [changeZoom],
  );

  useLayoutEffect(() => {
    const element = containerRef.current;
    if (element === null) {
      return undefined;
    }
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry === undefined) {
        return;
      }
      const nextSize = usableSize(
        entry.contentRect.width,
        entry.contentRect.height,
      );
      if (nextSize === null) {
        return;
      }
      const previousSize = sizeRef.current;
      sizeRef.current = nextSize;
      setTransform((current) => {
        if (keyRef.current !== structuralKey || previousSize === null) {
          keyRef.current = structuralKey;
          return fitTransform(nextSize, bounds);
        }
        if (current.intent === "auto-fit") {
          return fitTransform(nextSize, bounds);
        }
        return preserveWorldCenter(current, previousSize, nextSize, bounds);
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [bounds, structuralKey]);

  useLayoutEffect(() => {
    const size = sizeRef.current;
    if (keyRef.current === structuralKey) {
      return;
    }
    keyRef.current = structuralKey;
    if (size !== null) {
      setTransform(fitTransform(size, bounds));
    }
  }, [bounds, structuralKey]);

  useEffect(() => {
    const size = sizeRef.current;
    if (selectedRect !== null && size !== null) {
      setTransform((current) =>
        revealRect(current, size, bounds, selectedRect),
      );
    }
  }, [bounds, selectedRect]);

  useEffect(() => {
    const element = containerRef.current;
    if (element === null) {
      return undefined;
    }
    const handleWheel = (event: WheelEvent): void => {
      const size = sizeRef.current;
      if (size === null) {
        return;
      }
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        if (event.deltaY === 0) {
          return;
        }
        const rect = element.getBoundingClientRect();
        setTransform((current) =>
          zoomAt(
            current,
            wheelZoomTarget(current.zoom, event.deltaY),
            { x: event.clientX - rect.left, y: event.clientY - rect.top },
            size,
            bounds,
          ),
        );
        return;
      }
      if (event.deltaX === 0 && event.deltaY === 0) {
        return;
      }
      setTransform((current) =>
        panBy(current, { x: -event.deltaX, y: -event.deltaY }, size, bounds),
      );
    };
    element.addEventListener("wheel", handleWheel, { passive: false });
    return () => element.removeEventListener("wheel", handleWheel);
  }, [bounds]);

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>): void => {
      if (event.button !== 0 || isInteractiveTarget(event.target)) {
        return;
      }
      event.currentTarget.setPointerCapture(event.pointerId);
      setTransform((current) => {
        dragRef.current = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          origin: current,
          moved: false,
        };
        return current;
      });
    },
    [],
  );

  const onPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>): void => {
      const drag = dragRef.current;
      const size = sizeRef.current;
      if (drag?.pointerId !== event.pointerId || size === null) {
        return;
      }
      const deltaX = event.clientX - drag.startX;
      const deltaY = event.clientY - drag.startY;
      if (!drag.moved && Math.hypot(deltaX, deltaY) < VIEWPORT.dragThreshold) {
        return;
      }
      drag.moved = true;
      setTransform(
        clampTransform(
          {
            ...drag.origin,
            translateX: drag.origin.translateX + deltaX,
            translateY: drag.origin.translateY + deltaY,
            intent: "manual",
          },
          size,
          bounds,
        ),
      );
    },
    [bounds],
  );

  const finishPointer = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>): void => {
      const drag = dragRef.current;
      if (drag?.pointerId !== event.pointerId) {
        return;
      }
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
      if (!drag.moved && !isInteractiveTarget(event.target)) {
        onClearSelection();
      }
      dragRef.current = null;
    },
    [onClearSelection],
  );

  const onKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>): void => {
      if (event.target !== event.currentTarget) {
        return;
      }
      const size = sizeRef.current;
      if (size === null) {
        return;
      }
      let delta: { x: number; y: number } | null = null;
      if (event.key === "ArrowLeft")
        delta = { x: VIEWPORT.keyboardPanStep, y: 0 };
      if (event.key === "ArrowRight")
        delta = { x: -VIEWPORT.keyboardPanStep, y: 0 };
      if (event.key === "ArrowUp")
        delta = { x: 0, y: VIEWPORT.keyboardPanStep };
      if (event.key === "ArrowDown")
        delta = { x: 0, y: -VIEWPORT.keyboardPanStep };
      if (delta !== null) {
        event.preventDefault();
        setTransform((current) => panBy(current, delta, size, bounds));
        return;
      }
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        zoomIn();
      } else if (event.key === "-") {
        event.preventDefault();
        zoomOut();
      } else if (event.key === "0") {
        event.preventDefault();
        fit();
      } else if (event.key === "Escape") {
        onClearSelection();
      }
    },
    [bounds, fit, onClearSelection, zoomIn, zoomOut],
  );

  return {
    containerRef,
    transform,
    fit,
    zoomIn,
    zoomOut,
    pointerHandlers: {
      onPointerDown,
      onPointerMove,
      onPointerUp: finishPointer,
      onPointerCancel: finishPointer,
      onKeyDown,
    },
  };
}
