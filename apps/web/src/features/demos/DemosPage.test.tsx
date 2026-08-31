import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createDemoScenarioRun,
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

const PRESET = {
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
      call_to_action: {
        type: "string",
        minLength: 1,
        maxLength: 250,
      },
      source_urls: {
        type: "array",
        maxItems: 5,
        items: { type: "string", minLength: 1, maxLength: 2_048 },
      },
    },
  },
  preset: PRESET,
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

const RECEIPT: DemoScenarioRunReceipt = {
  status: "accepted",
  disposition: "created",
  scenarioId: SOCIAL_DRAFT_SCENARIO_ID,
  eventId: `manual-event-hmac-sha256-v1:${"d".repeat(64)}`,
  workId: "work.demo.social.01",
  runId: "run.demo.social.01",
  executionMode: "dry_run",
  instanceUrl: `/api/v1/agent-instances/${SOCIAL_DRAFT_INSTANCE_ID}`,
  runUrl: "/api/v1/runs/run.demo.social.01",
  timelineUrl: "/api/v1/runs/run.demo.social.01/timeline",
  artifactsUrl: "/api/v1/runs/run.demo.social.01/artifacts",
};

function Providers({
  children,
}: {
  readonly children: ReactNode;
}): React.JSX.Element {
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
  fetchScenariosMock.mockResolvedValue([SOCIAL_SCENARIO]);
  generateKeyMock.mockReturnValue("demo-social-draft-retry-0001");
  createRunMock.mockResolvedValue(RECEIPT);
});

describe("DEMO-01 Social demo page", () => {
  it("DEMO-01 presents the API-declared preset and exact safe execution boundary", async () => {
    renderPage();

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Social idea to draft artifact",
      }),
    ).toBeVisible();
    expect(
      screen.getAllByText(/Deterministic mock mode/i).length,
    ).toBeGreaterThan(0);
    expect(await screen.findByText("Read-only")).toBeVisible();
    expect(screen.getByText("0 external writes")).toBeVisible();
    expect(screen.getByText("No approval required")).toBeVisible();
    expect(screen.getByText(SOCIAL_DRAFT_TEMPLATE_ID)).toBeVisible();
    expect(screen.getByText(SOCIAL_DRAFT_INSTANCE_ID)).toBeVisible();
    expect(screen.getByRole("textbox", { name: /^idea/i })).toHaveValue(
      PRESET.idea,
    );
    expect(screen.getByRole("textbox", { name: /^audience/i })).toHaveValue(
      PRESET.audience,
    );
    expect(screen.getByRole("combobox", { name: /^tone/i })).toHaveValue(
      PRESET.tone,
    );
    expect(screen.getByRole("button", { name: "Create draft" })).toBeEnabled();
    expect(document.body).not.toHaveTextContent(/publish/i);
    expect(createRunMock).not.toHaveBeenCalled();
  });

  it("DEMO-01 submits the complete validated preset and exposes accepted run, timeline, and artifact links", async () => {
    const user = userEvent.setup();
    renderPage();
    const idea = await screen.findByRole("textbox", { name: /^idea/i });
    await user.clear(idea);
    await user.type(
      idea,
      "Explain why durable AI drafts are easier to review.",
    );

    await user.click(screen.getByRole("button", { name: "Create draft" }));

    await waitFor(() => expect(createRunMock).toHaveBeenCalledTimes(1));
    expect(createRunMock).toHaveBeenCalledWith({
      scenarioId: SOCIAL_DRAFT_SCENARIO_ID,
      instanceId: SOCIAL_DRAFT_INSTANCE_ID,
      overrides: {
        ...PRESET,
        idea: "Explain why durable AI drafts are easier to review.",
      },
      idempotencyKey: "demo-social-draft-retry-0001",
      signal: expect.any(AbortSignal),
    });
    expect(
      await screen.findByRole("heading", { name: "Draft run accepted" }),
    ).toBeVisible();
    expect(screen.getByText(RECEIPT.workId)).toBeVisible();
    expect(screen.getByText(RECEIPT.runId)).toBeVisible();
    expect(
      screen.getByRole("link", { name: "Open accepted run" }),
    ).toHaveAttribute("href", `/runs/${RECEIPT.runId}`);
    expect(screen.getByRole("link", { name: "Open timeline" })).toHaveAttribute(
      "href",
      `/runs/${RECEIPT.runId}#timeline-title`,
    );
    expect(
      screen.getByRole("link", { name: "Open artifacts" }),
    ).toHaveAttribute("href", `/runs/${RECEIPT.runId}#run-artifacts-title`);
  });

  it.each([
    [
      "a changed external-write contract",
      {
        ...SOCIAL_SCENARIO,
        expected: { ...SOCIAL_SCENARIO.expected, externalWrites: 1 },
      },
    ],
    [
      "an invalid safe preset",
      {
        ...SOCIAL_SCENARIO,
        preset: { ...PRESET, idea: "" },
      },
    ],
    [
      "an unsupported input schema",
      {
        ...SOCIAL_SCENARIO,
        inputSchema: { ...SOCIAL_SCENARIO.inputSchema, oneOf: [] },
      },
    ],
  ])("DEMO-01 fails closed for %s", async (_label, scenario) => {
    fetchScenariosMock.mockResolvedValue([scenario]);
    renderPage();

    expect(await screen.findByText("Demo unavailable")).toBeVisible();
    expect(screen.getByText(/Nothing can be submitted/)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Create draft" }),
    ).not.toBeInTheDocument();
    expect(createRunMock).not.toHaveBeenCalled();
  });
});
