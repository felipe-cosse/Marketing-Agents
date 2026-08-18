PYTHON ?= python3
BASE ?= main
HEAD ?= HEAD

.PHONY: format-check test test-network test-source test-tooling verify-source verify-history verify-requirement verify-governance

format-check:
	git diff --check

test-source:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest \
		tests.source.test_src_01_design_evidence \
		tests.source.test_src_02_source_authority \
		tests.source.test_src_03_assumption_register \
		tests.source.test_exec_01_source_inspection \
		tests.source.test_exec_02_architecture_decisions

test-tooling:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest tests.tooling.test_verify_requirement_evidence

test-network:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest tests.network.test_safe_11_network_isolation
	node --test tests/network/node_network_guard.test.mjs tests/network/browser_network_policy.test.mjs

test: test-source test-tooling test-network

verify-source:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_source_evidence.py --json

verify-history:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_requirement_evidence.py history --ref main --allow-incomplete --check-branches

verify-requirement:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/verify_requirement_evidence.py branch --id "$(REQUIREMENT)" --base "$(BASE)" --head "$(HEAD)" --run --witness

verify-governance: format-check verify-source test-source test-tooling verify-history
