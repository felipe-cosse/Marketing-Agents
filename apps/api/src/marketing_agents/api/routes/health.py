"""Side-effect-free process liveness endpoint."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict


class LiveHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    service: Literal["marketing-agents-api"] = "marketing-agents-api"


router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=LiveHealth)
def liveness() -> LiveHealth:
    return LiveHealth()
