"""Semantic source authorization; no generic fetch, browser, crawler, or scraper path."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from marketing_agents.security.url_policy import ValidatedReferenceUrl

FORBIDDEN_SOURCE_FAMILIES = frozenset(
    {
        "browser",
        "crawler",
        "generic-http",
        "scraper",
        "unofficial-api",
    }
)


class SourceAccessError(ValueError):
    """Raised when a declaration attempts undeclared or unofficial data access."""


class SourceChannel(StrEnum):
    SUPPLIED_INPUT = "supplied_input"
    COMMITTED_FIXTURE = "committed_fixture"
    CONFIGURED_OFFICIAL_API = "configured_official_api"


class DataSourceDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_id: str = Field(min_length=1, max_length=200)
    channel: SourceChannel
    connector_family: str = Field(min_length=1, max_length=100)
    explicit_resource_ids: tuple[str, ...] = Field(default=(), max_length=100)
    terms_review_reference: str | None = Field(default=None, max_length=300)
    arbitrary_url_fetch: Literal[False] = False


class SourceAccessPolicy:
    def authorize(
        self,
        declaration: DataSourceDeclaration,
        *,
        network_requested: bool,
        reference_url: ValidatedReferenceUrl | None = None,
        use_reference_as_fetch_target: bool = False,
    ) -> DataSourceDeclaration:
        if declaration.connector_family in FORBIDDEN_SOURCE_FAMILIES:
            raise SourceAccessError(
                "generic, scraping, crawler, and unofficial source families fail"
            )
        if use_reference_as_fetch_target or (
            reference_url is not None and not reference_url.provenance_only
        ):
            raise SourceAccessError("reference URLs are provenance only, never fetch targets")
        if declaration.channel in {
            SourceChannel.SUPPLIED_INPUT,
            SourceChannel.COMMITTED_FIXTURE,
        }:
            if network_requested:
                raise SourceAccessError(
                    "supplied inputs and fixtures cannot request network access"
                )
            return declaration
        if not network_requested:
            raise SourceAccessError(
                "configured official API sources require an explicit network request"
            )
        if not declaration.explicit_resource_ids:
            raise SourceAccessError("official API access requires explicit resource IDs")
        if not declaration.terms_review_reference:
            raise SourceAccessError("official API access requires a terms review reference")
        return declaration
