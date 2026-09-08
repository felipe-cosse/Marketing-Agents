PYTHON ?= python3
UV ?= uv
UV_CACHE_DIR ?= .cache/uv
export UV_CACHE_DIR
BASE ?= main
HEAD ?= HEAD
.DEFAULT_GOAL := bootstrap

.PHONY: bootstrap catalog-validate format format-check lint typecheck test test-backend test-catalog-compiler test-catalog-release test-demo-01-backend test-demo-02-backend test-demo-03-backend test-network test-source test-tooling web-bootstrap web-build web-format web-format-check web-lint web-test web-test-coverage web-test-demo-01-e2e web-test-demo-01-unit web-test-demo-02-e2e web-test-demo-02-unit web-test-demo-03-e2e web-test-demo-03-unit web-test-e2e web-test-web-01-unit web-test-web-01-witness web-test-web-02-unit web-test-web-02-witness web-test-web-03-unit web-test-web-03-witness web-test-web-04-unit web-test-web-04-witness web-test-web-05-unit web-test-web-05-witness web-test-web-06-unit web-test-web-06-witness web-test-web-07-unit web-test-web-07-witness web-test-web-08-unit web-test-web-08-witness web-test-web-09-unit web-test-web-09-witness web-typecheck verify-backend verify-catalog-release verify-ci-order verify-source verify-history verify-requirement verify-governance verify-web
.PHONY: test-demo-04-backend web-test-demo-04-e2e web-test-demo-04-unit
.PHONY: test-demo-05-backend web-test-demo-05-e2e web-test-demo-05-unit
.PHONY: test-demo-06-backend
.PHONY: web-test-orch-01-e2e web-test-orch-01-unit web-test-orch-01-witness
.PHONY: web-test-arch-02-build web-test-arch-02-e2e web-test-arch-02-unit web-test-arch-02-witness
.PHONY: test-arch-08-backend verify-architecture web-test-arch-08-unit
.PHONY: test-del-03-contracts test-del-03-demos
.PHONY: init-local-secret migrate seed seed-check test-del-04-persistence test-del-04-postgresql test-del-04-regression

DATABASE_URL ?= sqlite+aiosqlite:///./data/marketing_agents.db
MARKETING_AGENTS_DIGEST_KEY_PATH ?= data/digest.key
CATALOG_ROOT ?= catalog/v1

init-local-secret:
	$(UV) run marketing-agents-local-secret --database-url "$(DATABASE_URL)" --key-path "$(MARKETING_AGENTS_DIGEST_KEY_PATH)"

migrate: init-local-secret
	$(UV) run marketing-agents-db migrate --database-url "$(DATABASE_URL)" --key-path "$(MARKETING_AGENTS_DIGEST_KEY_PATH)"

seed:
	$(UV) run marketing-agents-db seed --database-url "$(DATABASE_URL)" --key-path "$(MARKETING_AGENTS_DIGEST_KEY_PATH)" --root "$(CATALOG_ROOT)"

seed-check:
	$(UV) run marketing-agents-db seed --check --database-url "$(DATABASE_URL)" --key-path "$(MARKETING_AGENTS_DIGEST_KEY_PATH)" --root "$(CATALOG_ROOT)"

test-del-04-persistence:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q --disable-socket --allow-unix-socket \
		tests/integration/db/test_del_04_migrations.py \
		tests/integration/db/test_del_04_catalog_seed.py \
		tests/integration/db/test_del_04_readiness.py \
		tests/integration/db/test_del_04_trigger_projection.py \
		tests/integration/db/test_del_04_postgresql_fixture.py \
		tests/integration/db/test_del_04_database_cli.py

test-del-04-postgresql:
	MARKETING_AGENTS_TEST_POSTGRES=1 PYTHONDONTWRITEBYTECODE=1 $(UV) run --offline --frozen --extra postgresql pytest -q --disable-socket --allow-unix-socket \
		tests/integration/db/test_del_04_postgresql.py \
		tests/integration/db/test_del_04_postgresql_installation.py \
		tests/integration/db/test_del_04_postgresql_cli.py

test-del-04-regression:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q --disable-socket --allow-unix-socket \
		tests/integration/api/test_api_01_health_readiness.py \
		tests/integration/db/test_api_03_instance_configuration_persistence.py \
		tests/integration/db/test_api_04_manual_work_persistence.py \
		tests/integration/db/test_api_05_webhook_intake.py \
		tests/integration/db/test_api_05_webhook_receipts.py \
		tests/acceptance/test_social_demo.py \
		tests/acceptance/test_blog_seo_demo.py \
		tests/acceptance/test_email_signup_demo.py

