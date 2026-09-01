import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  BLOG_CONTENT_REVIEW_INSTANCE_ID,
  BLOG_CONTENT_REVIEW_SCENARIO_ID,
  BLOG_CONTENT_REVIEW_TEMPLATE_ID,
  COMMUNITY_REMINDER_INSTANCE_ID,
  COMMUNITY_REMINDER_SCENARIO_ID,
  COMMUNITY_REMINDER_TEMPLATE_ID,
  PARTNERSHIP_REVIEW_INSTANCE_ID,
  PARTNERSHIP_REVIEW_SCENARIO_ID,
  PARTNERSHIP_REVIEW_TEMPLATE_ID,
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
  type DemoJsonObject,
  type DemoJsonValue,
  type DemoScenario,
  type DemoScenarioRunReceipt,
} from "../../api/demoScenarios";
import {
  compileInputSchema,
  InputSchemaCompileError,
} from "../dry-run/schemaModel";
import type {
  CompiledObjectSchema,
  JsonInputObject,
  JsonInputValue,
  SchemaDraftObject,
} from "../dry-run/schemaModel";
import {
  validateSchemaInput,
  type SchemaValidationIssue,
} from "../dry-run/schemaValidation";
import { DemoScenarioForm } from "./DemoScenarioForm";
import "../dry-run/dry-run.css";
import "./demo-scenarios.css";

const DEMO_SCENARIOS_QUERY_KEY = ["demo-scenarios", "v1"] as const;
const DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema";
const DIRECT_COMPLETION_STATE_PATH = [
  "received",
  "validated",
  "planned",
  "executing",
  "completed",
] as const;
const EMAIL_APPROVAL_STATE_PATH = [
  "received",
  "validated",
  "planned",
  "awaiting_approval",
  "executing",
  "completed",
] as const;

const SOCIAL_DRAFT_INPUT_SCHEMA = {
  $schema: DRAFT_2020_12,
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
} as const satisfies DemoJsonObject;

const SOCIAL_DRAFT_PRESET = {
  idea: "Share how governed AI workflows turn a raw marketing idea into a reviewable draft.",
  audience: "Marketing and platform leaders",
  tone: "professional",
  key_points: [
    "Treat external content as untrusted data.",
    "Keep generation separate from publishing authority.",
    "Persist a traceable artifact for review.",
  ],
  source_urls: ["https://example.com/governed-ai"],
} as const satisfies DemoJsonObject;

const BLOG_CONTENT_REVIEW_INPUT_SCHEMA = {
  $schema: DRAFT_2020_12,
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
} as const satisfies DemoJsonObject;

const BLOG_CONTENT_REVIEW_PRESET = {
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
} as const satisfies DemoJsonObject;

const EMAIL_SIGNUP_INPUT_SCHEMA = {
  $schema: DRAFT_2020_12,
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
    signup_at: {
      type: "string",
      format: "date-time",
      maxLength: 40,
    },
    welcome_context: { type: "string", minLength: 1, maxLength: 2_000 },
  },
} as const satisfies DemoJsonObject;

const EMAIL_SIGNUP_PRESET = {
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
} as const satisfies DemoJsonObject;

const COMMUNITY_REMINDER_INPUT_SCHEMA = {
  $schema: DRAFT_2020_12,
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
} as const satisfies DemoJsonObject;

const COMMUNITY_REMINDER_PRESET = {
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
} as const satisfies DemoJsonObject;

