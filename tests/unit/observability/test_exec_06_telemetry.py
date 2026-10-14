"""EXEC-06: operational diagnostics stay finite, local, and independent of success."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from io import StringIO

import pytest
from marketing_agents.observability import LocalTelemetry
from marketing_agents.observability.logging import (
    OperationEvent,
    OperationOutcome,
    SafeServerDiagnosticFormatter,
)
from marketing_agents.observability.metrics import MAX_SECONDS, MAX_SERIES, LocalMetrics
from marketing_agents.observability.telemetry import CommittedObservation
from marketing_agents.security.redaction import SecretValue, redact, redact_json_pointers


def total(telemetry, name):
    return sum(item.total for item in telemetry.metrics.snapshot() if item.name == name)


@pytest.mark.parametrize(
    "label",
    (
        "actor",
        "email",
        "run_id",
        "schedule_id",
        "payload_hash",
        "destination",
        "path",
        "error",
    ),
)
def test_arbitrary_metric_dimensions_never_create_a_series(label):
    metrics = LocalMetrics()
    with pytest.raises(ValueError, match="label keys"):
        metrics.observe("requests", labels={label: "canary@example.invalid"})
    assert len(metrics.snapshot()) == 1


@pytest.mark.parametrize("value", ("canary@example.invalid", "actor.secret", "a" * 4096))
def test_untrusted_label_values_are_not_stringified_or_retained(value):
    metrics = LocalMetrics()
    with pytest.raises(ValueError, match="label value"):
        metrics.observe("requests", labels={"status": value})
    assert value not in repr(metrics.snapshot())
    assert metrics.catalog_label("workflow", value) == "unknown"


@pytest.mark.parametrize("value", (True, float("nan"), float("inf"), -1, "secret"))
def test_metric_values_are_strict_nonnegative_finite_numbers(value):
    with pytest.raises(ValueError, match="observation"):
        LocalMetrics().observe("scheduler_claims", value=value)


def test_series_and_observation_storage_are_bounded_and_thread_safe():
    metrics = LocalMetrics(
        workflow_ids=tuple(f"workflow.{i}" for i in range(100)),
        template_ids=tuple(f"tpl.{i}" for i in range(100)),
    )
    for workflow in range(100):
        for template in range(100):
            metrics.observe(
                "run_transitions",
                labels={
                    "state": "completed",
                    "workflow": f"workflow.{workflow}",
                    "template": f"tpl.{template}",
                },
            )
    assert len(metrics.snapshot()) == MAX_SERIES
    assert next(s for s in metrics.snapshot() if s.name == "dropped_series").count > 0
    telemetry = LocalTelemetry(stream=StringIO())
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: telemetry.metrics.observe("scheduler_claims"), range(1000)))
    assert total(telemetry, "scheduler_claims") == 1000
    telemetry.metrics.observe("scheduler_lag_seconds", value=1e20)
    assert total(telemetry, "scheduler_lag_seconds") == MAX_SECONDS


def test_logger_excludes_untrusted_context_and_accepts_only_safe_owned_codes():
    stream = StringIO()
    telemetry = LocalTelemetry(stream=stream)
    canaries = ("canary@example.invalid", "Bearer secret-token-canary", "nested\nprivate-context")
    telemetry.record(
        CommittedObservation(
            OperationEvent.REQUEST,
            OperationOutcome.REJECTED,
            "requests",
            (("status", "4xx"),),
            correlation_id=canaries[0],
            run_id=canaries[1],
            step_id=canaries[2],
        )
    )
    line = json.loads(stream.getvalue())
    assert line == {
        "event": "request_completed",
        "outcome": "rejected",
        "provider_mode": "mock",
        "count": 1,
    }
    for canary in canaries:
        assert canary not in stream.getvalue() + repr(telemetry.metrics.snapshot())
    with pytest.raises(ValueError):
        telemetry.logger.emit("untrusted-secret-error", OperationOutcome.FAILED)


def test_actual_redaction_boundary_counts_only_scoped_masks():
    telemetry = LocalTelemetry(stream=StringIO())
    other = LocalTelemetry(stream=StringIO())
    assert redact({"email": "outside@example.invalid"}) == {"email": "[REDACTED]"}
    assert total(telemetry, "redactions") == 0
    with telemetry.bind():
        redact({"email": "inside@example.invalid"})
        redact("private", schema={"x-sensitive": True})
        redact(SecretValue("secret-canary"))
        redact_json_pointers({"sensitive": [{"body": "private"}]}, ("/sensitive/*/body",))
        with other.bind():
            redact({"api_key": "nested-private-key"})
        redact({"phone": "private-number"})
    assert total(telemetry, "redactions") == 5
    assert total(other, "redactions") == 1
    redact({"email": "outside-again@example.invalid"})
    assert total(telemetry, "redactions") == 5


class BrokenSink:
    def write(self, value):
        raise OSError("secret credential canary in untrusted exception")

    def flush(self):
        raise OSError("private flush exception")


def test_broken_sink_is_not_a_business_error_or_an_exception_leak():
    telemetry = LocalTelemetry(stream=BrokenSink())
    telemetry.request(200, 0.01, "request." + "a" * 32)
    telemetry.audit_failure()
    telemetry.scheduler_claim(3)
    assert total(telemetry, "requests") == 1
    assert total(telemetry, "audit_write_failures") == 1
    assert total(telemetry, "scheduler_claims") == 1
    assert total(telemetry, "diagnostic_failures") == 3
    assert "canary" not in repr(telemetry.metrics.snapshot())


def test_asgi_server_formatter_never_formats_message_exception_stack_or_extras():
    exception = RuntimeError("private-exception-canary")
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        "/private/path-canary",
        11,
        "private-message-canary %s",
        ("private-argument-canary",),
        (RuntimeError, exception, None),
    )
    record.stack_info = "private-stack-canary"
    record.exc_text = "cached-private-traceback-canary"
    record.headers = {"Authorization": "private-token-canary"}
    assert json.loads(SafeServerDiagnosticFormatter().format(record)) == {
        "event": "api_server_diagnostic",
        "level": "error",
    }
