import "@testing-library/jest-dom/vitest";

class TestResizeObserver implements ResizeObserver {
  readonly #callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.#callback = callback;
  }

  disconnect(): void {
    return undefined;
  }

  observe(target: Element): void {
    const measured = target.getBoundingClientRect();
    const contentRect =
      measured.width > 0 && measured.height > 0
        ? measured
        : new DOMRect(0, 0, 1536, 856);
    this.#callback(
      [
        {
          target,
          contentRect,
        } as ResizeObserverEntry,
      ],
      this,
    );
  }

  unobserve(): void {
    return undefined;
  }
}

globalThis.ResizeObserver = TestResizeObserver;

HTMLElement.prototype.setPointerCapture = () => undefined;
HTMLElement.prototype.releasePointerCapture = () => undefined;
HTMLElement.prototype.hasPointerCapture = () => false;
