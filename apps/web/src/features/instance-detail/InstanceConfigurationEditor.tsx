import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import type { AgentInstanceDetail } from "../../api/agentInstanceDetail";
import {
  fetchInstanceConfiguration,
  fetchInstanceConfigurationSchema,
  fetchLocalSession,
  InstanceConfigurationRequestError,
  serializeInstanceConfigurationPatch,
  updateInstanceConfiguration,
  type ConnectorBindingPatch,
  type InstanceConfigurationPatch,
  type InstanceConfigurationSchema,
  type ScheduledInput,
  type MisfirePolicy,
  type SchedulePatch,
  type SupportedTriggerType,
  type TriggerBindingPatch,
} from "../../api/instanceConfiguration";
import { SchemaField } from "../dry-run/SchemaField";
import {
  compileInputSchema,
  type CompiledObjectSchema,
  type SchemaDraftObject,
} from "../dry-run/schemaModel";
import { escapeJsonPointerSegment } from "../dry-run/schemaFieldIds";
import { validateSchemaInput } from "../dry-run/schemaValidation";
import "./instance-configuration.css";

const LOCAL_SESSION_QUERY_KEY = ["session", "local"] as const;
const EVENT_SOURCE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$/u;

interface TriggerDraft {
  readonly type: SupportedTriggerType;
  readonly configured: boolean;
  readonly enabled: boolean;
  readonly eventSource: string;
}

interface ConnectorDraft {
  readonly configured: boolean;
  readonly bindingId: string;
  readonly enabled: boolean;
}

interface ScheduleDraft {
  readonly enabled: boolean;
  readonly cron: string;
  readonly timezone: string;
  readonly misfirePolicy: MisfirePolicy;
  readonly misfireGraceSeconds: string;
}

interface ConfigurationDraft {
  readonly enabled: boolean;
  readonly variantLabel: string;
  readonly triggerBindings: Readonly<
    Record<SupportedTriggerType, TriggerDraft>
  >;
  readonly connectorBindings: Readonly<Record<string, ConnectorDraft>>;
  readonly schedule: ScheduleDraft;
  readonly scheduledInput: ScheduledInput | null;
}

interface EditableConfiguration {
  readonly enabled: boolean;
  readonly variantLabel: string | null;
  readonly triggerBindings: readonly TriggerBindingPatch[];
  readonly connectorBindings: Readonly<Record<string, ConnectorBindingPatch>>;
  readonly schedule: SchedulePatch | null;
  readonly scheduledInput: ScheduledInput | null;
}

export interface InstanceConfigurationEditorProps {
  readonly detail: AgentInstanceDetail;
  readonly onDirtyChange: (dirty: boolean) => void;
  readonly onSaved: () => Promise<void>;
  readonly onReload: () => Promise<void>;
}

function messageFrom(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.length > 0
    ? error.message
    : fallback;
}

function scheduleDefaults(
  detail: AgentInstanceDetail,
): Omit<ScheduleDraft, "enabled"> {
  const schedule = detail.instance.schedule;
  return {
    cron: schedule?.cron ?? "",
    timezone: schedule?.timezone ?? "UTC",
    misfirePolicy: schedule?.misfirePolicy ?? "run_once",
    misfireGraceSeconds: String(schedule?.misfireGraceSeconds ?? 300),
  };
}

function draftFrom(
  detail: AgentInstanceDetail,
  schema: InstanceConfigurationSchema,
  scheduledInput: ScheduledInput | null,
): ConfigurationDraft {
  const triggerDraft = (type: SupportedTriggerType): TriggerDraft => {
    const current = detail.instance.triggerBindings.find(
      (binding) => binding.type === type,
    );
    return {
      type,
      configured:
        schema.supportedTriggerTypes.includes(type) && current !== undefined,
      enabled: current?.enabled ?? false,
      eventSource: current?.eventSource ?? "",
    };
  };
  const triggerBindings: Record<SupportedTriggerType, TriggerDraft> = {
    manual: triggerDraft("manual"),
    webhook: triggerDraft("webhook"),
    schedule: triggerDraft("schedule"),
  };

  const connectorBindings: Record<string, ConnectorDraft> = {};
  for (const family of schema.connectorFamilies) {
    const current = detail.instance.connectorBindings[family.connectorFamily];
    connectorBindings[family.connectorFamily] = {
      configured: current !== undefined,
      bindingId: current?.bindingId ?? family.bindingIds[0] ?? "",
      enabled: current?.enabled ?? true,
    };
  }

  const scheduleTrigger = triggerBindings.schedule;
  return {
    enabled: detail.instance.enabled,
    variantLabel: detail.instance.variantLabel ?? "",
    triggerBindings,
    connectorBindings,
    scheduledInput,
    schedule: {
      enabled:
        schema.scheduleSupported &&
        scheduleTrigger.configured &&
        scheduleTrigger.enabled &&
        detail.instance.schedule !== null,
      ...scheduleDefaults(detail),
    },
  };
}

