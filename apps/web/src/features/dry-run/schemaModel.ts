// WEB-04 compiles the documented, bounded JSON Schema subset into a safe UI model.

export const INPUT_POINTER = "/input";

const DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema";
const SAFE_PROPERTY_NAME = /^[A-Za-z0-9_.-]{1,100}$/u;
const UNSAFE_KEYS = new Set(["__proto__", "prototype", "constructor"]);
const SUPPORTED_FORMATS = new Set([
  "date",
  "date-time",
  "uri",
  "email",
] as const);
const SUPPORTED_UI_CONTROLS = new Set(["text", "textarea"] as const);
const DATE_VALUE = /^(\d{4})-(\d{2})-(\d{2})$/u;
const DATE_TIME_VALUE =
  /^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$/u;
const URI_SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/u;
const EMAIL_VALUE = /^[^\s@]+@[^\s@]+$/u;

const MAX_SCHEMA_DEPTH = 8;
const MAX_SCHEMA_NODES = 256;
const MAX_OBJECT_PROPERTIES = 64;
const MAX_ARRAY_ITEMS = 32;
const MAX_ENUM_VALUES = 64;
const MAX_EXAMPLES = 8;
const MAX_STRING_LENGTH = 100_000;
const MAX_PATTERN_LENGTH = 256;
const MAX_TITLE_LENGTH = 120;
const MAX_DESCRIPTION_LENGTH = 500;
const MAX_UI_ORDER = 10_000;

// JavaScript regexes have no execution timeout. Keep the general policy simple,
// and explicitly review the rare delimiter-safe nested repeat used by catalog v1.
const REVIEWED_SAFE_NESTED_REPEAT_PATTERNS = new Set([
  "^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
]);

const COMMON_KEYS = new Set([
  "$schema",
  "$id",
  "type",
  "title",
  "description",
  "default",
  "examples",
  "x-sensitive",
  "x-ui",
]);
const OBJECT_KEYS = new Set([
  ...COMMON_KEYS,
  "additionalProperties",
  "properties",
  "required",
]);
const STRING_KEYS = new Set([
  ...COMMON_KEYS,
  "enum",
  "minLength",
  "maxLength",
  "pattern",
  "format",
]);
const NUMBER_KEYS = new Set([
  ...COMMON_KEYS,
  "enum",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
]);
const BOOLEAN_KEYS = new Set([...COMMON_KEYS, "enum"]);
const ARRAY_KEYS = new Set([...COMMON_KEYS, "items", "minItems", "maxItems"]);

export type JsonInputValue =
  string | number | boolean | readonly JsonInputValue[] | JsonInputObject;

// A recursive interface is required here; a Record alias creates a circular alias.
export interface JsonInputObject {
  readonly [key: string]: JsonInputValue;
}

export type SchemaDraftObject = Record<string, unknown>;

export interface CompiledUiHints {
  readonly control: "text" | "textarea" | null;
  readonly order: number | null;
  readonly help: string | null;
}

interface CompiledBase<Kind extends string, Value extends JsonInputValue> {
  readonly kind: Kind;
  readonly pointer: string;
  readonly title: string;
  readonly description: string | null;
  readonly sensitive: boolean;
  readonly examples: readonly Value[];
  readonly ui: CompiledUiHints;
  readonly hasDefault: boolean;
  readonly defaultValue: Value | undefined;
}

export interface CompiledProperty {
  readonly name: string;
  readonly required: boolean;
  readonly schema: CompiledSchema;
}

export interface CompiledObjectSchema extends CompiledBase<
  "object",
  JsonInputObject
> {
  readonly properties: readonly CompiledProperty[];
  readonly additionalProperties: false;
}

export interface CompiledStringSchema extends CompiledBase<"string", string> {
  readonly enumValues: readonly string[] | null;
  readonly minLength: number;
  readonly maxLength: number;
  readonly pattern: string | null;
  readonly format: "date" | "date-time" | "uri" | "email" | null;
}

interface CompiledNumericBase<
  Kind extends "number" | "integer",
> extends CompiledBase<Kind, number> {
  readonly enumValues: readonly number[] | null;
  readonly minimum: number | null;
  readonly exclusiveMinimum: boolean;
  readonly maximum: number | null;
  readonly exclusiveMaximum: boolean;
}

export type CompiledNumberSchema = CompiledNumericBase<"number">;

export type CompiledIntegerSchema = CompiledNumericBase<"integer">;

export interface CompiledBooleanSchema extends CompiledBase<
  "boolean",
  boolean
