"""Deterministic ground-truth extraction from Kastor PDF text using PyMuPDF (fitz)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
import pymupdf as fitz

KNOWN_CATEGORIES: list[str] = [
    "Remuneraciones",
    "Consumos",
    "Mantenciones y Servicios",
    "Gas Cargo Comun al 30%",
    "Reparaciones y Repuestos",
    "Seguros",
    "Asesorías y Honorarios Profesionales",
    "Insumos",
    "Otros Gastos",
]

SPANISH_MONTH_MAP: dict[str, int] = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "setiembre": 9,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


@dataclass
class GroundTruthData:
    """Declared totals and period date extracted directly from building report PDF text."""

    period_date: str | None = None
    operational_total: int | None = None
    fondos_total: int | None = None
    grand_total: int | None = None
    section_totals: dict[str, int] = field(default_factory=dict)
    raw_section_totals: dict[str, int] = field(default_factory=dict)
    fondos_items: list[tuple[str, int]] = field(default_factory=list)


def _format_fondo_item_name(raw_label: str) -> str:
    """Format an uppercase PDF fund line label into clean title case."""
    words = raw_label.strip().split()
    formatted: list[str] = []
    lowercase_connectors = {"de", "del", "al", "y", "e", "en", "por", "para"}
    for idx, word in enumerate(words):
        lower_w = word.lower()
        if idx > 0 and lower_w in lowercase_connectors:
            formatted.append(lower_w)
        elif word.endswith("%"):
            formatted.append(word)
        else:
            formatted.append(word.capitalize())
    return " ".join(formatted)


def extract_ground_truth(pdf_source: str | Path | bytes) -> GroundTruthData:
    """Extract declared operational section subtotals, Fondos, and grand total from a building expense PDF.

    Args:
        pdf_source: Path to the PDF file or raw PDF bytes.

    Returns:
        GroundTruthData with operational_total, fondos_total, grand_total, and normalized section_totals.
    """
    if isinstance(pdf_source, (str, Path)):
        doc = fitz.open(str(pdf_source))
    elif isinstance(pdf_source, bytes):
        doc = fitz.open(stream=pdf_source, filetype="pdf")
    else:
        raise TypeError(f"Unsupported pdf_source type: {type(pdf_source)}")

    try:
        full_text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()

    result = GroundTruthData()

    # 0. Extract Billing Period (Informe Gastos Comunes <Mes> <Año>)
    m_period = re.search(
        r"Informe Gastos Comunes\s*[\r\n]+\s*([A-Za-z]+)\s+(\d{4})",
        full_text,
        re.IGNORECASE,
    )
    if m_period:
        month_name = m_period.group(1).lower().strip()
        year = int(m_period.group(2))
        month_num = SPANISH_MONTH_MAP.get(month_name)
        if month_num is not None:
            result.period_date = f"01/{month_num:02d}/{year:04d}"

    # 1. Extract Operational Total (TOTAL GASTOS COMUNES)
    m_op_total = re.search(
        r"TOTAL GASTOS COMUNES\s*[\r\n]+\s*(-?)\$(-?)([\d\.]+)", full_text
    )
    if m_op_total:
        sign = -1 if (m_op_total.group(1) == "-" or m_op_total.group(2) == "-") else 1
        result.operational_total = sign * int(m_op_total.group(3).replace(".", ""))

    # 2. Extract Operational Section Subtotals by text block ordering (before TOTAL GASTOS COMUNES)
    cat_patterns = [
        re.escape(c) + r"\s*$" for c in KNOWN_CATEGORIES if c != "Gas Cargo Comun al 30%"
    ]
    cat_patterns.append(r"Gas Cargo Comun al 30%(?:\s*\(.*?\))?\s*$")
    pattern = re.compile(r"^(?:" + "|".join(cat_patterns) + r")", re.MULTILINE)

    pos_end = full_text.find("TOTAL GASTOS COMUNES")
    effective_end = pos_end if pos_end != -1 else len(full_text)

    positions: list[tuple[int, str]] = []
    for m in pattern.finditer(full_text):
        if m.start() >= effective_end:
            continue
        line = m.group().strip()
        matched_cat = None
        for cat in KNOWN_CATEGORIES:
            if line.startswith(cat):
                matched_cat = cat
                break
        if matched_cat:
            positions.append((m.start(), matched_cat))

    positions.sort(key=lambda x: x[0])

    if pos_end != -1:
        positions.append((pos_end, "TOTAL GASTOS COMUNES"))
    else:
        positions.append((len(full_text), "END_OF_DOC"))

    raw_subtotals: dict[str, int] = {}
    for i in range(len(positions) - 1):
        start_pos, cat = positions[i]
        end_pos, _ = positions[i + 1]
        chunk = full_text[start_pos:end_pos]
        amounts = []
        for m in re.finditer(r"(-?)\$(-?)([\d\.]+)", chunk):
            sign = -1 if (m.group(1) == "-" or m.group(2) == "-") else 1
            amounts.append(sign * int(m.group(3).replace(".", "")))
        if amounts:
            raw_subtotals[cat] = raw_subtotals.get(cat, 0) + amounts[-1]

    result.raw_section_totals = dict(raw_subtotals)

    # 3. Normalize: Merge "Gas Cargo Comun al 30%" into "Consumos"
    normalized_totals: dict[str, int] = dict(raw_subtotals)
    gas_amount = normalized_totals.pop("Gas Cargo Comun al 30%", None)

    if gas_amount is not None:
        if "Consumos" in normalized_totals:
            normalized_totals["Consumos"] += gas_amount
        else:
            normalized_totals["Consumos"] = gas_amount

    # 4. Extract "Fondos" items (between TOTAL GASTOS COMUNES and standalone TOTAL) and final TOTAL
    if m_op_total:
        tail_text = full_text[m_op_total.end() :]
        m_final_total = re.search(
            r"^\s*TOTAL\s*[\r\n]+\s*(-?)\$(-?)([\d\.]+)",
            tail_text,
            re.MULTILINE,
        )
        if m_final_total:
            sign = (
                -1
                if (m_final_total.group(1) == "-" or m_final_total.group(2) == "-")
                else 1
            )
            result.grand_total = sign * int(m_final_total.group(3).replace(".", ""))

            fondos_block = tail_text[: m_final_total.start()]
            fondos_items: list[tuple[str, int]] = []
            for m_item in re.finditer(
                r"^\s*([^\r\n\$]+?)\s*[\r\n]+\s*(-?)\$(-?)([\d\.]+)",
                fondos_block,
                re.MULTILINE,
            ):
                raw_label = m_item.group(1).strip()
                item_sign = (
                    -1 if (m_item.group(2) == "-" or m_item.group(3) == "-") else 1
                )
                item_val = item_sign * int(m_item.group(4).replace(".", ""))
                fondos_items.append((_format_fondo_item_name(raw_label), item_val))

            result.fondos_items = fondos_items
            if fondos_items:
                fondos_sum = sum(val for _, val in fondos_items)
                result.fondos_total = fondos_sum
                normalized_totals["Fondos"] = fondos_sum
            else:
                result.fondos_total = 0
        else:
            result.grand_total = result.operational_total
            result.fondos_total = 0

    result.section_totals = normalized_totals
    return result


@dataclass
class UnitGroundTruthData:
    """Declared subtotals, header metadata, and totals extracted from a unit bill ('Nota de Cobro') PDF."""

    period_date: str | None = None
    unit_code: str | None = None
    unit_tower: str | None = None
    unit_number: int | None = None
    alicuota_total: float | None = None
    last_payment_date: str | None = None
    last_payment_amount: int | None = None
    declared_dept_subtotal: int | None = None
    declared_consumo_subtotal: int | None = None
    declared_recargos_subtotal: int | None = None
    declared_total_month: int | None = None


def _parse_clp_match(match: re.Match) -> int:
    """Parse signed CLP integer from regex groups."""
    sign = -1 if (match.group(1) == "-" or match.group(2) == "-") else 1
    return sign * int(match.group(3).replace(".", ""))


def extract_unit_ground_truth(pdf_source: str | Path | bytes) -> UnitGroundTruthData:
    """Extract declared header fields, section subtotals, and monthly total from a unit bill PDF."""
    if isinstance(pdf_source, (str, Path)):
        doc = fitz.open(str(pdf_source))
    elif isinstance(pdf_source, bytes):
        doc = fitz.open(stream=pdf_source, filetype="pdf")
    else:
        raise TypeError(f"Unsupported pdf_source type: {type(pdf_source)}")

    try:
        full_text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()

    result = UnitGroundTruthData()

    # 1. Billing Period & Unit Code ("Nota de Cobro Agosto 2026   B-504")
    m_header = re.search(
        r"Nota\s+de\s+Cobro\s+([A-Za-z]+)\s+(\d{4})(?:[ \t]+([^\r\n]+))?",
        full_text,
        re.IGNORECASE,
    )
    if m_header:
        month_name = m_header.group(1).lower().strip()
        year = int(m_header.group(2))
        month_num = SPANISH_MONTH_MAP.get(month_name)
        if month_num is not None:
            result.period_date = f"01/{month_num:02d}/{year:04d}"
        if m_header.group(3):
            unit_raw = m_header.group(3).strip()
            if unit_raw:
                result.unit_code = unit_raw
                m_dim = re.match(r"^([A-Za-z]+)\s*[-]?\s*(\d+)$", unit_raw) or re.match(
                    r"^(\d+)\s*[-]?\s*([A-Za-z]+)$", unit_raw
                )
                if m_dim:
                    if m_dim.group(1).isalpha():
                        result.unit_tower = m_dim.group(1).upper()
                        result.unit_number = int(m_dim.group(2))
                    else:
                        result.unit_number = int(m_dim.group(1))
                        result.unit_tower = m_dim.group(2).upper()

    # 2. Alícuota Total ("Alícuota Total \n 0,16736 %")
    m_alicuota = re.search(
        r"Al[ií]cuota\s+Total\s*[\r\n]+\s*([\d,\.]+)\s*%",
        full_text,
        re.IGNORECASE,
    )
    if m_alicuota:
        raw_num = m_alicuota.group(1).strip().replace(".", "").replace(",", ".")
        result.alicuota_total = float(raw_num)

    # 3. Fecha último Pago ("Fecha ultimo Pago \n 31-08-2026")
    m_last_date = re.search(
        r"Fecha\s+[uú]ltimo\s+Pago\s*[\r\n]+\s*(\d{2})[-/](\d{2})[-/](\d{4})",
        full_text,
        re.IGNORECASE,
    )
    if m_last_date:
        result.last_payment_date = (
            f"{m_last_date.group(1)}/{m_last_date.group(2)}/{m_last_date.group(3)}"
        )

    # 4. Monto último Pago ("Monto ultimo Pago \n $ 125.204")
    m_last_amt = re.search(
        r"Monto\s+[uú]ltimo\s+Pago\s*[\r\n]+\s*(-?)\$\s*(-?)([\d\.]+)",
        full_text,
        re.IGNORECASE,
    )
    if m_last_amt:
        result.last_payment_amount = _parse_clp_match(m_last_amt)

    # 5. Subtotal Departamento
    m_dept_sub = re.search(
        r"Subtotal\s+Departamento\s*[\r\n]+\s*(-?)\$\s*(-?)([\d\.]+)",
        full_text,
        re.IGNORECASE,
    )
    if m_dept_sub:
        result.declared_dept_subtotal = _parse_clp_match(m_dept_sub)

    # 6. Subtotal Consumo
    m_consumo_sub = re.search(
        r"Subtotal\s+Consumo\s*[\r\n]+\s*(-?)\$\s*(-?)([\d\.]+)",
        full_text,
        re.IGNORECASE,
    )
    if m_consumo_sub:
        result.declared_consumo_subtotal = _parse_clp_match(m_consumo_sub)

    # 7. Subtotal Recargos
    m_recargos_sub = re.search(
        r"Subtotal\s+Recargos\s*[\r\n]+\s*(-?)\$\s*(-?)([\d\.]+)",
        full_text,
        re.IGNORECASE,
    )
    if m_recargos_sub:
        result.declared_recargos_subtotal = _parse_clp_match(m_recargos_sub)

    # 8. Total del mes
    m_total_mes = re.search(
        r"Total\s+del\s+mes\s*[\r\n]+\s*(-?)\$\s*(-?)([\d\.]+)",
        full_text,
        re.IGNORECASE,
    )
    if m_total_mes:
        result.declared_total_month = _parse_clp_match(m_total_mes)

    return result