const STABLE_ID = "^[a-z0-9][a-z0-9._-]{0,79}$";
const RISK_FLAGS = [
  "compliance_gap",
  "delivery_capacity_gap",
  "security_gap",
  "unverified_claim",
] as const;
// Exported for exact-contract regression fixtures alongside the page component.
// eslint-disable-next-line react-refresh/only-export-components
export const PARTNERSHIP_REVIEW_INPUT_SCHEMA = {
  $schema: DRAFT_2020_12,
  $id: "schema.demo.partnerships.application-review.input.v1",
  type: "object",
  additionalProperties: false,
  required: [
    "applicant_id",
    "organization_metadata",
    "declared_capabilities",
    "declared_regions",
    "evidence_records",
    "program_criteria",
    "program_constraints",
    "missing_information_indicators",
  ],
  properties: {
    applicant_id: {
      type: "string",
      minLength: 1,
      maxLength: 120,
      pattern: "^[a-z0-9][a-z0-9._-]{0,119}$",
      "x-sensitive": true,
    },
    organization_metadata: {
      type: "object",
      additionalProperties: false,
      "x-sensitive": true,
      required: [
        "organization_name",
        "organization_type",
        "organization_summary",
        "website_reference",
      ],
      properties: {
        organization_name: {
          type: "string",
          minLength: 1,
          maxLength: 160,
        },
        organization_type: {
          type: "string",
          enum: [
            "systems_integrator",
            "consultancy",
            "technology_provider",
            "training_provider",
            "other",
          ],
        },
        organization_summary: {
          type: "string",
          minLength: 1,
          maxLength: 2000,
        },
        website_reference: {
          type: "string",
          format: "uri",
          minLength: 1,
          maxLength: 2048,
        },
      },
    },
    declared_capabilities: {
      type: "array",
      minItems: 1,
      maxItems: 16,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["capability_id", "label"],
        properties: {
          capability_id: {
            type: "string",
            minLength: 1,
            maxLength: 80,
            pattern: STABLE_ID,
          },
          label: { type: "string", minLength: 1, maxLength: 160 },
        },
      },
    },
    declared_regions: {
      type: "array",
      minItems: 1,
      maxItems: 16,
      items: {
        type: "string",
        minLength: 1,
        maxLength: 80,
        pattern: STABLE_ID,
      },
    },
    evidence_records: {
      type: "array",
      maxItems: 24,
      "x-sensitive": true,
      items: {
        type: "object",
        additionalProperties: false,
        required: [
          "evidence_id",
          "evidence_type",
          "summary",
          "supports_criterion_ids",
          "risk_flags",
        ],
        properties: {
          evidence_id: {
            type: "string",
            minLength: 1,
            maxLength: 80,
            pattern: STABLE_ID,
          },
          evidence_type: {
            type: "string",
            enum: [
              "case_study",
              "certification",
              "customer_reference",
              "program_history",
              "security_attestation",
              "other",
            ],
          },
          summary: {
            type: "string",
            minLength: 1,
            maxLength: 2000,
          },
          supports_criterion_ids: {
            type: "array",
            maxItems: 24,
            items: {
              type: "string",
              minLength: 1,
              maxLength: 80,
              pattern: STABLE_ID,
            },
          },
          risk_flags: {
            type: "array",
            maxItems: 4,
            items: { type: "string", enum: RISK_FLAGS },
          },
        },
      },
    },
    program_criteria: {
      type: "array",
      minItems: 1,
      maxItems: 24,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["criterion_id", "description", "required"],
        properties: {
          criterion_id: {
            type: "string",
            minLength: 1,
            maxLength: 80,
            pattern: STABLE_ID,
          },
          description: { type: "string", minLength: 1, maxLength: 500 },
          required: { type: "boolean" },
        },
      },
    },
    program_constraints: {
      type: "object",
      additionalProperties: false,
      required: [
        "eligible_regions",
        "required_capability_ids",
        "minimum_evidence_records",
        "disqualifying_risk_flags",
      ],
      properties: {
        eligible_regions: {
          type: "array",
          minItems: 1,
          maxItems: 16,
          items: {
            type: "string",
            minLength: 1,
            maxLength: 80,
            pattern: STABLE_ID,
          },
        },
        required_capability_ids: {
          type: "array",
          maxItems: 16,
          items: {
            type: "string",
            minLength: 1,
            maxLength: 80,
            pattern: STABLE_ID,
          },
        },
        minimum_evidence_records: { type: "integer", minimum: 0, maximum: 24 },
        disqualifying_risk_flags: {
          type: "array",
          maxItems: 4,
          items: { type: "string", enum: RISK_FLAGS },
        },
      },
    },
    missing_information_indicators: {
      type: "array",
      maxItems: 24,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["indicator_id", "description", "related_criterion_ids"],
        properties: {
          indicator_id: {
            type: "string",
            minLength: 1,
            maxLength: 80,
            pattern: STABLE_ID,
          },
          description: {
            type: "string",
            minLength: 1,
            maxLength: 500,
          },
          related_criterion_ids: {
            type: "array",
            maxItems: 24,
            items: {
              type: "string",
              minLength: 1,
              maxLength: 80,
              pattern: STABLE_ID,
            },
          },
        },
      },
    },
  },
} as const satisfies DemoJsonObject;

// eslint-disable-next-line react-refresh/only-export-components
export const PARTNERSHIP_REVIEW_PRESET = {
  applicant_id: "applicant.partnership-demo-0001",
  organization_metadata: {
    organization_name: "Northstar Systems Demo",
    organization_type: "systems_integrator",
    organization_summary:
      "Synthetic implementation partner specializing in governed automation.",
    website_reference: "https://example.com/partners/northstar-systems",
  },
  declared_capabilities: [
    {
      capability_id: "implementation.governance",
      label: "AI governance implementation",
    },
    { capability_id: "integration.delivery", label: "Integration delivery" },
  ],
  declared_regions: ["europe", "north_america"],
  evidence_records: [
    {
      evidence_id: "evidence.partner-demo-001",
      evidence_type: "case_study",
      summary: "Synthetic case study documents a governed deployment.",
      supports_criterion_ids: ["criterion.governed-delivery"],
      risk_flags: [],
    },
    {
      evidence_id: "evidence.partner-demo-002",
      evidence_type: "certification",
      summary:
        "Synthetic training record documents integration delivery readiness.",
      supports_criterion_ids: ["criterion.integration-readiness"],
      risk_flags: [],
    },
  ],
  program_criteria: [
    {
      criterion_id: "criterion.governed-delivery",
      description: "Evidence of governed delivery.",
      required: true,
    },
    {
      criterion_id: "criterion.integration-readiness",
      description: "Evidence of integration delivery readiness.",
      required: true,
    },
    {
      criterion_id: "criterion.security-attestation",
      description: "Current security attestation.",
      required: true,
    },
  ],
  program_constraints: {
    eligible_regions: ["europe", "north_america"],
    required_capability_ids: [
      "implementation.governance",
      "integration.delivery",
    ],
    minimum_evidence_records: 3,
    disqualifying_risk_flags: ["compliance_gap", "security_gap"],
  },
  missing_information_indicators: [
    {
      indicator_id: "missing.security-attestation",
      description: "A current security attestation was not supplied.",
      related_criterion_ids: ["criterion.security-attestation"],
    },
  ],
} as const satisfies DemoJsonObject;