> {
  readonly enumValues: readonly boolean[] | null;
}

export interface CompiledArraySchema extends CompiledBase<
  "array",
  readonly JsonInputValue[]
> {
  readonly items: CompiledSchema;
  readonly minItems: number;
  readonly maxItems: number;
}

export type CompiledSchema =
  | CompiledObjectSchema
  | CompiledStringSchema
  | CompiledNumberSchema
  | CompiledIntegerSchema
  | CompiledBooleanSchema
  | CompiledArraySchema;

export class InputSchemaCompileError extends Error {
  readonly code: string;
  readonly pointer: string;

  constructor(code: string, pointer: string, message: string) {
    super(message);
    this.name = "InputSchemaCompileError";
    this.code = code;
    this.pointer = pointer;
  }
}

interface CompileState {
  nodes: number;
}

interface CommonFields {
  readonly pointer: string;
  readonly title: string;
  readonly description: string | null;
  readonly sensitive: boolean;
  readonly ui: CompiledUiHints;
}

type JsonRecord = Record<string, unknown>;

function fail(code: string, pointer: string, message: string): never {
  throw new InputSchemaCompileError(code, pointer, message);
}

function isRecord(value: unknown): value is JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return false;
  const prototype = Object.getPrototypeOf(value) as unknown;
  return prototype === Object.prototype || prototype === null;
}

function recordAt(value: unknown, pointer: string): JsonRecord {
  if (!isRecord(value)) {
    fail(
      "schema_not_object",
      pointer,
      "The input schema must contain an object here.",
    );
  }
  for (const key of Object.keys(value)) {
    if (UNSAFE_KEYS.has(key)) {
      fail(
        "unsafe_schema_key",
        pointer,
        "The input schema contains an unsafe key.",
      );
    }
  }
  return value;
}

function propertiesAt(value: unknown, pointer: string): JsonRecord {
  if (!isRecord(value)) {
    fail(
      "schema_not_object",
      pointer,
      "The input schema properties must be an object.",
    );
  }
  return value;
}

function exactKeys(
  record: JsonRecord,
  allowed: ReadonlySet<string>,
  pointer: string,
): void {
  if (Object.keys(record).some((key) => !allowed.has(key))) {
    fail(
      "unsupported_schema_keyword",
      pointer,
      "The input schema uses a keyword that this form does not support.",
    );
  }
}

function boundedString(
  value: unknown,
  pointer: string,
  label: string,
  maximum: number,
): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum ||
    value.trim() !== value
  ) {
    fail(
      "invalid_schema_annotation",
      pointer,
      `${label} must be a bounded string.`,
    );
  }
  return value;
}

function optionalString(
  record: JsonRecord,
  key: string,
  pointer: string,
  maximum: number,
): string | null {
  if (!Object.hasOwn(record, key)) return null;
  return boundedString(record[key], pointer, key, maximum);
}

interface PatternGroupState {
  variableRepeatCount: number;
}

interface PatternAtomState {
  readonly group: boolean;
  readonly containsVariableRepeat: boolean;
  quantified: boolean;
}

interface PatternRepeat {
  readonly end: number;
  readonly variable: boolean;
  readonly repeatsMultiple: boolean;
}

function bracedRepeatAt(pattern: string, index: number): PatternRepeat | null {
  const match = /^\{([0-9]+)(?:,([0-9]*))?\}/u.exec(pattern.slice(index));
  if (match === null) return null;
  const minimumText = match[1];
  const maximumText = match[2];
  if (minimumText === undefined) return null;
  const minimum = Number(minimumText);
  const maximum =
    maximumText === undefined
      ? minimum
      : maximumText.length === 0
        ? Number.POSITIVE_INFINITY
        : Number(maximumText);
  return {
    end: index + match[0].length - 1,
    variable: maximum !== minimum,
    repeatsMultiple: maximum > 1,
  };
}

