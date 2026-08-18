"""SAFE-03: supplied fixtures and reviewed official APIs replace scraping automation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from marketing_agents.infrastructure.catalog import CatalogCompilationError, compile_catalog
from marketing_agents.security.source_access import (
    DataSourceDeclaration,
    SourceAccessError,
    SourceAccessPolicy,
    SourceChannel,
)
from marketing_agents.security.url_policy import validate_reference_url
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]


def test_safe_03_supplied_inputs_and_committed_fixtures_are_offline_sources() -> None:
    policy = SourceAccessPolicy()
    for channel in (SourceChannel.SUPPLIED_INPUT, SourceChannel.COMMITTED_FIXTURE):
        declaration = DataSourceDeclaration(
            source_id=f"source:{channel.value}",
            channel=channel,
            connector_family="fixture",
        )
        assert policy.authorize(declaration, network_requested=False) is declaration
        with pytest.raises(SourceAccessError, match="cannot request network"):
            policy.authorize(declaration, network_requested=True)


def test_safe_03_reviewed_official_api_requires_explicit_resource_ids() -> None:
    policy = SourceAccessPolicy()
    declaration = DataSourceDeclaration(
        source_id="source:official-social",
        channel=SourceChannel.CONFIGURED_OFFICIAL_API,
        connector_family="social",
        explicit_resource_ids=("post:123",),
        terms_review_reference="review:official-social-v1",
    )
    assert policy.authorize(declaration, network_requested=True) is declaration

    for update, message in (
        ({"explicit_resource_ids": ()}, "resource IDs"),
        ({"terms_review_reference": None}, "terms review"),
    ):
        with pytest.raises(SourceAccessError, match=message):
            policy.authorize(declaration.model_copy(update=update), network_requested=True)


@pytest.mark.parametrize(
    "family", ["browser", "crawler", "generic-http", "scraper", "unofficial-api"]
)
def test_safe_03_unofficial_and_generic_source_families_fail(family: str) -> None:
    declaration = DataSourceDeclaration(
        source_id=f"source:{family}",
        channel=SourceChannel.CONFIGURED_OFFICIAL_API,
        connector_family=family,
        explicit_resource_ids=("resource:1",),
        terms_review_reference="review:1",
    )
    with pytest.raises(SourceAccessError, match="unofficial"):
        SourceAccessPolicy().authorize(declaration, network_requested=True)

    with pytest.raises(ValidationError):
        DataSourceDeclaration.model_validate(
            {
                **declaration.model_dump(),
                "arbitrary_url_fetch": True,
            }
        )


def test_safe_03_reference_urls_cannot_be_promoted_to_fetch_targets() -> None:
    declaration = DataSourceDeclaration(
        source_id="source:supplied",
        channel=SourceChannel.SUPPLIED_INPUT,
        connector_family="fixture",
    )
    reference = validate_reference_url("https://example.com/reference")
    with pytest.raises(SourceAccessError, match="never fetch targets"):
        SourceAccessPolicy().authorize(
            declaration,
            network_requested=False,
            reference_url=reference,
            use_reference_as_fetch_target=True,
        )


def test_safe_03_catalog_rejects_forbidden_families_even_when_unassigned(
    tmp_path: Path,
) -> None:
    source = ROOT / "catalog" / "v1"
    mutated = tmp_path / "catalog" / "v1"
    shutil.copytree(source, mutated)
    capabilities_path = mutated / "tool-capabilities.yaml"
    document = yaml.safe_load(capabilities_path.read_text(encoding="utf-8"))
    document["tool_capabilities"].append(
        {
            "id": "cap.generic-http.fetch",
            "description": "Forbidden arbitrary fetch",
            "effect": "read",
            "connector_family": "generic-http",
            "idempotency_support": "not_applicable",
            "default_timeout_seconds": 10,
            "data_classification": "internal",
        }
    )
    capabilities_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(CatalogCompilationError) as captured:
        compile_catalog(mutated)
    assert any(
        issue.code == "capability-forbidden-source-family" for issue in captured.value.issues
    )

    current = compile_catalog(source)
    assert all(
        capability.connector_family
        not in {"browser", "crawler", "generic-http", "scraper", "unofficial-api"}
        for capability in current.tool_capabilities
    )
