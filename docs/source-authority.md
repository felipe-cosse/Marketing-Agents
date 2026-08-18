# Source authority

The local frames are evidence of the visible hierarchy, role names, role intent, and Community card multiplicity. They are not a specification of hidden integrations or runtime behavior. The normalized textual catalog in `IMPLEMENTATION_PROMPT.md` wins if a frame label and the prompt differ.

`catalog/source-evidence.json` is the machine-checkable evidence index. Its hashes make accidental reference replacement visible without treating the images as executable configuration.

## Allowed conclusions

- Marketing Agents is the root, with five departments and twelve functions.
- The prompt's 36 named roles are reusable templates.
- The seven Community templates each have two deployed instances.
- Names and purposes come from the prompt; the implementation may add safety constraints without changing that intent.

## Prohibited inferences

- A logo does not authorize or select a provider, connector, credential, endpoint, or scraping strategy.
- Duplicate Community cards do not imply regions, audiences, shifts, or any other business distinction.
- A role card does not prove a trigger, schedule, retry policy, write permission, or autonomous action.
- Source branding is not copied into the product UI. Connector bindings remain vendor-neutral configuration.

Community instances therefore use only stable ordinal identity (`.01` and `.02`) by default. `variant_label` and the business reason remain null until explicit evidence or operator configuration exists.
