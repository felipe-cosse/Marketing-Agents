import { readFileSync, readdirSync } from "node:fs";
import { dirname, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import * as ts from "typescript";
import { describe, expect, it } from "vitest";

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SOURCE_ROOT = resolve(WEB_ROOT, "src");
const TYPESCRIPT_SOURCE = /\.(?:ts|tsx)$/u;
const TEST_MODULE = /(?:^|\/)test(?:\/|$)|\.test(?:\.|$)/u;
const COLOCATED_TEST_HELPER = /(?:^|\/)[^/]*TestFixtures?(?:\.(?:ts|tsx))?$/u;
const GLOBAL_FETCH_OWNERS = new Set(["globalThis", "self", "window"]);

type BoundaryCode =
  | "api-outward-import"
  | "contract-impure-import"
  | "production-test-import"
  | "ui-api-path-literal"
  | "ui-direct-fetch";

interface BoundaryViolation {
  readonly code: BoundaryCode;
  readonly detail: string;
  readonly file: string;
  readonly line: number;
}

function portablePath(path: string): string {
  return path.split(sep).join("/");
}

function relativeToWeb(path: string): string {
  return portablePath(relative(WEB_ROOT, path));
}

function typescriptSources(root: string): readonly string[] {
  return readdirSync(root, { withFileTypes: true })
    .flatMap((entry) => {
      const path = resolve(root, entry.name);
      if (entry.isDirectory()) return typescriptSources(path);
      return entry.isFile() && TYPESCRIPT_SOURCE.test(entry.name) ? [path] : [];
    })
    .sort();
}

function isProductionSource(path: string): boolean {
  return (
    path.startsWith("src/") &&
    !path.startsWith("src/test/") &&
    !path.endsWith(".test.ts") &&
    !path.endsWith(".test.tsx") &&
    !COLOCATED_TEST_HELPER.test(path)
  );
}

function resolveRelativeImport(
  importer: string,
  specifier: string,
): string | null {
  if (!specifier.startsWith(".")) return null;
  return portablePath(
    relative(WEB_ROOT, resolve(WEB_ROOT, dirname(importer), specifier)),
  );
}

function isTestModule(path: string): boolean {
  return (
    TEST_MODULE.test(path) ||
    COLOCATED_TEST_HELPER.test(path) ||
    path.startsWith("config/") ||
    path.startsWith("e2e/")
  );
}

function isUiSource(path: string): boolean {
  return (
    path.startsWith("src/features/") ||
    path.startsWith("src/app/") ||
    path.startsWith("src/accessibility/") ||
    path === "src/main.tsx"
  );
}

function analyzesAsFetchCall(node: ts.CallExpression): boolean {
  if (ts.isIdentifier(node.expression)) return node.expression.text === "fetch";
  if (
    ts.isPropertyAccessExpression(node.expression) &&
    node.expression.name.text === "fetch" &&
    ts.isIdentifier(node.expression.expression)
  ) {
    return GLOBAL_FETCH_OWNERS.has(node.expression.expression.text);
  }
  if (
    ts.isElementAccessExpression(node.expression) &&
    ts.isIdentifier(node.expression.expression) &&
    GLOBAL_FETCH_OWNERS.has(node.expression.expression.text)
  ) {
    const argument = node.expression.argumentExpression;
    return (
      (ts.isStringLiteral(argument) ||
        ts.isNoSubstitutionTemplateLiteral(argument)) &&
      argument.text === "fetch"
    );
  }
  return false;
}

function isApiPathSegment(value: string): boolean {
  return value === "/api" || value.startsWith("/api/");
}

function analyzesAsApiPathLiteral(node: ts.Node): boolean {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    return isApiPathSegment(node.text);
  }
  return (
    ts.isTemplateExpression(node) &&
    [
      node.head.text,
      ...node.templateSpans.map((span) => span.literal.text),
    ].some(isApiPathSegment)
  );
}

