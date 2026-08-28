import { useEffect, useRef } from "react";

import "./unsaved-configuration.css";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not(:disabled)",
  "input:not(:disabled)",
  "select:not(:disabled)",
  "textarea:not(:disabled)",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

interface UnsavedConfigurationDialogProps {
  readonly open: boolean;
  readonly destinationLabel: string;
  readonly changeKind?: "configuration" | "dry-run" | "multiple";
  readonly onDiscard: () => void;
  readonly onKeepEditing: () => void;
}

export function UnsavedConfigurationDialog({
  open,
  destinationLabel,
  changeKind = "configuration",
  onDiscard,
  onKeepEditing,
}: UnsavedConfigurationDialogProps): React.JSX.Element | null {
  const keepButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    const previouslyFocused =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    keepButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        event.preventDefault();
        onKeepEditing();
        return;
      }
      if (event.key !== "Tab") return;

      const dialog = dialogRef.current;
      if (dialog === null) return;
      const focusable = [
        ...dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ];
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable.at(-1);
      const active = document.activeElement;
      if (
        event.shiftKey &&
        (active === first ||
          !(active instanceof Node) ||
          !dialog.contains(active))
      ) {
        event.preventDefault();
        last?.focus();
      } else if (
        !event.shiftKey &&
        (active === last ||
          !(active instanceof Node) ||
          !dialog.contains(active))
      ) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    };
  }, [onKeepEditing, open]);

  if (!open) return null;

  const title =
    changeKind === "configuration"
      ? "Discard configuration changes?"
      : changeKind === "dry-run"
        ? "Discard dry-run input?"
        : "Discard unsaved changes?";
  const description =
    changeKind === "configuration"
      ? "Your unsaved deployment changes"
      : changeKind === "dry-run"
        ? "Your unsaved dry-run input"
        : "Your unsaved configuration and dry-run input";

  return (
    <div className="unsaved-configuration-backdrop">
      <section
        ref={dialogRef}
        className="unsaved-configuration-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="unsaved-configuration-title"
        aria-describedby="unsaved-configuration-description"
        tabIndex={-1}
      >
        <strong id="unsaved-configuration-title">{title}</strong>
        <p id="unsaved-configuration-description">
          {description} will be lost before you {destinationLabel}.
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
