"""Process-local, authenticated-source webhook admission rate limiting."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock

from marketing_agents.application.ports.webhooks import require_webhook_source_id

MAX_WEBHOOK_RATE_CALLS = 10_000
MAX_WEBHOOK_RATE_WINDOW_SECONDS = 3_600
MAX_TRACKED_WEBHOOK_SOURCES = 1_024


class WebhookAdmissionRateLimiterUnavailable(RuntimeError):
    """The bounded process-local limiter cannot safely track another authority."""


@dataclass(frozen=True, slots=True)
class WebhookAdmissionRateDecision:
    """Safe result of consuming one authenticated source admission slot."""

    allowed: bool
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ValueError("webhook rate decision allowed flag must be an exact boolean")
        if self.allowed:
            if self.retry_after_seconds is not None:
                raise ValueError("allowed webhook rate decision cannot contain retry metadata")
            return
        if (
            type(self.retry_after_seconds) is not int
            or not 1 <= self.retry_after_seconds <= MAX_WEBHOOK_RATE_WINDOW_SECONDS
        ):
            raise ValueError("denied webhook rate decision requires a bounded retry interval")


@dataclass(slots=True)
class _SourceWindow:
    started_at: datetime
    last_seen_at: datetime
    calls: int
    max_calls: int
    window_seconds: int


class ProcessLocalWebhookAdmissionRateLimiter:
    """Bound authenticated sources with exact fixed windows in one API process.

    V1 intentionally runs one API process. A multi-process deployment must replace
    this limiter with shared state before it can claim a global admission bound.
    """

    def __init__(self, *, max_tracked_sources: int = MAX_TRACKED_WEBHOOK_SOURCES) -> None:
        if (
            type(max_tracked_sources) is not int
            or not 1 <= max_tracked_sources <= MAX_TRACKED_WEBHOOK_SOURCES
        ):
            raise ValueError("tracked webhook source bound is invalid")
        self._max_tracked_sources = max_tracked_sources
        self._windows: dict[str, _SourceWindow] = {}
        self._lock = Lock()

    def consume(
        self,
        *,
        source: str,
        observed_at: datetime,
        max_calls: int,
        window_seconds: int,
    ) -> WebhookAdmissionRateDecision:
        """Consume one slot after signature verification, or return bounded retry data."""

        require_webhook_source_id(source, "webhook rate-limit source")
        if type(observed_at) is not datetime:
            raise ValueError("webhook rate-limit observation must be a datetime")
        offset = observed_at.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("webhook rate-limit observation must be timezone-aware UTC")
        if type(max_calls) is not int or not 1 <= max_calls <= MAX_WEBHOOK_RATE_CALLS:
            raise ValueError("webhook rate-limit call bound is invalid")
        if (
            type(window_seconds) is not int
            or not 1 <= window_seconds <= MAX_WEBHOOK_RATE_WINDOW_SECONDS
        ):
            raise ValueError("webhook rate-limit window is invalid")

        with self._lock:
            state = self._windows.get(source)
            if state is None:
                self._prune_expired(observed_at)
                if len(self._windows) >= self._max_tracked_sources:
                    raise WebhookAdmissionRateLimiterUnavailable(
                        "webhook admission rate limiter capacity is exhausted"
                    )
                self._windows[source] = _SourceWindow(
                    started_at=observed_at,
                    last_seen_at=observed_at,
                    calls=1,
                    max_calls=max_calls,
                    window_seconds=window_seconds,
                )
                return WebhookAdmissionRateDecision(allowed=True)

            if state.max_calls != max_calls or state.window_seconds != window_seconds:
                raise WebhookAdmissionRateLimiterUnavailable(
                    "webhook admission rate policy changed within an active process"
                )

            # A wall-clock rollback must not create a fresh quota. Retain the most
            # recent observation until the clock catches up.
            effective_at = max(observed_at, state.last_seen_at)
            state.last_seen_at = effective_at
            expires_at = state.started_at + timedelta(seconds=state.window_seconds)
            if effective_at >= expires_at:
                state.started_at = effective_at
                state.calls = 1
                return WebhookAdmissionRateDecision(allowed=True)
            if state.calls < state.max_calls:
                state.calls += 1
                return WebhookAdmissionRateDecision(allowed=True)

            retry_after = math.ceil((expires_at - effective_at).total_seconds())
            return WebhookAdmissionRateDecision(
                allowed=False,
                retry_after_seconds=max(1, min(retry_after, state.window_seconds)),
            )

    def tracked_source_count(self) -> int:
        """Return only bounded cardinality, never source identities."""

        with self._lock:
            return len(self._windows)

    def _prune_expired(self, observed_at: datetime) -> None:
        expired = tuple(
            source
            for source, state in self._windows.items()
            if max(observed_at, state.last_seen_at)
            >= state.started_at + timedelta(seconds=state.window_seconds)
        )
        for source in expired:
            del self._windows[source]


__all__ = [
    "MAX_TRACKED_WEBHOOK_SOURCES",
    "MAX_WEBHOOK_RATE_CALLS",
    "MAX_WEBHOOK_RATE_WINDOW_SECONDS",
    "ProcessLocalWebhookAdmissionRateLimiter",
    "WebhookAdmissionRateDecision",
    "WebhookAdmissionRateLimiterUnavailable",
]
