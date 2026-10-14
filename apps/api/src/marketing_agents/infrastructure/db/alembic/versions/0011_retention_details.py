"""Frozen schema-only retention tables and exact count-only maintenance audit shape.

Revision ID: 0011
Revises: 0010

Legacy expiry registration is bounded maintenance work, never migration DML.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_CONSTRAINTS = {
    "ck_audit_events_aggregate_links": "(aggregate_type = 'campaign_brief' AND event_type = 'campaign_brief.created' AND aggregate_id LIKE 'brief.%' AND length(aggregate_id) = 70 AND actor_source = 'user' AND outcome = 'accepted' AND mutation_version = 1 AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND attempted_command IS NULL AND expected_version IS NULL AND observed_version IS NULL AND observed_state IS NULL AND requested_state IS NULL AND previous_state IS NULL AND new_state IS NULL AND reason_code IS NULL) OR (aggregate_type = 'schedule_occurrence' AND event_type IN ('schedule.occurrence_created','schedule.misfire_skipped','schedule.misfire_run_once') AND schedule_id IS NOT NULL AND occurrence_id IS NOT NULL AND aggregate_id = occurrence_id AND outcome = 'accepted' AND mutation_version = 1 AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL) OR (aggregate_type = 'schedule' AND event_type = 'schedule.next_occurrence_persisted' AND schedule_id IS NOT NULL AND occurrence_id IS NOT NULL AND aggregate_id = schedule_id AND outcome = 'accepted' AND mutation_version IS NOT NULL AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL) OR (aggregate_type = 'agent_instance_configuration' AND event_type = 'instance.configuration_changed' AND aggregate_id LIKE 'inst.%' AND outcome = 'accepted' AND mutation_version IS NOT NULL AND expected_version IS NOT NULL AND observed_version IS NOT NULL AND mutation_version >= 2 AND expected_version >= 1 AND observed_version = expected_version AND mutation_version = expected_version + 1 AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND attempted_command IS NULL AND observed_state IS NULL AND requested_state IS NULL AND previous_state IS NULL AND new_state IS NULL AND reason_code IS NULL) OR (aggregate_type = 'manual_ingress_rejection' AND event_type = 'ingress.schema_rejected' AND run_id IS NULL AND outcome = 'rejected' AND mutation_version IS NULL AND reason_code = 'schema_rejected' AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND attempted_command IS NULL AND observed_state IS NULL AND requested_state IS NULL AND previous_state IS NULL AND new_state IS NULL) OR (aggregate_type = 'webhook_ingress' AND event_type IN ('ingress.rate_limited','webhook.signature_validated','webhook.signature_rejected','webhook.received','webhook.duplicate_suppressed','webhook.idempotency_collision','webhook.schema_rejected') AND run_id IS NULL AND aggregate_id LIKE 'webhook-audit-v1:%' AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND attempted_command IS NULL AND expected_version IS NULL AND observed_version IS NULL AND observed_state IS NULL AND requested_state IS NULL AND previous_state IS NULL AND new_state IS NULL AND ((event_type = 'webhook.signature_rejected' AND actor_source = 'system' AND auth_method = 'internal' AND outcome = 'rejected' AND mutation_version IS NULL AND reason_code = 'webhook_authentication_failed') OR (event_type = 'ingress.rate_limited' AND actor_source = 'service' AND auth_method = 'verified_webhook' AND outcome = 'rejected' AND mutation_version IS NULL AND reason_code = 'rate_limit_exhausted') OR (event_type IN ('webhook.signature_validated','webhook.received','webhook.duplicate_suppressed') AND actor_source = 'service' AND auth_method = 'verified_webhook' AND outcome = 'accepted' AND mutation_version = 1 AND reason_code IS NULL) OR (event_type = 'webhook.idempotency_collision' AND actor_source = 'service' AND auth_method = 'verified_webhook' AND outcome = 'rejected' AND mutation_version IS NULL AND reason_code = 'idempotency_conflict') OR (event_type = 'webhook.schema_rejected' AND actor_source = 'service' AND auth_method = 'verified_webhook' AND outcome = 'rejected' AND mutation_version IS NULL AND reason_code = 'schema_rejected'))) OR (aggregate_type = 'manual_ingress' AND event_type IN ('ingress.manual_received','work.duplicate_returned','work.idempotency_collision') AND run_id IS NOT NULL AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND ((event_type IN ('ingress.manual_received','work.duplicate_returned') AND outcome = 'accepted' AND mutation_version = 1 AND reason_code IS NULL) OR (event_type = 'work.idempotency_collision' AND outcome = 'rejected' AND mutation_version IS NULL AND reason_code = 'idempotency_conflict'))) OR (aggregate_type = 'work_item' AND event_type = 'work.created' AND run_id IS NOT NULL AND outcome = 'accepted' AND mutation_version = 1 AND reason_code IS NULL AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL) OR (aggregate_type = 'run' AND event_type IN ('run.received','run.transitioned','run.plan_recorded') AND aggregate_id = run_id AND outcome = 'accepted' AND mutation_version IS NOT NULL AND run_transition_sequence IS NOT NULL AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND attempt_id IS NULL) OR (aggregate_type = 'step' AND event_type IN ('step.recorded','step.transitioned') AND step_id IS NOT NULL AND aggregate_id = step_id AND outcome = 'accepted' AND mutation_version IS NOT NULL AND step_transition_sequence IS NOT NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND attempt_id IS NULL) OR (aggregate_type = 'execution_attempt' AND event_type IN ('attempt.reserved','attempt.completed') AND step_id IS NOT NULL AND attempt_id IS NOT NULL AND aggregate_id = attempt_id AND outcome = 'accepted' AND mutation_version IS NOT NULL AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL) OR (aggregate_type = 'artifact' AND ((event_type = 'artifact.persisted' AND attempt_id IS NOT NULL) OR (event_type IN ('artifact.transformed','artifact.previewed') AND attempt_id IS NULL)) AND step_id IS NOT NULL AND artifact_id IS NOT NULL AND aggregate_id = artifact_id AND outcome = 'accepted' AND mutation_version = 1 AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL) OR (aggregate_type = 'external_action' AND event_type IN ('action.proposed','action.awaiting_approval','action.approved','action.rejected','action.dispatch_reserved','action.cancelled','action.dispatch_claimed','action.call_started','action.retry_released','action.succeeded','action.failed','action.outcome_unknown','action.receipt_reconciled') AND step_id IS NOT NULL AND action_id IS NOT NULL AND aggregate_id = action_id AND outcome = 'accepted' AND mutation_version IS NOT NULL AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND attempt_id IS NULL AND ((event_type IN ('action.approved','action.rejected','action.dispatch_reserved') AND approval_request_id IS NOT NULL AND approval_decision_id IS NOT NULL) OR (event_type = 'action.cancelled' AND approval_request_id IS NOT NULL) OR (event_type NOT IN ('action.approved','action.rejected','action.dispatch_reserved','action.cancelled') AND approval_request_id IS NULL AND approval_decision_id IS NULL))) OR (aggregate_type = 'connector_receipt' AND event_type = 'connector.receipt_committed' AND step_id IS NOT NULL AND action_id IS NOT NULL AND action_attempt_number IS NOT NULL AND receipt_id IS NOT NULL AND aggregate_id = receipt_id AND outcome = 'observed' AND mutation_version = 1 AND attempt_id IS NULL) OR (aggregate_type = 'run_attempt' AND event_type = 'run.transition_rejected' AND aggregate_id = attempt_id AND outcome = 'rejected' AND mutation_version IS NULL AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND attempt_id IS NOT NULL AND attempted_command IS NOT NULL AND expected_version IS NOT NULL AND observed_version IS NOT NULL AND observed_state IS NOT NULL AND requested_state IS NOT NULL) OR (aggregate_type = 'runtime_control_denial' AND event_type = 'runtime.control_denied' AND aggregate_id LIKE 'runtime-control-denial-v1:%' AND length(aggregate_id) = 90 AND outcome = 'rejected' AND mutation_version IS NULL AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND step_id IS NOT NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND attempted_command IS NULL AND expected_version IS NULL AND observed_version IS NULL AND observed_state IS NULL AND requested_state IS NULL AND previous_state IS NULL AND new_state IS NULL AND reason_code IS NULL) OR (aggregate_type = 'approval_request' AND event_type IN ('approval.requested','approval.approved','approval.rejected','approval.consumed','approval.superseded','approval.expired','approval.renewed') AND approval_request_id IS NOT NULL AND aggregate_id = approval_request_id AND step_id IS NOT NULL AND action_id IS NOT NULL AND outcome = 'accepted' AND mutation_version IS NOT NULL AND run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND action_attempt_number IS NULL AND receipt_id IS NULL AND artifact_id IS NULL AND attempt_id IS NULL AND ((event_type IN ('approval.approved','approval.rejected','approval.consumed') AND approval_decision_id IS NOT NULL) OR event_type = 'approval.superseded' OR (event_type NOT IN ('approval.approved','approval.rejected','approval.consumed','approval.superseded') AND approval_decision_id IS NULL)))",
    "ck_audit_events_timeline_scope": "((aggregate_type IN ('schedule','schedule_occurrence') AND run_id IS NULL AND run_sequence IS NULL AND schedule_id IS NOT NULL AND occurrence_id IS NOT NULL) OR (aggregate_type = 'agent_instance_configuration' AND run_id IS NULL AND run_sequence IS NULL AND schedule_id IS NULL AND occurrence_id IS NULL) OR (aggregate_type = 'manual_ingress_rejection' AND run_id IS NULL AND run_sequence IS NULL AND schedule_id IS NULL AND occurrence_id IS NULL) OR (aggregate_type = 'webhook_ingress' AND run_id IS NULL AND run_sequence IS NULL AND schedule_id IS NULL AND occurrence_id IS NULL) OR (aggregate_type = 'campaign_brief' AND run_id IS NULL AND run_sequence IS NULL AND schedule_id IS NULL AND occurrence_id IS NULL) OR (aggregate_type NOT IN ('schedule','schedule_occurrence','agent_instance_configuration','manual_ingress_rejection','webhook_ingress','campaign_brief') AND run_id IS NOT NULL AND run_sequence IS NOT NULL AND schedule_id IS NULL AND occurrence_id IS NULL))",
}
_RETENTION_SHAPE = (
    "(aggregate_type = 'maintenance_run' AND event_type = 'retention.completed' AND "
    "aggregate_id LIKE 'maintenance.%' AND actor_source = 'worker' AND auth_method = 'internal' AND "
    "outcome = 'accepted' AND mutation_version = 1 AND "
    "run_transition_sequence IS NULL AND step_transition_sequence IS NULL AND "
    "step_id IS NULL AND action_id IS NULL AND action_attempt_number IS NULL AND "
    "receipt_id IS NULL AND approval_request_id IS NULL AND approval_decision_id IS NULL AND "
    "artifact_id IS NULL AND attempt_id IS NULL AND attempted_command IS NULL AND "
    "expected_version IS NULL AND observed_version IS NULL AND observed_state IS NULL AND "
    "requested_state IS NULL AND previous_state IS NULL AND new_state IS NULL AND reason_code IS NULL) OR "
)


def upgrade() -> None:
    op.create_table(
        "retention_details",
        sa.Column("record_kind", sa.String(40), nullable=False),
        sa.Column("record_id", sa.String(240), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("run_id", sa.String(240), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skeleton", sa.JSON(), nullable=False),
        sa.Column("integrity_digest", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "category IN ('admitted_payload','external_action_payload','approval_detail','artifact_detail','connector_receipt_detail','audit_metadata')",
            name="ck_retention_details_category",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_retention_details_expiry"),
        sa.CheckConstraint(
            "(purged_at IS NULL AND integrity_digest IS NULL) OR (purged_at >= expires_at AND length(integrity_digest) = 64)",
            name="ck_retention_details_purge",
        ),
        sa.PrimaryKeyConstraint("record_kind", "record_id", name="pk_retention_details"),
    )
    op.create_index(
        "ix_retention_details_due",
        "retention_details",
        ["purged_at", "expires_at", "record_kind", "record_id"],
    )
    op.add_column(
        "maintenance_runs", sa.Column("scope_json", sa.Text(), server_default="{}", nullable=False)
    )
    constraints = dict(_CONSTRAINTS)
    constraints["ck_audit_events_aggregate_links"] = (
        _RETENTION_SHAPE + constraints["ck_audit_events_aggregate_links"]
    )
    constraints["ck_audit_events_timeline_scope"] = (
        constraints["ck_audit_events_timeline_scope"]
        .replace(
            "aggregate_type = 'campaign_brief' AND",
            "aggregate_type IN ('campaign_brief','maintenance_run') AND",
        )
        .replace(
            "'webhook_ingress','campaign_brief')",
            "'webhook_ingress','campaign_brief','maintenance_run')",
        )
    )
    with op.batch_alter_table("audit_events") as batch:
        for name, condition in constraints.items():
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, condition)


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are unsupported; restore a verified backup instead.")
