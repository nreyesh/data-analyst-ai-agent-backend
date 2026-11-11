"""Configuration and settings management for the agent analytics package."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Export .env variables to os.environ for LangSmith / LangGraph tracing
load_dotenv()


class AnalyticsSettings(BaseSettings):
    """Runtime configuration for analytics graph and services."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 1. Credentials
    gemini_api_key: str | None = Field(
        default=None,
        validation_alias="GEMINI_API_KEY",
        description="Google Gemini API Key",
    )

    # 2. Operational App Settings
    default_model: str = Field(
        default="gemini-3.5-flash-lite",
        validation_alias="GEMINI_MODEL",
        description="Default Gemini model name",
    )
    fallback_models: list[str] = Field(
        default_factory=lambda: [
            "gemini-3.1-flash-lite",
            "gemini-3.6-flash",
            "gemini-3.7-flash",
            "gemini-3.8-flash",
        ],
        validation_alias="FALLBACK_MODELS",
        description="Ordered list of backup Gemini Flash models",
    )
    max_retries: int = Field(
        default=2,
        validation_alias="MAX_RETRIES",
        description="Maximum self-healing retry iterations",
    )

    def get_candidate_models(self, primary_override: str | None = None) -> list[str]:
        """Return deduplicated ordered list of primary and fallback models."""
        primary = primary_override or os.getenv("DEFAULT_MODEL") or self.default_model
        fallback_env = os.getenv("FALLBACK_MODEL")
        fallbacks = [fallback_env, *self.fallback_models] if fallback_env else self.fallback_models
        candidates: list[str] = []
        for model_name in [primary, *fallbacks]:
            if model_name and model_name not in candidates:
                candidates.append(model_name)
        return candidates

    # 3. Timeouts & Limits
    query_timeout_seconds: float = Field(
        default=5.0,
        validation_alias="QUERY_TIMEOUT_SECONDS",
        description="Data Gateway hard query execution timeout in seconds",
    )
    max_query_rows: int = Field(
        default=500,
        validation_alias="MAX_QUERY_ROWS",
        description="Hard maximum row limit returned by Data Gateway",
    )
    sandbox_timeout_seconds: int = Field(
        default=15,
        validation_alias="SANDBOX_TIMEOUT_SECONDS",
        description="Subprocess sandbox script timeout in seconds",
    )

    # 4. Service URLs & Ports (for diagnostic/compatibility references)
    backend_base_url: str = Field(
        default="http://localhost:8000",
        validation_alias="BACKEND_BASE_URL",
        description="Base URL for the persistence backend service",
    )

    @property
    def gateway_execute_url(self) -> str:
        """Data Gateway query execution endpoint URL."""
        return f"{self.backend_base_url.rstrip('/')}/api/v1/analytics/execute-query"

    @property
    def analytics_query_url(self) -> str:
        """High-level consumer analytics endpoint URL."""
        return f"{self.backend_base_url.rstrip('/')}/api/v1/analytics/query"

    @property
    def unit_bills_api_url(self) -> str:
        """Endpoint URL for unit bills REST resource."""
        return f"{self.backend_base_url.rstrip('/')}/api/v1/unit-bills"

    @property
    def reports_api_url(self) -> str:
        """Endpoint URL for macro building expense reports REST resource."""
        return f"{self.backend_base_url.rstrip('/')}/api/v1/reports"

    @property
    def sync_queue_status_url(self) -> str:
        """Endpoint URL for sync status REST resource."""
        return f"{self.backend_base_url.rstrip('/')}/api/v1/sync/status"

    # 5. Path Boundaries (Cloud Run ephemeral-safe defaults in /tmp)
    prompts_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent / "prompts"
    )
    output_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("ANALYTICS_OUTPUT_DIR", str(Path(tempfile.gettempdir()) / "building_analytics_output")))
    )

    @property
    def charts_dir(self) -> Path:
        """Directory storing rendered chart image files."""
        return self.output_dir / "charts"

    @property
    def scratch_dir(self) -> Path:
        """Directory in OS temp storage for ephemeral sandbox script and dataset files."""
        return Path(tempfile.gettempdir()) / "data_analyst_scratch"

    @property
    def data_dictionary_path(self) -> Path:
        """Path to YAML/MD Data Dictionary asset."""
        return self.prompts_dir / "data_dictionary.md"

    @property
    def data_dictionary_dir(self) -> Path:
        """Directory storing modular markdown data dictionary slices."""
        return self.prompts_dir / "data_dictionary"

    def ensure_directories(self) -> None:
        """Ensure standard charts, scratch, and prompts directories exist."""
        for path in (self.charts_dir, self.scratch_dir, self.prompts_dir):
            path.mkdir(parents=True, exist_ok=True)


_settings_instance: AnalyticsSettings | None = None


def get_analytics_settings() -> AnalyticsSettings:
    """Retrieve global AnalyticsSettings instance."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = AnalyticsSettings()
    return _settings_instance
