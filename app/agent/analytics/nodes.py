"""LangGraph node functions executing Planner, Trajectory Inspector/Evaluator, SQL Coder, Gateway, Visualizer, and Synthesizer."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from google import genai
from google.genai import types
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from backend.app.agent.analytics.config import get_analytics_settings
from backend.app.agent.analytics.gateway_client import (
    is_gateway_unavailable_error,
    query_data_gateway,
)
from backend.app.agent.analytics.prompt_loader import (
    load_modular_data_dictionary,
    load_prompt_template,
)
from backend.app.agent.analytics.sandbox import run_plotting_sandbox
from backend.app.agent.analytics.state import (
    DataAnalystState,
    ExecutionError,
    IntentPlan,
    SQLSubQuery,
    TrajectoryEvaluation,
)

logger = logging.getLogger("backend.agent.analytics.nodes")


def get_gemini_client() -> genai.Client:
    """Create and return a configured Gemini client."""
    settings = get_analytics_settings()
    api_key = settings.gemini_api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Please set it in your .env file or environment."
        )
    return genai.Client(api_key=api_key)


def generate_content_with_fallback(
    client: genai.Client,
    contents: list[Any],
    config: types.GenerateContentConfig,
) -> Any:
    """Execute Gemini generate_content across primary and fallback models."""
    settings = get_analytics_settings()
    candidate_models = settings.get_candidate_models()
    last_error: Exception | None = None

    for idx, current_model in enumerate(candidate_models):
        if idx > 0:
            logger.warning(
                "Model '%s' failed (%s). Activating fallback model '%s'...",
                candidate_models[idx - 1],
                last_error,
                current_model,
            )
        try:
            response = client.models.generate_content(
                model=current_model,
                contents=contents,
                config=config,
            )
            if not response.text:
                raise RuntimeError(f"Model '{current_model}' returned empty response.")
            return response
        except Exception as exc:
            last_error = exc
            logger.warning("Attempt with model '%s' failed: %s", current_model, exc)

    raise RuntimeError(
        f"All configured analytics models failed. Attempted models: {candidate_models}. "
        f"Last error: {last_error}"
    ) from last_error


class SQLCoderOutput(BaseModel):
    """Structured output schema for single or multi-query SQL generation."""

    reasoning: str = Field(description="Brief explanation of the query logic")
    sql_query: str = Field(
        default="",
        description="Primary read-only SELECT query (used when a single query suffices)",
    )
    queries: list[SQLSubQuery] = Field(
        default_factory=list,
        description="1 to 3 named read-only queries when conducting multiple analyses or comparisons (e.g. MoM + YoY + Variance).",
    )


class VisualizerOutput(BaseModel):
    """Structured output schema for Python script generation."""

    reasoning: str = Field(description="Explanation of chart layout and visual choices")
    python_code: str = Field(description="Complete, executable Python Matplotlib script")


# ============================================================================
# 1. Planner Node (with Scope Guard & Archetype Classification)
# ============================================================================


def planner_node(state: DataAnalystState, client: genai.Client | None = None) -> dict[str, Any]:
    """Analyze user prompt, evaluate scope guard, and produce an IntentPlan."""
    gemini_client = client or get_gemini_client()
    system_instruction = load_prompt_template("planner_system.md")

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=IntentPlan,
        system_instruction=system_instruction,
        temperature=0.0,
    )

    user_choice = state.get("user_clarification_choice")
    pre_target_unit = state.get("target_unit")
    pre_domain = state.get("knowledge_domain")
    query_context = f"User Query: {state['user_query']}"
    if pre_target_unit:
        query_context += f"\nTarget Unit Context: {pre_target_unit}"
    if pre_domain:
        query_context += f"\nPre-Verified Knowledge Domain: {pre_domain}"
    if user_choice:
        query_context += f"\nUser Clarification / Selected Focus: {user_choice}"

    contents = [query_context]

    response = generate_content_with_fallback(
        client=gemini_client,
        contents=contents,
        config=config,
    )

    plan = IntentPlan.model_validate_json(response.text)

    if pre_domain in ("building", "unit", "cross_domain"):
        plan.knowledge_domain = pre_domain
        if pre_domain == "building":
            plan.target_unit = None
            plan.unit_tower = None
            plan.unit_number = None

    if pre_target_unit and not plan.target_unit and pre_domain != "building":
        is_macro_only = bool(
            re.search(
                r"\b(total del edificio|gasto operacional del edificio|edificio completo)\b",
                state.get("user_query", ""),
                re.I,
            )
        ) and not bool(re.search(r"\b(mi|mio|mío|depto|departamento|unidad)\b", state.get("user_query", ""), re.I))
        if not is_macro_only or pre_domain in ("unit", "cross_domain"):
            plan.target_unit = pre_target_unit

    if user_choice:
        m_tower = re.search(r"Torre\s+([A-Za-z]+)", user_choice, re.I)
        if m_tower:
            plan.unit_tower = m_tower.group(1).upper()

    raw_text = f"{state.get('user_query', '')} {plan.target_unit or ''}"
    m_tower_only = re.search(r"\btorre\s+([A-Za-z0-9]{1,2})\b", raw_text, re.I)
    m_depto_num = re.search(r"\b(?:depto|departamento|dpto|unidad)\s*[-#]?\s*(\d{1,4})\b", raw_text, re.I)
    m3 = re.search(r"\b(?:depto|departamento|dpto|unidad)?\s*(\d{1,4})\s*(?:de\s+la\s+|en\s+)?torre\s+([A-Za-z0-9]{1,2})\b", raw_text, re.I)
    m4 = re.search(r"\btorre\s+([A-Za-z0-9]{1,2})\s*(?:depto|departamento|dpto|unidad)?\s*(\d{1,4})\b", raw_text, re.I)
    m2 = re.search(r"\b(\d{1,4})\s*[-]?\s*([A-Za-z]{1,2})\b", raw_text)
    m1 = re.search(r"\b([A-Za-z]{1,2})\s*[-]\s*(\d{1,4})\b", raw_text)

    if plan.knowledge_domain in ("unit", "cross_domain") or m_depto_num or m3 or m4 or m2 or m1:
        if not plan.unit_tower or not plan.unit_number:
            if m3:
                plan.unit_number = plan.unit_number or int(m3.group(1))
                plan.unit_tower = plan.unit_tower or m3.group(2).upper()
            elif m4:
                plan.unit_tower = plan.unit_tower or m4.group(1).upper()
                plan.unit_number = plan.unit_number or int(m4.group(2))
            elif m2:
                plan.unit_number = plan.unit_number or int(m2.group(1))
                plan.unit_tower = plan.unit_tower or m2.group(2).upper()
            elif m1:
                plan.unit_tower = plan.unit_tower or m1.group(1).upper()
                plan.unit_number = plan.unit_number or int(m1.group(2))
            elif m_depto_num:
                plan.unit_number = plan.unit_number or int(m_depto_num.group(1))

    # Granularity Guard: Reject tower-only requests without unit number
    if m_tower_only and not m_depto_num and not m3 and not m4 and not m2 and not m1 and not plan.unit_number:
        if re.search(r"\b(gasto|cobro|evolucion|consumo|total).*\btorre\s+([A-Za-z]+)\b", raw_text, re.I):
            plan.needs_clarification = True
            plan.clarification_prompt = (
                f"Los gastos comunes no están desagregados por torre (Torre {m_tower_only.group(1).upper()}), "
                "sino a nivel de edificio completo o de departamento individual. "
                "Por favor indica el número de departamento específico a consultar."
            )
            plan.candidate_options = []

    # Dynamic DB-backed clarification if unit_number is present but tower is missing (3NF schema: units table)
    if plan.unit_number and not plan.unit_tower and not user_choice:
        plan.knowledge_domain = "unit"
        towers: list[str] = []
        try:
            sql_towers = (
                f"SELECT DISTINCT u.tower FROM units u "
                f"WHERE u.unit_number = {int(plan.unit_number)} ORDER BY u.tower ASC;"
            )
            rows, err = query_data_gateway(sql=sql_towers)
            if rows and not err:
                towers = [str(r["tower"]) for r in rows if r.get("tower")]
        except Exception:
            towers = []
        plan.needs_clarification = True
        plan.clarification_prompt = f"¿A qué torre corresponde el departamento {plan.unit_number}?"
        if towers:
            plan.candidate_options = [f"Torre {t} ({t}-{plan.unit_number})" for t in towers]
        else:
            plan.candidate_options = [f"Torre A (A-{plan.unit_number})", f"Torre B (B-{plan.unit_number})"]

    # Canonicalize target_unit when both are present and enforce Unit-First Focus Guard
    if plan.unit_tower and plan.unit_number:
        plan.target_unit = f"{plan.unit_tower.upper().strip()}-{plan.unit_number}"
        is_explicit_building_inquiry = (
            pre_domain == "cross_domain"
            or bool(
                re.search(
                    r"\b(edificio|macro|caldera|compara.*edificio|en relacion al edificio|respecto al edificio|impacto del edificio)\b",
                    raw_text,
                    re.I,
                )
            )
        )
        if pre_domain == "cross_domain":
            plan.knowledge_domain = "cross_domain"
            if len(plan.sub_analyses) < 2:
                plan.sub_analyses = [
                    f"Desglose y variación de componentes de la boleta de la unidad {plan.target_unit} (MoM)",
                    "Atribución de causas macro en el gasto general y caldera del edificio",
                ]
        elif not is_explicit_building_inquiry:
            plan.knowledge_domain = "unit"
            if len(plan.sub_analyses) > 1:
                unit_subs = [
                    s
                    for s in plan.sub_analyses
                    if re.search(
                        r"\b(unidad|depto|departamento|" + str(plan.unit_number) + r")\b",
                        s,
                        re.I,
                    )
                ]
                plan.sub_analyses = (
                    unit_subs[:1]
                    if unit_subs
                    else [f"Evolución y desglose mensual de la unidad {plan.target_unit}"]
                )

    if plan.analysis_type in ("variance_driver", "time_series_trend"):
        plan.temporal_resolution = "monthly"

    if user_choice:
        plan.needs_clarification = False

    logger.info(
        "Planner produced plan: authorized=%s, type=%s, goal=%s, clarify=%s, tower=%s, unit=%s",
        plan.is_authorized_query,
        plan.analysis_type,
        plan.analysis_goal,
        plan.needs_clarification,
        plan.unit_tower,
        plan.unit_number,
    )

    return {
        "intent_plan": plan,
        "target_unit": plan.target_unit if plan else None,
        "unit_tower": plan.unit_tower if plan else None,
        "unit_number": plan.unit_number if plan else None,
        "retry_count": 0,
    }


# ============================================================================
# 1b. Macro Trajectory Inspector Node (Hop 1 — 3NF Schema JOINs)
# ============================================================================


def trajectory_inspector_node(state: DataAnalystState) -> dict[str, Any]:
    """Execute Hop 1 macro query on expense_reports (or unit_bills joined with units and billing_periods)."""
    target_unit = state.get("target_unit")
    tower = state.get("unit_tower")
    number = state.get("unit_number")
    plan = state.get("intent_plan")
    if plan:
        target_unit = target_unit or plan.target_unit
        tower = tower or plan.unit_tower
        number = number or plan.unit_number

    if tower and number is not None:
        safe_tower = tower.strip().upper().replace("'", "")
        sql = (
            "SELECT b.period_date, p.period_year, p.period_month, "
            "b.calculated_dept_subtotal, b.calculated_consumo_total, "
            "b.calculated_unit_subtotal, b.calculated_total_month AS calculated_grand_total "
            "FROM unit_bills b "
            "JOIN units u ON b.unit_code = u.unit_code "
            "JOIN billing_periods p ON b.period_date = p.period_date "
            f"WHERE u.tower = '{safe_tower}' AND u.unit_number = {int(number)} ORDER BY b.period_date ASC"
        )
    elif target_unit:
        safe_unit = target_unit.strip().upper().replace("'", "")
        sql = (
            "SELECT b.period_date, p.period_year, p.period_month, "
            "b.calculated_dept_subtotal, b.calculated_consumo_total, "
            "b.calculated_unit_subtotal, b.calculated_total_month AS calculated_grand_total "
            "FROM unit_bills b "
            "JOIN billing_periods p ON b.period_date = p.period_date "
            f"WHERE b.unit_code = '{safe_unit}' ORDER BY b.period_date ASC"
        )
    else:
        sql = (
            "SELECT r.period_date, p.period_year, p.period_month, "
            "r.operational_total, r.fondos_total, r.calculated_grand_total "
            "FROM expense_reports r "
            "JOIN billing_periods p ON r.period_date = p.period_date "
            "ORDER BY r.period_date ASC"
        )

    rows, error_msg = query_data_gateway(sql=sql)
    if error_msg is not None:
        if is_gateway_unavailable_error(error_msg):
            err_record = ExecutionError(
                node="trajectory_inspector_node",
                error_message=error_msg,
                attempt=1,
            )
            logger.error("Data Gateway unreachable during trajectory inspection: %s", error_msg)
            return {
                "macro_trajectory": None,
                "gateway_unavailable": True,
                "error_history": [err_record],
            }
        logger.warning("Trajectory inspection query failed: %s", error_msg)
        return {"macro_trajectory": []}

    logger.info("Trajectory inspector fetched %d monthly total records", len(rows or []))
    return {
        "macro_trajectory": rows or [],
        "gateway_unavailable": False,
    }


# ============================================================================
# 1c. Trajectory Evaluator Node (Premise Fact-Check)
# ============================================================================


def trajectory_evaluator_node(
    state: DataAnalystState, client: genai.Client | None = None
) -> dict[str, Any]:
    """Evaluate macro trajectory against user's premise, isolate inflection points, or trigger clarification."""
    gemini_client = client or get_gemini_client()
    system_instruction = load_prompt_template("evaluator_system.md")

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=TrajectoryEvaluation,
        system_instruction=system_instruction,
        temperature=0.0,
    )

    plan = state.get("intent_plan")
    trajectory = state.get("macro_trajectory") or []
    user_choice = state.get("user_clarification_choice")

    contents = [
        f"Original User Question: {state['user_query']}\n"
        f"Planner Analysis Goal: {plan.analysis_goal if plan else ''}\n"
        f"Sub-Analyses Requested: {plan.sub_analyses if plan and plan.sub_analyses else 'Single analysis'}\n"
        f"Time Scope: {plan.time_scope if plan else ''}\n"
        f"User Clarification Choice: {user_choice or 'None'}\n"
        f"Verified Macro Monthly Totals:\n{json.dumps(trajectory, ensure_ascii=False, indent=2, default=str)}"
    ]

    response = generate_content_with_fallback(
        client=gemini_client,
        contents=contents,
        config=config,
    )

    evaluation = TrajectoryEvaluation.model_validate_json(response.text)

    if user_choice:
        evaluation.needs_clarification = False

    logger.info(
        "Trajectory evaluated: status=%s, baseline=%s, peak=%s, clarify=%s",
        evaluation.premise_status,
        evaluation.baseline_period,
        evaluation.peak_period,
        evaluation.needs_clarification,
    )

    return {
        "trajectory_evaluation": evaluation,
    }