function conservativelySafePattern(pattern: string): boolean {
  if (!pattern.startsWith("^") || pattern.length < 2) return false;

  const groups: PatternGroupState[] = [{ variableRepeatCount: 0 }];
  let lastAtom: PatternAtomState | null = null;
  let inCharacterClass = false;
  let escaped = false;
  let sawEndAnchor = false;

  for (let index = 0; index < pattern.length; index += 1) {
    const character = pattern[index];
    if (character === undefined) return false;
    if (escaped) {
      if (
        !inCharacterClass &&
        (/^[1-9]$/u.test(character) ||
          (character === "k" && pattern[index + 1] === "<"))
      ) {
        return false;
      }
      escaped = false;
      if (!inCharacterClass) {
        lastAtom = {
          group: false,
          containsVariableRepeat: false,
          quantified: false,
        };
      }
      continue;
    }
    if (character === "\\") {
      escaped = true;
      continue;
    }
    if (inCharacterClass) {
      if (character === "]") {
        inCharacterClass = false;
        lastAtom = {
          group: false,
          containsVariableRepeat: false,
          quantified: false,
        };
      }
      continue;
    }
    if (character === "[") {
      inCharacterClass = true;
      lastAtom = null;
      continue;
    }
    if (character === "(") {
      if (pattern.slice(index, index + 3) !== "(?:") return false;
      groups.push({ variableRepeatCount: 0 });
      lastAtom = null;
      index += 2;
      continue;
    }
    if (character === ")") {
      if (groups.length === 1) return false;
      const closed = groups.pop();
      const parent = groups.at(-1);
      if (closed === undefined || parent === undefined) return false;
      parent.variableRepeatCount += closed.variableRepeatCount;
      if (
        parent.variableRepeatCount > 1 &&
        !REVIEWED_SAFE_NESTED_REPEAT_PATTERNS.has(pattern)
      ) {
        return false;
      }
      lastAtom = {
        group: true,
        containsVariableRepeat: closed.variableRepeatCount > 0,
        quantified: false,
      };
      continue;
    }
    if (character === "|" || character === ".") return false;
    if (character === "^") {
      if (index !== 0) return false;
      lastAtom = null;
      continue;
    }
    if (character === "$") {
      if (index !== pattern.length - 1) return false;
      sawEndAnchor = true;
      lastAtom = null;
      continue;
    }

    let repeat: PatternRepeat | null = null;
    if (character === "*" || character === "+") {
      repeat = {
        end: index,
        variable: true,
        repeatsMultiple: true,
      };
    } else if (character === "?") {
      repeat = {
        end: index,
        variable: true,
        repeatsMultiple: false,
      };
    } else if (character === "{") {
      repeat = bracedRepeatAt(pattern, index);
      if (repeat === null) return false;
    }
    if (repeat !== null) {
      if (lastAtom === null || lastAtom.quantified) return false;
      if (
        lastAtom.group &&
        lastAtom.containsVariableRepeat &&
        repeat.repeatsMultiple &&
        !REVIEWED_SAFE_NESTED_REPEAT_PATTERNS.has(pattern)
      ) {
        return false;
      }
      lastAtom.quantified = true;
      if (repeat.variable) {
        const group = groups.at(-1);
        if (group === undefined) return false;
        group.variableRepeatCount += 1;
        if (
          group.variableRepeatCount > 1 &&
          !REVIEWED_SAFE_NESTED_REPEAT_PATTERNS.has(pattern)
        ) {
          return false;
        }
      }
      index = repeat.end;
      continue;
    }

    lastAtom = {
      group: false,
      containsVariableRepeat: false,
      quantified: false,
    };
  }

  return !escaped && !inCharacterClass && groups.length === 1 && sawEndAnchor;
}

function integerAt(
  value: unknown,
  minimum: number,
  maximum: number,
  pointer: string,
  label: string,
): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    fail(
      "invalid_schema_bound",
      pointer,
      `${label} must be a bounded integer.`,
    );
  }
  return value;
}

function finiteNumberAt(
  value: unknown,
  pointer: string,
  label: string,
): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    fail("invalid_schema_bound", pointer, `${label} must be a finite number.`);
  }
  return value;
}

function humanize(name: string): string {
  const words = name
    .split(/[._-]+/u)
    .filter((part) => part.length > 0)
    .join(" ");
  if (words.length === 0) return "Input";
  return `${words.slice(0, 1).toUpperCase()}${words.slice(1)}`;
}

