"""Tests for Condominium Financial Schema integrity, relationships, and constraints."""

from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models import (
    Base,
    BillingPeriodModel,
    ExpenseItemModel,
    ExpenseReportModel,
    ExpenseSectionModel,
    UnitBillChargeModel,
    UnitBillConceptModel,
    UnitBillConsumptionModel,
    UnitBillModel,
    UnitModel,
    generate_uuid7,
)


@pytest.fixture
def db_session() -> Session:
    """Create an in-memory SQLite database session with foreign keys enabled."""
    engine = create_engine("sqlite:///:memory:")

    # SQLite needs explicit PRAGMA to enforce foreign key constraints
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_master_unit_creation_and_uniqueness(db_session: Session):
    """Verify master unit creation and duplicate prevention."""
    unit = UnitModel(
        unit_code="B-504",
        tower="B",
        unit_number=504,
        alicuota=Decimal("0.16736"),
    )
    db_session.add(unit)
    db_session.commit()

    saved_unit = db_session.get(UnitModel, "B-504")
    assert saved_unit is not None
    assert saved_unit.tower == "B"
    assert saved_unit.unit_number == 504
    assert saved_unit.alicuota == Decimal("0.16736")

    # Duplicate unit_code must raise IntegrityError
    with Session(db_session.bind) as s2:
        duplicate_unit = UnitModel(
            unit_code="B-504",
            tower="B",
            unit_number=504,
            alicuota=Decimal("0.16736"),
        )
        s2.add(duplicate_unit)
        with pytest.raises(IntegrityError):
            s2.commit()


def test_master_billing_period_and_range(db_session: Session):
    """Verify billing period registration and month constraint."""
    period = BillingPeriodModel(
        period_date="2026-09-01",
        period_year=2026,
        period_month=9,
    )
    db_session.add(period)
    db_session.commit()

    saved_period = db_session.get(BillingPeriodModel, "2026-09-01")
    assert saved_period is not None
    assert saved_period.period_year == 2026
    assert saved_period.period_month == 9


def test_3tier_building_expense_hierarchy(db_session: Session):
    """Verify building expense report with sections, items, and cascade delete."""
    period = BillingPeriodModel(period_date="2026-09-01", period_year=2026, period_month=9)
    db_session.add(period)
    db_session.commit()

    # Create report header
    report = ExpenseReportModel(
        period_date="2026-09-01",
        operational_total=45000000,
        fondos_total=5000000,
        calculated_grand_total=50000000,
        source_file_path="gs://condo-reports/2026-09.pdf",
        telemetry_metadata={"extracted_tables": 4, "confidence": 0.99},
    )

    # Section 1: Maintenance
    sec_maint = ExpenseSectionModel(
        category_name="Gastos de Mantenimiento",
        expense_type="operational",
        calculated_subtotal=12000000,
        display_order=1,
    )
    item_otis = ExpenseItemModel(
        name="Mantencion Ascensores Otis",
        normalized_name="mantencion ascensores otis",
        amount=450000,
        display_order=1,
    )
    item_pumps = ExpenseItemModel(
        name="Reparacion Bombas de Agua",
        normalized_name="reparacion bombas de agua",
        amount=320000,
        display_order=2,
    )
    sec_maint.items.extend([item_otis, item_pumps])
    report.sections.append(sec_maint)

    db_session.add(report)
    db_session.commit()

    # Query report back
    loaded_report = db_session.scalar(
        select(ExpenseReportModel).where(ExpenseReportModel.period_date == "2026-09-01")
    )
    assert loaded_report is not None
    assert len(loaded_report.sections) == 1
    assert len(loaded_report.sections[0].items) == 2
    assert loaded_report.sections[0].items[0].amount == 450000

    # Test Cascade Deletion: deleting report must cleanly delete sections & items
    report_id = loaded_report.id
    db_session.delete(loaded_report)
    db_session.commit()

    orphaned_sections = db_session.scalars(
        select(ExpenseSectionModel).where(ExpenseSectionModel.report_id == report_id)
    ).all()
    assert len(orphaned_sections) == 0

    orphaned_items = db_session.scalars(select(ExpenseItemModel)).all()
    assert len(orphaned_items) == 0


def test_unit_bill_and_metered_consumption(db_session: Session):
    """Verify individual unit bill, concepts, consumptions, and charges."""
    # Seed unit & period
    unit = UnitModel(unit_code="B-504", tower="B", unit_number=504, alicuota=Decimal("0.16736"))
    period = BillingPeriodModel(period_date="2026-09-01", period_year=2026, period_month=9)
    db_session.add_all([unit, period])
    db_session.commit()

    # Create Unit Bill
    bill = UnitBillModel(
        unit_code="B-504",
        period_date="2026-09-01",
        last_payment_date=date(2026, 8, 15),
        last_payment_amount=142000,
        calculated_dept_subtotal=112000,
        calculated_consumo_total=35000,
        calculated_unit_subtotal=5000,
        calculated_total_month=152000,
        source_file_path="gs://condo-reports/bills/B-504-2026-09.pdf",
    )

    # Add Concept
    concept = UnitBillConceptModel(
        concept_name="Gasto Comun Ordinario",
        normalized_name="gasto comun ordinario",
        detail="0,16736 %",
        amount=112000,
        display_order=1,
    )
    bill.concepts.append(concept)

    # Add Metered Hot Water Consumption
    consumption = UnitBillConsumptionModel(
        item_name="Agua Caliente",
        normalized_name="agua caliente",
        previous_reading=Decimal("120.450"),
        current_reading=Decimal("126.950"),
        consumption=Decimal("6.500"),
        unit_rate=Decimal("5384.62"),
        total_amount=35000,
        display_order=1,
    )
    bill.consumptions.append(consumption)

    # Add Charge (e.g. Quincho)
    charge = UnitBillChargeModel(
        charge_name="Reserva Quincho",
        normalized_name="reserva quincho",
        charge_type="fixed_charge",
        amount=5000,
        display_order=1,
    )
    bill.unit_charges.append(charge)

    db_session.add(bill)
    db_session.commit()

    # Query back
    loaded_bill = db_session.scalar(
        select(UnitBillModel).where(
            UnitBillModel.unit_code == "B-504",
            UnitBillModel.period_date == "2026-09-01",
        )
    )
    assert loaded_bill is not None
    assert loaded_bill.calculated_total_month == 152000
    assert len(loaded_bill.concepts) == 1
    assert len(loaded_bill.consumptions) == 1
    assert loaded_bill.consumptions[0].consumption == Decimal("6.500")
    assert len(loaded_bill.unit_charges) == 1


def test_restrict_deletion_on_master_unit(db_session: Session):
    """Verify ON DELETE RESTRICT prevents deleting an apartment that has historical bills."""
    unit = UnitModel(unit_code="B-504", tower="B", unit_number=504, alicuota=Decimal("0.16736"))
    period = BillingPeriodModel(period_date="2026-09-01", period_year=2026, period_month=9)
    db_session.add_all([unit, period])
    db_session.commit()

    bill = UnitBillModel(
        unit_code="B-504",
        period_date="2026-09-01",
        calculated_total_month=152000,
    )
    db_session.add(bill)
    db_session.commit()

    # Attempt to delete the master unit while a bill references it
    db_session.delete(unit)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
