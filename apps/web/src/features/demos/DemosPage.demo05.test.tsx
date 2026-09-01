import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createDemoScenarioRun,
  fetchDemoScenarios,
  generateDemoScenarioIdempotencyKey,
  PARTNERSHIP_REVIEW_INSTANCE_ID,
  PARTNERSHIP_REVIEW_SCENARIO_ID,
  PARTNERSHIP_REVIEW_TEMPLATE_ID,
  SOCIAL_DRAFT_INSTANCE_ID,
  SOCIAL_DRAFT_SCENARIO_ID,
  SOCIAL_DRAFT_TEMPLATE_ID,
  type DemoScenario,
} from "../../api/demoScenarios";
import type * as DemoApi from "../../api/demoScenarios";
import {
  DemosPage,
  PARTNERSHIP_REVIEW_INPUT_SCHEMA,
  PARTNERSHIP_REVIEW_PRESET,
} from "./DemosPage";

vi.mock("../../api/demoScenarios", async () => {
  const actual = await vi.importActual<typeof DemoApi>(
    "../../api/demoScenarios",
  );
  return {
    ...actual,
    createDemoScenarioRun: vi.fn(),
    fetchDemoScenarios: vi.fn(),
    generateDemoScenarioIdempotencyKey: vi.fn(),
  };
});

const scenario: DemoScenario = {
  id: PARTNERSHIP_REVIEW_SCENARIO_ID,
  version: 1,
  displayName: "Partnership application review",
  description:
    "Create a deterministic advisory recommendation from supplied partner application evidence without external research, applicant notification, record mutation, or an automated decision.",
  workflowId: PARTNERSHIP_REVIEW_SCENARIO_ID,
  effect: "read_only",
  mode: "deterministic_mock",
  selectedAgents: [
    {
      templateId: PARTNERSHIP_REVIEW_TEMPLATE_ID,
      instanceId: PARTNERSHIP_REVIEW_INSTANCE_ID,
    },
  ],
  inputSchema: PARTNERSHIP_REVIEW_INPUT_SCHEMA,
  preset: PARTNERSHIP_REVIEW_PRESET,
  safeSubmitVerb: "Create advisory review",
  expected: {
    statePath: ["received", "validated", "planned", "executing", "completed"],
    modelCalls: 1,
    connectorCalls: 0,
    externalActions: 0,
    approvals: 0,
    externalWrites: 0,
  },
};

const socialScenario: DemoScenario = {
  id: SOCIAL_DRAFT_SCENARIO_ID,
  version: 1,
  displayName: "Social content draft",
  description:
    "Turn a supplied content idea into an inert, reviewable LinkedIn draft artifact.",
  workflowId: SOCIAL_DRAFT_SCENARIO_ID,
  effect: "read_only",
  mode: "deterministic_mock",
  selectedAgents: [
    {
      templateId: SOCIAL_DRAFT_TEMPLATE_ID,
      instanceId: SOCIAL_DRAFT_INSTANCE_ID,
    },
  ],
  inputSchema: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "schema.demo.social-media.content-draft.input.v1",
    type: "object",
    additionalProperties: false,
    required: ["idea", "audience", "tone", "key_points"],
    properties: {
      idea: { type: "string", minLength: 1, maxLength: 1_200 },
      audience: { type: "string", minLength: 1, maxLength: 160 },
      tone: {
        type: "string",
        enum: ["professional", "conversational", "educational", "bold"],
      },
      key_points: {
        type: "array",
        minItems: 1,
        maxItems: 6,
        items: { type: "string", minLength: 1, maxLength: 250 },
      },
      call_to_action: { type: "string", minLength: 1, maxLength: 250 },
      source_urls: {
        type: "array",
        maxItems: 5,
        items: { type: "string", minLength: 1, maxLength: 2_048 },
      },
    },
  },
  preset: {
    idea: "Share how governed AI workflows turn a raw marketing idea into a reviewable draft.",
    audience: "Marketing and platform leaders",
    tone: "professional",
    key_points: [
      "Treat external content as untrusted data.",
      "Keep generation separate from publishing authority.",
      "Persist a traceable artifact for review.",
    ],
    source_urls: ["https://example.com/governed-ai"],
  },
  safeSubmitVerb: "Create draft",
  expected: {
    statePath: ["received", "validated", "planned", "executing", "completed"],
    modelCalls: 1,
    connectorCalls: 0,
    externalActions: 0,
    approvals: 0,
    externalWrites: 0,
  },
};