# ============================================================================
# 1d. Interactive Clarification Node (Human-in-the-Loop Interrupt)
# ============================================================================


def clarification_node(state: DataAnalystState) -> dict[str, Any]:
    """Pause graph execution via LangGraph interrupt() and resume with user's selection."""
    eval_res = state.get("trajectory_evaluation")
    plan = state.get("intent_plan")

    if eval_res and eval_res.needs_clarification:
        prompt = eval_res.clarification_prompt or "Se requiere aclaración sobre el período a analizar."
        options = eval_res.candidate_options
    elif plan and plan.needs_clarification:
        prompt = plan.clarification_prompt or "Por favor aclara el período o enfoque de tu consulta."
        options = plan.candidate_options
    else:
        return {}

    user_response = interrupt(
        {
            "clarification_prompt": prompt,
            "candidate_options": options,
        }
    )

    choice_str = str(user_response).strip()
    if choice_str.isdigit() and options:
        idx = int(choice_str) - 1
        if 0 <= idx < len(options):
            choice_str = options[idx]

    logger.info("Resumed from clarification interrupt with user choice: %s", choice_str)

    updated_plan = plan.model_copy(update={"needs_clarification": False}) if plan else None
    updated_eval = (
        eval_res.model_copy(update={"needs_clarification": False, "premise_status": "validated"})
        if eval_res
        else None
    )

    return {
        "user_clarification_choice": choice_str,
        "intent_plan": updated_plan,
        "trajectory_evaluation": updated_eval,
    }


