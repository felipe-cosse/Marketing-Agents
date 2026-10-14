"""Allowlisted JSON-lines logging with no free-text messages or exception repr."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from enum import StrEnum
from threading import Lock
from typing import Any, TextIO

from marketing_agents.domain.enums import RunState

from .metrics import MAX_SECONDS


class OperationEvent(StrEnum):
    REQUEST = "request_completed"
    RUN = "run_state_changed"
    APPROVAL = "approval_decided"
    CONNECTOR = "connector_result"
    RETRY = "connector_retry"
    IDEMPOTENCY = "idempotency_observed"
    SCHEDULER_CLAIM = "scheduler_claimed"
    SCHEDULER_MISFIRE = "scheduler_misfire"
    AUDIT_FAILURE = "audit_write_failed"


class OperationOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


_INTERNAL_ID = re.compile(r"[a-z][a-z_-]{0,31}\.[a-f0-9]{32}\Z")


def safe_correlation_id(value: object) -> str | None:
    return value if type(value) is str and _INTERNAL_ID.fullmatch(value) else None


class SafeServerDiagnosticFormatter(logging.Formatter):
    """Do not let the ASGI server's error logger bypass application minimization."""

    def format(self, record: logging.LogRecord) -> str:
        level = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warning",
            logging.ERROR: "error",
            logging.CRITICAL: "critical",
        }.get(record.levelno, "unknown")
        # Never call getMessage(), formatException(), or formatStack(): Uvicorn
        # may attach untrusted exception text and transport/request context.
        return json.dumps({"event": "api_server_diagnostic", "level": level}, sort_keys=True)


def safe_server_logging_config() -> dict[str, Any]:
    """Own Uvicorn's diagnostic handlers, without configuring the root logger."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"safe": {"()": SafeServerDiagnosticFormatter}},
        "handlers": {
            "safe": {
                "class": "logging.StreamHandler",
                "formatter": "safe",
                "stream": "ext://sys.stderr",
            }
        },
        "loggers": {
            name: {"handlers": ["safe"], "level": "INFO", "propagate": False}
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
        },
    }


class OperationalLogger:
    """Writes only owned enum codes, bounded numbers, and generated correlation IDs.

    No global logging configuration is changed. The local process stream is
    the sole sink; neither logs nor metrics have a network/export capability.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = sys.stdout if stream is None else stream
        self._lock = Lock()

    def emit(
        self,
        event: OperationEvent,
        outcome: OperationOutcome,
        *,
        correlation_id: str | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        state: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        if type(event) is not OperationEvent or type(outcome) is not OperationOutcome:
            raise ValueError("operational logging requires exact allowlisted enums")
        record: dict[str, str | float | int] = {
            "event": event.value,
            "outcome": outcome.value,
            "provider_mode": "mock",
            "count": 1,
        }
        if type(state) is str and state in {item.value for item in RunState}:
            record["state"] = state
        if (
            type(duration_seconds) in {float, int}
            and duration_seconds is not None
            and math.isfinite(duration_seconds)
            and duration_seconds >= 0
        ):
            record["duration_seconds"] = min(duration_seconds, MAX_SECONDS)
        for name, value in (
            ("correlation_id", correlation_id),
            ("run_id", run_id),
            ("step_id", step_id),
        ):
            # IDs are correlation-only fields, never metric labels. Arbitrary
            # client-provided values are omitted, not stringified or hashed.
            safe_value = safe_correlation_id(value)
            if safe_value is not None:
                record[name] = safe_value
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            self._stream.write(line)
            self._stream.flush()