function parseUi(record: JsonRecord, pointer: string): CompiledUiHints {
  if (!Object.hasOwn(record, "x-ui")) {
    return Object.freeze({ control: null, order: null, help: null });
  }
  const ui = recordAt(record["x-ui"], pointer);
  const allowed = new Set(["control", "order", "help"]);
  if (Object.keys(ui).some((key) => !allowed.has(key))) {
    fail(
      "unsupported_ui_annotation",
      pointer,
      "The input schema uses an unsupported presentation annotation.",
    );
  }
  let control: "text" | "textarea" | null = null;
  if (Object.hasOwn(ui, "control")) {
    if (
      typeof ui.control !== "string" ||
      !SUPPORTED_UI_CONTROLS.has(ui.control as "text" | "textarea")
    ) {
      fail(
        "unsupported_ui_annotation",
        pointer,
        "The input schema requests an unsupported form control.",
      );
    }
    control = ui.control as "text" | "textarea";
  }
  const order = Object.hasOwn(ui, "order")
    ? integerAt(ui.order, -MAX_UI_ORDER, MAX_UI_ORDER, pointer, "x-ui order")
    : null;
  const help = Object.hasOwn(ui, "help")
    ? boundedString(ui.help, pointer, "x-ui help", MAX_DESCRIPTION_LENGTH)
    : null;
  return Object.freeze({ control, order, help });
}

function commonFields(
  record: JsonRecord,
  pointer: string,
  titleHint: string,
  inheritedSensitive: boolean,
): CommonFields {
  if (Object.hasOwn(record, "$schema") && record.$schema !== DRAFT_2020_12) {
    fail(
      "unsupported_schema_dialect",
      pointer,
      "The input schema dialect is unsupported.",
    );
  }
  if (Object.hasOwn(record, "$id")) {
    boundedString(record.$id, pointer, "$id", 500);
  }
  const title = Object.hasOwn(record, "title")
    ? boundedString(record.title, pointer, "title", MAX_TITLE_LENGTH)
    : humanize(titleHint);
  const description = optionalString(
    record,
    "description",
    pointer,
    MAX_DESCRIPTION_LENGTH,
  );
  if (
    Object.hasOwn(record, "x-sensitive") &&
    typeof record["x-sensitive"] !== "boolean"
  ) {
    fail(
      "invalid_sensitive_annotation",
      pointer,
      "The input schema sensitivity annotation must be boolean.",
    );
  }
  const sensitive =
    inheritedSensitive ||
    (Object.hasOwn(record, "x-sensitive") && record["x-sensitive"] === true);
  return {
    pointer,
    title,
    description,
    sensitive,
    ui: parseUi(record, pointer),
  };
}

function safePropertyName(value: string, pointer: string): void {
  if (!SAFE_PROPERTY_NAME.test(value) || UNSAFE_KEYS.has(value)) {
    fail(
      "unsafe_property_name",
      pointer,
      "The input schema contains a property that cannot be addressed safely.",
    );
  }
}

function parseRequired(
  record: JsonRecord,
  properties: JsonRecord,
  pointer: string,
): ReadonlySet<string> {
  if (!Object.hasOwn(record, "required")) return new Set<string>();
  if (
    !Array.isArray(record.required) ||
    record.required.length > MAX_OBJECT_PROPERTIES
  ) {
    fail(
      "invalid_required_fields",
      pointer,
      "The input schema required fields are invalid.",
    );
  }
  const required = new Set<string>();
  for (const item of record.required) {
    if (typeof item !== "string") {
      fail(
        "invalid_required_fields",
        pointer,
        "The input schema required fields are invalid.",
      );
    }
    safePropertyName(item, pointer);
    if (required.has(item) || !Object.hasOwn(properties, item)) {
      fail(
        "invalid_required_fields",
        pointer,
        "The input schema required fields are invalid.",
      );
    }
    required.add(item);
  }
  return required;
}

function parseStringEnum(
  value: unknown,
  pointer: string,
): readonly string[] | null {
  if (value === undefined) return null;
  if (
    !Array.isArray(value) ||
    value.length === 0 ||
    value.length > MAX_ENUM_VALUES
  ) {
    fail("invalid_schema_enum", pointer, "The input schema enum is invalid.");
  }
  const values: string[] = [];
  for (const item of value) {
    if (typeof item !== "string" || item.length > MAX_STRING_LENGTH) {
      fail("invalid_schema_enum", pointer, "The input schema enum is invalid.");
    }
    values.push(item);
  }
  if (new Set(values).size !== values.length) {
    fail("invalid_schema_enum", pointer, "The input schema enum is invalid.");
  }
  return Object.freeze(values);
}

function parseNumericEnum(
  value: unknown,
  pointer: string,
  integer: boolean,
): readonly number[] | null {
  if (value === undefined) return null;
  if (
    !Array.isArray(value) ||
    value.length === 0 ||
    value.length > MAX_ENUM_VALUES
  ) {
    fail("invalid_schema_enum", pointer, "The input schema enum is invalid.");
  }
  const values: number[] = [];
  for (const item of value) {
    if (
      typeof item !== "number" ||
      !Number.isFinite(item) ||
      (integer && !Number.isSafeInteger(item))
    ) {
      fail("invalid_schema_enum", pointer, "The input schema enum is invalid.");
    }
    values.push(item);
  }
  if (new Set(values).size !== values.length) {
    fail("invalid_schema_enum", pointer, "The input schema enum is invalid.");
  }
  return Object.freeze(values);
}

