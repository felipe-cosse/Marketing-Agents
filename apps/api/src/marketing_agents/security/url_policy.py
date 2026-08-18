"""Validation for inert provenance URLs; this module performs no fetch or DNS lookup."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class UrlPolicyError(ValueError):
    """Raised when a reference URL is unsafe or outside an exact allowlist."""


@dataclass(frozen=True)
class ValidatedReferenceUrl:
    value: str
    host: str
    port: int
    provenance_only: bool = True


def validate_reference_url(
    value: str, *, allowed_hosts: frozenset[str] | None = None
) -> ValidatedReferenceUrl:
    if not value or len(value) > 2_048:
        raise UrlPolicyError("reference URL length is invalid")
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https":
        raise UrlPolicyError("reference URLs must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise UrlPolicyError("reference URLs cannot contain user information")
    if parsed.fragment:
        raise UrlPolicyError("reference URLs cannot contain fragments")
    if not parsed.hostname:
        raise UrlPolicyError("reference URL must include a host")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise UrlPolicyError("reference URL port is invalid") from exc
    if port != 443:
        raise UrlPolicyError("reference URLs may use only the HTTPS default port")
    host = parsed.hostname.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UrlPolicyError("reference URL host is invalid") from exc
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UrlPolicyError("local reference URL hosts are forbidden")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UrlPolicyError("IP-literal reference URL hosts are forbidden")
    if allowed_hosts is not None and host not in {item.lower() for item in allowed_hosts}:
        raise UrlPolicyError("reference URL host is not allowlisted")
    normalized = urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    return ValidatedReferenceUrl(value=normalized, host=host, port=443)