bootstrap:
	$(UV) sync --frozen --python 3.12
	node apps/web/scripts/require-pinned-node.mjs
	corepack pnpm install --frozen-lockfile

format:
	$(UV) run ruff format apps/api/src tests/unit tests/integration tests/acceptance tests/catalog
	$(UV) run ruff check --fix apps/api/src tests/unit tests/integration tests/acceptance tests/catalog

format-check:
	git diff --check
	$(UV) run ruff format --check apps/api/src tests/unit tests/integration tests/acceptance tests/catalog

lint:
	$(UV) run ruff check apps/api/src tests/unit tests/integration tests/acceptance tests/catalog

typecheck:
	$(UV) run mypy apps/api/src/marketing_agents

test-source:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest \
		tests.source.test_src_01_design_evidence \
		tests.source.test_src_02_source_authority \
		tests.source.test_src_03_assumption_register \
		tests.source.test_exec_01_source_inspection \
		tests.source.test_exec_02_architecture_decisions

test-tooling:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest tests.tooling.test_verify_requirement_evidence

test-backend:
	$(UV) run pytest -q

test-catalog-compiler:
	$(UV) run pytest -q tests/catalog/test_arch_04_catalog_compiler.py

test-catalog-release:
	$(UV) run pytest -q tests/catalog/test_cat_01_authoritative_catalog.py

test-demo-01-backend:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q --disable-socket --allow-unix-socket \
		tests/acceptance/test_social_demo.py \
		tests/integration/api/test_demo_01_scenarios.py

test-demo-02-backend:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q --disable-socket --allow-unix-socket \
		tests/acceptance/test_blog_seo_demo.py \
		tests/integration/api/test_demo_02_scenarios.py

test-demo-03-backend:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q --disable-socket --allow-unix-socket \
		tests/acceptance/test_email_signup_demo.py \
		tests/acceptance/test_email_signup_demo_contract.py \
		tests/integration/api/test_demo_03_scenarios.py

test-demo-04-backend:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q --disable-socket --allow-unix-socket \
		tests/acceptance/test_community_reminder_demo.py \
		tests/integration/api/test_demo_04_scenarios.py

test-demo-05-backend:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q --disable-socket --allow-unix-socket \
		tests/acceptance/test_partnerships_demo.py \
		tests/integration/api/test_demo_05_scenarios.py

test-demo-06-backend:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q --disable-socket --allow-unix-socket \
		tests/acceptance/test_email_signup_demo.py

test-del-03-contracts:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q --disable-socket --allow-unix-socket \
		tests/contract/test_del_03_durable_mock_composition.py \
		tests/contract/test_arch_06_llm_provider.py \
		tests/contract/test_arch_07_connector_contract_matrix.py \
		tests/integration/db/test_run_05_external_action_idempotency.py \
		tests/integration/db/test_run_03_write_completion.py

test-del-03-demos:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q --disable-socket --allow-unix-socket \
		tests/acceptance/test_social_demo.py \
		tests/acceptance/test_blog_seo_demo.py \
		tests/acceptance/test_email_signup_demo.py \
		tests/acceptance/test_email_signup_demo_contract.py \
		tests/acceptance/test_community_reminder_demo.py \
		tests/acceptance/test_partnerships_demo.py

catalog-validate:
	$(UV) run marketing-agents-catalog validate --root catalog/v1

verify-catalog-release:
	$(UV) run python scripts/verify_catalog_release.py

verify-ci-order:
	$(UV) run python scripts/verify_ci_order.py

verify-architecture:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_architecture_boundaries.py

test-network:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run python -m unittest tests.network.test_safe_11_network_isolation
	node --test tests/network/node_network_guard.test.mjs tests/network/browser_network_policy.test.mjs

test-arch-08-backend:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run pytest -q \
		tests/unit/architecture/test_arch_08_repository_boundaries.py \
		tests/security/test_safe_01_default_mock_mode.py \
		tests/integration/api/test_api_01_health_readiness.py

web-bootstrap:
	corepack pnpm install --frozen-lockfile
	corepack pnpm --dir apps/web browser:install

web-format:
	corepack pnpm --dir apps/web format

web-format-check:
	corepack pnpm --dir apps/web format:check

web-lint:
	corepack pnpm --dir apps/web lint

web-typecheck:
	corepack pnpm --dir apps/web typecheck

web-test:
	corepack pnpm --dir apps/web test

web-test-coverage:
	corepack pnpm --dir apps/web test:coverage

web-build:
	corepack pnpm --dir apps/web build

