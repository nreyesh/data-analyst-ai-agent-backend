"""Deterministic fast-path spend evolution queries, concept catalog discovery, and plotting (3NF schema)."""

from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any
import uuid

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

from backend.app.agent.analytics.config import get_analytics_settings
from backend.app.agent.analytics.gateway_client import query_data_gateway

logger = logging.getLogger("backend.agent.analytics.tools.spend_evolution")

SPANISH_MONTH_ABBR = {
    1: "Ene",
    2: "Feb",
    3: "Mar",
    4: "Abr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Ago",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dic",
}


def _format_period_label(iso_date: str) -> str:
    """Convert 'YYYY-MM-01' into 'Mes AAAA' e.g. 'Jul 2026'."""
    m = re.match(r"^(\d{4})-(\d{2})", iso_date.strip())
    if not m:
        return iso_date
    year = m.group(1)
    month = int(m.group(2))
    month_name = SPANISH_MONTH_ABBR.get(month, str(month))
    return f"{month_name} {year}"


def _sanitize_sql_string(val: str) -> str:
    """Sanitize concept string to prevent SQL injection when matching text columns."""
    cleaned = re.sub(r"[^a-zA-Z0-9\s\-_áéíóúÁÉÍÓÚñÑ%]", "", val.strip())
    return cleaned.replace("'", "''")


def _parse_unit_filter(unit_code: str | None) -> tuple[str, str]:
    """Parse unit code into SQL filter and normalized representation, supporting both 'B-504' and '504'."""
    norm_unit = unit_code.strip().upper() if unit_code else ""
    if not norm_unit:
        return "WHERE 1=0", ""
    parts = norm_unit.split("-")
    tower_filter = f"AND u.tower = '{parts[0]}'" if len(parts) == 2 and not parts[0].isdigit() else ""
    num_val = 0
    if len(parts) == 2 and parts[1].isdigit():
        num_val = int(parts[1])
    elif len(parts) == 1 and parts[0].isdigit():
        num_val = int(parts[0])
    else:
        m = re.search(r"(\d{1,4})", norm_unit)
        if m:
            num_val = int(m.group(1))
        m_t = re.search(r"^([A-Z])", norm_unit)
        if m_t and not tower_filter:
            tower_filter = f"AND u.tower = '{m_t.group(1)}'"

    if num_val:
        return f"WHERE u.unit_number = {num_val} {tower_filter}".strip(), norm_unit
    return f"WHERE b.unit_code = '{norm_unit}'", norm_unit


def get_expense_concept_catalog(client: Any | None = None) -> dict[str, list[str]]:
    """Retrieve distinct building categories and unit concepts/charges from Data Gateway.

    Returns:
        Dictionary with 'building_categories', 'unit_concepts', 'unit_charges', and 'unit_consumptions'.
    """
    catalog: dict[str, list[str]] = {
        "building_categories": [],
        "unit_concepts": [],
        "unit_charges": [],
        "unit_consumptions": ["Agua Caliente (consumo)"],
    }

    try:
        # 1. Building categories
        sec_rows, err = query_data_gateway(
            sql="SELECT DISTINCT category_name FROM expense_sections ORDER BY category_name ASC;",
            client=client,
        )
        if sec_rows and not err:
            catalog["building_categories"] = [
                str(r["category_name"]).strip()
                for r in sec_rows
                if r.get("category_name")
            ]

        # 2. Unit concepts
        c_rows, err = query_data_gateway(
            sql="SELECT DISTINCT concept_name FROM unit_bill_concepts ORDER BY concept_name ASC;",
            client=client,
        )
        if c_rows and not err:
            catalog["unit_concepts"] = [
                str(r["concept_name"]).strip()
                for r in c_rows
                if r.get("concept_name")
            ]

        # 3. Unit charges (normalize interest variants to single 'Interes' entry)
        ch_rows, err = query_data_gateway(
            sql="SELECT DISTINCT charge_name FROM unit_bill_charges ORDER BY charge_name ASC;",
            client=client,
        )
        if ch_rows and not err:
            raw_charges: set[str] = set()
            has_interest = False
            for r in ch_rows:
                c_name = str(r.get("charge_name", "")).strip()
                if not c_name:
                    continue
                if c_name.lower().startswith("interes"):
                    has_interest = True
                else:
                    raw_charges.add(c_name)
            if has_interest:
                raw_charges.add("Interes")
            catalog["unit_charges"] = sorted(raw_charges)

    except Exception as exc:
        logger.warning("Could not fetch expense concept catalog: %s", exc)

    return catalog


