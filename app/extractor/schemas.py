"""Pydantic schemas and cross-validation for building and unit expense extractions."""

from __future__ import annotations

import re
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.extractor.ground_truth import GroundTruthData, UnitGroundTruthData

ExpenseType = Literal["operational", "fondos"]
UnitChargeType = Literal["recargo", "fixed_charge"]


# ============================================================================
# Telemetry Metadata
# ============================================================================


class ExtractionMetadata(BaseModel):
    """Telemetry captured for each LLM extraction run."""

    model_used: str
    fallback_triggered: bool = False
    fallback_reason: str | None = None
    latency_seconds: float = 0.0
    timestamp: str = ""


# ============================================================================
# Building Expense Report Schemas
# ============================================================================


class SubTopic(BaseModel):
    """Lowest-level expense item within a building report section."""

    name: str = Field(
        description="Clean base name of the item or sub-topic, stripped of voucher/invoice parentheses."
    )
    value: int = Field(
        description="Monetary integer value in Chilean Pesos (CLP). May be negative for credit notes."
    )


class Section(BaseModel):
    """Categorical section containing individual sub-topic line items."""

    general_topic: str = Field(
        description="Category name (e.g. Consumos, Remuneraciones, Mantenciones y Servicios, Fondos)."
    )
    expense_type: ExpenseType = Field(
        default="operational",
        description="Expense classification: 'fondos' when general_topic is 'Fondos', otherwise 'operational'.",
    )
    sub_topics: list[SubTopic] = Field(
        default_factory=list,
        description="Individual items and their values belonging to this category.",
    )

    @model_validator(mode="after")
    def enforce_expense_type(self) -> Section:
        if self.general_topic.strip().lower() == "fondos":
            self.expense_type = "fondos"
        else:
            self.expense_type = "operational"
        return self


class ExpenseReport(BaseModel):
    """Complete structured building expense report."""

    issue_date: str = Field(
        description="Expense billing period date normalized strictly to 01/MM/YYYY (e.g. 01/09/2026 for September 2026)."
    )
    sections: list[Section] = Field(
        default_factory=list,
        description="All expense sections and their line items.",
    )

    @field_validator("issue_date")
    @classmethod
    def validate_issue_date(cls, v: str) -> str:
        if not re.match(r"^01/\d{2}/\d{4}$", v.strip()):
            raise ValueError(f"issue_date must be strictly in '01/MM/YYYY' format, received: {v!r}")
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


class SectionValidation(BaseModel):
    """Validation report for a single section."""

    general_topic: str
    calculated_sum: int
    declared_subtotal: int | None
    difference: int
    is_valid: bool


