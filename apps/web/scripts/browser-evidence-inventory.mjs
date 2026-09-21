import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

export const BROWSER_RUNNERS = Object.freeze([
  "run-web-01-e2e.mjs",
  "run-web-02-e2e.mjs",
  "run-web-03-e2e.mjs",
  "run-web-04-e2e.mjs",
  "run-web-05-e2e.mjs",
  "run-web-06-e2e.mjs",
  "run-web-07-e2e.mjs",
  "run-web-08-e2e.mjs",
  "run-web-09-e2e.mjs",
  "run-arch-02-e2e.mjs",
  "run-demo-01-e2e.mjs",
  "run-demo-02-e2e.mjs",
  "run-demo-03-e2e.mjs",
  "run-demo-04-e2e.mjs",
  "run-demo-05-e2e.mjs",
  "run-obj-06-e2e.mjs",
]);

// OBJ-06 needs the real supervised runtime, not the discovery-only webServer.
// This is one reviewed configuration, never a general CLI/filter escape hatch.
const CONFIGURED_RUNNERS = new Map([
  ["run-obj-06-e2e.mjs", "config/playwright-obj-06.config.ts"],
]);

export class BrowserInventoryError extends Error {}

// Read the deliberately simple literal runLocalTool/run call, not comments or
// diagnostic strings. Dynamic/filtered selections fail closed for review.
function sourceTokens(source) {
  const tokens = [];
  for (let index = 0; index < source.length;) {
    const character = source[index];
    if (/\s/.test(character)) {
      index += 1;
      continue;
    }
    if (source.startsWith("//", index)) {
      const end = source.indexOf("\n", index + 2);
      index = end < 0 ? source.length : end + 1;
      continue;
    }
    if (source.startsWith("/*", index)) {
      const end = source.indexOf("*/", index + 2);
      if (end < 0)
        throw new BrowserInventoryError("Unterminated runner comment");
      index = end + 2;
      continue;
    }
    if (["'", '"', "`"].includes(character)) {
      const quote = character;
      let value = "";
      index += 1;
      while (index < source.length && source[index] !== quote) {
        if (source[index] === "\\") {
          // Escaped/dynamic spec paths are outside the literal runner contract.
          value += source[index++];
        }
        value += source[index++];
      }
      if (index >= source.length)
        throw new BrowserInventoryError("Unterminated runner string");
      index += 1;
      tokens.push({ kind: quote === "`" ? "template" : "string", value });
      continue;
    }
    const identifier = /^[A-Za-z_$][\w$]*/.exec(source.slice(index));
    if (identifier) {
      tokens.push({ kind: "identifier", value: identifier[0] });
      index += identifier[0].length;
      continue;
    }
    tokens.push({ kind: "punctuation", value: character });
    index += 1;
  }
  return tokens;
}