def format_catalog_for_prompt(catalog: dict[str, list[str]]) -> str:
    """Format live catalog into Markdown bullet points for Supervisor system instruction injection."""
    b_cats = catalog.get("building_categories") or [
        "mantenciones y servicios",
        "consumos",
        "remuneraciones",
        "reparaciones y repuestos",
        "seguros",
        "insumos",
        "fondos",
        "otros gastos",
        "asesorias y honorarios profesionales",
    ]
    u_concepts = catalog.get("unit_concepts") or [
        "Gasto Común",
        "Fondo de Reserva 5%",
        "Fondo Impermeabilizacion",
    ]
    u_charges = catalog.get("unit_charges") or [
        "Fondo de Indemnizacion",
        "Seguro departamento",
        "Interes",
        "Multa",
    ]
    u_consumptions = catalog.get("unit_consumptions") or ["Agua Caliente (consumo)"]

    lines = [
        "### Catálogo Oficial de Conceptos en la Base de Datos:",
        "- **Categorías del Edificio (knowledge_domain = 'building')**:",
        "  " + ", ".join(f"`{c}`" for c in b_cats),
        "- **Conceptos y Subtotales de la Unidad (knowledge_domain = 'unit')**:",
        "  - Subtotales base: " + ", ".join(f"`{c}`" for c in u_concepts),
        "  - Consumo medido: " + ", ".join(f"`{c}`" for c in u_consumptions),
        "  - Cargos particulares: " + ", ".join(f"`{c}`" for c in u_charges),
        "- **Regla de Clasificación**:",
        "  - Si el usuario pregunta por un concepto del edificio (ej. mantenciones, remuneraciones, calderas, ascensores) -> `knowledge_domain = 'building'`. NO preguntes si es del depto.",
        "  - Si el usuario pregunta por un concepto propio de su departamento (ej. mi agua caliente, mis multas, mi gasto común) -> `knowledge_domain = 'unit'`. NO preguntes si es del edificio.",
        "  - Si el concepto existe en ambos (ej. *'fondos'*, *'seguros'* o *'gasto común'* a secas sin 'mi' ni 'edificio') -> marca `route = 'clarify_intent'` y ofrece las opciones.",
    ]
    return "\n".join(lines)


def _compute_evolution_metrics(records: list[dict[str, Any]], value_key: str) -> dict[str, Any]:
    """Compute min, max, and month-over-month variations deterministically."""
    if not records:
        return {"min_record": None, "max_record": None, "mom_series": []}

    min_rec = min(records, key=lambda r: r.get(value_key, 0) or 0)
    max_rec = max(records, key=lambda r: r.get(value_key, 0) or 0)

    mom_series = []
    prev_val = None
    for r in records:
        curr_val = r.get(value_key, 0) or 0
        delta = curr_val - prev_val if prev_val is not None else 0
        pct = (delta / prev_val * 100.0) if (prev_val and prev_val != 0) else 0.0
        row_copy = dict(r)
        row_copy["delta_clp"] = delta
        row_copy["pct_change"] = round(pct, 1)
        mom_series.append(row_copy)
        prev_val = curr_val

    return {
        "min_record": min_rec,
        "max_record": max_rec,
        "mom_series": mom_series,
    }


