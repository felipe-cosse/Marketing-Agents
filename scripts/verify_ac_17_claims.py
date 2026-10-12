"""AC-17: offline, curated documentation claim contracts, not arbitrary prose proof.

This checks named current-scope sections against source inventories and preserves
explicit evidence limits. It never imports product code, executes examples, or
turns documented commands or historical results into current acceptance evidence.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

from scripts import verify_product_docs as product_docs

ROOT = Path(__file__).resolve().parents[1]
MAX_INPUT_BYTES = 2 * 1024**2
BROWSER_INVENTORY = "apps/web/scripts/browser-evidence-inventory.mjs"
MIGRATIONS = "apps/api/src/marketing_agents/infrastructure/db/migrations.py"
WORKFLOWS = "apps/api/src/marketing_agents/infrastructure/executable_workflows.py"
MATRIX = "docs/implementation-plan/16-requirements-traceability-matrix.md"
CURRENT_SECTIONS = (
    ("README.md", "Current workflow scope", "Deterministic mock behavior"),
    ("README.md", "Current verification commands", "Implemented but not live-tested"),
    ("docs/architecture.md", "Current workflow scope", "Deterministic mock behavior"),
    ("docs/architecture.md", "Current migration scope", "Implemented but not live-tested"),
    (
        "docs/catalog-authoring.md",
        "Current executable catalog boundary",
        "Deterministic mock behavior",
    ),
    ("docs/operations.md", "Current workflow scope", "Deterministic mock behavior"),
    ("docs/testing.md", "Current browser verification scope", "Implemented but not live-tested"),
    (
        "docs/verification.md",
        "AC-17 current documentation review",
        "Implemented but not live-tested",
    ),
)
# Only known stale present-tense claims are prohibited. Historical evidence in
# verification.md is deliberately not searched for old counts or old revisions.
STALE_CLAIMS = {
    "README.md": (
        "worker currently advances only the five implemented demo workflows",
        "current make test is only the source/tooling/network aggregate",
        "planned full make verify and acceptance aggregation are not implemented",
    ),
    "docs/architecture.md": (
        "deployed run worker executes the five registered demo workflows",
        "current local schema includes del-05 revision 0006",
        "run worker: implemented demo workflows",
    ),
    "docs/catalog-authoring.md": ("current runtime executes five registered demo",),
    "docs/operations.md": ("current worker executes only registered demo workflows",),
    "docs/testing.md": (
        "all 15 owned browser specs",
        "all browser specs now use the automatic",
    ),
}

DocumentationError = product_docs.DocumentationError
require = product_docs.require


def read(root: Path, relative: str) -> str:
    path = root / relative
    require(
        path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root),
        f"ac17_missing_input:{relative}",
    )
    with path.open("rb") as stream:
        value = stream.read(MAX_INPUT_BYTES + 1)
    require(len(value) <= MAX_INPUT_BYTES, f"ac17_input_bound:{relative}")
    return value.decode("utf-8")


def section(text: str, heading: str, relative: str) -> str:
    """One literal H2 section, excluding fenced examples and HTML comments."""
    clean = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    lines: list[str] = []
    fenced: str | None = None
    for line in clean.splitlines():
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)[0]
            if fenced is None:
                fenced = marker
            elif fenced == marker:
                fenced = None
            lines.append("")
        else:
            lines.append("" if fenced else line)
    clean = "\n".join(lines)
    matches = list(re.finditer(r"^## " + re.escape(heading) + r"\s*$", clean, re.MULTILINE))
    require(len(matches) == 1, f"ac17_section:{relative}:{heading}")
    start = matches[0].end()
    end = re.search(r"^#{1,2} ", clean[start:], re.MULTILINE)
    return clean[start : start + end.start()] if end else clean[start:]


def require_phrases(body: str, phrases: tuple[str, ...], label: str) -> None:
    normalized = product_docs.plain(body).lower()
    for phrase in phrases:
        require(phrase.lower() in normalized, f"ac17_claim:{label}:{phrase}")


def require_links(root: Path, relative: str, body: str, expected: tuple[str, ...]) -> None:
    linked = {
        path.relative_to(root).as_posix()
        for path, _ in product_docs.local_links(root, relative, body)
    }
    for path in expected:
        require(path in linked, f"ac17_evidence:{relative}:{path}")
        read(root, path)


def source_facts(root: Path) -> dict[str, int | str]:
    # Deliberately accept only the existing simple literal inventory contract.
    # Pin the executable prefix, not a regex match that could live in a comment
    # or template string. New imports/header syntax/bindings need manual review.
    source = read(root, BROWSER_INVENTORY)
    prefix = (
        'import { readFileSync, readdirSync } from "node:fs";\n'
        'import { join } from "node:path";\n\n'
    )
    match = re.match(
        re.escape(prefix) + r"export const BROWSER_RUNNERS = Object\.freeze\(\[([^]]*)\]\);",
        source,
    )
    require(match is not None, "ac17_browser_source")
    assert match is not None
    # The only other reviewed occurrence is the inventory reader's default.
    # Count raw occurrences conservatively: even a new mention in a comment or
    # string is rejected rather than requiring a general JavaScript evaluator.
    require(
        len(re.findall(r"\bBROWSER_RUNNERS\b", source)) == 2
        and source.count(
            "export function readBrowserInventory(webRoot, runners = BROWSER_RUNNERS) {"
        )
        == 1,
        "ac17_browser_source",
    )
    require(
        re.fullmatch(r'(?:\s*"run-[a-z0-9-]+-e2e\.mjs",)+\s*', match.group(1)) is not None,
        "ac17_browser_source",
    )
    runners = re.findall(r'"([^"]+)"', match.group(1))
    require(
        len(runners) == len(set(runners)) and "run-obj-06-e2e.mjs" in runners,
        "ac17_browser_source",
    )
    head = migration_head(read(root, MIGRATIONS))
    lock = json.loads(read(root, "catalog/v1/release.lock.json"))
    instances = lock["counts"]["instances"]
    require(type(instances) is int and instances > 0, "ac17_catalog_source")
    return {"browser_runners": len(runners), "migration_head": head, "instances": instances}


def migration_head(source: str) -> str:
    """Accept one direct literal binding; reject shadowing or later mutations.

    This is a conservative source-shape check, not Python execution or a proof
    against arbitrary reflective code. Unsupported binding forms fail closed.
    """
    tree = ast.parse(source)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "HEAD_REVISION"
    ]
    require(len(assignments) == 1, "ac17_migration_source")
    assignment = assignments[0]
    require(
        isinstance(assignment.value, ast.Constant)
        and type(assignment.value.value) is str
        and re.fullmatch(r"\d{4}", assignment.value.value) is not None,
        "ac17_migration_source",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "HEAD_REVISION":
            require(
                isinstance(node.ctx, ast.Load) or node is assignment.targets[0],
                "ac17_migration_source",
            )
        elif isinstance(node, ast.arg):
            require(node.arg != "HEAD_REVISION", "ac17_migration_source")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            require(node.name != "HEAD_REVISION", "ac17_migration_source")
        elif isinstance(node, ast.alias):
            require(
                (node.asname or node.name.split(".")[0]) != "HEAD_REVISION",
                "ac17_migration_source",
            )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            require("HEAD_REVISION" not in node.names, "ac17_migration_source")
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            require(node.name != "HEAD_REVISION", "ac17_migration_source")
        elif isinstance(node, ast.MatchMapping):
            require(node.rest != "HEAD_REVISION", "ac17_migration_source")
        elif isinstance(node, ast.Attribute):
            require(node.attr != "HEAD_REVISION", "ac17_migration_source")
        elif isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load):
            require(
                not any(
                    isinstance(item, ast.Name) and item.id == "HEAD_REVISION"
                    for item in ast.walk(node.value)
                ),
                "ac17_migration_source",
            )
        elif isinstance(node, ast.Constant) and node.value == "HEAD_REVISION":
            # Do not overlook a straightforward globals()["HEAD_REVISION"]
            # or setattr(module, "HEAD_REVISION", ...) mutation.
            raise DocumentationError("ac17_migration_source")
    return assignment.value.value


def verify(root: Path = ROOT) -> dict[str, int | str | bool]:
    root = root.resolve()
    # Preflight bounded guide inputs before reusing DEL-06's complete 13-guide,
    # seven-label, navigation, link, command, catalog and toolchain checks.
    documents = {relative: read(root, relative) for relative in product_docs.GUIDES}
    for relative in ("Makefile", ".python-version", ".nvmrc", "package.json"):
        read(root, relative)
    for relative, text in documents.items():
        for path, fragment in product_docs.local_links(root, relative, text):
            if fragment and path.is_file():
                read(root, path.relative_to(root).as_posix())
    facts = source_facts(root)
    bodies: dict[tuple[str, str], str] = {}
    for relative, heading, taxonomy in CURRENT_SECTIONS:
        body = section(documents[relative], heading, relative)
        require_phrases(body, ("Claim: " + taxonomy,), relative + ":" + heading)
        bodies[relative, heading] = body
    for relative, phrases in STALE_CLAIMS.items():
        normalized = product_docs.plain(documents[relative]).lower()
        for phrase in phrases:
            require(phrase not in normalized, f"ac17_stale_claim:{relative}")

    for relative, heading in (
        ("README.md", "Current workflow scope"),
        ("docs/architecture.md", "Current workflow scope"),
        ("docs/catalog-authoring.md", "Current executable catalog boundary"),
        ("docs/operations.md", "Current workflow scope"),
    ):
        body = bodies[relative, heading]
        require_phrases(
            body,
            (
                "five named demo workflows",
                "manual dry-run paths",
                f"{facts['instances']} catalog instances",
                "37 read-role",
                "six",
                "inert",
                "write-proposal previews",
                "exact-action approval",
                "not external delivery",
            ),
            relative + ":workflow",
        )
        require(
            re.search(
                r"no (?:action, approval or )?dispatch authority", product_docs.plain(body).lower()
            )
            is not None,
            f"ac17_preview_authority:{relative}",
        )
        require_links(root, relative, body, (WORKFLOWS, "docs/verification/requirements/OBJ-03.md"))

    commands = bodies["README.md", "Current verification commands"]
    require_phrases(
        commands,
        (
            "make test runs catalog, backend, network, frontend unit "
            "and full browser checks in order",
            "make verify adds static checks, safety coverage and source-drift checks",
            "make acceptance ref=head additionally requires source matching the selected commit",
            "committed clean-state verification and paired compose backup/restore",
            "command availability and historical results are not a fresh passing result",
            "does not establish ac-15, ac-16, or final release acceptance",
        ),
        "commands",
    )
    require_links(
        root, "README.md", commands, ("Makefile", "scripts/verify_local.py", "docs/testing.md")
    )
    migration = bodies["docs/architecture.md", "Current migration scope"]
    require_phrases(
        migration, (f"revision {facts['migration_head']}", "historical", "0006"), "migration"
    )
    require_links(root, "docs/architecture.md", migration, (MIGRATIONS,))
    browser = bodies["docs/testing.md", "Current browser verification scope"]
    require_phrases(
        browser,
        (
            f"{facts['browser_runners']} browser runners",
            "demo browser mutations in those runners use explicit route mocks",
            "Claim: Deterministic mock behavior",
            "OBJ-06",
            "real supervised",
            "native browser transport, not route-mocked application responses",
            "providers remain deterministic mocks",
            "do not prove external delivery",
            "historical del-07",
            "counts are not the current inventory or a new result",
            "no fresh full-suite pass",
            "ac-15 no-external-call acceptance",
            "ac-16 quality-gate pass",
            "not a universal operating-system egress boundary",
        ),
        "browser",
    )
    require_links(
        root,
        "docs/testing.md",
        browser,
        (BROWSER_INVENTORY, "docs/verification/requirements/OBJ-06.md"),
    )

    review = bodies["docs/verification.md", "AC-17 current documentation review"]
    require_phrases(
        review,
        (
            "2026-10-12",
            "09a732609f8d871fc5a6495e84f5347e3c939be5",
            "source review only",
            "not product reruns",
            "Claim: Acceptance target not yet verified",
            "AC-15",
            "AC-16",
            "final-source acceptance remain unverified",
        ),
        "review",
    )
    require_links(
        root,
        "docs/verification.md",
        review,
        (
            MATRIX,
            "scripts/verify_ac_17_claims.py",
            "tests/tooling/test_ac_17_claim_taxonomy.py",
            "docs/testing.md",
            "docs/adapter-contracts.md",
        ),
    )
    adapters = section(
        documents["docs/adapter-contracts.md"],
        "Adding a real integration",
        "docs/adapter-contracts.md",
    )
    require_phrases(
        adapters,
        (
            "Claim: Deferred real-adapter work",
            "provider credentials",
            "provider-specific verification",
            "do not establish those live-provider guarantees",
        ),
        "real-adapters",
    )
    identity = section(
        documents["docs/identity-and-authorization.md"],
        "Approval identity and self-approval",
        "docs/identity-and-authorization.md",
    )
    require_phrases(
        identity,
        (
            "Assumption",
            "allows the same local operator",
            "not describe local-v1 self-approval as dual control",
        ),
        "identity",
    )
    safety = section(
        documents["docs/security.md"], "Secrets, defaults and network isolation", "docs/security.md"
    )
    require_phrases(
        safety,
        ("Residual risk", "outbound route", "not every container is network-isolated"),
        "network-risk",
    )
    prerequisites = section(
        documents["docs/testing.md"], "Claim status and prerequisites", "docs/testing.md"
    )
    require_phrases(
        prerequisites,
        (
            "dependency acquisition requires registry access unless caches are prewarmed",
            "no required test needs a model/provider/cloud credential",
            "ci must not be treated as green",
        ),
        "prerequisites",
    )
    receipts = section(
        documents["docs/demos.md"], "Shared walkthrough and evidence boundary", "docs/demos.md"
    )
    require_phrases(
        receipts,
        (
            "proves intake, not execution, completion, delivery, or call counts",
            "contractual expectations, not live metrics",
        ),
        "receipt",
    )
    retention = section(
        documents["docs/data-handling.md"],
        "Configured retention is not automatic deletion",
        "docs/data-handling.md",
    )
    require_phrases(
        retention,
        (
            "implemented and verified — policy and projection only",
            "acceptance target not yet verified",
            "there is no physical retention sweeper",
            "hiding an api field is not disk erasure",
            "residual risk",
        ),
        "retention",
    )
    postgres = section(
        documents["docs/testing.md"], "Optional PostgreSQL compatibility", "docs/testing.md"
    )
    require_phrases(
        postgres,
        (
            "skipped compatibility cases are skipped, not passed",
            "a sqlite success is not postgresql qualification",
        ),
        "postgres",
    )
    delivery = section(
        documents["docs/architecture.md"],
        "Approval and external-action sequence",
        "docs/architecture.md",
    )
    require_phrases(
        delivery,
        (
            "claim: residual risk",
            "no universal distributed exactly-once guarantee",
            "post-call cancellation cannot recall a sent request",
        ),
        "delivery",
    )
    operation = section(
        documents["docs/operations.md"], "Verification and disclosure", "docs/operations.md"
    )
    require_phrases(
        operation,
        (
            "residual risk",
            "filesystem administrators",
            "are trusted",
            "do not expose the ui publicly",
        ),
        "host-trust",
    )
    historical = section(
        documents["docs/verification.md"], "DEL-07 local verification", "docs/verification.md"
    )
    require_phrases(
        historical, ("all 15 unfiltered runners", "32 cases with zero skips"), "historical-browser"
    )
    result = product_docs.verify(root)
    return {**result, **facts, "claim_sections": len(bodies), "acceptance_verified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        result = verify(args.root)
    except DocumentationError as error:
        # Only the fixed code is reported, never a document-controlled link,
        # path, parser value, source excerpt or operating-system exception.
        code = str(error).partition(":")[0]
        if re.fullmatch(r"[a-z0-9_]+", code) is None:
            code = "ac17_invalid_documentation"
        print(f"AC-17 documentation claim verification failed: {code}")
        return 1
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        print("AC-17 documentation claim verification failed: ac17_invalid_input")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
