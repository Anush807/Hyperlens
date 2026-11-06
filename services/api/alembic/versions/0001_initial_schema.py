"""Initial HVOps schema: users, sources, inventory, ML results, assistant, audit.

Revision ID: 0001
Revises:
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = pg.ENUM("viewer", "operator", "admin", name="role_enum", create_type=False)
SOURCE_TYPE = pg.ENUM("vsphere", "nutanix", name="source_type_enum", create_type=False)
SOURCE_STATUS = pg.ENUM("ok", "failed", "pending", name="source_status_enum", create_type=False)
POWER_STATE = pg.ENUM(
    "on", "off", "suspended", "unknown", name="power_state_enum", create_type=False
)
ENTITY_TYPE = pg.ENUM(
    "vm", "host", "cluster", "datastore", name="entity_type_enum", create_type=False
)
ANOMALY_STATUS = pg.ENUM(
    "open", "acknowledged", "resolved", name="anomaly_status_enum", create_type=False
)
FORECAST_MODEL = pg.ENUM("lstm", "naive", name="forecast_model_enum", create_type=False)
REC_KIND = pg.ENUM(
    "downsize_cpu",
    "downsize_mem",
    "upsize_cpu",
    "upsize_mem",
    "idle",
    name="recommendation_kind_enum",
    create_type=False,
)
REC_STATUS = pg.ENUM("open", "dismissed", name="recommendation_status_enum", create_type=False)
MESSAGE_ROLE = pg.ENUM("user", "assistant", "tool", name="message_role_enum", create_type=False)

_ENUMS = [
    ROLE,
    SOURCE_TYPE,
    SOURCE_STATUS,
    POWER_STATE,
    ENTITY_TYPE,
    ANOMALY_STATUS,
    FORECAST_MODEL,
    REC_KIND,
    REC_STATUS,
    MESSAGE_ROLE,
]

_TS = [
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
]


def _id() -> sa.Column:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    for enum in _ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "users",
        _id(),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", ROLE, nullable=False, server_default="viewer"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_TS,
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "refresh_tokens",
        _id(),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_TS,
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"])

    op.create_table(
        "hypervisor_sources",
        _id(),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("type", SOURCE_TYPE, nullable=False),
        sa.Column("endpoint", sa.String(512), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("verify_ssl", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_inventory_at", sa.DateTime(timezone=True)),
        sa.Column("last_status", SOURCE_STATUS, nullable=False, server_default="pending"),
        sa.Column("last_error", sa.Text()),
        *_TS,
    )

    op.create_table(
        "clusters",
        _id(),
        sa.Column(
            "source_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("hypervisor_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        *_TS,
        sa.UniqueConstraint("source_id", "external_id", name="uq_clusters_source_ext"),
    )
    op.create_index("ix_clusters_source_id", "clusters", ["source_id"])

    op.create_table(
        "hosts",
        _id(),
        sa.Column(
            "source_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("hypervisor_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "cluster_id", pg.UUID(as_uuid=True), sa.ForeignKey("clusters.id", ondelete="SET NULL")
        ),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("cpu_cores", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cpu_mhz_per_core", sa.Integer()),
        sa.Column("memory_mb", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("power_state", POWER_STATE, nullable=False, server_default="unknown"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        *_TS,
        sa.UniqueConstraint("source_id", "external_id", name="uq_hosts_source_ext"),
    )
    op.create_index("ix_hosts_source_id", "hosts", ["source_id"])
    op.create_index("ix_hosts_cluster_id", "hosts", ["cluster_id"])
    op.create_index("ix_hosts_name", "hosts", ["name"])

    op.create_table(
        "vms",
        _id(),
        sa.Column(
            "source_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("hypervisor_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("host_id", pg.UUID(as_uuid=True), sa.ForeignKey("hosts.id", ondelete="SET NULL")),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("vcpus", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("memory_mb", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provisioned_storage_gb", sa.Float(), nullable=False, server_default="0"),
        sa.Column("guest_os", sa.String(255)),
        sa.Column("power_state", POWER_STATE, nullable=False, server_default="unknown"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_TS,
        sa.UniqueConstraint("source_id", "external_id", name="uq_vms_source_ext"),
    )
    op.create_index("ix_vms_source_id", "vms", ["source_id"])
    op.create_index("ix_vms_host_id", "vms", ["host_id"])
    op.create_index("ix_vms_name", "vms", ["name"])
    op.create_index("ix_vms_name_lower", "vms", ["name"])
    op.create_index("ix_vms_is_deleted", "vms", ["is_deleted"])

    op.create_table(
        "datastores",
        _id(),
        sa.Column(
            "source_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("hypervisor_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("capacity_gb", sa.Float(), nullable=False, server_default="0"),
        sa.Column("free_gb", sa.Float(), nullable=False, server_default="0"),
        *_TS,
        sa.UniqueConstraint("source_id", "external_id", name="uq_datastores_source_ext"),
    )
    op.create_index("ix_datastores_source_id", "datastores", ["source_id"])
    op.create_index("ix_datastores_name", "datastores", ["name"])

    op.create_table(
        "anomalies",
        _id(),
        sa.Column("entity_type", ENTITY_TYPE, nullable=False),
        sa.Column("entity_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("features", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", ANOMALY_STATUS, nullable=False, server_default="open"),
        sa.Column("summary", sa.Text()),
        *_TS,
    )
    op.create_index(
        "ix_anomalies_entity_window", "anomalies", ["entity_type", "entity_id", "window_start"]
    )
    op.create_index("ix_anomalies_status_detected", "anomalies", ["status", "detected_at"])

    op.create_table(
        "forecasts",
        _id(),
        sa.Column("entity_type", ENTITY_TYPE, nullable=False),
        sa.Column("entity_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("model", FORECAST_MODEL, nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("points", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("days_to_threshold", sa.Integer()),
        *_TS,
    )
    op.create_index(
        "ix_forecasts_lookup",
        "forecasts",
        ["entity_type", "entity_id", "metric", "model", "generated_at"],
    )

    op.create_table(
        "recommendations",
        _id(),
        sa.Column(
            "vm_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("vms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", REC_KIND, nullable=False),
        sa.Column("current_value", sa.Float(), nullable=False),
        sa.Column("suggested_value", sa.Float(), nullable=False),
        sa.Column("evidence", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", REC_STATUS, nullable=False, server_default="open"),
        *_TS,
    )
    op.create_index("ix_recommendations_vm_id", "recommendations", ["vm_id"])
    op.create_index(
        "ix_recommendations_vm_kind_status", "recommendations", ["vm_id", "kind", "status"]
    )

    op.create_table(
        "assistant_conversations",
        _id(),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False, server_default="New conversation"),
        *_TS,
    )
    op.create_index("ix_assistant_conversations_user_id", "assistant_conversations", ["user_id"])

    op.create_table(
        "assistant_messages",
        _id(),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", MESSAGE_ROLE, nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_name", sa.String(64)),
        sa.Column("tool_args", pg.JSONB()),
        *_TS,
    )
    op.create_index(
        "ix_assistant_messages_conversation_id", "assistant_messages", ["conversation_id"]
    )

    op.create_table(
        "audit_log",
        _id(),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("entity_type", sa.String(64)),
        sa.Column("entity_id", sa.String(64)),
        sa.Column("details", pg.JSONB(), nullable=False, server_default="{}"),
        *_TS,
    )
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])


def downgrade() -> None:
    for table in (
        "audit_log",
        "assistant_messages",
        "assistant_conversations",
        "recommendations",
        "forecasts",
        "anomalies",
        "datastores",
        "vms",
        "hosts",
        "clusters",
        "hypervisor_sources",
        "refresh_tokens",
        "users",
    ):
        op.drop_table(table)
    bind = op.get_bind()
    for enum in _ENUMS:
        enum.drop(bind, checkfirst=True)