web-test-e2e:
	node apps/web/scripts/run-web-e2e.mjs

web-test-demo-01-unit:
	node apps/web/scripts/run-demo-01-unit.mjs

web-test-demo-01-e2e:
	node apps/web/scripts/run-demo-01-e2e.mjs

web-test-demo-02-unit:
	node apps/web/scripts/run-demo-02-unit.mjs

web-test-demo-02-e2e:
	node apps/web/scripts/run-demo-02-e2e.mjs

web-test-demo-03-unit:
	node apps/web/scripts/run-demo-03-unit.mjs

web-test-demo-03-e2e:
	node apps/web/scripts/run-demo-03-e2e.mjs

web-test-demo-04-unit:
	node apps/web/scripts/run-demo-04-unit.mjs

web-test-demo-04-e2e:
	node apps/web/scripts/run-demo-04-e2e.mjs

web-test-demo-05-unit:
	node apps/web/scripts/run-demo-05-unit.mjs

web-test-demo-05-e2e:
	node apps/web/scripts/run-demo-05-e2e.mjs

web-test-web-01-unit:
	node apps/web/scripts/run-web-01-unit.mjs

web-test-web-01-witness:
	node apps/web/scripts/run-web-01-witness.mjs

web-test-orch-01-unit:
	node apps/web/scripts/run-orch-01-unit.mjs

web-test-orch-01-witness:
	node apps/web/scripts/run-orch-01-witness.mjs

web-test-orch-01-e2e:
	node apps/web/scripts/run-orch-01-e2e.mjs

web-test-arch-02-build:
	node apps/web/scripts/run-arch-02-build.mjs

web-test-arch-02-unit:
	node apps/web/scripts/run-arch-02-unit.mjs

web-test-arch-02-witness:
	node apps/web/scripts/run-arch-02-witness.mjs

web-test-arch-02-e2e:
	node apps/web/scripts/run-arch-02-e2e.mjs

web-test-arch-08-unit:
	node apps/web/scripts/run-arch-08-unit.mjs

web-test-web-02-unit:
	node apps/web/scripts/run-web-02-unit.mjs

web-test-web-02-witness:
	node apps/web/scripts/run-web-02-witness.mjs

web-test-web-03-unit:
	node apps/web/scripts/run-web-03-unit.mjs

web-test-web-03-witness:
	node apps/web/scripts/run-web-03-witness.mjs

web-test-web-04-unit:
	node apps/web/scripts/run-web-04-unit.mjs

web-test-web-04-witness:
	node apps/web/scripts/run-web-04-witness.mjs

web-test-web-05-unit:
	node apps/web/scripts/run-web-05-unit.mjs

web-test-web-05-witness:
	node apps/web/scripts/run-web-05-witness.mjs

web-test-web-06-unit:
	node apps/web/scripts/run-web-06-unit.mjs

web-test-web-06-witness:
	node apps/web/scripts/run-web-06-witness.mjs

web-test-web-07-unit:
	node apps/web/scripts/run-web-07-unit.mjs

web-test-web-07-witness:
	node apps/web/scripts/run-web-07-witness.mjs

web-test-web-08-unit:
	node apps/web/scripts/run-web-08-unit.mjs

web-test-web-08-witness:
	node apps/web/scripts/run-web-08-witness.mjs

web-test-web-09-unit:
	node apps/web/scripts/run-web-09-unit.mjs

web-test-web-09-witness:
	node apps/web/scripts/run-web-09-witness.mjs

test: test-source test-tooling test-network

verify-source:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_source_evidence.py --json

verify-history:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_requirement_evidence.py history --ref main --allow-incomplete --check-branches

verify-requirement:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_requirement_evidence.py branch --id "$(REQUIREMENT)" --base "$(BASE)" --head "$(HEAD)" --run --witness

verify-governance: format-check verify-source test-source test-tooling verify-architecture verify-history

verify-backend: format-check lint typecheck test-backend

verify-web: web-format-check web-lint web-typecheck web-test web-test-demo-01-unit web-test-demo-02-unit web-test-demo-03-unit web-test-demo-04-unit web-test-demo-05-unit web-test-web-01-unit web-test-web-01-witness web-test-orch-01-unit web-test-orch-01-witness web-test-arch-02-unit web-test-arch-02-witness web-test-arch-02-build web-test-arch-08-unit web-test-web-02-unit web-test-web-02-witness web-test-web-03-unit web-test-web-03-witness web-test-web-04-unit web-test-web-04-witness web-test-web-05-unit web-test-web-05-witness web-test-web-06-unit web-test-web-06-witness web-test-web-07-unit web-test-web-07-witness web-test-web-08-unit web-test-web-08-witness web-test-web-09-unit web-test-web-09-witness web-build

