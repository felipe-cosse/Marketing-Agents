import type { CompiledObjectSchema } from "./schemaModel";
import type { SchemaValidationIssue } from "./schemaValidation";
import { asDraftObject } from "./fieldSupport";
import { FieldError } from "./FieldError";
import { SchemaField } from "./SchemaField";
import {
  escapeJsonPointerSegment,
  fieldDescriptionIdForPointer,
  fieldErrorIdForPointer,
  fieldHelpIdForPointer,
  fieldIdForPointer,
} from "./schemaFieldIds";

export interface ObjectFieldProps {
  readonly schema: CompiledObjectSchema;
  readonly pointer: string;
  readonly value: unknown;
  readonly required: boolean;
  readonly issues: readonly SchemaValidationIssue[];
  readonly formId: string;
  readonly disabled: boolean;
  readonly onChange: (value: unknown) => void;
}

function describedBy(
  schema: CompiledObjectSchema,
  formId: string,
  pointer: string,
  hasError: boolean,
): string | undefined {
  const ids = [];
  if (schema.description !== null) {
    ids.push(fieldDescriptionIdForPointer(formId, pointer));
  }
  if (schema.ui.help !== null) ids.push(fieldHelpIdForPointer(formId, pointer));
  if (hasError) ids.push(fieldErrorIdForPointer(formId, pointer));
  return ids.length === 0 ? undefined : ids.join(" ");
}

export function ObjectField({
  schema,
  pointer,
  value,
  required,
  issues,
  formId,
  disabled,
  onChange,
}: ObjectFieldProps): React.JSX.Element {
  const objectValue = asDraftObject(value);
  const messages = issues
    .filter((issue) => issue.pointer === pointer)
    .map((issue) => issue.message);
  const fieldId = fieldIdForPointer(formId, pointer);
  const errorId = fieldErrorIdForPointer(formId, pointer);

  return (
    <fieldset
      id={fieldId}
      className="schema-form__object"
      aria-describedby={describedBy(
        schema,
        formId,
        pointer,
        messages.length > 0,
      )}
      aria-invalid={messages.length > 0 ? "true" : undefined}
      aria-errormessage={messages.length > 0 ? errorId : undefined}
      tabIndex={messages.length > 0 ? -1 : undefined}
    >
      <legend>
        {schema.title}
        {required ? <span aria-hidden="true"> *</span> : null}
        {required ? <span className="visually-hidden"> (required)</span> : null}
      </legend>
      {schema.description === null ? null : (
        <p
          id={fieldDescriptionIdForPointer(formId, pointer)}
          className="schema-form__description"
        >
          {schema.description}
        </p>
      )}
      {schema.ui.help === null ? null : (
        <p
          id={fieldHelpIdForPointer(formId, pointer)}
          className="schema-form__help"
        >
          {schema.ui.help}
        </p>
      )}
      <FieldError id={errorId} messages={messages} />
      {!required && value !== undefined ? (
        <button
          type="button"
          className="schema-form__button is-omit"
          aria-label={`Omit ${schema.title} from dry-run input`}
          disabled={disabled}
          onClick={() => onChange(undefined)}
        >
          Omit {schema.title}
        </button>
      ) : null}
      <div className="schema-form__object-fields">
        {schema.properties.map((property) => {
          const childPointer = `${pointer}/${escapeJsonPointerSegment(property.name)}`;
          return (
            <SchemaField
              key={property.name}
              schema={property.schema}
              pointer={childPointer}
              value={objectValue[property.name]}
              required={property.required}
              issues={issues}
              formId={formId}
              disabled={disabled}
              onChange={(nextValue) => {
                onChange({ ...objectValue, [property.name]: nextValue });
              }}
            />
          );
        })}
      </div>
    </fieldset>
  );
}
