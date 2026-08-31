export const ARTIFACT_RENDER_LIMITS = Object.freeze({
  maxDepth: 12,
  maxNodes: 1_000,
  maxStringLength: 100_000,
});

export type ArtifactOmissionReason =
  | "accessor value"
  | "circular reference"
  | "depth limit reached"
  | "node limit reached"
  | "sparse array item"
  | "string limit reached"
  | "unsupported value";

export interface ArtifactScalarNode {
  readonly kind: "scalar";
  readonly value: boolean | null | number | string;
}

export interface ArtifactOmittedNode {
  readonly kind: "omitted";
  readonly reason: ArtifactOmissionReason;
}

export interface ArtifactArrayNode {
  readonly kind: "array";
  readonly items: readonly ArtifactRenderNode[];
  readonly omission: ArtifactOmittedNode | null;
}

export interface ArtifactObjectEntry {
  readonly key: string;
  readonly value: ArtifactRenderNode;
}

export interface ArtifactObjectNode {
  readonly kind: "object";
  readonly entries: readonly ArtifactObjectEntry[];
  readonly omission: ArtifactOmittedNode | null;
}

export type ArtifactRenderNode =
  | ArtifactArrayNode
  | ArtifactObjectNode
  | ArtifactOmittedNode
  | ArtifactScalarNode;

export interface PreparedArtifactValue {
  readonly nodeCount: number;
  readonly root: ArtifactRenderNode;
  readonly wasTruncated: boolean;
}

export type ArtifactJsonTokenKind =
  | "boolean"
  | "key"
  | "null"
  | "number"
  | "punctuation"
  | "string"
  | "whitespace";

export interface ArtifactJsonToken {
  readonly kind: ArtifactJsonTokenKind;
  readonly text: string;
}

interface PreparationState {
  nodeCount: number;
  wasTruncated: boolean;
}

const JSON_NUMBER_CHARACTER = /[-+0-9.eE]/u;
const JSON_WHITESPACE = /\s/u;
const PUNCTUATION = new Set(["{", "}", "[", "]", ":", ","]);

function omitted(
  state: PreparationState,
  reason: ArtifactOmissionReason,
): ArtifactOmittedNode {
  state.wasTruncated = true;
  return { kind: "omitted", reason };
}

function countedOmission(
  state: PreparationState,
  reason: ArtifactOmissionReason,
): ArtifactOmittedNode {
  state.nodeCount += 1;
  return omitted(state, reason);
}

function truncateString(value: string, state: PreparationState): string {
  if (value.length <= ARTIFACT_RENDER_LIMITS.maxStringLength) return value;
  state.wasTruncated = true;
  let boundary = ARTIFACT_RENDER_LIMITS.maxStringLength;
  const finalCodeUnit = value.charCodeAt(boundary - 1);
  if (finalCodeUnit >= 0xd800 && finalCodeUnit <= 0xdbff) boundary -= 1;
  return `${value.slice(0, boundary)}\n[Content omitted: string limit reached]`;
}

function prepareArray(
  value: readonly unknown[],
  depth: number,
  state: PreparationState,
  activeObjects: WeakSet<object>,
): ArtifactArrayNode {
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const items: ArtifactRenderNode[] = [];
  let omission: ArtifactOmittedNode | null = null;

  for (let index = 0; index < value.length; index += 1) {
    if (state.nodeCount >= ARTIFACT_RENDER_LIMITS.maxNodes) {
      omission = omitted(state, "node limit reached");
      break;
    }
    const descriptor = descriptors[String(index)];
    if (descriptor === undefined) {
      items.push(countedOmission(state, "sparse array item"));
    } else if (!("value" in descriptor)) {
      items.push(countedOmission(state, "accessor value"));
    } else {
      items.push(
        prepareNode(descriptor.value, depth + 1, state, activeObjects),
      );
    }
  }

  return { kind: "array", items, omission };
}

function prepareObject(
  value: object,
  depth: number,
  state: PreparationState,
  activeObjects: WeakSet<object>,
): ArtifactObjectNode {
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const entries: ArtifactObjectEntry[] = [];
  let omission: ArtifactOmittedNode | null = null;

  for (const [key, descriptor] of Object.entries(descriptors)) {
    if (!descriptor.enumerable) continue;
    if (state.nodeCount >= ARTIFACT_RENDER_LIMITS.maxNodes) {
      omission = omitted(state, "node limit reached");
      break;
    }
    entries.push({
      key: truncateString(key, state),
      value:
        "value" in descriptor
          ? prepareNode(descriptor.value, depth + 1, state, activeObjects)
          : countedOmission(state, "accessor value"),
    });
  }

  return { kind: "object", entries, omission };
}

function prepareNode(
  value: unknown,
  depth: number,
  state: PreparationState,
  activeObjects: WeakSet<object>,
): ArtifactRenderNode {
  if (state.nodeCount >= ARTIFACT_RENDER_LIMITS.maxNodes) {
    return omitted(state, "node limit reached");
  }
  state.nodeCount += 1;

  if (value === null || typeof value === "boolean") {
    return { kind: "scalar", value };
  }
  if (typeof value === "string") {
    return { kind: "scalar", value: truncateString(value, state) };
  }
  if (typeof value === "number") {
    return Number.isFinite(value)
      ? { kind: "scalar", value }
      : omitted(state, "unsupported value");
  }
  if (typeof value !== "object") {
    return omitted(state, "unsupported value");
  }
  if (activeObjects.has(value)) {
    return omitted(state, "circular reference");
  }
  if (depth >= ARTIFACT_RENDER_LIMITS.maxDepth) {
    return omitted(state, "depth limit reached");
  }

  let prototype: object | null;
  try {
    prototype = Object.getPrototypeOf(value) as object | null;
  } catch {
    return omitted(state, "unsupported value");
  }
  const isArray = Array.isArray(value);
  if (
    (!isArray && prototype !== Object.prototype && prototype !== null) ||
    (isArray && prototype !== Array.prototype)
  ) {
    return omitted(state, "unsupported value");
  }

  activeObjects.add(value);
  try {
    return isArray
      ? prepareArray(value, depth, state, activeObjects)
      : prepareObject(value, depth, state, activeObjects);
  } catch {
    return omitted(state, "unsupported value");
  } finally {
    activeObjects.delete(value);
  }
}