# DEL-05 canonical operations. Docker requires only Docker/Compose and Make;
# Python/Node acquisition is needed only for the secondary native workflow.
.PHONY: help up down logs dev backup-local restore-local verify-clean test-del-05-runtime test-del-05-backup test-del-05-tooling verify-del-05-offline-backend verify-del-05-offline-web
LOCAL_MODE ?= compose
LOCAL_PROJECT ?= marketing-agents-local
LOCAL_IMAGE ?= $(LOCAL_PROJECT)-backend:local
LOCAL_STATE ?= $(CURDIR)/data/native
REF ?= HEAD

help:
	@echo 'make up                  Build and start safe local Compose stack (127.0.0.1:8080)'
	@echo 'make down / logs         Stop (preserve paired volumes) / read scoped service logs'
	@echo 'make bootstrap / dev     Frozen native dependencies / supervised local development'
	@echo 'make backup-local DESTINATION=<new-path>  Protected paired backup (secret-bearing)'
	@echo 'make restore-local BACKUP=<path> LOCAL_PROJECT=<new-project> LOCAL_IMAGE=<existing-image>'
	@echo 'make verify-clean REF=HEAD  Verify committed tracked source in isolated Docker storage'
	@echo 'Use LOCAL_MODE=native and DESTINATION=<new-path> for native backup/restore.'

up:
	sh scripts/compose.sh up

down:
	sh scripts/compose.sh down

logs:
	sh scripts/compose.sh logs

dev:
	.venv/bin/python scripts/dev.py --state-dir "$(LOCAL_STATE)"

backup-local:
	.venv/bin/python scripts/local_backup.py backup --mode "$(LOCAL_MODE)" --project "$(LOCAL_PROJECT)" --destination "$(DESTINATION)" $(if $(filter native,$(LOCAL_MODE)),--database-url "$(DATABASE_URL)" --key-path "$(MARKETING_AGENTS_DIGEST_KEY_PATH)",)

restore-local:
	.venv/bin/python scripts/local_backup.py restore --mode "$(LOCAL_MODE)" --project "$(LOCAL_PROJECT)" --image "$(LOCAL_IMAGE)" --backup "$(BACKUP)" $(if $(DESTINATION),--destination "$(DESTINATION)",)

verify-clean:
	sh scripts/verify_clean_state.sh --ref "$(REF)"

test-del-05-runtime:
	PYTHONPATH="$(CURDIR)/apps/api/src:$(CURDIR)" PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m pytest -q tests/integration/runtime

test-del-05-backup:
	PYTHONPATH="$(CURDIR)/apps/api/src:$(CURDIR)" PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m pytest -q tests/integration/db/test_del_05_backup.py

.PHONY: test-del-05-compose-backup
test-del-05-compose-backup:
	PYTHONPATH="$(CURDIR)/apps/api/src:$(CURDIR)" PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_del_05_backup.py --build

test-del-05-tooling:
	PYTHONPATH="$(CURDIR)/apps/api/src:$(CURDIR)" PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m pytest -q tests/tooling/test_del_05_*.py

# These targets operate in a git archive export, not a checkout. Git provenance
# and no-generated-drift checks belong to the outer clean-state verifier.
verify-del-05-offline-backend:
	python -m ruff format --check apps/api/src tests/unit tests/integration tests/acceptance tests/catalog
	python -m ruff check apps/api/src tests/unit tests/integration tests/acceptance tests/catalog
	python -m ruff format --check scripts/del_05_*.py scripts/verify_del_05_*.py scripts/dev.py scripts/health_http.py scripts/local_backup.py tests/tooling/test_del_05_*.py
	python -m ruff check scripts/del_05_*.py scripts/verify_del_05_*.py scripts/dev.py scripts/health_http.py scripts/local_backup.py tests/tooling/test_del_05_*.py
	python -m mypy apps/api/src/marketing_agents
	python scripts/verify_architecture_boundaries.py
	python -m marketing_agents.workers.catalog_cli validate --root catalog/v1
	python -m pytest -q

verify-del-05-offline-web:
	cd apps/web && node_modules/.bin/prettier --check .
	cd apps/web && node_modules/.bin/eslint . --max-warnings=0
	cd apps/web && node_modules/.bin/tsc -b --pretty false
	cd apps/web && node_modules/.bin/vitest run
	cd apps/web && node_modules/.bin/vite build