function parseBooleanEnum(
  value: unknown,
  pointer: string,
): readonly boolean[] | null {
  if (value === undefined) return null;
  if (!Array.isArray(value) || value.length === 0 || value.length > 2) {
    fail("invalid_schema_enum", pointer, "The input schema enum is invalid.");
  }
  const values: boolean[] = [];
  for (const item of value) {
    if (typeof item !== "boolean") {
      fail("invalid_schema_enum", pointer, "The input schema enum is invalid.");
    }
    values.push(item);
  }
  if (new Set(values).size !== values.length) {
    fail("invalid_schema_enum", pointer, "The input schema enum is invalid.");
  }
  return Object.freeze(values);
}

function numericBounds(
  record: JsonRecord,
  pointer: string,
): Pick<
  CompiledNumberSchema,
  "minimum" | "exclusiveMinimum" | "maximum" | "exclusiveMaximum"
> {
  if (
    Object.hasOwn(record, "minimum") &&
    Object.hasOwn(record, "exclusiveMinimum")
  ) {
    fail(
      "ambiguous_schema_bound",
      pointer,
      "The input schema lower bound is ambiguous.",
    );
  }
  if (
    Object.hasOwn(record, "maximum") &&
    Object.hasOwn(record, "exclusiveMaximum")
  ) {
    fail(
      "ambiguous_schema_bound",
      pointer,
      "The input schema upper bound is ambiguous.",
    );
  }
  const hasInclusiveMinimum = Object.hasOwn(record, "minimum");
  const hasExclusiveMinimum = Object.hasOwn(record, "exclusiveMinimum");
  const hasInclusiveMaximum = Object.hasOwn(record, "maximum");
  const hasExclusiveMaximum = Object.hasOwn(record, "exclusiveMaximum");
  const minimum = hasInclusiveMinimum
    ? finiteNumberAt(record.minimum, pointer, "minimum")
    : hasExclusiveMinimum
      ? finiteNumberAt(record.exclusiveMinimum, pointer, "exclusiveMinimum")
      : null;
  const maximum = hasInclusiveMaximum
    ? finiteNumberAt(record.maximum, pointer, "maximum")
    : hasExclusiveMaximum
      ? finiteNumberAt(record.exclusiveMaximum, pointer, "exclusiveMaximum")
      : null;
  if (minimum !== null && maximum !== null && minimum > maximum) {
    fail(
      "invalid_schema_bound",
      pointer,
      "The input schema numeric bounds are invalid.",
    );
  }
  if (
    minimum !== null &&
    maximum !== null &&
    minimum === maximum &&
    (hasExclusiveMinimum || hasExclusiveMaximum)
  ) {
    fail(
      "invalid_schema_bound",
      pointer,
      "The input schema numeric bounds are empty.",
    );
  }
  return {
    minimum,
    exclusiveMinimum: hasExclusiveMinimum,
    maximum,
    exclusiveMaximum: hasExclusiveMaximum,
  };
}

function emptyAnnotations<Value extends JsonInputValue>(): Pick<
  CompiledBase<string, Value>,
  "examples" | "hasDefault" | "defaultValue"
> {
  return {
    examples: Object.freeze<Value[]>([]),
    hasDefault: false,
    defaultValue: undefined,
  };
}