# ============================================================================
# 2. SQL Coder Node (with Self-Healing, Multi-Query & Delta CTEs)
# ============================================================================


def sql_coder_node(state: DataAnalystState, client: genai.Client | None = None) -> dict[str, Any]:
    """Generate or correct 1 to 3 read-only SELECT queries using IntentPlan, TrajectoryEvaluation, and 3NF Data Dictionary."""
    gemini_client = client or get_gemini_client()
    system_instruction = load_prompt_template("sql_coder_system.md")

    plan = state.get("intent_plan")
    domain = plan.knowledge_domain if plan and plan.knowledge_domain else "building"
    data_dict_yaml = load_modular_data_dictionary(domain=domain)

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=SQLCoderOutput,
        system_instruction=system_instruction,
        temperature=0.0,
    )
    eval_res = state.get("trajectory_evaluation")
    user_choice = state.get("user_clarification_choice")

    plan_summary = (
        f"Goal: {plan.analysis_goal}\n"
        f"Analysis Type: {plan.analysis_type}\n"
        f"Sub-Analyses: {plan.sub_analyses}\n"
        f"Temporal Resolution: {plan.temporal_resolution}\n"
        f"Tables: {plan.relevant_tables}\n"
        f"Time Scope: {plan.time_scope}"
    ) if plan else state["user_query"]

    contents = [
        f"=== DATABASE SCHEMA & CONVENTIONS ===\n{data_dict_yaml}\n",
        f"=== INTENT PLAN ===\n{plan_summary}\nOriginal User Prompt: {state['user_query']}",
    ]

    if user_choice:
        contents.append(f"=== USER CLARIFICATION / SELECTED FOCUS ===\n{user_choice}\n")

    if eval_res:
        contents.append(
            f"=== VERIFIED TRAJECTORY EVALUATION (HOP 1) ===\n"
            f"Premise Status: {eval_res.premise_status}\n"
            f"Trajectory Summary: {eval_res.evaluation_summary}\n"
            f"Baseline Period: {eval_res.baseline_period}\n"
            f"Peak/Target Period: {eval_res.peak_period}\n"
            "IMPORTANT: Ensure every comparison query explicitly selects period provenance columns "
            "(e.g., baseline_period, peak_period, period_date, or start_period/end_period/months_count). "
            "If multiple sub-analyses are requested (e.g. MoM + YoY), populate `queries` with a separate "
            "named SQLSubQuery for each analysis!"
        )

    errors = state.get("error_history", [])
    if errors:
        last_error = errors[-1]["error_message"]
        contents.append(
            f"ATTENTION - PREVIOUS SQL ERROR DETECTED:\n{last_error}\n"
            f"Previous Failing SQL:\n{state.get('current_sql', '')}\n"
            "Please fix the SQL syntax or column name (remember: `tower`, `unit_number`, and `alicuota` are in `units` table; `period_year` and `period_month` are in `billing_periods` table)."
        )

    response = generate_content_with_fallback(
        client=gemini_client,
        contents=contents,
        config=config,
    )

    output = SQLCoderOutput.model_validate_json(response.text)

    if output.queries:
        capped_queries = output.queries[:3]
        sql_queries = [q.model_dump() for q in capped_queries]
        if len(sql_queries) == 1:
            current_sql = sql_queries[0]["sql_query"]
        else:
            current_sql = "\n\n".join(
                f"-- [{q['query_id']}] {q['title']}\n{q['sql_query']}"
                for q in sql_queries
            )
    else:
        default_title = (
            plan.analysis_goal
            if plan and plan.analysis_goal
            else "Análisis Principal"
        )
        sql_queries = [
            {
                "query_id": "primary",
                "title": default_title,
                "sql_query": output.sql_query,
            }
        ]
        current_sql = output.sql_query

    logger.info(
        "SQL Coder generated %d query(ies): %s",
        len(sql_queries),
        [q["query_id"] for q in sql_queries],
    )

    return {
        "current_sql": current_sql,
        "sql_queries": sql_queries,
    }