class ValidationResult(BaseModel):
    """Overall cross-validation outcome for an expense report."""

    is_valid: bool
    calculated_operational_total: int = 0
    declared_operational_total: int | None = None
    operational_difference: int = 0
    calculated_fondos_total: int = 0
    declared_fondos_total: int | None = None
    fondos_difference: int = 0
    calculated_grand_total: int = 0
    declared_grand_total: int | None = None
    grand_total_difference: int = 0
    section_validations: list[SectionValidation] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def validate_expense_report(report: ExpenseReport, ground_truth: GroundTruthData) -> ValidationResult:
    """Validate an extracted ExpenseReport against ground-truth declared totals."""
    errors: list[str] = []
    section_validations: list[SectionValidation] = []

    # 1. Billing period date check
    if ground_truth.period_date is not None:
        if report.issue_date.strip() != ground_truth.period_date.strip():
            errors.append(
                f"Billing period date mismatch: extracted '{report.issue_date}', declared '{ground_truth.period_date}'."
            )

    extracted_sections_map = {sec.general_topic.strip(): sec for sec in report.sections}

    # 2. Section level checks
    for section in report.sections:
        topic_name = section.general_topic.strip()
        calc_sum = sum(item.value for item in section.sub_topics)
        declared_subtotal = ground_truth.section_totals.get(topic_name)

        if declared_subtotal is not None:
            diff = calc_sum - declared_subtotal
            sec_valid = (diff == 0)
            if not sec_valid:
                errors.append(
                    f"Section '{topic_name}' sum mismatch: items sum to ${calc_sum:,} CLP, "
                    f"declared is ${declared_subtotal:,} CLP (difference: {diff:+,} CLP)."
                )
        else:
            diff = 0
            sec_valid = True

        section_validations.append(
            SectionValidation(
                general_topic=topic_name,
                calculated_sum=calc_sum,
                declared_subtotal=declared_subtotal,
                difference=diff,
                is_valid=sec_valid,
            )
        )

    # 3. Missing declared sections
    for declared_topic, expected_amt in ground_truth.section_totals.items():
        if declared_topic not in extracted_sections_map:
            errors.append(
                f"Missing section '{declared_topic}': declared subtotal ${expected_amt:,} CLP not found in extraction."
            )
            section_validations.append(
                SectionValidation(
                    general_topic=declared_topic,
                    calculated_sum=0,
                    declared_subtotal=expected_amt,
                    difference=-expected_amt,
                    is_valid=False,
                )
            )

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
    calc_grand = calc_operational + calc_fondos

    diff_op = calc_operational - (ground_truth.operational_total or calc_operational)
    diff_fondos = calc_fondos - (ground_truth.fondos_total or calc_fondos)
    diff_grand = calc_grand - (ground_truth.grand_total or calc_grand)

    if ground_truth.operational_total is not None and diff_op != 0:
        errors.append(f"Operational total mismatch: calc ${calc_operational:,}, declared ${ground_truth.operational_total:,}.")
    if ground_truth.fondos_total is not None and diff_fondos != 0:
        errors.append(f"Fondos total mismatch: calc ${calc_fondos:,}, declared ${ground_truth.fondos_total:,}.")
    if ground_truth.grand_total is not None and diff_grand != 0:
        errors.append(f"Grand total mismatch: calc ${calc_grand:,}, declared ${ground_truth.grand_total:,}.")

    is_valid = len(errors) == 0

    return ValidationResult(
        is_valid=is_valid,
        calculated_operational_total=calc_operational,
        declared_operational_total=ground_truth.operational_total,
        operational_difference=diff_op,
        calculated_fondos_total=calc_fondos,
        declared_fondos_total=ground_truth.fondos_total,
        fondos_difference=diff_fondos,
        calculated_grand_total=calc_grand,
        declared_grand_total=ground_truth.grand_total,
        grand_total_difference=diff_grand,
        section_validations=section_validations,
        errors=errors,
    )


# ============================================================================
# Unit Billing Statement ("Nota de Cobro") Schemas
# ============================================================================


class ConceptItem(BaseModel):
    """Line item from the 'Concepto / Detalle / Monto a pagar' table."""

    concept: str = Field(description="Clean concept name (e.g. 'Gasto Común', 'Fondo de Reserva 5%').")
    detail: str | None = Field(default=None, description="Detail string (e.g. '0,16736 %') or None.")
    amount: int = Field(description="Payable amount in integer CLP.")


class ConsumptionItem(BaseModel):
    """Row from the 'Consumos Generales' table."""

    item_name: str = Field(description="Name of meter / item (e.g. 'Torre B').")
    previous_reading: float = Field(description="Lectura Anterior")
    current_reading: float = Field(description="Lectura Actual")
    consumption: float = Field(description="Consumos delta")
    unit_rate: float = Field(description="Valor unitario")
    total: int = Field(description="Total CLP")


class UnitChargeItem(BaseModel):
    """Row from the 'Gastos por Unidad' table."""

    charge_name: str = Field(description="Charge or fine name.")
    charge_type: UnitChargeType = Field(default="fixed_charge")
    amount: int = Field(description="Amount in integer CLP.")


class UnitBillReport(BaseModel):
    """Structured unit billing statement ('Nota de Cobro')."""

    issue_date: str = Field(description="Normalized billing period date 01/MM/YYYY.")
    unit_code: str = Field(default="", description="Apartment code e.g. 'B-504'.")
    unit_tower: str = Field(default="B", description="Tower e.g. 'B'.")
    unit_number: int = Field(default=0, description="Unit number e.g. 504.")
    alicuota_total: float = Field(description="Prorate percentage as float (e.g. 0.16736).")
    last_payment_date: str | None = Field(default=None, description="DD/MM/YYYY or None.")
    last_payment_amount: int | None = Field(default=None, description="Integer CLP or None.")
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
                m1 = re.match(r"^([A-Za-z]+)\s*[-]?\s*(\d+)$", code)
                m2 = re.match(r"^(\d+)\s*[-]?\s*([A-Za-z]+)$", code)
                if m1:
                    tower = tower or m1.group(1).upper()
                    number = number if number is not None else int(m1.group(2))
                elif m2:
                    tower = tower or m2.group(2).upper()
                    number = number if number is not None else int(m2.group(1))
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
            raise ValueError(f"issue_date must be strictly in '01/MM/YYYY' format, received: {v!r}")
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


