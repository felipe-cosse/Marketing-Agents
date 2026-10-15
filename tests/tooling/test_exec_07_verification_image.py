"""EXEC-07 supplies real Node inventory checks only in the verification image."""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE_PIN = (
    "node:24.20.0-bookworm-slim@sha256:"
    "ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e"
)
NODE_STAGE = f"FROM {NODE_PIN} AS verification-node"
NODE_COPY = "COPY --from=verification-node /usr/local/bin/node /usr/local/bin/node"
NODE_CHECK = (
    "RUN node --input-type=module -e '"
    'import { readFileSync } from "node:fs"; '
    'import { readBrowserInventory } from "./apps/web/scripts/browser-evidence-inventory.mjs"; '
    'if (process.versions.node !== readFileSync(".nvmrc", "utf8").trim()) '
    'throw new Error("verification_node_version_mismatch"); '
    'readBrowserInventory("./apps/web");\''
)
INSTALL = (
    "RUN apt-get update && apt-get install --yes --no-install-recommends make git && apt-get clean"
)
RUNTIME_MARKER = "FROM base AS runtime\n"
# Independently frozen from the approved EXEC-06 runtime stage. This image
# prerequisite repair must not change unrelated runtime bytes. DEL-10 permits
# only the explicit database default replacement below in that historical stage.
RUNTIME_SHA256 = "e6d94d05b5e2e788270982b52af7184af558f236a2692018c3adff056793c199"
POSTGRES_STAGE = (
    "FROM postgres:17.11-bookworm@sha256:"
    "3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826 AS postgres-tools"
)
POSTGRES_DEFAULT = (
    'DATABASE_URL="postgresql+asyncpg://marketing_agents@localhost:5432/'
    'marketing_agents?host=/var/run/marketing-agents-postgres"'
)


def _assert_verification_contract(recipe: str, web_recipe: str, node_version: str) -> None:
    lines = recipe.splitlines()
    assert lines.count(NODE_STAGE) == 1, "EXEC-07 requires the reviewed pinned Node stage"
    assert f"FROM {NODE_PIN} AS dependencies" in web_recipe.splitlines(), (
        "EXEC-07 must reuse the exact web Node pin"
    )
    assert NODE_PIN.startswith(f"node:{node_version}-"), "EXEC-07 Node version drift"
    stages: dict[str, list[str]] = {}
    current = None
    for line in lines:
        if line.startswith("FROM "):
            match = re.fullmatch(r"FROM \S+ AS ([a-z-]+)", line)
            assert match is not None, "EXEC-07 requires explicit named stages"
            current = match[1]
            assert current not in stages, "EXEC-07 duplicate Docker stage"
            stages[current] = []
        elif current is not None and line.strip() and not line.lstrip().startswith("#"):
            stages[current].append(line)
    assert set(stages) == {
        "verification-node",
        "uv",
        "base",
        "dependencies",
        "verification",
        "runtime",
        "postgres-tools",
        "postgres",
    }, "EXEC-07 changed the bounded Docker stage inventory"
    assert lines.count(POSTGRES_STAGE) == 1, "EXEC-07/DEL-10 requires the exact PostgreSQL pin"
    assert stages["postgres-tools"] == [], "EXEC-07/DEL-10 PostgreSQL source must not mutate"
    assert stages["verification-node"] == [], "EXEC-07 Node source must be copied without mutation"
    verification = stages["verification"]
    assert lines.count(NODE_COPY) == verification.count(NODE_COPY) == 1, (
        "EXEC-07 requires one verification-only Node executable copy"
    )
    assert lines.count(NODE_CHECK) == verification.count(NODE_CHECK) == 1, (
        "EXEC-07 requires the real build-time version and inventory check"
    )
    source_copy = "COPY --chown=10001:10001 . /app"
    assert (
        verification.index(NODE_COPY)
        < verification.index(source_copy)
        < verification.index(NODE_CHECK)
    ), "EXEC-07 must check the copied Node against the copied source"
    assert verification.count(INSTALL) == 1, "EXEC-07 must not add unrelated system libraries"
    assert [line for line in lines if "--from=verification-node" in line] == [NODE_COPY], (
        "EXEC-07 must copy only the Node executable"
    )
    assert not re.search(r"\b(?:npm|pnpm|corepack|node_modules)\b", "\n".join(verification)), (
        "EXEC-07 must not install a frontend toolchain in the backend verification image"
    )
    for stage in ("base", "dependencies", "runtime", "postgres", "postgres-tools"):
        assert not re.search(
            r"\b(?:node|npm|pnpm|corepack|node_modules)\b", "\n".join(stages[stage])
        ), "EXEC-07 Node must not enter runtime or its ancestors"
    assert recipe.count(RUNTIME_MARKER) == 1, "EXEC-07 changed the runtime stage parent"
    runtime = RUNTIME_MARKER + recipe.split(RUNTIME_MARKER, 1)[1]
    assert runtime.count(POSTGRES_DEFAULT) == 1, (
        "EXEC-07/DEL-10 requires private PostgreSQL default"
    )
    runtime = runtime.replace(
        POSTGRES_DEFAULT,
        "DATABASE_URL=sqlite+aiosqlite:////var/lib/marketing-agents/data/marketing_agents.db",
        1,
    )
    assert hashlib.sha256(runtime.encode()).hexdigest() == RUNTIME_SHA256, (
        "EXEC-07 must preserve the approved runtime stage byte-for-byte"
    )


