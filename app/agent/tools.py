"""Deterministic RBAC-enforced tool wrappers for the Conversational Supervisor Agent."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
import re
from typing import Any, Callable
from langsmith import traceable

from backend.app.agent.analytics.tools.building_tools import (

    get_building_summary,
    get_system_diagnostics,
)
from backend.app.agent.analytics.tools.spend_evolution import (
    get_expense_concept_catalog,
    get_spend_evolution_data,
    plot_spend_evolution,
)
from backend.app.agent.analytics.tools.unit_tools import get_unit_bill_details
from backend.app.agent.analytics.workflow import run_analytics_workflow
from backend.app.agent.state import UserContext

logger = logging.getLogger("backend.agent.tools")


def _encode_chart_base64(chart_path: str | None) -> str | None:
    """Read a PNG file from disk and return its base64-encoded ASCII string."""
    if not chart_path:
        return None
    try:
        p = Path(chart_path)
        if p.is_file():
            return base64.b64encode(p.read_bytes()).decode("ascii")
    except Exception as exc:
        logger.warning("Failed to base64-encode chart at '%s': %s", chart_path, exc)
    return None


def _format_clp(amount: int | float | None) -> str:
    """Format integer CLP amount using Chilean dot thousand separators (e.g. $145.320 CLP)."""
    if amount is None:
        return "$0 CLP"
    val = int(round(amount))
    formatted = f"{abs(val):,}".replace(",", ".")
    sign = "-" if val < 0 else ""
    return f"{sign}${formatted} CLP"


def _normalize_unit(unit_code: str | None) -> str | None:
    """Normalize apartment code into 'TOWER-NUMBER' format when possible."""
    if not unit_code:
        return None
    raw = unit_code.strip().upper()
    m1 = re.match(r"^([A-Z]{1,2})\s*[-]\s*(\d{1,4})$", raw)
    if m1:
        return f"{m1.group(1)}-{int(m1.group(2))}"
    m2 = re.match(r"^(\d{1,4})\s*[-]?\s*([A-Z]{1,2})$", raw)
    if m2:
        return f"{m2.group(2)}-{int(m2.group(1))}"
    return raw


def _is_cross_unit_cohort_query(query: str) -> bool:
    """Detect if a resident query asks for multi-unit rankings or neighbor comparisons."""
    return bool(
        re.search(
            r"\b(departamentos\s+con\s+mayor|departamentos\s+en\s+mora|ranking\s+de\s+departamentos|"
            r"todos\s+los\s+departamentos|otros\s+departamentos|vecinos)\b",
            query,
            re.I,
        )
    )


@traceable(name="lookup_unit_bill", run_type="tool")
def lookup_unit_bill(
    user_context: UserContext,
    unit_code: str | None = None,
    period_date: str | None = None,
    query_text: str = "",
    client: Any | None = None,
    is_unauthorized_unit_access: bool = False,
    unauthorized_target_unit: str | None = None,
) -> dict[str, Any]:

    """Fast-path apartment unit bill snapshot lookup with deterministic RBAC enforcement."""
    norm_requested = _normalize_unit(unit_code)

    if user_context.role == "resident":
        assigned_unit = _normalize_unit(user_context.unit_code) or ""
        assigned_num = assigned_unit.split("-")[-1]
        assigned_has_tower = "-" in assigned_unit

        is_different_unit = False
        if norm_requested and norm_requested != assigned_unit:
            req_num = norm_requested.split("-")[-1]
            if norm_requested != assigned_num and not (not assigned_has_tower and req_num == assigned_num):
                is_different_unit = True

        if is_unauthorized_unit_access or is_different_unit or unauthorized_target_unit:
            target_val = unauthorized_target_unit or (norm_requested if is_different_unit else None)
            target_str = f" (no puedes consultar `{target_val}`)" if target_val else ""
            logger.warning(
                "RBAC block: Resident '%s' (%s) attempted to query unit '%s'",
                user_context.email,
                assigned_unit,
                target_val or norm_requested,
            )
            msg = (
                f"Acceso denegado: Tu perfil de residente solo está autorizado para "
                f"consultar la unidad **{assigned_unit}**{target_str}."
            )
            return {"error": msg, "unauthorized": True, "summary": msg}

        effective_unit = norm_requested if (norm_requested and "-" in norm_requested and "-" not in assigned_unit) else assigned_unit
    else:
        effective_unit = norm_requested or ""
        if not effective_unit:
            msg = "Por favor indica el código del departamento que deseas consultar (por ejemplo, `B-504`)."
            return {"error": msg, "needs_unit": True, "summary": msg}

    data = get_unit_bill_details(unit_code=effective_unit, period_date=period_date, client=client)
    if "error" in data:
        return {
            "error": data["error"],
            "summary": f"No se encontró información de cobro para la unidad **{effective_unit}**: {data['error']}",
            "data": data,
        }

    period_str = data.get("period_date", "último periodo")
    total_month = _format_clp(data.get("calculated_total_month"))
    dept_sub = _format_clp(data.get("calculated_dept_subtotal"))
    consumo_sub = _format_clp(data.get("calculated_consumo_total"))
    charges_sub = _format_clp(data.get("calculated_unit_subtotal"))
    alicuota = data.get("alicuota_total", 0)
    last_pay_date = data.get("last_payment_date") or "Sin registro"
    last_pay_amt = _format_clp(data.get("last_payment_amount")) if data.get("last_payment_amount") else "N/A"

    lines = [
        f"### Resumen de Nota de Cobro — Unidad **{data.get('unit_code', effective_unit)}** (`{period_str}`)",
        f"- **Total Mes**: **{total_month}** (Alícuota: `{alicuota}%`)",
        f"- **Subtotal Depto (Cuota Base + Fondos)**: {dept_sub}",
        f"- **Agua Caliente**: {consumo_sub}",
        f"- **Cargos Particulares (Multas y otros)**: {charges_sub}",
        f"- **Último Pago Registrado**: {last_pay_amt} (Fecha: `{last_pay_date}`)",
    ]

    concepts = data.get("concepts", [])
    if concepts:
        lines.append("\n**Desglose Subtotal Depto:**")
        for c in concepts:
            c_name = c.get("concept_name") or c.get("concept") or "Concepto"
            c_detail = f" ({c.get('detail')})" if c.get("detail") else ""
            lines.append(f"- {c_name}{c_detail}: {_format_clp(c.get('amount'))}")

    consumptions = data.get("consumptions", [])
    if consumptions:
        lines.append("\n**Consumos Individuales:**")
        for cons in consumptions:
            cons_total = cons.get("total_amount") if cons.get("total_amount") is not None else cons.get("total")
            lines.append(
                f"- {cons.get('item_name')}: `{cons.get('consumption')} m³` "
                f"(Lectura `{cons.get('previous_reading')}` → `{cons.get('current_reading')}`, "
                f"Tarifa `{_format_clp(cons.get('unit_rate'))}/m³`) = **{_format_clp(cons_total)}**"
            )

    unit_charges = data.get("unit_charges", [])
    if unit_charges:
        lines.append("\n**Cargos Particulares de la Unidad:**")
        for ch in unit_charges:
            ch_name = ch.get("charge_name") or ch.get("name") or "Cargo"
            lines.append(f"- {ch_name}: {_format_clp(ch.get('amount'))}")

    step_detail = (
        f"GET /api/v1/unit-bills?unit_code={effective_unit}"
        + (f"&period_date={period_date}" if period_date else "")
        if user_context.role == "admin"
        else f"Información verificada en los registros oficiales de la unidad {effective_unit} ({period_str})."
    )

    return {
        "summary": "\n".join(lines),
        "data": data,
        "unit_code": data.get("unit_code", effective_unit),
        "period_date": data.get("period_date"),
        "step_name": f"Estado de cuenta verificado ({effective_unit})",
        "step_detail": step_detail,
    }


@traceable(name="lookup_building_summary", run_type="tool")
def lookup_building_summary(
    user_context: UserContext,
    period_date: str | None = None,
    client: Any | None = None,
) -> dict[str, Any]:

    """Fast-path macro building expense report lookup (permitted for both admin and resident)."""
    _ = user_context  # Macro building totals are public to all co-owners under Ley 21.442
    data = get_building_summary(period_date=period_date, client=client)
    if "error" in data:
        return {
            "error": data["error"],
            "summary": f"No se encontró el informe general del edificio: {data['error']}",
            "data": data,
        }

    period_str = data.get("period_date", "último periodo")
    op_total = _format_clp(data.get("operational_total"))
    fondos_total = _format_clp(data.get("fondos_total"))
    grand_total = _format_clp(data.get("calculated_grand_total"))

    lines = [
        f"### Resumen de Gastos Comunes del Edificio (`{period_str}`)",
        f"- **Total Gasto Operacional**: **{op_total}**",
        f"- **Total Recaudación de Fondos**: **{fondos_total}**",
        f"- **Total General del Mes**: **{grand_total}**",
    ]

    sections = data.get("sections", [])
    if sections:
        lines.append("\n**Subtotales por Categoría:**")
        for sec in sections:
            lines.append(
                f"- {sec.get('category_name')} (`{sec.get('expense_type')}`): "
                f"**{_format_clp(sec.get('calculated_subtotal'))}**"
            )

    step_detail = (
        f"GET /api/v1/reports" + (f"/period/{period_date}" if period_date else " (latest)")
        if user_context.role == "admin"
        else f"Información verificada en el informe general del edificio ({period_str})."
    )

    return {
        "summary": "\n".join(lines),
        "data": data,
        "period_date": data.get("period_date"),
        "step_name": "Resumen del edificio verificado",
        "step_detail": step_detail,
    }


@traceable(name="check_system_status", run_type="tool")
def check_system_status(
    user_context: UserContext,
    client: Any | None = None,
) -> dict[str, Any]:

    """Admin-only system diagnostics and database synchronization verification."""
    if user_context.role != "admin":
        logger.warning(
            "RBAC block: Resident '%s' attempted to invoke admin tool check_system_status",
            user_context.email,
        )
        msg = "Acceso denegado: El diagnóstico del sistema y estado de sincronización es exclusivo para la Administración."
        return {"error": msg, "unauthorized": True, "summary": msg}

    data = get_system_diagnostics(client=client)
    if "error" in data:
        return {
            "error": data["error"],
            "summary": f"Error al consultar el estado del sistema: {data['error']}",
            "data": data,
        }

    periods = data.get("loaded_periods", [])
    periods_str = ", ".join(f"`{p}`" for p in periods) if periods else "Ninguno"
    lines = [
        "### Diagnóstico del Sistema y Estado de Sincronización",
        f"- **Estado del Servicio**: `{data.get('status', 'healthy')}`",
        f"- **Informes Generales Cargados ({data.get('loaded_reports_count', 0)})**: {periods_str}",
        f"- **Último Periodo Disponible**: `{data.get('latest_period') or 'N/A'}`",
        f"- **Cola Spool Pendientes**: `{data.get('pending_count', 0)}` archivos",
        f"- **Cola Spool Sincronizados**: `{data.get('synced_count', 0)}` archivos",
        f"- **Cola Spool Fallidos**: `{data.get('failed_count', 0)}` archivos",
    ]
    if data.get("total_seeded_units") is not None:
        lines.append(f"- **Unidades Registradas**: `{data.get('total_seeded_units', 0)}` departamentos")
    if data.get("total_unit_bills") is not None:
        lines.append(f"- **Notas de Cobro Sincronizadas**: `{data.get('total_unit_bills', 0)}` registros")

    return {
        "summary": "\n".join(lines),
        "data": data,
        "step_name": "Diagnóstico del sistema",
        "step_detail": "GET /api/v1/reports & GET /api/v1/sync/status",
    }


@traceable(name="lookup_spend_evolution", run_type="tool")
def lookup_spend_evolution(
    user_context: UserContext,
    knowledge_domain: str = "building",
    unit_code: str | None = None,
    target_concept: str | None = None,
    time_scope: str | None = None,
    query_text: str = "",
    client: Any | None = None,
    is_unauthorized_unit_access: bool = False,
    unauthorized_target_unit: str | None = None,
) -> dict[str, Any]:

    """Fast-path deterministic time-series spend evolution with RBAC and direct Matplotlib chart generation."""
    norm_requested = _normalize_unit(unit_code)

    if knowledge_domain == "unit":
        if user_context.role == "resident":
            assigned_unit = _normalize_unit(user_context.unit_code) or ""
            assigned_num = assigned_unit.split("-")[-1]
            assigned_has_tower = "-" in assigned_unit

            is_different_unit = False
            if norm_requested and norm_requested != assigned_unit:
                req_num = norm_requested.split("-")[-1]
                if norm_requested != assigned_num and not (not assigned_has_tower and req_num == assigned_num):
                    is_different_unit = True

            if is_unauthorized_unit_access or is_different_unit or unauthorized_target_unit:
                target_val = unauthorized_target_unit or (norm_requested if is_different_unit else None)
                target_str = f" (no puedes consultar `{target_val}`)" if target_val else ""
                logger.warning(
                    "RBAC block: Resident '%s' (%s) attempted to query evolution for unit '%s'",
                    user_context.email,
                    assigned_unit,
                    target_val or norm_requested,
                )
                msg = (
                    f"Acceso denegado: Tu perfil de residente solo está autorizado para "
                    f"consultar la evolución de la unidad **{assigned_unit}**{target_str}."
                )
                return {"error": msg, "unauthorized": True, "summary": msg}

            effective_unit = norm_requested if (norm_requested and "-" in norm_requested and "-" not in assigned_unit) else assigned_unit
        else:
            effective_unit = norm_requested or ""
            if not effective_unit:
                msg = "Por favor indica el código del departamento que deseas consultar (por ejemplo, `B-504`)."
                return {"error": msg, "needs_unit": True, "summary": msg}
    else:
        effective_unit = None

    data = get_spend_evolution_data(
        knowledge_domain=knowledge_domain,
        unit_code=effective_unit,
        target_concept=target_concept,
        time_scope=time_scope,
        client=client,
    )

    if data.get("series_type") == "not_found" or not data.get("rows"):
        if knowledge_domain == "unit":
            msg = (
                f"No se encontraron registros de cobro para la unidad **{effective_unit}** "
                f"en los períodos consultados."
            )
        else:
            cat_info = get_expense_concept_catalog(client=client)
            available_cats = ", ".join(f"`{c}`" for c in cat_info.get("building_categories", [])[:6])
            concept_display = target_concept or "el concepto consultado"
            msg = (
                f"No se encontraron registros de cobro para «**{concept_display}**» en los períodos disponibles.\n\n"
                f"Algunas de las categorías disponibles para consultar son: {available_cats}."
            )
        return {
            "error": "Concept not found",
            "summary": msg,
            "data": data,
            "chart_file_path": None,
            "chart_base64": None,
            "unit_code": effective_unit,
        }

    rows = data.get("rows", [])
    series_type = data.get("series_type", "building_total")
    metrics = data.get("metrics", {})
    min_rec = metrics.get("min_record")
    max_rec = metrics.get("max_record")
    mom_series = metrics.get("mom_series", [])
    concept_title = data.get("concept_name", "Gasto Común")

    dates = [r["period_date"] for r in rows]
    series_dict: dict[str, list[int | float]] = {}
    is_volume = False

    lines = [f"### Evolución Mensual — {concept_title}"]

    if series_type == "unit_total":
        lines.append("| Período | Total Mes | Cuota Base + Fondos | Agua Caliente | Cargos Particulares | Var. Mensual (%) |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: |")
        for r in mom_series:
            p_lbl = r.get("period_date", "")
            tot = _format_clp(r.get("calculated_total_month"))
            dept = _format_clp(r.get("calculated_dept_subtotal"))
            cons = _format_clp(r.get("calculated_consumo_total"))
            charg = _format_clp(r.get("calculated_unit_subtotal"))
            pct = f"{r.get('pct_change', 0):+.1f}%" if r.get("delta_clp", 0) != 0 else "-"
            lines.append(f"| {p_lbl} | **{tot}** | {dept} | {cons} | {charg} | {pct} |")

        series_dict = {
            "Total Mes": [r.get("calculated_total_month", 0) for r in rows],
            "Cuota Base + Fondos": [r.get("calculated_dept_subtotal", 0) for r in rows],
            "Agua Caliente": [r.get("calculated_consumo_total", 0) for r in rows],
            "Cargos Particulares": [r.get("calculated_unit_subtotal", 0) for r in rows],
        }

    elif series_type == "unit_hot_water":
        lines.append("| Período | Consumo (m³) | Tarifa/m³ | Total Cobrado | Var. Mensual (%) |")
        lines.append("| :--- | :---: | :---: | :---: | :---: |")
        for r in mom_series:
            p_lbl = r.get("period_date", "")
            vol = f"{r.get('volume_m3', 0)} m³"
            rate = _format_clp(r.get("unit_rate"))
            tot = _format_clp(r.get("amount_clp"))
            pct = f"{r.get('pct_change', 0):+.1f}%" if r.get("delta_clp", 0) != 0 else "-"
            lines.append(f"| {p_lbl} | {vol} | {rate} | **{tot}** | {pct} |")

        series_dict = {
            "Total Cobrado (CLP)": [r.get("amount_clp", 0) for r in rows],
        }

    elif series_type == "building_total":
        lines.append("| Período | Total General | Gasto Operacional | Fondos y Recargos | Var. Mensual (%) |")
        lines.append("| :--- | :---: | :---: | :---: | :---: |")
        for r in mom_series:
            p_lbl = r.get("period_date", "")
            tot = _format_clp(r.get("amount_clp"))
            op = _format_clp(r.get("operational_total"))
            fnd = _format_clp(r.get("fondos_total"))
            pct = f"{r.get('pct_change', 0):+.1f}%" if r.get("delta_clp", 0) != 0 else "-"
            lines.append(f"| {p_lbl} | **{tot}** | {op} | {fnd} | {pct} |")

        series_dict = {
            "Total General": [r.get("amount_clp", 0) for r in rows],
            "Gasto Operacional": [r.get("operational_total", 0) for r in rows],
            "Fondos y Recargos": [r.get("fondos_total", 0) for r in rows],
        }

    else:
        lines.append("| Período | Monto Cobrado | Var. Mensual (CLP) | Var. Mensual (%) |")
        lines.append("| :--- | :---: | :---: | :---: |")
        for r in mom_series:
            p_lbl = r.get("period_date", "")
            tot = _format_clp(r.get("amount_clp"))
            d_clp = _format_clp(r.get("delta_clp")) if r.get("delta_clp") is not None else "-"
            pct = f"{r.get('pct_change', 0):+.1f}%" if r.get("delta_clp", 0) != 0 else "-"
            lines.append(f"| {p_lbl} | **{tot}** | {d_clp} | {pct} |")

        series_dict = {
            concept_title: [r.get("amount_clp", 0) for r in rows],
        }

    if min_rec and max_rec:
        min_val = _format_clp(min_rec.get("amount_clp") or min_rec.get("calculated_total_month"))
        max_val = _format_clp(max_rec.get("amount_clp") or max_rec.get("calculated_total_month"))
        lines.append(f"\n- **Mínimo del período**: {min_val} `[{min_rec.get('period_date')}]`")
        lines.append(f"- **Máximo del período**: {max_val} `[{max_rec.get('period_date')}]`")

    chart_path = plot_spend_evolution(
        dates=dates,
        series_dict=series_dict,
        title=f"Evolución Mensual: {concept_title}",
        is_volume=is_volume,
    )
    chart_b64 = _encode_chart_base64(chart_path)

    latest_period = dates[-1] if dates else None

    return {
        "summary": "\n".join(lines),
        "data": data,
        "chart_file_path": chart_path,
        "chart_base64": chart_b64,
        "unit_code": effective_unit,
        "period_date": latest_period,
        "concept_name": concept_title,
        "step_name": f"Evolución consultada ({concept_title})",
        "step_detail": f"GET /api/v1/analytics/execute-query ({concept_title})",
    }


@traceable(name="run_deep_financial_analysis", run_type="tool")
def run_deep_financial_analysis(
    user_context: UserContext,
    query: str,
    unit_code: str | None = None,
    knowledge_domain: str | None = None,
    thread_id: str | None = None,
    resume_choice: str | None = None,
    workflow_fn: Callable[..., Any] | None = None,
    status_callback: Callable[[str], None] | None = None,
    conversation_id: str | None = None,
    is_unauthorized_unit_access: bool = False,
    unauthorized_target_unit: str | None = None,
) -> dict[str, Any]:

    """Execute the 9-node Analytics LangGraph state machine with deterministic RBAC tenant scoping."""
    norm_requested = _normalize_unit(unit_code)

    if user_context.role == "resident":
        assigned_unit = _normalize_unit(user_context.unit_code) or ""
        assigned_num = assigned_unit.split("-")[-1]
        assigned_has_tower = "-" in assigned_unit

        is_different_unit = False
        if norm_requested and norm_requested != assigned_unit:
            req_num = norm_requested.split("-")[-1]
            if norm_requested != assigned_num and not (not assigned_has_tower and req_num == assigned_num):
                is_different_unit = True

        if is_unauthorized_unit_access or is_different_unit or unauthorized_target_unit:
            target_val = unauthorized_target_unit or (norm_requested if is_different_unit else None)
            target_str = f" (no puedes consultar `{target_val}`)" if target_val else ""
            logger.warning(
                "RBAC block: Resident '%s' (%s) attempted to analyze unauthorized unit '%s'",
                user_context.email,
                assigned_unit,
                target_val or norm_requested,
            )
            msg = (
                f"Acceso denegado: Tu perfil de residente solo está autorizado para "
                f"analizar la unidad **{assigned_unit}**{target_str}."
            )
            return {"error": msg, "unauthorized": True, "summary": msg}

        if _is_cross_unit_cohort_query(query):
            msg = (
                f"Acceso denegado: Las comparativas o rankings entre múltiples departamentos "
                f"están restringidos a la Administración. Puedes consultar la evolución de tu unidad (**{assigned_unit}**) "
                f"o los gastos generales del edificio."
            )
            return {"error": msg, "unauthorized": True, "summary": msg}

        # Allow resident to analyze building-wide macro expenses when explicitly requested or selected
        if knowledge_domain == "building":
            effective_unit = None
        else:
            effective_unit = norm_requested if (norm_requested and "-" in norm_requested and "-" not in assigned_unit) else assigned_unit
    else:
        effective_unit = None if knowledge_domain == "building" else norm_requested

    runner = workflow_fn or run_analytics_workflow
    import inspect
    sig = inspect.signature(runner)
    has_var_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    kwargs: dict[str, Any] = {
        "query": query,
        "target_unit": effective_unit,
        "thread_id": thread_id,
        "resume_choice": resume_choice,
    }
    if knowledge_domain is not None and ("knowledge_domain" in sig.parameters or has_var_kw):
        kwargs["knowledge_domain"] = knowledge_domain
    if "status_callback" in sig.parameters:
        kwargs["status_callback"] = status_callback
    if conversation_id is not None and ("conversation_id" in sig.parameters or has_var_kw):
        kwargs["conversation_id"] = conversation_id

    result = runner(**kwargs)


    if result.needs_clarification:
        step_name = "Revisión inicial completada"
    else:
        step_name = "Análisis financiero completado"

    if user_context.role == "admin":
        step_detail = result.sql_query or f"Análisis ejecutado para consulta: {query}"
    else:
        scope_label = f"la unidad {effective_unit}" if effective_unit else "el condominio"
        step_detail = f"Información verificada en los registros financieros oficiales de {scope_label}."

    chart_b64 = _encode_chart_base64(result.chart_file_path)

    return {
        "summary": result.summary,
        "is_authorized": result.is_authorized,
        "needs_clarification": result.needs_clarification,
        "clarification_prompt": result.clarification_prompt,
        "candidate_options": result.candidate_options,
        "thread_id": result.thread_id,
        "sql_query": result.sql_query,
        "data": result.data,
        "chart_file_path": result.chart_file_path,
        "chart_base64": chart_b64,
        "unit_code": effective_unit,
        "step_name": step_name,
        "step_detail": step_detail,
    }
