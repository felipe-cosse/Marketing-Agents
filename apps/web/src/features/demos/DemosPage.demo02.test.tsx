import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  BLOG_CONTENT_REVIEW_INSTANCE_ID,
  BLOG_CONTENT_REVIEW_SCENARIO_ID,
  BLOG_CONTENT_REVIEW_TEMPLATE_ID,
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

const BLOG_PRESET = {
  article_title: "Governed AI workflows for marketing teams",
  canonical_url: "https://example.com/blog/governed-ai-workflows",
  supplied_excerpt:
    "Governed AI helps marketing teams create reviewable drafts with artifact provenance.",
  last_updated_at: "2025-12-01T00:00:00Z",
  assessment_at: "2026-08-31T00:00:00Z",
  target_keywords: ["governed AI", "marketing teams", "approval workflows"],
  current_product_metadata: {
    features: [
      {
        name: "Artifact provenance",
        summary: "Generated artifacts retain source and provider provenance.",
      },
      {
        name: "Exact approval gates",
        summary: "External writes require approval of the exact payload.",
      },
    ],
    integrations: [
      {
        name: "CMS review export",
        summary:
          "Review artifacts can be prepared for a later human-controlled CMS workflow.",
      },
    ],
  },
} as const;

const DIRECT_STATE_PATH = [
  "received",
  "validated",
  "planned",
  "executing",
  "completed",
] as const;

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
    statePath: DIRECT_STATE_PATH,
    modelCalls: 1,
    connectorCalls: 0,
    externalActions: 0,
    approvals: 0,
    externalWrites: 0,
  },
};

const BLOG_SCENARIO: DemoScenario = {
  id: BLOG_CONTENT_REVIEW_SCENARIO_ID,
  version: 1,
  displayName: "Blog & SEO content review",
  description:
    "Review supplied article and product metadata for deterministic SEO and content gaps without fetching or updating a CMS.",
  workflowId: BLOG_CONTENT_REVIEW_SCENARIO_ID,
  effect: "read_only",
  mode: "deterministic_mock",
  selectedAgents: [
    {
      templateId: BLOG_CONTENT_REVIEW_TEMPLATE_ID,
      instanceId: BLOG_CONTENT_REVIEW_INSTANCE_ID,
    },
  ],
  inputSchema: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "schema.demo.blog-seo.content-review.input.v1",
    type: "object",
    additionalProperties: false,
    required: [
      "article_title",
      "canonical_url",
      "supplied_excerpt",
      "last_updated_at",
      "assessment_at",
      "target_keywords",
      "current_product_metadata",
    ],
    properties: {
      article_title: { type: "string", minLength: 1, maxLength: 240 },
      canonical_url: {
        type: "string",
        format: "uri",
        minLength: 1,
        maxLength: 2_048,
      },
      supplied_excerpt: { type: "string", minLength: 1, maxLength: 8_000 },
      last_updated_at: {
        type: "string",
        format: "date-time",
        maxLength: 40,
      },
      assessment_at: {
        type: "string",
        format: "date-time",
        maxLength: 40,
      },
      target_keywords: {
        type: "array",
        minItems: 1,
        maxItems: 8,
        items: { type: "string", minLength: 1, maxLength: 80 },
      },
      current_product_metadata: {
        type: "object",
        additionalProperties: false,
        required: ["features", "integrations"],
        properties: {
          features: {
            type: "array",
            minItems: 0,
            maxItems: 6,
            items: {
              type: "object",
              additionalProperties: false,
              required: ["name", "summary"],
              properties: {
                name: { type: "string", minLength: 1, maxLength: 120 },
                summary: { type: "string", minLength: 1, maxLength: 500 },
              },
            },
          },
          integrations: {
            type: "array",
            minItems: 0,
            maxItems: 6,
            items: {
              type: "object",
              additionalProperties: false,
              required: ["name", "summary"],
              properties: {
                name: { type: "string", minLength: 1, maxLength: 120 },
                summary: { type: "string", minLength: 1, maxLength: 500 },
              },
            },
          },
        },
      },
    },
  },
  preset: BLOG_PRESET,
  safeSubmitVerb: "Create review",
  expected: {
    statePath: DIRECT_STATE_PATH,
    modelCalls: 1,
    connectorCalls: 0,
    externalActions: 0,
    approvals: 0,
    externalWrites: 0,
  },
};

