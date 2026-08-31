import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import type { AgentInstanceDetail } from "../../api/agentInstanceDetail";
import {
  fetchInstanceConfigurationSchema,
  fetchLocalSession,
  InstanceConfigurationRequestError,
  serializeInstanceConfigurationPatch,
  updateInstanceConfiguration,
  type ConnectorBindingPatch,
  type InstanceConfigurationPatch,
  type InstanceConfigurationSchema,
  type MisfirePolicy,
  type SchedulePatch,
  type SupportedTriggerType,
  type TriggerBindingPatch,
} from "../../api/instanceConfiguration";
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
}

interface EditableConfiguration {
  readonly enabled: boolean;
  readonly variantLabel: string | null;
  readonly triggerBindings: readonly TriggerBindingPatch[];
  readonly connectorBindings: Readonly<Record<string, ConnectorBindingPatch>>;
  readonly schedule: SchedulePatch | null;
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
  } = {};
  if (previous.enabled !== next.enabled) patch.enabled = next.enabled;
  if (previous.variantLabel !== next.variantLabel) {
    patch.variantLabel = next.variantLabel;
  }
  if (!sameValue(previous.connectorBindings, next.connectorBindings)) {
    patch.connectorBindings = next.connectorBindings;
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
  readonly onDirtyChange: (dirty: boolean) => void;
  readonly onCancel: () => void;
  readonly onCommitted: () => Promise<void>;
  readonly onReloaded: () => Promise<void>;
}

function ConfigurationForm({
  detail,
  schema,
  onDirtyChange,
  onCancel,
  onCommitted,
  onReloaded,
}: ConfigurationFormProps): React.JSX.Element {
  const [draft, setDraft] = useState<ConfigurationDraft>(() =>
    draftFrom(detail, schema),
  );
  const [baseline] = useState<EditableConfiguration>(() =>
    editableFrom(draftFrom(detail, schema), schema),
  );
  const [saving, setSaving] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [committed, setCommitted] = useState(false);
  const [requestError, setRequestError] =
    useState<InstanceConfigurationRequestError | null>(null);
  const [unexpectedError, setUnexpectedError] = useState<string | null>(null);
  const editable = useMemo(() => editableFrom(draft, schema), [draft, schema]);
  const patch = useMemo(
    () => partialPatch(baseline, editable),
    [baseline, editable],
  );
  const dirty = !committed && Object.keys(patch).length > 0;
  const clientErrors = useMemo(
    () => validationMessages(draft, schema),
    [draft, schema],
  );

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
      serializeInstanceConfigurationPatch(patch, schema);
      await updateInstanceConfiguration({
        instanceId: detail.instance.id,
        configurationEtag: detail.instance.configurationEtag,
        patch,
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
      className="instance-configuration__form"
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

      {editing && schemaQuery.data !== undefined && compatibility === null ? (
        <ConfigurationForm
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
