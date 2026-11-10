"""initial_condominium_schema

Revision ID: 0001
Revises: 
Create Date: 2026-11-09 17:34:30.163901

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Type helpers with cross-engine safety
UUID_TYPE = postgresql.UUID(as_uuid=True).with_variant(sa.Uuid(as_uuid=True), "sqlite")
JSON_TYPE = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    """Upgrade schema to baseline condominium architecture."""
    # 1. Master Units Table
    op.create_table(
        "units",
        sa.Column("unit_code", sa.String(length=32), nullable=False, comment="Natural primary key e.g. 'B-504'"),
        sa.Column("tower", sa.String(length=16), nullable=False, comment="Tower identifier e.g. 'A', 'B'"),
        sa.Column("unit_number", sa.Integer(), nullable=False, comment="Unit number e.g. 504"),
        sa.Column("alicuota", sa.Numeric(precision=8, scale=5), nullable=False, comment="Official proration share percentage"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("unit_code"),
        sa.UniqueConstraint("tower", "unit_number", name="uq_units_tower_unit"),
        sa.CheckConstraint("alicuota > 0", name="chk_units_alicuota_positive"),
    )
    op.create_index("idx_units_tower_number", "units", ["tower", "unit_number"])

    # 2. Master Billing Periods Table
    op.create_table(
        "billing_periods",
        sa.Column("period_date", sa.String(length=10), nullable=False, comment="Natural primary key in ISO format 'YYYY-MM-01'"),
        sa.Column("period_year", sa.Integer(), nullable=False, comment="Accounting year e.g. 2026"),
        sa.Column("period_month", sa.Integer(), nullable=False, comment="Accounting month 1-12"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("period_date"),
        sa.CheckConstraint("period_month BETWEEN 1 AND 12", name="chk_periods_month_range"),
    )
    op.create_index("idx_periods_year_month", "billing_periods", ["period_year", "period_month"])

    # 3. Expense Reports (Header)
    op.create_table(
        "expense_reports",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("period_date", sa.String(length=10), nullable=False, comment="Links to billing period YYYY-MM-01"),
        sa.Column("operational_total", sa.BigInteger(), server_default="0", nullable=False, comment="Total Gastos Comunes in CLP"),
        sa.Column("fondos_total", sa.BigInteger(), server_default="0", nullable=False, comment="Reserve funds in CLP"),
        sa.Column("calculated_grand_total", sa.BigInteger(), nullable=False, comment="Final billed total in CLP"),
        sa.Column("source_file_path", sa.Text(), nullable=True, comment="GCS URI or path to PDF report"),
        sa.Column("telemetry_metadata", JSON_TYPE, nullable=True, comment="Extraction telemetry and confidence metadata"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["period_date"], ["billing_periods.period_date"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("period_date", name="uq_expense_reports_period"),
    )
    op.create_index("idx_expense_reports_period", "expense_reports", ["period_date"])

    # 4. Expense Sections (Categories)
    op.create_table(
        "expense_sections",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("report_id", UUID_TYPE, nullable=False),
        sa.Column("category_name", sa.String(length=128), nullable=False),
        sa.Column("expense_type", sa.String(length=32), server_default="operational", nullable=False),
        sa.Column("calculated_subtotal", sa.BigInteger(), nullable=False, comment="Subtotal in CLP"),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["expense_reports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id", "category_name", name="uq_report_category"),
    )
    op.create_index("idx_expense_sections_report", "expense_sections", ["report_id"])

    # 5. Expense Items (Line Items)
    op.create_table(
        "expense_items",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("section_id", UUID_TYPE, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False, comment="Amount in CLP"),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["section_id"], ["expense_sections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_expense_items_section", "expense_items", ["section_id"])
    op.create_index("idx_expense_items_normalized_name", "expense_items", ["normalized_name"])

    # 6. Unit Bills (Header)
    op.create_table(
        "unit_bills",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("unit_code", sa.String(length=32), nullable=False),
        sa.Column("period_date", sa.String(length=10), nullable=False),
        sa.Column("last_payment_date", sa.Date(), nullable=True),
        sa.Column("last_payment_amount", sa.BigInteger(), nullable=True),
        sa.Column("calculated_dept_subtotal", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("calculated_consumo_total", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("calculated_unit_subtotal", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("calculated_total_month", sa.BigInteger(), nullable=False),
        sa.Column("source_file_path", sa.Text(), nullable=True),
        sa.Column("telemetry_metadata", JSON_TYPE, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["period_date"], ["billing_periods.period_date"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["unit_code"], ["units.unit_code"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("unit_code", "period_date", name="uq_unit_bill_period"),
    )
    op.create_index("idx_unit_bills_unit", "unit_bills", ["unit_code"])
    op.create_index("idx_unit_bills_period", "unit_bills", ["period_date"])
    op.create_index("idx_unit_bills_period_unit", "unit_bills", ["period_date", "unit_code"])

    # 7. Unit Bill Concepts
    op.create_table(
        "unit_bill_concepts",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("unit_bill_id", UUID_TYPE, nullable=False),
        sa.Column("concept_name", sa.String(length=128), nullable=False),
        sa.Column("normalized_name", sa.String(length=128), nullable=False),
        sa.Column("detail", sa.String(length=64), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["unit_bill_id"], ["unit_bills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_unit_bill_concepts_bill", "unit_bill_concepts", ["unit_bill_id"])
    op.create_index("idx_unit_bill_concepts_normalized", "unit_bill_concepts", ["normalized_name"])

    # 8. Unit Bill Consumptions
    op.create_table(
        "unit_bill_consumptions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("unit_bill_id", UUID_TYPE, nullable=False),
        sa.Column("item_name", sa.String(length=128), nullable=False),
        sa.Column("normalized_name", sa.String(length=128), nullable=False),
        sa.Column("previous_reading", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("current_reading", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("consumption", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("unit_rate", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("total_amount", sa.BigInteger(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["unit_bill_id"], ["unit_bills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_unit_bill_consumptions_bill", "unit_bill_consumptions", ["unit_bill_id"])

    # 9. Unit Bill Charges
    op.create_table(
        "unit_bill_charges",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("unit_bill_id", UUID_TYPE, nullable=False),
        sa.Column("charge_name", sa.String(length=128), nullable=False),
        sa.Column("normalized_name", sa.String(length=128), nullable=False),
        sa.Column("charge_type", sa.String(length=32), server_default="fixed_charge", nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["unit_bill_id"], ["unit_bills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_unit_bill_charges_bill", "unit_bill_charges", ["unit_bill_id"])


def downgrade() -> None:
    """Downgrade schema dropping all tables in dependency reverse order."""
    op.drop_table("unit_bill_charges")
    op.drop_table("unit_bill_consumptions")
    op.drop_table("unit_bill_concepts")
    op.drop_table("unit_bills")
    op.drop_table("expense_items")
    op.drop_table("expense_sections")
    op.drop_table("expense_reports")
    op.drop_table("billing_periods")
    op.drop_table("units")
