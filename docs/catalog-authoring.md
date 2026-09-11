# Catalog authoring

The versioned catalog defines the source-backed organization and reusable agent
templates. It does not contain credentials, install providers, or automatically
implement new executable workflows. This guide documents the existing compiler
and seed interfaces; follow the repository’s requirement/change approvals before
changing the authoritative inventory or its release lock.

## Authority and current inventory

Claim: Implemented and verified —
[`catalog/v1/manifest.yaml`](../catalog/v1/manifest.yaml) selects the source files;
[`release.lock.json`](../catalog/v1/release.lock.json) records content version
`1.0.0` and the semantic hash. The fixed v1 contract is **5 departments, 12 functions,
36 templates, 43 instances**, distributed as Social Media 12, Blog/SEO 6, Email 5,
Community 14, and Partnerships 6. The
[compiler tests](../tests/catalog/test_arch_04_catalog_compiler.py) and
[exact-inventory tests](../tests/catalog/test_cat_01_authoritative_catalog.py)
cover structural, reference, semantic, hash, and inventory checks.

Claim: Assumption — Community’s seven templates each have two distinct instances,
using source ordinals `.01` and `.02`; their default variant labels are null.
The duplicate source visuals do not prove locale, audience, integration, or team
assignments. Keep those interpretations separate from source-backed display names.
See [source authority](source-authority.md), [assumptions](assumptions.md), and the
actual [Community instances](../catalog/v1/instances/community.yaml).

## File and field ownership

Claim: Implemented and verified — records use the exact schemas under
[`catalog/schema`](../catalog/schema); unknown record properties are rejected.

| Source | Owns |
| --- | --- |
| `manifest.yaml` | Format/content versions, schema dialect, file lists, source-evidence paths |
| `departments.yaml`, `functions.yaml` | Stable organization IDs, source display names, hierarchy, order |
| `tool-capabilities.yaml` | Capability IDs, READ/WRITE effect, connector family, idempotency, timeout, classification |
| `approval-policies.yaml` | Approval policy IDs and policy constraints |
| `templates/{department}.yaml` | Reusable agent definitions under a `templates` list |
| `instances/{department}.yaml` | Deployment defaults under an `instances` list |
| `prompts/*.md` | Catalog-controlled system instructions referenced by templates |
| `schemas/{template-id}/input.schema.json` and `output.schema.json` | Draft 2020-12 structured contracts |
| `release.lock.json` | Reviewed semantic release identity and exact counts |

The actual department filenames are `social-media`, `blog-seo`, `email`,
`community`, and `partnerships`; the manifest is authoritative rather than an
implicit directory glob. Start from an existing record, such as
[Email templates](../catalog/v1/templates/email.yaml), not an invented alias.

A [template](../catalog/schema/template.schema.json) owns `id`, `display_name`,
`department_id`, `function_id`, `display_order`, `purpose`, `system_prompt_ref`,
`input_schema_ref`, `output_schema_ref`, `allowed_tool_capability_ids`,
`supported_trigger_types`, `operation_classification`, `approval_policy_id`,
retry/timeout/budget/rate policies, `source_confidence`, `source_references`, and
`implementation_notes`. Optional `output_handling` is `standard` or `advisory`.
Use the schema’s actual bounds; do not invent unbounded retries or execution limits.

An [instance](../catalog/schema/instance.schema.json) references exactly one
`template_id` and owns `id`, `display_order`, `enabled`, `variant`,
`trigger_bindings`, `connector_bindings`, `schedule`, and `configuration_revision`.
Do not copy template purposes, prompts, capabilities, or effects into instances.
Supported trigger kinds are exactly `manual`, `webhook`, and `schedule`.
Connector bindings contain logical family/binding identity and enabled state,
never API keys or secret values.

## Compiler and safety rules

Claim: Implemented and verified — the
[loader](../apps/api/src/marketing_agents/infrastructure/catalog/loader.py)
rejects duplicate YAML keys, unsupported extensions, oversized files, absolute
references, traversal, and symlink-based resource resolution. JSON Schema
[`$ref` checks](../apps/api/src/marketing_agents/infrastructure/catalog/references.py)
reject remote and escaping references; validation does not fetch schemas online.
Keep prompt/schema resources local and bounded.

