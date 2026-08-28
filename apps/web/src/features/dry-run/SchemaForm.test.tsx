import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { createSchemaDefaults } from "./schemaDefaults";
import { SchemaForm } from "./SchemaForm";
import { compileInputSchema, type SchemaDraftObject } from "./schemaModel";
import type { SchemaValidationIssue } from "./schemaValidation";

const RAW_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  type: "object",
  additionalProperties: false,
  required: ["source_content", "priority"],
  properties: {
    source_content: {
      type: "string",
      title: "Source content",
      description: "Content to inspect.",
      minLength: 1,
      maxLength: 12_000,
      "x-sensitive": true,
      "x-ui": { help: "Paste only the source needed for this request." },
    },
    priority: {
      type: "integer",
      title: "Priority",
      minimum: 1,
      maximum: 5,
    },
    destination: {
      type: "string",
      title: "Destination",
      format: "uri",
      minLength: 1,
      maxLength: 500,
    },
    enabled: {
      type: "boolean",
      title: "Enabled",
    },
    profile: {
      type: "object",
      title: "Profile",
      additionalProperties: false,
      required: ["display_name"],
      properties: {
        display_name: {
          type: "string",
          title: "Display name",
          minLength: 1,
          maxLength: 80,
        },
      },
    },
    escalations: {
      type: "array",
      title: "Escalations",
      minItems: 1,
      maxItems: 2,
      items: {
        type: "string",
        title: "Escalation note",
        minLength: 1,
        maxLength: 120,
      },
    },
    recipients: {
      type: "array",
      title: "Recipients",
      minItems: 0,
      maxItems: 2,
      items: {
        type: "object",
        title: "Recipient",
        additionalProperties: false,
        required: ["email"],
        properties: {
          email: {
            type: "string",
            title: "Email",
            format: "email",
            minLength: 1,
            maxLength: 320,
          },
        },
      },
    },
  },
} as const;

const COMPILED_SCHEMA = compileInputSchema(RAW_SCHEMA);
const SOURCE_ISSUE: SchemaValidationIssue = {
  pointer: "/input/source_content",
  code: "required",
  message: "This field is required.",
};

function Harness({
  issues = [],
  validationRevision = 0,
  onSubmit = vi.fn(),
}: {
  readonly issues?: readonly SchemaValidationIssue[];
  readonly validationRevision?: number;
  readonly onSubmit?: () => void;
}): React.JSX.Element {
  const [draft, setDraft] = useState<SchemaDraftObject>(() =>
    createSchemaDefaults(COMPILED_SCHEMA),
  );
  return (
    <SchemaForm
      schema={COMPILED_SCHEMA}
      draft={draft}
      issues={issues}
      validationRevision={validationRevision}
      executionMode="dry_run"
      mockAvailable
      pending={false}
      onDraftChange={setDraft}
      onExecutionModeChange={vi.fn()}
      onSubmit={onSubmit}
      onStopWaiting={vi.fn()}
    />
  );
}

describe("WEB-04 SchemaForm", () => {
  it("connects stable labels, required state, help, sensitive notes, and errors", async () => {
    const { rerender } = render(
      <Harness issues={[SOURCE_ISSUE]} validationRevision={1} />,
    );

    const summary = screen.getByRole("alert");
    await waitFor(() => expect(summary).toHaveFocus());
    const source = screen.getByRole("textbox", { name: /source content/iu });
    const stableId = source.id;
    expect(source).toBeRequired();
    expect(source.tagName).toBe("TEXTAREA");
    expect(source).toHaveAttribute("autocomplete", "off");
    expect(source).toHaveAttribute("spellcheck", "false");
    expect(source).toHaveAttribute("aria-invalid", "true");

    const describedBy = source.getAttribute("aria-describedby") ?? "";
    expect(describedBy).toContain("description");
    expect(describedBy).toContain("help");
    expect(describedBy).toContain("sensitive-note");
    expect(describedBy).toContain("error");
    expect(screen.getByText("Content to inspect.")).toHaveAttribute(
      "id",
      expect.stringContaining("description"),
    );
    expect(
      screen.getByText("Sensitive value. Kept only in this open form."),
    ).toHaveAttribute("id", expect.stringContaining("sensitive-note"));

    await userEvent.click(
      within(summary).getByRole("button", {
        name: "This field is required.",
      }),
    );
    expect(source).toHaveFocus();

    rerender(<Harness issues={[SOURCE_ISSUE]} validationRevision={1} />);
    expect(
      screen.getByRole("textbox", { name: /source content/iu }),
    ).toHaveAttribute("id", stableId);
  });

  it("renders nested bounded arrays, formats, booleans, and exact actions", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    render(<Harness onSubmit={onSubmit} />);

    expect(screen.getByText(/does not fetch it/iu)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Enabled" })).toBeVisible();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Enabled" }),
      "true",
    );
    await user.click(screen.getByRole("button", { name: "Add Recipient" }));
    expect(screen.getByRole("textbox", { name: /email/iu })).toHaveAttribute(
      "type",
      "email",
    );
    expect(
      screen.getByRole("button", { name: "Remove Recipient 1" }),
    ).toBeEnabled();

    await user.type(
      screen.getByRole("textbox", { name: /source content/iu }),
      "memory-only input",
    );
    expect(storageSpy).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Create dry run" }),
    ).toBeVisible();
    expect(
      screen.getByRole("radio", { name: /mock execution/iu }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Create dry run" }));
    expect(onSubmit).toHaveBeenCalledOnce();
    storageSpy.mockRestore();
  });

  it("lets optional complex fields return to an omitted draft", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const displayName = screen.getByRole("textbox", {
      name: /display name/iu,
    });
    await user.type(displayName, "Temporary profile");
    const omitProfile = screen.getByRole("button", {
      name: "Omit Profile from dry-run input",
    });
    expect(omitProfile).toBeVisible();
    await user.click(omitProfile);
    expect(displayName).toHaveValue("");
    expect(
      screen.queryByRole("button", {
        name: "Omit Profile from dry-run input",
      }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Add Escalation note" }),
    );
    expect(
      screen.getByRole("button", { name: "Remove Escalation note 1" }),
    ).toBeDisabled();
    const omitEscalations = screen.getByRole("button", {
      name: "Omit Escalations from dry-run input",
    });
    expect(omitEscalations).toBeVisible();
    await user.click(omitEscalations);
    expect(
      screen.queryByRole("textbox", { name: /escalation note/iu }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "Omit Escalations from dry-run input",
      }),
    ).not.toBeInTheDocument();
  });
});
