"""Dual-model token bucket rate limiter for interleaved 20 RPM Gemini Flash Lite throughput."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Sequence

logger = logging.getLogger("backend.extractor.rate_limiter")


class DualModelRateLimiter:
    """Interleaves requests between two models to maintain 20 RPM total (10 RPM per model).

    Free-tier constraints:
    - 15 RPM per model, 500 RPD per model.
    - Combined pace: 1 request every 3.0 seconds = 20 RPM.
    - If one model hits a 429 or quota pause, it backs off and temporarily shifts traffic
      to the other model at safe intervals.
    """

    def __init__(
        self,
        total_rpm: int = 20,
        models: Sequence[str] = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
    ):
        self.total_rpm = total_rpm
        self.interval = 60.0 / total_rpm  # 3.0s for 20 RPM
        self.models = list(models)
        self.turn = 0
        self.lock = asyncio.Lock()
        self.last_global_call = 0.0
        self.last_model_call: dict[str, float] = {m: 0.0 for m in self.models}
        self.backoff_until: dict[str, float] = {m: 0.0 for m in self.models}

    def report_rate_limit(self, model: str, backoff_seconds: float = 60.0) -> None:
        """Mark a model as rate-limited, pausing its assignment temporarily."""
        now = time.time()
        self.backoff_until[model] = now + backoff_seconds
        logger.warning(
            "Model '%s' rate-limited (429). Backing off for %.1f seconds.",
            model,
            backoff_seconds,
        )

    async def wait_and_get_model(self, candidate_models: Sequence[str] | None = None) -> str:
        """Acquire a slot at the 20 RPM pace and return the chosen model.

        Ensures at least `interval` seconds between consecutive calls globally,
        and rotates between available (non-backed-off) models.
        """
        async with self.lock:
            models_to_use = list(candidate_models) if candidate_models else self.models
            now = time.time()

            # Global rate-limiting wait
            elapsed_since_last = now - self.last_global_call
            if elapsed_since_last < self.interval:
                wait_time = self.interval - elapsed_since_last
                await asyncio.sleep(wait_time)
                now = time.time()

            # Filter out backed-off models if at least one model is active
            active_models = [m for m in models_to_use if self.backoff_until.get(m, 0.0) <= now]
            if not active_models:
                # All models backed off: find the one that will be available soonest
                soonest_model = min(models_to_use, key=lambda m: self.backoff_until.get(m, 0.0))
                sleep_needed = max(0.0, self.backoff_until[soonest_model] - now)
                logger.warning(
                    "All candidate models backed off. Sleeping %.1f seconds for '%s'.",
                    sleep_needed,
                    soonest_model,
                )
                await asyncio.sleep(sleep_needed)
                now = time.time()
                active_models = [soonest_model]

            # Interleaved round-robin selection among active models
            chosen_model = active_models[self.turn % len(active_models)]
            self.turn = (self.turn + 1) % len(active_models)

            # Record call timestamp
            self.last_global_call = now
            self.last_model_call[chosen_model] = now

            logger.debug("Dispatched request slot to model: %s", chosen_model)
            return chosen_model
