"""In-process Python test isolation; ordinary child processes need a kernel boundary."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Never


class NetworkAccessBlocked(RuntimeError):
    """A categorical denial that never exposes addresses or credentials."""


@dataclass
class _ExpectedDenial:
    count: int = 0
    active: bool = True


_expected_denial: ContextVar[_ExpectedDenial | None] = ContextVar("ac15_denial", default=None)
_denied_attempts = 0
_unexpected_attempts = 0


def network_attempt_counts() -> tuple[int, int]:
    """Monotonic (all denied, unacknowledged denied) counts across nested guards."""
    return _denied_attempts, _unexpected_attempts


def network_guard_installed() -> bool:
    """Detect transport replacement by pytest-socket before fixture execution."""
    resolvers = all(
        getattr(getattr(socket, name), "_ac15_network_guard", False)
        for name in (
            "create_connection",
            "getaddrinfo",
            "gethostbyname",
            "gethostbyname_ex",
            "gethostbyaddr",
            "getnameinfo",
        )
    )
    transports = all(
        getattr(getattr(socket.socket, name), "_ac15_network_guard", False)
        for name in ("connect", "connect_ex", "sendto", "sendmsg")
        if hasattr(socket.socket, name)
    )
    return resolvers and transports


def _deny() -> Never:
    global _denied_attempts, _unexpected_attempts
    _denied_attempts += 1
    expected = _expected_denial.get()
    if expected is None or not expected.active:
        _unexpected_attempts += 1
    else:
        expected.count += 1
    raise NetworkAccessBlocked("external network operation blocked by the Python test policy")


@contextmanager
def expected_network_denial() -> Iterator[None]:
    """Acknowledge exactly one actual guard denial, never a no-op or arbitrary error."""
    global _unexpected_attempts
    previous = _expected_denial.get()
    if previous is not None and previous.active:
        raise AssertionError("expected network denial scopes cannot nest")
    expected = _ExpectedDenial()
    token = _expected_denial.set(expected)
    accepted = False
    try:
        try:
            yield
        except NetworkAccessBlocked:
            if expected.count != 1:
                raise AssertionError("expected exactly one installed-guard denial") from None
            accepted = True
        else:
            raise AssertionError("expected an installed-guard denial to escape the operation")
    finally:
        # Copied async contexts retain this object; a child cannot acknowledge
        # later attempts merely because it was created inside a completed scope.
        expected.active = False
        _expected_denial.reset(token)
        if not accepted:
            # Catching an invalid scope's AssertionError must not erase actual
            # attempts already made while that scope was active.
            _unexpected_attempts += expected.count


def _normalized_host(host: Any) -> str | None:
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return None
    return host.lower().rstrip(".") if isinstance(host, str) else None


def _host_from_address(address: Any) -> str | bytes | None:
    if isinstance(address, tuple) and address:
        return address[0]
    if isinstance(address, str | bytes):
        if address.startswith("/" if isinstance(address, str) else b"/"):
            return None
        return address
    return None


def is_loopback_host(host: str | bytes | None) -> bool:
    if host is None:
        return True
    normalized = _normalized_host(host)
    if normalized in {"", "localhost", "localhost.localdomain"}:
        return True
    if normalized is None:
        return False
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def assert_loopback(address: Any) -> None:
    """Pure classification also used by safe delegate tripwires; not an operation."""
    if not is_loopback_host(_host_from_address(address)):
        raise NetworkAccessBlocked("external network destination blocked by the Python test policy")


def _numeric_host(host: Any, family: int = socket.AF_UNSPEC) -> str:
    normalized = _normalized_host(host)
    if normalized in {"localhost", "localhost.localdomain", ""} or host is None:
        return "::1" if family == socket.AF_INET6 else "127.0.0.1"
    if normalized is None:
        return _deny()
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return _deny()
    if not address.is_loopback:
        return _deny()
    return str(address)


def _local_address(address: Any, family: int = socket.AF_UNSPEC) -> Any:
    if family == socket.AF_UNIX:
        return address
    if not isinstance(address, tuple) or len(address) < 2:
        return _deny()
    return (_numeric_host(address[0], family), *address[1:])


@dataclass
class InstalledNetworkGuard:
    originals: tuple[tuple[Any, str, Any], ...]
    restored: bool = False

    def restore(self) -> None:
        if not self.restored:
            for owner, name, original in reversed(self.originals):
                setattr(owner, name, original)
            self.restored = True


def install_network_guard() -> InstalledNetworkGuard:
    socket_class = socket.socket
    originals: list[tuple[Any, str, Any]] = []

    def patch(owner: Any, name: str, replacement: Any) -> Any:
        original = getattr(owner, name)
        originals.append((owner, name, original))
        replacement._ac15_network_guard = True
        setattr(owner, name, replacement)
        return original

    def guarded_connect(instance: socket.socket, address: Any) -> Any:
        return connect(instance, _local_address(address, instance.family))

    def guarded_connect_ex(instance: socket.socket, address: Any) -> Any:
        return connect_ex(instance, _local_address(address, instance.family))

    def guarded_sendto(instance: socket.socket, data: bytes, *args: Any) -> Any:
        if not args:
            return _deny()
        return sendto(instance, data, *args[:-1], _local_address(args[-1], instance.family))

    def guarded_sendmsg(instance: socket.socket, buffers: Any, *args: Any, **kwargs: Any) -> Any:
        # Connected sockets passed through guarded connect; explicit UDP peers
        # must be checked too. Unix ancillary-data traffic is preserved.
        if len(args) >= 3 and args[2] is not None:
            args = (*args[:2], _local_address(args[2], instance.family), *args[3:])
        if kwargs.get("address") is not None:
            kwargs["address"] = _local_address(kwargs["address"], instance.family)
        return sendmsg(instance, buffers, *args, **kwargs)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        return create_connection(_local_address(address), *args, **kwargs)

    def guarded_getaddrinfo(
        host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> Any:
        numeric = _numeric_host(host, family)
        if port is not None and not isinstance(port, int) and not str(port).isdigit():
            raise socket.gaierror(socket.EAI_SERVICE, "numeric test service required")
        # Numeric-only flags prohibit resolver/NSS lookup in the native delegate.
        # Localhost is rewritten before this call, never sent to an OS resolver.
        return getaddrinfo(
            numeric,
            port,
            family,
            type,
            proto,
            flags | socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
        )

    def guarded_gethostbyname(host: Any) -> str:
        numeric = _numeric_host(host, socket.AF_INET)
        if ":" in numeric:
            raise socket.gaierror(socket.EAI_ADDRFAMILY, "IPv4 test address required")
        return numeric

    def guarded_gethostbyname_ex(host: Any) -> tuple[str, list[str], list[str]]:
        numeric = guarded_gethostbyname(host)
        return (_normalized_host(host) or numeric, [], [numeric])

    def guarded_gethostbyaddr(host: Any) -> tuple[str, list[str], list[str]]:
        return ("localhost", [], [_numeric_host(host)])

    def guarded_getnameinfo(address: Any, flags: int) -> tuple[str, str]:
        numeric, port, *_ = _local_address(address)
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise socket.gaierror(socket.EAI_SERVICE, "numeric test service required")
        return (numeric if flags & socket.NI_NUMERICHOST else "localhost", str(port))

    connect = patch(socket_class, "connect", guarded_connect)
    connect_ex = patch(socket_class, "connect_ex", guarded_connect_ex)
    sendto = patch(socket_class, "sendto", guarded_sendto)
    if hasattr(socket_class, "sendmsg"):
        sendmsg = patch(socket_class, "sendmsg", guarded_sendmsg)
    create_connection = patch(socket, "create_connection", guarded_create_connection)
    getaddrinfo = patch(socket, "getaddrinfo", guarded_getaddrinfo)
    patch(socket, "gethostbyname", guarded_gethostbyname)
    patch(socket, "gethostbyname_ex", guarded_gethostbyname_ex)
    patch(socket, "gethostbyaddr", guarded_gethostbyaddr)
    patch(socket, "getnameinfo", guarded_getnameinfo)
    return InstalledNetworkGuard(tuple(originals))


@contextmanager
def deny_external_network() -> Iterator[InstalledNetworkGuard]:
    guard = install_network_guard()
    try:
        yield guard
    finally:
        guard.restore()
