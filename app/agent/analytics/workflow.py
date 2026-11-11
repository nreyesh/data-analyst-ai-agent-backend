"""LangGraph state machine assembling Planner, Trajectory Inspector/Evaluator, SQL Coder, Gateway, Visualizer, and Synthesizer."""

from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any, Callable, Literal
import uuid
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from langsmith import traceable

from backend.app.agent.analytics.config import get_analytics_settings
from backend.app.agent.analytics.nodes import (
    clarification_node,
    planner_node,
    sandbox_runner_node,
    sql_coder_node,
    sql_gateway_node,
    synthesizer_node,
    trajectory_evaluator_node,
    trajectory_inspector_node,
    visualizer_node,
)
from backend.app.agent.analytics.state import AnalyticsResult, DataAnalystState

logger = logging.getLogger("backend.agent.analytics.workflow")

# Shared in-memory checkpointer for stateful pause/resume across turns
_DEFAULT_CHECKPOINTER = MemorySaver(
    serde=JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("backend.app.agent.analytics.state", "IntentPlan"),
            ("backend.app.agent.analytics.state", "TrajectoryEvaluation"),
            ("backend.app.agent.analytics.state", "SQLSubQuery"),
            ("analytics.state", "IntentPlan"),
            ("analytics.state", "TrajectoryEvaluation"),
            ("analytics.state", "SQLSubQuery"),
        ]
    )
)


# ============================================================================
# Conditional Edge Routers
# ============================================================================


def route_after_planner(
    state: DataAnalystState,
) -> Literal["synthesizer", "clarification", "trajectory_inspector", "sql_coder"]:
    """Route after Planner: refusal -> synthesizer, ambiguity -> clarification, variance -> trajectory_inspector, else -> sql_coder."""
    plan = state.get("intent_plan")
    if plan and not plan.is_authorized_query:
        logger.warning("Scope Guard triggered: refusing query '%s'", state.get("user_query"))
        return "synthesizer"

    if plan and plan.needs_clarification and not state.get("user_clarification_choice"):
        logger.info("Planner requested clarification for query '%s'", state.get("user_query"))
        return "clarification"

    if plan and (
        plan.analysis_type in ("variance_driver", "comparative_cohort")
        or len(plan.sub_analyses) > 1
    ):
        logger.info(
            "Routing %s / multi-analysis query to trajectory_inspector (Hop 1)",
            plan.analysis_type,
        )
        return "trajectory_inspector"

    return "sql_coder"


def route_after_inspector(
    state: DataAnalystState,
) -> Literal["synthesizer", "trajectory_evaluator"]:
    """Route after Trajectory Inspector: short-circuit to synthesizer if Data Gateway is unreachable."""
    if state.get("gateway_unavailable"):
        logger.warning("Data Gateway offline during trajectory inspection; routing to synthesizer")
        return "synthesizer"
    return "trajectory_evaluator"


def route_after_evaluator(
    state: DataAnalystState,
) -> Literal["clarification", "sql_coder"]:
    """Route after Trajectory Evaluator: pause for clarification if needed, otherwise proceed to SQL Coder (Hop 2)."""
    eval_res = state.get("trajectory_evaluation")
    if eval_res and eval_res.needs_clarification and not state.get("user_clarification_choice"):
        logger.info("Trajectory evaluator triggered user clarification")
        return "clarification"
    return "sql_coder"


def route_after_clarification(
    state: DataAnalystState,
) -> Literal["planner", "trajectory_evaluator"]:
    """Route after user resumes from clarification interrupt."""
    if state.get("macro_trajectory") is not None:
        return "trajectory_evaluator"
    return "planner"


def route_after_sql_gateway(
    state: DataAnalystState,
) -> Literal["sql_coder", "visualizer", "synthesizer"]:
    """Evaluate gateway response: short-circuit on gateway outage, retry on SQL error, proceed to visualizer, or go to synthesizer."""
    if state.get("gateway_unavailable"):
        logger.warning("Data Gateway offline during SQL execution; routing directly to synthesizer")
        return "synthesizer"

    results = state.get("sql_results")
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 2)

    if results is None:
        if retry_count < max_retries:
            logger.info("Retrying SQL generation (attempt %d/%d)", retry_count + 1, max_retries)
            return "sql_coder"
        logger.warning("SQL retries exhausted (%d/%d); proceeding to synthesizer", retry_count, max_retries)
        return "synthesizer"

    plan = state.get("intent_plan")
    if plan and plan.requires_visualization and len(results) > 0:
        return "visualizer"

    return "synthesizer"