# ============================================================================
# 3. SQL Gateway Execution Node (Single & Multi-Query Execution)
# ============================================================================


def sql_gateway_node(state: DataAnalystState) -> dict[str, Any]:
    """Dispatch 1 to 3 SQL queries to Data Gateway and evaluate execution results."""
    sql_queries = state.get("sql_queries")
    if not sql_queries:
        fallback_sql = state.get("current_sql") or ""
        sql_queries = [
            {
                "query_id": "primary",
                "title": "Análisis Principal",
                "sql_query": fallback_sql,
            }
        ]

    multi_results: dict[str, list[dict[str, Any]]] = {}
    tagged_results: list[dict[str, Any]] = []

    for q in sql_queries:
        q_id = q.get("query_id", "primary")
        q_title = q.get("title", q_id)
        q_sql = q.get("sql_query", "")

        rows, error_msg = query_data_gateway(sql=q_sql)

        if error_msg is not None:
            if is_gateway_unavailable_error(error_msg):
                err_record = ExecutionError(
                    node="sql_gateway_node",
                    error_message=error_msg,
                    attempt=1,
                )
                logger.error("Data Gateway unreachable during SQL execution: %s", error_msg)
                return {
                    "sql_results": None,
                    "multi_sql_results": None,
                    "gateway_unavailable": True,
                    "error_history": [err_record],
                }

            attempt = state.get("retry_count", 0) + 1
            if len(sql_queries) > 1 or q_id != "primary":
                formatted_err = f"[Query '{q_id}' ({q_title})] {error_msg} | Failing SQL: {q_sql}"
            else:
                formatted_err = error_msg

            err_record = ExecutionError(
                node="sql_gateway_node",
                error_message=formatted_err,
                attempt=attempt,
            )
            logger.warning("Gateway query '%s' attempt %d failed: %s", q_id, attempt, error_msg)
            return {
                "sql_results": None,
                "multi_sql_results": None,
                "gateway_unavailable": False,
                "error_history": [err_record],
                "retry_count": attempt,
            }

        clean_rows = rows or []
        multi_results[q_id] = clean_rows
        tagged_results.append(
            {
                "query_id": q_id,
                "title": q_title,
                "rows": clean_rows,
            }
        )

    if len(sql_queries) == 1:
        first_id = sql_queries[0].get("query_id", "primary")
        final_sql_results = multi_results.get(first_id, [])
    else:
        final_sql_results = tagged_results

    logger.info(
        "Gateway executed %d query(ies) successfully",
        len(sql_queries),
    )
    return {
        "sql_results": final_sql_results,
        "multi_sql_results": multi_results,
        "gateway_unavailable": False,
        "retry_count": 0,
    }


