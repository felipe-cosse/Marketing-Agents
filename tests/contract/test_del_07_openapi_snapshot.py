"""DEL-07: actual API metadata, deterministic generation, and explicit safe settings."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.export_openapi import contract_document

ROOT = Path(__file__).resolve().parents[2]


def test_del_07_openapi_snapshot_matches_actual_factory():
    expected = json.loads((ROOT / "apps/api/openapi.json").read_text())
    assert contract_document() == expected
    assert expected["paths"]
    assert expected["components"]["schemas"]["SessionResponse"]


def test_del_07_contract_defaults_ignore_dotenv_and_poisoned_environment(monkeypatch, tmp_path):
    before = contract_document()
    for key in (
        "APP_ENV",
        "AUTH_MODE",
        "LOCAL_IDENTITY_ROLES",
        "DATABASE_URL",
        "CATALOG_ROOT",
        "LLM_PROVIDER",
        "CONNECTOR_MODE",
        "ALLOW_EXTERNAL_NETWORK",
        "REAL_LLM_API_KEY",
    ):
        monkeypatch.setenv(key, "not-a-valid-setting")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("APP_ENV=production\nAUTH_MODE=invalid\n")
    assert contract_document() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [".env"]


def test_del_07_schema_has_no_runtime_csrf_token():
    document = contract_document()
    session = document["components"]["schemas"]["SessionResponse"]
    assert "csrfToken" in session["required"]
    assert "default" not in session["properties"]["csrfToken"]
    assert document == contract_document()
