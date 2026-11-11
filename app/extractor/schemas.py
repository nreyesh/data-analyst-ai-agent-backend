"""Pydantic schemas and cross-validation for building and unit expense extractions."""

from __future__ import annotations

import re
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.extractor.ground_truth import (
    GroundTruthData,
    UnitGroundTruthData,
    parse_unit_dimensions,
)

ExpenseType = Literal["operational", "fondos"]
UnitChargeType = Literal["recargo", "fixed_charge"]


# ============================================================================
# Telemetry & Validation Results
# ============================================================================


class ExtractionMetadata(BaseModel):
    """Telemetry captured for each LLM extraction run."""

    model_used: str
    fallback_triggered: bool = False
    fallback_reason: str | None = None
    latency_seconds: float = 0.0
    timestamp: str = ""


class ValidationResult(BaseModel):
    """Cross-validation outcome with detected discrepancies."""

    is_valid: bool
    errors: list[str] = Field(default_factory=list)


UnitValidationResult = ValidationResult


def _check_diff(calc: int, declared: int | None, label: str, errors: list[str]) -> None:
    if declared is not None and calc != declared:
        errors.append(
            f"{label} mismatch: calc ${calc:,}, declared ${declared:,} (diff: {calc - declared:+,} CLP)."
        )


# ============================================================================
# Building Expense Report Schemas
# ============================================================================


class SubTopic(BaseModel):
    """Lowest-level expense item within a building report section."""

    name: str = Field(description="Clean base name of the item stripped of voucher/invoice info.")
    value: int = Field(description="Monetary integer value in CLP (negative for credit notes).")


class Section(BaseModel):
    """Categorical section containing individual sub-topic line items."""

    general_topic: str
    expense_type: ExpenseType = "operational"
    sub_topics: list[SubTopic] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_expense_type(self) -> Section:
        self.expense_type = "fondos" if self.general_topic.strip().lower() == "fondos" else "operational"
        return self


class ExpenseReport(BaseModel):
    """Complete structured building expense report."""

    issue_date: str = Field(description="Normalized billing period date strictly 01/MM/YYYY.")
    sections: list[Section] = Field(default_factory=list)

    @field_validator("issue_date")
    @classmethod
    def validate_issue_date(cls, v: str) -> str:
        if not re.match(r"^01/\d{2}/\d{4}$", v.strip()):
            raise ValueError(f"issue_date must be strictly '01/MM/YYYY', received: {v!r}")
        return v.strip()

    @model_validator(mode="after")
    def consolidate_duplicate_sections(self) -> ExpenseReport:
        consolidated: dict[str, Section] = {}
        for sec in self.sections:
            topic = sec.general_topic.strip()
            if topic in consolidated:
                consolidated[topic].sub_topics.extend(sec.sub_topics)
            else:
                consolidated[topic] = Section(
                    general_topic=topic,
                    expense_type=sec.expense_type,
                    sub_topics=list(sec.sub_topics),
                )
        self.sections = list(consolidated.values())
        return self


def validate_expense_report(report: ExpenseReport, ground_truth: GroundTruthData) -> ValidationResult:
    """Validate an extracted ExpenseReport against ground-truth declared totals."""
    errors: list[str] = []

    if ground_truth.period_date and report.issue_date.strip() != ground_truth.period_date.strip():
        errors.append(
            f"Billing period date mismatch: extracted '{report.issue_date}', declared '{ground_truth.period_date}'."
        )

    extracted_map = {
        sec.general_topic.strip(): sum(item.value for item in sec.sub_topics)
        for sec in report.sections
    }
    for topic, expected in ground_truth.section_totals.items():
        if topic not in extracted_map:
            errors.append(f"Missing section '{topic}': declared subtotal ${expected:,} CLP not found.")
        else:
            _check_diff(extracted_map[topic], expected, f"Section '{topic}' sum", errors)

    if ground_truth.section_totals:
        for topic in extracted_map:
            if topic not in ground_truth.section_totals and topic.lower() != "fondos":
                errors.append(f"Unexpected section '{topic}': not declared in ground truth report.")

    calc_op = sum(sum(item.value for item in s.sub_topics) for s in report.sections if s.expense_type == "operational")
    calc_fondos = sum(sum(item.value for item in s.sub_topics) for s in report.sections if s.expense_type == "fondos")

    _check_diff(calc_op, ground_truth.operational_total, "Operational total", errors)
    _check_diff(calc_fondos, ground_truth.fondos_total, "Fondos total", errors)
    _check_diff(calc_op + calc_fondos, ground_truth.grand_total, "Grand total", errors)

    return ValidationResult(is_valid=len(errors) == 0, errors=errors)


