"""Finite-label, fixed-memory process-local counters and duration summaries."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock

from marketing_agents.domain.enums import RunState

MAX_SERIES = 1_024
MAX_VALUE = 1_000_000_000_000
MAX_SECONDS = 365 * 24 * 60 * 60
_CATALOG_ID = re.compile(r"[a-z][a-z0-9_.-]{0,159}\Z")
_FIXED_LABELS = {
    "state": frozenset(item.value for item in RunState),
    "status": frozenset({"1xx", "2xx", "3xx", "4xx", "5xx"}),
    "decision": frozenset({"approved", "rejected", "expired"}),
    "result": frozenset({"succeeded", "failed", "unknown", "retry"}),
    "kind": frozenset({"duplicate", "conflict"}),
    "disposition": frozenset({"skipped", "run_once"}),
    "boundary": frozenset({"schema", "field", "secret", "pointer"}),
}
_METRIC_LABELS = {
    "requests": ("status",),
    "request_seconds": ("status",),
    "run_transitions": ("state", "workflow", "template"),
    "approval_decisions": ("decision",),
    "approval_queue_seconds": ("decision",),
    "connector_results": ("result",),
    "connector_retries": (),
    "idempotency": ("kind",),
    "scheduler_claims": (),
    "scheduler_lag_seconds": (),
    "scheduler_misfires": ("disposition",),
    "audit_write_failures": (),
    "redactions": ("boundary",),
    "diagnostic_failures": (),
    "dropped_series": (),
}


@dataclass(frozen=True, slots=True)
class MetricSample:
    name: str
    labels: tuple[tuple[str, str], ...]
    count: int
    total: float
    maximum: float


class LocalMetrics:
    """No exporter, identifiers, raw observations, or unbounded label creation.

    Summaries retain count/sum/max only, never individual observations. Values
    saturate; excess series increment one preallocated diagnostic counter.
    Catalog labels come only from the trusted startup snapshot, never requests.
    """

    def __init__(
        self, *, workflow_ids: tuple[str, ...] = (), template_ids: tuple[str, ...] = ()
    ) -> None:
        self._labels = dict(_FIXED_LABELS)
        for kind, values in (("workflow", workflow_ids), ("template", template_ids)):
            if (
                type(values) is not tuple
                or len(values) > 128
                or any(
                    type(value) is not str or not _CATALOG_ID.fullmatch(value) for value in values
                )
            ):
                raise ValueError("telemetry catalog labels must be bounded trusted identifiers")
            self._labels[kind] = frozenset((*values, "unknown"))
        self._lock = Lock()
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], MetricSample] = {
            ("dropped_series", ()): MetricSample("dropped_series", (), 0, 0, 0)
        }

    def catalog_label(self, kind: str, value: object) -> str:
        if kind not in {"workflow", "template"}:
            raise ValueError("unknown telemetry catalog label kind")
        return value if type(value) is str and value in self._labels[kind] else "unknown"

    def observe(
        self, name: str, *, labels: Mapping[str, str] | None = None, value: float = 1
    ) -> None:
        expected = _METRIC_LABELS.get(name)
        actual = {} if labels is None else dict(labels)
        if expected is None or set(actual) != set(expected):
            raise ValueError("metric name or label keys are not allowlisted")
        if any(type(v) is not str or v not in self._labels[k] for k, v in actual.items()):
            raise ValueError("metric label value is not allowlisted")
        if type(value) not in {float, int} or not math.isfinite(value) or value < 0:
            raise ValueError("metric observation must be a finite nonnegative number")
        bounded = float(min(value, MAX_SECONDS if name.endswith("_seconds") else MAX_VALUE))
        key = (name, tuple(sorted(actual.items())))
        with self._lock:
            if key not in self._values and len(self._values) >= MAX_SERIES:
                key, bounded = ("dropped_series", ()), 1
            prior = self._values.get(key, MetricSample(key[0], key[1], 0, 0, 0))
            self._values[key] = MetricSample(
                key[0],
                key[1],
                min(prior.count + 1, MAX_VALUE),
                min(prior.total + bounded, MAX_VALUE),
                max(prior.maximum, bounded),
            )

    def snapshot(self) -> tuple[MetricSample, ...]:
        with self._lock:
            return tuple(self._values[key] for key in sorted(self._values))
