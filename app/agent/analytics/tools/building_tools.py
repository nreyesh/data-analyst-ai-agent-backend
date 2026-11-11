"""Fast-path tools for macro building expense reports and system diagnostics."""

from __future__ import annotations

import logging
import re
from typing import Any
import requests
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.agent.analytics.config import get_analytics_settings
from backend.app.database import SessionLocal
from backend.app.models import (
    BillingPeriodModel,
    ExpenseReportModel,
    UnitBillModel,
    UnitModel,
)

logger = logging.getLogger("backend.agent.analytics.tools.building_tools")


def _normalize_period_date(period_date: str | None) -> str | None:
    """Normalize 'YYYY-MM' or 'YYYY-MM-01' into 'YYYY-MM-01'."""
    if not period_date:
        return None
    cleaned = period_date.strip()
    if re.match(r"^\d{4}-\d{2}$", cleaned):
        return f"{cleaned}-01"
    return cleaned


def _get_db_session(client: Any | None = None) -> tuple[Session | None, bool]:
    """Return a SQLAlchemy Session (and whether caller must close it) if client is not an HTTP mock."""
    if isinstance(client, Session):
        return client, False
    if isinstance(client, Engine):
        return Session(client), True
    if client is not None and hasattr(client, "get"):
        return None, False
    return SessionLocal(), True


