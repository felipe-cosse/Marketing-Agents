import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AgentInstanceDetail } from "../../api/agentInstanceDetail";
import type { AgentInstance } from "../org-chart/model";
import { AgentInspector } from "./AgentInspector";

const SUMMARY: AgentInstance = {
  id: "inst.email.newsletter.newsletter-subscriber.01",
  templateId: "tpl.email.newsletter.newsletter-subscriber",
  displayName: "Newsletter Subscriber",
  purpose:
    "Add new signups from website forms to the configured newsletter system.",
  displayOrder: 10,
  enabled: true,
  operationClassification: "mutating",
  triggerTypes: ["manual", "webhook", "schedule"],
  capabilitySummaries: [
    {
      id: "cap.newsletter.subscribe",
      displayName: "Newsletter: Subscribe",
      connectorFamily: "newsletter",
      effect: "write",
    },
  ],
  sourceOrdinal: 1,
  deploymentCount: 1,
};

function detail(
  runtime: "dynamic" | "never-run" | "unavailable" = "dynamic",
): AgentInstanceDetail {
  const common = {
    etag: `"${"a".repeat(64)}"`,
    catalogVersion: "1.0.0",
    catalogHash: `catalog-sha256-v1:${"b".repeat(64)}`,
    instance: {
      id: SUMMARY.id,
      templateId: SUMMARY.templateId,
      displayOrder: 10,
      enabled: true,
      sourceOrdinal: 1,
      variantLabel: "Primary local deployment",
      triggerBindings: [
        {
          type: "webhook",
          enabled: true,
          eventSource: "Form submission received",
          cron: null,
          timezone: null,
          misfirePolicy: null,
          misfireGraceSeconds: null,
        },
        {
          type: "schedule",
          enabled: false,
          eventSource: null,
          cron: "0 9 * * 1",
          timezone: "UTC",
          misfirePolicy: "run_once",
          misfireGraceSeconds: 300,
        },
      ],
      connectorBindings: {
        newsletter: {
          connectorFamily: "newsletter",
          bindingId: "mock.newsletter.local",
          enabled: true,
        },
      },
      schedule: {
        cron: "0 9 * * 1",
        timezone: "UTC",
        misfirePolicy: "run_once",
        misfireGraceSeconds: 300,
      },
      configurationRevision: 4,
      configurationEtag: '"instance-configuration-v1-4"',
    },
    template: {
      id: SUMMARY.templateId,
      displayName: SUMMARY.displayName,
      departmentId: "dept.email",
      functionId: "func.email.newsletter",
      displayOrder: 10,
      purpose: SUMMARY.purpose,
      inputSchemaId: "schema.email.newsletter-subscriber.input.v1",
      outputSchemaId: "schema.email.newsletter-subscriber.output.v1",
      allowedToolCapabilityIds: ["cap.newsletter.subscribe"],
      supportedTriggerTypes: ["manual", "webhook", "schedule"],
      operationClassification: "mutating",
      outputHandling: "standard",
      approvalPolicyId: "policy.human-external-write",
      retryPolicy: { maxAttempts: 2, backoff: "bounded_exponential" },
      timeoutPolicy: { stepSeconds: 30, runSeconds: 180 },
      budgetPolicy: {
        maxSteps: 5,
        maxModelCalls: 2,
        maxToolCalls: 3,
        maxInputBytes: 65_536,
        maxInputFieldBytes: 16_384,
        maxOutputBytes: 131_072,
        maxModelOutputTokens: 2_048,
      },
      rateLimitPolicy: { maxCalls: 10, windowSeconds: 60 },
      sourceConfidence: "high",
      sourceReferences: ["catalog/marketing-agents.yaml"],
      implementationNotes: "Uses only registered local connector bindings.",
    },
    sharedTemplateDeploymentCount: 1,
    capabilities: [
      {
        id: "cap.newsletter.subscribe",
        displayName: "Newsletter: Subscribe",
        description:
          "Create or update a subscriber in the configured newsletter system.",
        effect: "write",
        connectorFamily: "newsletter",
        idempotencySupport: "required",
        defaultTimeoutSeconds: 20,
        dataClassification: "personal",
      },
    ],
    approvalPolicy: {
      id: "policy.human-external-write",
      kind: "human_external_write",
      requiredRoles: ["approver"],
      expirySeconds: 3_600,
      allowSelfApproval: false,
    },
    inputSchema: {
      $id: "schema.email.newsletter-subscriber.input.v1",
      type: "object",
      properties: {
        email: { type: "string", example: "<script>unsafe()</script>" },
      },
    },
    outputSchema: {
      $id: "schema.email.newsletter-subscriber.output.v1",
      type: "object",
      properties: { accepted: { type: "boolean" } },
    },
    templateSourceReferences: [
      "catalog/marketing-agents.yaml#newsletter-subscriber",
    ],
    templateImplementationNotes:
      "Mock-safe external write with approval enforcement.",
    configurationSchema: `/api/v1/agent-instances/${SUMMARY.id}/configuration-schema`,
  };

  if (runtime === "unavailable") {
    return {
      ...common,
      runtimeAvailable: false,
      runtimeWatermark: null,
      runtimeStatus: null,
      recentRuns: [],
    } as unknown as AgentInstanceDetail;
  }

  const neverRun = runtime === "never-run";
  return {
    ...common,
    runtimeAvailable: true,
    runtimeWatermark: `instance-status-sha256-v1:${"c".repeat(64)}`,
    runtimeStatus: {
      status: neverRun ? "never_run" : "completed",
      latestRunId: neverRun ? null : "run.web-03.01",
      latestRunState: neverRun ? null : "completed",
      latestRunCreatedAt: neverRun ? null : "2026-08-28T18:00:00Z",
      latestRunUpdatedAt: neverRun ? null : "2026-08-28T18:01:00Z",
      latestRunUrl: neverRun ? null : "/api/v1/runs/run.web-03.01",
    },
    recentRuns: neverRun
      ? []
      : [
          {
            id: "run.web-03.01",
            state: "completed",
            workflowId: "workflow.email.newsletter-subscription",
            createdAt: "2026-08-28T18:00:00Z",
            updatedAt: "2026-08-28T18:01:00Z",
            runUrl: "/api/v1/runs/run.web-03.01",
          },
        ],
  } as unknown as AgentInstanceDetail;
}

