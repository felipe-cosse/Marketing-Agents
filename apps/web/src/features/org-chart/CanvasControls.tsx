import { FitIcon, MinusIcon, PlusIcon } from "./icons";

interface CanvasControlsProps {
  readonly zoom: number;
  readonly onZoomIn: () => void;
  readonly onZoomOut: () => void;
  readonly onFit: () => void;
}

export function CanvasControls({
  zoom,
  onZoomIn,
  onZoomOut,
  onFit,
}: CanvasControlsProps): React.JSX.Element {
  return (
    <div className="canvas-controls" aria-label="Chart viewport controls">
      <button
        type="button"
        className="icon-button"
        aria-label="Zoom out"
        onClick={onZoomOut}
      >
        <MinusIcon />
      </button>
      <output
        className="zoom-readout"
        aria-label="Current zoom"
        data-testid="zoom-readout"
      >
        {String(Math.round(zoom * 100))}%
      </output>
      <button
        type="button"
        className="icon-button"
        aria-label="Zoom in"
        onClick={onZoomIn}
      >
        <PlusIcon />
      </button>
      <span className="control-divider" aria-hidden="true" />
      <button type="button" className="fit-button" onClick={onFit}>
        <FitIcon />
        Fit hierarchy
      </button>
    </div>
  );
}
