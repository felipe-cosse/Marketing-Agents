"""Application-level socket and DNS denial for Python test processes."""

from __future__ import annotations

import ipaddress
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator


class NetworkAccessBlocked(RuntimeError):
    """Raised before a test can resolve or connect to a non-loopback host."""


def _host_from_address(address: Any) -> str | None:
    if isinstance(address, tuple) and address:
        return str(address[0])
    if isinstance(address, bytes):
        return address.decode("ascii", errors="ignore")
    if isinstance(address, str):
        if address.startswith("/"):
            return None
        return address
    return None


def is_loopback_host(host: str | bytes | None) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    normalized = host.strip().lower().rstrip(".")
    if normalized in {"", "localhost", "localhost.localdomain"}:
        return True
    candidate = normalized.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def assert_loopback(address: Any) -> None:
    host = _host_from_address(address)
    if not is_loopback_host(host):
        raise NetworkAccessBlocked(f"external network access blocked for host {host!r}")


@dataclass
class InstalledNetworkGuard:
    originals: dict[str, Any]

    def restore(self) -> None:
        socket.socket.connect = self.originals["socket.connect"]
        socket.socket.connect_ex = self.originals["socket.connect_ex"]
        socket.socket.sendto = self.originals["socket.sendto"]
        socket.create_connection = self.originals["create_connection"]
        socket.getaddrinfo = self.originals["getaddrinfo"]


def install_network_guard() -> InstalledNetworkGuard:
    originals = {
        "socket.connect": socket.socket.connect,
        "socket.connect_ex": socket.socket.connect_ex,
        "socket.sendto": socket.socket.sendto,
        "create_connection": socket.create_connection,
        "getaddrinfo": socket.getaddrinfo,
    }

    def guarded_connect(instance: socket.socket, address: Any) -> Any:
        assert_loopback(address)
        return originals["socket.connect"](instance, address)

    def guarded_connect_ex(instance: socket.socket, address: Any) -> Any:
        assert_loopback(address)
        return originals["socket.connect_ex"](instance, address)

    def guarded_sendto(instance: socket.socket, data: bytes, *args: Any) -> Any:
        if args:
            assert_loopback(args[-1])
        return originals["socket.sendto"](instance, data, *args)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        assert_loopback(address)
        return originals["create_connection"](address, *args, **kwargs)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not is_loopback_host(host):
            raise NetworkAccessBlocked(f"external DNS resolution blocked for host {host!r}")
        return originals["getaddrinfo"](host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.socket.sendto = guarded_sendto
    socket.create_connection = guarded_create_connection
    socket.getaddrinfo = guarded_getaddrinfo
    return InstalledNetworkGuard(originals)


@contextmanager
def deny_external_network() -> Iterator[None]:
    guard = install_network_guard()
    try:
        yield
    finally:
        guard.restore()