interface ScenarioPresentation {
  readonly scenarioId:
    | typeof SOCIAL_DRAFT_SCENARIO_ID
    | typeof BLOG_CONTENT_REVIEW_SCENARIO_ID
    | typeof EMAIL_SIGNUP_SCENARIO_ID
    | typeof COMMUNITY_REMINDER_SCENARIO_ID
    | typeof PARTNERSHIP_REVIEW_SCENARIO_ID;
  readonly effect: DemoScenario["effect"];
  readonly primaryInstanceId: string;
  readonly selectedAgents: readonly {
    readonly templateId: string;
    readonly instanceId: string;
    readonly templateLabel: string;
    readonly instanceLabel: string;
  }[];
  readonly expected: DemoScenario["expected"];
  readonly receiptExecutionMode: "dry_run" | "mock_execute";
  readonly displayName: string;
  readonly description: string;
  readonly inputSchema: DemoJsonObject;
  readonly preset: DemoJsonObject;
  readonly safeSubmitVerb: string;
  readonly eyebrow: string;
  readonly pageTitle: string;
  readonly pageDescription: string;
  readonly modeTitle: string;
  readonly modeDetail: string;
  readonly modeTone: "safe" | "awaiting";
  readonly formId: string;
  readonly formLabel: string;
  readonly formModeTitle: string;
  readonly formModeDescription: string;
  readonly guardrailBadges: readonly string[];
  readonly boundaryNote: string | null;
  readonly receiptTitle: string;
  readonly receiptDescription: string;
  readonly approvalQueueLink: boolean;
}

const SOCIAL_PRESENTATION: ScenarioPresentation = Object.freeze({
  scenarioId: SOCIAL_DRAFT_SCENARIO_ID,
  effect: "read_only",
  primaryInstanceId: SOCIAL_DRAFT_INSTANCE_ID,
  selectedAgents: [
    {
      templateId: SOCIAL_DRAFT_TEMPLATE_ID,
      instanceId: SOCIAL_DRAFT_INSTANCE_ID,
      templateLabel: "Selected Social template",
      instanceLabel: "Selected Social instance",
    },
  ],
  expected: {
    statePath: DIRECT_COMPLETION_STATE_PATH,
    modelCalls: 1,
    connectorCalls: 0,
    externalActions: 0,
    approvals: 0,
    externalWrites: 0,
  },
  receiptExecutionMode: "dry_run",
  displayName: "Social content draft",
  description:
    "Turn a supplied content idea into an inert, reviewable LinkedIn draft artifact.",
  inputSchema: SOCIAL_DRAFT_INPUT_SCHEMA,
  preset: SOCIAL_DRAFT_PRESET,
  safeSubmitVerb: "Create draft",
  eyebrow: "DEMO-01 · Social workflow",
  pageTitle: "Social idea to draft artifact",
  pageDescription:
    "Start from an API-declared safe preset, admit one deterministic mock run, and follow its durable timeline to the draft artifact.",
  modeTitle: "Deterministic mock mode",
  modeDetail: "No connector delivery",
  modeTone: "safe",
  formId: "demo-social-draft-form",
  formLabel: "Social draft demo preset",
  formModeTitle: "Deterministic mock mode",
  formModeDescription:
    "The API admits durable dry-run work; the demo model response is fixed and connectors stay unused.",
  guardrailBadges: ["Read-only", "0 external writes", "No approval required"],
  boundaryNote: null,
  receiptTitle: "Draft run accepted",
  receiptDescription:
    "Durable intake created the work receipt. Follow the run for timeline progress and the eventual draft artifact.",
  approvalQueueLink: false,
});

const BLOG_PRESENTATION: ScenarioPresentation = Object.freeze({
  scenarioId: BLOG_CONTENT_REVIEW_SCENARIO_ID,
  effect: "read_only",
  primaryInstanceId: BLOG_CONTENT_REVIEW_INSTANCE_ID,
  selectedAgents: [
    {
      templateId: BLOG_CONTENT_REVIEW_TEMPLATE_ID,
      instanceId: BLOG_CONTENT_REVIEW_INSTANCE_ID,
      templateLabel: "Selected Blog & SEO template",
      instanceLabel: "Selected Blog & SEO instance",
    },
  ],
  expected: {
    statePath: DIRECT_COMPLETION_STATE_PATH,
    modelCalls: 1,
    connectorCalls: 0,
    externalActions: 0,
    approvals: 0,
    externalWrites: 0,
  },
  receiptExecutionMode: "dry_run",
  displayName: "Blog & SEO content review",
  description:
    "Review supplied article and product metadata for deterministic SEO and content gaps without fetching or updating a CMS.",
  inputSchema: BLOG_CONTENT_REVIEW_INPUT_SCHEMA,
  preset: BLOG_CONTENT_REVIEW_PRESET,
  safeSubmitVerb: "Create review",
  eyebrow: "DEMO-02 · Blog & SEO workflow",
  pageTitle: "Blog metadata to SEO/content review",
  pageDescription:
    "Assess only the supplied article, timestamps, target keywords, and product metadata, then follow the durable run to an advisory review artifact.",
  modeTitle: "Deterministic mock mode",
  modeDetail: "No crawling or CMS changes",
  modeTone: "safe",
  formId: "demo-blog-content-review-form",
  formLabel: "Blog & SEO content review preset",
  formModeTitle: "Deterministic mock mode",
  formModeDescription:
    "The API admits durable dry-run work; the demo model response is fixed and connectors stay unused.",
  guardrailBadges: [
    "Read-only",
    "0 external writes",
    "No approval required",
    "No crawling or CMS actions",
  ],
  boundaryNote:
    "Only supplied metadata is reviewed. The canonical URL is provenance text and is never fetched.",
  receiptTitle: "Review run accepted",
  receiptDescription:
    "Durable intake created the work receipt. Follow the run for timeline progress and the eventual advisory content-review artifact.",
  approvalQueueLink: false,
});

