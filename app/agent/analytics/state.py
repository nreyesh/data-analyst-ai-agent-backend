"""State schemas and data models for analytics workflow."""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, Optional, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


AnalysisType = Literal[
    "scalar_lookup",
    "time_series_trend",
    "variance_driver",
    "composition_share",
    "comparative_cohort",
]

TemporalResolution = Literal["monthly", "yearly", "snapshot"]


class ExecutionError(TypedDict):
    """Structured telemetry record of a node failure."""

    node: str
    error_message: str
    attempt: int


class IntentPlan(BaseModel):
    """Output contract for the Planner node with Scope Guarding and Archetype Classification."""

    is_authorized_query: bool = Field(
        description="False if the query probes database infrastructure, table names, schema metadata, or credentials."
    )
    refusal_reason: Optional[str] = Field(
        default=None,
        description="Reason for refusal if is_authorized_query is False.",
    )
    analysis_type: AnalysisType = Field(
        default="scalar_lookup",
        description="Analytical archetype: 'variance_driver' for why/increase/decrease questions, 'time_series_trend' for evolution over time, 'composition_share' for static category distribution, 'comparative_cohort' for period-to-period comparisons, 'scalar_lookup' for single metrics.",
    )
    temporal_resolution: TemporalResolution = Field(
        default="snapshot",
        description="Required temporal granularity. MUST be 'monthly' when analysis_type is 'variance_driver' or 'time_series_trend'.",
    )
    analysis_goal: str = Field(
        default="",
        description="Summary of the business question to solve (only populated if authorized).",
    )
    sub_analyses: list[str] = Field(
        default_factory=list,
        description="1 to 3 concrete analytical sub-tasks when the user requests multiple analyses or comparisons (e.g., ['Revisión mensual (MoM) 2026', 'Comparación interanual (YoY) 2025 vs 2026']).",
    )
    relevant_tables: list[str] = Field(
        default_factory=list,
        description="Tables required (e.g. ['expense_reports', 'expense_sections', 'billing_periods'])",
    )
    target_unit: Optional[str] = Field(
        default=None,
        description="Identified apartment unit code (e.g. 'B-504', 'A-102') if query targets a specific unit. None for general building queries.",
    )
    unit_tower: Optional[str] = Field(
        default=None,
        description="Identified building tower (e.g. 'B', 'A') if query targets an individual unit.",
    )
    unit_number: Optional[int] = Field(
        default=None,
        description="Identified apartment number as integer (e.g. 504, 506) if query targets an individual unit.",
    )
    knowledge_domain: Literal["building", "unit", "cross_domain"] = Field(
        default="building",
        description="Domain slice to assemble: 'building' for condominium totals/sections, 'unit' for personal apartment bills/water meters, 'cross_domain' for causal questions explaining unit cost surges.",
    )
    time_scope: str = Field(
        default="",
        description="Time horizon (e.g. 'all periods', '2026', '2025-06-01 to 2026-06-01')",
    )
    requires_visualization: bool = Field(
        default=False,
        description="True if a chart is needed or analytically beneficial.",
    )
    visualization_goal: Optional[str] = Field(
        default=None,
        description="What the chart should highlight (e.g., 'Monthly trend of maintenance costs')",
    )
    suggested_chart_type: Optional[Literal["line", "bar", "pie", "stacked_bar"]] = (
        Field(default=None)
    )
    needs_clarification: bool = Field(
        default=False,
        description="True if the query is critically underspecified (e.g. no time period or context at all for a change question) and requires user input.",
    )
    clarification_prompt: Optional[str] = Field(
        default=None,
        description="Message explaining what is ambiguous and asking the user to choose an option.",
    )
    candidate_options: list[str] = Field(
        default_factory=list,
        description="2 to 3 concrete interpretation options for the user to select from.",
    )


class SQLSubQuery(BaseModel):
    """Named read-only SQL query corresponding to an analytical sub-task."""

    query_id: str = Field(
        description="Short snake_case identifier for this dataset (e.g. 'mom_2026', 'yoy_2025_vs_2026', 'variance_may_jul')."
    )
    title: str = Field(
        description="Human-readable Spanish title for this analysis/comparison (e.g. 'Evolución Mensual (MoM) 2026')."
    )
    sql_query: str = Field(
        description="Valid read-only SELECT or WITH...SELECT query including explicit period_date provenance columns."
    )


