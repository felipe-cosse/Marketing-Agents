import { useEffect, useRef } from "react";

import "./unsaved-configuration.css";

interface UnsavedConfigurationDialogProps {
  readonly open: boolean;
  readonly destinationLabel: string;
  readonly onDiscard: () => void;
  readonly onKeepEditing: () => void;
}

export function UnsavedConfigurationDialog({
  open,
  destinationLabel,
  onDiscard,
  onKeepEditing,
}: UnsavedConfigurationDialogProps): React.JSX.Element | null {
  const keepButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    keepButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onKeepEditing();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onKeepEditing, open]);

  if (!open) return null;

  return (
    <div className="unsaved-configuration-backdrop">
      <section
        className="unsaved-configuration-dialog"
        role="alertdialog"
        aria-labelledby="unsaved-configuration-title"
        aria-describedby="unsaved-configuration-description"
      >
        <strong id="unsaved-configuration-title">
          Discard configuration changes?
        </strong>
        <p id="unsaved-configuration-description">
          Your unsaved deployment changes will be lost before you{" "}
          {destinationLabel}.
        </p>
        <div className="unsaved-configuration-dialog__actions">
          <button ref={keepButtonRef} type="button" onClick={onKeepEditing}>
            Keep editing
          </button>
          <button type="button" className="is-danger" onClick={onDiscard}>
            Discard changes
          </button>
        </div>
      </section>
    </div>
  );
}
