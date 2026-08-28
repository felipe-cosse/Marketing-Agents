import type { CompiledArraySchema } from "./schemaModel";
import type { SchemaValidationIssue } from "./schemaValidation";
import { asDraftArray, valueForNewArrayItem } from "./fieldSupport";
import { FieldError } from "./FieldError";
import { SchemaField } from "./SchemaField";
import {
  fieldDescriptionIdForPointer,
  fieldErrorIdForPointer,
  fieldHelpIdForPointer,
  fieldIdForPointer,
} from "./schemaFieldIds";

export interface ArrayFieldProps {
  readonly schema: CompiledArraySchema;
  readonly pointer: string;
  readonly value: unknown;
  readonly required: boolean;
  readonly issues: readonly SchemaValidationIssue[];
  readonly formId: string;
  readonly disabled: boolean;
  readonly onChange: (value: unknown) => void;
}

export function ArrayField({
  schema,
  pointer,
  value,
  required,
  issues,
  formId,
  disabled,
  onChange,
}: ArrayFieldProps): React.JSX.Element {
  const items = asDraftArray(value);
  const messages = issues
    .filter((issue) => issue.pointer === pointer)
    .map((issue) => issue.message);
  const fieldId = fieldIdForPointer(formId, pointer);
  const errorId = fieldErrorIdForPointer(formId, pointer);
  const describedByIds = [
    schema.description === null
      ? null
      : fieldDescriptionIdForPointer(formId, pointer),
    schema.ui.help === null ? null : fieldHelpIdForPointer(formId, pointer),
    messages.length === 0 ? null : errorId,
  ].filter((id): id is string => id !== null);

  return (
    <fieldset
      id={fieldId}
      className="schema-form__array"
      aria-describedby={
        describedByIds.length === 0 ? undefined : describedByIds.join(" ")
      }
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

      <div className="schema-form__array-items">
        {items.map((item, index) => {
          const itemPointer = `${pointer}/${String(index)}`;
          return (
            <div className="schema-form__array-item" key={itemPointer}>
              <SchemaField
                schema={schema.items}
                pointer={itemPointer}
                value={item}
                required
                issues={issues}
                formId={formId}
                disabled={disabled}
                onChange={(nextValue) => {
                  const nextItems = [...items];
                  nextItems[index] = nextValue;
                  onChange(nextItems);
                }}
              />
              <button
                type="button"
                className="schema-form__button is-remove"
                disabled={disabled || items.length <= schema.minItems}
                onClick={() => {
                  onChange(items.filter((_, itemIndex) => itemIndex !== index));
                }}
              >
                Remove {schema.items.title} {String(index + 1)}
              </button>
            </div>
          );
        })}
      </div>

      <button
        type="button"
        className="schema-form__button is-add"
        disabled={disabled || items.length >= schema.maxItems}
        onClick={() => {
          onChange([...items, valueForNewArrayItem(schema.items)]);
        }}
      >
        Add {schema.items.title}
      </button>
      <p className="schema-form__limit">
        {String(schema.minItems)}–{String(schema.maxItems)} items allowed.
      </p>
    </fieldset>
  );
}