const EMAIL_PRESENTATION: ScenarioPresentation = Object.freeze({
  scenarioId: EMAIL_SIGNUP_SCENARIO_ID,
  effect: "mutating",
  primaryInstanceId: EMAIL_NEWSLETTER_INSTANCE_ID,
  selectedAgents: [
    {
      templateId: EMAIL_NEWSLETTER_TEMPLATE_ID,
      instanceId: EMAIL_NEWSLETTER_INSTANCE_ID,
      templateLabel: "Selected Newsletter template",
      instanceLabel: "Selected Newsletter instance",
    },
    {
      templateId: EMAIL_ONBOARDING_TEMPLATE_ID,
      instanceId: EMAIL_ONBOARDING_INSTANCE_ID,
      templateLabel: "Selected Onboarding template",
      instanceLabel: "Selected Onboarding instance",
    },
  ],
  expected: {
    statePath: EMAIL_APPROVAL_STATE_PATH,
    modelCalls: 1,
    connectorCalls: 2,
    externalActions: 2,
    approvals: 2,
    externalWrites: 2,
  },
  receiptExecutionMode: "mock_execute",
  displayName: "Email signup onboarding",
  description:
    "Prepare approved mock newsletter and CRM onboarding actions, then create a welcome-message draft that is never sent.",
  inputSchema: EMAIL_SIGNUP_INPUT_SCHEMA,
  preset: EMAIL_SIGNUP_PRESET,
  safeSubmitVerb: "Propose onboarding actions",
  eyebrow: "DEMO-03 · Email workflow",
  pageTitle: "Email signup approval boundary",
  pageDescription:
    "Prepare two immutable mock actions from synthetic signup data, pause for their separate exact approvals, and follow the durable run to an unsent welcome-message draft.",
  modeTitle: "Approval-gated mock execution",
  modeDetail: "Both approvals before any call",
  modeTone: "awaiting",
  formId: "demo-email-signup-form",
  formLabel: "Email signup onboarding preset",
  formModeTitle: "Approval-gated mock execution",
  formModeDescription:
    "Planning proposes two immutable mock actions. Neither connector nor the welcome-draft model runs until both exact approvals remain valid.",
  guardrailBadges: [
    "Mock writes only",
    "2 exact approvals required",
    "Both approvals before any call",
    "Welcome draft only · not sent",
  ],
  boundaryNote:
    "Submission proposes newsletter.subscribe and crm.upsert-contact. The accepted receipt is not proof of approval, connector execution, CRM completion, or email delivery.",
  receiptTitle: "Approval-gated run accepted",
  receiptDescription:
    "Durable intake accepted the run for planning. Open the authoritative run and approval queue to verify its current state; this receipt does not prove zero calls or execution.",
  approvalQueueLink: true,
});

const COMMUNITY_PRESENTATION: ScenarioPresentation = Object.freeze({
  scenarioId: COMMUNITY_REMINDER_SCENARIO_ID,
  effect: "read_only",
  primaryInstanceId: COMMUNITY_REMINDER_INSTANCE_ID,
  selectedAgents: [
    {
      templateId: COMMUNITY_REMINDER_TEMPLATE_ID,
      instanceId: COMMUNITY_REMINDER_INSTANCE_ID,
      templateLabel: "Selected Community template",
      instanceLabel: "Selected Community instance",
    },
  ],
  expected: {
    statePath: DIRECT_COMPLETION_STATE_PATH,
    modelCalls: 1,
    connectorCalls: 0,
    externalActions: 0,
    approvals: 0,
    externalWrites: 0,
  },
  receiptExecutionMode: "dry_run",
  displayName: "Community reminder draft",
  description:
    "Create a deterministic reminder draft and recommended UTC time from supplied event signup details without scheduling or sending.",
  inputSchema: COMMUNITY_REMINDER_INPUT_SCHEMA,
  preset: COMMUNITY_REMINDER_PRESET,
  safeSubmitVerb: "Create reminder draft",
  eyebrow: "DEMO-04 · Community workflow",
  pageTitle: "Event signup to reminder draft",
  pageDescription:
    "Resolve the supplied local session time to UTC, recommend a reminder time, and create a reviewable draft without touching an event, calendar, scheduler, or messaging provider.",
  modeTitle: "Deterministic mock mode",
  modeDetail: "Recommended UTC · never scheduled",
  modeTone: "safe",
  formId: "demo-community-reminder-form",
  formLabel: "Community reminder draft preset",
  formModeTitle: "Read-only reminder planning",
  formModeDescription:
    "The supplied IANA timezone and offset produce UTC provenance. The draft remains local and is never scheduled or sent.",
  guardrailBadges: [
    "Read-only",
    "Recommended UTC time",
    "Not sent · not scheduled",
    "No calendar or enrollment",
  ],
  boundaryNote:
    "The channel is drafting context only. This demo does not enroll an attendee, mutate a calendar, create a provider schedule, or send a reminder.",
  receiptTitle: "Reminder draft run accepted",
  receiptDescription:
    "Durable intake accepted a dry-run draft request. Follow the authoritative run for the recommended UTC time and scheduled_reminder_draft artifact; acceptance is not proof of scheduling or delivery.",
  approvalQueueLink: false,
});

