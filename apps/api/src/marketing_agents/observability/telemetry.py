"""Best-effort local operational observations that never grant product authority."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TextIO

from .context import redaction_observer
from .logging import OperationalLogger, OperationEvent, OperationOutcome
from .metrics import LocalMetrics


@dataclass(frozen=True, slots=True)
class CommittedObservation:
    """Only projected safe facts may be staged until the outer DB commit."""

    event: OperationEvent
    outcome: OperationOutcome
    metric: str
    labels: tuple[tuple[str, str], ...] = ()
    value: float = 1
    correlation_id: str | None = None
    run_id: str | None = None
    step_id: str | None = None
    approval_age_seconds: float | None = None
    duration_seconds: float | None = None


class LocalTelemetry:
    def __init__(
        self,
        *,
        workflow_ids: tuple[str, ...] = (),
        template_ids: tuple[str, ...] = (),
        instance_templates: Mapping[str, str] | None = None,
        stream: TextIO | None = None,
    ) -> None:
        if instance_templates is not None and len(instance_templates) > 1_024:
            raise ValueError("telemetry instance map must be bounded")
        self.metrics = LocalMetrics(workflow_ids=workflow_ids, template_ids=template_ids)
        self.logger = OperationalLogger(stream)
        self._instance_templates = dict(instance_templates or {})

    def template_for(self, instance_id: object) -> str:
        value = self._instance_templates.get(instance_id) if type(instance_id) is str else None
        return self.metrics.catalog_label("template", value)

    @contextmanager
    def bind(self) -> Iterator[None]:
        token = redaction_observer.set(self.redaction)
        try:
            yield
        finally:
            redaction_observer.reset(token)

    def diagnostic_failure(self) -> None:
        with suppress(Exception):
            self.metrics.observe("diagnostic_failures")

    def record(self, observation: CommittedObservation) -> None:
        try:
            self.metrics.observe(
                observation.metric, labels=dict(observation.labels), value=observation.value
            )
            if observation.approval_age_seconds is not None:
                self.metrics.observe(
                    "approval_queue_seconds",
                    labels=dict(observation.labels),
                    value=observation.approval_age_seconds,
                )
            self.logger.emit(
                observation.event,
                observation.outcome,
                correlation_id=observation.correlation_id,
                run_id=observation.run_id,
                step_id=observation.step_id,
                state=dict(observation.labels).get("state"),
                duration_seconds=observation.duration_seconds,
            )
        except Exception:
            # A broken local sink cannot turn a successful committed mutation
            # into a client-visible failure/retry. Do not log the exception.
            self.diagnostic_failure()

    def request(self, status: int, seconds: float, correlation_id: str) -> None:
        status_class = f"{min(5, max(1, status // 100))}xx"
        self.record(
            CommittedObservation(
                OperationEvent.REQUEST,
                OperationOutcome.ACCEPTED if status < 400 else OperationOutcome.REJECTED,
                "requests",
                (("status", status_class),),
                correlation_id=correlation_id,
                duration_seconds=seconds,
            )
        )
        with suppress(Exception):
            self.metrics.observe("request_seconds", labels={"status": status_class}, value=seconds)

    def scheduler_claim(self, lag_seconds: float) -> None:
        self.record(
            CommittedObservation(
                OperationEvent.SCHEDULER_CLAIM,
                OperationOutcome.ACCEPTED,
                "scheduler_claims",
            )
        )
        with suppress(Exception):
            self.metrics.observe("scheduler_lag_seconds", value=max(0, lag_seconds))

    def audit_failure(self) -> None:
        self.record(
            CommittedObservation(
                OperationEvent.AUDIT_FAILURE,
                OperationOutcome.FAILED,
                "audit_write_failures",
            )
        )

    def redaction(self, boundary: str) -> None:
        try:
            self.metrics.observe("redactions", labels={"boundary": boundary})
        except Exception:
            self.diagnostic_failure()