class UnitValidationResult(BaseModel):
    """Validation report for an individual unit billing statement."""

    is_valid: bool
    calculated_dept_subtotal: int = 0
    declared_dept_subtotal: int | None = None
    dept_subtotal_difference: int = 0
    calculated_consumo_subtotal: int = 0
    declared_consumo_subtotal: int | None = None
    consumo_subtotal_difference: int = 0
    calculated_recargos_subtotal: int = 0
    declared_recargos_subtotal: int | None = None
    recargos_subtotal_difference: int = 0
    calculated_unit_subtotal: int = 0
    calculated_total_month: int = 0
    declared_total_month: int | None = None
    total_month_difference: int = 0
    errors: list[str] = Field(default_factory=list)


def validate_unit_bill_report(
    report: UnitBillReport, ground_truth: UnitGroundTruthData
) -> UnitValidationResult:
    """Validate extracted UnitBillReport against declared ground-truth totals."""
    errors: list[str] = []

    # 1. Check issue date
    if ground_truth.period_date and report.issue_date.strip() != ground_truth.period_date.strip():
        errors.append(
            f"Period mismatch: extracted '{report.issue_date}', declared '{ground_truth.period_date}'."
        )

    # 2. Check concept sum vs Subtotal Departamento
    calc_dept = sum(c.amount for c in report.concepts)
    diff_dept = calc_dept - (ground_truth.declared_dept_subtotal or calc_dept)
    if ground_truth.declared_dept_subtotal is not None and diff_dept != 0:
        errors.append(
            f"Dept subtotal mismatch: calc ${calc_dept:,}, declared ${ground_truth.declared_dept_subtotal:,}."
        )

    # 3. Check consumptions sum vs Subtotal Consumo
    calc_consumo = sum(c.total for c in report.consumptions)
    diff_consumo = calc_consumo - (ground_truth.declared_consumo_subtotal or calc_consumo)
    if ground_truth.declared_consumo_subtotal is not None and diff_consumo != 0:
        errors.append(
            f"Consumo subtotal mismatch: calc ${calc_consumo:,}, declared ${ground_truth.declared_consumo_subtotal:,}."
        )

    # 4. Check recargos and unit charges
    calc_recargos = sum(u.amount for u in report.unit_charges if u.charge_type == "recargo")
    diff_recargos = calc_recargos - (ground_truth.declared_recargos_subtotal or calc_recargos)
    if ground_truth.declared_recargos_subtotal is not None and diff_recargos != 0:
        errors.append(
            f"Recargos mismatch: calc ${calc_recargos:,}, declared ${ground_truth.declared_recargos_subtotal:,}."
        )

    calc_unit_subtotal = sum(u.amount for u in report.unit_charges)
    calc_total_month = calc_dept + calc_consumo + calc_unit_subtotal
    diff_total = calc_total_month - (ground_truth.declared_total_month or calc_total_month)
    if ground_truth.declared_total_month is not None and diff_total != 0:
        errors.append(
            f"Total month mismatch: calc ${calc_total_month:,}, declared ${ground_truth.declared_total_month:,}."
        )

    is_valid = len(errors) == 0

    return UnitValidationResult(
        is_valid=is_valid,
        calculated_dept_subtotal=calc_dept,
        declared_dept_subtotal=ground_truth.declared_dept_subtotal,
        dept_subtotal_difference=diff_dept,
        calculated_consumo_subtotal=calc_consumo,
        declared_consumo_subtotal=ground_truth.declared_consumo_subtotal,
        consumo_subtotal_difference=diff_consumo,
        calculated_recargos_subtotal=calc_recargos,
        declared_recargos_subtotal=ground_truth.declared_recargos_subtotal,
        recargos_subtotal_difference=diff_recargos,
        calculated_unit_subtotal=calc_unit_subtotal,
        calculated_total_month=calc_total_month,
        declared_total_month=ground_truth.declared_total_month,
        total_month_difference=diff_total,
        errors=errors,
    )
