import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  COMMUNITY_REMINDER_INSTANCE_ID,
  COMMUNITY_REMINDER_SCENARIO_ID,
  COMMUNITY_REMINDER_TEMPLATE_ID,
  createDemoScenarioRun,
  fetchDemoScenarios,
  generateDemoScenarioIdempotencyKey,
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
  event_id: "event.community-live-session.2026-09-17",
  event_name: "Marketing operators live session",
  signup_event_id: "signup.community-demo-0001",
  admitted_source: "fixture.community-signup",
  signup_at: "2026-09-01T16:30:00Z",
  session_local_start: "2026-09-17T09:00:00",
  session_timezone: "America/Los_Angeles",
  reminder_offset_minutes: 1_440,
  attendee_display_name: "Demo Attendee",
  channel_label: "email",
  event_details:
    "A live session on governed marketing automation and approval-safe workflows.",
} as const;

const SCENARIO: DemoScenario = {
  id: COMMUNITY_REMINDER_SCENARIO_ID,
  version: 1,
  displayName: "Community reminder draft",
  description:
    "Create a deterministic reminder draft and recommended UTC time from supplied event signup details without scheduling or sending.",
  workflowId: COMMUNITY_REMINDER_SCENARIO_ID,
  effect: "read_only",
  mode: "deterministic_mock",
  selectedAgents: [
    {
      templateId: COMMUNITY_REMINDER_TEMPLATE_ID,
      instanceId: COMMUNITY_REMINDER_INSTANCE_ID,
    },
  ],
  inputSchema: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "schema.demo.community.reminder-draft.input.v1",
    type: "object",
    additionalProperties: false,
    required: [
      "event_id",
      "event_name",
      "signup_event_id",
      "admitted_source",
      "signup_at",
      "session_local_start",
      "session_timezone",
      "reminder_offset_minutes",
      "attendee_display_name",
      "channel_label",
      "event_details",
    ],
    properties: {
      event_id: {
        type: "string",
        minLength: 1,
        maxLength: 120,
        pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$",
      },
      event_name: { type: "string", minLength: 1, maxLength: 160 },
      signup_event_id: {
        type: "string",
        minLength: 1,
        maxLength: 120,
        pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$",
      },
      admitted_source: {
        type: "string",
        minLength: 1,
        maxLength: 80,
        pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$",
      },
      signup_at: { type: "string", format: "date-time", maxLength: 40 },
      session_local_start: {
        type: "string",
        pattern: "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$",
        maxLength: 19,
      },
      session_timezone: {
        type: "string",
        minLength: 1,
        maxLength: 64,
        pattern: "^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$",
      },
      reminder_offset_minutes: {
        type: "integer",
        minimum: 1,
        maximum: 10_080,
      },
      attendee_display_name: {
        type: "string",
        minLength: 1,
        maxLength: 120,
        "x-sensitive": true,
      },
      channel_label: {
        type: "string",
        enum: ["email", "community", "in_app"],
      },
      event_details: { type: "string", minLength: 1, maxLength: 2_000 },
    },
  },
  preset: PRESET,
  safeSubmitVerb: "Create reminder draft",
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
  scenarioId: COMMUNITY_REMINDER_SCENARIO_ID,
  eventId: `manual-event-hmac-sha256-v1:${"f".repeat(64)}`,
  workId: "work.demo.community.01",
  runId: "run.demo.community.01",
  executionMode: "dry_run",
  instanceUrl: `/api/v1/agent-instances/${COMMUNITY_REMINDER_INSTANCE_ID}`,
  runUrl: "/api/v1/runs/run.demo.community.01",
  timelineUrl: "/api/v1/runs/run.demo.community.01/timeline",
  artifactsUrl: "/api/v1/runs/run.demo.community.01/artifacts",
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
  fetchScenariosMock.mockResolvedValue([SCENARIO]);
  generateKeyMock.mockReturnValue("demo-community-reminder-retry-0001");
  createRunMock.mockResolvedValue(RECEIPT);
});

describe("DEMO-04 Community reminder demo page", () => {
  it("DEMO-04 renders UTC provenance and creates only an inert reminder draft", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Event signup to reminder draft",
      }),
    ).toBeVisible();
    expect(screen.getByText("Recommended UTC time")).toBeVisible();
    expect(screen.getByText("Not sent · not scheduled")).toBeVisible();
    expect(screen.getByText("No calendar or enrollment")).toBeVisible();
    expect(screen.getByText(COMMUNITY_REMINDER_TEMPLATE_ID)).toBeVisible();
    expect(screen.getByText(COMMUNITY_REMINDER_INSTANCE_ID)).toBeVisible();

    const form = screen.getByRole("form", {
      name: "Community reminder draft preset",
    });
    expect(
      within(form).getByRole("textbox", { name: /^session timezone/iu }),
    ).toHaveValue("America/Los_Angeles");
    expect(
      within(form).getByRole("spinbutton", {
        name: /^reminder offset minutes/iu,
      }),
    ).toHaveValue(1_440);
    const attendee = within(form).getByRole("textbox", {
      name: /^attendee display name/iu,
    });
    expect(attendee).toHaveAttribute("autocomplete", "off");
    expect(attendee).toHaveAttribute("spellcheck", "false");
    expect(attendee.getAttribute("aria-describedby")).toMatch(/sensitive/iu);
    const signupEvent = within(form).getByRole("textbox", {
      name: /^signup event id/iu,
    });
    signupEvent.focus();
    expect(signupEvent).toHaveFocus();
    const actions = within(form)
      .getByRole("button", { name: "Create reminder draft" })
      .closest(".demo-scenario-form__actions");
    expect(actions).not.toBeNull();
    expect(
      signupEvent.compareDocumentPosition(actions as Node) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(
      within(form).queryByRole("button", {
        name: /send|schedule|calendar|enroll/iu,
      }),
    ).not.toBeInTheDocument();

    await user.click(
      within(form).getByRole("button", { name: "Create reminder draft" }),
    );
    await waitFor(() => expect(createRunMock).toHaveBeenCalledTimes(1));
    expect(createRunMock).toHaveBeenCalledWith({
      scenarioId: COMMUNITY_REMINDER_SCENARIO_ID,
      instanceId: COMMUNITY_REMINDER_INSTANCE_ID,
      overrides: PRESET,
      idempotencyKey: "demo-community-reminder-retry-0001",
      expectedExecutionMode: "dry_run",
      signal: expect.any(AbortSignal),
    });
    expect(
      await screen.findByRole("heading", {
        name: "Reminder draft run accepted",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(/scheduled_reminder_draft artifact/iu),
    ).toBeVisible();
    expect(screen.queryByRole("link", { name: /approval queue/iu })).toBeNull();
  });

  it("DEMO-04 fails closed when the zero-connector contract drifts", async () => {
    fetchScenariosMock.mockResolvedValue([
      {
        ...SCENARIO,
        expected: { ...SCENARIO.expected, connectorCalls: 1 },
      },
    ]);
    renderPage();

    expect(await screen.findByText("Demo unavailable")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Create reminder draft" }),
    ).not.toBeInTheDocument();
    expect(createRunMock).not.toHaveBeenCalled();
  });
});
