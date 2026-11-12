"""LangGraph Conversational Supervisor orchestrating Fast-Path tools, Deep Analytics, Clarification, and RBAC."""

from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any, Callable
import uuid
from google import genai
from google.genai import types
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from backend.app.agent.analytics.config import get_analytics_settings
from backend.app.agent.analytics.gateway_client import query_data_gateway
from backend.app.agent.analytics.nodes import (
    generate_content_with_fallback,
    get_gemini_client,
)
from backend.app.agent.analytics.tools.spend_evolution import (
    format_catalog_for_prompt,
    get_expense_concept_catalog,
)
from backend.app.agent.state import (
    FocusContext,
    KnowledgeDomain,
    SupervisorDecision,
    SupervisorRoute,
    SupervisorState,
    SupervisorTurnResult,
    UserContext,
)
from backend.app.agent.tools import (
    _encode_chart_base64,
    check_system_status,
    lookup_building_summary,
    lookup_spend_evolution,
    lookup_unit_bill,
    run_deep_financial_analysis,
)

logger = logging.getLogger("backend.agent.supervisor")

MAX_EPHEMERAL_MESSAGES = 8
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "supervisor_system.md"

SPANISH_MONTHS: dict[str, int] = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def load_supervisor_prompt(client: Any | None = None) -> str:
    """Load the supervisor system prompt from markdown template and inject live database catalog."""
    base_prompt = (
        PROMPT_PATH.read_text(encoding="utf-8")
        if PROMPT_PATH.exists()
        else "Eres el Supervisor Conversacional de Inteligencia Financiera de Condominios."
    )
    try:
        catalog = get_expense_concept_catalog(client=client)
        catalog_snippet = format_catalog_for_prompt(catalog)
        return f"{base_prompt}\n\n---\n\n{catalog_snippet}"
    except Exception as exc:
        logger.debug("Could not inject dynamic catalog into supervisor prompt: %s", exc)
        return base_prompt


def _previous_month_iso(period_date: str) -> str:
    """Compute the previous month ISO date 'YYYY-MM-01' from 'YYYY-MM-01'."""
    m = re.match(r"^(\d{4})-(\d{2})(?:-\d{2})?$", period_date.strip())
    if not m:
        return period_date
    year = int(m.group(1))
    month = int(m.group(2))
    if month == 1:
        return f"{year - 1:04d}-12-01"
    return f"{year:04d}-{month - 1:02d}-01"


def _extract_period_from_text(text: str, focus: FocusContext) -> str | None:
    """Extract ISO period 'YYYY-MM-01' from Spanish text or resolve relative follow-up ('mes anterior')."""
    lowered = text.lower()

    # 1. Explicit ISO YYYY-MM-01 or YYYY-MM
    m_iso = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])(?:-01)?\b", lowered)
    if m_iso:
        return f"{m_iso.group(1)}-{m_iso.group(2)}-01"

    # 2. Relative anaphora: "el mes anterior" / "mes pasado"
    if re.search(r"\b(mes\s+anterior|mes\s+pasado|periodo\s+anterior)\b", lowered):
        if focus.last_period:
            return _previous_month_iso(focus.last_period)

    # 3. Spanish month name + optional year (defaults to year of focus.last_period or 2026)
    for month_name, month_num in SPANISH_MONTHS.items():
        if re.search(rf"\b{month_name}\b", lowered):
            m_year = re.search(r"\b(20\d{2})\b", lowered)
            if m_year:
                year = int(m_year.group(1))
            elif focus.last_period and re.match(r"^\d{4}-", focus.last_period):
                year = int(focus.last_period[:4])
            else:
                year = 2026
            return f"{year:04d}-{month_num:02d}-01"

    return None


def _extract_unit_dimensions(text: str) -> tuple[str | None, int | None, str | None]:
    """Extract (unit_tower, unit_number, canonical_unit_code) from text if present."""
    # 1. Explicit "depto 504 torre B" or "504 de la torre B"
    m_num_tower = re.search(
        r"\b(?:depto|departamento|dpto|unidad)?\s*(\d{1,4})\s*(?:de\s+la\s+|en\s+)?torre\s+([A-Za-z0-9]{1,2})\b",
        text,
        re.I,
    )
    if m_num_tower:
        num = int(m_num_tower.group(1))
        tower = m_num_tower.group(2).upper()
        return tower, num, f"{tower}-{num}"

    # 2. Explicit "Torre B depto 504" or "Torre B - 504"
    m_tower_num = re.search(
        r"\btorre\s+([A-Za-z0-9]{1,2})\s*(?:[-]|depto|departamento|dpto|unidad)?\s*(\d{1,4})\b",
        text,
        re.I,
    )
    if m_tower_num:
        tower = m_tower_num.group(1).upper()
        num = int(m_tower_num.group(2))
        return tower, num, f"{tower}-{num}"

    # 3. Explicit TOWER-NUMBER (e.g. B-504)
    m1 = re.search(r"\b([A-Za-z]{1,2})\s*[-]\s*(\d{1,4})\b", text)
    if m1:
        tower = m1.group(1).upper()
        num = int(m1.group(2))
        return tower, num, f"{tower}-{num}"

    # 4. Explicit depto/unidad NUMBER-TOWER or NUMBER TOWER (e.g. depto 504B)
    m2 = re.search(
        r"\b(?:depto|departamento|dpto|unidad)\s*[-#]?\s*(\d{1,4})\s*[-]?\s*([A-Za-z]{1,2})\b",
        text,
        re.I,
    )
    if m2:
        word_after = m2.group(2).upper()
        if word_after not in ("DE", "EN", "EL", "LA", "Y", "O", "A"):
            num = int(m2.group(1))
            return word_after, num, f"{word_after}-{num}"

    # 5. Explicit NUMBER-TOWER or NUMBER<TOWER> (e.g. 504-B, 504B)
    m3 = re.search(r"\b(\d{1,4})\s*[-]\s*([A-Za-z]{1,2})\b", text)
    if m3:
        tower = m3.group(2).upper()
        num = int(m3.group(1))
        return tower, num, f"{tower}-{num}"

    m3_compact = re.search(r"\b(\d{2,4})([A-Za-z])\b", text)
    if m3_compact:
        num_val = int(m3_compact.group(1))
        letter = m3_compact.group(2).upper()
        if not (num_val > 1900 and letter in ("M", "K")):
            return letter, num_val, f"{letter}-{num_val}"

    # 6. Bare unit number without tower (e.g. "depto 504")
    m4 = re.search(r"\b(?:depto|departamento|dpto|unidad)\s*[-#]?\s*(\d{1,4})\b", text, re.I)
    if m4:
        num = int(m4.group(1))
        return None, num, str(num)

    return None, None, None


