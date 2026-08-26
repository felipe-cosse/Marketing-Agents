"""Append-only redacted audit timeline records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("id", name="uq_audit_events_id"),
        UniqueConstraint("run_id", "run_sequence", name="uq_audit_events_run_sequence"),
        UniqueConstraint(
            "run_id",
            "event_type",
            "attempt_id",
            name="uq_audit_events_run_event_attempt",
        ),
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "mutation_version",
            name="uq_audit_events_aggregate_version",
        ),
        ForeignKeyConstraint(
            ["run_id", "run_transition_sequence"],
            ["run_state_transitions.run_id", "run_state_transitions.sequence"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["step_id", "run_id"],
            ["run_steps.id", "run_steps.run_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["step_id", "run_id", "step_transition_sequence"],
            [
                "run_step_state_transitions.step_id",
                "run_step_state_transitions.run_id",
                "run_step_state_transitions.sequence",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "run_id", "step_id"],
            ["execution_attempts.id", "execution_attempts.run_id", "execution_attempts.step_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["artifact_id", "run_id", "step_id"],
            ["artifacts.id", "artifacts.run_id", "artifacts.step_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["action_id", "run_id", "step_id"],
            ["external_actions.id", "external_actions.run_id", "external_actions.step_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["action_id", "action_attempt_number"],
            [
                "external_action_dispatch_attempts.external_action_id",
                "external_action_dispatch_attempts.attempt_number",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["action_id", "receipt_id"],
            [
                "connector_action_receipts.external_action_id",
                "connector_action_receipts.receipt_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["approval_request_id", "action_id", "run_id", "step_id"],
            [
                "approval_requests.id",
                "approval_requests.action_id",
                "approval_requests.run_id",
                "approval_requests.step_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "approval_decision_id",
                "approval_request_id",
                "action_id",
                "run_id",
                "step_id",
            ],
            [
                "approval_decisions.id",
                "approval_decisions.request_id",
                "approval_decisions.action_id",
                "approval_decisions.run_id",
                "approval_decisions.step_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["occurrence_id", "schedule_id"],
            ["schedule_occurrences.id", "schedule_occurrences.schedule_id"],
            name="fk_audit_events_occurrence_schedule",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "run_sequence IS NULL OR run_sequence >= 1",
            name="ck_audit_events_run_sequence_positive",
        ),
        CheckConstraint(
            "((aggregate_type IN ('schedule','schedule_occurrence') AND "
            "run_id IS NULL AND run_sequence IS NULL AND schedule_id IS NOT NULL AND "
            "occurrence_id IS NOT NULL) OR "
            "(aggregate_type = 'agent_instance_configuration' AND "
            "run_id IS NULL AND run_sequence IS NULL AND schedule_id IS NULL AND "
            "occurrence_id IS NULL) OR "
            "(aggregate_type = 'manual_ingress_rejection' AND "
            "run_id IS NULL AND run_sequence IS NULL AND schedule_id IS NULL AND "
            "occurrence_id IS NULL) OR "
            "(aggregate_type = 'webhook_ingress' AND "
            "run_id IS NULL AND run_sequence IS NULL AND schedule_id IS NULL AND "
            "occurrence_id IS NULL) OR "
            "(aggregate_type NOT IN "
            "('schedule','schedule_occurrence','agent_instance_configuration',"
            "'manual_ingress_rejection','webhook_ingress') AND "
            "run_id IS NOT NULL AND run_sequence IS NOT NULL AND schedule_id IS NULL AND "
            "occurrence_id IS NULL))",
            name="ck_audit_events_timeline_scope",
        ),
        CheckConstraint("schema_version = 1", name="ck_audit_events_schema_version"),
        CheckConstraint(
            "actor_source IN ('system','service','user','worker','connector')",
            name="ck_audit_events_actor_source",
        ),
        CheckConstraint(
            "outcome IN ('accepted','rejected','observed')",
            name="ck_audit_events_outcome",
        ),
        CheckConstraint(
            "(outcome IN ('accepted','observed') AND mutation_version IS NOT NULL) OR "
            "(outcome = 'rejected' AND mutation_version IS NULL)",
            name="ck_audit_events_outcome_mutation",
        ),
        CheckConstraint(
            "(aggregate_type = 'schedule_occurrence' AND event_type IN "
            "('schedule.occurrence_created','schedule.misfire_skipped',"
            "'schedule.misfire_run_once') AND schedule_id IS NOT NULL AND "
            "occurrence_id IS NOT NULL AND aggregate_id = occurrence_id AND "
            "outcome = 'accepted' AND mutation_version = 1 AND "
            "run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND "
            "step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND "
            "receipt_id IS NULL AND approval_request_id IS NULL AND "
            "approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL) OR "
            "(aggregate_type = 'schedule' AND "
            "event_type = 'schedule.next_occurrence_persisted' AND "
            "schedule_id IS NOT NULL AND occurrence_id IS NOT NULL AND "
            "aggregate_id = schedule_id AND outcome = 'accepted' AND "
            "mutation_version IS NOT NULL AND run_transition_sequence IS NULL AND "
            "step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND "
            "action_attempt_number IS NULL AND receipt_id IS NULL AND "
            "approval_request_id IS NULL AND approval_decision_id IS NULL AND "
            "artifact_id IS NULL AND attempt_id IS NULL) OR "
            "(aggregate_type = 'agent_instance_configuration' AND "
            "event_type = 'instance.configuration_changed' AND "
            "aggregate_id LIKE 'inst.%' AND outcome = 'accepted' AND "
            "mutation_version IS NOT NULL AND expected_version IS NOT NULL AND "
            "observed_version IS NOT NULL AND mutation_version >= 2 AND expected_version >= 1 AND "
            "observed_version = expected_version AND "
            "mutation_version = expected_version + 1 AND "
            "run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND "
            "step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND "
            "receipt_id IS NULL AND approval_request_id IS NULL AND "
            "approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND "
            "attempted_command IS NULL AND observed_state IS NULL AND requested_state IS NULL AND "
            "previous_state IS NULL AND new_state IS NULL AND reason_code IS NULL) OR "
            "(aggregate_type = 'manual_ingress_rejection' AND "
            "event_type = 'ingress.schema_rejected' AND run_id IS NULL AND "
            "outcome = 'rejected' AND mutation_version IS NULL AND "
            "reason_code = 'schema_rejected' AND run_transition_sequence IS NULL AND "
            "step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND "
            "action_attempt_number IS NULL AND receipt_id IS NULL AND "
            "approval_request_id IS NULL AND approval_decision_id IS NULL AND "
            "artifact_id IS NULL AND attempt_id IS NULL AND attempted_command IS NULL AND "
            "observed_state IS NULL AND requested_state IS NULL AND previous_state IS NULL AND "
            "new_state IS NULL) OR "
            "(aggregate_type = 'webhook_ingress' AND event_type IN "
            "('webhook.signature_validated','webhook.signature_rejected','webhook.received',"
            "'webhook.duplicate_suppressed','webhook.idempotency_collision',"
            "'webhook.schema_rejected') AND run_id IS NULL AND "
            "aggregate_id LIKE 'webhook-audit-v1:%' AND "
            "run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND "
            "step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND "
            "receipt_id IS NULL AND approval_request_id IS NULL AND "
            "approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND "
            "attempted_command IS NULL AND expected_version IS NULL AND "
            "observed_version IS NULL AND observed_state IS NULL AND "
            "requested_state IS NULL AND previous_state IS NULL AND new_state IS NULL AND "
            "((event_type = 'webhook.signature_rejected' AND actor_source = 'system' AND "
            "auth_method = 'internal' AND outcome = 'rejected' AND mutation_version IS NULL AND "
            "reason_code = 'webhook_authentication_failed') OR "
            "(event_type IN ('webhook.signature_validated','webhook.received',"
            "'webhook.duplicate_suppressed') AND actor_source = 'service' AND "
            "auth_method = 'verified_webhook' AND outcome = 'accepted' AND "
            "mutation_version = 1 AND reason_code IS NULL) OR "
            "(event_type = 'webhook.idempotency_collision' AND actor_source = 'service' AND "
            "auth_method = 'verified_webhook' AND outcome = 'rejected' AND "
            "mutation_version IS NULL AND reason_code = 'idempotency_conflict') OR "
            "(event_type = 'webhook.schema_rejected' AND actor_source = 'service' AND "
            "auth_method = 'verified_webhook' AND outcome = 'rejected' AND "
            "mutation_version IS NULL AND reason_code = 'schema_rejected'))) OR "
            "(aggregate_type = 'manual_ingress' AND event_type IN "
            "('ingress.manual_received','work.duplicate_returned',"
            "'work.idempotency_collision') AND run_id IS NOT NULL AND "
            "run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND "
            "step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND "
            "receipt_id IS NULL AND approval_request_id IS NULL AND "
            "approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND "
            "((event_type IN ('ingress.manual_received','work.duplicate_returned') AND "
            "outcome = 'accepted' AND mutation_version = 1 AND reason_code IS NULL) OR "
            "(event_type = 'work.idempotency_collision' AND outcome = 'rejected' AND "
            "mutation_version IS NULL AND reason_code = 'idempotency_conflict'))) OR "
            "(aggregate_type = 'work_item' AND event_type = 'work.created' AND "
            "run_id IS NOT NULL AND outcome = 'accepted' AND mutation_version = 1 AND "
            "reason_code IS NULL AND run_transition_sequence IS NULL AND "
            "step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND "
            "action_attempt_number IS NULL AND receipt_id IS NULL AND "
            "approval_request_id IS NULL AND approval_decision_id IS NULL AND "
            "artifact_id IS NULL AND attempt_id IS NULL) OR "
            "(aggregate_type = 'run' AND event_type IN "
            "('run.received','run.transitioned','run.plan_recorded') AND "
            "aggregate_id = run_id AND outcome = 'accepted' AND mutation_version IS NOT NULL "
            "AND run_transition_sequence IS NOT NULL AND step_id IS NULL AND action_id IS NULL "
            "AND action_attempt_number IS NULL AND receipt_id IS NULL AND attempt_id IS NULL) OR "
            "(aggregate_type = 'step' AND event_type IN "
            "('step.recorded','step.transitioned') AND step_id IS NOT NULL AND "
            "aggregate_id = step_id AND outcome = 'accepted' AND mutation_version IS NOT NULL "
            "AND step_transition_sequence IS NOT NULL AND action_id IS NULL "
            "AND action_attempt_number IS NULL AND receipt_id IS NULL AND attempt_id IS NULL) OR "
            "(aggregate_type = 'execution_attempt' AND event_type IN "
            "('attempt.reserved','attempt.completed') AND step_id IS NOT NULL AND "
            "attempt_id IS NOT NULL AND aggregate_id = attempt_id AND outcome = 'accepted' AND "
            "mutation_version IS NOT NULL AND run_transition_sequence IS NULL AND "
            "step_transition_sequence IS NULL AND action_id IS NULL AND "
            "action_attempt_number IS NULL AND receipt_id IS NULL AND "
            "approval_request_id IS NULL AND approval_decision_id IS NULL) OR "
            "(aggregate_type = 'artifact' AND event_type = 'artifact.persisted' AND "
            "step_id IS NOT NULL AND attempt_id IS NOT NULL AND artifact_id IS NOT NULL AND "
            "aggregate_id = artifact_id AND outcome = 'accepted' AND mutation_version = 1 AND "
            "run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND "
            "action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND "
            "approval_request_id IS NULL AND approval_decision_id IS NULL) OR "
            "(aggregate_type = 'external_action' AND event_type IN "
            "('action.proposed','action.awaiting_approval','action.approved','action.rejected',"
            "'action.dispatch_reserved','action.cancelled',"
            "'action.dispatch_claimed','action.call_started',"
            "'action.retry_released','action.succeeded','action.failed',"
            "'action.outcome_unknown','action.receipt_reconciled') AND "
            "step_id IS NOT NULL AND action_id IS NOT NULL AND aggregate_id = action_id AND "
            "outcome = 'accepted' AND mutation_version IS NOT NULL AND "
            "run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND "
            "attempt_id IS NULL AND "
            "((event_type IN ('action.approved','action.rejected',"
            "'action.dispatch_reserved') AND "
            "approval_request_id IS NOT NULL AND approval_decision_id IS NOT NULL) OR "
            "(event_type = 'action.cancelled' AND approval_request_id IS NOT NULL) OR "
            "(event_type NOT IN ('action.approved','action.rejected',"
            "'action.dispatch_reserved','action.cancelled') AND "
            "approval_request_id IS NULL AND approval_decision_id IS NULL))) OR "
            "(aggregate_type = 'connector_receipt' AND "
            "event_type = 'connector.receipt_committed' AND step_id IS NOT NULL AND "
            "action_id IS NOT NULL AND action_attempt_number IS NOT NULL AND "
            "receipt_id IS NOT NULL AND aggregate_id = receipt_id AND outcome = 'observed' "
            "AND mutation_version = 1 AND attempt_id IS NULL) OR "
            "(aggregate_type = 'run_attempt' AND event_type = 'run.transition_rejected' "
            "AND aggregate_id = attempt_id AND outcome = 'rejected' AND mutation_version IS NULL "
            "AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL "
            "AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL "
            "AND receipt_id IS NULL AND attempt_id IS NOT NULL AND attempted_command IS NOT NULL "
            "AND expected_version IS NOT NULL AND observed_version IS NOT NULL "
            "AND observed_state IS NOT NULL AND requested_state IS NOT NULL) OR "
            "(aggregate_type = 'runtime_control_denial' AND "
            "event_type = 'runtime.control_denied' AND "
            "aggregate_id LIKE 'runtime-control-denial-v1:%' AND "
            "length(aggregate_id) = 90 AND outcome = 'rejected' AND "
            "mutation_version IS NULL AND run_transition_sequence IS NULL AND "
            "step_transition_sequence IS NULL AND step_id IS NOT NULL AND "
            "action_attempt_number IS NULL AND receipt_id IS NULL AND "
            "approval_request_id IS NULL AND approval_decision_id IS NULL AND "
            "artifact_id IS NULL AND attempt_id IS NULL AND attempted_command IS NULL AND "
            "expected_version IS NULL AND observed_version IS NULL AND "
            "observed_state IS NULL AND requested_state IS NULL AND "
            "previous_state IS NULL AND new_state IS NULL AND reason_code IS NULL) OR "
            "(aggregate_type = 'approval_request' AND event_type IN "
            "('approval.requested','approval.approved','approval.rejected',"
            "'approval.consumed','approval.superseded','approval.expired',"
            "'approval.renewed') AND "
            "approval_request_id IS NOT NULL AND aggregate_id = approval_request_id AND "
            "step_id IS NOT NULL AND action_id IS NOT NULL AND outcome = 'accepted' AND "
            "mutation_version IS NOT NULL AND run_transition_sequence IS NULL AND "
            "step_transition_sequence IS NULL AND action_attempt_number IS NULL AND "
            "receipt_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND "
            "((event_type IN ('approval.approved','approval.rejected',"
            "'approval.consumed') AND "
            "approval_decision_id IS NOT NULL) OR "
            "event_type = 'approval.superseded' OR "
            "(event_type NOT IN ('approval.approved','approval.rejected',"
            "'approval.consumed','approval.superseded') AND "
            "approval_decision_id IS NULL)))",
            name="ck_audit_events_aggregate_links",
        ),
        CheckConstraint(
            "(aggregate_type = 'approval_request' OR "
            "event_type IN ('action.approved','action.rejected','action.dispatch_reserved',"
            "'action.cancelled') OR "
            "approval_request_id IS NULL) AND "
            "(event_type IN ('action.approved','action.rejected',"
            "'action.dispatch_reserved','action.cancelled','approval.approved',"
            "'approval.rejected','approval.consumed','approval.superseded') OR "
            "approval_decision_id IS NULL) AND "
            "(event_type IN ('attempt.completed','artifact.persisted') OR artifact_id IS NULL)",
            name="ck_audit_events_future_links_null",
        ),
        CheckConstraint(
            "(aggregate_type NOT IN ('run','step','run_attempt','approval_request')) OR "
            "reason_code IS NOT NULL",
            name="ck_audit_events_lifecycle_reason",
        ),
        CheckConstraint(
            "(event_type = 'run.received' AND run_sequence = 1 AND "
            "mutation_version = 1 AND run_transition_sequence = 1 AND previous_state IS NULL AND "
            "new_state = 'received') OR "
            "(event_type = 'step.recorded' AND mutation_version = 1 AND "
            "step_transition_sequence = 1 AND previous_state IS NULL AND "
            "new_state = 'pending') OR "
            "(event_type NOT IN ('run.received','step.recorded') AND "
            "aggregate_type NOT IN ('run','step')) OR "
            "(event_type NOT IN ('run.received','step.recorded') AND "
            "aggregate_type IN ('run','step') AND mutation_version > 1 AND "
            "previous_state IS NOT NULL AND new_state IS NOT NULL)",
            name="ck_audit_events_lifecycle_shape",
        ),
        CheckConstraint(
            "(event_type = 'attempt.reserved' AND mutation_version = 1 AND "
            "previous_state IS NULL AND new_state = 'reserved' AND artifact_id IS NULL AND "
            "reason_code IS NULL) OR "
            "(event_type = 'attempt.completed' AND mutation_version = 2 AND "
            "previous_state = 'reserved' AND new_state IN "
            "('succeeded','transient_failure','permanent_failure','cancelled') AND "
            "((new_state = 'succeeded' AND artifact_id IS NOT NULL) OR "
            "(new_state <> 'succeeded' AND artifact_id IS NULL)) AND reason_code IS NULL) OR "
            "aggregate_type <> 'execution_attempt'",
            name="ck_audit_events_attempt_shape",
        ),
        CheckConstraint(
            "(event_type = 'artifact.persisted' AND mutation_version = 1 AND "
            "previous_state IS NULL AND new_state = 'persisted' AND artifact_id IS NOT NULL AND "
            "reason_code IS NULL) OR aggregate_type <> 'artifact'",
            name="ck_audit_events_artifact_shape",
        ),
        CheckConstraint(
            "(event_type = 'action.proposed' AND action_attempt_number IS NULL AND "
            "receipt_id IS NULL AND mutation_version = 1 AND previous_state IS NULL AND "
            "new_state = 'proposed') OR "
            "(event_type IN ('action.dispatch_claimed','action.call_started',"
            "'action.retry_released','action.failed','action.outcome_unknown') AND "
            "action_attempt_number IS NOT NULL AND receipt_id IS NULL) OR "
            "(event_type = 'action.awaiting_approval' "
            "AND action_attempt_number IS NULL AND receipt_id IS NULL AND "
            "mutation_version > 1) OR "
            "(event_type IN ('action.approved','action.rejected') AND "
            "action_attempt_number IS NULL AND receipt_id IS NULL AND "
            "mutation_version >= 3 AND (mutation_version % 2) = 1 AND "
            "previous_state = 'awaiting_approval' AND "
            "((event_type = 'action.approved' AND new_state = 'approved') OR "
            "(event_type = 'action.rejected' AND new_state = 'rejected'))) OR "
            "(event_type = 'action.dispatch_reserved' AND "
            "action_attempt_number IS NULL AND receipt_id IS NULL AND "
            "mutation_version >= 4 AND (mutation_version % 2) = 0 AND "
            "previous_state = 'approved' AND new_state = 'dispatch_reserved') OR "
            "(event_type = 'action.cancelled' AND receipt_id IS NULL AND "
            "new_state = 'cancelled' AND ((action_attempt_number IS NULL AND "
            "mutation_version >= 3 AND previous_state IN ('awaiting_approval','approved')) OR "
            "(action_attempt_number IS NULL AND mutation_version >= 5 AND "
            "previous_state = 'dispatch_reserved') OR "
            "(action_attempt_number IS NOT NULL AND mutation_version >= 6 AND "
            "previous_state = 'dispatching'))) OR "
            "(event_type IN ('action.succeeded','action.receipt_reconciled') AND "
            "action_attempt_number IS NOT NULL AND receipt_id IS NOT NULL) OR "
            "aggregate_type <> 'external_action'",
            name="ck_audit_events_action_shape",
        ),
        CheckConstraint(
            "(event_type = 'approval.requested' AND mutation_version = 1 AND "
            "previous_state IS NULL AND new_state = 'pending') OR "
            "(event_type = 'approval.approved' AND mutation_version = 2 AND "
            "previous_state = 'pending' AND new_state = 'approved') OR "
            "(event_type = 'approval.rejected' AND mutation_version = 2 AND "
            "previous_state = 'pending' AND new_state = 'rejected') OR "
            "(event_type = 'approval.consumed' AND mutation_version = 3 AND "
            "previous_state = 'approved' AND new_state = 'consumed') OR "
            "(event_type = 'approval.superseded' AND mutation_version IN (2,3) AND "
            "previous_state IN ('pending','approved') AND new_state = 'superseded') OR "
            "(event_type = 'approval.expired' AND mutation_version >= 2 AND "
            "previous_state IN ('pending','approved') AND new_state = 'expired') OR "
            "(event_type = 'approval.renewed' AND mutation_version >= 3 AND "
            "previous_state = 'expired' AND new_state = 'expired') OR "
            "aggregate_type <> 'approval_request'",
            name="ck_audit_events_approval_shape",
        ),
        CheckConstraint(
            "(aggregate_type = 'run_attempt') OR "
            "(aggregate_type = 'agent_instance_configuration' AND "
            "attempted_command IS NULL AND expected_version IS NOT NULL AND "
            "observed_version IS NOT NULL AND observed_version = expected_version AND "
            "observed_state IS NULL AND "
            "requested_state IS NULL) OR "
            "(aggregate_type NOT IN ('run_attempt','agent_instance_configuration') AND "
            "attempted_command IS NULL AND expected_version IS NULL AND "
            "observed_version IS NULL AND observed_state IS NULL AND requested_state IS NULL)",
            name="ck_audit_events_attempt_observations",
        ),
        CheckConstraint(
            "aggregate_type NOT IN ('schedule','schedule_occurrence') OR "
            "(attempted_command IS NULL AND expected_version IS NULL AND "
            "observed_version IS NULL AND observed_state IS NULL AND "
            "requested_state IS NULL AND previous_state IS NULL AND new_state IS NULL AND "
            "reason_code IS NULL)",
            name="ck_audit_events_scheduler_shape",
        ),
        CheckConstraint(
            "expected_version IS NULL OR expected_version >= 0",
            name="ck_audit_events_expected_version",
        ),
        CheckConstraint(
            "observed_version IS NULL OR observed_version >= 1",
            name="ck_audit_events_observed_version",
        ),
        CheckConstraint(
            "attempted_command IS NULL OR attempted_command IN "
            "('receive','mark_validated','record_plan','activate_plan',"
            "'release_approved_plan','reject_approval','complete','fail','cancel')",
            name="ck_audit_events_attempt_command",
        ),
        CheckConstraint(
            "observed_state IS NULL OR observed_state IN "
            "('received','validated','planned','awaiting_approval','executing',"
            "'completed','failed','rejected','cancelled')",
            name="ck_audit_events_observed_state",
        ),
        CheckConstraint(
            "requested_state IS NULL OR requested_state IN "
            "('received','validated','planned','awaiting_approval','executing',"
            "'completed','failed','rejected','cancelled')",
            name="ck_audit_events_requested_state",
        ),
        CheckConstraint(
            "attempted_command IS NULL OR "
            "(attempted_command = 'receive' AND requested_state = 'received') OR "
            "(attempted_command = 'mark_validated' AND requested_state = 'validated') OR "
            "(attempted_command = 'record_plan' AND requested_state = 'planned') OR "
            "(attempted_command = 'activate_plan' AND requested_state IN "
            "('awaiting_approval','executing')) OR "
            "(attempted_command = 'release_approved_plan' AND requested_state = 'executing') OR "
            "(attempted_command = 'reject_approval' AND requested_state = 'rejected') OR "
            "(attempted_command = 'complete' AND requested_state = 'completed') OR "
            "(attempted_command = 'fail' AND requested_state = 'failed') OR "
            "(attempted_command = 'cancel' AND requested_state = 'cancelled')",
            name="ck_audit_events_requested_command",
        ),
        CheckConstraint(
            "metadata_classification IN ('public','internal','personal','sensitive')",
            name="ck_audit_events_metadata_classification",
        ),
        CheckConstraint(
            "length(metadata_fingerprint) = 64 AND length(event_fingerprint) = 64",
            name="ck_audit_events_fingerprint_lengths",
        ),
        CheckConstraint(
            "metadata_expires_at > occurred_at",
            name="ck_audit_events_metadata_expiry",
        ),
        CheckConstraint(
            "(run_transition_sequence IS NULL OR (aggregate_type = 'run' AND "
            "run_transition_sequence = mutation_version)) AND "
            "(step_transition_sequence IS NULL OR (aggregate_type = 'step' AND "
            "step_transition_sequence = mutation_version))",
            name="ck_audit_events_transition_links",
        ),
        Index("ix_audit_events_run_time_id", "run_id", "occurred_at", "id"),
        Index(
            "ix_audit_events_schedule_time_id",
            "schedule_id",
            "occurred_at",
            "id",
        ),
        Index("ix_audit_events_global_time", "occurred_at", "global_sequence"),
    )

    global_sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    run_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_id: Mapped[str | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="RESTRICT"),
        nullable=True,
    )
    occurrence_id: Mapped[str | None] = mapped_column(
        ForeignKey("schedule_occurrences.id", ondelete="RESTRICT"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(240), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(240), nullable=False)
    actor_source: Mapped[str] = mapped_column(String(32), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(240), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(240), nullable=False)
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    metadata_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    metadata_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    event_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    step_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    action_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    action_attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    receipt_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    approval_request_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    approval_decision_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    attempted_command: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expected_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mutation_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_transition_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    step_transition_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(240), nullable=True)