def route_after_sandbox(
    state: DataAnalystState,
) -> Literal["visualizer", "synthesizer"]:
    """Evaluate sandbox rendering: retry on plotting error or proceed to synthesizer."""
    chart_path = state.get("chart_file_path")
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 2)

    if not chart_path:
        if retry_count < max_retries:
            logger.info("Retrying visualization script generation (attempt %d/%d)", retry_count + 1, max_retries)
            return "visualizer"
        logger.warning("Visualization retries exhausted (%d/%d); gracefully falling back to text", retry_count, max_retries)
        return "synthesizer"

    return "synthesizer"


# ============================================================================
# Graph Assembly
# ============================================================================


def build_analytics_graph(checkpointer: MemorySaver | None = None):
    """Assemble and compile the LangGraph analytics state machine."""
    workflow = StateGraph(DataAnalystState)

    workflow.add_node("planner", planner_node)
    workflow.add_node("trajectory_inspector", trajectory_inspector_node)
    workflow.add_node("trajectory_evaluator", trajectory_evaluator_node)
    workflow.add_node("clarification", clarification_node)
    workflow.add_node("sql_coder", sql_coder_node)
    workflow.add_node("sql_gateway", sql_gateway_node)
    workflow.add_node("visualizer", visualizer_node)
    workflow.add_node("sandbox_runner", sandbox_runner_node)
    workflow.add_node("synthesizer", synthesizer_node)

    workflow.add_edge(START, "planner")

    workflow.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            "synthesizer": "synthesizer",
            "clarification": "clarification",
            "trajectory_inspector": "trajectory_inspector",
            "sql_coder": "sql_coder",
        },
    )

    workflow.add_conditional_edges(
        "trajectory_inspector",
        route_after_inspector,
        {
            "synthesizer": "synthesizer",
            "trajectory_evaluator": "trajectory_evaluator",
        },
    )

    workflow.add_conditional_edges(
        "trajectory_evaluator",
        route_after_evaluator,
        {
            "clarification": "clarification",
            "sql_coder": "sql_coder",
        },
    )

    workflow.add_conditional_edges(
        "clarification",
        route_after_clarification,
        {
            "planner": "planner",
            "trajectory_evaluator": "trajectory_evaluator",
        },
    )

    workflow.add_edge("sql_coder", "sql_gateway")

    workflow.add_conditional_edges(
        "sql_gateway",
        route_after_sql_gateway,
        {
            "sql_coder": "sql_coder",
            "visualizer": "visualizer",
            "synthesizer": "synthesizer",
        },
    )

    workflow.add_edge("visualizer", "sandbox_runner")

    workflow.add_conditional_edges(
        "sandbox_runner",
        route_after_sandbox,
        {
            "visualizer": "visualizer",
            "synthesizer": "synthesizer",
        },
    )

    workflow.add_edge("synthesizer", END)

    active_checkpointer = checkpointer if checkpointer is not None else _DEFAULT_CHECKPOINTER
    return workflow.compile(
        checkpointer=active_checkpointer,
        name="ExpenseAnalyticsGraph",
    )


# ============================================================================
# Primary Execution Interface
# ============================================================================


def _emit_analytics_node_status(
    node_name: str,
    node_output: dict[str, Any],
    accumulated_state: dict[str, Any],
    status_callback: Callable[[str], None] | None,
) -> None:
    """Emit human-friendly, non-technical Spanish progress updates after each LangGraph node."""
    if status_callback is None:
        return

    plan = node_output.get("intent_plan") or accumulated_state.get("intent_plan")
    max_retries = accumulated_state.get("max_retries", 2)

    if node_name == "planner":
        if plan and not plan.is_authorized_query:
            status_callback("Verificando políticas de acceso...")
        elif plan and plan.needs_clarification and not accumulated_state.get("user_clarification_choice"):
            status_callback("Preparando opciones de consulta...")
        elif plan and (
            plan.analysis_type in ("variance_driver", "comparative_cohort")
            or len(plan.sub_analyses) > 1
        ):
            status_callback("Revisando el historial mensual de gastos...")
        else:
            status_callback("Extrayendo información financiera de los registros...")

    elif node_name == "trajectory_inspector":
        if node_output.get("gateway_unavailable"):
            status_callback("Verificando disponibilidad del servicio de datos...")
        else:
            status_callback("Evaluando la evolución mensual del período...")

    elif node_name == "trajectory_evaluator":
        eval_res = node_output.get("trajectory_evaluation")
        if eval_res and eval_res.needs_clarification and not accumulated_state.get("user_clarification_choice"):
            status_callback("Preparando opciones de período a analizar...")
        else:
            status_callback("Extrayendo el desglose detallado de gastos...")

    elif node_name == "clarification":
        status_callback("Procesando la opción seleccionada...")

    elif node_name == "sql_coder":
        status_callback("Consultando los registros financieros de la comunidad...")

    elif node_name == "sql_gateway":
        if node_output.get("gateway_unavailable"):
            status_callback("Verificando estado de conexión...")
        elif node_output.get("sql_results") is None:
            retry_count = node_output.get("retry_count", 0)
            if retry_count < max_retries:
                status_callback("Refinando la búsqueda de información financiera...")
            else:
                status_callback("Redactando el resumen del análisis...")
        else:
            results = node_output.get("sql_results") or []
            if plan and plan.requires_visualization and len(results) > 0:
                status_callback("Preparando el gráfico comparativo...")
            else:
                status_callback("Redactando el informe y conclusiones...")

    elif node_name == "visualizer":
        status_callback("Generando la visualización del gráfico...")

    elif node_name == "sandbox_runner":
        if not node_output.get("chart_file_path"):
            retry_count = node_output.get("retry_count", 0)
            if retry_count < max_retries:
                status_callback("Ajustando el formato del gráfico...")
            else:
                status_callback("Redactando el informe y conclusiones...")
        else:
            status_callback("Redactando el informe y conclusiones...")


