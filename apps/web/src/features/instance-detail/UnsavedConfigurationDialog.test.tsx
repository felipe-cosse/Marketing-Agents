import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { UnsavedConfigurationDialog } from "./UnsavedConfigurationDialog";

function Harness(): React.JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Review navigation
      </button>
      <button type="button">Background action</button>
      <UnsavedConfigurationDialog
        open={open}
        destinationLabel="open another agent"
        changeKind="dry-run"
        onDiscard={() => setOpen(false)}
        onKeepEditing={() => setOpen(false)}
      />
    </>
  );
}

describe("WEB-04 UnsavedConfigurationDialog", () => {
  it("contains keyboard focus and restores it when the modal closes", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Review navigation" });

    await user.click(opener);
    const dialog = screen.getByRole("alertdialog", {
      name: "Discard dry-run input?",
    });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    const keep = within(dialog).getByRole("button", { name: "Keep editing" });
    const discard = within(dialog).getByRole("button", {
      name: "Discard changes",
    });
    expect(keep).toHaveFocus();

    await user.tab();
    expect(discard).toHaveFocus();
    await user.tab();
    expect(keep).toHaveFocus();
    await user.tab({ shift: true });
    expect(discard).toHaveFocus();

    await user.click(keep);
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(opener).toHaveFocus();
  });
});
