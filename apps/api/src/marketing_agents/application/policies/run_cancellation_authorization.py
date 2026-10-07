"""Human-operator authority for cancellation, independent of transport claims."""

from marketing_agents.domain.identity import AuthenticatedPrincipal, PrincipalKind


class RunCancellationAuthorizationError(PermissionError):
    """An intact server-issued human operator is required."""


def authorize_run_cancellation_operator(principal: AuthenticatedPrincipal) -> None:
    if type(principal) is not AuthenticatedPrincipal:
        raise RunCancellationAuthorizationError("run cancellation is forbidden")
    try:
        principal.verify_integrity()
    except (TypeError, ValueError):
        raise RunCancellationAuthorizationError("run cancellation is forbidden") from None
    if principal.kind is not PrincipalKind.HUMAN or "operator" not in principal.roles:
        raise RunCancellationAuthorizationError("run cancellation is forbidden")
