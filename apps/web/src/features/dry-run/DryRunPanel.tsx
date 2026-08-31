import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import type { AgentInstanceDetail } from "../../api/agentInstanceDetail";
import { fetchLocalSession } from "../../api/instanceConfiguration";
import {
  createManualDryRun,
  generateManualDryRunIdempotencyKey,
  ManualDryRunRequestError,
  type ManualDryRunExecutionMode,
  type ManualDryRunReceipt,
} from "../../api/manualDryRun";
import { mapDryRunFieldErrors } from "./mapProblemDetails";
import { createSchemaDefaults } from "./schemaDefaults";
import {
  compileInputSchema,
  InputSchemaCompileError,
  type CompiledObjectSchema,
  type SchemaDraftObject,
} from "./schemaModel";
import {
  validateSchemaInput,
  type SchemaValidationIssue,
} from "./schemaValidation";
import { SchemaForm } from "./SchemaForm";
import "./dry-run.css";

const LOCAL_SESSION_QUERY_KEY = ["session", "local"] as const;

interface CompiledInput {
  readonly ok: true;
  readonly schema: CompiledObjectSchema;
}

interface UnsupportedInput {
  readonly ok: false;
  readonly code: string;
  readonly pointer: string;
}

type InputCompilation = CompiledInput | UnsupportedInput;

export interface DryRunPanelProps {
  readonly detail: AgentInstanceDetail;
  readonly onDirtyChange: (dirty: boolean) => void;
  readonly onRuntimeMayHaveChanged: () => Promise<void>;
}

