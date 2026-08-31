import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

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

interface ScenarioPresentation {
  readonly scenarioId:
    typeof SOCIAL_DRAFT_SCENARIO_ID | typeof BLOG_CONTENT_REVIEW_SCENARIO_ID;
  readonly templateId: string;
  readonly instanceId: string;
  readonly displayName: string;
  readonly description: string;
  readonly inputSchema: DemoJsonObject;
  readonly preset: DemoJsonObject;
  readonly safeSubmitVerb: string;
  readonly eyebrow: string;
  readonly pageTitle: string;
  readonly pageDescription: string;
  readonly modeDetail: string;
  readonly formId: string;
  readonly formLabel: string;
  readonly guardrailBadges: readonly string[];
  readonly selectedTemplateLabel: string;
  readonly selectedInstanceLabel: string;
  readonly boundaryNote: string | null;
  readonly receiptTitle: string;
  readonly receiptDescription: string;
}

const SOCIAL_PRESENTATION: ScenarioPresentation = Object.freeze({
  scenarioId: SOCIAL_DRAFT_SCENARIO_ID,
  templateId: SOCIAL_DRAFT_TEMPLATE_ID,
  instanceId: SOCIAL_DRAFT_INSTANCE_ID,
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
  modeDetail: "No connector delivery",
  formId: "demo-social-draft-form",
  formLabel: "Social draft demo preset",
  guardrailBadges: ["Read-only", "0 external writes", "No approval required"],
  selectedTemplateLabel: "Selected Social template",
  selectedInstanceLabel: "Selected Social instance",
  boundaryNote: null,
  receiptTitle: "Draft run accepted",
  receiptDescription:
    "Durable intake created the work receipt. Follow the run for timeline progress and the eventual draft artifact.",
});

const BLOG_PRESENTATION: ScenarioPresentation = Object.freeze({
  scenarioId: BLOG_CONTENT_REVIEW_SCENARIO_ID,
  templateId: BLOG_CONTENT_REVIEW_TEMPLATE_ID,
  instanceId: BLOG_CONTENT_REVIEW_INSTANCE_ID,
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
  modeDetail: "No crawling or CMS changes",
  formId: "demo-blog-content-review-form",
  formLabel: "Blog & SEO content review preset",
  guardrailBadges: [
    "Read-only",
    "0 external writes",
    "No approval required",
    "No crawling or CMS actions",
  ],
  selectedTemplateLabel: "Selected Blog & SEO template",
  selectedInstanceLabel: "Selected Blog & SEO instance",
  boundaryNote:
    "Only supplied metadata is reviewed. The canonical URL is provenance text and is never fetched.",
  receiptTitle: "Review run accepted",
  receiptDescription:
    "Durable intake created the work receipt. Follow the run for timeline progress and the eventual advisory content-review artifact.",
});

const SUPPORTED_PRESENTATIONS = [
  SOCIAL_PRESENTATION,
  BLOG_PRESENTATION,
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
  const selected = scenario.selectedAgents[0];
  return (
    scenario.id === presentation.scenarioId &&
    scenario.version === 1 &&
    scenario.workflowId === presentation.scenarioId &&
    scenario.displayName === presentation.displayName &&
    scenario.description === presentation.description &&
    scenario.effect === "read_only" &&
    scenario.safeSubmitVerb === presentation.safeSubmitVerb &&
    exactJson(scenario.inputSchema, presentation.inputSchema) &&
    exactJson(scenario.preset, presentation.preset) &&
    scenario.selectedAgents.length === 1 &&
    selected?.templateId === presentation.templateId &&
    selected.instanceId === presentation.instanceId &&
    scenario.expected.statePath.length ===
      DIRECT_COMPLETION_STATE_PATH.length &&
    scenario.expected.statePath.every(
      (state, index) => state === DIRECT_COMPLETION_STATE_PATH[index],
    ) &&
    scenario.expected.modelCalls === 1 &&
    scenario.expected.connectorCalls === 0 &&
    scenario.expected.externalActions === 0 &&
    scenario.expected.approvals === 0 &&
    scenario.expected.externalWrites === 0
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
      className="demo-card demo-safety"
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
      <div className="demo-safety__agent">
        <span>{presentation.selectedTemplateLabel}</span>
        <code>{scenario.selectedAgents[0]?.templateId}</code>
        <span>{presentation.selectedInstanceLabel}</span>
        <code>{scenario.selectedAgents[0]?.instanceId}</code>
      </div>
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
        instanceId: scenario.selectedAgents[0]?.instanceId ?? "",
        overrides: validation.input,
        idempotencyKey,
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
        <div className="demo-page__mode" aria-label="Demo execution boundary">
          <span aria-hidden="true">◆</span>
          <span>
            <strong>Deterministic mock mode</strong>
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