function schedulePatchFrom(draft: ScheduleDraft): SchedulePatch {
  return {
    cron: draft.cron.trim(),
    timezone: draft.timezone.trim(),
    misfirePolicy: draft.misfirePolicy,
    misfireGraceSeconds: Number(draft.misfireGraceSeconds),
  };
}

function editableFrom(
  draft: ConfigurationDraft,
  schema: InstanceConfigurationSchema,
): EditableConfiguration {
  const schedule = draft.schedule.enabled
    ? schedulePatchFrom(draft.schedule)
    : null;
  const triggerBindings: TriggerBindingPatch[] = [];

  for (const type of schema.supportedTriggerTypes) {
    const trigger = draft.triggerBindings[type];
    if (!trigger.configured) continue;
    if (type === "manual") {
      triggerBindings.push({ type, enabled: trigger.enabled });
    } else if (type === "webhook") {
      triggerBindings.push({
        type,
        enabled: trigger.enabled,
        eventSource: trigger.eventSource.trim(),
      });
    } else if (schedule === null) {
      triggerBindings.push({ type, enabled: false });
    } else {
      triggerBindings.push({ type, enabled: true, ...schedule });
    }
  }

  const connectorBindings: Record<string, ConnectorBindingPatch> = {};
  for (const option of [...schema.connectorFamilies].sort((left, right) =>
    left.connectorFamily.localeCompare(right.connectorFamily),
  )) {
    const connector = draft.connectorBindings[option.connectorFamily];
    if (!connector?.configured) continue;
    connectorBindings[option.connectorFamily] = {
      connectorFamily: option.connectorFamily,
      bindingId: connector.bindingId,
      enabled: connector.enabled,
    };
  }

  const normalizedVariantLabel = draft.variantLabel.trim().normalize("NFC");
  return {
    enabled: draft.enabled,
    variantLabel:
      normalizedVariantLabel.length === 0 ? null : normalizedVariantLabel,
    triggerBindings,
    connectorBindings,
    schedule,
    scheduledInput: draft.scheduledInput,
  };
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function partialPatch(
  previous: EditableConfiguration,
  next: EditableConfiguration,
): InstanceConfigurationPatch {
  const patch: {
    enabled?: boolean;
    variantLabel?: string | null;
    triggerBindings?: readonly TriggerBindingPatch[];
    connectorBindings?: Readonly<Record<string, ConnectorBindingPatch>>;
    schedule?: SchedulePatch | null;
    scheduledInput?: ScheduledInput | null;
  } = {};
  if (previous.enabled !== next.enabled) patch.enabled = next.enabled;
  if (previous.variantLabel !== next.variantLabel) {
    patch.variantLabel = next.variantLabel;
  }
  if (!sameValue(previous.connectorBindings, next.connectorBindings)) {
    patch.connectorBindings = next.connectorBindings;
  }
  if (!sameValue(previous.scheduledInput, next.scheduledInput)) {
    patch.scheduledInput = next.scheduledInput;
  }
  if (
    !sameValue(previous.triggerBindings, next.triggerBindings) ||
    !sameValue(previous.schedule, next.schedule)
  ) {
    // These fields travel together so an enabled schedule trigger and its
    // separately persisted schedule can never diverge.
    patch.triggerBindings = next.triggerBindings;
    patch.schedule = next.schedule;
  }
  return patch;
}

function validationMessages(
  draft: ConfigurationDraft,
  schema: InstanceConfigurationSchema,
): readonly string[] {
  const messages: string[] = [];
  const variantLabel = draft.variantLabel.trim();
  if (Array.from(variantLabel).length > schema.variantLabelMaxLength) {
    messages.push(
      `Variant label must be ${String(schema.variantLabelMaxLength)} characters or fewer.`,
    );
  }

  const webhook = draft.triggerBindings.webhook;
  if (
    webhook.configured &&
    !EVENT_SOURCE_PATTERN.test(webhook.eventSource.trim())
  ) {
    messages.push(
      "Webhook event source must use 1–100 letters, numbers, dots, colons, underscores, or hyphens.",
    );
  }

  if (draft.schedule.enabled) {
    for (const [label, value] of [
      ["Schedule expression", draft.schedule.cron],
      ["Schedule timezone", draft.schedule.timezone],
    ] as const) {
      const trimmed = value.trim();
      if (trimmed.length === 0 || Array.from(trimmed).length > 100) {
        messages.push(`${label} must contain 1–100 characters.`);
      }
    }
    const grace = Number(draft.schedule.misfireGraceSeconds);
    if (
      !/^\d+$/u.test(draft.schedule.misfireGraceSeconds) ||
      !Number.isSafeInteger(grace) ||
      grace < 0 ||
      grace > 86_400
    ) {
      messages.push(
        "Misfire grace must be a whole number from 0 through 86400 seconds.",
      );
    }
  }
  return messages;
}

function compatibilityIssue(
  detail: AgentInstanceDetail,
  schema: InstanceConfigurationSchema,
): string | null {
  const unsupportedTrigger = detail.instance.triggerBindings.find(
    (trigger) => !schema.supportedTriggerTypes.includes(trigger.type),
  );
  if (unsupportedTrigger !== undefined) {
    return `The current ${unsupportedTrigger.type} trigger is not present in the editable schema.`;
  }
  for (const binding of Object.values(detail.instance.connectorBindings)) {
    const option = schema.connectorFamilies.find(
      (candidate) => candidate.connectorFamily === binding.connectorFamily,
    );
    if (!option?.bindingIds.includes(binding.bindingId)) {
      return `The current ${binding.connectorFamily} connector is not registered in the editable schema.`;
    }
  }
  if (detail.instance.schedule !== null && !schema.scheduleSupported) {
    return "The current schedule is not present in the editable schema.";
  }
  return null;
}

interface ConfigurationFormProps {
  readonly detail: AgentInstanceDetail;
  readonly schema: InstanceConfigurationSchema;
  readonly savedInput: ScheduledInput | null;
  readonly onDirtyChange: (dirty: boolean) => void;
  readonly onCancel: () => void;
  readonly onCommitted: () => Promise<void>;
  readonly onReloaded: () => Promise<void>;
}

function ConfigurationForm({
  detail,
  schema,
  savedInput,
  onDirtyChange,
  onCancel,
  onCommitted,
  onReloaded,
}: ConfigurationFormProps): React.JSX.Element {
  const formRef = useRef<HTMLFormElement>(null);
  const [draft, setDraft] = useState<ConfigurationDraft>(() =>
    draftFrom(detail, schema, savedInput),
  );
  const [baseline] = useState<EditableConfiguration>(() =>
    editableFrom(draftFrom(detail, schema, savedInput), schema),
  );
  const [saving, setSaving] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [committed, setCommitted] = useState(false);
  const [requestError, setRequestError] =
    useState<InstanceConfigurationRequestError | null>(null);
  const [unexpectedError, setUnexpectedError] = useState<string | null>(null);
  useEffect(() => {
    formRef.current
      ?.querySelector<HTMLElement>("input:not(:disabled)")
      ?.focus();
  }, []);
  const editable = useMemo(() => editableFrom(draft, schema), [draft, schema]);
  const patch = useMemo(
    () => partialPatch(baseline, editable),
    [baseline, editable],
  );
  const dirty = !committed && Object.keys(patch).length > 0;
  const inputSchema = useMemo(() => {
    try {
      return compileInputSchema(detail.inputSchema);
    } catch {
      return null;
    }
  }, [detail.inputSchema]);
  const inputValidation = useMemo(
    () =>
      inputSchema === null || draft.scheduledInput === null
        ? null
        : validateSchemaInput(
            inputSchema,
            draft.scheduledInput.input as SchemaDraftObject,
          ),
    [inputSchema, draft.scheduledInput],
  );
  const clientErrors = [
    ...validationMessages(draft, schema),
    ...(draft.schedule.enabled && draft.scheduledInput === null
      ? [
          "Saved input is required before this schedule can execute. Enter the reusable business input below.",
        ]
      : []),
    ...(draft.scheduledInput !== null && inputSchema === null
      ? ["The input schema cannot be edited safely."]
      : []),
    ...(inputValidation?.ok === false
      ? inputValidation.issues.map((issue) => issue.message)
      : []),
  ];

  useEffect(() => {
    onDirtyChange(dirty);
    return () => {
      if (dirty) onDirtyChange(false);
    };
  }, [dirty, onDirtyChange]);

  const updateTrigger = (
    type: SupportedTriggerType,
    update: (current: TriggerDraft) => TriggerDraft,
  ): void => {
    setDraft((current) => ({
      ...current,
      triggerBindings: {
        ...current.triggerBindings,
        [type]: update(current.triggerBindings[type]),
      },
    }));
    setRequestError(null);
    setUnexpectedError(null);
  };

  const setScheduleEnabled = (enabled: boolean): void => {
    setDraft((current) => ({
      ...current,
      triggerBindings: {
        ...current.triggerBindings,
        schedule: {
          ...current.triggerBindings.schedule,
          configured: true,
          enabled,
        },
      },
      schedule: { ...current.schedule, enabled },
    }));
    setRequestError(null);
    setUnexpectedError(null);
  };

  const save = async (
    event: React.SyntheticEvent<HTMLFormElement>,
  ): Promise<void> => {
    event.preventDefault();
    if (!dirty || clientErrors.length > 0 || saving) return;
    setSaving(true);
    setRequestError(null);
    setUnexpectedError(null);
    try {
      const validatedPatch =
        patch.scheduledInput !== undefined &&
        patch.scheduledInput !== null &&
        inputValidation?.ok === true
          ? {
              ...patch,
              scheduledInput: {
                ...patch.scheduledInput,
                input: inputValidation.input,
              },
            }
          : patch;
      serializeInstanceConfigurationPatch(validatedPatch, schema);
      await updateInstanceConfiguration({
        instanceId: detail.instance.id,
        configurationEtag: detail.instance.configurationEtag,
        patch: validatedPatch,
      });
      setCommitted(true);
      await onCommitted();
    } catch (error) {
      if (error instanceof InstanceConfigurationRequestError) {
        setRequestError(error);
      } else {
        setUnexpectedError(
          messageFrom(error, "The configuration could not be saved."),
        );
      }
      setSaving(false);
    }
  };

  const reload = async (): Promise<void> => {
    if (reloading) return;
    setReloading(true);
    setUnexpectedError(null);
    try {
      await onReloaded();
    } catch (error) {
      setUnexpectedError(
        messageFrom(error, "The current configuration could not be reloaded."),
      );
      setReloading(false);
    }
  };

  const conflict = requestError?.status === 409;
  const serverFieldErrors =
    requestError?.status === 422 ? requestError.fieldErrors : [];

  return (
    <form
      ref={formRef}
      className="instance-configuration__form"
      autoComplete="off"
      aria-label="Deployment configuration editor"
      noValidate
      onSubmit={(event) => void save(event)}
    >
      <fieldset className="instance-configuration__group">
        <legend>Instance</legend>
        <label className="instance-configuration__check">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                enabled: event.target.checked,
              }))
            }
          />
          Deployment enabled
        </label>
        <label className="instance-configuration__field">
          <span>Variant label</span>
          <input
            value={draft.variantLabel}
            maxLength={schema.variantLabelMaxLength}
            placeholder="No variant label"
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                variantLabel: event.target.value,
              }))
            }
          />
        </label>
      </fieldset>

      <fieldset className="instance-configuration__group">
        <legend>Trigger bindings</legend>
        {schema.supportedTriggerTypes.map((type) => {
          const trigger = draft.triggerBindings[type];
          const title = type.charAt(0).toUpperCase() + type.slice(1);
          return (
            <div className="instance-configuration__binding" key={type}>
              <strong>{title}</strong>
              <label className="instance-configuration__check">
                <input
                  type="checkbox"
                  checked={trigger.configured}
                  onChange={(event) => {
                    const configured = event.target.checked;
                    if (type === "schedule") {
                      setDraft((current) => ({
                        ...current,
                        triggerBindings: {
                          ...current.triggerBindings,
                          schedule: {
                            ...current.triggerBindings.schedule,
                            configured,
                            enabled: configured
                              ? current.triggerBindings.schedule.enabled
                              : false,
                          },
                        },
                        schedule: {
                          ...current.schedule,
                          enabled: configured
                            ? current.schedule.enabled
                            : false,
                        },
                      }));
                    } else {
                      updateTrigger(type, (current) => ({
                        ...current,
                        configured,
                        enabled: configured ? current.enabled : false,
                      }));
                    }
                  }}
                />
                Configure {type} trigger
              </label>
              <label className="instance-configuration__check">
                <input
                  type="checkbox"
                  checked={
                    type === "schedule"
                      ? draft.schedule.enabled
                      : trigger.enabled
                  }
                  disabled={!trigger.configured}
                  onChange={(event) => {
                    if (type === "schedule") {
                      setScheduleEnabled(event.target.checked);
                    } else {
                      updateTrigger(type, (current) => ({
                        ...current,
                        enabled: event.target.checked,
                      }));
                    }
                  }}
                />
                Enable {type} trigger
              </label>
              {type === "webhook" && trigger.configured ? (
                <label className="instance-configuration__field">
                  <span>Event source</span>
                  <input
                    value={trigger.eventSource}
                    maxLength={100}
                    aria-invalid={
                      !EVENT_SOURCE_PATTERN.test(trigger.eventSource.trim())
                    }
                    onChange={(event) =>
                      updateTrigger("webhook", (current) => ({
                        ...current,
                        eventSource: event.target.value,
                      }))
                    }
                  />
                </label>
              ) : null}
            </div>
          );
        })}
      </fieldset>

      {schema.scheduleSupported && draft.triggerBindings.schedule.configured ? (
        <fieldset className="instance-configuration__group">
          <legend>Schedule</legend>
          <p className="instance-configuration__hint">
            Schedule values are saved with the enabled schedule trigger as one
            consistent configuration.
          </p>
          <label className="instance-configuration__field">
            <span>Cron expression</span>
            <input
              value={draft.schedule.cron}
              disabled={!draft.schedule.enabled}
              maxLength={100}
              placeholder="0 9 * * 1"
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  schedule: { ...current.schedule, cron: event.target.value },
                }))
              }
            />
          </label>
          <label className="instance-configuration__field">
            <span>Timezone</span>
            <input
              value={draft.schedule.timezone}
              disabled={!draft.schedule.enabled}
              maxLength={100}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  schedule: {
                    ...current.schedule,
                    timezone: event.target.value,
                  },
                }))
              }
            />
          </label>
          <label className="instance-configuration__field">
            <span>Misfire policy</span>
            <select
              value={draft.schedule.misfirePolicy}
              disabled={!draft.schedule.enabled}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  schedule: {
                    ...current.schedule,
                    misfirePolicy: event.target.value as MisfirePolicy,
                  },
                }))
              }
            >
              <option value="skip">Skip missed occurrence</option>
              <option value="run_once">Run once after recovery</option>
            </select>
          </label>
          <label className="instance-configuration__field">
            <span>Misfire grace (seconds)</span>
            <input
              type="number"
              min={0}
              max={86_400}
              step={1}
              value={draft.schedule.misfireGraceSeconds}
              disabled={!draft.schedule.enabled}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  schedule: {
                    ...current.schedule,
                    misfireGraceSeconds: event.target.value,
                  },
                }))
              }
            />
          </label>
          <fieldset
            className="instance-configuration__group"
            disabled={saving || reloading}
          >
            <legend>Saved schedule input</legend>
            <p className="instance-configuration__hint">
              Every occurrence reuses this exact input. Supply the business
              content explicitly; no request or content is generated for you.
              Saved input is stored locally, restricted to configuration
              administrators, and omitted from the catalog and audit timeline.
            </p>
            {draft.scheduledInput === null ? (
              <p role="status">
                No input is saved. This schedule cannot execute until reusable
                input is provided.
              </p>
            ) : null}
            {inputSchema === null ? (
              <p role="alert">The input schema cannot be edited safely.</p>
            ) : (
              <ScheduledInputFields
                schema={inputSchema}
                draft={draft.scheduledInput?.input ?? {}}
                onChange={(input) =>
                  setDraft((current) => ({
                    ...current,
                    scheduledInput: {
                      input,
                      executionMode:
                        current.scheduledInput?.executionMode ?? "dry_run",
                    },
                  }))
                }
              />
            )}
            <label className="instance-configuration__field">
              <span>Scheduled execution mode</span>
              <select
                value={draft.scheduledInput?.executionMode ?? "dry_run"}
                onChange={(event) => {
                  const executionMode = event.target
                    .value as ScheduledInput["executionMode"];
                  setDraft((current) => ({
                    ...current,
                    scheduledInput: {
                      input: current.scheduledInput?.input ?? {},
                      executionMode,
                    },
                  }));
                }}
              >
                <option value="dry_run">
                  Dry run — external effects disabled
                </option>
              </select>
            </label>
            {draft.scheduledInput !== null ? (
              <button
                type="button"
                className="instance-configuration__button is-secondary"
                onClick={() =>
                  setDraft((current) => ({ ...current, scheduledInput: null }))
                }
              >
                Clear saved input
              </button>
            ) : null}
          </fieldset>
        </fieldset>
      ) : null}

      <fieldset className="instance-configuration__group">
        <legend>Connector bindings</legend>
        {schema.connectorFamilies.length === 0 ? (
          <p className="instance-configuration__hint">
            This deployment has no configurable connector families.
          </p>
        ) : null}
        {schema.connectorFamilies.map((option) => {
          const connector = draft.connectorBindings[option.connectorFamily];
          if (connector === undefined) return null;
          return (
            <div
              className="instance-configuration__binding"
              key={option.connectorFamily}
            >
              <strong>{option.connectorFamily}</strong>
              <label className="instance-configuration__check">
                <input
                  type="checkbox"
                  checked={connector.configured}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      connectorBindings: {
                        ...current.connectorBindings,
                        [option.connectorFamily]: {
                          ...connector,
                          configured: event.target.checked,
                        },
                      },
                    }))
                  }
                />
                Configure {option.connectorFamily} connector
              </label>
              <label className="instance-configuration__field">
                <span>Registered binding</span>
                <select
                  aria-label={`${option.connectorFamily} registered binding`}
                  value={connector.bindingId}
                  disabled={!connector.configured}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      connectorBindings: {
                        ...current.connectorBindings,
                        [option.connectorFamily]: {
                          ...connector,
                          bindingId: event.target.value,
                        },
                      },
                    }))
                  }
                >
                  {option.bindingIds.map((bindingId) => (
                    <option value={bindingId} key={bindingId}>
                      {bindingId}
                    </option>
                  ))}
                </select>
              </label>
              <label className="instance-configuration__check">
                <input
                  type="checkbox"
                  checked={connector.enabled}
                  disabled={!connector.configured}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      connectorBindings: {
                        ...current.connectorBindings,
                        [option.connectorFamily]: {
                          ...connector,
                          enabled: event.target.checked,
                        },
                      },
                    }))
                  }
                />
                Enable {option.connectorFamily} connector
              </label>
            </div>
          );
        })}
      </fieldset>

      {clientErrors.length > 0 ? (
        <div className="instance-configuration__validation" role="alert">
          <strong>Resolve these fields before saving</strong>
          <ul>
            {clientErrors.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {requestError !== null || unexpectedError !== null ? (
        <div className="instance-configuration__error" role="alert">
          <strong>
            {conflict ? "Configuration changed on the server" : "Save failed"}
          </strong>
          <p>{requestError?.message ?? unexpectedError}</p>
          {conflict && requestError.currentResourceVersion !== null ? (
            <p>
              Current server revision: {requestError.currentResourceVersion}
            </p>
          ) : null}
          {serverFieldErrors.length > 0 ? (
            <ul>
              {serverFieldErrors.map((fieldError) => (
                <li key={`${fieldError.pointer}:${fieldError.code}`}>
                  {fieldError.pointer}: {fieldError.message}
                </li>
              ))}
            </ul>
          ) : null}
          {conflict ? (
            <button
              className="instance-configuration__button is-secondary"
              type="button"
              disabled={reloading}
              onClick={() => void reload()}
            >
              {reloading ? "Reloading…" : "Reload current configuration"}
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="instance-configuration__actions">
        <button
          className="instance-configuration__button is-secondary"
          type="button"
          disabled={saving || reloading}
          onClick={onCancel}
        >
          Cancel
        </button>
        <button
          className="instance-configuration__button is-primary"
          type="submit"
          disabled={
            !dirty || clientErrors.length > 0 || saving || reloading || conflict
          }
        >
          {saving ? "Saving…" : "Save configuration"}
        </button>
      </div>
    </form>
  );
}

function ScheduledInputFields({
  schema,
  draft,
  onChange,
}: {
  readonly schema: CompiledObjectSchema;
  readonly draft: Readonly<Record<string, unknown>>;
  readonly onChange: (draft: SchemaDraftObject) => void;
}): React.JSX.Element {
  return (
    <div className="schema-form__fields">
      {schema.properties.map((property) => (
        <SchemaField
          key={property.name}
          schema={property.schema}
          pointer={`${schema.pointer}/${escapeJsonPointerSegment(property.name)}`}
          value={draft[property.name]}
          required={property.required}
          issues={[]}
          formId="saved-schedule-input"
          sensitiveValueNotice="Sensitive value. Saving stores this input locally for scheduled runs; configuration administrators can reopen it."
          disabled={false}
          onChange={(value) => onChange({ ...draft, [property.name]: value })}
        />
      ))}
    </div>
  );
}

function SavedConfigurationForm(
  props: Omit<ConfigurationFormProps, "savedInput" | "schema"> & {
    readonly schema: InstanceConfigurationSchema | undefined;
  },
): React.JSX.Element | null {
  const query = useQuery({
    queryKey: [
      "agent-instance",
      props.detail.instance.id,
      "restricted-configuration",
    ],
    queryFn: ({ signal }) =>
      fetchInstanceConfiguration(props.detail.instance.id, signal),
    retry: false,
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  if (props.schema === undefined) return null;
  if (query.isPending) return <p role="status">Loading saved configuration…</p>;
  if (query.isError)
    return (
      <div role="alert" className="instance-configuration__error">
        <strong>Saved configuration is unavailable</strong>
        <p>
          {messageFrom(
            query.error,
            "The current configuration could not be loaded.",
          )}
        </p>
        <button type="button" onClick={props.onCancel}>
          Cancel
        </button>
        <button type="button" onClick={() => void query.refetch()}>
          Try again
        </button>
      </div>
    );
  const saved = query.data;
  const detail: AgentInstanceDetail = {
    ...props.detail,
    instance: {
      ...props.detail.instance,
      ...saved.configuration,
      configurationEtag: saved.configurationEtag,
    },
  };
  const compatibility = compatibilityIssue(detail, props.schema);
  if (compatibility !== null)
    return (
      <div role="alert">
        <p>{compatibility}</p>
        <button type="button" onClick={props.onCancel}>
          Cancel
        </button>
      </div>
    );
  return (
    <ConfigurationForm
      {...props}
      schema={props.schema}
      detail={detail}
      savedInput={saved.configuration.scheduledInput}
      key={saved.configurationEtag}
    />
  );
}

export function InstanceConfigurationEditor({
  detail,
  onDirtyChange,
  onSaved,
  onReload,
}: InstanceConfigurationEditorProps): React.JSX.Element {
  const [editing, setEditing] = useState(false);
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const editButtonRef = useRef<HTMLButtonElement>(null);
  const editorSectionRef = useRef<HTMLElement>(null);
  const priorEditingRef = useRef(false);
  const sessionQuery = useQuery({
    queryKey: LOCAL_SESSION_QUERY_KEY,
    queryFn: ({ signal }) => fetchLocalSession(signal),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });
  const canEdit = sessionQuery.data?.roles.includes("local_admin") ?? false;
  const schemaQuery = useQuery({
    queryKey: [
      "agent-instance",
      detail.instance.id,
      "configuration-schema",
      detail.instance.templateId,
      detail.catalogHash,
    ],
    queryFn: ({ signal }) =>
      fetchInstanceConfigurationSchema(
        {
          instanceId: detail.instance.id,
          templateId: detail.instance.templateId,
        },
        signal,
      ),
    enabled: editing && canEdit,
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });

  const handleCommitted = async (): Promise<void> => {
    let refreshFailed = false;
    try {
      await onSaved();
    } catch {
      refreshFailed = true;
    }
    setEditing(false);
    setAnnouncement(
      refreshFailed
        ? "Configuration saved, but the refreshed detail is not available yet."
        : "Configuration saved.",
    );
  };

  const handleReloaded = async (): Promise<void> => {
    await onReload();
    setEditing(false);
    setAnnouncement("Configuration reloaded from the server.");
  };

  const compatibility =
    schemaQuery.data === undefined
      ? null
      : compatibilityIssue(detail, schemaQuery.data);

  useEffect(() => {
    const wasEditing = priorEditingRef.current;
    priorEditingRef.current = editing;
    if (editing && schemaQuery.data !== undefined && compatibility === null) {
      editorSectionRef.current
        ?.querySelector<HTMLElement>(
          "form input:not(:disabled), form select:not(:disabled), form textarea:not(:disabled), form button:not(:disabled)",
        )
        ?.focus();
    } else if (wasEditing && !editing) {
      editButtonRef.current?.focus();
    }
  }, [compatibility, editing, schemaQuery.data]);

  return (
    <section
      ref={editorSectionRef}
      className="instance-configuration"
      aria-labelledby="instance-configuration-title"
    >
      <div className="instance-configuration__heading">
        <div>
          <h4 id="instance-configuration-title">Configuration controls</h4>
          <p>
            Deployment-only fields · revision{" "}
            {detail.instance.configurationRevision}
          </p>
        </div>
        {!editing && canEdit ? (
          <button
            ref={editButtonRef}
            className="instance-configuration__button is-secondary"
            type="button"
            onClick={() => {
              setAnnouncement(null);
              setEditing(true);
            }}
          >
            Edit
          </button>
        ) : null}
      </div>

      {announcement !== null ? (
        <p
          className="instance-configuration__success"
          role="status"
          aria-live="polite"
        >
          {announcement}
        </p>
      ) : null}

      {sessionQuery.isPending ? (
        <p className="instance-configuration__hint" role="status">
          Checking edit access…
        </p>
      ) : null}

      {sessionQuery.isError ? (
        <div className="instance-configuration__error" role="alert">
          <strong>Edit access is unavailable</strong>
          <p>
            {messageFrom(
              sessionQuery.error,
              "The local session could not be loaded.",
            )}
          </p>
          <button
            className="instance-configuration__button is-secondary"
            type="button"
            onClick={() => void sessionQuery.refetch()}
          >
            Try again
          </button>
        </div>
      ) : null}

      {sessionQuery.isSuccess && !canEdit ? (
        <p className="instance-configuration__readonly">
          Read-only. This session does not include the local_admin role.
        </p>
      ) : null}

      {editing && schemaQuery.isPending ? (
        <p className="instance-configuration__hint" role="status">
          Loading editable fields…
        </p>
      ) : null}

      {editing && schemaQuery.isError ? (
        <div className="instance-configuration__error" role="alert">
          <strong>Editable fields are unavailable</strong>
          <p>
            {messageFrom(
              schemaQuery.error,
              "The configuration schema could not be loaded.",
            )}
          </p>
          <div className="instance-configuration__actions">
            <button
              className="instance-configuration__button is-secondary"
              type="button"
              onClick={() => setEditing(false)}
            >
              Cancel
            </button>
            <button
              className="instance-configuration__button is-secondary"
              type="button"
              onClick={() => void schemaQuery.refetch()}
            >
              Try again
            </button>
          </div>
        </div>
      ) : null}

      {editing && schemaQuery.data !== undefined && compatibility !== null ? (
        <div className="instance-configuration__error" role="alert">
          <strong>Configuration cannot be edited safely</strong>
          <p>{compatibility}</p>
          <button
            className="instance-configuration__button is-secondary"
            type="button"
            onClick={() => setEditing(false)}
          >
            Cancel
          </button>
        </div>
      ) : null}

      {editing && canEdit && compatibility === null ? (
        <SavedConfigurationForm
          key={`${detail.instance.id}:${detail.instance.configurationEtag}`}
          detail={detail}
          schema={schemaQuery.data}
          onDirtyChange={onDirtyChange}
          onCancel={() => setEditing(false)}
          onCommitted={handleCommitted}
          onReloaded={handleReloaded}
        />
      ) : null}
    </section>
  );
}
