"""Backend configuration loader with environment profile support (dev, prod)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
import urllib.parse
from dotenv import load_dotenv
import yaml

logger = logging.getLogger("backend.config")

# Automatically load environment variables from .env
load_dotenv()
_backend_env = Path(__file__).resolve().parent.parent / ".env"
if _backend_env.is_file():
    load_dotenv(_backend_env)

_PROFILE_DIR = Path(__file__).resolve().parent.parent / "profile"



def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override dictionary into base dictionary."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_profile_config() -> dict[str, Any]:
    """Load base application.yaml and merge active environment profile (dev, prod)."""
    env = os.environ.get("ENVIRONMENT", "dev").strip().lower()

    # 1. Load base configuration
    base_file = _PROFILE_DIR / "application.yaml"
    config: dict[str, Any] = {}
    if base_file.is_file():
        with open(base_file, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

    # 2. Merge environment-specific profile
    env_file = _PROFILE_DIR / f"application-{env}.yaml"
    if env_file.is_file():
        with open(env_file, "r", encoding="utf-8") as f:
            env_config = yaml.safe_load(f) or {}
            config = _deep_merge(config, env_config)
            logger.info("Loaded profile: application-%s.yaml", env)
    else:
        logger.warning("No profile found for environment '%s'. Using defaults.", env)

    # 3. Environment Variable Overrides (Highest Precedence)
    db_config = config.setdefault("database", {})
    if os.environ.get("DB_HOST"):
        db_config["host"] = os.environ["DB_HOST"]
    if os.environ.get("DB_PORT"):
        db_config["port"] = int(os.environ["DB_PORT"])
    if os.environ.get("DB_NAME"):
        db_config["name"] = os.environ["DB_NAME"]
    if os.environ.get("DB_USER"):
        db_config["user"] = os.environ["DB_USER"]
    if os.environ.get("DB_PASS"):
        db_config["password"] = os.environ["DB_PASS"]
    elif os.environ.get("DB_PASSWORD"):
        db_config["password"] = os.environ["DB_PASSWORD"]

    return config


# Global active configuration singleton
CONFIG = load_profile_config()


def get_database_url() -> str:
    """Construct full SQLAlchemy connection URL from config and environment variables.

    Precedence:
    1. Direct DATABASE_URL environment variable (if explicitly set).
    2. Dynamic URL constructed from profile config + Secret Manager injected DB_PASS.
    """
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]

    db = CONFIG.get("database", {})
    driver = db.get("driver", "postgresql+psycopg")
    user = db.get("user", "postgres")
    host = db.get("host", "localhost")
    port = db.get("port", 5432)
    name = db.get("name", "condominium_db")

    # DB_PASS or DB_PASSWORD from Secret Manager
    password = os.environ.get("DB_PASS") or os.environ.get("DB_PASSWORD") or db.get("password")

    if password:
        escaped_password = urllib.parse.quote_plus(str(password))
        escaped_user = urllib.parse.quote_plus(str(user))
        return f"{driver}://{escaped_user}:{escaped_password}@{host}:{port}/{name}"
    else:
        escaped_user = urllib.parse.quote_plus(str(user))
        return f"{driver}://{escaped_user}@{host}:{port}/{name}"