def _extract_unit_from_text(text: str) -> str | None:
    """Extract explicit apartment code from user query if present."""
    _, _, code = _extract_unit_dimensions(text)
    return code


def _lookup_towers_for_unit_number(unit_number: int, client: Any | None = None) -> list[str]:
    """Query Data Gateway (3NF units table, with fallback) for distinct towers matching a bare apartment number."""
    try:
        sql_units = (
            f"SELECT DISTINCT tower FROM units "
            f"WHERE unit_number = {int(unit_number)} ORDER BY tower ASC;"
        )
        rows, err = query_data_gateway(sql=sql_units, client=client)
        if rows and not err:
            return [str(r["tower"]).strip().upper() for r in rows if r.get("tower")]
    except Exception as exc:
        logger.debug("Tower lookup on units table failed: %s", exc)

    try:
        sql_legacy = (
            f"SELECT DISTINCT tower FROM unit_bills "
            f"WHERE unit_number = {int(unit_number)} ORDER BY tower ASC;"
        )
        rows, err = query_data_gateway(sql=sql_legacy, client=client)
        if rows and not err:
            return [str(r["tower"]).strip().upper() for r in rows if r.get("tower")]
    except Exception as exc:
        logger.debug("Fallback tower lookup on unit_bills failed: %s", exc)
    return []


def _check_tower_only_guard(text: str) -> SupervisorDecision | None:
    """Enforce Granularity Guard when user queries expenses for a tower alone without unit number."""
    tower, num, _ = _extract_unit_dimensions(text)
    if tower or num is not None:
        return None
    m_tower_only = re.search(r"\btorre\s+([A-Za-z0-9]{1,2})\b", text, re.I)
    if m_tower_only and re.search(
        r"\b(gasto\w*|cobro\w*|evolucion\w*|consumo\w*|total\w*|cuenta\w*|boleta\w*)\b",
        text,
        re.I,
    ):
        tower_label = m_tower_only.group(1).upper()
        prompt = (
            f"Los gastos comunes no están desagregados por torre (Torre {tower_label}), "
            "sino a nivel de edificio completo o de departamento individual. "
            "Por favor indica el número de departamento específico a consultar."
        )
        return SupervisorDecision(
            route="clarify_intent",
            reasoning="Granularity Guard: consulta por torre sin número de departamento.",
            resolved_query=text,
            needs_clarification=True,
            clarification_prompt=prompt,
            candidate_options=[],
        )
    return None


def _build_turn1_scope_clarification(
    user_message: str,
    user_context: UserContext,
    period_date: str | None,
) -> SupervisorDecision:
    """Build Turn 1 Building vs. Unit scope clarification decision for Resident (3 options) or Admin (2 options)."""
    if user_context.role == "resident":
        unit = user_context.unit_code or "B-504"
        prompt = "Para darte una respuesta precisa, ¿qué alcance deseas consultar?"
        options = [
            f"Mi departamento ({unit})",
            "Gasto general del edificio",
            f"Ambos (impacto del edificio en mi unidad {unit})",
        ]
    else:
        prompt = (
            "Para darte una respuesta precisa, ¿deseas consultar el gasto general del edificio "
            "o un departamento específico?"
        )
        options = [
            "Gasto general del edificio",
            "Consultar un departamento específico",
        ]

    return SupervisorDecision(
        route="clarify_intent",
        reasoning="Consulta inicial ambigua entre alcance de edificio y unidad; se solicita aclaración.",
        resolved_query=user_message,
        period_date=period_date,
        needs_clarification=True,
        clarification_prompt=prompt,
        candidate_options=options,
    )


