import { useEffect, useRef } from "react";

import type { ManualDryRunExecutionMode } from "../../api/manualDryRun";
import { SchemaField } from "./SchemaField";
import { escapeJsonPointerSegment, fieldIdForPointer } from "./schemaFieldIds";
import type { CompiledObjectSchema, SchemaDraftObject } from "./schemaModel";
import type { SchemaValidationIssue } from "./schemaValidation";

export interface SchemaFormProps {
  readonly schema: CompiledObjectSchema;
  readonly draft: SchemaDraftObject;
  readonly issues: readonly SchemaValidationIssue[];
  readonly validationRevision: number;
  readonly executionMode: ManualDryRunExecutionMode;
  readonly mockAvailable: boolean;
  readonly pending: boolean;
  readonly onDraftChange: (draft: SchemaDraftObject) => void;
  readonly onExecutionModeChange: (mode: ManualDryRunExecutionMode) => void;
  readonly onSubmit: () => void;
  readonly onStopWaiting: () => void;
  readonly id?: string;
}

function distinctIssues(
  issues: readonly SchemaValidationIssue[],
): readonly SchemaValidationIssue[] {
  const seen = new Set<string>();
  return issues.filter((issue) => {
    const key = `${issue.pointer}\u0000${issue.message}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function SchemaForm({
  schema,
  draft,
  issues,
  validationRevision,
  executionMode,
  mockAvailable,
  pending,
  onDraftChange,
  onExecutionModeChange,
  onSubmit,
  onStopWaiting,
  id = "manual-dry-run-form",
}: SchemaFormProps): React.JSX.Element {
  const summaryRef = useRef<HTMLDivElement>(null);
  const visibleIssues = distinctIssues(issues);

  useEffect(() => {
    if (validationRevision <= 0 || visibleIssues.length === 0) return;
    summaryRef.current?.focus();
  }, [validationRevision, visibleIssues.length]);

  const focusIssue = (pointer: string): void => {
    document
      .getElementById(
        pointer === schema.pointer ? id : fieldIdForPointer(id, pointer),
      )
      ?.focus();
  };
  const submitLabel =
    executionMode === "mock_execute" ? "Run with mocks" : "Create dry run";

  return (
    <form
      id={id}
      className="schema-form"
      aria-label="Manual dry-run input"
      aria-busy={pending}
      tabIndex={-1}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      {visibleIssues.length === 0 ? null : (
        <div
          ref={summaryRef}
          className="schema-form__error-summary"
          role="alert"
          tabIndex={-1}
        >
          <strong>Check the highlighted input</strong>
          <p>
            {visibleIssues.length === 1
              ? "There is one input error."
              : `There are ${String(visibleIssues.length)} input errors.`}
          </p>
          <ul>
            {visibleIssues.map((issue, index) => (
              <li key={`${issue.pointer}-${issue.code}-${String(index)}`}>
                <button type="button" onClick={() => focusIssue(issue.pointer)}>
                  {issue.message}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="schema-form__fields">
        {schema.properties.map((property) => {
          const pointer = `${schema.pointer}/${escapeJsonPointerSegment(property.name)}`;
          return (
            <SchemaField
              key={property.name}
              schema={property.schema}
              pointer={pointer}
              value={draft[property.name]}
              required={property.required}
              issues={issues}
              formId={id}
              disabled={pending}
              onChange={(nextValue) => {
                onDraftChange({ ...draft, [property.name]: nextValue });
              }}
            />
          );
        })}
      </div>

      <fieldset className="schema-form__mode" disabled={pending}>
        <legend>Execution mode</legend>
        <label>
          <input
            type="radio"
            name={`${id}-execution-mode`}
            value="dry_run"
            checked={executionMode === "dry_run"}
            onChange={() => onExecutionModeChange("dry_run")}
          />
          <span>
            <strong>Dry run</strong>
            Validate and admit work with external effects disabled.
          </span>
        </label>
        {mockAvailable ? (
          <label>
            <input
              type="radio"
              name={`${id}-execution-mode`}
              value="mock_execute"
              checked={executionMode === "mock_execute"}
              onChange={() => onExecutionModeChange("mock_execute")}
            />
            <span>
              <strong>Mock execution</strong>
              Admit work using the configured local mock session.
            </span>
          </label>
        ) : null}
      </fieldset>

      <div className="schema-form__actions">
        {pending ? (
          <button
            type="button"
            className="schema-form__button is-secondary"
            onClick={onStopWaiting}
          >
            Stop waiting
          </button>
        ) : null}
        <button
          type="submit"
          className="schema-form__button is-primary"
          disabled={pending}
        >
          {pending ? "Waiting for acceptance…" : submitLabel}
        </button>
      </div>
    </form>
  );
}