const BLOG_RECEIPT: DemoScenarioRunReceipt = {
  status: "accepted",
  disposition: "created",
  scenarioId: BLOG_CONTENT_REVIEW_SCENARIO_ID,
  eventId: `manual-event-hmac-sha256-v1:${"e".repeat(64)}`,
  workId: "work.demo.blog.01",
  runId: "run.demo.blog.01",
  executionMode: "dry_run",
  instanceUrl: `/api/v1/agent-instances/${BLOG_CONTENT_REVIEW_INSTANCE_ID}`,
  runUrl: "/api/v1/runs/run.demo.blog.01",
  timelineUrl: "/api/v1/runs/run.demo.blog.01/timeline",
  artifactsUrl: "/api/v1/runs/run.demo.blog.01/artifacts",
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
  fetchScenariosMock.mockResolvedValue([BLOG_SCENARIO, SOCIAL_SCENARIO]);
  generateKeyMock.mockReturnValue("demo-blog-review-retry-0001");
  createRunMock.mockResolvedValue(BLOG_RECEIPT);
});

describe("DEMO-02 Blog & SEO demo page", () => {
  it("DEMO-02 selects the exact Blog review preset and submits supplied metadata only", async () => {
    const user = userEvent.setup();
    let resolveRun: ((receipt: DemoScenarioRunReceipt) => void) | undefined;
    createRunMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRun = resolve;
        }),
    );
    renderPage();

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Social idea to draft artifact",
      }),
    ).toBeVisible();
    const blogSwitch = await screen.findByRole("button", {
      name: /Blog & SEO content review/iu,
    });
    await user.click(blogSwitch);

    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "Blog metadata to SEO/content review",
      }),
    ).toBeVisible();
    expect(screen.getByText("Read-only")).toBeVisible();
    expect(screen.getByText("0 external writes")).toBeVisible();
    expect(screen.getByText("No crawling or CMS actions")).toBeVisible();
    expect(screen.getByText(BLOG_CONTENT_REVIEW_TEMPLATE_ID)).toBeVisible();
    expect(screen.getByText(BLOG_CONTENT_REVIEW_INSTANCE_ID)).toBeVisible();
    expect(
      screen.getByText(
        /canonical URL is provenance text and is never fetched/iu,
      ),
    ).toBeVisible();

    const form = screen.getByRole("form", {
      name: "Blog & SEO content review preset",
    });
    expect(
      within(form).getByRole("textbox", { name: /^article title/iu }),
    ).toHaveValue(BLOG_PRESET.article_title);
    expect(
      within(form).getByRole("textbox", { name: /^canonical url/iu }),
    ).toHaveValue(BLOG_PRESET.canonical_url);
    expect(within(form).getByText(/does not fetch it/iu)).toBeVisible();
    expect(
      within(form).queryByRole("button", {
        name: /crawl|update cms|upload|publish/iu,
      }),
    ).not.toBeInTheDocument();

    await user.click(
      within(form).getByRole("button", { name: "Create review" }),
    );
    await waitFor(() => expect(createRunMock).toHaveBeenCalledTimes(1));
    expect(createRunMock).toHaveBeenCalledWith({
      scenarioId: BLOG_CONTENT_REVIEW_SCENARIO_ID,
      instanceId: BLOG_CONTENT_REVIEW_INSTANCE_ID,
      overrides: BLOG_PRESET,
      idempotencyKey: "demo-blog-review-retry-0001",
      expectedExecutionMode: "dry_run",
      signal: expect.any(AbortSignal),
    });
    expect(
      screen.getByRole("button", { name: /Social content draft/iu }),
    ).toBeDisabled();
    expect(blogSwitch).toBeDisabled();

    resolveRun?.(BLOG_RECEIPT);
    expect(
      await screen.findByRole("heading", { name: "Review run accepted" }),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: "Open accepted run" }),
    ).toHaveAttribute("href", `/runs/${BLOG_RECEIPT.runId}`);
    expect(screen.getByRole("link", { name: "Open timeline" })).toHaveAttribute(
      "href",
      `/runs/${BLOG_RECEIPT.runId}#timeline-title`,
    );
    expect(
      screen.getByRole("link", { name: "Open artifacts" }),
    ).toHaveAttribute(
      "href",
      `/runs/${BLOG_RECEIPT.runId}#run-artifacts-title`,
    );
  });

  it("DEMO-02 removes Blog submission when its safety contract drifts", async () => {
    fetchScenariosMock.mockResolvedValue([
      {
        ...BLOG_SCENARIO,
        expected: { ...BLOG_SCENARIO.expected, externalWrites: 1 },
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
      await screen.findByRole("button", { name: "Create draft" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: /Blog & SEO content review/iu }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Create review" }),
    ).not.toBeInTheDocument();
    expect(createRunMock).not.toHaveBeenCalled();
  });
});
