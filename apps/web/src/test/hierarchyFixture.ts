interface FixtureInstance {
  id: string;
  templateId: string;
  displayName: string;
  purpose: string;
  displayOrder: number;
  enabled: boolean;
  operationClassification: "read_only" | "mutating";
  triggerTypes: string[];
  capabilitySummaries: {
    id: string;
    displayName: string;
    connectorFamily: string;
    effect: "read" | "write";
  }[];
  sourceOrdinal: number;
}

interface FixtureFunction {
  id: string;
  displayName: string;
  displayOrder: number;
  instances: FixtureInstance[];
}

interface FixtureDepartment {
  id: string;
  displayName: string;
  displayOrder: number;
  functions: FixtureFunction[];
}

const DEFINITIONS = [
  {
    slug: "social-media",
    name: "Social media",
    functions: [
      ["new-content", "New content", 6],
      ["research", "Research", 2],
      ["tracking-analysis", "Tracking & analysis", 4],
    ],
  },
  {
    slug: "blog-seo",
    name: "Blog & SEO",
    functions: [
      ["new-content", "New content", 3],
      ["tracking-analysis", "Tracking & analysis", 3],
    ],
  },
  {
    slug: "email",
    name: "Email",
    functions: [
      ["newsletter", "Newsletter", 2],
      ["lifecycle-marketing", "Lifecycle marketing", 3],
    ],
  },
  {
    slug: "community",
    name: "Community",
    functions: [
      ["events", "Events", 6],
      ["education", "Education", 6],
      ["discussion", "Discussion", 2],
    ],
  },
  {
    slug: "partnerships",
    name: "Partnerships",
    functions: [
      ["implementation-partners", "Implementation partners", 5],
      ["integration-partners", "Integration partners", 1],
    ],
  },
] as const;

function fixtureInstances(
  departmentSlug: string,
  functionSlug: string,
  count: number,
  community: boolean,
): FixtureInstance[] {
  return Array.from({ length: count }, (_, index) => {
    const templateIndex = community ? Math.floor(index / 2) + 1 : index + 1;
    const ordinal = community ? (index % 2) + 1 : 1;
    const templateId = `tpl.${departmentSlug}.${functionSlug}.agent-${String(templateIndex)}`;
    return {
      id: `inst.${departmentSlug}.${functionSlug}.agent-${String(templateIndex)}.${String(
        ordinal,
      ).padStart(2, "0")}`,
      templateId,
      displayName: `Agent ${String(templateIndex)}`,
      purpose: `Completes source-backed ${functionSlug} work for the local catalog.`,
      displayOrder: (templateIndex - 1) * 10 + ordinal,
      enabled: true,
      operationClassification: index % 3 === 0 ? "mutating" : "read_only",
      triggerTypes: ["manual"],
      capabilitySummaries: [
        {
          id: `cap.${departmentSlug}.read`,
          displayName: "Catalog read",
          connectorFamily: departmentSlug,
          effect: "read",
        },
      ],
      sourceOrdinal: ordinal,
    };
  });
}

export function makeHierarchyPayload(): Record<string, unknown> {
  const departments: FixtureDepartment[] = DEFINITIONS.map(
    (department, departmentIndex) => ({
      id: `dept.${department.slug}`,
      displayName: department.name,
      displayOrder: (departmentIndex + 1) * 10,
      functions: department.functions.map(
        ([functionSlug, functionName, instanceCount], functionIndex) => ({
          id: `func.${department.slug}.${functionSlug}`,
          displayName: functionName,
          displayOrder: (functionIndex + 1) * 10,
          instances: fixtureInstances(
            department.slug,
            functionSlug,
            instanceCount,
            department.slug === "community",
          ),
        }),
      ),
    }),
  );

  return {
    catalogVersion: "1.0.0",
    catalogHash: `catalog-sha256-v1:${"a".repeat(64)}`,
    counts: { departments: 5, functions: 12, templates: 36, instances: 43 },
    departmentCounts: departments.map((department) => ({
      departmentId: department.id,
      instanceCount: department.functions.reduce(
        (sum, agentFunction) => sum + agentFunction.instances.length,
        0,
      ),
    })),
    departments,
  };
}