const PARTNERSHIP_PRESENTATION: ScenarioPresentation = Object.freeze({
  scenarioId: PARTNERSHIP_REVIEW_SCENARIO_ID,
  effect: "read_only",
  primaryInstanceId: PARTNERSHIP_REVIEW_INSTANCE_ID,
  selectedAgents: [
    {
      templateId: PARTNERSHIP_REVIEW_TEMPLATE_ID,
      instanceId: PARTNERSHIP_REVIEW_INSTANCE_ID,
      templateLabel: "Selected Partnerships template",
      instanceLabel: "Selected Partnerships instance",
    },
  ],
  expected: {
    statePath: DIRECT_COMPLETION_STATE_PATH,
    modelCalls: 1,
    connectorCalls: 0,
    externalActions: 0,
    approvals: 0,
    externalWrites: 0,
  },
  receiptExecutionMode: "dry_run",
  displayName: "Partnership application review",
  description:
    "Create a deterministic advisory recommendation from supplied partner application evidence without external research, applicant notification, record mutation, or an automated decision.",
  inputSchema: PARTNERSHIP_REVIEW_INPUT_SCHEMA,
  preset: PARTNERSHIP_REVIEW_PRESET,
  safeSubmitVerb: "Create advisory review",
  eyebrow: "DEMO-05 · Partnerships workflow",
  pageTitle: "Partner application to advisory review",
  pageDescription:
    "Assess only the supplied application, criteria, and evidence, then create an evidence-linked recommendation for human review.",
  modeTitle: "Deterministic mock mode",
  modeDetail: "Advisory only · no automated decision",
  modeTone: "safe",
  formId: "demo-partnership-review-form",
  formLabel: "Partnership application review preset",
  formModeTitle: "Read-only advisory review",
  formModeDescription:
    "The fixed model evaluates supplied evidence only. It cannot research the applicant, change a partner record, notify anyone, or accept or reject an application.",
  guardrailBadges: [
    "Read-only",
    "Advisory only",
    "No automated decision",
    "No external research or notification",
  ],
  boundaryNote:
    "Accept, reject, and needs_information are recommendation labels only. A human remains responsible for every consequential partnership decision.",
  receiptTitle: "Advisory review run accepted",
  receiptDescription:
    "Durable intake accepted a dry-run advisory review. Follow the authoritative run and artifact for its recommendation, rationale, uncertainty, risks, missing information, and follow-up questions; acceptance is not an applicant decision.",
  approvalQueueLink: false,
});

const SUPPORTED_PRESENTATIONS = [
  SOCIAL_PRESENTATION,
  BLOG_PRESENTATION,
  EMAIL_PRESENTATION,
  COMMUNITY_PRESENTATION,
  PARTNERSHIP_PRESENTATION,
] as const;

interface PreparedScenario {
  readonly scenario: DemoScenario;
  readonly schema: CompiledObjectSchema;
  readonly preset: SchemaDraftObject;
  readonly presentation: ScenarioPresentation;
}

type Preparation =
  Readonly<{ ok: true; value: PreparedScenario }> | Readonly<{ ok: false }>;

function cloneDraftValue(value: JsonInputValue): unknown {
  if (Array.isArray(value)) {
    const items = value as readonly JsonInputValue[];
    return items.map((item) => cloneDraftValue(item));
  }
  if (typeof value === "object") {
    const result: SchemaDraftObject = {};
    for (const [key, child] of Object.entries(value as JsonInputObject)) {
      result[key] = cloneDraftValue(child);
    }
    return result;
  }
  return value;
}

function clonePreset(preset: SchemaDraftObject): SchemaDraftObject {
  const result: SchemaDraftObject = {};
  for (const [key, value] of Object.entries(preset)) {
    result[key] = cloneDraftValue(value as JsonInputValue);
  }
  return result;
}

function exactJson(
  left: DemoJsonValue | undefined,
  right: DemoJsonValue | undefined,
): boolean {
  if (left === right) return true;
  if (left === undefined || right === undefined) return false;
  if (left === null || right === null) return false;
  if (typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right)) return false;
    const leftArray = left as readonly DemoJsonValue[];
    const rightArray = right as readonly DemoJsonValue[];
    return (
      leftArray.length === rightArray.length &&
      leftArray.every((item, index) => exactJson(item, rightArray[index]))
    );
  }
  const leftObject = left as DemoJsonObject;
  const rightObject = right as DemoJsonObject;
  const leftKeys = Object.keys(leftObject).sort();
  const rightKeys = Object.keys(rightObject).sort();
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key, index) =>
        key === rightKeys[index] &&
        exactJson(leftObject[key], rightObject[key]),
    )
  );
}

