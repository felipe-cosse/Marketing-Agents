# Marketing Agents

A local control surface and deterministic mock runtime for a source-backed
marketing organization: **5 departments, 12 functions, 36 role templates, and
43 instances**. The seven duplicated Community roles remain distinct instances;
the Marketing Orchestrator is a separate control plane, not a 44th instance.

## Start locally

Install Docker Engine 28+ with Compose and Make, then run:

```sh
make up
```

Open **http://127.0.0.1:8080** after readiness succeeds. No `.env`, provider key,
cloud credential, Redis, or separate database is required. The first build needs
public image/package registries. Subsequent application activity uses deterministic
mocks; it does not publish, email, query real accounts, or call a model provider.

`make down` stops this project and preserves its paired database/key volumes.
`make logs` shows bounded service diagnostics. Set `MARKETING_AGENTS_WEB_PORT` for
a conflicting port and `COMPOSE_PROJECT_NAME=marketing-agents-<name>` to create a
separate installation; use the same values when stopping it.

## Try the workflows

Open Demos in the application and run Social post draft, Blog/SEO draft, Email
signup onboarding, Community reminder draft, or Partnership application review.
The Email workflow stops for approval of its newsletter and CRM actions; **both
approvals** are required before either mock write. The other flows produce drafts
or recommendations, not external delivery or an authoritative business decision.

The chart supports search, zoom, instance details, and a keyboard-accessible
tree on narrow screens. Configuration, work, approvals, artifacts, and audit
events are persisted in SQLite. The executable worker currently advances the
five implemented demos; generic role workflows and real adapters are not implied
by the presence of a catalog card.

## Development and verification

The secondary native workflow uses Python 3.12, Node **24.20.0**, pnpm **11.24.0**,
and uv **0.10.7**. Activate those installed versions before running:

```sh
make bootstrap
make dev
```

`make dev` supervises API, workers, scheduler, and Vite at
http://127.0.0.1:5173, using the ignored `data/native` installation. Ctrl-C stops
only its child processes. See [local operations](docs/local-operations.md) for
explicit paths, paired backups, recovery, networking, and troubleshooting.

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