def _apply_entity_and_scope_guards(
    decision: SupervisorDecision,
    user_message: str,
    user_context: UserContext,
    focus: FocusContext,
) -> SupervisorDecision:
    """Enforce deterministic unit/tower resolution, RBAC unit preservation, and Turn 1 scope disambiguation."""
    # 1. Pure greeting or system status need no unit/scope guards
    if decision.route in ("direct_response", "check_system_status"):
        return decision

    # 2. Tower-only Granularity Guard
    tower_guard = _check_tower_only_guard(user_message)
    if tower_guard is not None:
        return tower_guard

    tower, unit_num, extracted_code = _extract_unit_dimensions(user_message)
    period_date = decision.period_date or _extract_period_from_text(user_message, focus)
    decision.period_date = period_date

    has_multi_unit_cohort = bool(
        re.search(
            r"\b(ranking|compar\w*|todos\s+los\s+departamentos|departamentos\s+con\s+mayor|"
            r"departamentos\s+en\s+mora|mora|morosos|vecinos)\b",
            user_message,
            re.I,
        )
    )

    # 3. Missing-tower check when bare unit number is given (e.g. "depto 504")
    if unit_num is not None and tower is None:
        # If resident is querying a different bare unit number, pass through so RBAC blocks it deterministically!
        if user_context.role == "resident" and user_context.unit_code:
            assigned_num = user_context.unit_code.split("-")[-1]
            if str(unit_num) != assigned_num:
                decision.unit_code = str(unit_num)
                decision.knowledge_domain = "unit"
                if decision.route == "clarify_intent":
                    decision.route = (
                        "run_deep_financial_analysis"
                        if ("por que" in user_message.lower() or "aument" in user_message.lower())
                        else "lookup_unit_bill"
                    )
                    decision.needs_clarification = False
                return decision

        # Discover towers in DB for this unit number
        towers = _lookup_towers_for_unit_number(unit_num)
        if len(towers) != 1:
            options = (
                [f"Torre {t} ({t}-{unit_num})" for t in towers]
                if towers
                else [f"Torre A (A-{unit_num})", f"Torre B (B-{unit_num})"]
            )
            prompt = f"¿A qué torre corresponde el departamento {unit_num}?"
            return SupervisorDecision(
                route="clarify_intent",
                reasoning=f"El departamento {unit_num} requiere especificar la torre.",
                resolved_query=user_message,
                period_date=period_date,
                needs_clarification=True,
                clarification_prompt=prompt,
                candidate_options=options,
            )
        # Exactly 1 tower found in DB -> auto-resolve
        resolved_unit = f"{towers[0]}-{unit_num}"
        decision.unit_code = resolved_unit
        decision.knowledge_domain = decision.knowledge_domain or "unit"
    elif tower and unit_num is not None:
        decision.unit_code = f"{tower}-{unit_num}"

    # 4. If LLM already requested clarify_intent, ensure prompt/options are well-formed
    if decision.route == "clarify_intent":
        decision.needs_clarification = True
        if not decision.clarification_prompt:
            fallback_clarify = _build_turn1_scope_clarification(
                user_message, user_context, period_date
            )
            decision.clarification_prompt = fallback_clarify.clarification_prompt
            decision.candidate_options = fallback_clarify.candidate_options
        return decision

    # 5. Admin requesting a unit-scoped route without any unit specified -> prompt to type unit code
    if (
        user_context.role == "admin"
        and decision.route == "lookup_unit_bill"
        and not decision.unit_code
        and not focus.last_unit
    ):
        return SupervisorDecision(
            route="clarify_intent",
            reasoning="Administrador solicitó consulta de unidad sin indicar el código del departamento.",
            resolved_query=user_message,
            period_date=period_date,
            needs_clarification=True,
            clarification_prompt="Por favor escribe el código del departamento que deseas consultar (por ejemplo, `B-504`).",
            candidate_options=[],
        )

    # 6. Turn 1 Cold-Start Scope Ambiguity (Building vs. Unit)
    if (
        decision.knowledge_domain is None
        and focus.last_domain is None
        and extracted_code is None
        and not decision.unit_code
    ):
        return _build_turn1_scope_clarification(user_message, user_context, period_date)

    # 7. Turn 2+ FocusContext Inheritance when domain/unit not explicitly overridden
    if not decision.knowledge_domain:
        if focus.last_domain in ("building", "unit", "cross_domain"):
            decision.knowledge_domain = focus.last_domain
        elif extracted_code or decision.route == "lookup_unit_bill":
            decision.knowledge_domain = "unit"
        else:
            decision.knowledge_domain = (
                "unit" if user_context.role == "resident" else "building"
            )

    if decision.knowledge_domain == "building":
        decision.unit_code = None
    elif decision.knowledge_domain in ("unit", "cross_domain") and not decision.unit_code:
        decision.unit_code = (
            user_context.unit_code if user_context.role == "resident" else focus.last_unit
        )

    # If resident's unit_code is a bare number (e.g. "504") and a single tower exists in DB, resolve it
    if (
        decision.unit_code
        and "-" not in decision.unit_code
        and decision.unit_code.isdigit()
    ):
        towers = _lookup_towers_for_unit_number(int(decision.unit_code))
        if len(towers) == 1:
            decision.unit_code = f"{towers[0]}-{int(decision.unit_code)}"

    # If Admin ended up in unit/cross_domain without a unit_code, ask for the unit code conversationally
    if (
        user_context.role == "admin"
        and decision.knowledge_domain in ("unit", "cross_domain")
        and not decision.unit_code
        and not has_multi_unit_cohort
    ):
        return SupervisorDecision(
            route="clarify_intent",
            reasoning="Se requiere conocer el código del departamento para ejecutar la consulta de unidad.",
            resolved_query=user_message,
            period_date=period_date,
            needs_clarification=True,
            clarification_prompt="Por favor escribe el código del departamento que deseas consultar (por ejemplo, `B-504`).",
            candidate_options=[],
        )

    return decision