def test_exec_07_verification_image_reuses_node_without_changing_runtime():
    _assert_verification_contract(
        (ROOT / "docker/api.Dockerfile").read_text(),
        (ROOT / "docker/web.Dockerfile").read_text(),
        (ROOT / ".nvmrc").read_text().strip(),
    )


@pytest.mark.parametrize("mutation", (None, "version", "inventory"))
def test_exec_07_build_time_node_check_executes_real_version_and_inventory(tmp_path, mutation):
    web = tmp_path / "apps/web"
    web.mkdir(parents=True)
    # Only tracked source directories, never dependency trees or browser binaries.
    for directory in ("scripts", "e2e"):
        shutil.copytree(ROOT / "apps/web" / directory, web / directory)
    (tmp_path / ".nvmrc").write_text(
        "24.19.0\n" if mutation == "version" else (ROOT / ".nvmrc").read_text()
    )
    if mutation == "inventory":
        (web / "e2e/unowned-exec-07.spec.ts").write_text("// Unowned synthetic inventory member.\n")
    result = subprocess.run(
        shlex.split(NODE_CHECK.removeprefix("RUN ")),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if mutation is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        expected = (
            "verification_node_version_mismatch"
            if mutation == "version"
            else "Unowned browser spec"
        )
        assert expected in result.stderr


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_stage",
        "different_pin",
        "different_web_pin",
        "different_version",
        "missing_copy",
        "copy_in_dependencies",
        "runtime_copy",
        "node_modules_copy",
        "missing_version_check",
        "missing_inventory_check",
        "ignored_check",
        "extra_library",
        "runtime_environment_change",
        "postgres_pin_change",
        "postgres_default_change",
    ),
)
def test_exec_07_image_prerequisite_or_runtime_drift_fails(mutation):
    recipe = (ROOT / "docker/api.Dockerfile").read_text()
    web_recipe = (ROOT / "docker/web.Dockerfile").read_text()
    node_version = (ROOT / ".nvmrc").read_text().strip()
    _assert_verification_contract(recipe, web_recipe, node_version)
    if mutation == "missing_stage":
        recipe = recipe.replace(NODE_STAGE + "\n", "", 1)
    elif mutation == "different_pin":
        recipe = recipe.replace(NODE_PIN, NODE_PIN[:-1] + "0", 1)
    elif mutation == "different_web_pin":
        web_recipe = web_recipe.replace(NODE_PIN, NODE_PIN[:-1] + "0", 1)
    elif mutation == "different_version":
        node_version = "24.19.0"
    elif mutation == "missing_copy":
        recipe = recipe.replace(NODE_COPY + "\n", "", 1)
    elif mutation == "copy_in_dependencies":
        recipe = recipe.replace(NODE_COPY + "\n", "", 1).replace(
            "FROM base AS dependencies\n", "FROM base AS dependencies\n" + NODE_COPY + "\n", 1
        )
    elif mutation == "runtime_copy":
        recipe += NODE_COPY + "\n"
    elif mutation == "node_modules_copy":
        recipe = recipe.replace(
            NODE_COPY,
            NODE_COPY + "\nCOPY --from=verification-node /usr/local/lib/node_modules "
            "/usr/local/lib/node_modules",
            1,
        )
    elif mutation == "missing_version_check":
        recipe = recipe.replace(
            'if (process.versions.node !== readFileSync(".nvmrc", "utf8").trim()) '
            'throw new Error("verification_node_version_mismatch"); ',
            "",
            1,
        )
    elif mutation == "missing_inventory_check":
        recipe = recipe.replace('readBrowserInventory("./apps/web");', "", 1)
    elif mutation == "ignored_check":
        recipe = recipe.replace(NODE_CHECK, NODE_CHECK + " || true", 1)
    elif mutation == "extra_library":
        recipe = recipe.replace(
            "make git && apt-get clean", "make git libstdc++6 && apt-get clean", 1
        )
    elif mutation == "runtime_environment_change":
        recipe = recipe.replace("ALLOW_EXTERNAL_NETWORK=false", "ALLOW_EXTERNAL_NETWORK=true", 1)
    elif mutation == "postgres_pin_change":
        recipe = recipe.replace(POSTGRES_STAGE, POSTGRES_STAGE.replace("17.11", "17.10"), 1)
    elif mutation == "postgres_default_change":
        recipe = recipe.replace(POSTGRES_DEFAULT, "DATABASE_URL=postgresql+asyncpg://remote/db", 1)
    else:
        raise AssertionError("unknown fixture mutation")
    with pytest.raises(AssertionError, match="EXEC-07"):
        _assert_verification_contract(recipe, web_recipe, node_version)
