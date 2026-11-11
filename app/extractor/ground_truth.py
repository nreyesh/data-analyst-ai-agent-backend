"""Deterministic ground-truth extraction from Kastor PDF text using PyMuPDF (fitz)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
import pymupdf as fitz

# Standard known categories for condominium expense reports (retained for reference and fallback)
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

# Normalization mapping for categories that should be folded into a target category
CATEGORY_NORMALIZATION_MAP: dict[str, str] = {
    "Gas Cargo Comun al 30%": "Consumos",
}

SPANISH_MONTH_MAP: dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "setiembre": 9, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

_LOWERCASE_CONNECTORS: set[str] = {
    "a", "al", "ante", "bajo", "con", "contra", "de", "del",
    "desde", "e", "en", "entre", "hacia", "hasta", "o", "para",
    "por", "según", "sin", "sobre", "tras", "u", "y"
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


@dataclass
class UnitGroundTruthData:
    """Declared subtotals, header metadata, and totals extracted from a unit bill PDF."""

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


def parse_unit_dimensions(code: str) -> tuple[str | None, int | None]:
    """Parse 'B-504' or '504-B' or 'B504' into tower and number ('B', 504)."""
    clean = code.strip()
    m1 = re.match(r"^([A-Za-z]+)\s*[-]?\s*(\d+)$", clean)
    if m1:
        return m1.group(1).upper(), int(m1.group(2))
    m2 = re.match(r"^(\d+)\s*[-]?\s*([A-Za-z]+)$", clean)
    if m2:
        return m2.group(2).upper(), int(m2.group(1))
    return None, None


def _parse_clp(sign1: str, sign2: str, num_str: str) -> int:
    sign = -1 if (sign1 == "-" or sign2 == "-") else 1
    return sign * int(num_str.replace(".", ""))


def _parse_clp_match(match: re.Match, offset: int = 1) -> int:
    return _parse_clp(match.group(offset), match.group(offset + 1), match.group(offset + 2))


def _parse_spanish_period(month_str: str, year_str: str) -> str | None:
    month_num = SPANISH_MONTH_MAP.get(month_str.lower().strip())
    return f"01/{month_num:02d}/{int(year_str):04d}" if month_num else None


def _format_fondo_item_name(raw_label: str) -> str:
    words = raw_label.strip().split()
    formatted = [
        w.lower() if idx > 0 and w.lower() in _LOWERCASE_CONNECTORS
        else w if w.endswith("%")
        else w.capitalize()
        for idx, w in enumerate(words)
    ]
    return " ".join(formatted)


def _clean_header_name(raw_name: str) -> str:
    """Clean a category header, removing trailing invoice citations or whitespace."""
    return re.sub(r"\s*\(.*?\).*", "", raw_name).strip()


def _extract_operational_sections_fallback(full_text: str, effective_end: int) -> dict[str, int]:
    """Fallback extraction using known category headers if dynamic structural extraction fails."""
    cat_patterns = [re.escape(c) + r"\s*$" for c in KNOWN_CATEGORIES if c != "Gas Cargo Comun al 30%"]
    cat_patterns.append(r"Gas Cargo Comun al 30%(?:\s*\(.*?\))?\s*$")
    pattern = re.compile(r"^(?:" + "|".join(cat_patterns) + r")", re.MULTILINE)

    positions: list[tuple[int, str]] = []
    for m in pattern.finditer(full_text):
        if m.start() < effective_end:
            matched = next((c for c in KNOWN_CATEGORIES if m.group().strip().startswith(c)), None)
            if matched:
                positions.append((m.start(), matched))

    positions.sort(key=lambda x: x[0])
    positions.append((effective_end, "TOTAL GASTOS COMUNES"))

    raw_subtotals: dict[str, int] = {}
    for i in range(len(positions) - 1):
        start_pos, cat = positions[i]
        end_pos, _ = positions[i + 1]
        amounts = [_parse_clp_match(m) for m in re.finditer(r"(-?)\$(-?)([\d\.]+)", full_text[start_pos:end_pos])]
        if amounts:
            raw_subtotals[cat] = raw_subtotals.get(cat, 0) + amounts[-1]

    return raw_subtotals


def _extract_operational_sections(
    op_text: str,
    full_text: str,
    effective_end: int,
    declared_op_total: int | None,
) -> dict[str, int]:
    """Dynamically discover operational category sections and declared subtotals.

    In Kastor building reports, each section consists of expense items followed by an amount.
    Every section ends with two consecutive monetary amounts (the final item amount followed
    by the section subtotal), after which the next section header (or 'TOTAL GASTOS COMUNES')
    immediately follows.

    Falls back to regex pattern matching against KNOWN_CATEGORIES if dynamic parsing fails
    or is inconclusive against declared_op_total.
    """
    raw_lines = [l.strip() for l in op_text.split("\n") if l.strip()]
    lines = [
        l for l in raw_lines
        if not re.match(r"^\d+\s*/\s*\d+$", l) and not l.startswith("Powered by TCPDF")
    ]
    if not lines:
        return {}

    clp_re = re.compile(r"^(-?)\$(-?)([\d\.]+)$")

    # Check if lines follow full Kastor report structure (consecutive amounts at section ends)
    has_consecutive_amounts = any(
        clp_re.match(lines[idx]) and clp_re.match(lines[idx + 1])
        for idx in range(len(lines) - 1)
    )

    raw_subtotals: dict[str, int] = {}

    if has_consecutive_amounts:
        # Dynamic structural parser for standard multi-item Kastor PDFs
        current_cat = _clean_header_name(lines[0])
        idx = 1
        while idx < len(lines):
            l = lines[idx]
            if clp_re.match(l):
                if idx + 1 < len(lines) and clp_re.match(lines[idx + 1]):
                    subtotal_val = _parse_clp_match(clp_re.match(lines[idx + 1]))
                    raw_subtotals[current_cat] = raw_subtotals.get(current_cat, 0) + subtotal_val
                    if idx + 2 < len(lines):
                        next_header = lines[idx + 2]
                        if not clp_re.match(next_header):
                            current_cat = _clean_header_name(next_header)
                        idx += 3
                        continue
                    else:
                        break
            idx += 1
    else:
        # Minimal / synthetic structure (e.g. test PDFs with Category followed directly by single amount)
        current_cat = None
        for l in lines:
            m = clp_re.match(l)
            if m:
                if current_cat:
                    val = _parse_clp_match(m)
                    raw_subtotals[current_cat] = raw_subtotals.get(current_cat, 0) + val
                    current_cat = None
            else:
                current_cat = _clean_header_name(l)

    # Verification: If operational total is declared and dynamic parsing didn't match the sum,
    # fall back to pattern matching if pattern matching yields an exact match.
    if declared_op_total is not None and sum(raw_subtotals.values()) != declared_op_total:
        fallback_subtotals = _extract_operational_sections_fallback(full_text, effective_end)
        if sum(fallback_subtotals.values()) == declared_op_total:
            return fallback_subtotals

    return raw_subtotals


def _open_pdf_text(pdf_source: str | Path | bytes) -> str:
    if isinstance(pdf_source, Path):
        doc = fitz.open(str(pdf_source))
    elif isinstance(pdf_source, bytes):
        doc = fitz.open(stream=pdf_source, filetype="pdf")
    elif isinstance(pdf_source, str):
        if "\n" not in pdf_source and len(pdf_source) < 260 and Path(pdf_source).is_file():
            doc = fitz.open(pdf_source)
        else:
            return pdf_source
    else:
        raise TypeError(f"Unsupported pdf_source type: {type(pdf_source)}")
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def extract_ground_truth(pdf_source: str | Path | bytes) -> GroundTruthData:
    """Extract declared operational section subtotals, Fondos, and grand total from a building expense PDF."""
    full_text = _open_pdf_text(pdf_source)
    result = GroundTruthData()

    # 0. Billing Period
    m_period = re.search(r"Informe Gastos Comunes\s*[\r\n]+\s*([A-Za-z]+)\s+(\d{4})", full_text, re.IGNORECASE)
    if m_period:
        result.period_date = _parse_spanish_period(m_period.group(1), m_period.group(2))

    # 1. Operational Total
    m_op_total = re.search(r"TOTAL GASTOS COMUNES\s*[\r\n]+\s*(-?)\$(-?)([\d\.]+)", full_text)
    if m_op_total:
        result.operational_total = _parse_clp_match(m_op_total)

    # 2. Operational Section Subtotals (Dynamic structural extraction)
    start_pos = m_period.end() if m_period else 0
    pos_end = full_text.find("TOTAL GASTOS COMUNES")
    effective_end = pos_end if pos_end != -1 else len(full_text)
    op_text = full_text[start_pos:effective_end]

    raw_subtotals = _extract_operational_sections(
        op_text=op_text,
        full_text=full_text,
        effective_end=effective_end,
        declared_op_total=result.operational_total,
    )

    result.raw_section_totals = dict(raw_subtotals)

    # 3. Apply Category Normalization Map (e.g. merge "Gas Cargo Comun al 30%" into "Consumos")
    normalized_totals = dict(raw_subtotals)
    for source_cat, target_cat in CATEGORY_NORMALIZATION_MAP.items():
        val = normalized_totals.pop(source_cat, None)
        if val is not None:
            normalized_totals[target_cat] = normalized_totals.get(target_cat, 0) + val

    # 4. Extract "Fondos" items & final TOTAL
    if m_op_total:
        tail_text = full_text[m_op_total.end():]
        m_final = re.search(r"^\s*TOTAL\s*[\r\n]+\s*(-?)\$(-?)([\d\.]+)", tail_text, re.MULTILINE)
        if m_final:
            result.grand_total = _parse_clp_match(m_final)
            fondos_block = tail_text[:m_final.start()]
            fondos_items: list[tuple[str, int]] = []
            for m_item in re.finditer(r"^\s*([^\r\n\$]+?)\s*[\r\n]+\s*(-?)\$(-?)([\d\.]+)", fondos_block, re.MULTILINE):
                fondos_items.append((
                    _format_fondo_item_name(m_item.group(1)),
                    _parse_clp_match(m_item, offset=2),
                ))
            result.fondos_items = fondos_items
            fondos_sum = sum(val for _, val in fondos_items)
            result.fondos_total = fondos_sum
            normalized_totals["Fondos"] = fondos_sum
        else:
            result.grand_total = result.operational_total
            result.fondos_total = 0

    result.section_totals = normalized_totals
    return result


def extract_unit_ground_truth(pdf_source: str | Path | bytes) -> UnitGroundTruthData:
    """Extract declared header fields, section subtotals, and monthly total from a unit bill PDF."""
    full_text = _open_pdf_text(pdf_source)
    result = UnitGroundTruthData()

    # 1. Period & Unit Code
    m_header = re.search(r"Nota\s+de\s+Cobro\s+([A-Za-z]+)\s+(\d{4})(?:[ \t]+([^\r\n]+))?", full_text, re.IGNORECASE)
    if m_header:
        result.period_date = _parse_spanish_period(m_header.group(1), m_header.group(2))
        unit_raw = (m_header.group(3) or "").strip()
        if unit_raw:
            result.unit_code = unit_raw
            result.unit_tower, result.unit_number = parse_unit_dimensions(unit_raw)

    # 2. Alícuota Total
    m_alicuota = re.search(r"Al[ií]cuota\s+Total\s*[\r\n]+\s*([\d,\.]+)\s*%", full_text, re.IGNORECASE)
    if m_alicuota:
        result.alicuota_total = float(m_alicuota.group(1).strip().replace(".", "").replace(",", "."))

    # 3. Fecha último Pago
    m_last_date = re.search(r"Fecha\s+[uú]ltimo\s+Pago\s*[\r\n]+\s*(\d{2})[-/](\d{2})[-/](\d{4})", full_text, re.IGNORECASE)
    if m_last_date:
        result.last_payment_date = f"{m_last_date.group(1)}/{m_last_date.group(2)}/{m_last_date.group(3)}"

    # 4-8. Monetary Subtotals
    for pattern, attr in [
        (r"Monto\s+[uú]ltimo\s+Pago\s*[\r\n]+\s*(-?)\$\s*(-?)([\d\.]+)", "last_payment_amount"),
        (r"Subtotal\s+Departamento\s*[\r\n]+\s*(-?)\$\s*(-?)([\d\.]+)", "declared_dept_subtotal"),
        (r"Subtotal\s+Consumo\s*[\r\n]+\s*(-?)\$\s*(-?)([\d\.]+)", "declared_consumo_subtotal"),
        (r"Subtotal\s+Recargos\s*[\r\n]+\s*(-?)\$\s*(-?)([\d\.]+)", "declared_recargos_subtotal"),
        (r"Total\s+del\s+mes\s*[\r\n]+\s*(-?)\$\s*(-?)([\d\.]+)", "declared_total_month"),
    ]:
        m = re.search(pattern, full_text, re.IGNORECASE)
        if m:
            setattr(result, attr, _parse_clp_match(m))

    return result