class TrajectoryEvaluation(BaseModel):
    """Output contract for the Trajectory Evaluator node (Hop 1 evaluation)."""

    premise_status: Literal["validated", "contradicted", "nuanced_inflection"] = Field(
        description="Whether the user's premise (e.g. 'increase') is directly validated, contradicted by the data, or nuanced with multiple phases (e.g. drop then surge)."
    )
    evaluation_summary: str = Field(
        description="Concise factual summary of the observed monthly trajectory in Spanish."
    )
    baseline_period: Optional[str] = Field(
        default=None,
        description="Baseline/trough period_date ('YYYY-MM-01') to compare from.",
    )
    peak_period: Optional[str] = Field(
        default=None,
        description="Target/peak period_date ('YYYY-MM-01') to compare against.",
    )
    needs_clarification: bool = Field(
        default=False,
        description="True if the trajectory contradicts the user's premise or has multiple distinct phases requiring user selection.",
    )
    clarification_prompt: Optional[str] = Field(
        default=None,
        description="Explanation in Spanish of the real trajectory observed in data and question asking which phase/comparison to investigate.",
    )
    candidate_options: list[str] = Field(
        default_factory=list,
        description="List of 2-3 clear analytical focus options in Spanish.",
    )


class DataAnalystState(TypedDict, total=False):
    """Full LangGraph state machine state definition."""

    # 1. CONVERSATION TIER (Append-only reducer; enables multi-turn integration)
    messages: Annotated[list[BaseMessage], add_messages]

    # 2. OPERATIONAL TIER (Overwritten per pass)
    user_query: str
    target_unit: Optional[str]
    knowledge_domain: Optional[Literal["building", "unit", "cross_domain"]]
    unit_tower: Optional[str]
    unit_number: Optional[int]
    intent_plan: Optional[IntentPlan]
    macro_trajectory: Optional[list[dict[str, Any]]]
    trajectory_evaluation: Optional[TrajectoryEvaluation]
    user_clarification_choice: Optional[str]
    current_sql: Optional[str]
    sql_queries: Optional[list[dict[str, str]]]
    sql_results: Optional[list[dict[str, Any]]]
    multi_sql_results: Optional[dict[str, list[dict[str, Any]]]]
    visualization_code: Optional[str]
    chart_file_path: Optional[str]
    final_markdown: Optional[str]

    # 3. CONTROL & SAFETY TIER (Circuit breakers & retry routing)
    gateway_unavailable: bool
    retry_count: int
    max_retries: int
    error_history: Annotated[list[ExecutionError], operator.add]


class AnalyticsResult(BaseModel):
    """Final user-facing result of an analytical query."""

    summary: str = Field(description="Final executive summary in markdown")
    is_authorized: bool = Field(
        default=True, description="False if query was refused by scope guard"
    )
    needs_clarification: bool = Field(
        default=False,
        description="True if execution paused awaiting user clarification",
    )
    clarification_prompt: Optional[str] = Field(
        default=None,
        description="Clarification question presented to the user when paused",
    )
    candidate_options: list[str] = Field(
        default_factory=list,
        description="Selectable candidate options when needs_clarification is True",
    )
    thread_id: Optional[str] = Field(
        default=None,
        description="Session thread ID for resuming paused execution",
    )
    sql_query: Optional[str] = Field(
        default=None, description="Executed SQL query (internal telemetry)"
    )
    data: Optional[list[dict[str, Any]]] = Field(
        default=None, description="Fetched database records"
    )
    chart_file_path: Optional[str] = Field(
        default=None, description="Path to rendered chart PNG if generated"
    )
    error_history: list[dict[str, Any]] = Field(
        default_factory=list, description="List of retry errors if any occurred"
    )
    latency_seconds: float = Field(
        default=0.0, description="Total execution duration in seconds"
    )
