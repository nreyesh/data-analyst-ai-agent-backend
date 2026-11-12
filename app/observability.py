"""LangSmith observability, session thread telemetry, fail-open client handling, and feedback ingestion."""

from __future__ import annotations

import logging
import os
from typing import Any
import langsmith

from backend.app.config import CONFIG

logger = logging.getLogger("backend.app.observability")

_cached_client: langsmith.Client | None = None


def get_telemetry_settings() -> dict[str, Any]:
    """Retrieve telemetry settings merged from profile configuration and environment variables."""
    cfg = CONFIG.get("telemetry", {})
    env_project = os.environ.get("LANGSMITH_PROJECT") or cfg.get("project", "building-analytics-local")
    env_endpoint = os.environ.get("LANGSMITH_ENDPOINT") or cfg.get("endpoint", "https://api.smith.langchain.com")
    api_key = os.environ.get("LANGSMITH_API_KEY")

    # Tracing is enabled if LANGSMITH_TRACING="true" or if an API key is present and telemetry.enabled is not False
    tracing_env = os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2")
    if tracing_env is not None:
        tracing_enabled = tracing_env.strip().lower() in ("true", "1", "yes")
    else:
        tracing_enabled = bool(api_key and cfg.get("enabled", True))

    return {
        "enabled": cfg.get("enabled", True),
        "tracing_enabled": tracing_enabled,
        "project": env_project,
        "endpoint": env_endpoint,
        "api_key": api_key,
    }


def setup_langsmith_environment(project: str | None = None) -> None:
    """Export standard LangChain/LangSmith environment variables to ensure graph auto-tracing."""
    settings = get_telemetry_settings()
    active_project = project or settings["project"]

    if settings["tracing_enabled"]:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_PROJECT"] = active_project
        if settings.get("endpoint"):
            os.environ["LANGSMITH_ENDPOINT"] = settings["endpoint"]
        if settings.get("api_key"):
            os.environ["LANGSMITH_API_KEY"] = settings["api_key"]
        logger.info("LangSmith tracing enabled for project '%s'.", active_project)
    else:
        logger.debug("LangSmith tracing is disabled or unconfigured.")


def _get_langsmith_client() -> langsmith.Client | None:
    """Obtain or cache a langsmith.Client instance with fail-open error handling."""
    global _cached_client
    if _cached_client is not None:
        return _cached_client

    settings = get_telemetry_settings()
    api_key = settings.get("api_key")
    if not api_key:
        return None

    try:
        _cached_client = langsmith.Client(
            api_url=settings.get("endpoint"),
            api_key=api_key,
        )
        return _cached_client
    except Exception as exc:
        logger.warning("Could not initialize LangSmith client (fail-open): %s", exc)
        return None


def flush_telemetry() -> None:
    """Explicitly flush buffered LangSmith traces to guarantee delivery before container termination."""
    client = _get_langsmith_client()
    if client is not None:
        try:
            logger.info("Flushing pending LangSmith telemetry traces...")
            client.flush()
            logger.info("LangSmith telemetry flush completed.")
        except Exception as exc:
            logger.warning("LangSmith telemetry flush encountered an error (fail-open): %s", exc)


def record_feedback(
    run_id: str,
    key: str = "user_score",
    score: float | None = None,
    value: str | None = None,
    comment: str | None = None,
    source_info: dict[str, Any] | None = None,
) -> bool:
    """Record user sentiment, score, or correction feedback directly to LangSmith."""
    client = _get_langsmith_client()
    if client is None:
        logger.debug("LangSmith client unavailable. Skipping feedback creation for run_id %s.", run_id)
        return False

    try:
        client.create_feedback(
            run_id=run_id,
            key=key,
            score=score,
            value=value,
            comment=comment,
            source_info=source_info,
        )
        logger.info("Successfully recorded LangSmith feedback for run_id %s (key=%s, score=%s).", run_id, key, score)
        return True
    except Exception as exc:
        logger.warning("Failed to record LangSmith feedback for run %s (fail-open): %s", run_id, exc)
        return False