def _resolve_supervisor_clarification_choice(
    choice_text: str,
    user_context: UserContext,
    focus: FocusContext,
) -> SupervisorDecision | None:
    """Resolve a user response to a pending Supervisor clarify_intent turn."""
    pending_query = focus.pending_clarification_query
    if not pending_query:
        return None

    lowered = choice_text.strip().lower()
    period_date = _extract_period_from_text(pending_query, focus) or focus.last_period
    target_route: SupervisorRoute = focus.pending_clarification_route or "lookup_unit_bill"

    # 1. Option: "Consultar un departamento específico" (Admin two-step flow)
    if "consultar un departamento espec" in lowered:
        return SupervisorDecision(
            route="clarify_intent",
            reasoning="Administrador seleccionó consultar un departamento específico; se solicita el código de unidad.",
            resolved_query=pending_query,
            period_date=period_date,
            needs_clarification=True,
            clarification_prompt="Por favor escribe el código del departamento que deseas consultar (por ejemplo, `B-504`).",
            candidate_options=[],
            resume_pending_thread=True,
        )

    # 2. Option: "Ambos (impacto del edificio en mi unidad ...)" -> cross_domain
    if lowered.startswith("ambos") or "impacto del edificio" in lowered:
        unit = (
            _extract_unit_from_text(choice_text)
            or (user_context.unit_code if user_context.role == "resident" else focus.last_unit)
        )
        return SupervisorDecision(
            route="run_deep_financial_analysis",
            reasoning="Usuario seleccionó análisis combinado (cross_domain) de edificio y unidad.",
            resolved_query=f"{pending_query} (Analizar impacto del edificio en la unidad {unit})",
            knowledge_domain="cross_domain",
            unit_code=unit,
            period_date=period_date,
            resume_pending_thread=True,
        )

    # 3. Option: "Gasto general del edificio"
    if "gasto general del edificio" in lowered or lowered in ("edificio", "el edificio", "2"):
        bldg_route: SupervisorRoute = (
            focus.pending_clarification_route
            if focus.pending_clarification_route in ("run_deep_financial_analysis", "lookup_spend_evolution")
            else "lookup_building_summary"
        )
        return SupervisorDecision(
            route=bldg_route,
            reasoning="Usuario seleccionó alcance de gasto general del edificio.",
            resolved_query=f"{pending_query} (Alcance: gasto general del edificio)",
            knowledge_domain="building",
            unit_code=None,
            period_date=period_date,
            resume_pending_thread=True,
        )

    # 4. Option: "Mi departamento (B-504)" or explicit unit/tower selection like "Torre B (B-504)" or typed "B-504"
    tower, unit_num, _ = _extract_unit_dimensions(choice_text)
    if "mi departamento" in lowered or "mi unidad" in lowered or (tower and unit_num is not None):
        unit = (
            f"{tower}-{unit_num}"
            if (tower and unit_num is not None)
            else (user_context.unit_code if user_context.role == "resident" else focus.last_unit)
        )
        unit_route: SupervisorRoute = (
            focus.pending_clarification_route
            if focus.pending_clarification_route in ("run_deep_financial_analysis", "lookup_spend_evolution")
            else "lookup_unit_bill"
        )
        return SupervisorDecision(
            route=unit_route,
            reasoning=f"Usuario seleccionó alcance de unidad individual ({unit}).",
            resolved_query=f"{pending_query} (Unidad {unit})",
            knowledge_domain="unit",
            unit_code=unit,
            period_date=period_date,
            resume_pending_thread=True,
        )

    # 5. Bare unit number typed by Admin when asked for unit code (e.g. "504") -> trigger tower lookup
    if unit_num is not None and tower is None and re.match(r"^(?:depto|departamento|unidad)?\s*[-#]?\s*\d{1,4}$", lowered):
        towers = _lookup_towers_for_unit_number(unit_num)
        if len(towers) == 1:
            unit = f"{towers[0]}-{unit_num}"
            return SupervisorDecision(
                route=target_route,
                reasoning=f"Unidad {unit} resuelta para la consulta pendiente.",
                resolved_query=f"{pending_query} (Unidad {unit})",
                knowledge_domain="unit",
                unit_code=unit,
                period_date=period_date,
                resume_pending_thread=True,
            )
        options = (
            [f"Torre {t} ({t}-{unit_num})" for t in towers]
            if towers
            else [f"Torre A (A-{unit_num})", f"Torre B (B-{unit_num})"]
        )
        return SupervisorDecision(
            route="clarify_intent",
            reasoning=f"El departamento {unit_num} requiere especificar la torre.",
            resolved_query=pending_query,
            period_date=period_date,
            needs_clarification=True,
            clarification_prompt=f"¿A qué torre corresponde el departamento {unit_num}?",
            candidate_options=options,
            resume_pending_thread=True,
        )

    return None


def _looks_like_analytics_clarification_answer(text: str) -> bool:
    """Heuristic check for whether free text answers a paused Analytics Hop 1 clarification."""
    lowered = text.strip().lower()
    if re.match(r"^[123]$", lowered):
        return True
    if re.search(
        r"\b(opci[oó]n\s*[123]|primera|segunda|tercera|analizar\s+el|analizar\s+la|"
        r"alza\s+de|baja\s+de|variaci[oó]n\s+de|comparaci[oó]n|torre\s+[a-z])\b",
        lowered,
    ):
        return True
    return False


