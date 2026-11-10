"""Multimodal PDF extractor for Chilean condominium financial reports with dual-model failover,
self-healing cross-validation, and PostgreSQL 3NF persistence.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
import logging
import os
from pathlib import Path
import time
from typing import Tuple

from google import genai
from google.genai import types
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.extractor.ground_truth import (
    GroundTruthData,
    UnitGroundTruthData,
    extract_ground_truth,
    extract_unit_ground_truth,
)
from backend.app.extractor.rate_limiter import DualModelRateLimiter
from backend.app.extractor.schemas import (
    ExpenseReport,
    ExtractionMetadata,
    UnitBillReport,
    ValidationResult,
    UnitValidationResult,
    validate_expense_report,
    validate_unit_bill_report,
)
from backend.app.models import (
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

logger = logging.getLogger("backend.extractor.extractor")

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(filename: str) -> str:
    path = _PROMPT_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"Prompt template file not found: {path}")
    return path.read_text(encoding="utf-8")


def get_gemini_client() -> genai.Client:
    """Create and return a configured Google GenAI client."""
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Please set it in your environment or Secret Manager."
        )
    return genai.Client(api_key=api_key)


def _dmy_to_iso(dmy_str: str) -> str:
    """Convert '01/MM/YYYY' to 'YYYY-MM-01'."""
    parts = dmy_str.strip().split("/")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return dmy_str


def _parse_dmy_date(dmy_str: str | None) -> date | None:
    """Convert 'DD/MM/YYYY' or 'YYYY-MM-DD' to datetime.date object."""
    if not dmy_str:
        return None
    dmy_clean = dmy_str.strip().replace("-", "/")
    parts = dmy_clean.split("/")
    if len(parts) == 3:
        if len(parts[0]) == 4:  # YYYY/MM/DD
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        return date(int(parts[2]), int(parts[1]), int(parts[0]))
    return None


def format_error_message(exc: Exception) -> str:
    """Format exceptions into clean, human-readable messages without raw nested dict dumps."""
    msg = str(exc)
    if "503" in msg or "UNAVAILABLE" in msg:
        return "Model temporarily experiencing high demand (503 Service Unavailable). Backing off before retry."
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return "API rate limit reached (429 Quota Exceeded). Backing off."
    if "API_KEY_INVALID" in msg:
        return "Invalid Gemini API key provided. Please verify GEMINI_API_KEY in Secret Manager."
    if "404" in msg or "NOT_FOUND" in msg:
        return "Resource or model not found (404)."

    import re
    match = re.search(r"'message':\s*'([^']+)'", msg)
    if match:
        return match.group(1)
    match_double = re.search(r'"message":\s*"([^"]+)"', msg)
    if match_double:
        return match_double.group(1)

    if len(msg) > 150:
        return msg[:147] + "..."
    return msg


class CondominiumExtractor:
    """Orchestrates resilient multimodal extraction, self-healing validation, and database persistence."""

    def __init__(
        self,
        client: genai.Client | None = None,
        rate_limiter: DualModelRateLimiter | None = None,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        max_retries: int = 2,
    ):
        self._client = client
        self.primary_model = primary_model or os.getenv("DEFAULT_MODEL", "gemini-3.5-flash-lite")
        self.fallback_model = fallback_model or os.getenv("FALLBACK_MODEL", "gemini-3.1-flash-lite")
        self.models = [self.primary_model, self.fallback_model]
        self.rate_limiter = rate_limiter or DualModelRateLimiter(total_rpm=20, models=self.models)
        self.max_retries = max_retries

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = get_gemini_client()
        return self._client

    # =========================================================================
    # Building Expense Report Extraction & Self-Healing
    # =========================================================================

    async def extract_building_report(
        self,
        pdf_bytes: bytes,
    ) -> Tuple[ExpenseReport, ExtractionMetadata, GroundTruthData, ValidationResult]:
        """Extract building expenses from PDF bytes with ground-truth validation and self-healing."""
        ground_truth = extract_ground_truth(pdf_bytes)
        system_instruction = _load_prompt("extractor_system.txt")
        self_heal_template = _load_prompt("self_heal_feedback.txt")

        feedback_prompt: str | None = None
        best_report: ExpenseReport | None = None
        best_val: ValidationResult | None = None
        best_metadata: ExtractionMetadata | None = None

        for attempt in range(self.max_retries + 1):
            chosen_model = await self.rate_limiter.wait_and_get_model(self.models)
            fallback_triggered = (chosen_model != self.primary_model)

            user_message = (
                "Please extract all categorical line items from this building expense report. "
                "Strictly adhere to the extraction rules, normalize the billing period date to 01/MM/YYYY, "
                "extract clean base names, output values as integer CLP, and map 'Gas Cargo Comun al 30%' under 'Consumos'."
            )
            contents: list[types.Part | str] = [
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                user_message,
            ]
            if feedback_prompt:
                contents.append(f"ATTENTION - PREVIOUS EXTRACTION ERRORS DETECTED:\n{feedback_prompt}")

            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExpenseReport,
                system_instruction=system_instruction,
                temperature=0.0,
            )

            try:
                start_time = time.perf_counter()
                response = self.client.models.generate_content(
                    model=chosen_model,
                    contents=contents,
                    config=config,
                )
                latency = time.perf_counter() - start_time

                if not response.text:
                    raise RuntimeError(f"Model '{chosen_model}' returned empty text.")

                report = ExpenseReport.model_validate_json(response.text)
                val_result = validate_expense_report(report, ground_truth)

                metadata = ExtractionMetadata(
                    model_used=chosen_model,
                    fallback_triggered=fallback_triggered,
                    latency_seconds=round(latency, 2),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

                best_report = report
                best_val = val_result
                best_metadata = metadata

                if val_result.is_valid:
                    logger.info(
                        "Building report validation successful on attempt %d with model %s.",
                        attempt + 1,
                        chosen_model,
                    )
                    return report, metadata, ground_truth, val_result

                # Discrepancy detected -> build feedback prompt for self-healing
                logger.warning(
                    "Building report validation errors on attempt %d: %s. Initiating self-heal.",
                    attempt + 1,
                    val_result.errors,
                )
                feedback_prompt = self_heal_template.format(
                    validation_errors="\n- " + "\n- ".join(val_result.errors)
                )

            except Exception as exc:
                clean_err = format_error_message(exc)
                logger.warning(
                    "Building extraction attempt %d failed with model %s: %s",
                    attempt + 1,
                    chosen_model,
                    clean_err,
                )
                if "429" in str(exc) or "ResourceExhausted" in type(exc).__name__:
                    self.rate_limiter.report_rate_limit(chosen_model, backoff_seconds=30.0)
                    await asyncio.sleep(2.0)
                elif "503" in str(exc) or "UNAVAILABLE" in str(exc):
                    await asyncio.sleep(3.0 * (attempt + 1))

                if attempt == self.max_retries and best_report is None:
                    raise RuntimeError(clean_err) from exc

        return best_report, best_metadata, ground_truth, best_val  # type: ignore[return-value]

    # =========================================================================
    # Unit Bill Report Extraction & Self-Healing
    # =========================================================================

    async def extract_unit_bill(
        self,
        pdf_bytes: bytes,
    ) -> Tuple[UnitBillReport, ExtractionMetadata, UnitGroundTruthData, UnitValidationResult]:
        """Extract individual unit bill ('Nota de Cobro') from PDF bytes with self-healing."""
        ground_truth = extract_unit_ground_truth(pdf_bytes)
        system_instruction = _load_prompt("unit_extractor_system.txt")
        self_heal_template = _load_prompt("unit_self_heal_feedback.txt")

        feedback_prompt: str | None = None
        best_report: UnitBillReport | None = None
        best_val: UnitValidationResult | None = None
        best_metadata: ExtractionMetadata | None = None

        for attempt in range(self.max_retries + 1):
            chosen_model = await self.rate_limiter.wait_and_get_model(self.models)
            fallback_triggered = (chosen_model != self.primary_model)

            user_message = (
                "Please extract the unit billing statement ('Nota de Cobro') metadata and all three tables "
                "(Tabla Conceptos, Tabla Consumos Generales, and Tabla Gastos por Unidad). "
                "Strictly adhere to the extraction rules, normalize issue_date to 01/MM/YYYY, "
                "last_payment_date to DD/MM/YYYY, output CLP amounts as integers, and readings/rates/alicuota as floats."
            )
            contents: list[types.Part | str] = [
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                user_message,
            ]
            if feedback_prompt:
                contents.append(f"ATTENTION - PREVIOUS EXTRACTION ERRORS DETECTED:\n{feedback_prompt}")

            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=UnitBillReport,
                system_instruction=system_instruction,
                temperature=0.0,
            )

            try:
                start_time = time.perf_counter()
                response = self.client.models.generate_content(
                    model=chosen_model,
                    contents=contents,
                    config=config,
                )
                latency = time.perf_counter() - start_time

                if not response.text:
                    raise RuntimeError(f"Model '{chosen_model}' returned empty text.")

                report = UnitBillReport.model_validate_json(response.text)
                # Overwrite unit dimension defaults from ground truth if model missed them
                if ground_truth.unit_code and not report.unit_code:
                    report.unit_code = ground_truth.unit_code
                if ground_truth.unit_tower and not report.unit_tower:
                    report.unit_tower = ground_truth.unit_tower
                if ground_truth.unit_number and not report.unit_number:
                    report.unit_number = ground_truth.unit_number

                val_result = validate_unit_bill_report(report, ground_truth)

                metadata = ExtractionMetadata(
                    model_used=chosen_model,
                    fallback_triggered=fallback_triggered,
                    latency_seconds=round(latency, 2),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

                best_report = report
                best_val = val_result
                best_metadata = metadata

                if val_result.is_valid:
                    logger.info(
                        "Unit bill validation successful on attempt %d with model %s.",
                        attempt + 1,
                        chosen_model,
                    )
                    return report, metadata, ground_truth, val_result

                logger.warning(
                    "Unit bill validation errors on attempt %d: %s. Initiating self-heal.",
                    attempt + 1,
                    val_result.errors,
                )
                feedback_prompt = self_heal_template.format(
                    validation_errors="\n- " + "\n- ".join(val_result.errors)
                )

            except Exception as exc:
                clean_err = format_error_message(exc)
                logger.warning(
                    "Unit bill extraction attempt %d failed with model %s: %s",
                    attempt + 1,
                    chosen_model,
                    clean_err,
                )
                if "429" in str(exc) or "ResourceExhausted" in type(exc).__name__:
                    self.rate_limiter.report_rate_limit(chosen_model, backoff_seconds=30.0)
                    await asyncio.sleep(2.0)
                elif "503" in str(exc) or "UNAVAILABLE" in str(exc):
                    await asyncio.sleep(3.0 * (attempt + 1))

                if attempt == self.max_retries and best_report is None:
                    raise RuntimeError(clean_err) from exc

        return best_report, best_metadata, ground_truth, best_val  # type: ignore[return-value]

    # =========================================================================
    # PostgreSQL 3NF Persistence
    # =========================================================================

    def save_building_report_to_db(
        self,
        db: Session,
        report: ExpenseReport,
        file_uri: str,
        metadata: ExtractionMetadata,
        ground_truth: GroundTruthData,
    ) -> ExpenseReportModel:
        """Persist structured building expense report into 3NF schema."""
        period_iso = _dmy_to_iso(report.issue_date)  # YYYY-MM-01
        parts = period_iso.split("-")
        year = int(parts[0])
        month = int(parts[1])

        # 1. Ensure billing period master record exists
        period_record = db.scalar(
            select(BillingPeriodModel).where(BillingPeriodModel.period_date == period_iso)
        )
        if not period_record:
            period_record = BillingPeriodModel(
                period_date=period_iso,
                period_year=year,
                period_month=month,
            )
            db.add(period_record)
            db.flush()

        # 2. Check for existing expense report (idempotency / update)
        existing_report = db.scalar(
            select(ExpenseReportModel).where(ExpenseReportModel.period_date == period_iso)
        )
        if existing_report:
            db.delete(existing_report)
            db.flush()

        # 3. Calculate category and report totals
        calc_operational = sum(
            sum(item.value for item in sec.sub_topics)
            for sec in report.sections
            if sec.expense_type == "operational"
        )
        calc_fondos = sum(
            sum(item.value for item in sec.sub_topics)
            for sec in report.sections
            if sec.expense_type == "fondos"
        )
        grand_total = calc_operational + calc_fondos

        telemetry = {
            "model_used": metadata.model_used,
            "fallback_triggered": metadata.fallback_triggered,
            "latency_seconds": metadata.latency_seconds,
            "timestamp": metadata.timestamp,
            "declared_operational": ground_truth.operational_total,
            "declared_fondos": ground_truth.fondos_total,
            "declared_grand": ground_truth.grand_total,
        }

        report_model = ExpenseReportModel(
            period_date=period_iso,
            operational_total=calc_operational,
            fondos_total=calc_fondos,
            calculated_grand_total=grand_total,
            source_file_path=file_uri,
            telemetry_metadata=telemetry,
        )
        db.add(report_model)
        db.flush()

        # 4. Insert sections and line items
        for sec_idx, sec in enumerate(report.sections):
            sec_subtotal = sum(item.value for item in sec.sub_topics)
            section_model = ExpenseSectionModel(
                report_id=report_model.id,
                category_name=sec.general_topic.strip(),
                expense_type=sec.expense_type,
                calculated_subtotal=sec_subtotal,
                display_order=sec_idx,
            )
            db.add(section_model)
            db.flush()

            for item_idx, item in enumerate(sec.sub_topics):
                item_model = ExpenseItemModel(
                    section_id=section_model.id,
                    name=item.name.strip(),
                    normalized_name=item.name.strip().lower(),
                    amount=item.value,
                    display_order=item_idx,
                )
                db.add(item_model)

        db.commit()
        db.refresh(report_model)
        logger.info(
            "Persisted ExpenseReport for period %s (ID: %s, Total: $%s CLP).",
            period_iso,
            report_model.id,
            f"{grand_total:,}",
        )
        return report_model

    def save_unit_bill_to_db(
        self,
        db: Session,
        report: UnitBillReport,
        file_uri: str,
        metadata: ExtractionMetadata,
        ground_truth: UnitGroundTruthData,
    ) -> UnitBillModel:
        """Persist structured unit billing statement ('Nota de Cobro') into 3NF schema."""
        period_iso = _dmy_to_iso(report.issue_date)
        parts = period_iso.split("-")
        year = int(parts[0])
        month = int(parts[1])

        # 1. Ensure billing period master record exists
        period_record = db.scalar(
            select(BillingPeriodModel).where(BillingPeriodModel.period_date == period_iso)
        )
        if not period_record:
            period_record = BillingPeriodModel(
                period_date=period_iso,
                period_year=year,
                period_month=month,
            )
            db.add(period_record)
            db.flush()

        # 2. Ensure unit master record exists and auto-populate alicuota if missing
        unit_code = report.unit_code.strip()
        unit_record = db.scalar(select(UnitModel).where(UnitModel.unit_code == unit_code))
        if not unit_record:
            # Create unit if not seeded yet
            unit_record = UnitModel(
                unit_code=unit_code,
                tower=report.unit_tower or "B",
                unit_number=report.unit_number or int(unit_code.split("-")[-1]),
                alicuota=Decimal(str(report.alicuota_total)),
            )
            db.add(unit_record)
            db.flush()
        elif unit_record.alicuota is None and report.alicuota_total:
            # Auto-populate official proration share on first bill extraction
            unit_record.alicuota = Decimal(str(report.alicuota_total))
            db.flush()
            logger.info("Auto-populated official alicuota %.5f for unit %s.", report.alicuota_total, unit_code)

        # 3. Check for existing unit bill (idempotency / update)
        existing_bill = db.scalar(
            select(UnitBillModel).where(
                UnitBillModel.unit_code == unit_code,
                UnitBillModel.period_date == period_iso,
            )
        )
        if existing_bill:
            db.delete(existing_bill)
            db.flush()

        calc_dept = sum(c.amount for c in report.concepts)
        calc_consumo = sum(c.total for c in report.consumptions)
        calc_unit_charges = sum(u.amount for u in report.unit_charges)
        total_month = calc_dept + calc_consumo + calc_unit_charges

        telemetry = {
            "model_used": metadata.model_used,
            "fallback_triggered": metadata.fallback_triggered,
            "latency_seconds": metadata.latency_seconds,
            "timestamp": metadata.timestamp,
            "declared_total": ground_truth.declared_total_month,
        }

        bill_model = UnitBillModel(
            unit_code=unit_code,
            period_date=period_iso,
            last_payment_date=_parse_dmy_date(report.last_payment_date),
            last_payment_amount=report.last_payment_amount,
            calculated_dept_subtotal=calc_dept,
            calculated_consumo_total=calc_consumo,
            calculated_unit_subtotal=calc_unit_charges,
            calculated_total_month=total_month,
            source_file_path=file_uri,
            telemetry_metadata=telemetry,
        )
        db.add(bill_model)
        db.flush()

        # 4. Insert Concepts
        for idx, concept in enumerate(report.concepts):
            c_model = UnitBillConceptModel(
                unit_bill_id=bill_model.id,
                concept_name=concept.concept.strip(),
                normalized_name=concept.concept.strip().lower(),
                detail=concept.detail,
                amount=concept.amount,
                display_order=idx,
            )
            db.add(c_model)

        # 5. Insert Consumptions
        for idx, cons in enumerate(report.consumptions):
            cons_model = UnitBillConsumptionModel(
                unit_bill_id=bill_model.id,
                item_name=cons.item_name.strip(),
                normalized_name=cons.item_name.strip().lower(),
                previous_reading=Decimal(str(cons.previous_reading)),
                current_reading=Decimal(str(cons.current_reading)),
                consumption=Decimal(str(cons.consumption)),
                unit_rate=Decimal(str(cons.unit_rate)),
                total_amount=cons.total,
                display_order=idx,
            )
            db.add(cons_model)

        # 6. Insert Unit Charges
        for idx, charge in enumerate(report.unit_charges):
            ch_model = UnitBillChargeModel(
                unit_bill_id=bill_model.id,
                charge_name=charge.charge_name.strip(),
                normalized_name=charge.charge_name.strip().lower(),
                charge_type=charge.charge_type,
                amount=charge.amount,
                display_order=idx,
            )
            db.add(ch_model)

        db.commit()
        db.refresh(bill_model)
        logger.info(
            "Persisted UnitBill for %s in period %s (ID: %s, Total: $%s CLP).",
            unit_code,
            period_iso,
            bill_model.id,
            f"{total_month:,}",
        )
        return bill_model