function isExactSupportedContract(
  scenario: DemoScenario,
  presentation: ScenarioPresentation,
): boolean {
  return (
    scenario.id === presentation.scenarioId &&
    scenario.version === 1 &&
    scenario.workflowId === presentation.scenarioId &&
    scenario.displayName === presentation.displayName &&
    scenario.description === presentation.description &&
    scenario.effect === presentation.effect &&
    scenario.safeSubmitVerb === presentation.safeSubmitVerb &&
    exactJson(scenario.inputSchema, presentation.inputSchema) &&
    exactJson(scenario.preset, presentation.preset) &&
    scenario.selectedAgents.length === presentation.selectedAgents.length &&
    scenario.selectedAgents.every((selected, index) => {
      const expected = presentation.selectedAgents[index];
      return exactJson(
        selected,
        expected === undefined
          ? null
          : {
              templateId: expected.templateId,
              instanceId: expected.instanceId,
            },
      );
    }) &&
    presentation.selectedAgents.some(
      ({ instanceId }) => instanceId === presentation.primaryInstanceId,
    ) &&
    scenario.expected.statePath.length ===
      presentation.expected.statePath.length &&
    scenario.expected.statePath.every(
      (state, index) => state === presentation.expected.statePath[index],
    ) &&
    scenario.expected.modelCalls === presentation.expected.modelCalls &&
    scenario.expected.connectorCalls === presentation.expected.connectorCalls &&
    scenario.expected.externalActions ===
      presentation.expected.externalActions &&
    scenario.expected.approvals === presentation.expected.approvals &&
    scenario.expected.externalWrites === presentation.expected.externalWrites
  );
}

