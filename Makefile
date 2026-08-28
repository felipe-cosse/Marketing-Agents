PYTHON ?= python3
UV ?= uv
UV_CACHE_DIR ?= .cache/uv
export UV_CACHE_DIR
BASE ?= main
HEAD ?= HEAD

.PHONY: bootstrap catalog-validate format format-check lint typecheck test test-backend test-catalog-compiler test-catalog-release test-network test-source test-tooling web-bootstrap web-build web-format web-format-check web-lint web-test web-test-coverage web-test-e2e web-test-web-01-unit web-test-web-01-witness web-test-web-02-unit web-test-web-02-witness web-typecheck verify-backend verify-catalog-release verify-ci-order verify-source verify-history verify-requirement verify-governance verify-web

bootstrap:
	$(UV) sync --frozen --python 3.12

format:
	$(UV) run ruff format apps/api/src tests/unit tests/integration tests/catalog
	$(UV) run ruff check --fix apps/api/src tests/unit tests/integration tests/catalog

format-check:
	git diff --check
	$(UV) run ruff format --check apps/api/src tests/unit tests/integration tests/catalog

lint:
	$(UV) run ruff check apps/api/src tests/unit tests/integration tests/catalog

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

catalog-validate:
	$(UV) run marketing-agents-catalog validate --root catalog/v1

verify-catalog-release:
	$(UV) run python scripts/verify_catalog_release.py

verify-ci-order:
	$(UV) run python scripts/verify_ci_order.py

test-network:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run python -m unittest tests.network.test_safe_11_network_isolation
	node --test tests/network/node_network_guard.test.mjs tests/network/browser_network_policy.test.mjs

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

web-test-web-01-unit:
	node apps/web/scripts/run-web-01-unit.mjs

web-test-web-01-witness:
	node apps/web/scripts/run-web-01-witness.mjs

web-test-web-02-unit:
	node apps/web/scripts/run-web-02-unit.mjs

web-test-web-02-witness:
	node apps/web/scripts/run-web-02-witness.mjs

test: test-source test-tooling test-network

verify-source:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_source_evidence.py --json

verify-history:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_requirement_evidence.py history --ref main --allow-incomplete --check-branches

verify-requirement:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_requirement_evidence.py branch --id "$(REQUIREMENT)" --base "$(BASE)" --head "$(HEAD)" --run --witness

verify-governance: format-check verify-source test-source test-tooling verify-history

verify-backend: format-check lint typecheck test-backend

verify-web: web-format-check web-lint web-typecheck web-test web-test-web-01-unit web-test-web-01-witness web-test-web-02-unit web-test-web-02-witness web-build
