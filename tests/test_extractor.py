"""Unit and integration tests for CondominiumExtractor, ground truth, and DB persistence."""

from datetime import date
from decimal import Decimal
from pathlib import Path
import pymupdf as fitz
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.extractor.extractor import CondominiumExtractor
from backend.app.extractor.ground_truth import (
    GroundTruthData,
    UnitGroundTruthData,
    extract_ground_truth,
    extract_unit_ground_truth,
)
from backend.app.extractor.schemas import (
    ConceptItem,
    ConsumptionItem,
    ExpenseReport,
    ExtractionMetadata,
    Section,
    SubTopic,
    UnitBillReport,
    UnitChargeItem,
    validate_expense_report,
    validate_unit_bill_report,
)
from backend.app.models import (
    Base,
    BillingPeriodModel,
    ExpenseSectionModel,
    UnitBillChargeModel,
    UnitBillConceptModel,
    UnitBillConsumptionModel,
    UnitModel,
)

SAMPLE_BUILDING_PDF = Path("/home/nereyes/agentes/data-analyst/examples/2026-9.pdf")


def make_synthetic_building_pdf() -> bytes:
    """Generate in-memory synthetic 1-page building expense PDF for parser testing."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (50, 50),
        "Informe Gastos Comunes\n"
        "Septiembre 2026\n\n"
        "Remuneraciones\n"
        "$20.000.000\n"
        "Consumos\n"
        "$10.000.000\n"
        "TOTAL GASTOS COMUNES\n"
        "$30.000.000\n"
        "Fondo de Reserva 5%\n"
        "$1.500.000\n"
        "TOTAL\n"
        "$31.500.000\n",
    )
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def make_synthetic_unit_pdf() -> bytes:
    """Generate in-memory synthetic 1-page individual unit bill PDF for parser testing."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (50, 50),
        "Nota de Cobro Agosto 2026 B-504\n\n"
        "Alicuota Total\n"
        "0,16736 %\n\n"
        "Fecha ultimo Pago\n"
        "31-08-2026\n\n"
        "Monto ultimo Pago\n"
        "$ 125.204\n\n"
        "Subtotal Departamento\n"
        "$ 108.776\n\n"
        "Subtotal Consumo\n"
        "$ 14.189\n\n"
        "Subtotal Recargos\n"
        "$ 2.500\n\n"
        "Total del mes\n"
        "$ 125.465\n",
    )
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite session with foreign key support."""
    engine = create_engine("sqlite:///:memory:")
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_ground_truth_extraction_building_pdf():
    """Verify deterministic extraction from in-memory building expense PDF."""
    pdf_bytes = make_synthetic_building_pdf()
    gt = extract_ground_truth(pdf_bytes)
    assert gt.period_date == "01/09/2026"
    assert gt.operational_total == 30000000
    assert gt.fondos_total == 1500000
    assert gt.grand_total == 31500000
    assert "Remuneraciones" in gt.section_totals
    assert "Consumos" in gt.section_totals
    assert "Fondos" in gt.section_totals


def test_ground_truth_extraction_real_building_pdf():
    """Verify deterministic extraction from real building expense PDF file."""
    if not SAMPLE_BUILDING_PDF.is_file():
        pytest.skip(f"Sample PDF not found at {SAMPLE_BUILDING_PDF}")
    gt = extract_ground_truth(SAMPLE_BUILDING_PDF)
    assert gt.period_date == "01/09/2026"
    assert gt.operational_total == 56454297
    assert gt.fondos_total == 9322715
    assert gt.grand_total == 65777012
    assert gt.section_totals["Remuneraciones"] == 19632258
    assert gt.section_totals["Consumos"] == 13669643  # 8442107 + 5227536 (Gas Cargo)
    assert gt.section_totals["Mantenciones y Servicios"] == 17071495
    assert gt.section_totals["Reparaciones y Repuestos"] == 1965195
    assert gt.section_totals["Seguros"] == 2243977
    assert gt.section_totals["Asesorías y Honorarios Profesionales"] == 24324
    assert gt.section_totals["Insumos"] == 1713550
    assert gt.section_totals["Otros Gastos"] == 133855
    assert gt.section_totals["Fondos"] == 9322715


def test_ground_truth_extraction_unit_pdf():
    """Verify deterministic extraction from in-memory individual unit bill PDF."""
    pdf_bytes = make_synthetic_unit_pdf()
    gt = extract_unit_ground_truth(pdf_bytes)
    assert gt.period_date == "01/08/2026"
    assert gt.unit_code == "B-504"
    assert gt.unit_tower == "B"
    assert gt.unit_number == 504
    assert gt.alicuota_total == 0.16736
    assert gt.last_payment_date == "31/08/2026"
    assert gt.last_payment_amount == 125204
    assert gt.declared_dept_subtotal == 108776
    assert gt.declared_consumo_subtotal == 14189
    assert gt.declared_recargos_subtotal == 2500
    assert gt.declared_total_month == 125465


def test_save_building_report_to_db(db_session: Session):
    """Test saving a structured building report to 3NF schema."""
    extractor = CondominiumExtractor()

    report = ExpenseReport(
        issue_date="01/09/2026",
        sections=[
            Section(
                general_topic="Remuneraciones",
                expense_type="operational",
                sub_topics=[
                    SubTopic(name="Sueldos", value=20000000),
                    SubTopic(name="Leyes Sociales", value=5000000),
                ],
            ),
            Section(
                general_topic="Fondos",
                expense_type="fondos",
                sub_topics=[
                    SubTopic(name="Fondo de Reserva 5%", value=1250000),
                ],
            ),
        ],
    )
    metadata = ExtractionMetadata(
        model_used="gemini-3.5-flash-lite",
        latency_seconds=1.2,
        timestamp="2026-11-10T12:00:00Z",
    )
    gt = GroundTruthData(
        period_date="01/09/2026",
        operational_total=25000000,
        fondos_total=1250000,
        grand_total=26250000,
        section_totals={"Remuneraciones": 25000000, "Fondos": 1250000},
    )

    saved_model = extractor.save_building_report_to_db(
        db=db_session,
        report=report,
        file_uri="gs://building-analytics-data/building/2026-09.pdf",
        metadata=metadata,
        ground_truth=gt,
    )

    assert saved_model is not None
    assert saved_model.period_date == "2026-09-01"
    assert saved_model.operational_total == 25000000
    assert saved_model.fondos_total == 1250000
    assert saved_model.calculated_grand_total == 26250000

    # Verify billing period was auto-created
    period = db_session.get(BillingPeriodModel, "2026-09-01")
    assert period is not None
    assert period.period_year == 2026
    assert period.period_month == 9

    # Verify sections and items
    sections = db_session.scalars(
        select(ExpenseSectionModel).where(ExpenseSectionModel.report_id == saved_model.id)
    ).all()
    assert len(sections) == 2

    # Verify idempotency (re-saving overwrites cleanly)
    saved_model2 = extractor.save_building_report_to_db(
        db=db_session,
        report=report,
        file_uri="gs://building-analytics-data/building/2026-09.pdf",
        metadata=metadata,
        ground_truth=gt,
    )
    assert saved_model2.period_date == "2026-09-01"


def test_save_unit_bill_to_db_and_auto_populate_alicuota(db_session: Session):
    """Test saving a unit bill, creating child records, and auto-populating alicuota."""
    extractor = CondominiumExtractor()

    # Create unseeded unit without alicuota
    unit = UnitModel(
        unit_code="B-504",
        tower="B",
        unit_number=504,
        kastor_id=336,
        alicuota=None,  # Not yet populated
    )
    db_session.add(unit)
    db_session.commit()

    report = UnitBillReport(
        issue_date="01/08/2026",
        unit_code="B-504",
        unit_tower="B",
        unit_number=504,
        alicuota_total=0.16736,
        last_payment_date="31/08/2026",
        last_payment_amount=125204,
        concepts=[
            ConceptItem(concept="Gasto Común", detail="0,16736 %", amount=93236),
            ConceptItem(concept="Fondo de Reserva 5%", detail="5,00 %", amount=4662),
            ConceptItem(concept="Fondo Impermeabilizacion", detail=None, amount=10878),
        ],
        consumptions=[
            ConsumptionItem(
                item_name="Torre B",
                previous_reading=100.581,
                current_reading=102.282,
                consumption=1.701,
                unit_rate=8340.24,
                total=14189,
            )
        ],
        unit_charges=[
            UnitChargeItem(
                charge_name="Fondo de Indemnizacion",
                charge_type="fixed_charge",
                amount=2500,
            )
        ],
    )
    metadata = ExtractionMetadata(
        model_used="gemini-3.1-flash-lite",
        latency_seconds=0.9,
        timestamp="2026-11-10T12:05:00Z",
    )
    gt = UnitGroundTruthData(
        period_date="01/08/2026",
        unit_code="B-504",
        unit_tower="B",
        unit_number=504,
        alicuota_total=0.16736,
        last_payment_date="31/08/2026",
        last_payment_amount=125204,
        declared_dept_subtotal=108776,
        declared_consumo_subtotal=14189,
        declared_recargos_subtotal=2500,
        declared_total_month=125465,
    )

    saved_bill = extractor.save_unit_bill_to_db(
        db=db_session,
        report=report,
        file_uri="gs://building-analytics-data/units/B-504/2026-08.pdf",
        metadata=metadata,
        ground_truth=gt,
    )

    assert saved_bill is not None
    assert saved_bill.unit_code == "B-504"
    assert saved_bill.period_date == "2026-08-01"
    assert saved_bill.last_payment_date == date(2026, 8, 31)
    assert saved_bill.last_payment_amount == 125204
    assert saved_bill.calculated_dept_subtotal == 108776
    assert saved_bill.calculated_consumo_total == 14189
    assert saved_bill.calculated_unit_subtotal == 2500
    assert saved_bill.calculated_total_month == 108776 + 14189 + 2500

    # Verify unit alicuota was auto-populated!
    db_session.refresh(unit)
    assert unit.alicuota == Decimal("0.16736")

    # Verify child tables
    concepts = db_session.scalars(
        select(UnitBillConceptModel).where(UnitBillConceptModel.unit_bill_id == saved_bill.id)
    ).all()
    assert len(concepts) == 3

    consumptions = db_session.scalars(
        select(UnitBillConsumptionModel).where(
            UnitBillConsumptionModel.unit_bill_id == saved_bill.id
        )
    ).all()
    assert len(consumptions) == 1
    assert consumptions[0].item_name == "Torre B"
    assert consumptions[0].total_amount == 14189

    charges = db_session.scalars(
        select(UnitBillChargeModel).where(UnitBillChargeModel.unit_bill_id == saved_bill.id)
    ).all()
    assert len(charges) == 1
    assert charges[0].charge_name == "Fondo de Indemnizacion"
    assert charges[0].charge_type == "fixed_charge"
    assert charges[0].amount == 2500


def test_validate_expense_report():
    """Test validation logic for building expense report against ground truth."""
    gt = GroundTruthData(
        period_date="01/09/2026",
        operational_total=30000000,
        fondos_total=1500000,
        grand_total=31500000,
        section_totals={
            "Consumos": 10000000,
            "Remuneraciones": 20000000,
            "Mantenciones y Servicios": 5000000,
        },
    )
    # Valid report matching GT period and section totals
    report = ExpenseReport(
        issue_date="01/09/2026",
        sections=[
            Section(
                general_topic="Consumos",
                expense_type="operational",
                sub_topics=[SubTopic(name="Agua", value=gt.section_totals.get("Consumos", 0))],
            ),
            Section(
                general_topic="Remuneraciones",
                expense_type="operational",
                sub_topics=[SubTopic(name="Sueldos", value=gt.section_totals.get("Remuneraciones", 0))],
            ),
        ],
    )
    val = validate_expense_report(report, gt)
    # Expect missing other sections since only 2 were populated
    assert not val.is_valid
    assert any("Missing section" in err for err in val.errors)

    # Date mismatch error test
    mismatch_report = ExpenseReport(issue_date="01/01/2025", sections=[])
    val_mismatch = validate_expense_report(mismatch_report, gt)
    assert not val_mismatch.is_valid
    assert any("Billing period date mismatch" in err for err in val_mismatch.errors)


def test_validate_unit_bill_report():
    """Test validation logic for individual unit bill against ground truth."""
    gt = UnitGroundTruthData(
        period_date="01/08/2026",
        unit_code="B-504",
        alicuota_total=0.16736,
        declared_dept_subtotal=108776,
        declared_consumo_subtotal=14189,
        declared_recargos_subtotal=2500,
        declared_total_month=125465,
    )
    report = UnitBillReport(
        issue_date="01/08/2026",
        unit_code="B-504",
        alicuota_total=0.16736,
        concepts=[ConceptItem(concept="Gasto", amount=gt.declared_dept_subtotal or 0)],
        consumptions=[
            ConsumptionItem(
                item_name="Torre B",
                previous_reading=0,
                current_reading=1,
                consumption=1,
                unit_rate=1,
                total=gt.declared_consumo_subtotal or 0,
            )
        ],
        unit_charges=[
            UnitChargeItem(
                charge_name="Fondo de Indemnizacion",
                charge_type="recargo",
                amount=2500,
            )
        ],
    )
    val = validate_unit_bill_report(report, gt)
    assert val.is_valid
    assert len(val.errors) == 0


def test_ground_truth_dynamic_discovery_new_category():
    """Verify dynamic discovery of unseen category without hardcoding."""
    mock_pdf_text = (
        "Informe Gastos Comunes\n"
        "Octubre 2026\n\n"
        "Remuneraciones\n"
        "Sueldos (Factura: 01)\n"
        "$10.000.000\n"
        "$10.000.000\n"
        "Seguridad y Vigilancia\n"
        "Guardias de Seguridad (Factura: 101)\n"
        "$2.500.000\n"
        "Camaras CCTV (Factura: 102)\n"
        "$500.000\n"
        "$3.000.000\n"
        "Consumos\n"
        "Agua Potable (Boleta: 999)\n"
        "$1.000.000\n"
        "$1.000.000\n"
        "TOTAL GASTOS COMUNES\n"
        "$14.000.000\n"
        "FONDO DE RESERVA 5%\n"
        "$700.000\n"
        "TOTAL\n"
        "$14.700.000\n"
    )
    gt = extract_ground_truth(mock_pdf_text)
    assert gt.period_date == "01/10/2026"
    assert gt.operational_total == 14000000
    assert gt.fondos_total == 700000
    assert gt.grand_total == 14700000

    # Verify dynamically discovered new category
    assert "Seguridad y Vigilancia" in gt.section_totals
    assert gt.section_totals["Seguridad y Vigilancia"] == 3000000
    assert gt.section_totals["Remuneraciones"] == 10000000
    assert gt.section_totals["Consumos"] == 1000000
    assert gt.section_totals["Fondos"] == 700000

    # Verify validation succeeds with the new category
    report = ExpenseReport(
        issue_date="01/10/2026",
        sections=[
            Section(
                general_topic="Remuneraciones",
                expense_type="operational",
                sub_topics=[SubTopic(name="Sueldos", value=10000000)],
            ),
            Section(
                general_topic="Seguridad y Vigilancia",
                expense_type="operational",
                sub_topics=[
                    SubTopic(name="Guardias de Seguridad", value=2500000),
                    SubTopic(name="Camaras CCTV", value=500000),
                ],
            ),
            Section(
                general_topic="Consumos",
                expense_type="operational",
                sub_topics=[SubTopic(name="Agua Potable", value=1000000)],
            ),
            Section(
                general_topic="Fondos",
                expense_type="fondos",
                sub_topics=[SubTopic(name="Fondo de Reserva 5%", value=700000)],
            ),
        ],
    )
    val = validate_expense_report(report, gt)
    assert val.is_valid
    assert len(val.errors) == 0

    # Verify unexpected section detection
    report_extra = ExpenseReport(
        issue_date="01/10/2026",
        sections=report.sections + [
            Section(
                general_topic="Caja Chica Fantasma",
                expense_type="operational",
                sub_topics=[SubTopic(name="Extra", value=50000)],
            )
        ],
    )
    val_extra = validate_expense_report(report_extra, gt)
    assert not val_extra.is_valid
    assert any("Unexpected section 'Caja Chica Fantasma'" in err for err in val_extra.errors)