function renderInspector(
  overrides: Partial<React.ComponentProps<typeof AgentInspector>> = {},
): ReturnType<typeof render> {
  return render(
    <AgentInspector
      summary={SUMMARY}
      departmentName="Email"
      functionName="Newsletter"
      detail={detail()}
      isPending={false}
      error={null}
      onRetry={vi.fn()}
      onClose={vi.fn()}
      onOpenRun={vi.fn()}
      {...overrides}
    />,
  );
}

describe("WEB-03 AgentInspector", () => {
  it("keeps the hierarchy identity visible while details load and fail", async () => {
    const retry = vi.fn();
    const { rerender } = renderInspector({
      detail: undefined,
      isPending: true,
      onRetry: retry,
    });

    expect(
      screen.getByRole("heading", { name: "Newsletter Subscriber" }),
    ).toBeVisible();
    expect(screen.getByText(SUMMARY.id)).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading agent details",
    );

    rerender(
      <AgentInspector
        summary={SUMMARY}
        departmentName="Email"
        functionName="Newsletter"
        detail={undefined}
        isPending={false}
        error={new Error("The local detail API is unavailable.")}
        onRetry={retry}
        onClose={vi.fn()}
        onOpenRun={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The local detail API is unavailable.",
    );
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("renders every read-only metadata section and the owned configuration slot", async () => {
    renderInspector({
      configurationControls: (
        <form aria-label="Deployment configuration editor">
          <label>
            Variant label
            <input defaultValue="Primary local deployment" />
          </label>
        </form>
      ),
    });

    for (const heading of [
      "Overview",
      "Deployment & configuration",
      "Template",
      "Capabilities & policies",
      "Schemas",
      "Recent runs",
    ]) {
      expect(screen.getByRole("heading", { name: heading })).toBeVisible();
    }
    expect(screen.getByText("Form submission received")).toBeVisible();
    expect(screen.getByText("mock.newsletter.local · Enabled")).toBeVisible();
    expect(screen.getByText("0 9 * * 1", { selector: "dd" })).toBeVisible();
    expect(screen.getByText("Newsletter: Subscribe")).toBeVisible();
    expect(screen.getByText("Human external write")).toBeVisible();
    const recentRunLink = screen.getByRole("link", { name: "run.web-03.01" });
    expect(recentRunLink).toHaveAttribute("href", "/runs/run.web-03.01");
    expect(recentRunLink.parentElement).toHaveTextContent(
      "run.web-03.01 · workflow.email.newsletter-subscription",
    );

    const editor = screen.getByRole("form", {
      name: "Deployment configuration editor",
    });
    const deploymentSection = screen
      .getByRole("heading", { name: "Deployment & configuration" })
      .closest("section");
    if (deploymentSection === null)
      throw new Error("deployment section missing");
    expect(within(deploymentSection).getByRole("form")).toBe(editor);

    await userEvent.setup().click(screen.getByText("Input schema JSON"));
    expect(screen.getByText(/<script>unsafe\(\)<\/script>/u)).toBeVisible();
    expect(document.querySelector("script")).toBeNull();
    expect(screen.queryByText("Run with mocks")).toBeNull();
    expect(screen.queryByText("View all")).toBeNull();
    expect(screen.getAllByRole("link")).toEqual([recentRunLink]);
  });

  it("distinguishes unavailable runtime data from a confirmed never-run state", () => {
    const { rerender } = renderInspector({ detail: detail("unavailable") });
    expect(
      screen.getByText(
        "Recent run data is unavailable for this local runtime.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByText("No runs have been recorded for this agent."),
    ).toBeNull();
    expect(screen.queryByText("Never run")).toBeNull();

    rerender(
      <AgentInspector
        summary={SUMMARY}
        departmentName="Email"
        functionName="Newsletter"
        detail={detail("never-run")}
        isPending={false}
        error={null}
        onRetry={vi.fn()}
        onClose={vi.fn()}
        onOpenRun={vi.fn()}
      />,
    );
    expect(
      screen.getByText("No runs have been recorded for this agent."),
    ).toBeVisible();
    expect(screen.getByText("Never run")).toBeVisible();
    expect(
      screen.queryByText(
        "Recent run data is unavailable for this local runtime.",
      ),
    ).toBeNull();
  });

  it("includes the duplicate ordinal in the title", () => {
    const duplicateSummary: AgentInstance = {
      ...SUMMARY,
      id: "inst.community.education.course-cohort-onboarder.02",
      templateId: "tpl.community.education.course-cohort-onboarder",
      displayName: "Course Cohort Onboarder",
      sourceOrdinal: 2,
      deploymentCount: 2,
    };
    renderInspector({
      summary: duplicateSummary,
      detail: undefined,
      isPending: true,
    });
    expect(
      screen.getByRole("heading", {
        name: "Course Cohort Onboarder · Instance 2 of 2",
      }),
    ).toBeVisible();
  });

  it("keeps recent runs deep-linkable and delegates same-tab navigation", async () => {
    const openRun = vi.fn();
    renderInspector({ onOpenRun: openRun });

    const link = screen.getByRole("link", { name: "run.web-03.01" });
    expect(link).toHaveAttribute("href", "/runs/run.web-03.01");
    await userEvent.setup().click(link);

    expect(openRun).toHaveBeenCalledOnce();
    expect(openRun).toHaveBeenCalledWith("run.web-03.01");
  });

  it("closes from its button and Escape without exposing inert actions", async () => {
    const user = userEvent.setup();
    const close = vi.fn();
    renderInspector({ onClose: close });

    const closeButton = screen.getByRole("button", {
      name: "Close details for Newsletter Subscriber",
    });
    closeButton.focus();
    await user.keyboard("{Escape}");
    expect(close).toHaveBeenCalledOnce();

    await user.click(closeButton);
    expect(close).toHaveBeenCalledTimes(2);
  });
});
