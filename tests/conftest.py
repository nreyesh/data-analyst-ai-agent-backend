"""Pytest configuration, environment bootstrapping, and 3NF database fixtures."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import types
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Ensure 'backend' and 'front' namespaces are importable regardless of working directory
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if "backend" not in sys.modules:
    pkg = types.ModuleType("backend")
    pkg.__path__ = [str(Path(__file__).resolve().parent.parent)]
    sys.modules["backend"] = pkg

from backend.app.database import Base, get_db  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.models import (  # noqa: E402
    BillingPeriodModel,
    ExpenseItemModel,
    ExpenseReportModel,
    ExpenseSectionModel,
    UnitBillChargeModel,
    UnitBillConceptModel,
    UnitBillConsumptionModel,
    UnitBillModel,
    UnitModel,
)


def seed_3nf_test_data(session: Session) -> None:
    """Populate a 3NF test database with units, billing periods, building reports, and unit bills."""
    units = [
        UnitModel(unit_code="B-504", tower="B", unit_number=504, alicuota=Decimal("0.16736")),
        UnitModel(unit_code="A-504", tower="A", unit_number=504, alicuota=Decimal("0.16736")),
        UnitModel(unit_code="A-101", tower="A", unit_number=101, alicuota=Decimal("0.15000")),
        UnitModel(unit_code="B-506", tower="B", unit_number=506, alicuota=Decimal("0.18000")),
    ]
    periods = [
        BillingPeriodModel(period_date="2026-07-01", period_year=2026, period_month=7),
        BillingPeriodModel(period_date="2026-08-01", period_year=2026, period_month=8),
        BillingPeriodModel(period_date="2026-09-01", period_year=2026, period_month=9),
    ]
    session.add_all(units + periods)
    session.commit()

    # Building Expense Reports
    for p_date, op_tot, fnd_tot, maint_sub in [
        ("2026-07-01", 42000000, 4000000, 12000000),
        ("2026-08-01", 44000000, 5000000, 13500000),
        ("2026-09-01", 46000000, 5000000, 15000000),
    ]:
        rep = ExpenseReportModel(
            period_date=p_date,
            operational_total=op_tot,
            fondos_total=fnd_tot,
            calculated_grand_total=op_tot + fnd_tot,
            source_file_path=f"gs://test/{p_date}.pdf",
        )
        sec_maint = ExpenseSectionModel(
            category_name="Mantenciones y Servicios",
            expense_type="operational",
            calculated_subtotal=maint_sub,
            display_order=1,
        )
        sec_maint.items.append(
            ExpenseItemModel(
                name="Mantencion Ascensores Otis",
                normalized_name="mantencion ascensores otis",
                amount=maint_sub // 2,
                display_order=1,
            )
        )
        sec_maint.items.append(
            ExpenseItemModel(
                name="Mantencion Calderas",
                normalized_name="mantencion calderas",
                amount=maint_sub - (maint_sub // 2),
                display_order=2,
            )
        )
        rep.sections.append(sec_maint)
        session.add(rep)

    # Unit Bills for B-504
    for p_date, dept_sub, cons_tot, chg_tot, m3_val, rate_val in [
        ("2026-07-01", 105000, 12000, 0, Decimal("2.000"), Decimal("6000.00")),
        ("2026-08-01", 108776, 14189, 2500, Decimal("1.700"), Decimal("8346.47")),
        ("2026-09-01", 112000, 15000, 5000, Decimal("2.500"), Decimal("6000.00")),
    ]:
        bill = UnitBillModel(
            unit_code="B-504",
            period_date=p_date,
            last_payment_date=date(2026, 8, 15),
            last_payment_amount=120000,
            calculated_dept_subtotal=dept_sub,
            calculated_consumo_total=cons_tot,
            calculated_unit_subtotal=chg_tot,
            calculated_total_month=dept_sub + cons_tot + chg_tot,
            source_file_path=f"gs://test/B-504-{p_date}.pdf",
        )
        bill.concepts.append(
            UnitBillConceptModel(
                concept_name="Gasto Comun Ordinario",
                normalized_name="gasto comun ordinario",
                detail="0,16736 %",
                amount=dept_sub - 5000,
                display_order=1,
            )
        )
        bill.concepts.append(
            UnitBillConceptModel(
                concept_name="Fondo de Reserva",
                normalized_name="fondo de reserva",
                detail="5 %",
                amount=5000,
                display_order=2,
            )
        )
        bill.consumptions.append(
            UnitBillConsumptionModel(
                item_name="Agua Caliente",
                normalized_name="agua caliente",
                previous_reading=Decimal("100.000"),
                current_reading=Decimal("100.000") + m3_val,
                consumption=m3_val,
                unit_rate=rate_val,
                total_amount=cons_tot,
                display_order=1,
            )
        )
        if chg_tot > 0:
            bill.unit_charges.append(
                UnitBillChargeModel(
                    charge_name="Reserva Quincho",
                    normalized_name="reserva quincho",
                    charge_type="fixed_charge",
                    amount=chg_tot,
                    display_order=1,
                )
            )
        session.add(bill)

    session.commit()


@pytest.fixture
def seeded_db(monkeypatch):
    """Provide an in-memory SQLite Session pre-seeded with 3NF condominium data and wired into SessionLocal."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(test_engine, "connect")
    def _set_fk(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    # Wire in-process gateways & tools to use the test engine/sessionmaker
    monkeypatch.setattr("backend.app.database.engine", test_engine)
    monkeypatch.setattr("backend.app.database.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("backend.app.agent.analytics.gateway_client.default_engine", test_engine)
    monkeypatch.setattr("backend.app.agent.analytics.tools.building_tools.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("backend.app.agent.analytics.tools.unit_tools.SessionLocal", TestingSessionLocal)

    with TestingSessionLocal() as session:
        seed_3nf_test_data(session)
        yield session


@pytest.fixture
def seeded_client(seeded_db: Session):
    """Provide a FastAPI TestClient backed by the seeded 3NF in-memory database."""
    def override_get_db():
        yield seeded_db

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
