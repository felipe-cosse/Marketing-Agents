import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createDemoScenarioRun,
  EMAIL_NEWSLETTER_INSTANCE_ID,
  EMAIL_NEWSLETTER_TEMPLATE_ID,
  EMAIL_ONBOARDING_INSTANCE_ID,
  EMAIL_ONBOARDING_TEMPLATE_ID,
  EMAIL_SIGNUP_SCENARIO_ID,
  fetchDemoScenarios,
  generateDemoScenarioIdempotencyKey,
  SOCIAL_DRAFT_INSTANCE_ID,
  SOCIAL_DRAFT_SCENARIO_ID,
  SOCIAL_DRAFT_TEMPLATE_ID,
  type DemoScenario,
  type DemoScenarioRunReceipt,
} from "../../api/demoScenarios";
import type * as DemoScenariosApi from "../../api/demoScenarios";
import { DemosPage } from "./DemosPage";

vi.mock("../../api/demoScenarios", async () => {
  const actual = await vi.importActual<typeof DemoScenariosApi>(
    "../../api/demoScenarios",
  );
  return {
    ...actual,
    createDemoScenarioRun: vi.fn(),
    fetchDemoScenarios: vi.fn(),
    generateDemoScenarioIdempotencyKey: vi.fn(),
  };
});

const fetchScenariosMock = vi.mocked(fetchDemoScenarios);
const createRunMock = vi.mocked(createDemoScenarioRun);
const generateKeyMock = vi.mocked(generateDemoScenarioIdempotencyKey);

const SOCIAL_PRESET = {
  idea: "Share how governed AI workflows turn a raw marketing idea into a reviewable draft.",
  audience: "Marketing and platform leaders",
  tone: "professional",
  key_points: [
    "Treat external content as untrusted data.",
    "Keep generation separate from publishing authority.",
    "Persist a traceable artifact for review.",
  ],
  source_urls: ["https://example.com/governed-ai"],
} as const;

const EMAIL_PRESET = {
  contact_id: "demo-contact-0001",
  name: "Avery Demo",
  email: "avery.demo@example.test",
  newsletter_list_ref: "list.demo.email.signup-onboarding.v1",
  consent: {
    granted: true,
    source: "demo_signup_form",
    captured_at: "2026-08-31T16:00:00Z",
  },
  signup_at: "2026-08-31T16:05:00Z",
  welcome_context:
    "Welcome the subscriber to governed AI updates for marketing teams.",
} as const;

const SOCIAL_SCENARIO: DemoScenario = {
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
  preset: SOCIAL_PRESET,
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

const EMAIL_SCENARIO: DemoScenario = {
  id: EMAIL_SIGNUP_SCENARIO_ID,
  version: 1,
  displayName: "Email signup onboarding",
  description:
    "Prepare approved mock newsletter and CRM onboarding actions, then create a welcome-message draft that is never sent.",
  workflowId: EMAIL_SIGNUP_SCENARIO_ID,
  effect: "mutating",
  mode: "deterministic_mock",
  selectedAgents: [
    {
      templateId: EMAIL_NEWSLETTER_TEMPLATE_ID,
      instanceId: EMAIL_NEWSLETTER_INSTANCE_ID,
    },
    {
      templateId: EMAIL_ONBOARDING_TEMPLATE_ID,
      instanceId: EMAIL_ONBOARDING_INSTANCE_ID,
    },
  ],
  inputSchema: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "schema.demo.email.signup-onboarding.input.v1",
    type: "object",
    additionalProperties: false,
    required: [
      "contact_id",
      "name",
      "email",
      "newsletter_list_ref",
      "consent",
      "signup_at",
      "welcome_context",
    ],
    properties: {
      contact_id: {
        type: "string",
        minLength: 1,
        maxLength: 200,
        pattern: "^demo-contact-[a-z0-9-]+$",
      },
      name: {
        type: "string",
        minLength: 1,
        maxLength: 120,
        "x-sensitive": true,
      },
      email: {
        type: "string",
        format: "email",
        minLength: 3,
        maxLength: 254,
        "x-sensitive": true,
      },
      newsletter_list_ref: {
        type: "string",
        const: "list.demo.email.signup-onboarding.v1",
      },
      consent: {
        type: "object",
        additionalProperties: false,
        required: ["granted", "source", "captured_at"],
        properties: {
          granted: { type: "boolean", const: true },
          source: { type: "string", const: "demo_signup_form" },
          captured_at: {
            type: "string",
            format: "date-time",
            maxLength: 40,
          },
        },
      },
      signup_at: { type: "string", format: "date-time", maxLength: 40 },
      welcome_context: { type: "string", minLength: 1, maxLength: 2_000 },
    },
  },
  preset: EMAIL_PRESET,
  safeSubmitVerb: "Propose onboarding actions",
  expected: {
    statePath: [
      "received",
      "validated",
      "planned",
      "awaiting_approval",
      "executing",
      "completed",
    ],
    modelCalls: 1,
    connectorCalls: 2,
    externalActions: 2,
    approvals: 2,
    externalWrites: 2,
  },
};

