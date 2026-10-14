import type { components } from "./generated/schema";

type CamelKey<Key extends PropertyKey> =
  Key extends `${infer Head}_${infer Tail}`
    ? `${Head}${Capitalize<CamelKey<Tail>>}`
    : Key;

// EXEC-05: transport schemas remain generated authority. Normalizers still
// validate unknown JSON and explicitly supply nullable optional response fields.
// This type performs no runtime conversion and grants no trust to fetched data.
export type NormalizedContract<Value> = Value extends readonly (infer Item)[]
  ? readonly NormalizedContract<Item>[]
  : Value extends object
    ? {
        readonly [Key in keyof Value as CamelKey<Key>]-?: NormalizedContract<
          Exclude<Value[Key], undefined>
        >;
      }
    : Value;

export type ApiView<Name extends keyof components["schemas"]> =
  NormalizedContract<components["schemas"][Name]>;
