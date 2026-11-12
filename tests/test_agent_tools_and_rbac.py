"""Tests for 3NF Data Gateway, Fast-Path Tools, Spend Evolution, Prompts, and RBAC."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from backend.app.agent.analytics.gateway_client import (
    QuerySecurityError,
    execute_readonly_sql,
    query_data_gateway,
    validate_query_security,
)
from backend.app.agent.analytics.prompt_loader import (
    load_modular_data_dictionary,
    load_prompt_template,
)
from backend.app.agent.analytics.state import AnalyticsResult
from backend.app.agent.analytics.tools.building_tools import (
    get_building_summary,
    get_system_diagnostics,
)
from backend.app.agent.analytics.tools.spend_evolution import (
    format_catalog_for_prompt,
    get_expense_concept_catalog,
    get_spend_evolution_data,
    plot_spend_evolution,
)
from backend.app.agent.analytics.tools.unit_tools import get_unit_bill_details
from backend.app.agent.state import UserContext
from backend.app.agent.tools import (
    check_system_status,
    lookup_building_summary,
    lookup_spend_evolution,
    lookup_unit_bill,
    run_deep_financial_analysis,
)


@pytest.fixture
def resident_b504() -> UserContext:
    return UserContext(
        uid="uid_res_504b",
        email="residente504b@condominio.cl",
        role="resident",
        unit_code="B-504",
    )


@pytest.fixture
def admin_user() -> UserContext:
    return UserContext(
        uid="uid_admin_001",
        email="admin@condominio.cl",
        role="admin",
        unit_code=None,
    )


def test_load_modular_data_dictionary_3nf_domains():
    """Verify modular data dictionary loads 3NF schema documentation for building, unit, and cross_domain."""
    bldg_text = load_modular_data_dictionary(domain="building")
    assert "Domain Conventions & Financial Rules" in bldg_text
    assert "billing_periods" in bldg_text
    assert "expense_reports" in bldg_text
    assert "unit_bills" not in bldg_text

    unit_text = load_modular_data_dictionary(domain="unit")
    assert "units" in unit_text
    assert "unit_bills" in unit_text
    assert "unit_bill_consumptions" in unit_text
    assert "expense_sections" not in unit_text

    cross_text = load_modular_data_dictionary(domain="cross_domain")
    assert "expense_reports" in cross_text
    assert "unit_bills" in cross_text
    assert "units" in cross_text
    assert "Prorating Relationships (Ley de Copropiedad 21.442)" in cross_text

    sql_prompt = load_prompt_template("sql_coder_system.md")
    assert "JOIN units" in sql_prompt


def test_gateway_security_whitelist_and_3nf_joins(seeded_db: Session, seeded_client: TestClient):
    """Verify 3NF tables ('units', 'billing_periods') are whitelisted while mutations and catalogs are blocked."""
    # Valid 3NF JOIN query across unit_bills, units, and billing_periods
    valid_sql = """
    SELECT b.unit_code, u.tower, u.unit_number, u.alicuota, p.period_year, p.period_month, b.calculated_total_month
    FROM unit_bills b
    JOIN units u ON b.unit_code = u.unit_code
    JOIN billing_periods p ON b.period_date = p.period_date
    WHERE u.tower = 'B' AND u.unit_number = 504
    ORDER BY b.period_date DESC;
    """
    validate_query_security(valid_sql)
    rows, err = query_data_gateway(valid_sql, client=seeded_db)
    assert err is None
    assert rows is not None
    assert len(rows) == 3
    assert rows[0]["tower"] == "B"
    assert rows[0]["unit_number"] == 504

    # HTTP /api/v1/analytics/execute-query endpoint test
    resp = seeded_client.post("/api/v1/analytics/execute-query", json={"sql": valid_sql})
    assert resp.status_code == 200
    assert resp.json()["row_count"] == 3

    # Rejects mutations, multi-statements, system catalogs, and non-whitelisted tables (including security_audit_logs!)
    with pytest.raises(QuerySecurityError) as e_mut:
        validate_query_security("DELETE FROM unit_bills;")
    assert e_mut.value.status_code == 400

    with pytest.raises(QuerySecurityError) as e_cat:
        validate_query_security("SELECT * FROM sqlite_master;")
    assert e_cat.value.status_code == 403

    with pytest.raises(QuerySecurityError) as e_unauth_tbl:
        validate_query_security("SELECT * FROM security_audit_logs;")
    assert e_unauth_tbl.value.status_code == 403


def test_unit_and_building_tools_3nf(seeded_db: Session):
    """Verify fast-path unit and building tools query 3NF SQLAlchemy models directly."""
    # 1. Unit bill details (latest & specific period)
    u_latest = get_unit_bill_details("B-504", client=seeded_db)
    assert "error" not in u_latest
    assert u_latest["unit_code"] == "B-504"
    assert u_latest["tower"] == "B"
    assert u_latest["unit_number"] == 504
    assert u_latest["period_date"] == "2026-09-01"
    assert u_latest["period_year"] == 2026
    assert u_latest["period_month"] == 9
    assert u_latest["alicuota_total"] == pytest.approx(0.16736)
    assert len(u_latest["concepts"]) == 2
    assert len(u_latest["consumptions"]) == 1
    assert len(u_latest["unit_charges"]) == 1

    u_aug = get_unit_bill_details("B-504", period_date="2026-08", client=seeded_db)
    assert u_aug["period_date"] == "2026-08-01"
    assert u_aug["calculated_total_month"] == 125465

    u_missing = get_unit_bill_details("Z-999", client=seeded_db)
    assert "error" in u_missing

    # 2. Building summary & diagnostics
    b_latest = get_building_summary(client=seeded_db)
    assert "error" not in b_latest
    assert b_latest["period_date"] == "2026-09-01"
    assert b_latest["period_year"] == 2026
    assert b_latest["calculated_grand_total"] == 51000000
    assert len(b_latest["sections"]) == 1

    b_aug = get_building_summary(period_date="2026-08", client=seeded_db)
    assert b_aug["period_date"] == "2026-08-01"

    diag = get_system_diagnostics(client=seeded_db)
    assert diag["status"] == "healthy"
    assert diag["loaded_reports_count"] == 3
    assert diag["total_seeded_units"] == 4
    assert diag["total_unit_bills"] == 3


def test_spend_evolution_and_base64_chart_delivery(
    seeded_db: Session,
    resident_b504: UserContext,
):
    """Verify spend evolution across building and unit domains generates valid PNG and base64 payload."""
    catalog = get_expense_concept_catalog(client=seeded_db)
    assert "Mantenciones y Servicios" in catalog["building_categories"]
    assert "Gasto Comun Ordinario" in catalog["unit_concepts"]
    assert "Reserva Quincho" in catalog["unit_charges"]
    snippet = format_catalog_for_prompt(catalog)
    assert "Catálogo Oficial de Conceptos" in snippet

    # 1. Building total evolution
    out_bldg = lookup_spend_evolution(
        user_context=resident_b504,
        knowledge_domain="building",
        client=seeded_db,
    )
    assert "error" not in out_bldg
    assert "Evolución Mensual" in out_bldg["summary"]
    assert out_bldg["chart_file_path"] is not None
    assert out_bldg["chart_base64"] is not None
    raw_png = base64.b64decode(out_bldg["chart_base64"])
    assert raw_png[:8] == b"\x89PNG\r\n\x1a\n"
    Path(out_bldg["chart_file_path"]).unlink(missing_ok=True)

    # 2. Unit total evolution (4 series)
    out_unit = lookup_spend_evolution(
        user_context=resident_b504,
        knowledge_domain="unit",
        unit_code="B-504",
        client=seeded_db,
    )
    assert "error" not in out_unit
    assert "Cuota Base + Fondos" in out_unit["summary"]
    assert out_unit["chart_base64"] is not None
    if out_unit["chart_file_path"]:
        Path(out_unit["chart_file_path"]).unlink(missing_ok=True)

    # 3. Unit hot water evolution
    out_hw = lookup_spend_evolution(
        user_context=resident_b504,
        knowledge_domain="unit",
        unit_code="B-504",
        target_concept="agua caliente",
        client=seeded_db,
    )
    assert "error" not in out_hw
    assert "Consumo (m³)" in out_hw["summary"]
    if out_hw["chart_file_path"]:
        Path(out_hw["chart_file_path"]).unlink(missing_ok=True)

    # 4. Unit spend evolution without tower prefix ("504") and natural phrasings
    out_unit_num = lookup_spend_evolution(
        user_context=resident_b504,
        knowledge_domain="unit",
        unit_code="504",
        target_concept="Gasto Común",
        client=seeded_db,
    )
    assert "error" not in out_unit_num
    assert "Cuota Base + Fondos" in out_unit_num["summary"]
    if out_unit_num["chart_file_path"]:
        Path(out_unit_num["chart_file_path"]).unlink(missing_ok=True)

    out_unit_phrase = lookup_spend_evolution(
        user_context=resident_b504,
        knowledge_domain="unit",
        unit_code="504",
        target_concept="gasto de mi unidad",
        client=seeded_db,
    )
    assert "error" not in out_unit_phrase
    assert "Cuota Base + Fondos" in out_unit_phrase["summary"]
    if out_unit_phrase["chart_file_path"]:
        Path(out_unit_phrase["chart_file_path"]).unlink(missing_ok=True)

    # 5. Operational spend: Unit vs Building
    out_unit_op = lookup_spend_evolution(
        user_context=resident_b504,
        knowledge_domain="unit",
        unit_code="504",
        target_concept="gasto operacional",
        client=seeded_db,
    )
    assert "error" not in out_unit_op
    assert "Cuota Base + Fondos" in out_unit_op["summary"]
    if out_unit_op["chart_file_path"]:
        Path(out_unit_op["chart_file_path"]).unlink(missing_ok=True)

    out_bldg_op = lookup_spend_evolution(
        user_context=resident_b504,
        knowledge_domain="building",
        target_concept="gasto operacional",
        client=seeded_db,
    )
    assert "error" not in out_bldg_op
    assert "Gasto Operacional" in out_bldg_op["summary"]
    if out_bldg_op["chart_file_path"]:
        Path(out_bldg_op["chart_file_path"]).unlink(missing_ok=True)


def test_rbac_enforcement_across_all_tools(
    seeded_db: Session,
    resident_b504: UserContext,
    admin_user: UserContext,
):
    """Verify resident RBAC blocks cross-unit and admin-only calls while allowing authorized queries."""
    # Resident querying own unit succeeds
    ok_res = lookup_unit_bill(user_context=resident_b504, client=seeded_db)
    assert "error" not in ok_res
    assert ok_res["unit_code"] == "B-504"

    # Resident querying another unit is blocked with unauthorized=True
    unauth_unit = lookup_unit_bill(
        user_context=resident_b504,
        unit_code="A-101",
        query_text="Boleta A-101",
        client=seeded_db,
    )
    assert unauth_unit.get("unauthorized") is True

    # Resident querying system status is blocked
    unauth_sys = check_system_status(user_context=resident_b504, client=seeded_db)
    assert unauth_sys.get("unauthorized") is True

    # Resident querying multi-unit cohort in deep analysis is blocked
    mock_wf = MagicMock()
    unauth_cohort = run_deep_financial_analysis(
        user_context=resident_b504,
        query="¿Cuáles son los departamentos con mayor deuda?",
        workflow_fn=mock_wf,
    )
    assert unauth_cohort.get("unauthorized") is True
    mock_wf.assert_not_called()

    # Admin can access any unit and system status
    adm_unit = lookup_unit_bill(user_context=admin_user, unit_code="B-504", client=seeded_db)
    assert "error" not in adm_unit
    adm_sys = check_system_status(user_context=admin_user, client=seeded_db)
    assert "error" not in adm_sys
    adm_bldg = lookup_building_summary(user_context=admin_user, client=seeded_db)
    assert "error" not in adm_bldg
