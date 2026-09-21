# OBJ-05 adapter extension evidence

Requirement: allow later real adapters without redesigning core.

The [manifest](OBJ-05.json) requires committed-feature contract, integration,
architecture, type, lint, format and documentation gates before merge. Its scope
is an infrastructure extension boundary and offline qualification, not a shipped
live integration or whole-system acceptance.

Preflight passed 154 contract/architecture regression cases, five independent
adapter integration cases, 59 RUN-05 dispatch-recovery cases, all 277 application
source files under strict mypy, and the scoped lint/format/documentation checks.
The manifest gates must pass again against the committed feature, including the
restored-base witness; preflight alone is not a commit-bound attestation.

## Implementation

The existing application-owned LLM and connector protocols already define typed
operations. This change removes the remaining concrete-mock dependency from the
generic connector dispatch bridges. An immutable `ConnectorBindingRegistry`
selects explicitly registered async handlers by binding ID and capability. Multiple
implementations of one family remain distinct; there is no family fallback.
Registrations declare provider identity and durable receipt support. READ output
provenance uses that identity, while mock registrations preserve existing values.

Composition checks exact operation-registry identity and normalized positive
binding revisions. Binding registration rejects duplicate IDs, incompatible family
or disabled/unknown capabilities, and non-async handlers. Requested operations not
supported by a selected binding fail at the contract/pre-call boundary. WRITE
composition rejects write-bearing bindings without durable receipt support.

Catalog validation also compares each v1 operation's exact request/result classes
and method against the canonical application contract. Ten negative controls first
failed against the old validator because it accepted incompatible declarations;
they pass with the new check. Matching metadata/schema labels alone are not enough.

No application/domain implementation, state machine, approval service, worker,
default settings, dependency lockfile or CI workflow changes are part of OBJ-05.
The shipped process composition remains mock-only and rejects live/network modes.

## Independent behavior qualification

The new integration gate uses migrated SQLite and the existing API/application
services and worker. Its independent providers neither subclass nor delegate to
shipped mock providers. Model substitution uses `StructuredLLMReadAdapter` with
trusted bindings. Connector substitution supplies the neutral registry through
the existing infrastructure bundle factory in a test-only composition fixture.

The required checks are:

- Independent model output produces a validated artifact with the declared
  provider identity; malformed output produces no artifact.
- A newsletter command cannot call the independent handler before approval.
  After exact approval, the handler validates dispatcher authority and stores the
  exact `ConnectorActionReceipt` before success is accepted.
- Reconstructed runtime/adapter state retains durable receipt identity and does
  not repeat a completed effect on replay.
- Changing binding configuration after approval prevents a provider call.

Existing ARCH-06/ARCH-07, DEL-03, DEL-07 and SAFE-01 contracts remain regression
gates, including typed response handling, authorization refusal, receipt replay,
disabled operations and no silent real-to-mock fallback. Python qualification
tests disable network sockets while allowing the event loop's Unix socket pair.

## Architecture connection witness

The dependency-free `scripts/verify_obj_05_boundaries.py` parses the actual bridge
source. It rejects concrete implementation imports, concrete receipt-ledger access,
constant provider identity and constructors without the neutral source protocol.
Tooling tests include positive and deliberately coupled-source controls.

The restored-base witness replaces only `dispatch.py` with its pre-OBJ-05 version
in a committed export. That source must fail an ordinary architecture assertion,
not a missing dependency/import error. This is deliberately a **static boundary**
witness; separate contract and integration gates supply behavioral evidence.
The existing repository-wide ARCH-08 checker verifies layer boundaries too.

## Limits and subsequent integration work

No vendor SDK, credentials, external calls, live delivery or supported real-runtime
profile is added. The fixtures are local qualification implementations, not proof
of a vendor's availability, rate limits, cancellation, privacy, idempotency or
uncertain-outcome reconciliation. A production composition requires explicit
network/credential policy and provider-specific qualification. The neutral registry
does not grant that authority.

The durable-support flag is only a composition declaration. Runtime approval,
dispatch fencing and matching persisted receipt checks remain authoritative.
Remote effects and database commits are not one transaction, so live exactly-once
delivery is not claimed. Optional PostgreSQL and browser journeys are outside this
adapter-boundary requirement. CI remains disabled; no remote refs are published.
