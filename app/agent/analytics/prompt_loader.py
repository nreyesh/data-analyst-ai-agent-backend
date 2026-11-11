"""Prompt templates and Data Dictionary loader for analytics graph."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import yaml

from backend.app.agent.analytics.config import get_analytics_settings

logger = logging.getLogger("backend.agent.analytics.prompt_loader")


def get_prompts_dir() -> Path:
    """Retrieve prompts asset directory."""
    return get_analytics_settings().prompts_dir


def load_data_dictionary() -> dict[str, Any]:
    """Parse and return the YAML Data Dictionary describing database schema and domain rules."""
    settings = get_analytics_settings()
    dict_path = settings.data_dictionary_path
    if not dict_path.exists():
        raise FileNotFoundError(
            f"YAML Data Dictionary not found at {dict_path}. Please initialize the prompts asset."
        )

    with open(dict_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Data dictionary at {dict_path} must be a YAML mapping.")

    return data


def load_data_dictionary_str() -> str:
    """Return raw content of Data Dictionary for prompt injection."""
    settings = get_analytics_settings()
    dict_path = settings.data_dictionary_path
    if not dict_path.exists():
        raise FileNotFoundError(f"Data Dictionary not found at {dict_path}.")
    return dict_path.read_text(encoding="utf-8")


def load_modular_data_dictionary(domain: str = "building") -> str:
    """Assemble modular Data Dictionary slices based on domain.

    Args:
        domain: One of 'building', 'unit', or 'cross_domain'.

    Returns:
        Assembled markdown string of the relevant domain slices.
    """
    settings = get_analytics_settings()
    dd_dir = settings.data_dictionary_dir

    if not dd_dir.exists():
        return load_data_dictionary_str()

    core_file = dd_dir / "00_core_conventions.md"
    building_file = dd_dir / "01_building_domain.md"
    unit_file = dd_dir / "02_unit_domain.md"
    cross_file = dd_dir / "03_cross_domain_bridge.md"

    files_to_load = [core_file]

    normalized_domain = domain.lower().strip()
    if normalized_domain == "unit":
        files_to_load.append(unit_file)
    elif normalized_domain in ("cross_domain", "cross-domain", "hybrid"):
        files_to_load.extend([building_file, unit_file, cross_file])
    else:  # default 'building'
        files_to_load.append(building_file)

    parts: list[str] = []
    for f in files_to_load:
        if f.exists():
            parts.append(f.read_text(encoding="utf-8").strip())
        else:
            logger.warning("Modular data dictionary slice '%s' not found.", f)

    return "\n\n---\n\n".join(parts)


def load_prompt_template(name: str) -> str:
    """Load prompt template text from prompts directory.

    Args:
        name: Template filename (e.g. 'planner_system.md', 'sql_coder_system.md').

    Returns:
        Stripped prompt string.
    """
    prompts_dir = get_prompts_dir()
    file_path = prompts_dir / name
    if not file_path.exists():
        raise FileNotFoundError(f"Prompt template '{name}' not found at {file_path}")
    return file_path.read_text(encoding="utf-8").strip()