function prepareScenario(
  scenario: DemoScenario,
  presentation: ScenarioPresentation,
): Preparation {
  if (!isExactSupportedContract(scenario, presentation)) return { ok: false };
  try {
    const schema = compileInputSchema(scenario.inputSchema);
    const preset = validateSchemaInput(schema, scenario.preset);
    if (!preset.ok) return { ok: false };
    return {
      ok: true,
      value: {
        scenario,
        schema,
        preset: clonePreset(preset.input),
        presentation,
      },
    };
  } catch (error) {
    if (error instanceof InputSchemaCompileError) return { ok: false };
    return { ok: false };
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function stableErrorMessage(error: unknown): string {
  return error instanceof Error && error.message.length > 0
    ? error.message
    : "The local API could not accept this demo run.";
}

function SafetyFacts({
  scenario,
  presentation,
}: {
  readonly scenario: DemoScenario;
  readonly presentation: ScenarioPresentation;
}): React.JSX.Element {
  return (
    <section
      className={`demo-card demo-safety is-${presentation.modeTone}`}
      aria-labelledby="demo-safety-title"
    >
      <div className="demo-card__heading">
        <p className="eyebrow">Guardrails</p>
        <h2 id="demo-safety-title">What this demo can do</h2>
      </div>
      <ul className="demo-safety__badges" aria-label="Demo safety summary">
        {presentation.guardrailBadges.map((badge) => (
          <li key={badge}>{badge}</li>
        ))}
      </ul>
      <dl className="demo-facts">
        <div>
          <dt>Model calls</dt>
          <dd>{scenario.expected.modelCalls} deterministic mock</dd>
        </div>
        <div>
          <dt>Connector calls</dt>
          <dd>{scenario.expected.connectorCalls}</dd>
        </div>
        <div>
          <dt>External actions</dt>
          <dd>{scenario.expected.externalActions}</dd>
        </div>
        <div>
          <dt>Approvals</dt>
          <dd>{scenario.expected.approvals}</dd>
        </div>
      </dl>
      <ol className="demo-safety__agents" aria-label="Selected demo agents">
        {scenario.selectedAgents.map((selected, index) => {
          const labels = presentation.selectedAgents[index];
          if (labels === undefined) return null;
          return (
            <li key={selected.instanceId} className="demo-safety__agent">
              <span>{labels.templateLabel}</span>
              <code>{selected.templateId}</code>
              <span>{labels.instanceLabel}</span>
              <code>{selected.instanceId}</code>
            </li>
          );
        })}
      </ol>
      {presentation.boundaryNote === null ? null : (
        <p className="demo-safety__boundary">{presentation.boundaryNote}</p>
      )}
    </section>
  );
}

function ExpectedJourney({
  scenario,
}: {
  readonly scenario: DemoScenario;
}): React.JSX.Element {
  return (
    <section
      className="demo-card demo-journey"
      aria-labelledby="demo-journey-title"
    >
      <div className="demo-card__heading">
        <p className="eyebrow">Expected journey</p>
        <h2 id="demo-journey-title">Durable state path</h2>
      </div>
      <ol aria-label="Expected durable state path">
        {scenario.expected.statePath.map((state, index) => (
          <li key={state}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <strong>{state}</strong>
          </li>
        ))}
      </ol>
      <p>
        Acceptance creates durable work and a run. The receipt is not proof that
        the final artifact has completed yet.
      </p>
    </section>
  );
}

function AcceptedReceipt({
  receipt,
  presentation,
}: {
  readonly receipt: DemoScenarioRunReceipt;
  readonly presentation: ScenarioPresentation;
}): React.JSX.Element {
  const runPath = `/runs/${encodeURIComponent(receipt.runId)}`;
  return (
    <section
      className="demo-receipt"
      aria-labelledby="demo-receipt-title"
      aria-live="polite"
      role="status"
    >
      <div>
        <p className="eyebrow">Accepted</p>
        <h3 id="demo-receipt-title">{presentation.receiptTitle}</h3>
        <p>{presentation.receiptDescription}</p>
      </div>
      <dl>
        <div>
          <dt>Disposition</dt>
          <dd>{receipt.disposition}</dd>
        </div>
        <div>
          <dt>Work ID</dt>
          <dd>{receipt.workId}</dd>
        </div>
        <div>
          <dt>Run ID</dt>
          <dd>{receipt.runId}</dd>
        </div>
        <div>
          <dt>Durable intake mode</dt>
          <dd>{receipt.executionMode}</dd>
        </div>
      </dl>
      <nav aria-label="Accepted demo resources">
        <Link to={runPath}>Open accepted run</Link>
        <Link to={`${runPath}#timeline-title`}>Open timeline</Link>
        <Link to={`${runPath}#run-artifacts-title`}>Open artifacts</Link>
        {presentation.approvalQueueLink ? (
          <Link to={`/approvals?run_id=${encodeURIComponent(receipt.runId)}`}>
            Open approval queue
          </Link>
        ) : null}
      </nav>
    </section>
  );
}

function DemoWorkspace({
  prepared,
  onPendingChange,
}: {
  readonly prepared: PreparedScenario;
  readonly onPendingChange: (pending: boolean) => void;
}): React.JSX.Element {
  const { scenario, schema, preset, presentation } = prepared;
  const [draft, setDraft] = useState<SchemaDraftObject>(() =>
    clonePreset(preset),
  );
  const [issues, setIssues] = useState<readonly SchemaValidationIssue[]>([]);
  const [validationRevision, setValidationRevision] = useState(0);
  const [pending, setPending] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [abortNotice, setAbortNotice] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<DemoScenarioRunReceipt | null>(null);
  const idempotencyKeyRef = useRef<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      controllerRef.current?.abort();
      onPendingChange(false);
    };
  }, [onPendingChange]);

  const clearFeedback = (): void => {
    setIssues([]);
    setRequestError(null);
    setAbortNotice(null);
    setReceipt(null);
  };

  const updateDraft = (nextDraft: SchemaDraftObject): void => {
    idempotencyKeyRef.current = null;
    setDraft(nextDraft);
    clearFeedback();
  };

  const resetPreset = (): void => {
    idempotencyKeyRef.current = null;
    setDraft(clonePreset(preset));
    clearFeedback();
  };

  const stopWaiting = (): void => {
    controllerRef.current?.abort();
  };

  const submit = async (): Promise<void> => {
    if (pending) return;
    const validation = validateSchemaInput(schema, draft);
    if (!validation.ok) {
      setIssues(validation.issues);
      setValidationRevision((revision) => revision + 1);
      setRequestError(null);
      setAbortNotice(null);
      setReceipt(null);
      return;
    }
    let idempotencyKey: string;
    try {
      idempotencyKey =
        idempotencyKeyRef.current ?? generateDemoScenarioIdempotencyKey();
    } catch (error) {
      setRequestError(stableErrorMessage(error));
      return;
    }
    idempotencyKeyRef.current = idempotencyKey;
    const controller = new AbortController();
    controllerRef.current = controller;
    setPending(true);
    onPendingChange(true);
    clearFeedback();
    try {
      const accepted = await createDemoScenarioRun({
        scenarioId: scenario.id,
        instanceId: presentation.primaryInstanceId,
        overrides: validation.input,
        idempotencyKey,
        expectedExecutionMode: presentation.receiptExecutionMode,
        signal: controller.signal,
      });
      if (!mountedRef.current) return;
      controllerRef.current = null;
      idempotencyKeyRef.current = null;
      setPending(false);
      onPendingChange(false);
      setReceipt(accepted);
    } catch (error) {
      if (!mountedRef.current) return;
      controllerRef.current = null;
      setPending(false);
      onPendingChange(false);
      if (isAbortError(error)) {
        setAbortNotice(
          "Stopped waiting. The API may still have accepted this idempotent request; retry without editing to recover its receipt.",
        );
        return;
      }
      setRequestError(stableErrorMessage(error));
    }
  };

  return (
    <div className="demo-page__layout">
      <SafetyFacts scenario={scenario} presentation={presentation} />
      <section
        className="demo-card demo-builder"
        aria-labelledby="demo-builder-title"
      >
        <div className="demo-card__heading demo-builder__heading">
          <div>
            <p className="eyebrow">Schema-driven safe preset</p>
            <h2 id="demo-builder-title">{scenario.displayName}</h2>
          </div>
          <span>Deterministic mock</span>
        </div>
        <p className="demo-builder__description">{scenario.description}</p>
        <DemoScenarioForm
          formId={presentation.formId}
          ariaLabel={presentation.formLabel}
          submitLabel={presentation.safeSubmitVerb}
          modeTitle={presentation.formModeTitle}
          modeDescription={presentation.formModeDescription}
          modeTone={presentation.modeTone}
          schema={schema}
          draft={draft}
          issues={issues}
          validationRevision={validationRevision}
          pending={pending}
          onDraftChange={updateDraft}
          onSubmit={() => void submit()}
          onStopWaiting={stopWaiting}
          onReset={resetPreset}
        />
        {requestError === null ? null : (
          <p className="demo-feedback is-error" role="alert">
            {requestError}
          </p>
        )}
        {abortNotice === null ? null : (
          <p className="demo-feedback is-notice" role="status">
            {abortNotice}
          </p>
        )}
        {receipt === null ? null : (
          <AcceptedReceipt receipt={receipt} presentation={presentation} />
        )}
      </section>
      <ExpectedJourney scenario={scenario} />
    </div>
  );
}

