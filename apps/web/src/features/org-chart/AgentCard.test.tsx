import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { InstanceLayout } from "./layout";
import type { AgentInstance } from "./model";
import { AgentCard } from "./AgentCard";

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
        "Department: Community. Function: Events. Hierarchy level 4.",
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
});
