import { ArrayField } from "./ArrayField";
import { FieldError } from "./FieldError";
import { ObjectField } from "./ObjectField";
import {
  fieldDescriptionIdForPointer,
  fieldErrorIdForPointer,
  fieldHelpIdForPointer,
  fieldIdForPointer,
  fieldSensitiveNoteIdForPointer,
  fieldUriWarningIdForPointer,
} from "./schemaFieldIds";
import type { CompiledSchema } from "./schemaModel";
import type { SchemaValidationIssue } from "./schemaValidation";

export interface SchemaFieldProps {
  readonly schema: CompiledSchema;
  readonly pointer: string;
  readonly value: unknown;
  readonly required: boolean;
  readonly issues: readonly SchemaValidationIssue[];
  readonly formId: string;
  readonly disabled: boolean;
  readonly onChange: (value: unknown) => void;
}

function displayValue(value: unknown): string {
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : "";
}

function inputType(
  schema: Extract<CompiledSchema, { kind: "string" }>,
): "date" | "email" | "text" | "url" {
  switch (schema.format) {
    case "date":
      return "date";
    case "email":
      return "email";
    case "uri":
      return "url";
    case "date-time":
    case null:
      return "text";
  }
}

function PrimitiveField({
  schema,
  pointer,
  value,
  required,
  issues,
  formId,
  disabled,
  onChange,
}: SchemaFieldProps & {
  readonly schema: Exclude<
    CompiledSchema,
    { kind: "array" } | { kind: "object" }
  >;
}): React.JSX.Element {
  const messages = issues
    .filter((issue) => issue.pointer === pointer)
    .map((issue) => issue.message);
  const fieldId = fieldIdForPointer(formId, pointer);
  const errorId = fieldErrorIdForPointer(formId, pointer);
  const descriptionId = fieldDescriptionIdForPointer(formId, pointer);
  const helpId = fieldHelpIdForPointer(formId, pointer);
  const uriWarningId = fieldUriWarningIdForPointer(formId, pointer);
  const sensitiveNoteId = fieldSensitiveNoteIdForPointer(formId, pointer);
  const describedByIds = [
    schema.description === null ? null : descriptionId,
    schema.ui.help === null ? null : helpId,
    schema.kind === "string" && schema.format === "uri" ? uriWarningId : null,
    schema.sensitive ? sensitiveNoteId : null,
    messages.length === 0 ? null : errorId,
  ].filter((id): id is string => id !== null);
  const describedBy =
    describedByIds.length === 0 ? undefined : describedByIds.join(" ");
  const accessibilityProps = {
    "aria-describedby": describedBy,
    "aria-errormessage": messages.length > 0 ? errorId : undefined,
    "aria-invalid": messages.length > 0 ? (true as const) : undefined,
  };

  let control: React.JSX.Element;
  if (schema.kind === "boolean") {
    control = (
      <select
        {...accessibilityProps}
        id={fieldId}
        value={typeof value === "boolean" ? String(value) : ""}
        required={required}
        disabled={disabled}
        autoComplete={schema.sensitive ? "off" : undefined}
        onChange={(event) => {
          const next = event.currentTarget.value;
          onChange(next === "" ? undefined : next === "true");
        }}
      >
        <option value="">Select true or false</option>
        {(schema.enumValues ?? [true, false]).map((option) => (
          <option value={String(option)} key={String(option)}>
            {String(option)}
          </option>
        ))}
      </select>
    );
  } else if (schema.enumValues !== null) {
    control = (
      <select
        {...accessibilityProps}
        id={fieldId}
        value={displayValue(value)}
        required={required}
        disabled={disabled}
        autoComplete={schema.sensitive ? "off" : undefined}
        onChange={(event) => onChange(event.currentTarget.value)}
      >
        <option value="">Select an option</option>
        {schema.enumValues.map((option) => (
          <option value={String(option)} key={String(option)}>
            {String(option)}
          </option>
        ))}
      </select>
    );
  } else if (
    schema.kind === "string" &&
    (schema.ui.control === "textarea" || schema.maxLength > 2_000)
  ) {
    control = (
      <textarea
        {...accessibilityProps}
        id={fieldId}
        value={displayValue(value)}
        required={required}
        disabled={disabled}
        minLength={schema.minLength}
        maxLength={schema.maxLength}
        autoComplete={schema.sensitive ? "off" : undefined}
        spellCheck={schema.sensitive ? false : undefined}
        placeholder={schema.sensitive ? undefined : schema.examples[0]}
        onChange={(event) => onChange(event.currentTarget.value)}
      />
    );
  } else if (schema.kind === "string") {
    const example = schema.sensitive ? undefined : schema.examples[0];
    control = (
      <input
        {...accessibilityProps}
        id={fieldId}
        type={inputType(schema)}
        value={displayValue(value)}
        required={required}
        disabled={disabled}
        minLength={schema.minLength}
        maxLength={schema.maxLength}
        pattern={schema.pattern ?? undefined}
        placeholder={
          example ??
          (schema.format === "date-time" ? "YYYY-MM-DDTHH:mm:ssZ" : undefined)
        }
        autoComplete={schema.sensitive ? "off" : undefined}
        spellCheck={schema.sensitive ? false : undefined}
        onChange={(event) => onChange(event.currentTarget.value)}
      />
    );
  } else {
    const example = schema.sensitive ? undefined : schema.examples[0];
    control = (
      <input
        {...accessibilityProps}
        id={fieldId}
        type="number"
        value={displayValue(value)}
        required={required}
        disabled={disabled}
        step={schema.kind === "integer" ? 1 : "any"}
        min={
          schema.minimum === null || schema.exclusiveMinimum
            ? undefined
            : schema.minimum
        }
        max={
          schema.maximum === null || schema.exclusiveMaximum
            ? undefined
            : schema.maximum
        }
        autoComplete={schema.sensitive ? "off" : undefined}
        placeholder={example === undefined ? undefined : String(example)}
        onChange={(event) => onChange(event.currentTarget.value)}
      />
    );
  }

  return (
    <div
      className={`schema-form__field${schema.sensitive ? " is-sensitive" : ""}`}
    >
      <label htmlFor={fieldId}>
        {schema.title}
        {required ? <span aria-hidden="true"> *</span> : null}
        {required ? <span className="visually-hidden"> (required)</span> : null}
      </label>
      {control}
      {schema.description === null ? null : (
        <p id={descriptionId} className="schema-form__description">
          {schema.description}
        </p>
      )}
      {schema.ui.help === null ? null : (
        <p id={helpId} className="schema-form__help">
          {schema.ui.help}
        </p>
      )}
      {schema.kind === "string" && schema.format === "uri" ? (
        <p id={uriWarningId} className="schema-form__help">
          A valid address is checked as text only. This form does not fetch it.
        </p>
      ) : null}
      {schema.sensitive ? (
        <p id={sensitiveNoteId} className="schema-form__sensitive-note">
          Sensitive value. Kept only in this open form.
        </p>
      ) : null}
      <FieldError id={errorId} messages={messages} />
    </div>
  );
}

export function SchemaField(props: SchemaFieldProps): React.JSX.Element {
  if (props.schema.kind === "object") {
    return <ObjectField {...props} schema={props.schema} />;
  }
  if (props.schema.kind === "array") {
    return <ArrayField {...props} schema={props.schema} />;
  }
  return <PrimitiveField {...props} schema={props.schema} />;
}
