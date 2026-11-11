"""API router for analytics queries and isolated Data Gateway execution."""

from __future__ import annotations

import logging
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.agent.analytics.gateway_client import (
    ALLOWED_TABLES,
    QuerySecurityError,
    execute_readonly_sql,
    extract_cte_names,
    extract_table_names,
    strip_sql_comments,
    validate_query_security,
)
from backend.app.database import get_db

logger = logging.getLogger("backend.app.routes.analytics")

router = APIRouter(prefix="/analytics", tags=["analytics"])

__all__ = [
    "ALLOWED_TABLES",
    "AnalyticsQueryRequest",
    "AnalyticsQueryResponse",
    "ExecuteQueryRequest",
    "ExecuteQueryResponse",
    "execute_query",
    "extract_cte_names",
    "extract_table_names",
    "router",
    "run_analytics_query_endpoint",
    "strip_sql_comments",
    "validate_query_security",
]


class ExecuteQueryRequest(BaseModel):
    """Payload for low-level query execution via Data Gateway Proxy."""

    model_config = ConfigDict(extra="ignore")

    sql: str = Field(..., min_length=1, description="Read-only SQL query to execute")
    timeout_seconds: float = Field(
        default=3.0, ge=0.1, le=30.0, description="Execution timeout ceiling in seconds"
    )
    max_rows: int = Field(
        default=500, ge=1, le=5000, description="Maximum number of rows to return"
    )


class ExecuteQueryResponse(BaseModel):
    """Response payload returned by Data Gateway Proxy."""

    columns: list[str] = Field(default_factory=list, description="Column names returned")
    rows: list[dict[str, Any]] = Field(
        default_factory=list, description="Array of row dictionaries"
    )
    row_count: int = Field(..., description="Number of rows returned")
    truncated: bool = Field(
        default=False, description="True if results were capped by max_rows limit"
    )
    execution_time_ms: float = Field(
        ..., description="Server execution time in milliseconds"
    )


@router.post(
    "/execute-query",
    response_model=ExecuteQueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute validated read-only SQL query",
    description="Internal Data Gateway proxy executing vetted read-only analytical queries under strict timeouts and row ceilings.",
)
def execute_query(
    payload: ExecuteQueryRequest,
    db: Session = Depends(get_db),
) -> ExecuteQueryResponse:
    """Execute a validated read-only SQL query against the active database session."""
    try:
        result = execute_readonly_sql(
            sql=payload.sql,
            timeout_seconds=payload.timeout_seconds,
            max_rows=payload.max_rows,
            db=db,
        )
        return ExecuteQueryResponse(**result)
    except QuerySecurityError as sec_err:
        raise HTTPException(status_code=sec_err.status_code, detail=sec_err.detail) from sec_err
    except TimeoutError as time_err:
        raise HTTPException(status_code=status.HTTP_408_REQUEST_TIMEOUT, detail=str(time_err)) from time_err
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# ============================================================================
# High-Level Consumer Analytics API
# ============================================================================


class AnalyticsQueryRequest(BaseModel):
    """Consumer payload requesting financial analytics and optional visualization."""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(..., min_length=1, description="Natural language question")
    include_chart: bool = Field(default=True, description="Whether to generate an analytical chart")
    max_retries: int = Field(default=2, ge=0, le=5, description="Maximum self-healing retries")
    thread_id: str | None = Field(default=None, description="Optional thread ID for resuming paused clarification")
    resume_choice: str | None = Field(default=None, description="Optional user clarification choice when resuming")


class AnalyticsQueryResponse(BaseModel):
    """Final answer returned to consumer client."""

    summary: str = Field(..., description="Executive markdown answer")
    is_authorized: bool = Field(default=True, description="False if query violated scope guard")
    needs_clarification: bool = Field(
        default=False, description="True if workflow paused awaiting user clarification"
    )
    clarification_prompt: str | None = Field(
        default=None, description="Clarification prompt when needs_clarification is True"
    )
    candidate_options: list[str] = Field(
        default_factory=list, description="Selectable options when needs_clarification is True"
    )
    thread_id: str | None = Field(
        default=None, description="Session thread ID for resuming paused execution"
    )
    sql_query: str | None = Field(default=None, description="Executed SQL query")
    data: list[dict[str, Any]] | None = Field(default=None, description="Fetched database records")
    chart_file_path: str | None = Field(default=None, description="Path to generated chart PNG")
    error_history: list[dict[str, Any]] = Field(default_factory=list, description="Telemetry errors")
    latency_seconds: float = Field(..., description="Total execution duration in seconds")


@router.post(
    "/query",
    response_model=AnalyticsQueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Run high-level natural language analytics query",
    description="Executes the full LangGraph analytics state machine and returns markdown answer, data, chart link, or clarification options.",
)
def run_analytics_query_endpoint(payload: AnalyticsQueryRequest) -> AnalyticsQueryResponse:
    """Execute end-to-end natural language analytics query."""
    from backend.app.agent.analytics.workflow import run_analysis

    try:
        if payload.thread_id is not None or payload.resume_choice is not None:
            result = run_analysis(
                query=payload.query,
                max_retries=payload.max_retries,
                thread_id=payload.thread_id,
                resume_choice=payload.resume_choice,
            )
        else:
            result = run_analysis(
                query=payload.query,
                max_retries=payload.max_retries,
            )
        return AnalyticsQueryResponse(
            summary=result.summary,
            is_authorized=result.is_authorized,
            needs_clarification=result.needs_clarification,
            clarification_prompt=result.clarification_prompt,
            candidate_options=result.candidate_options,
            thread_id=result.thread_id,
            sql_query=result.sql_query,
            data=result.data,
            chart_file_path=result.chart_file_path,
            error_history=result.error_history,
            latency_seconds=result.latency_seconds,
        )
    except Exception as exc:
        logger.error("Analytics query execution failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Analytics workflow execution failed: {exc}",
        ) from exc
