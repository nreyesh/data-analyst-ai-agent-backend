"""Add security_audit_logs table for credential validation and 3-strike 24h blocks

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11 18:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "security_audit_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_uid", sa.String(length=128), nullable=False),
        sa.Column("user_email", sa.String(length=255), nullable=False),
        sa.Column("violation_type", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("strike_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_security_audit_logs_user_uid"), "security_audit_logs", ["user_uid"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_user_email"), "security_audit_logs", ["user_email"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_violation_type"), "security_audit_logs", ["violation_type"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_is_blocked"), "security_audit_logs", ["is_blocked"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_blocked_until"), "security_audit_logs", ["blocked_until"], unique=False)
    op.create_index(op.f("ix_security_audit_logs_created_at"), "security_audit_logs", ["created_at"], unique=False)
    op.create_index(
        "idx_sec_audit_uid_blocked",
        "security_audit_logs",
        ["user_uid", "is_blocked", "blocked_until"],
        unique=False,
    )
    op.create_index(
        "idx_sec_audit_email_blocked",
        "security_audit_logs",
        ["user_email", "is_blocked", "blocked_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_sec_audit_email_blocked", table_name="security_audit_logs")
    op.drop_index("idx_sec_audit_uid_blocked", table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_created_at"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_blocked_until"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_is_blocked"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_violation_type"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_user_email"), table_name="security_audit_logs")
    op.drop_index(op.f("ix_security_audit_logs_user_uid"), table_name="security_audit_logs")
    op.drop_table("security_audit_logs")
