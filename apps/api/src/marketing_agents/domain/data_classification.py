"""Ordered data classifications shared by redaction, artifacts, and retention."""

from __future__ import annotations

from enum import StrEnum


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


_CLASSIFICATION_RANK = {item: rank for rank, item in enumerate(DataClassification)}


def highest_classification(
    *values: DataClassification,
    default: DataClassification = DataClassification.INTERNAL,
) -> DataClassification:
    if not values:
        return default
    return max(values, key=_CLASSIFICATION_RANK.__getitem__)