function UnavailableState({
  message,
}: {
  readonly message: string;
}): React.JSX.Element {
  return (
    <section className="demo-page__state" role="alert">
      <strong>Demo unavailable</strong>
      <p>{message}</p>
    </section>
  );
}

function ScenarioSwitchboard({
  scenarios,
  activeScenarioId,
  disabled,
  onSelect,
}: {
  readonly scenarios: readonly PreparedScenario[];
  readonly activeScenarioId: string;
  readonly disabled: boolean;
  readonly onSelect: (scenarioId: string) => void;
}): React.JSX.Element {
  return (
    <nav className="demo-switchboard" aria-label="Deterministic demo scenarios">
      <div className="demo-switchboard__label" aria-hidden="true">
        <span>Workflow switchboard</span>
        <strong>{String(scenarios.length).padStart(2, "0")} verified</strong>
      </div>
      <div className="demo-switchboard__rail">
        {scenarios.map((prepared, index) => {
          const { presentation, scenario } = prepared;
          const active = scenario.id === activeScenarioId;
          return (
            <button
              key={scenario.id}
              type="button"
              className={active ? "is-active" : undefined}
              aria-pressed={active}
              disabled={disabled}
              onClick={() => onSelect(scenario.id)}
            >
              <span className="demo-switchboard__number" aria-hidden="true">
                {String(index + 1).padStart(2, "0")}
              </span>
              <span>
                <strong>{presentation.displayName}</strong>
                <small>{presentation.modeDetail}</small>
              </span>
            </button>
          );
        })}
      </div>
      {disabled ? (
        <span className="sr-only" role="status">
          Scenario switching is unavailable while the durable receipt is
          pending.
        </span>
      ) : null}
    </nav>
  );
}

export function DemosPage(): React.JSX.Element {
  const [selectedScenarioId, setSelectedScenarioId] = useState<string>(
    SOCIAL_DRAFT_SCENARIO_ID,
  );
  const [selectorLocked, setSelectorLocked] = useState(false);
  const scenariosQuery = useQuery({
    queryKey: DEMO_SCENARIOS_QUERY_KEY,
    queryFn: ({ signal }) => fetchDemoScenarios(signal),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });
  const preparedScenarios = useMemo<readonly PreparedScenario[]>(() => {
    if (scenariosQuery.data === undefined) return [];
    const prepared: PreparedScenario[] = [];
    for (const presentation of SUPPORTED_PRESENTATIONS) {
      const scenario = scenariosQuery.data.find(
        (item) => item.id === presentation.scenarioId,
      );
      if (scenario === undefined) continue;
      const result = prepareScenario(scenario, presentation);
      if (result.ok) prepared.push(result.value);
    }
    return prepared;
  }, [scenariosQuery.data]);
  const activeScenario =
    preparedScenarios.find(
      (prepared) => prepared.scenario.id === selectedScenarioId,
    ) ??
    preparedScenarios[0] ??
    null;
  const activePresentation =
    activeScenario?.presentation ?? SOCIAL_PRESENTATION;
  const onPendingChange = useCallback((pending: boolean): void => {
    setSelectorLocked(pending);
  }, []);

  return (
    <main className="demo-page" aria-labelledby="demo-page-title">
      <header className="demo-page__header">
        <div>
          <p className="eyebrow">{activePresentation.eyebrow}</p>
          <h1 id="demo-page-title">{activePresentation.pageTitle}</h1>
          <p>{activePresentation.pageDescription}</p>
        </div>
        <div
          className={`demo-page__mode is-${activePresentation.modeTone}`}
          aria-label="Demo execution boundary"
        >
          <span aria-hidden="true">◆</span>
          <span>
            <strong>{activePresentation.modeTitle}</strong>
            {activePresentation.modeDetail}
          </span>
        </div>
      </header>

      {scenariosQuery.isPending ? (
        <section className="demo-page__state" role="status">
          <strong>Loading safe demo preset…</strong>
          <p>Waiting for local scenario discovery.</p>
        </section>
      ) : scenariosQuery.isError ? (
        <UnavailableState message="The local scenario catalog could not be verified. Nothing can be submitted." />
      ) : activeScenario === null ? (
        <UnavailableState message="No supported scenario matches its required safe contract. Nothing can be submitted." />
      ) : (
        <>
          {preparedScenarios.length > 1 ? (
            <ScenarioSwitchboard
              scenarios={preparedScenarios}
              activeScenarioId={activeScenario.scenario.id}
              disabled={selectorLocked}
              onSelect={setSelectedScenarioId}
            />
          ) : null}
          <DemoWorkspace
            key={`${activeScenario.scenario.id}:${String(activeScenario.scenario.version)}`}
            prepared={activeScenario}
            onPendingChange={onPendingChange}
          />
        </>
      )}
    </main>
  );
}
