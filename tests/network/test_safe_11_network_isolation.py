import socket
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))

from marketing_agents.security.network_policy import AdapterNetworkPolicy, NetworkPolicyError  # noqa: E402
from tests.network.python_network_guard import NetworkAccessBlocked, deny_external_network  # noqa: E402


class NetworkIsolationTests(unittest.TestCase):
    """Requirement SAFE-11: Python sockets, DNS, and unsafe real modes fail before egress."""

    def test_safe_11_denies_socket_dns_udp_and_create_connection_canaries(self) -> None:
        with deny_external_network():
            with self.assertRaises(NetworkAccessBlocked):
                socket.getaddrinfo("example.invalid", 443)
            with self.assertRaises(NetworkAccessBlocked):
                socket.create_connection(("203.0.113.10", 443), timeout=0.01)
            with socket.socket() as tcp:
                with self.assertRaises(NetworkAccessBlocked):
                    tcp.connect(("198.51.100.20", 443))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                with self.assertRaises(NetworkAccessBlocked):
                    udp.sendto(b"canary", ("192.0.2.30", 53))

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
        AdapterNetworkPolicy(llm_provider="real", allow_external_network=True, real_llm_opt_in=True).validate()
        with self.assertRaises(NetworkPolicyError):
            AdapterNetworkPolicy(connector_mode="real", allow_external_network=True).validate()
        AdapterNetworkPolicy(connector_mode="real", allow_external_network=True, real_connector_opt_in=True).validate()
        with self.assertRaises(NetworkPolicyError):
            AdapterNetworkPolicy(allow_external_network=True).validate()


if __name__ == "__main__":
    unittest.main()