export function prepareArtifactValue(value: unknown): PreparedArtifactValue {
  const state: PreparationState = { nodeCount: 0, wasTruncated: false };
  return {
    root: prepareNode(value, 0, state, new WeakSet<object>()),
    nodeCount: state.nodeCount,
    wasTruncated: state.wasTruncated,
  };
}

export function artifactOmissionText(reason: ArtifactOmissionReason): string {
  return `[Content omitted: ${reason}]`;
}

function quotedJson(value: string): string {
  return JSON.stringify(value);
}

function omissionKey(entries: readonly ArtifactObjectEntry[]): string {
  const keys = new Set(entries.map(({ key }) => key));
  let key = "$artifact_viewer_omitted";
  while (keys.has(key)) key = `$${key}`;
  return key;
}

function nodeToJsonLines(
  node: ArtifactRenderNode,
  depth: number,
): readonly string[] {
  const indent = "  ".repeat(depth);
  const childIndent = "  ".repeat(depth + 1);

  if (node.kind === "omitted") {
    return [`${indent}${quotedJson(artifactOmissionText(node.reason))}`];
  }
  if (node.kind === "scalar") {
    if (typeof node.value === "string") {
      return [`${indent}${quotedJson(node.value)}`];
    }
    return [`${indent}${String(node.value)}`];
  }
  if (node.kind === "array") {
    const children = [...node.items];
    if (node.omission !== null) children.push(node.omission);
    if (children.length === 0) return [`${indent}[]`];
    const lines = [`${indent}[`];
    children.forEach((child, index) => {
      const childLines = [...nodeToJsonLines(child, depth + 1)];
      const lastIndex = childLines.length - 1;
      childLines[lastIndex] =
        `${childLines[lastIndex] ?? ""}${index < children.length - 1 ? "," : ""}`;
      lines.push(...childLines);
    });
    lines.push(`${indent}]`);
    return lines;
  }

  const entries = [...node.entries];
  if (node.omission !== null) {
    entries.push({ key: omissionKey(entries), value: node.omission });
  }
  if (entries.length === 0) return [`${indent}{}`];
  const lines = [`${indent}{`];
  entries.forEach((entry, index) => {
    const childLines = [...nodeToJsonLines(entry.value, depth + 1)];
    childLines[0] = `${childIndent}${quotedJson(entry.key)}: ${childLines[0]?.slice(childIndent.length) ?? ""}`;
    const lastIndex = childLines.length - 1;
    childLines[lastIndex] =
      `${childLines[lastIndex] ?? ""}${index < entries.length - 1 ? "," : ""}`;
    lines.push(...childLines);
  });
  lines.push(`${indent}}`);
  return lines;
}

export function artifactNodeToJson(node: ArtifactRenderNode): string {
  return nodeToJsonLines(node, 0).join("\n");
}

export function tokenizeArtifactJson(
  source: string,
): readonly ArtifactJsonToken[] {
  const tokens: ArtifactJsonToken[] = [];
  let cursor = 0;

  while (cursor < source.length) {
    const character = source[cursor] ?? "";
    if (JSON_WHITESPACE.test(character)) {
      const start = cursor;
      while (JSON_WHITESPACE.test(source[cursor] ?? "")) cursor += 1;
      tokens.push({ kind: "whitespace", text: source.slice(start, cursor) });
      continue;
    }
    if (PUNCTUATION.has(character)) {
      tokens.push({ kind: "punctuation", text: character });
      cursor += 1;
      continue;
    }
    if (character === '"') {
      const start = cursor;
      cursor += 1;
      while (cursor < source.length) {
        const current = source[cursor];
        if (current === "\\") {
          cursor += 2;
        } else {
          cursor += 1;
          if (current === '"') break;
        }
      }
      let after = cursor;
      while (JSON_WHITESPACE.test(source[after] ?? "")) after += 1;
      tokens.push({
        kind: source[after] === ":" ? "key" : "string",
        text: source.slice(start, cursor),
      });
      continue;
    }
    if (JSON_NUMBER_CHARACTER.test(character)) {
      const start = cursor;
      while (JSON_NUMBER_CHARACTER.test(source[cursor] ?? "")) cursor += 1;
      tokens.push({ kind: "number", text: source.slice(start, cursor) });
      continue;
    }
    const keyword = source.startsWith("true", cursor)
      ? "true"
      : source.startsWith("false", cursor)
        ? "false"
        : source.startsWith("null", cursor)
          ? "null"
          : character;
    tokens.push({
      kind:
        keyword === "true" || keyword === "false"
          ? "boolean"
          : keyword === "null"
            ? "null"
            : "string",
      text: keyword,
    });
    cursor += keyword.length;
  }

  return tokens;
}