const EMAIL_RECEIPT: DemoScenarioRunReceipt = {
  status: "accepted",
  disposition: "created",
  scenarioId: EMAIL_SIGNUP_SCENARIO_ID,
  eventId: `manual-event-hmac-sha256-v1:${"e".repeat(64)}`,
  workId: "work.demo.email.01",
  runId: "run.demo.email.01",
  executionMode: "mock_execute",
  instanceUrl: `/api/v1/agent-instances/${EMAIL_NEWSLETTER_INSTANCE_ID}`,
  runUrl: "/api/v1/runs/run.demo.email.01",
  timelineUrl: "/api/v1/runs/run.demo.email.01/timeline",
  artifactsUrl: "/api/v1/runs/run.demo.email.01/artifacts",
};

function Providers({ children }: { readonly children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Number.POSITIVE_INFINITY },
    },
  });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/demos"]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function renderPage(): ReturnType<typeof render> {
  return render(
    <Providers>
      <DemosPage />
    </Providers>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  fetchScenariosMock.mockResolvedValue([EMAIL_SCENARIO, SOCIAL_SCENARIO]);
  generateKeyMock.mockReturnValue("demo-email-signup-retry-0001");
  createRunMock.mockResolvedValue(EMAIL_RECEIPT);
});

describe("DEMO-03 Email signup demo page", () => {
  it("DEMO-03 selects the exact Email preset and proposes only approval-gated mock actions", async () => {
    const user = userEvent.setup();
    renderPage();

    const emailSwitch = await screen.findByRole("button", {
      name: /Email signup onboarding/iu,
    });
    await user.click(emailSwitch);

    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "Email signup approval boundary",
      }),
    ).toBeVisible();
    expect(screen.getByText("2 exact approvals required")).toBeVisible();
    expect(
      screen.getAllByText("Both approvals before any call").length,
    ).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(EMAIL_NEWSLETTER_TEMPLATE_ID)).toBeVisible();
    expect(screen.getByText(EMAIL_NEWSLETTER_INSTANCE_ID)).toBeVisible();
    expect(screen.getByText(EMAIL_ONBOARDING_TEMPLATE_ID)).toBeVisible();
    expect(screen.getByText(EMAIL_ONBOARDING_INSTANCE_ID)).toBeVisible();
    expect(screen.getByText("awaiting_approval")).toBeVisible();

    const form = screen.getByRole("form", {
      name: "Email signup onboarding preset",
    });
    const name = within(form).getByRole("textbox", { name: /^name/iu });
    const email = within(form).getByRole("textbox", { name: /^email/iu });
    expect(name).toHaveValue(EMAIL_PRESET.name);
    expect(name).toHaveAttribute("autocomplete", "off");
    expect(name).toHaveAttribute("spellcheck", "false");
    expect(email).toHaveValue(EMAIL_PRESET.email);
    expect(email).toHaveAttribute("autocomplete", "off");
    expect(email).toHaveAttribute("spellcheck", "false");
    expect(name.getAttribute("aria-describedby")).toMatch(/sensitive/iu);
    expect(email.getAttribute("aria-describedby")).toMatch(/sensitive/iu);
    expect(within(form).getAllByText(/Sensitive value/iu)).toHaveLength(2);
    expect(
      within(form).getByRole("combobox", { name: /^newsletter list ref/iu }),
    ).toHaveValue(EMAIL_PRESET.newsletter_list_ref);
    expect(
      within(form).queryByRole("button", {
        name: /approve|subscribe|send|enroll/iu,
      }),
    ).not.toBeInTheDocument();

    await user.click(
      within(form).getByRole("button", {
        name: "Propose onboarding actions",
      }),
    );

    await waitFor(() => expect(createRunMock).toHaveBeenCalledTimes(1));
    expect(createRunMock).toHaveBeenCalledWith({
      scenarioId: EMAIL_SIGNUP_SCENARIO_ID,
      instanceId: EMAIL_NEWSLETTER_INSTANCE_ID,
      overrides: EMAIL_PRESET,
      idempotencyKey: "demo-email-signup-retry-0001",
      expectedExecutionMode: "mock_execute",
      signal: expect.any(AbortSignal),
    });
    expect(
      await screen.findByRole("heading", {
        name: "Approval-gated run accepted",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(/receipt does not prove zero calls or execution/iu),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: "Open approval queue" }),
    ).toHaveAttribute("href", "/approvals?run_id=run.demo.email.01");
  });

  it("DEMO-03 removes Email submission when its two-action approval contract drifts", async () => {
    fetchScenariosMock.mockResolvedValue([
      {
        ...EMAIL_SCENARIO,
        expected: { ...EMAIL_SCENARIO.expected, externalActions: 1 },
      },
      SOCIAL_SCENARIO,
    ]);
    renderPage();

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Social idea to draft artifact",
      }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /Email signup onboarding/iu }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Propose onboarding actions" }),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Create draft" }),
    ).toBeEnabled();
    expect(createRunMock).not.toHaveBeenCalled();
  });
});