function analyzeSource(
  file: string,
  source: string,
): readonly BoundaryViolation[] {
  const violations: BoundaryViolation[] = [];
  const sourceFile = ts.createSourceFile(
    file,
    source,
    ts.ScriptTarget.Latest,
    true,
    file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const apiSource = file.startsWith("src/api/");
  const contractSource = file.startsWith("src/contracts/");
  const productionSource = isProductionSource(file);
  const uiSource = isUiSource(file);

  const addViolation = (
    code: BoundaryCode,
    node: ts.Node,
    detail: string,
  ): void => {
    const location = sourceFile.getLineAndCharacterOfPosition(
      node.getStart(sourceFile),
    );
    violations.push({ code, detail, file, line: location.line + 1 });
  };

  const inspectImport = (node: ts.Node, specifier: string): void => {
    const target = resolveRelativeImport(file, specifier);
    if (productionSource && target !== null && isTestModule(target)) {
      addViolation("production-test-import", node, specifier);
    }
    if (
      apiSource &&
      target !== null &&
      !target.startsWith("src/api/") &&
      !target.startsWith("src/contracts/")
    ) {
      addViolation("api-outward-import", node, specifier);
    }
    if (contractSource && !target?.startsWith("src/contracts/")) {
      addViolation("contract-impure-import", node, specifier);
    }
  };

  const visit = (node: ts.Node): void => {
    if (
      (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
      node.moduleSpecifier !== undefined &&
      ts.isStringLiteral(node.moduleSpecifier)
    ) {
      inspectImport(node, node.moduleSpecifier.text);
    } else if (
      ts.isCallExpression(node) &&
      node.expression.kind === ts.SyntaxKind.ImportKeyword &&
      node.arguments.length === 1 &&
      ts.isStringLiteral(node.arguments[0])
    ) {
      inspectImport(node, node.arguments[0].text);
    }

    if (uiSource && ts.isCallExpression(node) && analyzesAsFetchCall(node)) {
      addViolation("ui-direct-fetch", node, "use the src/api boundary");
    }
    if (uiSource && analyzesAsApiPathLiteral(node)) {
      addViolation("ui-api-path-literal", node, "use an API adapter export");
    }

    ts.forEachChild(node, visit);
  };

  visit(sourceFile);
  return violations;
}

const NEGATIVE_FIXTURES = [
  {
    name: "an API module importing a feature",
    file: "src/api/example.ts",
    source: 'import "../features/example";',
    expectedCodes: ["api-outward-import"],
  },
  {
    name: "a feature making a direct API request",
    file: "src/features/example.ts",
    source: 'fetch("/api/v1/examples");',
    expectedCodes: ["ui-direct-fetch", "ui-api-path-literal"],
  },
  {
    name: "a feature calling fetch through a global property",
    file: "src/features/example.ts",
    source: 'self.fetch("/api/v1/examples");',
    expectedCodes: ["ui-direct-fetch", "ui-api-path-literal"],
  },
  {
    name: "a feature calling fetch through global bracket access",
    file: "src/features/example.ts",
    source: 'globalThis["fetch"]("/api/v1/examples");',
    expectedCodes: ["ui-direct-fetch", "ui-api-path-literal"],
  },
  {
    name: "a feature owning an API path in a template tail",
    file: "src/features/example.ts",
    source: "const endpoint = `${origin}/api/v1/examples`;",
    expectedCodes: ["ui-api-path-literal"],
  },
  {
    name: "production code importing a test helper",
    file: "src/features/example.ts",
    source: 'import "../test/exampleFixture";',
    expectedCodes: ["production-test-import"],
  },
  {
    name: "a shared contract importing a runtime package",
    file: "src/contracts/example.ts",
    source: 'import "react";',
    expectedCodes: ["contract-impure-import"],
  },
] as const satisfies readonly {
  readonly name: string;
  readonly file: string;
  readonly source: string;
  readonly expectedCodes: readonly BoundaryCode[];
}[];

describe("ARCH-08 frontend dependency boundaries", () => {
  it.each(NEGATIVE_FIXTURES)("ARCH-08 rejects $name", (fixture) => {
    expect(
      analyzeSource(fixture.file, fixture.source).map(({ code }) => code),
    ).toEqual(fixture.expectedCodes);
  });

  it("ARCH-08 ignores comments and inert fetch prose", () => {
    const source = `
      const quoted = 'fetch("/api/v1/inert")';
      const template = \`window.fetch("/api/v1/inert")\`;
      // fetch("/api/v1/comment");
      /* globalThis["fetch"]("/api/v1/comment"); */
    `;

    expect(analyzeSource("src/features/example.ts", source)).toEqual([]);
  });

  it("ARCH-08 keeps production UI, API, contracts, and tests directionally separated", () => {
    const violations = typescriptSources(SOURCE_ROOT)
      .map((absolutePath) => ({
        absolutePath,
        relativePath: relativeToWeb(absolutePath),
      }))
      .filter(({ relativePath }) => isProductionSource(relativePath))
      .flatMap(({ absolutePath, relativePath }) =>
        analyzeSource(relativePath, readFileSync(absolutePath, "utf8")),
      );

    expect(violations).toEqual([]);
  });
});
