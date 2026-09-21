import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { InstanceLayout } from "./layout";
import type { AgentInstance } from "./model";
import { AgentCard } from "./AgentCard";
import type { InstanceRuntimeStatus } from "../../api/instanceStatusSummary";

const INSTANCE = {
  id: "inst.community.events.live-session-reminder.01",
  templateId: "tpl.community.events.live-session-reminder",
  displayName: "Live Session Reminder",
  purpose: "Drafts a reminder for review.",
  displayOrder: 10,
  enabled: true,
  operationClassification: "read_only",
  triggerTypes: ["manual"],
  capabilitySummaries: [],
  sourceOrdinal: 1,
  deploymentCount: 2,
} satisfies AgentInstance;

const PLACEMENT = {
  id: INSTANCE.id,
  templateId: INSTANCE.templateId,
  departmentId: "dept.community",
  functionId: "func.community.events",
  x: 0,
  y: 0,
  width: 104,
  height: 80,
} satisfies InstanceLayout;

describe("AgentCard", () => {
  it("ARCH-02 exposes stable graph hierarchy context without changing the accessible name", () => {
    const secondInstance = {
      ...INSTANCE,
      id: "inst.community.events.live-session-reminder.02",
      sourceOrdinal: 2,
    } satisfies AgentInstance;

    render(
      <>
        <AgentCard
          instance={INSTANCE}
          departmentLabel="Community"
          functionLabel="Events"
          placement={PLACEMENT}
          selected={false}
          onSelect={vi.fn()}
          tabIndex={0}
          onFocus={vi.fn()}
          onNavigate={vi.fn()}
        />
        <AgentCard
          instance={secondInstance}
          departmentLabel="Community"
          functionLabel="Events"
          placement={{ ...PLACEMENT, id: secondInstance.id }}
          selected={false}
          onSelect={vi.fn()}
          tabIndex={-1}
          onFocus={vi.fn()}
          onNavigate={vi.fn()}
        />
      </>,
    );

    const cards = screen.getAllByRole("button");
    expect(cards).toHaveLength(2);
    expect(cards[0]).toHaveAccessibleName(
      "Live Session Reminder, Instance 1 of 2. Drafts a reminder for review. Enabled deployment.",
    );
    expect(cards[1]).toHaveAccessibleName(
      "Live Session Reminder, Instance 2 of 2. Drafts a reminder for review. Enabled deployment.",
    );
    for (const card of cards) {
      expect(card).toHaveAccessibleDescription(
        "Department: Community. Function: Events. Hierarchy level 4. Latest run: Unavailable.",
      );
    }

    const descriptionIds = cards.map((card) =>
      card.getAttribute("aria-describedby"),
    );
    expect(descriptionIds).toEqual([
      "agent-card-hierarchy-inst.community.events.live-session-reminder.01",
      "agent-card-hierarchy-inst.community.events.live-session-reminder.02",
    ]);
    expect(new Set(descriptionIds).size).toBe(2);
    for (const descriptionId of descriptionIds) {
      expect(descriptionId).not.toBeNull();
      if (descriptionId !== null) {
        expect(document.getElementById(descriptionId)).toHaveClass("sr-only");
      }
    }
  });

  it("OBJ-06 keeps observed runtime separate from deployment and unknown data", () => {
    const runtimeStatus: InstanceRuntimeStatus = {
      instanceId: INSTANCE.id,
      status: "awaiting_approval",
      latestRunId: "run.obj-06.latest",
      latestRunState: "awaiting_approval",
      latestRunCreatedAt: "2026-09-21T10:00:00Z",
      latestRunUpdatedAt: "2026-09-21T10:01:00Z",
      instanceUrl: `/api/v1/agent-instances/${INSTANCE.id}`,
      latestRunUrl: "/api/v1/runs/run.obj-06.latest",
    };
    const props = {
      instance: { ...INSTANCE, enabled: false },
      departmentLabel: "Community",
      functionLabel: "Events",
      placement: PLACEMENT,
      selected: false,
      onSelect: vi.fn(),
      tabIndex: 0 as const,
      onFocus: vi.fn(),
      onNavigate: vi.fn(),
    };
    const { rerender } = render(
      <AgentCard {...props} runtimeStatus={runtimeStatus} />,
    );
    const card = screen.getByRole("button");
    expect(card).toHaveAccessibleName(/Disabled deployment\.$/u);
    expect(card).toHaveAccessibleDescription(
      /Latest run: Awaiting approval\.$/u,
    );
    expect(within(card).getByText("Disabled")).toBeVisible();
    expect(within(card).getByText("Run: Awaiting approval")).toHaveAttribute(
      "data-runtime-status",
      "awaiting_approval",
    );
    expect(card).toHaveStyle({ width: "104px", height: "80px" });

    rerender(<AgentCard {...props} />);
    expect(within(card).getByText("Run: Unavailable")).toHaveAttribute(
      "data-runtime-status",
      "unavailable",
    );
    expect(within(card).queryByText(/Never run/u)).not.toBeInTheDocument();
    expect(card).toHaveAccessibleDescription(/Latest run: Unavailable\.$/u);
  });
});
