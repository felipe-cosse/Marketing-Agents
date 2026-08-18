"""Shared SQLAlchemy metadata without owning any data-bearing tables."""

from __future__ import annotations

from types import MappingProxyType

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = MappingProxyType(
    {
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative root reserved for DEL-04 persistence records and migrations."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
