"""SQLAlchemy 2.0 ORM models for Chilean condominium financial management on PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import uuid_utils
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database import Base


def generate_uuid7() -> uuid.UUID:
    """Generate a time-ordered UUIDv7 instance."""
    return uuid.UUID(str(uuid_utils.uuid7()))


# Cross-engine type wrappers: PostgreSQL native types with fallbacks
UUID_TYPE = UUID(as_uuid=True).with_variant(Uuid(as_uuid=True), "sqlite")
JSON_TYPE = JSONB().with_variant(JSON, "sqlite")


# ============================================================================
# Master / Dimension Models
# ============================================================================


class UnitModel(Base):
    """Master record for the 420 condominium apartments ('Unidades')."""

    __tablename__ = "units"

    unit_code: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        comment="Natural primary key e.g. 'B-504'",
    )
    tower: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Tower identifier e.g. 'A', 'B'",
    )
    unit_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Unit number e.g. 504",
    )
    alicuota: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 5),
        nullable=True,
        default=None,
        comment="Official proration share percentage (populated on first bill extraction)",
    )
    kastor_id: Mapped[int | None] = mapped_column(
        Integer,
        unique=True,
        nullable=True,
        index=True,
        comment="Kastor unit ID (e.g. 336 for Depto 504 Torre B)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    bills: Mapped[list[UnitBillModel]] = relationship(
        back_populates="unit",
        passive_deletes="all",
    )

    __table_args__ = (
        UniqueConstraint("tower", "unit_number", name="uq_units_tower_unit"),
        CheckConstraint("alicuota IS NULL OR alicuota > 0", name="chk_units_alicuota_positive"),
        Index("idx_units_tower_number", "tower", "unit_number"),
    )


class BillingPeriodModel(Base):
    """Master record for monthly accounting cycles ('Período de Facturación')."""

    __tablename__ = "billing_periods"

    period_date: Mapped[str] = mapped_column(
        String(10),
        primary_key=True,
        comment="Natural primary key in ISO format 'YYYY-MM-01'",
    )
    period_year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Accounting year e.g. 2026",
    )
    period_month: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Accounting month 1-12",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    expense_reports: Mapped[list[ExpenseReportModel]] = relationship(
        back_populates="billing_period",
        passive_deletes="all",
    )
    unit_bills: Mapped[list[UnitBillModel]] = relationship(
        back_populates="billing_period",
        passive_deletes="all",
    )

    __table_args__ = (
        CheckConstraint("period_month BETWEEN 1 AND 12", name="chk_periods_month_range"),
        Index("idx_periods_year_month", "period_year", "period_month"),
    )


# ============================================================================
# Building Expense Relational Hierarchy (3-Tier)
# ============================================================================


class ExpenseReportModel(Base):
    """Level 1: Monthly building expense report header ('Informe Gastos Comunes')."""

    __tablename__ = "expense_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        primary_key=True,
        default=generate_uuid7,
    )
    period_date: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("billing_periods.period_date", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
        index=True,
        comment="Links to billing period YYYY-MM-01",
    )
    operational_total: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment="Total Gastos Comunes Ordinarios in CLP",
    )
    fondos_total: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment="Reserve funds and surcharges total in CLP",
    )
    calculated_grand_total: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Final billed total (operational + fondos) in CLP",
    )
    source_file_path: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="GCS bucket URI or file path to original PDF report",
    )
    telemetry_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON_TYPE,
        nullable=True,
        comment="Extraction telemetry and confidence metadata",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    billing_period: Mapped[BillingPeriodModel] = relationship(
        back_populates="expense_reports",
    )
    sections: Mapped[list[ExpenseSectionModel]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ExpenseSectionModel.display_order",
    )


class ExpenseSectionModel(Base):
    """Level 2: Categorical expense section grouping line items (e.g. Mantenimiento)."""

    __tablename__ = "expense_sections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        primary_key=True,
        default=generate_uuid7,
    )
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("expense_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="e.g. 'Gastos de Administracion', 'Gastos de Mantenimiento'",
    )
    expense_type: Mapped[str] = mapped_column(
        String(32),
        default="operational",
        nullable=False,
        comment="'operational' or 'fondos'",
    )
    calculated_subtotal: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Category subtotal in CLP",
    )
    display_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Relationships
    report: Mapped[ExpenseReportModel] = relationship(back_populates="sections")
    items: Mapped[list[ExpenseItemModel]] = relationship(
        back_populates="section",
        cascade="all, delete-orphan",
        order_by="ExpenseItemModel.display_order",
    )

    __table_args__ = (
        UniqueConstraint("report_id", "category_name", name="uq_report_category"),
    )


class ExpenseItemModel(Base):
    """Level 3: Individual vendor line item or invoice within a section."""

    __tablename__ = "expense_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        primary_key=True,
        default=generate_uuid7,
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("expense_sections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Full vendor or expense item name",
    )
    normalized_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Lowercase trimmed name for cross-month trend queries",
    )
    amount: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Amount in CLP (may be negative for credit notes)",
    )
    display_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Relationships
    section: Mapped[ExpenseSectionModel] = relationship(back_populates="items")


# ============================================================================
# Unit Billing Statement ("Nota de Cobro") Relational Hierarchy
# ============================================================================


class UnitBillModel(Base):
    """Monthly billing statement header for an apartment unit ('Nota de Cobro')."""

    __tablename__ = "unit_bills"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        primary_key=True,
        default=generate_uuid7,
    )
    unit_code: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("units.unit_code", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="Foreign key linking to master units e.g. 'B-504'",
    )
    period_date: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("billing_periods.period_date", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="Foreign key linking to billing_periods YYYY-MM-01",
    )
    last_payment_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="Date of last payment received",
    )
    last_payment_amount: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Last payment amount in CLP",
    )
    calculated_dept_subtotal: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment="Prorated building expenses share in CLP",
    )
    calculated_consumo_total: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment="Sum of metered consumptions (water/gas) in CLP",
    )
    calculated_unit_subtotal: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment="Sum of unit-specific charges/fines in CLP",
    )
    calculated_total_month: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Total amount payable for the month in CLP",
    )
    source_file_path: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="GCS bucket URI or file path to individual PDF bill",
    )
    telemetry_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON_TYPE,
        nullable=True,
        comment="Extraction telemetry and confidence metadata",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    unit: Mapped[UnitModel] = relationship(back_populates="bills")
    billing_period: Mapped[BillingPeriodModel] = relationship(back_populates="unit_bills")
    concepts: Mapped[list[UnitBillConceptModel]] = relationship(
        back_populates="unit_bill",
        cascade="all, delete-orphan",
        order_by="UnitBillConceptModel.display_order",
    )
    consumptions: Mapped[list[UnitBillConsumptionModel]] = relationship(
        back_populates="unit_bill",
        cascade="all, delete-orphan",
        order_by="UnitBillConsumptionModel.display_order",
    )
    unit_charges: Mapped[list[UnitBillChargeModel]] = relationship(
        back_populates="unit_bill",
        cascade="all, delete-orphan",
        order_by="UnitBillChargeModel.display_order",
    )

    __table_args__ = (
        UniqueConstraint("unit_code", "period_date", name="uq_unit_bill_period"),
        Index("idx_unit_bills_period_unit", "period_date", "unit_code"),
    )


class UnitBillConceptModel(Base):
    """Prorated concepts applied to the flat based on alícuota ('Tabla Conceptos')."""

    __tablename__ = "unit_bill_concepts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        primary_key=True,
        default=generate_uuid7,
    )
    unit_bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("unit_bills.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    concept_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="e.g. 'Gasto Comun Ordinario', 'Fondo de Reserva'",
    )
    normalized_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment="Lowercase accent-stripped name",
    )
    detail: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Detail or percentage string e.g. '0,16736 %'",
    )
    amount: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Payable amount in CLP",
    )
    display_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Relationships
    unit_bill: Mapped[UnitBillModel] = relationship(back_populates="concepts")


class UnitBillConsumptionModel(Base):
    """Metered utility consumption readings for a flat ('Tabla Consumos Generales')."""

    __tablename__ = "unit_bill_consumptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        primary_key=True,
        default=generate_uuid7,
    )
    unit_bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("unit_bills.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="e.g. 'Agua Caliente'",
    )
    normalized_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment="Lowercase accent-stripped name",
    )
    previous_reading: Mapped[Decimal] = mapped_column(
        Numeric(10, 3),
        nullable=False,
        comment="Meter previous reading",
    )
    current_reading: Mapped[Decimal] = mapped_column(
        Numeric(10, 3),
        nullable=False,
        comment="Meter current reading",
    )
    consumption: Mapped[Decimal] = mapped_column(
        Numeric(10, 3),
        nullable=False,
        comment="Units consumed (m3 / kWh)",
    )
    unit_rate: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        comment="Rate per unit in CLP",
    )
    total_amount: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Consumption total in CLP",
    )
    display_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Relationships
    unit_bill: Mapped[UnitBillModel] = relationship(back_populates="consumptions")


class UnitBillChargeModel(Base):
    """Flat-specific charges, fines, or fixed fees ('Tabla Gastos por Unidad')."""

    __tablename__ = "unit_bill_charges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        primary_key=True,
        default=generate_uuid7,
    )
    unit_bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("unit_bills.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    charge_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="e.g. 'Multa ruidos molestos', 'Uso Quincho'",
    )
    normalized_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment="Lowercase accent-stripped name",
    )
    charge_type: Mapped[str] = mapped_column(
        String(32),
        default="fixed_charge",
        nullable=False,
        comment="'recargo', 'fine', or 'fixed_charge'",
    )
    amount: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Charge amount in CLP",
    )
    display_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Relationships
    unit_bill: Mapped[UnitBillModel] = relationship(back_populates="unit_charges")