function playwrightArguments(source, runner) {
  const tokens = sourceTokens(source);
  const calls = [];
  for (let index = 0; index < tokens.length; index += 1) {
    if (
      tokens[index].kind !== "identifier" ||
      !["runLocalTool", "run"].includes(tokens[index].value) ||
      tokens[index + 1]?.value !== "(" ||
      tokens[index + 2]?.kind !== "string" ||
      tokens[index + 2]?.value !== "playwright"
    )
      continue;
    if (tokens[index + 3]?.value !== "," || tokens[index + 4]?.value !== "[")
      throw new BrowserInventoryError(
        `${runner}: Playwright arguments must be a literal array`,
      );
    const args = [];
    let cursor = index + 5;
    while (tokens[cursor]?.value !== "]") {
      if (tokens[cursor]?.kind !== "string")
        throw new BrowserInventoryError(
          `${runner}: Playwright selection must contain only literal strings`,
        );
      args.push(tokens[cursor++].value);
      if (tokens[cursor]?.value === ",") cursor += 1;
      else if (tokens[cursor]?.value !== "]")
        throw new BrowserInventoryError(
          `${runner}: invalid literal Playwright selection`,
        );
    }
    calls.push(args);
  }
  if (calls.length !== 1)
    throw new BrowserInventoryError(
      `${runner}: expected exactly one unfiltered Playwright invocation, found ${String(calls.length)}`,
    );
  const [command, ...selection] = calls[0];
  const requiredConfig = CONFIGURED_RUNNERS.get(runner);
  let specs = selection;
  if (requiredConfig !== undefined) {
    if (selection[0] !== "--config" || selection[1] !== requiredConfig)
      throw new BrowserInventoryError(
        `${runner}: expected its exact reviewed runtime configuration`,
      );
    specs = selection.slice(2);
  }
  if (
    command !== "test" ||
    specs.length === 0 ||
    specs.some(
      (spec) =>
        !/^e2e\/.+\.(?:spec|test)\.[cm]?[jt]sx?$/.test(spec) ||
        spec.includes("\\") ||
        spec.split("/").some((segment) => ["", ".", ".."].includes(segment)),
    )
  )
    throw new BrowserInventoryError(
      `${runner}: expected test plus explicit spec paths, with no filters or zero-test overrides`,
    );
  return specs;
}

export function validateBrowserInventory(runners, specFiles, runnerSources) {
  if (runners.length === 0 || specFiles.length === 0)
    throw new BrowserInventoryError(
      "Browser inventory must have nonzero runners and specs",
    );
  if (new Set(runners).size !== runners.length)
    throw new BrowserInventoryError("Duplicate browser runner in aggregate");
  if (new Set(specFiles).size !== specFiles.length)
    throw new BrowserInventoryError("Duplicate discovered browser spec");
  const owners = new Map();
  for (const runner of runners) {
    const source = runnerSources.get(runner);
    if (typeof source !== "string" || source.trim().length === 0)
      throw new BrowserInventoryError(
        `Missing or empty browser runner: ${runner}`,
      );
    for (const spec of playwrightArguments(source, runner)) {
      if (!specFiles.includes(spec))
        throw new BrowserInventoryError(
          `${runner}: selected browser spec does not exist: ${spec}`,
        );
      if (owners.has(spec))
        throw new BrowserInventoryError(
          `Browser spec has duplicate ownership: ${spec}`,
        );
      owners.set(spec, runner);
    }
  }
  for (const spec of specFiles) {
    if (!owners.has(spec))
      throw new BrowserInventoryError(
        `Unowned browser spec would be excluded from the aggregate: ${spec}`,
      );
  }
  return Object.freeze({
    runnerCount: runners.length,
    specCount: specFiles.length,
  });
}

function discoverSpecs(webRoot, directory = "e2e") {
  return readdirSync(join(webRoot, directory), { withFileTypes: true })
    .flatMap((entry) => {
      const path = `${directory}/${entry.name}`;
      if (entry.isSymbolicLink())
        throw new BrowserInventoryError(
          `Browser inventory cannot follow a symbolic link: ${path}`,
        );
      if (entry.isDirectory()) return discoverSpecs(webRoot, path);
      return entry.isFile() && /\.(?:spec|test)\.[cm]?[jt]sx?$/.test(entry.name)
        ? [path]
        : [];
    })
    .sort();
}

export function readBrowserInventory(webRoot, runners = BROWSER_RUNNERS) {
  const sources = new Map(
    runners.map((runner) => {
      if (!/^run-[a-z0-9-]+-e2e\.mjs$/.test(runner))
        throw new BrowserInventoryError(
          `Invalid browser runner filename: ${runner}`,
        );
      try {
        return [runner, readFileSync(join(webRoot, "scripts", runner), "utf8")];
      } catch {
        throw new BrowserInventoryError(
          `Cannot read browser runner: ${runner}`,
        );
      }
    }),
  );
  return validateBrowserInventory(runners, discoverSpecs(webRoot), sources);
}
