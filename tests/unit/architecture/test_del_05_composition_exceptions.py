"""The executable composition root gets exact imports, not a layer-wide exemption."""

from pathlib import Path

from scripts.verify_architecture_boundaries import check_repository, load_policy

ROOT = Path(__file__).resolve().parents[3]


def test_del_05_external_composition_exceptions_are_source_and_prefix_scoped(tmp_path):
    policy = load_policy(ROOT / "architecture-boundaries.json")
    package = tmp_path / "apps/api/src/marketing_agents"
    files = {
        "workers/runtime/composition.py": "from fastapi import FastAPI\nimport sqlalchemy\n",
        "workers/serve_api.py": "import uvicorn\nimport uvicorn_extension\n",
        "workers/other.py": "import fastapi\nimport uvicorn\n",
        "domain/leak.py": "import fastapi\n",
        "api/leak.py": "import sqlalchemy\n",
    }
    for relative, source in files.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    violations = check_repository(tmp_path, policy)
    external = [item for item in violations if item.code == "python-unapproved-external-import"]
    assert len(external) == 6
    assert not any(
        item.path.endswith("composition.py") and "fastapi" in item.detail for item in external
    )
    assert any(
        item.path.endswith("composition.py") and "sqlalchemy" in item.detail for item in external
    )
    assert any(
        item.path.endswith("serve_api.py") and "uvicorn_extension" in item.detail
        for item in external
    )
    assert any(item.code == "api-orm-import" for item in violations)