function compileObject(
  record: JsonRecord,
  common: CommonFields,
  depth: number,
  state: CompileState,
): CompiledObjectSchema {
  exactKeys(record, OBJECT_KEYS, common.pointer);
  if (common.ui.control !== null) {
    fail(
      "unsupported_ui_annotation",
      common.pointer,
      "Object fields cannot request a text presentation control.",
    );
  }
  if (record.additionalProperties !== false) {
    fail(
      "unbounded_object",
      common.pointer,
      "Object schemas must set additionalProperties to false.",
    );
  }
  const rawProperties = propertiesAt(record.properties, common.pointer);
  const names = Object.keys(rawProperties);
  if (names.length > MAX_OBJECT_PROPERTIES) {
    fail(
      "too_many_properties",
      common.pointer,
      "The input schema has too many fields.",
    );
  }
  const required = parseRequired(record, rawProperties, common.pointer);
  const indexed = names.map((name, index) => {
    safePropertyName(name, common.pointer);
    const schema = compileNode(
      rawProperties[name],
      `${common.pointer}/${name}`,
      name,
      common.sensitive,
      depth + 1,
      state,
      false,
    );
    return {
      index,
      property: Object.freeze({ name, required: required.has(name), schema }),
    };
  });
  indexed.sort((left, right) => {
    const leftOrder = left.property.schema.ui.order;
    const rightOrder = right.property.schema.ui.order;
    if (leftOrder !== null || rightOrder !== null) {
      const orderDifference =
        (leftOrder ?? Number.MAX_SAFE_INTEGER) -
        (rightOrder ?? Number.MAX_SAFE_INTEGER);
      if (orderDifference !== 0) return orderDifference;
    }
    if (
      leftOrder === null &&
      rightOrder === null &&
      left.property.required !== right.property.required
    ) {
      return left.property.required ? -1 : 1;
    }
    return left.index - right.index;
  });
  return Object.freeze({
    kind: "object",
    ...common,
    ...emptyAnnotations<JsonInputObject>(),
    properties: Object.freeze(indexed.map(({ property }) => property)),
    additionalProperties: false,
  });
}

function compileString(
  record: JsonRecord,
  common: CommonFields,
): CompiledStringSchema {
  exactKeys(record, STRING_KEYS, common.pointer);
  const enumValues = parseStringEnum(record.enum, common.pointer);
  const minLength = Object.hasOwn(record, "minLength")
    ? integerAt(
        record.minLength,
        0,
        MAX_STRING_LENGTH,
        common.pointer,
        "minLength",
      )
    : 0;
  const maxLength = Object.hasOwn(record, "maxLength")
    ? integerAt(
        record.maxLength,
        0,
        MAX_STRING_LENGTH,
        common.pointer,
        "maxLength",
      )
    : enumValues === null
      ? fail(
          "unbounded_string",
          common.pointer,
          "String schemas must set maxLength or a finite enum.",
        )
      : Math.max(...enumValues.map((value) => Array.from(value).length));
  if (minLength > maxLength) {
    fail(
      "invalid_schema_bound",
      common.pointer,
      "The input schema string bounds are invalid.",
    );
  }
  const pattern = optionalString(
    record,
    "pattern",
    common.pointer,
    MAX_PATTERN_LENGTH,
  );
  if (pattern !== null) {
    try {
      new RegExp(pattern, "u");
    } catch {
      fail(
        "invalid_schema_pattern",
        common.pointer,
        "The input schema pattern is invalid.",
      );
    }
    if (!conservativelySafePattern(pattern)) {
      fail(
        "unsafe_schema_pattern",
        common.pointer,
        "The input schema pattern is outside the safe form subset.",
      );
    }
  }
  let format: CompiledStringSchema["format"] = null;
  if (Object.hasOwn(record, "format")) {
    if (
      typeof record.format !== "string" ||
      !SUPPORTED_FORMATS.has(
        record.format as NonNullable<CompiledStringSchema["format"]>,
      )
    ) {
      fail(
        "unsupported_schema_format",
        common.pointer,
        "The input schema format is unsupported.",
      );
    }
    format = record.format as NonNullable<CompiledStringSchema["format"]>;
  }
  if (
    common.ui.control !== null &&
    format !== null &&
    common.ui.control === "textarea"
  ) {
    fail(
      "unsupported_ui_annotation",
      common.pointer,
      "Formatted strings cannot request a textarea control.",
    );
  }
  return Object.freeze({
    kind: "string",
    ...common,
    ...emptyAnnotations<string>(),
    enumValues,
    minLength,
    maxLength,
    pattern,
    format,
  });
}

function compileNumeric(
  record: JsonRecord,
  common: CommonFields,
  integer: boolean,
): CompiledNumberSchema | CompiledIntegerSchema {
  exactKeys(record, NUMBER_KEYS, common.pointer);
  if (common.ui.control !== null) {
    fail(
      "unsupported_ui_annotation",
      common.pointer,
      "Numeric fields cannot request a text presentation control.",
    );
  }
  const fields = {
    ...common,
    ...emptyAnnotations<number>(),
    enumValues: parseNumericEnum(record.enum, common.pointer, integer),
    ...numericBounds(record, common.pointer),
  };
  return integer
    ? Object.freeze({ kind: "integer", ...fields })
    : Object.freeze({ kind: "number", ...fields });
}