# ============================================================================
# 4. Visualizer Node
# ============================================================================


def visualizer_node(state: DataAnalystState, client: genai.Client | None = None) -> dict[str, Any]:
    """Generate standalone Matplotlib script reading from dataset.json (single or multi-panel)."""
    gemini_client = client or get_gemini_client()
    system_instruction = load_prompt_template("visualizer_system.md")

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=VisualizerOutput,
        system_instruction=system_instruction,
        temperature=0.0,
    )

    rows = state.get("sql_results") or []
    multi_results = state.get("multi_sql_results") or {}
    plan = state.get("intent_plan")
    eval_res = state.get("trajectory_evaluation")

    eval_context = ""
    if eval_res:
        eval_context = (
            f"Analysis Archetype: {plan.analysis_type if plan else 'variance_driver'}\n"
            f"Trajectory Context: {eval_res.evaluation_summary}\n"
            f"Baseline Period: {eval_res.baseline_period} -> Peak Period: {eval_res.peak_period}\n"
        )

    if len(multi_results) > 1:
        sample_multi = {k: v[:3] for k, v in multi_results.items()}
        counts_multi = {k: len(v) for k, v in multi_results.items()}
        dataset_description = (
            f"MULTI-ANALYSIS MODE (len={len(multi_results)} datasets): `dataset.json` is a JSON DICTIONARY "
            f"keyed by query_id: {list(multi_results.keys())}.\n"
            f"Render a multi-panel figure (`fig, axes = plt.subplots({len(multi_results)}, 1, figsize=(10, {4 * len(multi_results)}))`) "
            "with one subplot per dataset and save to 'output.png'!\n"
            f"Sample Rows per Key in dataset.json: {json.dumps(sample_multi, ensure_ascii=False, default=str)}\n"
            f"Row Counts per Key in dataset.json: {json.dumps(counts_multi, ensure_ascii=False)}"
        )
    else:
        sample_data = rows[:3]
        dataset_description = (
            f"SINGLE-ANALYSIS MODE: `dataset.json` is a JSON LIST of row dictionaries.\n"
            f"Sample Row Structure from dataset.json: {json.dumps(sample_data, ensure_ascii=False, default=str)}\n"
            f"Total Data Rows in dataset.json: {len(rows)}"
        )

    contents = [
        f"Goal: {plan.visualization_goal or plan.analysis_goal if plan else state['user_query']}\n"
        f"Sub-Analyses: {plan.sub_analyses if plan and plan.sub_analyses else []}\n"
        f"{eval_context}"
        f"Suggested Chart Type: {plan.suggested_chart_type if plan else 'auto'}\n"
        f"{dataset_description}"
    ]

    first_row = (
        rows[0]
        if (rows and isinstance(rows, list) and len(rows) > 0 and isinstance(rows[0], dict))
        else {}
    )
    if "calculated_dept_subtotal" in first_row and "calculated_unit_subtotal" in first_row:
        contents.append(
            "CRITICAL APARTMENT RECONCILIATION REQUIREMENT:\n"
            "Dataset contains apartment unit billing components. You MUST plot all 4 series:\n"
            "1. 'Total Unidad' (calculated_total_month)\n"
            "2. 'Gasto Común Depto' (calculated_dept_subtotal)\n"
            "3. 'Agua Caliente' (calculated_consumo_total)\n"
            "4. 'Cargos Particulares (Multas, Seguros, otros)' (calculated_unit_subtotal)\n"
            "DO NOT omit calculated_unit_subtotal. Use the exterior anti-collision legend: "
            "ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.18), ncol=4, frameon=True, fontsize=8.5) "
            "and 20% Y-axis headroom to avoid colliding with text labels."
        )

    errors = state.get("error_history", [])
    if errors:
        last_error = errors[-1]["error_message"]
        contents.append(
            f"ATTENTION - PREVIOUS VISUALIZATION ERROR DETECTED:\n{last_error}\n"
            f"Previous Failing Code:\n{state.get('visualization_code', '')}\n"
            "Please fix the Python script so that it runs without error and saves 'output.png'."
        )

    response = generate_content_with_fallback(
        client=gemini_client,
        contents=contents,
        config=config,
    )

    output = VisualizerOutput.model_validate_json(response.text)
    logger.info("Visualizer generated script: length=%d", len(output.python_code))

    return {
        "visualization_code": output.python_code,
    }


