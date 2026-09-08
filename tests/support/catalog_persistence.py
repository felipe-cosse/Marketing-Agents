"""Seed catalog parents for tests that exercise narrow configuration persistence."""

from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog_projection
from marketing_agents.infrastructure.db import DatabaseRuntime


async def seed_catalog_parents(runtime: DatabaseRuntime, catalog: CompiledCatalog) -> None:
    """Persist real catalog parents without creating mutable instance configurations."""

    async with runtime.session_factory() as session, session.begin():
        await seed_catalog_projection(session, catalog)