function compileBoolean(
  record: JsonRecord,
  common: CommonFields,
): CompiledBooleanSchema {
  exactKeys(record, BOOLEAN_KEYS, common.pointer);
  if (common.ui.control !== null) {
    fail(
      "unsupported_ui_annotation",
      common.pointer,
      "Boolean fields cannot request a text presentation control.",
    );
  }
  return Object.freeze({
    kind: "boolean",
    ...common,
    ...emptyAnnotations<boolean>(),
    enumValues: parseBooleanEnum(record.enum, common.pointer),
  });
}

function compileArray(
  record: JsonRecord,
  common: CommonFields,
  depth: number,
  state: CompileState,
): CompiledArraySchema {
  exactKeys(record, ARRAY_KEYS, common.pointer);
  if (common.ui.control !== null) {
    fail(
      "unsupported_ui_annotation",
      common.pointer,
      "Array fields cannot request a text presentation control.",
    );
  }
  if (!Object.hasOwn(record, "items")) {
    fail(
      "unbounded_array",
      common.pointer,
      "Array schemas must define one item schema.",
    );
  }
  const maxItems = Object.hasOwn(record, "maxItems")
    ? integerAt(record.maxItems, 0, MAX_ARRAY_ITEMS, common.pointer, "maxItems")
    : fail(
        "unbounded_array",
        common.pointer,
        "Array schemas must set maxItems.",
      );
  const minItems = Object.hasOwn(record, "minItems")
    ? integerAt(record.minItems, 0, maxItems, common.pointer, "minItems")
    : 0;
  const items = compileNode(
    record.items,
    `${common.pointer}/*`,
    `${common.title} item`,
    common.sensitive,
    depth + 1,
    state,
    false,
  );
  if (items.kind === "array") {
    fail(
      "unsupported_nested_array",
      common.pointer,
      "Nested arrays are not supported by this form.",
    );
  }
  return Object.freeze({
    kind: "array",
    ...common,
    ...emptyAnnotations<readonly JsonInputValue[]>(),
    items,
    minItems,
    maxItems,
  });
}

function scalarInEnum<Value extends string | number | boolean>(
  value: Value,
  values: readonly Value[] | null,
): boolean {
  return values === null || values.includes(value);
}

function validDate(value: string): boolean {
  const match = DATE_VALUE.exec(value);
  if (match === null) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(0, 0, 0, 0);
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}

function validUri(value: string): boolean {
  if (!URI_SCHEME.test(value)) return false;
  try {
    const parsed = new URL(value);
    return parsed.protocol.length > 1;
  } catch {
    return false;
  }
}

function validStringFormat(
  format: CompiledStringSchema["format"],
  value: string,
): boolean {
  switch (format) {
    case null:
      return true;
    case "date":
      return validDate(value);
    case "date-time":
      return (
        DATE_TIME_VALUE.test(value) &&
        validDate(value.slice(0, 10)) &&
        Number.isFinite(Date.parse(value))
      );
    case "uri":
      return validUri(value);
    case "email":
      return EMAIL_VALUE.test(value);
  }
}