# ============================================================================
# 5. Sandbox Runner Node
# ============================================================================


def sandbox_runner_node(state: DataAnalystState) -> dict[str, Any]:
    """Execute visualization script inside the isolated subprocess sandbox."""
    code = state.get("visualization_code") or ""
    multi_results = state.get("multi_sql_results")
    if multi_results and len(multi_results) > 1:
        dataset: list[dict[str, Any]] | dict[str, Any] = multi_results
    else:
        dataset = state.get("sql_results") or []

    result = run_plotting_sandbox(
        script_code=code,
        dataset=dataset,
    )

    if not result.success:
        attempt = state.get("retry_count", 0) + 1
        err_record = ExecutionError(
            node="sandbox_runner_node",
            error_message=result.error_message or "Unknown sandbox execution error",
            attempt=attempt,
        )
        logger.warning("Sandbox run attempt %d failed: %s", attempt, result.error_message)
        return {
            "chart_file_path": None,
            "error_history": [err_record],
            "retry_count": attempt,
        }

    logger.info("Sandbox rendered chart successfully: %s", result.chart_path)
    return {
        "chart_file_path": str(result.chart_path) if result.chart_path else None,
        "retry_count": 0,
    }


# ============================================================================
# 6. Synthesizer Node (with Zero Schema Policy, Variance Attribution & Period Citations)
# ============================================================================


