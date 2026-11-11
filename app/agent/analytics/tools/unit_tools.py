"""Fast-path tools for individual apartment unit statements."""

from __future__ import annotations

import logging
from typing import Any
import requests
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.agent.analytics.config import get_analytics_settings
from backend.app.database import SessionLocal
from backend.app.models import BillingPeriodModel, UnitBillModel, UnitModel

logger = logging.getLogger("backend.agent.analytics.tools.unit_tools")


def _get_db_session(client: Any | None = None) -> tuple[Session | None, bool]:
    """Return a SQLAlchemy Session (and whether caller must close it) if client is not an HTTP mock."""
    if isinstance(client, Session):
        return client, False
    if isinstance(client, Engine):
        return Session(client), True
    if client is not None and hasattr(client, "get"):
        return None, False
    return SessionLocal(), True


def get_unit_bill_details(
    unit_code: str,
    period_date: str | None = None,
    client: Any | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Retrieve structured billing snapshot for an apartment unit via SQLAlchemy or REST client.

    Args:
        unit_code: Apartment code (e.g. 'B-504').
        period_date: Optional period formatted as 'YYYY-MM-01'. If None, fetches latest period.
        client: Optional SQLAlchemy Session/Engine or HTTP client for testing.
        base_url: Optional base endpoint URL override.

    Returns:
        Structured dictionary with billing snapshot or clean error dict.
    """
    norm_unit = (unit_code or "").strip().upper()
    if not norm_unit:
        return {"error": "unit_code is required"}

    # Support HTTP mock client if explicitly passed in tests
    if (client is not None and hasattr(client, "get") and not isinstance(client, (Session, Engine))) or base_url is not None:
        settings = get_analytics_settings()
        endpoint = base_url or settings.unit_bills_api_url
        http_get = client.get if (client is not None and hasattr(client, "get")) else requests.get
        try:
            res = http_get(f"{endpoint}", params={"unit_code": norm_unit})
            if res.status_code != 200:
                return {"error": f"Backend returned HTTP {res.status_code} when querying unit '{norm_unit}'."}
            bills = res.json()
            if not bills or not isinstance(bills, list):
                return {"error": f"No unit bill found for unit '{norm_unit}'."}

            target_summary: dict[str, Any] | None = None
            if period_date:
                target_period = period_date.strip()
                for b in bills:
                    if b.get("period_date") == target_period:
                        target_summary = b
                        break
                if not target_summary:
                    return {"error": f"No unit bill found for unit '{norm_unit}' in period '{period_date}'."}
            else:
                target_summary = bills[0]

            bill_id = target_summary.get("id")
            if not bill_id:
                return {"error": f"Invalid unit bill record for '{norm_unit}'."}

            detail_res = http_get(f"{endpoint}/{bill_id}")
            if detail_res.status_code != 200:
                return {"error": f"Failed to retrieve bill detail for ID '{bill_id}' (HTTP {detail_res.status_code})."}

            detail = detail_res.json()
            return {
                "unit_code": detail.get("unit_code"),
                "period_date": detail.get("period_date"),
                "period_year": detail.get("period_year"),
                "period_month": detail.get("period_month"),
                "alicuota_total": detail.get("alicuota_total"),
                "calculated_total_month": detail.get("calculated_total_month"),
                "calculated_dept_subtotal": detail.get("calculated_dept_subtotal"),
                "calculated_consumo_total": detail.get("calculated_consumo_total"),
                "calculated_unit_subtotal": detail.get("calculated_unit_subtotal"),
                "last_payment_date": detail.get("last_payment_date"),
                "last_payment_amount": detail.get("last_payment_amount"),
                "concepts": detail.get("concepts", []),
                "consumptions": detail.get("consumptions", []),
                "unit_charges": detail.get("unit_charges", []),
            }
        except Exception as exc:
            logger.warning("Error fetching unit bill details over HTTP for '%s': %s", norm_unit, exc)
            return {"error": f"Service error fetching unit bill: {exc}"}

    # Direct In-Process SQLAlchemy Execution (3NF Schema)
    db, should_close = _get_db_session(client)
    assert db is not None
    try:
        if period_date:
            target_period = period_date.strip()
            if len(target_period) == 7:
                target_period = f"{target_period}-01"
            bill = db.scalar(
                select(UnitBillModel).where(
                    UnitBillModel.unit_code == norm_unit,
                    UnitBillModel.period_date == target_period,
                )
            )
            if not bill:
                return {"error": f"No unit bill found for unit '{norm_unit}' in period '{period_date}'."}
        else:
            bill = db.scalar(
                select(UnitBillModel)
                .where(UnitBillModel.unit_code == norm_unit)
                .order_by(UnitBillModel.period_date.desc())
                .limit(1)
            )
            if not bill:
                return {"error": f"No unit bill found for unit '{norm_unit}'."}

        unit_obj = bill.unit or db.scalar(
            select(UnitModel).where(UnitModel.unit_code == norm_unit)
        )
        bp = bill.billing_period or db.scalar(
            select(BillingPeriodModel).where(BillingPeriodModel.period_date == bill.period_date)
        )

        alicuota_val = float(unit_obj.alicuota) if (unit_obj and unit_obj.alicuota is not None) else 0.0
        p_year = bp.period_year if bp else int(bill.period_date[:4])
        p_month = bp.period_month if bp else int(bill.period_date[5:7])

        concepts_list = [
            {
                "concept_name": c.concept_name,
                "normalized_name": c.normalized_name,
                "detail": c.detail,
                "amount": int(c.amount),
            }
            for c in bill.concepts
        ]
        consumptions_list = [
            {
                "item_name": cons.item_name,
                "normalized_name": cons.normalized_name,
                "previous_reading": float(cons.previous_reading),
                "current_reading": float(cons.current_reading),
                "consumption": float(cons.consumption),
                "unit_rate": float(cons.unit_rate),
                "total_amount": int(cons.total_amount),
            }
            for cons in bill.consumptions
        ]
        charges_list = [
            {
                "charge_name": ch.charge_name,
                "normalized_name": ch.normalized_name,
                "charge_type": ch.charge_type,
                "amount": int(ch.amount),
            }
            for ch in bill.unit_charges
        ]

        return {
            "unit_code": bill.unit_code,
            "tower": unit_obj.tower if unit_obj else norm_unit.split("-")[0],
            "unit_number": unit_obj.unit_number if unit_obj else None,
            "period_date": bill.period_date,
            "period_year": p_year,
            "period_month": p_month,
            "alicuota_total": alicuota_val,
            "calculated_total_month": int(bill.calculated_total_month),
            "calculated_dept_subtotal": int(bill.calculated_dept_subtotal),
            "calculated_consumo_total": int(bill.calculated_consumo_total),
            "calculated_unit_subtotal": int(bill.calculated_unit_subtotal),
            "last_payment_date": bill.last_payment_date.isoformat() if bill.last_payment_date else None,
            "last_payment_amount": int(bill.last_payment_amount) if bill.last_payment_amount is not None else None,
            "concepts": concepts_list,
            "consumptions": consumptions_list,
            "unit_charges": charges_list,
        }
    except Exception as exc:
        logger.warning("Error querying unit bill details from DB for '%s': %s", norm_unit, exc)
        return {"error": f"Database error fetching unit bill: {exc}"}
    finally:
        if should_close:
            db.close()