function compile(raw: unknown): InputCompilation {
  try {
    return { ok: true, schema: compileInputSchema(raw) };
  } catch (error) {
    if (error instanceof InputSchemaCompileError) {
      return { ok: false, code: error.code, pointer: error.pointer };
    }
    return {
      ok: false,
      code: "schema_compile_failed",
      pointer: "/input",
    };
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function messageFrom(error: unknown): string {
  return error instanceof Error && error.message.length > 0
    ? error.message
    : "The local API could not accept this request.";
}

function manualGate(detail: AgentInstanceDetail): string | null {
  if (!detail.instance.enabled) {
    return "Dry runs are unavailable while this deployment is disabled.";
  }
  if (!detail.template.supportedTriggerTypes.includes("manual")) {
    return "This template does not support manual dry runs.";
  }
  const manualBinding = detail.instance.triggerBindings.find(
    (binding) => binding.type === "manual",
  );
  if (manualBinding !== undefined && !manualBinding.enabled) {
    return "Dry runs are unavailable while the manual trigger is disabled.";
  }
  return null;
}

function Receipt({
  receipt,
  refreshWarning,
}: {
  readonly receipt: ManualDryRunReceipt;
  readonly refreshWarning: boolean;
}): React.JSX.Element {
  return (
    <section
      className="dry-run__receipt"
      aria-labelledby="dry-run-receipt-title"
      aria-live="polite"
      role="status"
    >
      <h4 id="dry-run-receipt-title">Dry run accepted</h4>
      <p>
        The API accepted this request for asynchronous processing. This receipt
        does not report run progress.
      </p>
      <dl>
        <dt>Status</dt>
        <dd>{receipt.status}</dd>
        <dt>Receipt</dt>
        <dd>{receipt.disposition}</dd>
        <dt>Mode</dt>
        <dd>{receipt.executionMode}</dd>
        <dt>Work ID</dt>
        <dd>{receipt.workId}</dd>
        <dt>Run ID</dt>
        <dd>{receipt.runId}</dd>
      </dl>
      <a href={`/runs/${encodeURIComponent(receipt.runId)}`}>
        Open accepted run resource
      </a>
      {refreshWarning ? (
        <p className="dry-run__refresh-warning">
          The request was accepted, but refreshed inspector status is not
          available yet.
        </p>
      ) : null}
    </section>
  );
}

export function DryRunPanel({
  detail,
  onDirtyChange,
  onRuntimeMayHaveChanged,
}: DryRunPanelProps): React.JSX.Element {
  const compilation = useMemo(
    () => compile(detail.inputSchema),
    [detail.inputSchema],
  );
  const [draft, setDraft] = useState<SchemaDraftObject>(() =>
    compilation.ok ? createSchemaDefaults(compilation.schema) : {},
  );
  const [executionMode, setExecutionMode] =
    useState<ManualDryRunExecutionMode>("dry_run");
  const [issues, setIssues] = useState<readonly SchemaValidationIssue[]>([]);
  const [validationRevision, setValidationRevision] = useState(0);
  const [pending, setPending] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [receipt, setReceipt] = useState<ManualDryRunReceipt | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [abortNotice, setAbortNotice] = useState<string | null>(null);
  const [refreshWarning, setRefreshWarning] = useState(false);
  const retryTokenRef = useRef<{
    readonly key: string;
    readonly executionMode: ManualDryRunExecutionMode;
  } | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const refreshRevisionRef = useRef(0);
  const mountedRef = useRef(true);
  const sessionQuery = useQuery({
    queryKey: LOCAL_SESSION_QUERY_KEY,
    queryFn: ({ signal }) => fetchLocalSession(signal),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });
  const canOperate = sessionQuery.data?.roles.includes("operator") ?? false;
  const mockAvailable = sessionQuery.data?.connectorMode === "mock";
  const effectiveExecutionMode = mockAvailable ? executionMode : "dry_run";

  useEffect(() => {
    onDirtyChange(dirty);
    return () => {
      if (dirty) onDirtyChange(false);
    };
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      refreshRevisionRef.current += 1;
      controllerRef.current?.abort();
    };
  }, []);

  const clearAttemptFeedback = (): void => {
    refreshRevisionRef.current += 1;
    setIssues([]);
    setRequestError(null);
    setAbortNotice(null);
    setReceipt(null);
    setRefreshWarning(false);
  };

  const updateDraft = (nextDraft: SchemaDraftObject): void => {
    retryTokenRef.current = null;
    setDraft(nextDraft);
    setDirty(true);
    clearAttemptFeedback();
  };

  const updateExecutionMode = (mode: ManualDryRunExecutionMode): void => {
    if (mode === executionMode || (mode === "mock_execute" && !mockAvailable)) {
      return;
    }
    retryTokenRef.current = null;
    setExecutionMode(mode);
    setDirty(true);
    clearAttemptFeedback();
  };

  const refreshRuntime = async (): Promise<boolean> => {
    try {
      await onRuntimeMayHaveChanged();
      return false;
    } catch {
      return true;
    }
  };

  const refreshRuntimeInBackground = (): void => {
    const revision = refreshRevisionRef.current + 1;
    refreshRevisionRef.current = revision;
    void refreshRuntime().then((refreshFailed) => {
      if (!mountedRef.current || refreshRevisionRef.current !== revision)
        return;
      setRefreshWarning(refreshFailed);
    });
  };

  const submit = async (): Promise<void> => {
    if (
      !compilation.ok ||
      pending ||
      !canOperate ||
      manualGate(detail) !== null
    ) {
      return;
    }
    const validation = validateSchemaInput(compilation.schema, draft);
    if (!validation.ok) {
      setIssues(validation.issues);
      setValidationRevision((revision) => revision + 1);
      setDirty(true);
      setRequestError(null);
      setAbortNotice(null);
      return;
    }

    let idempotencyKey: string;
    try {
      idempotencyKey =
        retryTokenRef.current?.executionMode === effectiveExecutionMode
          ? retryTokenRef.current.key
          : generateManualDryRunIdempotencyKey();
    } catch (error) {
      setDirty(true);
      setRequestError(messageFrom(error));
      setAbortNotice(null);
      return;
    }
    retryTokenRef.current = {
      key: idempotencyKey,
      executionMode: effectiveExecutionMode,
    };
    const controller = new AbortController();
    controllerRef.current = controller;
    refreshRevisionRef.current += 1;
    setPending(true);
    setDirty(true);
    setIssues([]);
    setRequestError(null);
    setAbortNotice(null);
    setReceipt(null);
    setRefreshWarning(false);

    try {
      const accepted = await createManualDryRun({
        instanceId: detail.instance.id,
        input: validation.input,
        executionMode: effectiveExecutionMode,
        idempotencyKey,
        signal: controller.signal,
      });
      if (!mountedRef.current) return;
      retryTokenRef.current = null;
      controllerRef.current = null;
      setDraft(createSchemaDefaults(compilation.schema));
      setPending(false);
      setDirty(false);
      setReceipt(accepted);
      refreshRuntimeInBackground();
    } catch (error) {
      if (isAbortError(error)) {
        if (!mountedRef.current) return;
        controllerRef.current = null;
        setPending(false);
        setAbortNotice(
          "Stopped waiting for the response. The server may already have accepted this request. Retrying unchanged input uses the same retry key.",
        );
        refreshRuntimeInBackground();
        return;
      }
      if (!mountedRef.current) return;
      controllerRef.current = null;
      setPending(false);
      if (error instanceof ManualDryRunRequestError) {
        const mapped = mapDryRunFieldErrors(
          compilation.schema,
          error.fieldErrors,
        );
        setIssues(mapped);
        if (mapped.length > 0) {
          setValidationRevision((revision) => revision + 1);
        }
      }
      setRequestError(messageFrom(error));
    }
  };

  const gate = manualGate(detail);

  return (
    <section
      className="agent-inspector__section dry-run"
      aria-labelledby="dry-run-title"
    >
      <div className="dry-run__heading">
        <div>
          <h3 id="dry-run-title">Manual dry run</h3>
          <p>
            Build input from the template schema and request asynchronous local
            admission.
          </p>
        </div>
        <span className="dry-run__memory-badge">Memory only</span>
      </div>

      {detail.template.operationClassification === "mutating" ? (
        <p className="dry-run__approval-note">
          This template can propose external changes. Each external action still
          requires its configured approval before dispatch; this request never
          approves or dispatches it.
        </p>
      ) : null}

      {sessionQuery.isPending ? (
        <p className="dry-run__readonly" aria-live="polite">
          Checking operator access…
        </p>
      ) : null}
      {sessionQuery.isError ? (
        <p className="dry-run__error" role="alert">
          Operator access could not be verified. Try again when the local API is
          available.
        </p>
      ) : null}
      {sessionQuery.data !== undefined && !canOperate ? (
        <p className="dry-run__readonly">
          Read-only. This session does not include the operator role.
        </p>
      ) : null}
      {sessionQuery.data !== undefined && canOperate && gate !== null ? (
        <p className="dry-run__readonly">{gate}</p>
      ) : null}
      {sessionQuery.data !== undefined &&
      canOperate &&
      gate === null &&
      !compilation.ok ? (
        <div className="dry-run__error" role="alert">
          <strong>This input schema cannot be rendered safely.</strong>
          <p>
            {compilation.code} at {compilation.pointer}
          </p>
        </div>
      ) : null}

      {requestError === null ? null : (
        <p className="dry-run__error" role="alert">
          {requestError}
        </p>
      )}
      {abortNotice === null ? null : (
        <p className="dry-run__notice" role="status">
          {abortNotice}
        </p>
      )}
      {abortNotice !== null && refreshWarning ? (
        <p className="dry-run__refresh-warning">
          Refreshed inspector status is not available yet.
        </p>
      ) : null}
      {receipt === null ? null : (
        <Receipt receipt={receipt} refreshWarning={refreshWarning} />
      )}

      {sessionQuery.data !== undefined &&
      canOperate &&
      gate === null &&
      compilation.ok ? (
        <SchemaForm
          schema={compilation.schema}
          draft={draft}
          issues={issues}
          validationRevision={validationRevision}
          executionMode={effectiveExecutionMode}
          mockAvailable={mockAvailable}
          pending={pending}
          onDraftChange={updateDraft}
          onExecutionModeChange={updateExecutionMode}
          onSubmit={() => void submit()}
          onStopWaiting={() => controllerRef.current?.abort()}
        />
      ) : null}
    </section>
  );
}