def synthesizer_node(state: DataAnalystState, client: genai.Client | None = None) -> dict[str, Any]:
    """Synthesize final executive markdown answer or institutional refusal."""
    plan = state.get("intent_plan")

    if plan and not plan.is_authorized_query:
        refusal_text = (
            "### Aviso de Seguridad y Gobernanza de Datos\n\n"
            "Este sistema está configurado exclusivamente para realizar análisis financieros y reportería sobre los "
            "gastos comunes del edificio a nivel de datos y métricas agregadas.\n\n"
            "Por políticas de seguridad y protección de la infraestructura de la comunidad, **no está autorizado "
            "a divulgar información sobre la arquitectura interna, nombres de tablas ni esquemas técnicos de la base de datos**."
        )
        return {
            "final_markdown": refusal_text,
        }

    if state.get("gateway_unavailable"):
        settings = get_analytics_settings()
        offline_text = (
            "### ⚠️ Servicio de Datos No Disponible\n\n"
            f"No fue posible establecer conexión con el **Data Gateway** (`{settings.backend_base_url}`) "
            "para consultar los registros financieros de la comunidad.\n\n"
            "Por políticas de aislamiento y seguridad de grado productivo, el agente analítico opera de forma "
            "segura sobre la capa de datos autorizada. Por favor, verifica la conectividad con la base de datos "
            "e intenta nuevamente."
        )
        return {
            "final_markdown": offline_text,
        }

    gemini_client = client or get_gemini_client()
    system_instruction = load_prompt_template("synthesizer_system.md")

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.1,
    )

    rows = state.get("sql_results") or []
    chart_path = state.get("chart_file_path")
    trajectory = state.get("macro_trajectory") or []
    eval_res = state.get("trajectory_evaluation")
    user_choice = state.get("user_clarification_choice")

    trajectory_context = ""
    if plan and plan.sub_analyses:
        trajectory_context += f"Requested Sub-Analyses: {json.dumps(plan.sub_analyses, ensure_ascii=False)}\n"
    if trajectory:
        trajectory_context += f"Macro Monthly Totals: {json.dumps(trajectory, ensure_ascii=False, default=str)}\n"
    if eval_res:
        trajectory_context += (
            f"Trajectory Evaluation: {eval_res.evaluation_summary} "
            f"(Baseline: {eval_res.baseline_period}, Peak: {eval_res.peak_period})\n"
        )
    if user_choice:
        trajectory_context += f"User Selected Analysis Focus: {user_choice}\n"

    contents = [
        f"Original User Question: {state['user_query']}\n"
        f"Analysis Goal: {plan.analysis_goal if plan else ''}\n"
        f"Analysis Archetype: {plan.analysis_type if plan else 'scalar_lookup'}\n"
        f"{trajectory_context}"
        f"Verified Database Results: {json.dumps(rows, ensure_ascii=False, default=str)}\n"
        f"Chart Rendered: {'Yes (attached automatically by UI; do NOT emit ![...](...) markdown image links)' if chart_path else 'No chart generated'}"
    ]

    response = generate_content_with_fallback(
        client=gemini_client,
        contents=contents,
        config=config,
    )

    raw_markdown = response.text or "No se pudo generar la síntesis del reporte."
    markdown = re.sub(r"!\[[^\]]*\]\([^)]+\)\n?", "", raw_markdown).strip()
    logger.info("Synthesizer produced markdown summary: length=%d", len(markdown))

    return {
        "final_markdown": markdown,
    }
