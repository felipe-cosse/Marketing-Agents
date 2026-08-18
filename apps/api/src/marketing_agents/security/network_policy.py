"""Fail-closed adapter/network opt-in policy shared by settings and registries."""

from __future__ import annotations

from dataclasses import dataclass


class NetworkPolicyError(ValueError):
    """Raised when real-mode and network opt-ins do not form a safe combination."""


@dataclass(frozen=True)
class AdapterNetworkPolicy:
    llm_provider: str = "mock"
    connector_mode: str = "mock"
    allow_external_network: bool = False
    real_llm_opt_in: bool = False
    real_connector_opt_in: bool = False

    def validate(self) -> AdapterNetworkPolicy:
        real_llm = self.llm_provider != "mock"
        real_connectors = self.connector_mode != "mock"
        if real_llm and (not self.allow_external_network or not self.real_llm_opt_in):
            raise NetworkPolicyError(
                "real LLM mode requires external network and the independent LLM opt-in"
            )
        if real_connectors and (not self.allow_external_network or not self.real_connector_opt_in):
            raise NetworkPolicyError(
                "real connector mode requires external network and the independent connector opt-in"
            )
        if self.allow_external_network and not (real_llm or real_connectors):
            raise NetworkPolicyError(
                "external network cannot be enabled while every adapter remains mock"
            )
        if self.real_llm_opt_in and not real_llm:
            raise NetworkPolicyError("LLM opt-in cannot be enabled for the mock provider")
        if self.real_connector_opt_in and not real_connectors:
            raise NetworkPolicyError("connector opt-in cannot be enabled for mock connectors")
        return self
