import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

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
const SOCIAL_DRAFT_WORKFLOW_ID = SOCIAL_DRAFT_SCENARIO_ID;
const SOCIAL_DRAFT_SCHEMA_ID =
  "schema.demo.social-media.content-draft.input.v1";
const SOCIAL_STATE_PATH = [
  "received",
  "validated",
  "planned",
  "executing",
  "completed",
] as const;

interface PreparedScenario {
  readonly scenario: DemoScenario;
  readonly schema: CompiledObjectSchema;
  readonly preset: SchemaDraftObject;
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

function isExactSocialContract(scenario: DemoScenario): boolean {
  const selected = scenario.selectedAgents[0];
  return (
    scenario.id === SOCIAL_DRAFT_SCENARIO_ID &&
    scenario.version === 1 &&
    scenario.workflowId === SOCIAL_DRAFT_WORKFLOW_ID &&
    scenario.effect === "read_only" &&
    scenario.safeSubmitVerb === "Create draft" &&
    scenario.inputSchema.$id === SOCIAL_DRAFT_SCHEMA_ID &&
    scenario.selectedAgents.length === 1 &&
    selected?.templateId === SOCIAL_DRAFT_TEMPLATE_ID &&
    selected.instanceId === SOCIAL_DRAFT_INSTANCE_ID &&
    scenario.expected.statePath.length === SOCIAL_STATE_PATH.length &&
    scenario.expected.statePath.every(
      (state, index) => state === SOCIAL_STATE_PATH[index],
    ) &&
    scenario.expected.modelCalls === 1 &&
    scenario.expected.connectorCalls === 0 &&
    scenario.expected.externalActions === 0 &&
    scenario.expected.approvals === 0 &&
    scenario.expected.externalWrites === 0
  );
}

function prepareScenario(scenario: DemoScenario): Preparation {
  if (!isExactSocialContract(scenario)) return { ok: false };
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
    : "The local API could not accept this demo draft.";
}

function SafetyFacts({
  scenario,
}: {
  readonly scenario: DemoScenario;
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
        <li>Read-only</li>
        <li>0 external writes</li>
        <li>No approval required</li>
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
        <span>Selected Social template</span>
        <code>{scenario.selectedAgents[0]?.templateId}</code>
        <span>Selected Social instance</span>
        <code>{scenario.selectedAgents[0]?.instanceId}</code>
      </div>
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
}: {
  readonly receipt: DemoScenarioRunReceipt;
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
        <h3 id="demo-receipt-title">Draft run accepted</h3>
        <p>
          Durable intake created the work receipt. Follow the run for timeline
          progress and the eventual draft artifact.
        </p>
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
}: {
  readonly prepared: PreparedScenario;
}): React.JSX.Element {
  const { scenario, schema, preset } = prepared;
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
    };
  }, []);

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
      setReceipt(accepted);
    } catch (error) {
      if (!mountedRef.current) return;
      controllerRef.current = null;
      setPending(false);
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
      <SafetyFacts scenario={scenario} />
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
        {receipt === null ? null : <AcceptedReceipt receipt={receipt} />}
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

export function DemosPage(): React.JSX.Element {
  const scenariosQuery = useQuery({
    queryKey: DEMO_SCENARIOS_QUERY_KEY,
    queryFn: ({ signal }) => fetchDemoScenarios(signal),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });
  const preparation = useMemo<Preparation | null>(() => {
    const scenario = scenariosQuery.data?.find(
      (item) => item.id === SOCIAL_DRAFT_SCENARIO_ID,
    );
    return scenario === undefined ? null : prepareScenario(scenario);
  }, [scenariosQuery.data]);

  return (
    <main className="demo-page" aria-labelledby="demo-page-title">
      <header className="demo-page__header">
        <div>
          <p className="eyebrow">DEMO-01 · Social workflow</p>
          <h1 id="demo-page-title">Social idea to draft artifact</h1>
          <p>
            Start from an API-declared safe preset, admit one deterministic mock
            run, and follow its durable timeline to the draft artifact.
          </p>
        </div>
        <div className="demo-page__mode" aria-label="Demo execution boundary">
          <span aria-hidden="true">◆</span>
          <span>
            <strong>Deterministic mock mode</strong>
            No connector delivery
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
      ) : preparation === null ? (
        <UnavailableState message="The Social draft scenario is not present in the verified catalog. Nothing can be submitted." />
      ) : !preparation.ok ? (
        <UnavailableState message="The Social draft scenario does not match the required safe contract. Nothing can be submitted." />
      ) : (
        <DemoWorkspace
          key={`${preparation.value.scenario.id}:${String(preparation.value.scenario.version)}`}
          prepared={preparation.value}
        />
      )}
    </main>
  );
}
