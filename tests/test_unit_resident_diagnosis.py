"""Regression tests for resident RBAC tenant boundary validation without false positives."""

import pytest
from unittest.mock import MagicMock
from sqlalchemy.orm import Session

from backend.app.agent.analytics.tools.unit_tools import get_unit_bill_details
from backend.app.agent.state import UserContext, SupervisorDecision
from backend.app.agent.tools import (
    lookup_unit_bill,
    lookup_spend_evolution,
    run_deep_financial_analysis,
)


@pytest.fixture
def resident_b504() -> UserContext:
    return UserContext(
        uid="evkllxHvPAa6j144DRJ4bsgw6E53",
        email="nico.uk.cl@gmail.com",
        role="resident",
        unit_code="B-504",
    )


def test_resident_temporal_queries_do_not_trigger_rbac(resident_b504: UserContext, seeded_db: Session):
    """Verify queries with Spanish years, conjunctions, and prepositions are not blocked as unauthorized."""
    temporal_queries = [
        "que cargo particular se cargo en junio y octubre de este año?",
        "dame la evolucion del gasto comun del 2026",
        "dame la evolucion del gasto comun de 2026",
        "dame la evolucion del gasto comun de-2026",
        "evolución de 2026",
        "¿Qué cargos particulares específicos se cargaron en los meses de junio de 2026 y octubre de 2026 en el departamento B-504?",
        "comparar 2025 y 2026",
        "gastos de 2025 o 2026",
        "dia 15 a fin de mes",
    ]

    mock_workflow = MagicMock()
    mock_workflow.return_value = MagicMock(
        summary="Análisis completado",
        is_authorized=True,
        needs_clarification=False,
        clarification_prompt=None,
        candidate_options=[],
        thread_id=None,
        sql_query="SELECT 1",
        data=[],
        chart_file_path=None,
    )

    for q in temporal_queries:
        # 1. run_deep_financial_analysis must not trigger false positive unauthorized block
        res_analysis = run_deep_financial_analysis(
            user_context=resident_b504,
            query=q,
            unit_code="B-504",
            knowledge_domain="unit",
            workflow_fn=mock_workflow,
        )
        assert res_analysis.get("unauthorized") is not True, f"Query '{q}' was falsely blocked in analysis"
        assert "no puedes consultar" not in res_analysis.get("error", "")

        # 2. lookup_unit_bill must not trigger false positive unauthorized block
        res_bill = lookup_unit_bill(
            user_context=resident_b504,
            unit_code="B-504",
            period_date="2026-09-01",
            query_text=q,
            client=seeded_db,
        )
        assert res_bill.get("unauthorized") is not True, f"Query '{q}' was falsely blocked in bill lookup"


def test_resident_unauthorized_unit_access_is_blocked(resident_b504: UserContext):
    """Verify explicit attempts to access a different apartment unit are blocked deterministically."""
    # Attempting to access A-101
    res_bill = lookup_unit_bill(
        user_context=resident_b504,
        unit_code="A-101",
        period_date="2026-09-01",
        query_text="Muéstrame la boleta de A-101",
    )
    assert res_bill.get("unauthorized") is True
    assert "Acceso denegado" in res_bill.get("error", "")
    assert "B-504" in res_bill.get("error", "")

    # Attempting to access another tower of same number (A-504)
    res_bill_tower = lookup_unit_bill(
        user_context=resident_b504,
        unit_code="A-504",
        period_date="2026-09-01",
        query_text="Muéstrame la boleta de A-504",
    )
    assert res_bill_tower.get("unauthorized") is True

    # Blocked via structured decision flag
    res_analysis = run_deep_financial_analysis(
        user_context=resident_b504,
        query="Muéstrame el análisis de la Torre A",
        unit_code="Torre A",
        is_unauthorized_unit_access=True,
        unauthorized_target_unit="Torre A",
    )
    assert res_analysis.get("unauthorized") is True
    assert "no puedes consultar `Torre A`" in res_analysis.get("error", "")


def test_lookup_unit_bill_with_resident_unit_code(resident_b504: UserContext, seeded_db: Session):
    """Verify lookup_unit_bill succeeds for assigned unit B-504."""
    result = lookup_unit_bill(
        user_context=resident_b504,
        unit_code="B-504",
        period_date="2026-09-01",
        query_text="dame el detalle de mi gasto comun de septiembre",
        client=seeded_db,
    )
    assert "error" not in result, f"Expected successful bill lookup but got error: {result.get('error')}"
    assert "Resumen de Nota de Cobro" in result.get("summary", "")