def check_pure_greeting(
    user_message: str,
    user_context: UserContext,
) -> SupervisorDecision | None:
    """Fast-path check for pure greetings and help intents to avoid unnecessary LLM calls."""
    text = user_message.strip()
    lowered = text.lower()
    is_pure_greeting_or_help = bool(
        re.match(
            r"^[¡!¿?\s]*(hola|buenos\s+d[ií]as|buenas\s+tardes|buenas\s+noches|hey|saludos|"
            r"gracias|muchas\s+gracias|ayuda|help|qu[eé]\s+puedes\s+hacer|qui[eé]n\s+eres)[¡!¿?.\s]*$",
            lowered,
        )
    )
    if is_pure_greeting_or_help or not lowered:
        role_label = (
            f"Residente (Unidad **{user_context.unit_code}**)"
            if user_context.role == "resident"
            else "Administración / Comité"
        )
        reply = (
            f"¡Hola! Soy el asistente financiero del condominio (Perfil activo: {role_label}).\n\n"
            "Puedo ayudarte con:\n"
            "- **Consultas rápidas de tu unidad**: *«¿Cuánto debo pagar este mes?»*, *«¿Cuál fue mi consumo de agua caliente?»*\n"
            "- **Resumen general del edificio**: *«¿Cuál fue el gasto común total del edificio en septiembre?»*\n"
            "- **Evolución y tendencias temporales**: *«¿Cómo ha evolucionado el gasto de mantenciones?»*, *«Evolución de mi gasto común este año»*\n"
            "- **Análisis de variaciones y causas**: *«¿Por qué subió mi gasto común?»*, *«¿Qué explica el aumento en julio?»*"
        )
        if user_context.role == "admin":
            reply += "\n- **Diagnóstico del sistema**: *«¿Qué meses están cargados y cuál es el estado del spool?»*"

        return SupervisorDecision(
            route="direct_response",
            reasoning="Saludo o consulta conversacional general.",
            resolved_query=text,
            direct_reply=reply,
        )
    return None


def supervisor_router_node(
    state: SupervisorState,
    llm_client: genai.Client | None = None,
    http_client: Any | None = None,
) -> dict[str, Any]:
    """Classify user message into a SupervisorDecision using LLM-first routing and upfront clarification."""
    user_context = state["user_context"]
    focus = state.get("focus") or FocusContext()
    user_msg = (state.get("user_message") or "").strip()
    clarification_choice = state.get("clarification_choice")

    # 1. Explicit button click (`clarification_choice` passed by Chainlit Action)
    if clarification_choice:
        # 1a. Resolving a Supervisor-level pending clarification
        if focus.pending_clarification_query and not focus.active_analytics_thread_id:
            sup_res = _resolve_supervisor_clarification_choice(
                clarification_choice, user_context, focus
            )
            if sup_res is not None:
                return {"decision": sup_res}

        # 1b. Resuming a paused Analytics Hop 1 clarification thread
        if focus.active_analytics_thread_id:
            decision = SupervisorDecision(
                route="run_deep_financial_analysis",
                reasoning="Reanudando flujo analítico pausado con la selección de aclaración del usuario.",
                resolved_query=clarification_choice,
                knowledge_domain=focus.last_domain if focus.last_domain in ("building", "unit", "cross_domain") else None,
                unit_code=user_context.unit_code if user_context.role == "resident" else focus.last_unit,
                period_date=focus.last_period,
                resume_pending_thread=True,
            )
            return {"decision": decision}

    # 2. Free-text message when a Supervisor-level clarification is pending
    if focus.pending_clarification_query and not focus.active_analytics_thread_id:
        sup_res = _resolve_supervisor_clarification_choice(
            user_msg, user_context, focus
        )
        if sup_res is not None:
            return {"decision": sup_res}

    # 3. Pure greeting fast-path (avoids unnecessary LLM call on "hola" / "ayuda")
    greeting_decision = check_pure_greeting(user_msg, user_context)
    if greeting_decision is not None:
        return {"decision": greeting_decision}

    # 4. Primary Router: Gemini LLM with SupervisorDecision structured schema
    active_llm_client = llm_client
    if active_llm_client is None:
        settings = get_analytics_settings()
        if settings.gemini_api_key:
            try:
                active_llm_client = get_gemini_client()
            except Exception as exc:
                logger.warning("Could not initialize Gemini client in supervisor router: %s", exc)
                active_llm_client = None

    if active_llm_client is not None:
        try:
            system_instruction = load_supervisor_prompt(client=http_client)
            history_lines = []
            for msg in (state.get("messages") or [])[-MAX_EPHEMERAL_MESSAGES:]:
                if isinstance(msg, dict):
                    prefix = "Usuario" if msg.get("role") == "user" else "Asistente"
                    history_lines.append(f"{prefix}: {msg.get('content', '')}")
                else:
                    prefix = "Usuario" if isinstance(msg, HumanMessage) else "Asistente"
                    history_lines.append(f"{prefix}: {msg.content}")

            context_prompt = (
                f"UserContext: role={user_context.role}, unit_code={user_context.unit_code}\n"
                f"FocusContext: {focus.model_dump_json()}\n"
                f"Recent Conversation:\n" + "\n".join(history_lines) + "\n\n"
                f"Current User Message: {user_msg}"
            )
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SupervisorDecision,
                system_instruction=system_instruction,
                temperature=0.0,
            )
            response = generate_content_with_fallback(
                client=active_llm_client,
                contents=[context_prompt],
                generation_config=config,
            )

            decision = SupervisorDecision.model_validate_json(response.text)

            # If an analytics thread was paused and LLM determined the user answered it
            if focus.active_analytics_thread_id and decision.resume_pending_thread:
                return {
                    "decision": SupervisorDecision(
                        route="run_deep_financial_analysis",
                        reasoning="Reanudando flujo analítico pausado con respuesta de texto del usuario.",
                        resolved_query=user_msg,
                        knowledge_domain=focus.last_domain if focus.last_domain in ("building", "unit", "cross_domain") else None,
                        unit_code=user_context.unit_code if user_context.role == "resident" else focus.last_unit,
                        period_date=focus.last_period,
                        resume_pending_thread=True,
                    )
                }

            if not decision.period_date:
                decision.period_date = _extract_period_from_text(user_msg, focus)
            if not decision.resolved_query:
                decision.resolved_query = user_msg

            # Guard: never allow LLM to return direct_response greeting for a non-greeting query
            if decision.route == "direct_response":
                return {
                    "decision": SupervisorDecision(
                        route="run_deep_financial_analysis",
                        reasoning="Recuperación de direct_response para consulta no saludo.",
                        resolved_query=user_msg,
                        knowledge_domain=decision.knowledge_domain or focus.last_domain or "building",
                        unit_code=decision.unit_code or focus.last_unit,
                        period_date=decision.period_date,
                    )
                }

            decision = _apply_entity_and_scope_guards(decision, user_msg, user_context, focus)
            return {"decision": decision}
        except Exception as exc:
            logger.warning(
                "Supervisor LLM routing failed (%s); returning service unavailable notice.",
                exc,
            )
            return {
                "decision": SupervisorDecision(
                    route="direct_response",
                    reasoning=f"AI service unavailable: {exc}",
                    resolved_query=user_msg,
                    direct_reply="El servicio de asistencia inteligente no se encuentra disponible temporalmente. Por favor, intenta nuevamente más tarde.",
                )
            }

    # 5. Offline fallback when Gemini is unavailable and message is not a pure greeting
    if focus.active_analytics_thread_id and _looks_like_analytics_clarification_answer(user_msg):
        return {
            "decision": SupervisorDecision(
                route="run_deep_financial_analysis",
                reasoning="Reanudando flujo analítico pausado con respuesta del usuario.",
                resolved_query=user_msg,
                knowledge_domain=focus.last_domain if focus.last_domain in ("building", "unit", "cross_domain") else None,
                unit_code=user_context.unit_code if user_context.role == "resident" else focus.last_unit,
                period_date=focus.last_period,
                resume_pending_thread=True,
            )
        }

    return {
        "decision": SupervisorDecision(
            route="direct_response",
            reasoning="Servicio LLM no disponible en modo offline.",
            resolved_query=user_msg,
            direct_reply="El servicio de asistencia inteligente no se encuentra disponible temporalmente. Por favor, intenta nuevamente más tarde.",
        )
    }