function normalizeLiteral(
  schema: CompiledSchema,
  value: unknown,
  pointer: string,
): JsonInputValue {
  switch (schema.kind) {
    case "string": {
      if (typeof value !== "string") {
        fail(
          "invalid_schema_annotation",
          pointer,
          "A schema value does not match its field.",
        );
      }
      const length = Array.from(value).length;
      if (
        length < schema.minLength ||
        length > schema.maxLength ||
        !scalarInEnum(value, schema.enumValues) ||
        (schema.pattern !== null &&
          !new RegExp(schema.pattern, "u").test(value)) ||
        !validStringFormat(schema.format, value)
      ) {
        fail(
          "invalid_schema_annotation",
          pointer,
          "A schema value does not match its field.",
        );
      }
      return value;
    }
    case "number":
    case "integer": {
      if (
        typeof value !== "number" ||
        !Number.isFinite(value) ||
        (schema.kind === "integer" && !Number.isSafeInteger(value)) ||
        !scalarInEnum(value, schema.enumValues) ||
        (schema.minimum !== null &&
          (schema.exclusiveMinimum
            ? value <= schema.minimum
            : value < schema.minimum)) ||
        (schema.maximum !== null &&
          (schema.exclusiveMaximum
            ? value >= schema.maximum
            : value > schema.maximum))
      ) {
        fail(
          "invalid_schema_annotation",
          pointer,
          "A schema value does not match its field.",
        );
      }
      return value;
    }
    case "boolean":
      if (
        typeof value !== "boolean" ||
        !scalarInEnum(value, schema.enumValues)
      ) {
        fail(
          "invalid_schema_annotation",
          pointer,
          "A schema value does not match its field.",
        );
      }
      return value;
    case "array": {
      if (
        !Array.isArray(value) ||
        value.length < schema.minItems ||
        value.length > schema.maxItems
      ) {
        fail(
          "invalid_schema_annotation",
          pointer,
          "A schema value does not match its field.",
        );
      }
      return Object.freeze(
        value.map((item, index) =>
          normalizeLiteral(schema.items, item, `${pointer}/${String(index)}`),
        ),
      );
    }
    case "object": {
      const object = recordAt(value, pointer);
      const propertyByName = new Map(
        schema.properties.map((property) => [property.name, property]),
      );
      if (Object.keys(object).some((name) => !propertyByName.has(name))) {
        fail(
          "invalid_schema_annotation",
          pointer,
          "A schema value does not match its field.",
        );
      }
      const result: Record<string, JsonInputValue> = {};
      for (const property of schema.properties) {
        if (!Object.hasOwn(object, property.name)) {
          if (property.required) {
            fail(
              "invalid_schema_annotation",
              pointer,
              "A schema value does not match its field.",
            );
          }
          continue;
        }
        result[property.name] = normalizeLiteral(
          property.schema,
          object[property.name],
          `${pointer}/${property.name}`,
        );
      }
      return Object.freeze(result);
    }
  }
}

function withAnnotations(
  schema: CompiledSchema,
  record: JsonRecord,
): CompiledSchema {
  const hasDefault = Object.hasOwn(record, "default");
  const defaultValue = hasDefault
    ? normalizeLiteral(schema, record.default, schema.pointer)
    : undefined;
  let examples: readonly JsonInputValue[] = Object.freeze([]);
  if (Object.hasOwn(record, "examples")) {
    if (
      !Array.isArray(record.examples) ||
      record.examples.length > MAX_EXAMPLES
    ) {
      fail(
        "invalid_schema_examples",
        schema.pointer,
        "The input schema examples annotation is invalid.",
      );
    }
    examples = Object.freeze(
      record.examples.map((value) =>
        normalizeLiteral(schema, value, schema.pointer),
      ),
    );
  }
  return Object.freeze({
    ...schema,
    hasDefault,
    defaultValue,
    examples,
  }) as CompiledSchema;
}

function compileNode(
  raw: unknown,
  pointer: string,
  titleHint: string,
  inheritedSensitive: boolean,
  depth: number,
  state: CompileState,
  root: boolean,
): CompiledSchema {
  if (depth > MAX_SCHEMA_DEPTH) {
    fail(
      "schema_too_deep",
      pointer,
      "The input schema is too deeply nested for this form.",
    );
  }
  state.nodes += 1;
  if (state.nodes > MAX_SCHEMA_NODES) {
    fail("schema_too_large", pointer, "The input schema has too many fields.");
  }
  const record = recordAt(raw, pointer);
  if (typeof record.type !== "string") {
    fail(
      "unsupported_schema_type",
      pointer,
      "The input schema field type is unsupported.",
    );
  }
  if (root && record.type !== "object") {
    fail(
      "root_schema_not_object",
      pointer,
      "The dry-run input schema must be an object.",
    );
  }
  const common = commonFields(record, pointer, titleHint, inheritedSensitive);
  let schema: CompiledSchema;
  switch (record.type) {
    case "object":
      schema = compileObject(record, common, depth, state);
      break;
    case "string":
      schema = compileString(record, common);
      break;
    case "number":
      schema = compileNumeric(record, common, false);
      break;
    case "integer":
      schema = compileNumeric(record, common, true);
      break;
    case "boolean":
      schema = compileBoolean(record, common);
      break;
    case "array":
      schema = compileArray(record, common, depth, state);
      break;
    default:
      fail(
        "unsupported_schema_type",
        pointer,
        "The input schema field type is unsupported.",
      );
  }
  return withAnnotations(schema, record);
}

export function compileInputSchema(raw: unknown): CompiledObjectSchema {
  const compiled = compileNode(
    raw,
    INPUT_POINTER,
    "Input",
    false,
    0,
    { nodes: 0 },
    true,
  );
  if (compiled.kind !== "object") {
    fail(
      "root_schema_not_object",
      INPUT_POINTER,
      "The dry-run input schema must be an object.",
    );
  }
  return compiled;
}
