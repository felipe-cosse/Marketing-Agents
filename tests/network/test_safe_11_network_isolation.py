import socket
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))

from marketing_agents.security.network_policy import (  # noqa: E402
    AdapterNetworkPolicy,
    NetworkPolicyError,
)

from tests.network.python_network_guard import (  # noqa: E402
    deny_external_network,
    expected_network_denial,
)


class NetworkIsolationTests(unittest.TestCase):
    """Requirement SAFE-11: Python sockets, DNS, and unsafe real modes fail before egress."""

    def test_safe_11_denies_socket_dns_udp_and_create_connection_canaries(self) -> None:
        # AC-15: even a broken guard must never send a canary to a native delegate.
        tripwire = Mock(side_effect=AssertionError("network canary reached delegate"))
        with ExitStack() as stack:
            for owner, name in (
                (socket, "getaddrinfo"),
                (socket, "create_connection"),
                (socket.socket, "connect"),
                (socket.socket, "sendto"),
            ):
                stack.enter_context(patch.object(owner, name, tripwire))
            stack.enter_context(deny_external_network())
            with expected_network_denial():
                socket.getaddrinfo("example.invalid", 443)
            with expected_network_denial():
                socket.create_connection(("203.0.113.10", 443), timeout=0.01)
            peer = SimpleNamespace(family=socket.AF_INET)
            with expected_network_denial():
                socket.socket.connect(peer, ("198.51.100.20", 443))
            with expected_network_denial():
                socket.socket.sendto(peer, b"canary", ("192.0.2.30", 53))
        tripwire.assert_not_called()

    def test_safe_11_allows_only_loopback_host_classification(self) -> None:
        from tests.network.python_network_guard import is_loopback_host

        for allowed in (None, "localhost", "127.0.0.1", "127.22.1.9", "::1"):
            self.assertTrue(is_loopback_host(allowed), allowed)
        for denied in ("example.com", "0.0.0.0", "192.0.2.1", "2001:db8::1"):
            self.assertFalse(is_loopback_host(denied), denied)

    def test_safe_11_real_modes_require_independent_network_opt_ins(self) -> None:
        AdapterNetworkPolicy().validate()
        with self.assertRaises(NetworkPolicyError):
            AdapterNetworkPolicy(llm_provider="real").validate()
        with self.assertRaises(NetworkPolicyError):
            AdapterNetworkPolicy(llm_provider="real", allow_external_network=True).validate()
        AdapterNetworkPolicy(
            llm_provider="real", allow_external_network=True, real_llm_opt_in=True
        ).validate()
        with self.assertRaises(NetworkPolicyError):
            AdapterNetworkPolicy(connector_mode="real", allow_external_network=True).validate()
        AdapterNetworkPolicy(
            connector_mode="real", allow_external_network=True, real_connector_opt_in=True
        ).validate()
        with self.assertRaises(NetworkPolicyError):
            AdapterNetworkPolicy(allow_external_network=True).validate()


if __name__ == "__main__":
    unittest.main()
