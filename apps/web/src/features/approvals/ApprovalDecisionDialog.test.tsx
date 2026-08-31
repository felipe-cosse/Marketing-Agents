import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApprovalDecisionKind } from "../../api/approvals";
import { ApprovalDecisionDialog } from "./ApprovalDecisionDialog";
import { ApprovalReviewPanel } from "./ApprovalReviewPanel";
import { makeApprovalDetail } from "./approvalTestFixtures";

afterEach(() => {
  vi.useRealTimers();
});

function DialogHarness({
  decision = "approve",
}: {
  readonly decision?: ApprovalDecisionKind;
}): React.JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Review decision
      </button>
      {open ? (
        <ApprovalDecisionDialog
          approval={makeApprovalDetail()}
          decision={decision}
          pending={false}
          onCancel={() => setOpen(false)}
          onConfirm={vi.fn()}
        />
      ) : null}
    </>
  );
}

describe("WEB-05 ApprovalDecisionDialog", () => {
  it("focuses Cancel first, traps focus, closes on Escape, and restores its opener", async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);
    const opener = screen.getByRole("button", { name: "Review decision" });
    await user.click(opener);

    const dialog = screen.getByRole("dialog", {
      name: "Approve exact action?",
    });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel).toHaveFocus();

    const focusable = [
      ...dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ),
    ];
    focusable.at(-1)?.focus();
    await user.keyboard("{Tab}");
    expect(focusable[0]).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "Approve exact action?" }),
    ).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("repeats immutable safe action detail and renders payload text without HTML", async () => {
    const user = userEvent.setup();
    render(<DialogHarness decision="reject" />);
    await user.click(screen.getByRole("button", { name: "Review decision" }));

    const dialog = screen.getByRole("dialog", {
      name: "Reject exact action?",
    });
    expect(dialog).toHaveTextContent("newsletter.subscribe");
    expect(dialog).toHaveTextContent("Mock newsletter · Demo subscribers");
    expect(dialog).toHaveTextContent("a".repeat(64));
    expect(dialog).toHaveTextContent("run.web05.email");
    expect(dialog).toHaveTextContent("step.web05.newsletter");
    expect(dialog).toHaveTextContent("Unused");
    expect(screen.getByLabelText("Redacted payload JSON")).toHaveTextContent(
      '<img src=x onerror=\\"steal()\\">',
    );
    expect(dialog.querySelector("img")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Reject exact action" }),
    ).toBeEnabled();
  });

  it("disables every lifecycle state that the server reports as nonactionable and explains why", () => {
    const onRequestDecision = vi.fn();
    const cases = [
      {
        approval: makeApprovalDetail({ status: "expired" }),
        reason: /expired and can no longer be decided/iu,
      },
      {
        approval: makeApprovalDetail({ status: "approved" }),
        reason: /already recorded as approved/iu,
      },
      {
        approval: makeApprovalDetail({ status: "rejected" }),
        reason: /already recorded as rejected/iu,
      },
      {
        approval: makeApprovalDetail({ status: "consumed" }),
        reason: /already been consumed/iu,
      },
      {
        approval: makeApprovalDetail({ status: "superseded" }),
        reason: /superseded by a replacement request/iu,
      },
      {
        approval: makeApprovalDetail({ isActionable: false }),
        reason: /server reports that this approval is not actionable/iu,
      },
    ] as const;

    for (const value of cases) {
      const rendered = render(
        <ApprovalReviewPanel
          approval={value.approval}
          department={undefined}
          emailRunSafety={null}
          onClose={vi.fn()}
          onRequestDecision={onRequestDecision}
        />,
      );
      expect(screen.getByText(value.reason)).toBeVisible();
      expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
      rendered.unmount();
    }
    expect(onRequestDecision).not.toHaveBeenCalled();
  });

  it("disables a pending decision when the local clock reaches its authoritative expiry", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2099-08-31T21:59:59Z"));
    const onRequestDecision = vi.fn();
    render(
      <ApprovalReviewPanel
        approval={makeApprovalDetail({
          expiresAt: "2099-08-31T22:00:00Z",
        })}
        department={undefined}
        emailRunSafety={null}
        onClose={vi.fn()}
        onRequestDecision={onRequestDecision}
      />,
    );

    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();

    vi.setSystemTime(new Date("2099-08-31T22:00:00Z"));
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onRequestDecision).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_001);
    });

    expect(
      screen.getByText(/expired and can no longer be decided/iu),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(onRequestDecision).not.toHaveBeenCalled();
  });

  it("blocks confirmation when an already-open dialog reaches expiry", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2099-08-31T21:59:59Z"));
    const onConfirm = vi.fn();
    render(
      <ApprovalDecisionDialog
        approval={makeApprovalDetail({
          expiresAt: "2099-08-31T22:00:00Z",
        })}
        decision="approve"
        pending={false}
        onCancel={vi.fn()}
        onConfirm={onConfirm}
      />,
    );
    const confirm = screen.getByRole("button", {
      name: "Approve exact action",
    });
    expect(confirm).toBeEnabled();

    vi.setSystemTime(new Date("2099-08-31T22:00:00Z"));
    fireEvent.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_001);
    });
    expect(confirm).toBeDisabled();
    expect(screen.getByText(/close this dialog and refresh/iu)).toBeVisible();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