def get_building_summary(
    period_date: str | None = None,
    client: Any | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Retrieve structured macro building expense snapshot via SQLAlchemy or REST client.

    Args:
        period_date: Optional period formatted as 'YYYY-MM-01' or 'YYYY-MM'. If None, fetches latest period.
        client: Optional SQLAlchemy Session/Engine or HTTP client (requests.Session / FastAPI TestClient) for testing.
        base_url: Optional base endpoint URL override.

    Returns:
        Structured dictionary with building report snapshot or clean error dict.
    """
    norm_period = _normalize_period_date(period_date)

    # Support HTTP mock client if explicitly passed in tests
    if (client is not None and hasattr(client, "get") and not isinstance(client, (Session, Engine))) or base_url is not None:
        settings = get_analytics_settings()
        endpoint = (base_url or settings.reports_api_url).rstrip("/")
        http_get = client.get if (client is not None and hasattr(client, "get")) else requests.get
        try:
            if norm_period:
                detail_res = http_get(f"{endpoint}/period/{norm_period}")
                if detail_res.status_code == 404:
                    return {"error": f"No building expense report found for period '{norm_period}'."}
                if detail_res.status_code != 200:
                    return {"error": f"Backend returned HTTP {detail_res.status_code} when querying period '{norm_period}'."}
                detail = detail_res.json()
            else:
                list_res = http_get(endpoint)
                if list_res.status_code != 200:
                    return {"error": f"Backend returned HTTP {list_res.status_code} when listing building reports."}
                reports = list_res.json()
                if not reports or not isinstance(reports, list):
                    return {"error": "No building expense reports found in database."}
                latest = reports[0]
                report_id = latest.get("id")
                if not report_id:
                    return {"error": "Invalid building report record."}
                detail_res = http_get(f"{endpoint}/{report_id}")
                detail = detail_res.json() if detail_res.status_code == 200 else latest

            sections_raw = detail.get("sections", [])
            sections_summary = [
                {
                    "category_name": s.get("category_name"),
                    "expense_type": s.get("expense_type"),
                    "calculated_subtotal": s.get("calculated_subtotal"),
                }
                for s in sections_raw
            ]
            return {
                "id": detail.get("id"),
                "period_date": detail.get("period_date"),
                "period_year": detail.get("period_year"),
                "period_month": detail.get("period_month"),
                "operational_total": detail.get("operational_total", 0),
                "fondos_total": detail.get("fondos_total", 0),
                "calculated_grand_total": detail.get("calculated_grand_total", 0),
                "sections": sections_summary,
            }
        except Exception as exc:
            logger.warning("Error fetching building summary over HTTP for period '%s': %s", norm_period, exc)
            return {"error": f"Service error fetching building report: {exc}"}

    # Direct In-Process SQLAlchemy Execution
    db, should_close = _get_db_session(client)
    assert db is not None
    try:
        if norm_period:
            report = db.scalar(
                select(ExpenseReportModel).where(ExpenseReportModel.period_date == norm_period)
            )
            if not report:
                return {"error": f"No building expense report found for period '{norm_period}'."}
        else:
            report = db.scalar(
                select(ExpenseReportModel).order_by(ExpenseReportModel.period_date.desc()).limit(1)
            )
            if not report:
                return {"error": "No building expense reports found in database."}

        bp = report.billing_period or db.scalar(
            select(BillingPeriodModel).where(BillingPeriodModel.period_date == report.period_date)
        )
        p_year = bp.period_year if bp else int(report.period_date[:4])
        p_month = bp.period_month if bp else int(report.period_date[5:7])

        sections_summary = [
            {
                "category_name": s.category_name,
                "expense_type": s.expense_type,
                "calculated_subtotal": int(s.calculated_subtotal),
            }
            for s in report.sections
        ]

        return {
            "id": str(report.id),
            "period_date": report.period_date,
            "period_year": p_year,
            "period_month": p_month,
            "operational_total": int(report.operational_total),
            "fondos_total": int(report.fondos_total),
            "calculated_grand_total": int(report.calculated_grand_total),
            "sections": sections_summary,
        }
    except Exception as exc:
        logger.warning("Error querying building summary from DB for period '%s': %s", norm_period, exc)
        return {"error": f"Database error fetching building report: {exc}"}
    finally:
        if should_close:
            db.close()


def get_system_diagnostics(
    client: Any | None = None,
    reports_url: str | None = None,
    sync_url: str | None = None,
) -> dict[str, Any]:
    """Retrieve loaded report periods, unit bill counts, and seeded unit counts from database."""
    if (client is not None and hasattr(client, "get") and not isinstance(client, (Session, Engine))) or reports_url is not None or sync_url is not None:
        settings = get_analytics_settings()
        rep_endpoint = (reports_url or settings.reports_api_url).rstrip("/")
        queue_endpoint = (sync_url or settings.sync_queue_status_url).rstrip("/")
        http_get = client.get if (client is not None and hasattr(client, "get")) else requests.get
        try:
            rep_res = http_get(rep_endpoint)
            if rep_res.status_code != 200:
                return {"error": f"Backend returned HTTP {rep_res.status_code} when querying reports status."}
            reports = rep_res.json() if isinstance(rep_res.json(), list) else []
            loaded_periods = [r.get("period_date") for r in reports if r.get("period_date")]

            queue_res = http_get(queue_endpoint)
            if queue_res.status_code != 200:
                return {"error": f"Backend returned HTTP {queue_res.status_code} when querying spool queue status."}
            queue_data = queue_res.json()
            return {
                "status": "healthy",
                "loaded_reports_count": len(loaded_periods),
                "loaded_periods": loaded_periods,
                "latest_period": loaded_periods[0] if loaded_periods else None,
                "pending_count": queue_data.get("pending_count", 0),
                "synced_count": queue_data.get("synced_count", 0),
                "failed_count": queue_data.get("failed_count", 0),
                "pending_files": queue_data.get("pending", []),
                "failed_files": queue_data.get("failed", []),
            }
        except Exception as exc:
            logger.warning("Error fetching system diagnostics over HTTP: %s", exc)
            return {"error": f"Service error fetching system diagnostics: {exc}"}

    # Direct In-Process SQLAlchemy Execution
    db, should_close = _get_db_session(client)
    assert db is not None
    try:
        loaded_periods = list(
            db.scalars(
                select(ExpenseReportModel.period_date).order_by(ExpenseReportModel.period_date.desc())
            ).all()
        )
        total_units = db.scalar(select(func.count(UnitModel.unit_code))) or 0
        total_unit_bills = db.scalar(select(func.count(UnitBillModel.id))) or 0

        latest_period = loaded_periods[0] if loaded_periods else None
        latest_period_unit_bills = 0
        if latest_period:
            latest_period_unit_bills = (
                db.scalar(
                    select(func.count(UnitBillModel.id)).where(
                        UnitBillModel.period_date == latest_period
                    )
                )
                or 0
            )

        return {
            "status": "healthy",
            "loaded_reports_count": len(loaded_periods),
            "loaded_periods": loaded_periods,
            "latest_period": latest_period,
            "total_seeded_units": int(total_units),
            "total_unit_bills": int(total_unit_bills),
            "latest_period_unit_bills": int(latest_period_unit_bills),
            "pending_count": max(0, int(total_units) - int(latest_period_unit_bills)) if latest_period else 0,
            "synced_count": int(total_unit_bills) + len(loaded_periods),
            "failed_count": 0,
        }
    except Exception as exc:
        logger.warning("Error querying system diagnostics from DB: %s", exc)
        return {"error": f"Database error fetching system diagnostics: {exc}"}
    finally:
        if should_close:
            db.close()
