import { describe, expectTypeOf, it } from "vitest";
import type { ApiView, NormalizedContract } from "./apiContract";
import type { components } from "./generated/schema";
import type { ManualDryRunReceipt } from "./manualDryRun";
import type { DemoScenarioRunReceipt } from "./demoScenarios";
import type { RunSummary } from "./runArtifacts";
import type { ApprovalSummary } from "./approvals";
import type { AgentTemplateView } from "./agentInstanceDetail";
import type { InstanceRuntimeStatus } from "./instanceStatusSummary";
import type {
  AgentDepartment,
  AgentFunction,
  AgentInstance,
  CapabilitySummary,
  HierarchyCounts,
  NormalizedHierarchy,
} from "../contracts/catalogHierarchy";

describe("EXEC-05 generated contract ownership", () => {
  it("derives nested immutable views without trusting or converting JSON", () => {
    expectTypeOf<
      NormalizedContract<{
        run_id: string;
        next_cursor?: string | null;
        selected_instances: readonly { instance_id: string }[];
        unknown_payload: unknown;
      }>
    >().toEqualTypeOf<{
      readonly runId: string;
      readonly nextCursor: string | null;
      readonly selectedInstances: readonly { readonly instanceId: string }[];
      readonly unknownPayload: unknown;
    }>();
  });

  it("binds receipts, catalog, runs, approvals and status views to generated DTOs", () => {
    expectTypeOf<ManualDryRunReceipt>().toEqualTypeOf<
      components["schemas"]["ManualDryRunResponse"]
    >();
    expectTypeOf<DemoScenarioRunReceipt>().toEqualTypeOf<
      components["schemas"]["DemoScenarioRunResponse"]
    >();
    expectTypeOf<RunSummary>().toEqualTypeOf<ApiView<"RunSummaryView">>();
    expectTypeOf<ApprovalSummary>().toEqualTypeOf<
      ApiView<"ApprovalSummaryView">
    >();
    expectTypeOf<AgentTemplateView>().toEqualTypeOf<
      ApiView<"AgentTemplateView">
    >();
    expectTypeOf<InstanceRuntimeStatus>().toEqualTypeOf<
      ApiView<"marketing_agents__api__schemas__runs__InstanceRuntimeStatusView">
    >();
  });

  it("checks pure hierarchy models at the API boundary without reversing dependency direction", () => {
    expectTypeOf<CapabilitySummary>().toEqualTypeOf<
      ApiView<"CapabilitySummaryView">
    >();
    expectTypeOf<Omit<AgentInstance, "deploymentCount">>().toEqualTypeOf<
      ApiView<"HierarchyInstanceView">
    >();
    expectTypeOf<Omit<AgentFunction, "instances">>().toEqualTypeOf<
      Omit<ApiView<"HierarchyFunctionView">, "instances">
    >();
    expectTypeOf<
      Omit<AgentDepartment, "functions" | "instanceCount" | "templateCount">
    >().toEqualTypeOf<Omit<ApiView<"HierarchyDepartmentView">, "functions">>();
    expectTypeOf<HierarchyCounts>().toExtend<ApiView<"HierarchyCounts">>();
    expectTypeOf<
      Omit<NormalizedHierarchy, "counts" | "departments" | "structuralKey">
    >().toEqualTypeOf<
      Omit<
        ApiView<"CatalogHierarchyResponse">,
        "counts" | "departments" | "departmentCounts"
      >
    >();
  });
});