@traceable(name="run_analytics_query")
def run_analysis(
    query: str,
    chart_dir: Path | str | None = None,
    max_retries: int | None = None,
    thread_id: str | None = None,
    resume_choice: str | None = None,
    target_unit: str | None = None,
    knowledge_domain: Literal["building", "unit", "cross_domain"] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> AnalyticsResult:
    """Execute end-to-end analytical question answering, supporting checkpointed clarification pause/resume."""
    settings = get_analytics_settings()
    settings.ensure_directories()

    retries = max_retries if max_retries is not None else settings.max_retries
    active_thread_id = thread_id or f"thread_{uuid.uuid4().hex[:12]}"
    start_time = time.perf_counter()

    graph = build_analytics_graph()
    run_config: dict[str, Any] = {
        "configurable": {"thread_id": active_thread_id},
        "run_name": f"Analytics: {query[:40]}",
        "tags": ["analytics-graph"],
    }

    if resume_choice is not None:
        if status_callback:
            status_callback("Procesando la opción seleccionada...")
        stream_input: Any = Command(resume=resume_choice)
    else:
        if status_callback:
            status_callback("Planificando el análisis financiero...")
        initial_state: DataAnalystState = {
            "messages": [],
            "user_query": query,
            "target_unit": target_unit,
            "knowledge_domain": knowledge_domain,
            "intent_plan": None,
            "macro_trajectory": None,
            "trajectory_evaluation": None,
            "user_clarification_choice": None,
            "current_sql": None,
            "sql_queries": None,
            "sql_results": None,
            "multi_sql_results": None,
            "visualization_code": None,
            "chart_file_path": None,
            "final_markdown": None,
            "gateway_unavailable": False,
            "retry_count": 0,
            "max_retries": retries,
            "error_history": [],
        }
        stream_input = initial_state

    interrupts: Any = None
    accumulated_state: dict[str, Any] = {"max_retries": retries}

    for chunk in graph.stream(stream_input, config=run_config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            interrupts = chunk["__interrupt__"]
            continue
        for node_name, node_output in chunk.items():
            if isinstance(node_output, dict):
                accumulated_state.update(node_output)
                _emit_analytics_node_status(
                    node_name=node_name,
                    node_output=node_output,
                    accumulated_state=accumulated_state,
                    status_callback=status_callback,
                )

    snapshot = graph.get_state(run_config)
    final_state: dict[str, Any] = dict(snapshot.values) if snapshot and snapshot.values else accumulated_state

    duration = round(time.perf_counter() - start_time, 2)

    if interrupts:
        intr_payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
        prompt = intr_payload.get("clarification_prompt", "Se requiere aclaración.")
        options = intr_payload.get("candidate_options", [])
        return AnalyticsResult(
            summary=prompt,
            is_authorized=True,
            needs_clarification=True,
            clarification_prompt=prompt,
            candidate_options=options,
            thread_id=active_thread_id,
            sql_query=final_state.get("current_sql"),
            data=final_state.get("macro_trajectory"),
            chart_file_path=None,
            error_history=list(final_state.get("error_history", [])),
            latency_seconds=duration,
        )

    plan = final_state.get("intent_plan")
    is_authorized = plan.is_authorized_query if plan else True

    return AnalyticsResult(
        summary=final_state.get("final_markdown") or "No se pudo generar respuesta.",
        is_authorized=is_authorized,
        needs_clarification=False,
        clarification_prompt=None,
        candidate_options=[],
        thread_id=active_thread_id,
        sql_query=final_state.get("current_sql"),
        data=final_state.get("sql_results"),
        chart_file_path=final_state.get("chart_file_path"),
        error_history=list(final_state.get("error_history", [])),
        latency_seconds=duration,
    )


run_analytics_workflow = run_analysis