The [compiler](../apps/api/src/marketing_agents/infrastructure/catalog/compiler.py)
checks stable ID grammar, unique IDs, valid parent/template/capability/policy
references, compatible trigger/configuration semantics, and exact inventory.
A template with any WRITE capability must be `mutating` with a human external-write
policy. A `read_only` template cannot acquire WRITE capability by changing a label.
Prompt/schema content and the manifest/template records participate in the
canonical semantic hash, including their declared file and reference paths.
Renaming those references can change the hash. Absolute checkout location,
mtimes, and load timestamps are not semantic release identity.

Claim: Deterministic mock behavior — a catalog capability or schema is a contract,
not a working vendor connection. The current runtime executes five registered demo
workflows; editing YAML does not register a new workflow, renderer, connector
operation, or real provider. See [adapter contracts](adapter-contracts.md).

## Validate a proposed edit

Claim: Implemented and verified — these are the actual read-only interfaces from
the [catalog CLI](../apps/api/src/marketing_agents/workers/catalog_cli.py),
[release verifier](../scripts/verify_catalog_release.py), and [Makefile](../Makefile).
Run from the repository root after the pinned dependencies are installed as
described in [operations](operations.md):

```sh
uv run --offline --frozen marketing-agents-catalog validate --root catalog/v1
uv run --offline --frozen marketing-agents-catalog compile --root catalog/v1 --format json
make verify-catalog-release
uv run --offline --frozen pytest -q tests/catalog
```

`validate` returns validity/hash or structured issues and a nonzero exit on
failure. `compile --format json` prints **hash and counts**, not a generated full
catalog file. Neither command writes the database. `verify-catalog-release`
compares against the committed lock and does not regenerate it. There is no
`add-template`, `publish`, or lock-update CLI in this repository.

Claim: Acceptance target not yet verified — every proposed catalog change must
earn its own validation and review. Recommended review sequence:

1. Preserve source names and hierarchy; record any new interpretation as an assumption.
2. Edit the manifest-selected record and referenced prompt/schema together; retain
   stable identities and declarative bounds.
3. Run validation and catalog tests. Treat a changed count or department distribution
   as an intentional contract change requiring separate approval, not a check to bypass.
4. For an approved semantic change, choose a new content version and review the
   release-lock hash/count update together with the source change. Do not reuse an
   existing version for different content or refresh a lock merely to hide drift.
5. Validate migration/seed behavior on isolated storage, then review affected
   workflow, adapter, API, and UI contracts. A passing compiler is not execution proof.

## Seed versus deployment configuration

Claim: Implemented and verified — after an approved release, the actual local
commands are `make migrate`, `make seed`, and `make seed-check`. They operate on the
selected database/key pair; unlike compilation, migration and seed can mutate
that installation. Use a separate isolated installation for authoring tests, and
follow [operations](operations.md) for backups and explicit configuration.

The [seed service](../apps/api/src/marketing_agents/infrastructure/catalog/seed.py)
validates the complete compiled catalog before its transaction. It preserves
existing deployment enabled flags, connector/trigger bindings, schedules, variant
labels, and configuration revisions. Editing an instance’s default in YAML does
not force that value over an existing operator override. `seed --check` performs
read-only comparison and no repair. New incompatible overrides, changed stable
identity relationships, corrupt history, and same-version/different-content
releases fail closed. Evidence:
[DEL-04](verification/requirements/DEL-04.md) and
[catalog seed tests](../tests/integration/db/test_del_04_catalog_seed.py).

Claim: Residual risk — a schema-valid catalog still requires trusted review of
prompts, capability assignments, source claims, and human-readable descriptions.
Do not place personal data, credentials, or provider payloads into public catalog
assets or validation evidence. Compiler success is not production safety,
provider authenticity, or approval to perform an external action.
