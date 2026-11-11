"""In-process SQLAlchemy & HTTP Data Gateway adapter with AST/regex read-only security validation."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import logging
import re
import time
from typing import Any
import uuid
import requests
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.agent.analytics.config import get_analytics_settings
from backend.app.database import engine as default_engine

logger = logging.getLogger("backend.agent.analytics.gateway_client")

GATEWAY_UNAVAILABLE_PREFIX = "DataGatewayUnavailableError:"

ALLOWED_TABLES = {
    "units",
    "billing_periods",
    "expense_reports",
    "expense_sections",
    "expense_items",
    "unit_bills",
    "unit_bill_concepts",
    "unit_bill_consumptions",
    "unit_bill_charges",
}

FORBIDDEN_CATALOGS_PATTERN = re.compile(
    r"\b(sqlite_master|sqlite_schema|sqlite_temp_master|pg_catalog|information_schema|pg_shadow|pg_authid|pragma[a-zA-Z0-9_]*)\b",
    re.IGNORECASE,
)

FORBIDDEN_MUTATIONS_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|REPLACE|TRUNCATE|GRANT|REVOKE|COPY)\b",
    re.IGNORECASE,
)


class QuerySecurityError(Exception):
    """Raised when a SQL query violates read-only or table whitelist security rules."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def is_gateway_unavailable_error(error_msg: str | None) -> bool:
    """Return True if the error message indicates the Data Gateway service is unreachable/offline."""
    return bool(error_msg and error_msg.startswith(GATEWAY_UNAVAILABLE_PREFIX))


