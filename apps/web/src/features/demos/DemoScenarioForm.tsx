import { useEffect, useRef } from "react";

import { SchemaField } from "../dry-run/SchemaField";
import {
  escapeJsonPointerSegment,
  fieldIdForPointer,
} from "../dry-run/schemaFieldIds";
import type {
  CompiledObjectSchema,
  SchemaDraftObject,
} from "../dry-run/schemaModel";
import type { SchemaValidationIssue } from "../dry-run/schemaValidation";

interface DemoScenarioFormProps {
  readonly schema: CompiledObjectSchema;
  readonly draft: SchemaDraftObject;
  readonly issues: readonly SchemaValidationIssue[];
  readonly validationRevision: number;
  readonly pending: boolean;
  readonly onDraftChange: (draft: SchemaDraftObject) => void;
  readonly onSubmit: () => void;
  readonly onStopWaiting: () => void;
  readonly onReset: () => void;
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

export function DemoScenarioForm({
  schema,
  draft,
  issues,
  validationRevision,
  pending,
  onDraftChange,
  onSubmit,
  onStopWaiting,
  onReset,
}: DemoScenarioFormProps): React.JSX.Element {
  const formId = "demo-social-draft-form";
  const summaryRef = useRef<HTMLDivElement>(null);
  const visibleIssues = distinctIssues(issues);

  useEffect(() => {
    if (validationRevision <= 0 || visibleIssues.length === 0) return;
    summaryRef.current?.focus();
  }, [validationRevision, visibleIssues.length]);

  const focusIssue = (pointer: string): void => {
    document
      .getElementById(
        pointer === schema.pointer
          ? formId
          : fieldIdForPointer(formId, pointer),
      )
      ?.focus();
  };

  return (
    <form
      id={formId}
      className="schema-form demo-scenario-form"
      aria-label="Social draft demo preset"
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
          <strong>Check the safe preset</strong>
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
              formId={formId}
              disabled={pending}
              onChange={(nextValue) => {
                onDraftChange({ ...draft, [property.name]: nextValue });
              }}
            />
          );
        })}
      </div>

      <div className="demo-scenario-form__mode" aria-label="Execution mode">
        <span className="demo-scenario-form__mode-icon" aria-hidden="true">
          ◆
        </span>
        <span>
          <strong>Deterministic mock mode</strong>
          The API admits durable dry-run work; the demo model response is fixed
          and connectors stay unused.
        </span>
      </div>

      {pending ? (
        <p className="demo-scenario-form__waiting" role="status">
          Waiting for the durable intake receipt…
        </p>
      ) : null}

      <div className="schema-form__actions demo-scenario-form__actions">
        <button
          type="button"
          className="schema-form__button is-secondary"
          disabled={pending}
          onClick={onReset}
        >
          Reset safe preset
        </button>
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
          Create draft
        </button>
      </div>
    </form>
  );
}