# ============================================================================
# Unit Billing Statement ("Nota de Cobro") Schemas
# ============================================================================


class ConceptItem(BaseModel):
    """Line item from the 'Concepto / Detalle / Monto a pagar' table."""

    concept: str
    detail: str | None = None
    amount: int


class ConsumptionItem(BaseModel):
    """Row from the 'Consumos Generales' table."""

    item_name: str
    previous_reading: float
    current_reading: float
    consumption: float
    unit_rate: float
    total: int


class UnitChargeItem(BaseModel):
    """Row from the 'Gastos por Unidad' table."""

    charge_name: str
    charge_type: UnitChargeType = "fixed_charge"
    amount: int


class UnitBillReport(BaseModel):
    """Structured unit billing statement ('Nota de Cobro')."""

    issue_date: str
    unit_code: str = ""
    unit_tower: str = "B"
    unit_number: int = 0
    alicuota_total: float
    last_payment_date: str | None = None
    last_payment_amount: int | None = None
    concepts: list[ConceptItem] = Field(default_factory=list)
    consumptions: list[ConsumptionItem] = Field(default_factory=list)
    unit_charges: list[UnitChargeItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def populate_unit_dimensions(cls, data: Any) -> Any:
        if isinstance(data, dict):
            code = str(data.get("unit_code") or "").strip().upper()
            tower = data.get("unit_tower")
            number = data.get("unit_number")
            if (not tower or not number) and code:
                t, n = parse_unit_dimensions(code)
                tower = tower or t
                number = number if number is not None else n
            if tower and number and not code:
                code = f"{str(tower).upper().strip()}-{int(number)}"
            if tower:
                data["unit_tower"] = str(tower).upper().strip()
            if number is not None:
                data["unit_number"] = int(number)
            if code:
                data["unit_code"] = code
        return data

    @field_validator("issue_date")
    @classmethod
    def validate_issue_date(cls, v: str) -> str:
        if not re.match(r"^01/\d{2}/\d{4}$", v.strip()):
            raise ValueError(f"issue_date must be strictly '01/MM/YYYY', received: {v!r}")
        return v.strip()

    @field_validator("last_payment_date")
    @classmethod
    def validate_last_payment_date(cls, v: str | None) -> str | None:
        if not v:
            return None
        v_clean = v.strip().replace("-", "/")
        if not re.match(r"^\d{2}/\d{2}/\d{4}$", v_clean):
            raise ValueError(f"last_payment_date must be 'DD/MM/YYYY', received: {v!r}")
        return v_clean


def validate_unit_bill_report(report: UnitBillReport, ground_truth: UnitGroundTruthData) -> ValidationResult:
    """Validate extracted UnitBillReport against declared ground-truth totals."""
    errors: list[str] = []

    if ground_truth.period_date and report.issue_date.strip() != ground_truth.period_date.strip():
        errors.append(f"Period mismatch: extracted '{report.issue_date}', declared '{ground_truth.period_date}'.")

    calc_dept = sum(c.amount for c in report.concepts)
    calc_consumo = sum(c.total for c in report.consumptions)
    calc_recargos = sum(u.amount for u in report.unit_charges if u.charge_type == "recargo")
    calc_total = calc_dept + calc_consumo + sum(u.amount for u in report.unit_charges)

    _check_diff(calc_dept, ground_truth.declared_dept_subtotal, "Dept subtotal", errors)
    _check_diff(calc_consumo, ground_truth.declared_consumo_subtotal, "Consumo subtotal", errors)
    _check_diff(calc_recargos, ground_truth.declared_recargos_subtotal, "Recargos subtotal", errors)
    _check_diff(calc_total, ground_truth.declared_total_month, "Total month", errors)

    return ValidationResult(is_valid=len(errors) == 0, errors=errors)
