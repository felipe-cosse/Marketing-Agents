# Marketing Agents

A local control surface and deterministic mock runtime for a source-backed
marketing organization: **5 departments, 12 functions, 36 role templates, and
43 instances**. Department instance counts are Social media 12, Blog & SEO 6,
Email 5, Community 14, and Partnerships 6. Community's seven templates each back
two distinct instances; their duplication has no invented business meaning.
The Marketing Orchestrator is a separate control plane, not a 44th instance.

This is a runnable single-operator local foundation, not an autonomous marketing
service or a production deployment. Catalog cards describe 43 deployed roles;
the executable worker currently advances only the five implemented demo
workflows. A card's presence does not imply a general-purpose workflow or a real
integration.

Claims in these guides distinguish **deterministic mock behavior**, implemented
controls with linked verification evidence, acceptance targets, assumptions,
deferred integrations, and residual risks. Historical test results do not prove
that your installation or the current GitHub run is healthy. Operational status
recorded on **2026-09-10**: GitHub CI was paused at the operator's request and live
execution remains disabled. See the [verification record](docs/verification.md)
for dated results and unresolved failures; this README does not claim green CI
or completion of every requirement.

## Start locally

Install Docker Engine 28+ with Compose and Make, then run:

```sh
make up
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) after readiness succeeds. No `.env`, provider key,
cloud credential, Redis, or separate database is required. The first build needs
public image/package registries. Subsequent application activity uses deterministic
mocks; it does not publish, email, query real accounts, or call a model provider.

`make down` stops this project and preserves its paired database/key volumes.
`make logs` shows bounded service diagnostics. Set `MARKETING_AGENTS_WEB_PORT` for
a conflicting port and `COMPOSE_PROJECT_NAME=marketing-agents-<name>` to create a
separate installation; use the same values when stopping it.

Implemented startup and recovery behavior has historical local evidence in
[DEL-05](docs/verification/requirements/DEL-05.md). Builds may acquire dependencies;
that is distinct from application/provider execution. Do not enable live adapters
or resume CI as a prerequisite for trying the local mocks.

## Architecture in brief

The React/TypeScript/Vite UI is the configuration and control surface. FastAPI
validates intake and exposes catalog, configuration, run, artifact, and approval
resources. Separate run and scheduler workers share SQLite with the API through
application services; the scheduler admits work rather than executing it itself.

Version-controlled `catalog/v1` is authoritative for templates, schemas, prompts,
and the source hierarchy. Migration and repeatable seed create its database
projection while preserving instance-level operator configuration. Domain and
application layers depend on typed ports, not a model SDK. Deterministic routing
constructs explicit dependency graphs and passes typed artifacts. Each external
action has its own immutable approval and persisted idempotency identity.

Compose initializes a private digest key, migrates/seeds once, then starts the
API, workers, and web. The web proxy reaches the API through a Unix socket;
backend containers have no IP network. SQLite is the required local database;
optional PostgreSQL compatibility is not a production qualification. See
[architecture](docs/architecture.md) for boundaries and flow, and
[adapter contracts](docs/adapter-contracts.md) for deferred real integrations.

## Try the workflows

Open **Demos** in the navigation. Each scenario supplies a schema-driven safe
preset; use synthetic data and **Reset safe preset** to restore it. These are
deterministic mock walkthroughs, not live delivery tests:

1. **Social content draft:** keep the supplied idea, audience, tone, and key
   points; select **Create draft**. Follow **Open accepted run**, then **Open
   artifacts**, to inspect the `social_post_draft` and provenance. Nothing is
   published to LinkedIn.
2. **Blog & SEO content review:** keep the supplied article excerpt, timestamps,
   keywords, and product metadata; select **Create review**. Inspect the
   `content_review` for gaps and recommendations. Its canonical URL is a reference,
   not a page that was fetched; no CMS is updated.
3. **Email signup onboarding:** keep the synthetic signup and consent; select
   **Propose onboarding actions**. Follow **Open approval queue**, review each
   immutable newsletter/CRM action, and confirm **Approve exact action** for
   each. Zero or one valid approval permits no connector calls. After both valid
   approvals, the worker records one mock call per action and creates an unsent
   welcome draft. A recorded approval alone is not a delivery receipt.
4. **Community reminder draft:** keep the event signup, IANA timezone, local
   session time, and offset; select **Create reminder draft**. Inspect the
   `scheduled_reminder_draft` and recommended UTC time. Despite that artifact
   name, no reminder is sent and no external schedule or enrollment is created.
5. **Partnership application review:** keep the supplied synthetic application,
   criteria, and evidence; select **Create advisory review**. Inspect the
   `partner_review_recommendation`, missing information, and rationale. The
   preset recommends `needs_information`; no applicant is accepted, rejected,
   researched online, or notified automatically.

An accepted receipt proves durable intake, not completion or measured call
counts. Inspect the authoritative run, timeline, and artifacts; scenario cards
show the expected contract. [Demo walkthroughs](docs/demos.md) give exact inputs,
approval steps, expected results, limitations, and executable evidence.

The **Org chart** supports search, zoom, instance details, and a keyboard-accessible
tree on narrow screens. **Approvals** and **Runs & audit** expose recorded work.
Configuration, work, approvals, artifacts, and audit events persist in SQLite.

## Development and verification

The secondary native workflow uses Python 3.12, Node **24.20.0**, pnpm **11.24.0**,
and uv **0.10.7**. Activate those installed versions before running:

```sh
make bootstrap
make dev
```

`make dev` supervises API, workers, scheduler, and Vite at
[http://127.0.0.1:5173](http://127.0.0.1:5173), using the ignored `data/native`
installation. Ctrl-C stops only its child processes. See
[operations](docs/operations.md) for explicit paths, paired backups, recovery,
networking, and troubleshooting.

After bootstrap, these existing commands cover different checks:

```sh
make verify-catalog-release
make verify-governance
make verify-backend
make verify-web
make web-bootstrap
make web-test-e2e
```

`web-bootstrap` separately acquires the pinned browser. `verify-web` is not the
browser suite, and the current `make test` is only the source/tooling/network
aggregate. The planned full `make verify` and acceptance aggregation are not
implemented at this revision. Consult [testing](docs/testing.md) before treating
any individual command as release acceptance.

```sh
make verify-clean REF=HEAD
```

This also requires host Git and Python 3. It verifies the selected **commit**, not
uncommitted files: a fresh tracked
export, frozen image/dependency acquisition, isolated storage, startup, five
demos, approvals, restart/replay, offline backend/frontend suites, and a browser
smoke. It removes only its own test resources. Required tests use no provider
credentials. Broader delivery/acceptance status remains in the
[requirements matrix](docs/implementation-plan/16-requirements-traceability-matrix.md).

## Local-only safety boundary

This is not a multi-user production deployment. Local identity grants the local
operator administrative and approval privileges, including self-approval. Anyone
who can reach the loopback UI or access Docker is inside that trust boundary.

API and worker containers have **no IP network**; the web proxy reaches the API
through a private Unix socket. Docker Desktop does not publish ports on the
tested internal-network configurations, so only the credential-free static web
proxy uses a normal ingress bridge with a loopback-published port. That container
retains outbound network capability at the OS level; its committed nginx config
uses only static files and the fixed socket, with no external upstream or DNS
resolver. Do not interpret mock success as verified real-provider behavior,
production security, delivery guarantees, or a production backup strategy.

## Documentation map

- [Architecture](docs/architecture.md): components, ownership, workflows, storage.
- [Assumptions](docs/assumptions.md): source precedence and explicit decisions.
- [Security](docs/security.md): trust boundaries, controls, and residual risks.
- [Identity and authorization](docs/identity-and-authorization.md): local actor,
  self-approval, and HTTP authorization boundaries.
- [Data handling](docs/data-handling.md): redaction, retention, keys, and backups.
- [Adapter contracts](docs/adapter-contracts.md): mocks and real-adapter boundaries.
- [Catalog authoring](docs/catalog-authoring.md): source-backed template/instance changes.
- [Testing](docs/testing.md): exact commands, coverage, and known gate gaps.
- [Operations](docs/operations.md): setup, stopping, recovery, and troubleshooting.
- [Local operations runbook](docs/local-operations.md): paired storage and exact backup commands.
- [Demos](docs/demos.md): all five local walkthroughs and expected mock outcomes.
- [Verification record](docs/verification.md): dated evidence and unverified work.