def strip_sql_comments(sql: str) -> str:
    """Remove single-line and multi-line SQL comments."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def extract_cte_names(sql: str) -> set[str]:
    """Extract Common Table Expression (CTE) names declared in WITH clauses."""
    ctes: set[str] = set()
    matches = re.findall(
        r"(?:WITH\s+(?:RECURSIVE\s+)?|,)\s*([a-zA-Z_0-9]+)(?:\s*\([^)]*\))?\s+AS\s*\(",
        sql,
        re.IGNORECASE,
    )
    for m in matches:
        ctes.add(m.lower())
    return ctes


def extract_table_names(sql: str) -> set[str]:
    """Extract table identifiers referenced after FROM or JOIN clauses."""
    tables: set[str] = set()
    matches = re.findall(r"(?:FROM|JOIN)\s+([a-zA-Z_0-9]+)", sql, re.IGNORECASE)
    for m in matches:
        tables.add(m.lower())
    return tables


def validate_query_security(sql: str) -> str:
    """Perform AST/regex security validation against the query.

    Returns:
        Cleaned single SELECT/WITH SQL statement.

    Raises:
        QuerySecurityError: With status_code 400 (syntax/mutation) or 403 (forbidden catalog/table).
    """
    cleaned_sql = strip_sql_comments(sql)
    if not cleaned_sql:
        raise QuerySecurityError(400, "SQL query cannot be empty.")

    if FORBIDDEN_CATALOGS_PATTERN.search(cleaned_sql):
        raise QuerySecurityError(
            403,
            "Security Exception: Access to system catalogs, metadata schemas, and pragmas is forbidden.",
        )

    statements = [s.strip() for s in cleaned_sql.split(";") if s.strip()]
    if len(statements) > 1:
        raise QuerySecurityError(
            400,
            "Security Exception: Multiple semicolon-separated statements are not permitted.",
        )

    single_stmt = statements[0] if statements else cleaned_sql

    if not re.match(r"^(SELECT|WITH)\b", single_stmt, re.IGNORECASE):
        raise QuerySecurityError(
            400,
            "Security Exception: Only read-only queries beginning with SELECT or WITH are permitted.",
        )

    if FORBIDDEN_MUTATIONS_PATTERN.search(single_stmt):
        raise QuerySecurityError(
            400,
            "Security Exception: Data mutation statements (INSERT/UPDATE/DELETE/DROP/ALTER) are forbidden.",
        )

    cte_names = extract_cte_names(single_stmt)
    referenced_tables = extract_table_names(single_stmt)

    for table in referenced_tables:
        if table not in ALLOWED_TABLES and table not in cte_names:
            raise QuerySecurityError(
                403,
                f"Security Exception: Table '{table}' is not in the authorized table whitelist.",
            )

    return single_stmt


def _serialize_cell(val: Any) -> Any:
    """Convert Decimal, UUID, date, and datetime objects into JSON-serializable primitives."""
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, (date, datetime)):
        return val.isoformat()
    return val


def execute_readonly_sql(
    sql: str,
    timeout_seconds: float = 5.0,
    max_rows: int = 500,
    db: Session | Engine | None = None,
) -> dict[str, Any]:
    """Validate and execute a read-only SQL query against SQLAlchemy Engine or Session.

    Works seamlessly across PostgreSQL (Cloud SQL) and SQLite (local dev / pytest).
    """
    single_stmt = validate_query_security(sql)
    start_time = time.perf_counter()

    target = db if db is not None else default_engine

    def _run_on_connection(conn: Any) -> dict[str, Any]:
        dialect_name = conn.dialect.name
        interrupted_by_timeout = False

        if dialect_name == "sqlite":
            raw_dbapi = getattr(conn.connection, "dbapi_connection", None) or getattr(
                conn.connection, "connection", None
            )
            if raw_dbapi is not None and hasattr(raw_dbapi, "set_progress_handler"):
                def _progress_cb() -> int:
                    nonlocal interrupted_by_timeout
                    if time.perf_counter() - start_time > timeout_seconds:
                        interrupted_by_timeout = True
                        return 1
                    return 0
                raw_dbapi.set_progress_handler(_progress_cb, 1000)
        elif dialect_name == "postgresql":
            ms = max(100, int(timeout_seconds * 1000))
            conn.execute(text(f"SET LOCAL statement_timeout = {ms}"))

        try:
            result = conn.execute(text(single_stmt))
            columns = list(result.keys())
            fetched_rows = result.fetchmany(max_rows + 1)
            truncated = len(fetched_rows) > max_rows
            rows_to_return = fetched_rows[:max_rows]

            dict_rows = [
                {col: _serialize_cell(row[idx]) for idx, col in enumerate(columns)}
                for row in rows_to_return
            ]
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return {
                "columns": columns,
                "rows": dict_rows,
                "row_count": len(dict_rows),
                "truncated": truncated,
                "execution_time_ms": elapsed_ms,
            }
        except Exception as exc:
            if interrupted_by_timeout or "interrupted" in str(exc).lower() or "statement timeout" in str(exc).lower():
                raise TimeoutError(
                    f"Query execution exceeded {timeout_seconds:.1f}s timeout ceiling."
                ) from exc
            raise
        finally:
            if dialect_name == "sqlite":
                raw_dbapi = getattr(conn.connection, "dbapi_connection", None) or getattr(
                    conn.connection, "connection", None
                )
                if raw_dbapi is not None and hasattr(raw_dbapi, "set_progress_handler"):
                    raw_dbapi.set_progress_handler(None, 0)

    if isinstance(target, Session):
        conn = target.connection()
        return _run_on_connection(conn)
    else:
        with target.connect() as conn:
            with conn.begin():
                return _run_on_connection(conn)


def query_data_gateway(
    sql: str,
    timeout_seconds: float | None = None,
    max_rows: int | None = None,
    gateway_url: str | None = None,
    client: Any | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Execute a read-only query in-process via SQLAlchemy (or via HTTP TestClient if provided).

    Args:
        sql: Read-only SELECT/WITH SQL query string to execute.
        timeout_seconds: Optional timeout ceiling (defaults to settings).
        max_rows: Optional max rows limit (defaults to settings).
        gateway_url: Optional URL override when an HTTP client/URL is explicitly tested.
        client: Optional SQLAlchemy Session/Engine OR HTTP client (FastAPI TestClient / requests.Session).

    Returns:
        tuple[list[dict] | None, error_message | None]:
            On success: (rows, None)
            On query error (HTTP 4xx / SQL error): (None, "DataGatewayError (<status>): <detail>")
            On gateway outage: (None, "DataGatewayUnavailableError: <detail>")
    """
    settings = get_analytics_settings()
    timeout = timeout_seconds if timeout_seconds is not None else settings.query_timeout_seconds
    rows_limit = max_rows if max_rows is not None else settings.max_query_rows

    # 1. If an HTTP client (e.g. FastAPI TestClient) or explicit gateway_url is passed, support HTTP execution
    if (client is not None and hasattr(client, "post") and not isinstance(client, (Session, Engine))) or gateway_url is not None:
        target_url = gateway_url or settings.gateway_execute_url
        post_fn = client.post if (client is not None and hasattr(client, "post")) else requests.post
        payload = {
            "sql": sql,
            "timeout_seconds": timeout,
            "max_rows": rows_limit,
        }
        try:
            try:
                response = post_fn(target_url, json=payload, timeout=timeout + 2.0)
            except TypeError:
                response = post_fn(target_url, json=payload)

            if response.status_code == 200:
                data = response.json()
                return data.get("rows", []), None

            content_type = response.headers.get("content-type", "")
            error_detail = (
                response.json().get("detail", response.text)
                if "application/json" in content_type
                else response.text
            )
            if response.status_code in (502, 503, 504):
                return (
                    None,
                    f"{GATEWAY_UNAVAILABLE_PREFIX} Service unavailable (HTTP {response.status_code}) at {target_url}: {error_detail}",
                )
            return None, f"DataGatewayError ({response.status_code}): {error_detail}"
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            return (
                None,
                f"{GATEWAY_UNAVAILABLE_PREFIX} Cannot connect to Data Gateway at {target_url} ({exc})",
            )
        except Exception as exc:
            return (
                None,
                f"{GATEWAY_UNAVAILABLE_PREFIX} Unexpected error communicating with Data Gateway at {target_url} ({exc})",
            )

    # 2. Default: Direct In-Process SQLAlchemy Execution
    db_target = client if isinstance(client, (Session, Engine)) else default_engine
    try:
        res = execute_readonly_sql(
            sql=sql,
            timeout_seconds=timeout,
            max_rows=rows_limit,
            db=db_target,
        )
        return res["rows"], None
    except QuerySecurityError as sec_err:
        logger.warning("Data Gateway security validation rejected query (%d): %s", sec_err.status_code, sec_err.detail)
        return None, f"DataGatewayError ({sec_err.status_code}): {sec_err.detail}"
    except TimeoutError as time_err:
        logger.warning("Data Gateway query timeout: %s", time_err)
        return None, f"DataGatewayError (408): {time_err}"
    except Exception as exc:
        err_str = str(exc)
        if "could not connect" in err_str.lower() or "connection refused" in err_str.lower():
            logger.error("Database connection unavailable in Data Gateway: %s", exc)
            return None, f"{GATEWAY_UNAVAILABLE_PREFIX} Database connection error ({exc})"
        logger.warning("Data Gateway SQL operational error: %s", exc)
        return None, f"DataGatewayError (400): SQL Operational Error: {exc}"