function renderPage(): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/demos"]}>
        <DemosPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(fetchDemoScenarios).mockResolvedValue([socialScenario, scenario]);
  vi.mocked(generateDemoScenarioIdempotencyKey).mockReturnValue(
    "demo-partnership-review-0001",
  );
  vi.mocked(createDemoScenarioRun).mockResolvedValue({
    status: "accepted",
    disposition: "created",
    scenarioId: PARTNERSHIP_REVIEW_SCENARIO_ID,
    eventId: `manual-event-hmac-sha256-v1:${"a".repeat(64)}`,
    workId: "work.demo.partnership.01",
    runId: "run.demo.partnership.01",
    executionMode: "dry_run",
    instanceUrl: `/api/v1/agent-instances/${PARTNERSHIP_REVIEW_INSTANCE_ID}`,
    runUrl: "/api/v1/runs/run.demo.partnership.01",
    timelineUrl: "/api/v1/runs/run.demo.partnership.01/timeline",
    artifactsUrl: "/api/v1/runs/run.demo.partnership.01/artifacts",
  });
});

describe("DEMO-05 partnership application review", () => {
  it("DEMO-05 renders and submits only an advisory review of supplied evidence", async () => {
    const user = userEvent.setup();
    renderPage();
    const socialButton = await screen.findByRole("button", {
      name: /Social content draft/iu,
    });
    expect(socialButton).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "Social idea to draft artifact",
      }),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: /Partnership application review/iu }),
    );
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Partner application to advisory review",
      }),
    ).toBeVisible();
    expect(screen.getByText("Advisory only")).toBeVisible();
    expect(screen.getByText("No automated decision")).toBeVisible();
    expect(
      screen.getByText("No external research or notification"),
    ).toBeVisible();
    expect(screen.getByText(PARTNERSHIP_REVIEW_TEMPLATE_ID)).toBeVisible();
    expect(screen.getByText(PARTNERSHIP_REVIEW_INSTANCE_ID)).toBeVisible();
    const form = screen.getByRole("form", {
      name: "Partnership application review preset",
    });
    expect(
      within(form).queryByRole("button", {
        name: /accept|reject|notify|research/iu,
      }),
    ).not.toBeInTheDocument();
    const organizationName = within(form).getByRole("textbox", {
      name: /^organization name/iu,
    });
    expect(organizationName).toHaveAttribute("autocomplete", "off");
    expect(organizationName).toHaveAttribute("spellcheck", "false");
    const descriptions = within(form).getAllByRole("textbox", {
      name: /^description/iu,
    });
    const lowerField = descriptions.at(-1);
    expect(lowerField).toBeDefined();
    if (lowerField === undefined)
      throw new Error("Expected lower description field");
    lowerField.focus();
    expect(lowerField).toHaveFocus();
    await user.click(
      within(form).getByRole("button", { name: "Create advisory review" }),
    );
    expect(createDemoScenarioRun).toHaveBeenCalledWith(
      expect.objectContaining({
        scenarioId: PARTNERSHIP_REVIEW_SCENARIO_ID,
        instanceId: PARTNERSHIP_REVIEW_INSTANCE_ID,
        overrides: PARTNERSHIP_REVIEW_PRESET,
        expectedExecutionMode: "dry_run",
      }),
    );
    expect(
      await screen.findByRole("heading", {
        name: "Advisory review run accepted",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(/acceptance is not an applicant decision/iu),
    ).toBeVisible();
    expect(
      screen.queryByRole("link", { name: "Open approval queue" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open accepted run" }),
    ).toHaveAttribute("href", "/runs/run.demo.partnership.01");
    expect(screen.getByRole("link", { name: "Open timeline" })).toHaveAttribute(
      "href",
      "/runs/run.demo.partnership.01#timeline-title",
    );
    expect(
      screen.getByRole("link", { name: "Open artifacts" }),
    ).toHaveAttribute(
      "href",
      "/runs/run.demo.partnership.01#run-artifacts-title",
    );
  });

  it("DEMO-05 fails closed when advisory zero-action contract drifts", async () => {
    vi.mocked(fetchDemoScenarios).mockResolvedValue([
      { ...scenario, expected: { ...scenario.expected, externalActions: 1 } },
    ]);
    renderPage();
    expect(await screen.findByText(/unavailable/iu)).toBeVisible();
    expect(
      screen.queryByRole("button", {
        name: /partnership application review/iu,
      }),
    ).not.toBeInTheDocument();
    expect(createDemoScenarioRun).not.toHaveBeenCalled();
  });
});