def execute_tool_node(
    state: SupervisorState,
    http_client: Any | None = None,
    workflow_fn: Callable[..., Any] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute the selected RBAC-enforced tool or Supervisor clarification and build SupervisorTurnResult."""
    decision = state["decision"]
    assert decision is not None
    user_context = state["user_context"]
    focus = (state.get("focus") or FocusContext()).model_copy(deep=True)
    user_msg = state.get("user_message", "")
    clarification_choice = state.get("clarification_choice")

    route: SupervisorRoute = decision.route

    # If user did not resume an active thread, clear stale pending state before executing new tool
    if route != "clarify_intent" and not decision.resume_pending_thread:
        focus.active_analytics_thread_id = None
        focus.pending_clarification_query = None
        focus.pending_clarification_route = None

    if route == "clarify_intent":
        if status_callback:
            status_callback("Preparando opciones de consulta...")
        prompt = (
            decision.clarification_prompt
            or "Para darte una respuesta precisa, por favor confirma el alcance de tu consulta."
        )
        if not focus.pending_clarification_query or not decision.resume_pending_thread:
            focus.pending_clarification_query = decision.resolved_query or user_msg
            focus.pending_clarification_route = (
                "run_deep_financial_analysis"
                if any(
                    w in focus.pending_clarification_query.lower()
                    for w in ("por que", "por qué", "aument", "subi", "variac", "cambi", "compara")
                )
                else "lookup_unit_bill"
            )
        if decision.period_date:
            focus.last_period = decision.period_date

        tool_out: dict[str, Any] = {
            "summary": prompt,
            "needs_clarification": True,
            "clarification_prompt": prompt,
            "candidate_options": list(decision.candidate_options or []),
        }
        turn_result = SupervisorTurnResult(
            reply=prompt,
            route="clarify_intent",
            step_name="Aclaración requerida",
            step_detail="Esperando confirmación de alcance o departamento.",
            needs_clarification=True,
            clarification_prompt=prompt,
            candidate_options=list(decision.candidate_options or []),
            focus=focus,
        )

    elif route == "lookup_unit_bill":
        if status_callback:
            status_callback("Consultando el estado de cuenta de la unidad...")
        focus.pending_clarification_query = None
        focus.pending_clarification_route = None
        tool_out = lookup_unit_bill(
            user_context=user_context,
            unit_code=decision.unit_code,
            period_date=decision.period_date,
            query_text=decision.resolved_query or user_msg,
            client=http_client,
        )
        if not tool_out.get("error"):
            focus.last_domain = "unit"
            focus.last_route = "lookup_unit_bill"
            focus.last_unit = tool_out.get("unit_code") or decision.unit_code
            focus.last_period = tool_out.get("period_date") or decision.period_date

        turn_result = SupervisorTurnResult(
            reply=tool_out["summary"],
            route=route,
            step_name=tool_out.get("step_name", "Consulta unidad"),
            step_detail=tool_out.get("step_detail"),
            focus=focus,
        )

    elif route == "lookup_building_summary":
        if status_callback:
            status_callback("Consultando el resumen de gastos del edificio...")
        focus.pending_clarification_query = None
        focus.pending_clarification_route = None
        tool_out = lookup_building_summary(
            user_context=user_context,
            period_date=decision.period_date,
            client=http_client,
        )
        if not tool_out.get("error"):
            focus.last_domain = "building"
            focus.last_route = "lookup_building_summary"
            focus.last_unit = None
            focus.last_period = tool_out.get("period_date") or decision.period_date

        turn_result = SupervisorTurnResult(
            reply=tool_out["summary"],
            route=route,
            step_name=tool_out.get("step_name", "Consulta edificio"),
            step_detail=tool_out.get("step_detail"),
            focus=focus,
        )

    elif route == "lookup_spend_evolution":
        if status_callback:
            status_callback("Consultando la evolución histórica del gasto...")
        focus.pending_clarification_query = None
        focus.pending_clarification_route = None

        tool_out = lookup_spend_evolution(
            user_context=user_context,
            knowledge_domain=decision.knowledge_domain or "building",
            unit_code=decision.unit_code,
            target_concept=decision.target_concept,
            time_scope=decision.time_scope,
            query_text=decision.resolved_query or user_msg,
            client=http_client,
        )

        focus.last_route = "lookup_spend_evolution"
        focus.last_domain = decision.knowledge_domain or "building"
        if decision.knowledge_domain == "unit":
            focus.last_unit = tool_out.get("unit_code") or decision.unit_code
        else:
            focus.last_unit = None
        if decision.target_concept:
            focus.last_concept = decision.target_concept
        if tool_out.get("period_date"):
            focus.last_period = tool_out.get("period_date")

        chart_path = tool_out.get("chart_file_path")
        chart_b64 = tool_out.get("chart_base64") or _encode_chart_base64(chart_path)

        turn_result = SupervisorTurnResult(
            reply=tool_out["summary"],
            route=route,
            step_name=tool_out.get("step_name", "Evolución consultada"),
            step_detail=tool_out.get("step_detail"),
            chart_file_path=chart_path,
            chart_base64=chart_b64,
            focus=focus,
        )

    elif route == "check_system_status":
        if status_callback:
            status_callback("Verificando los períodos cargados en el sistema...")
        focus.pending_clarification_query = None
        focus.pending_clarification_route = None
        tool_out = check_system_status(
            user_context=user_context,
            client=http_client,
        )
        if not tool_out.get("error"):
            focus.last_domain = "system"
            focus.last_route = "check_system_status"

        turn_result = SupervisorTurnResult(
            reply=tool_out["summary"],
            route=route,
            step_name=tool_out.get("step_name", "Diagnóstico del sistema"),
            step_detail=tool_out.get("step_detail"),
            focus=focus,
        )

    elif route == "run_deep_financial_analysis":
        focus.pending_clarification_query = None
        focus.pending_clarification_route = None
        is_analytics_resume = bool(
            focus.active_analytics_thread_id
            and (clarification_choice or decision.resume_pending_thread)
        )
        active_thread = focus.active_analytics_thread_id if is_analytics_resume else None
        effective_resume_choice = (
            (clarification_choice or user_msg) if is_analytics_resume else None
        )

        tool_out = run_deep_financial_analysis(
            user_context=user_context,
            query=decision.resolved_query or user_msg,
            unit_code=decision.unit_code,
            knowledge_domain=decision.knowledge_domain,
            thread_id=active_thread,
            resume_choice=effective_resume_choice,
            workflow_fn=workflow_fn,
            status_callback=status_callback,
            conversation_id=state.get("conversation_id"),
        )

        focus.last_route = "run_deep_financial_analysis"
        effective_tool_unit = tool_out.get("unit_code")
        if decision.knowledge_domain == "cross_domain":
            focus.last_domain = "cross_domain"
            focus.last_unit = effective_tool_unit or decision.unit_code
        elif effective_tool_unit:
            focus.last_domain = "unit"
            focus.last_unit = effective_tool_unit
        else:
            focus.last_domain = "building"
            if decision.unit_code is None:
                focus.last_unit = None
        if decision.period_date:
            focus.last_period = decision.period_date

        if tool_out.get("needs_clarification"):
            focus.active_analytics_thread_id = tool_out.get("thread_id")
        else:
            focus.active_analytics_thread_id = None

        chart_path = tool_out.get("chart_file_path")
        chart_b64 = tool_out.get("chart_base64") or _encode_chart_base64(chart_path)

        turn_result = SupervisorTurnResult(
            reply=tool_out["summary"],
            route=route,
            step_name=tool_out.get("step_name", "Análisis financiero completado"),
            step_detail=tool_out.get("step_detail"),
            chart_file_path=chart_path,
            chart_base64=chart_b64,
            needs_clarification=bool(tool_out.get("needs_clarification")),
            clarification_prompt=tool_out.get("clarification_prompt"),
            candidate_options=list(tool_out.get("candidate_options") or []),
            thread_id=tool_out.get("thread_id"),
            focus=focus,
        )

    else:
        tool_out = {"summary": decision.direct_reply or "¿En qué te puedo ayudar hoy?"}
        turn_result = SupervisorTurnResult(
            reply=tool_out["summary"],
            route="direct_response",
            focus=focus,
        )

    # Tag security violations so the security layer can record guardrail strikes in `security_audit_logs`
    if tool_out.get("unauthorized") is True:
        turn_result.security_violation_type = "rbac_unauthorized_access"
        turn_result.security_violation_reason = str(
            tool_out.get("error")
            or tool_out.get("summary")
            or "Intento de acceso no autorizado a datos de otra unidad o rol."
        )
    elif tool_out.get("is_authorized") is False:
        turn_result.security_violation_type = "scope_guard_probe"
        turn_result.security_violation_reason = str(
            tool_out.get("summary")
            or "Activación del guardrail de alcance (consulta fuera de dominio o inyección)."
        )

    new_messages: list[BaseMessage] = [
        HumanMessage(content=clarification_choice or user_msg),
        AIMessage(content=turn_result.reply),
    ]
    return {
        "tool_output": tool_out,
        "turn_result": turn_result,
        "focus": focus,
        "messages": new_messages,
    }


def build_supervisor_graph(
    llm_client: genai.Client | None = None,
    http_client: Any | None = None,
    workflow_fn: Callable[..., Any] | None = None,
    status_callback: Callable[[str], None] | None = None,
):
    """Compile the Conversational Supervisor LangGraph state machine."""
    graph = StateGraph(SupervisorState)

    def _router(state: SupervisorState) -> dict[str, Any]:
        return supervisor_router_node(state, llm_client=llm_client, http_client=http_client)

    def _executor(state: SupervisorState) -> dict[str, Any]:
        return execute_tool_node(
            state,
            http_client=http_client,
            workflow_fn=workflow_fn,
            status_callback=status_callback,
        )

    graph.add_node("supervisor_router", _router)
    graph.add_node("execute_tool", _executor)

    graph.add_edge(START, "supervisor_router")
    graph.add_edge("supervisor_router", "execute_tool")
    graph.add_edge("execute_tool", END)

    return graph.compile(name="ConversationalSupervisorGraph")


def _normalize_message_list(raw_messages: list[Any] | None) -> list[BaseMessage]:
    """Convert raw dicts or BaseMessage objects into LangChain BaseMessage list."""
    if not raw_messages:
        return []
    normalized: list[BaseMessage] = []
    for m in raw_messages:
        if isinstance(m, BaseMessage):
            normalized.append(m)
        elif isinstance(m, dict):
            role = str(m.get("role") or m.get("type") or "user").lower()
            content = str(m.get("content") or "")
            if role in ("assistant", "ai"):
                normalized.append(AIMessage(content=content))
            else:
                normalized.append(HumanMessage(content=content))
        else:
            normalized.append(HumanMessage(content=str(m)))
    return normalized


@traceable(name="supervisor_turn", run_type="chain")
def run_supervisor_turn(
    user_message: str,
    user_context: UserContext,
    messages: list[Any] | None = None,
    focus: FocusContext | None = None,
    clarification_choice: str | None = None,
    conversation_id: str | None = None,
    llm_client: genai.Client | None = None,
    http_client: Any | None = None,
    workflow_fn: Callable[..., Any] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> tuple[SupervisorTurnResult, list[BaseMessage], FocusContext]:
    """Execute one conversational turn through the Supervisor graph and trim ephemeral history to 8 messages.

    Returns:
        Tuple of (SupervisorTurnResult, updated_ephemeral_messages, updated_focus).
    """
    prior_messages = _normalize_message_list(messages)[-MAX_EPHEMERAL_MESSAGES:]
    active_focus = focus or FocusContext()
    active_conversation_id = conversation_id or str(uuid.uuid4())

    # Attach LangSmith trace run metadata & thread context
    run_tree = get_current_run_tree()
    active_run_id = str(run_tree.id) if run_tree is not None else str(uuid.uuid4())
    if run_tree is not None:
        run_tree.metadata["thread_id"] = active_conversation_id
        run_tree.metadata["session_id"] = active_conversation_id
        run_tree.metadata["user_uid"] = user_context.uid
        run_tree.metadata["role"] = user_context.role
        if user_context.knowledge_domain:
            run_tree.metadata["domain"] = user_context.knowledge_domain

    compiled = build_supervisor_graph(
        llm_client=llm_client,
        http_client=http_client,
        workflow_fn=workflow_fn,
        status_callback=status_callback,
    )

    initial_state: SupervisorState = {
        "messages": prior_messages,
        "user_message": user_message,
        "clarification_choice": clarification_choice,
        "user_context": user_context,
        "focus": active_focus,
        "conversation_id": active_conversation_id,
        "decision": None,
        "tool_output": None,
        "turn_result": None,
    }


    run_config: dict[str, Any] = {
        "configurable": {"thread_id": active_conversation_id},
        "metadata": {
            "thread_id": active_conversation_id,
            "session_id": active_conversation_id,
            "user_uid": user_context.uid,
            "role": user_context.role,
        },
        "tags": ["supervisor-turn", f"role:{user_context.role}"],
    }

    final_state = compiled.invoke(initial_state, config=run_config)
    turn_result: SupervisorTurnResult = final_state["turn_result"]
    turn_result.run_id = active_run_id
    updated_messages = list(final_state.get("messages") or [])[-MAX_EPHEMERAL_MESSAGES:]
    updated_focus: FocusContext = final_state.get("focus") or turn_result.focus

    return turn_result, updated_messages, updated_focus