def get_spend_evolution_data(
    knowledge_domain: str = "building",
    unit_code: str | None = None,
    target_concept: str | None = None,
    time_scope: str | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Retrieve chronological spend evolution series using 3NF JOINs (`units` and `billing_periods`)."""
    scope_clause = ""
    if time_scope and re.match(r"^\d{4}$", time_scope.strip()):
        scope_clause = f"WHERE p.period_year = {int(time_scope.strip())}"
    elif time_scope and re.match(r"^20\d{2}-", time_scope.strip()):
        scope_clause = f"WHERE r.period_date >= '{time_scope.strip()}'"

    clean_concept = _sanitize_sql_string(target_concept) if target_concept else None
    unit_filter, norm_unit = _parse_unit_filter(unit_code)
    year_clause = f"AND p.period_year = {int(time_scope.strip())}" if time_scope and time_scope.strip().isdigit() else ""

    # Case 1: Unit Hot Water Consumption (m³ and CLP)
    if knowledge_domain == "unit" and clean_concept and "agua caliente" in clean_concept.lower():
        sql = (
            f"SELECT b.period_date, c.consumption AS volume_m3, c.total_amount AS amount_clp, "
            f"c.unit_rate, c.previous_reading, c.current_reading "
            f"FROM unit_bill_consumptions c "
            f"JOIN unit_bills b ON c.unit_bill_id = b.id "
            f"JOIN units u ON b.unit_code = u.unit_code "
            f"JOIN billing_periods p ON b.period_date = p.period_date "
            f"{unit_filter} {year_clause} "
            f"ORDER BY b.period_date DESC LIMIT 12;"
        )
        rows, err = query_data_gateway(sql=sql, client=client)
        if err or not rows:
            return {"error": err, "rows": [], "series_type": "unit_hot_water"}
        rows = list(reversed(rows))
        metrics = _compute_evolution_metrics(rows, "amount_clp")
        return {
            "rows": rows,
            "metrics": metrics,
            "series_type": "unit_hot_water",
            "concept_name": "Agua Caliente (Consumo)",
            "unit_code": norm_unit,
        }

    # Case 2: Unit Specific Charge or Concept (e.g. Multa, Seguro, Interes, Fondo)
    if knowledge_domain == "unit" and clean_concept:
        sql = (
            f"SELECT b.period_date, SUM(c.amount) AS amount_clp, MAX(c.charge_name) AS charge_name "
            f"FROM unit_bill_charges c "
            f"JOIN unit_bills b ON c.unit_bill_id = b.id "
            f"JOIN units u ON b.unit_code = u.unit_code "
            f"JOIN billing_periods p ON b.period_date = p.period_date "
            f"{unit_filter} {year_clause} AND LOWER(c.charge_name) LIKE '%{clean_concept.lower()}%' "
            f"GROUP BY b.period_date ORDER BY b.period_date DESC LIMIT 12;"
        )
        rows, err = query_data_gateway(sql=sql, client=client)
        if rows and not err:
            rows = list(reversed(rows))
            metrics = _compute_evolution_metrics(rows, "amount_clp")
            return {
                "rows": rows,
                "metrics": metrics,
                "series_type": "unit_charge",
                "concept_name": clean_concept.title(),
                "unit_code": norm_unit,
            }

        sql_conc = (
            f"SELECT b.period_date, SUM(c.amount) AS amount_clp, MAX(c.concept_name) AS concept_name "
            f"FROM unit_bill_concepts c "
            f"JOIN unit_bills b ON c.unit_bill_id = b.id "
            f"JOIN units u ON b.unit_code = u.unit_code "
            f"JOIN billing_periods p ON b.period_date = p.period_date "
            f"{unit_filter} {year_clause} AND LOWER(c.concept_name) LIKE '%{clean_concept.lower()}%' "
            f"GROUP BY b.period_date ORDER BY b.period_date DESC LIMIT 12;"
        )
        rows_conc, err_conc = query_data_gateway(sql=sql_conc, client=client)
        if rows_conc and not err_conc:
            rows_conc = list(reversed(rows_conc))
            metrics = _compute_evolution_metrics(rows_conc, "amount_clp")
            return {
                "rows": rows_conc,
                "metrics": metrics,
                "series_type": "unit_concept",
                "concept_name": clean_concept.title(),
                "unit_code": norm_unit,
            }

    # Case 3: Unit Overall Monthly Spend (Default for unit queries without matching sub-item)
    if knowledge_domain == "unit":
        sql = (
            f"SELECT b.period_date, b.calculated_dept_subtotal, b.calculated_consumo_total, "
            f"b.calculated_unit_subtotal, b.calculated_total_month "
            f"FROM unit_bills b "
            f"JOIN units u ON b.unit_code = u.unit_code "
            f"JOIN billing_periods p ON b.period_date = p.period_date "
            f"{unit_filter} {year_clause} "
            f"ORDER BY b.period_date DESC LIMIT 12;"
        )
        rows, err = query_data_gateway(sql=sql, client=client)
        if err or not rows:
            return {"error": err, "rows": [], "series_type": "unit_total"}
        rows = list(reversed(rows))
        metrics = _compute_evolution_metrics(rows, "calculated_total_month")
        return {
            "rows": rows,
            "metrics": metrics,
            "series_type": "unit_total",
            "concept_name": f"Gasto Común — Unidad {norm_unit}",
            "unit_code": norm_unit,
        }

    # Case 4: Building Specific Category or Item
    is_operational_query = bool(clean_concept and "operacional" in clean_concept.lower())
    if clean_concept and not is_operational_query and clean_concept.lower() not in ("total", "gasto general"):
        year_filter_sec = f"AND p.period_year = {int(time_scope.strip())}" if time_scope and time_scope.strip().isdigit() else ""
        sql = (
            f"SELECT r.period_date, s.category_name, s.calculated_subtotal AS amount_clp "
            f"FROM expense_sections s "
            f"JOIN expense_reports r ON s.report_id = r.id "
            f"JOIN billing_periods p ON r.period_date = p.period_date "
            f"WHERE LOWER(s.category_name) LIKE '%{clean_concept.lower()}%' {year_filter_sec} "
            f"ORDER BY r.period_date DESC LIMIT 12;"
        )
        rows, err = query_data_gateway(sql=sql, client=client)
        if rows and not err:
            rows = list(reversed(rows))
            matched_name = str(rows[0].get("category_name", clean_concept)).title()
            metrics = _compute_evolution_metrics(rows, "amount_clp")
            return {
                "rows": rows,
                "metrics": metrics,
                "series_type": "building_category",
                "concept_name": matched_name,
            }

        # Case 4b: Building Specific Item (expense_items)
        sql_item = (
            f"SELECT r.period_date, MAX(i.normalized_name) AS normalized_name, SUM(i.amount) AS amount_clp "
            f"FROM expense_items i "
            f"JOIN expense_sections s ON i.section_id = s.id "
            f"JOIN expense_reports r ON s.report_id = r.id "
            f"JOIN billing_periods p ON r.period_date = p.period_date "
            f"WHERE LOWER(i.normalized_name) LIKE '%{clean_concept.lower()}%' {year_filter_sec} "
            f"GROUP BY r.period_date ORDER BY r.period_date DESC LIMIT 12;"
        )
        rows_item, err_item = query_data_gateway(sql=sql_item, client=client)
        if rows_item and not err_item:
            rows_item = list(reversed(rows_item))
            matched_item = str(rows_item[0].get("normalized_name", clean_concept)).title()
            metrics = _compute_evolution_metrics(rows_item, "amount_clp")
            return {
                "rows": rows_item,
                "metrics": metrics,
                "series_type": "building_item",
                "concept_name": matched_item,
            }

        return {
            "error": f"No se encontraron registros para '{clean_concept}'.",
            "rows": [],
            "series_type": "not_found",
            "concept_name": clean_concept,
        }

    # Case 5: Building Macro Total / Operational Total Evolution
    limit_clause = "LIMIT 12" if not scope_clause else ""
    sql = (
        f"SELECT r.period_date, r.operational_total, r.fondos_total, r.calculated_grand_total AS amount_clp "
        f"FROM expense_reports r "
        f"JOIN billing_periods p ON r.period_date = p.period_date "
        f"{scope_clause} "
        f"ORDER BY r.period_date DESC {limit_clause};"
    )
    rows, err = query_data_gateway(sql=sql, client=client)
    if err or not rows:
        return {"error": err, "rows": [], "series_type": "building_total"}
    rows = list(reversed(rows))
    metrics = _compute_evolution_metrics(rows, "amount_clp")
    title = "Gasto Operacional del Edificio" if is_operational_query else "Gasto Común Total del Edificio"
    return {
        "rows": rows,
        "metrics": metrics,
        "series_type": "building_total",
        "concept_name": title,
    }


def plot_spend_evolution(
    dates: list[str],
    series_dict: dict[str, list[int | float]],
    title: str,
    unit_label: str = "CLP",
    is_volume: bool = False,
) -> str | None:
    """Render a clean Matplotlib time-series PNG chart directly in Python.

    Returns:
        Absolute path string to the generated PNG in charts_dir, or None on failure.
    """
    if not dates or not series_dict:
        return None

    try:
        settings = get_analytics_settings()
        charts_dir: Path = settings.charts_dir
        charts_dir.mkdir(parents=True, exist_ok=True)
        chart_id = uuid.uuid4().hex[:8]
        out_path = charts_dir / f"evolution_{chart_id}.png"

        fig, ax = plt.subplots(figsize=(9, 4.5), dpi=100)
        formatted_labels = [_format_period_label(d) for d in dates]

        colors = ["#2563EB", "#059669", "#D97706", "#DC2626", "#7C3AED"]
        markers = ["o", "s", "^", "D", "v"]

        for idx, (label, values) in enumerate(series_dict.items()):
            c = colors[idx % len(colors)]
            m = markers[idx % len(markers)]
            ax.plot(
                formatted_labels,
                values,
                marker=m,
                linewidth=2.2,
                markersize=6,
                label=label,
                color=c,
            )

        ax.set_title(title, fontsize=12, fontweight="bold", pad=12)
        ax.grid(True, linestyle="--", alpha=0.5)

        if not is_volume:
            def currency_fmt(x, _):
                if abs(x) >= 1_000_000:
                    return f"${x*1e-6:.1f}M"
                if abs(x) >= 1_000:
                    return f"${x*1e-3:.0f}K"
                return f"${int(x)}"

            ax.yaxis.set_major_formatter(ticker.FuncFormatter(currency_fmt))
            ax.set_ylabel("Monto (CLP)", fontsize=10)
        else:
            ax.set_ylabel(unit_label, fontsize=10)

        plt.xticks(rotation=25, ha="right", fontsize=9)
        plt.yticks(fontsize=9)

        if len(series_dict) > 1:
            ax.legend(
                loc="upper center",
                bbox_to_anchor=(0.5, -0.18),
                ncol=min(len(series_dict), 4),
                frameon=True,
                fontsize=8.5,
            )

        plt.tight_layout()
        fig.savefig(str(out_path), bbox_inches="tight")
        plt.close(fig)

        return str(out_path)

    except Exception as exc:
        logger.warning("Could not render evolution chart: %s", exc)
        return None
