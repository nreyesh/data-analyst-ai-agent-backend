"""Tests for DualModelRateLimiter 20 RPM interleaving and backoff."""

import asyncio
import time
import pytest

from backend.app.extractor.rate_limiter import DualModelRateLimiter


def test_dual_model_interleaving():
    """Verify that requests alternate between primary and fallback models."""
    async def _run():
        limiter = DualModelRateLimiter(
            total_rpm=600,  # fast for test: interval = 0.1s
            models=["model-a", "model-b"],
        )

        m1 = await limiter.wait_and_get_model()
        assert m1 == "model-a"

        m2 = await limiter.wait_and_get_model()
        assert m2 == "model-b"

        m3 = await limiter.wait_and_get_model()
        assert m3 == "model-a"

        m4 = await limiter.wait_and_get_model()
        assert m4 == "model-b"

    asyncio.run(_run())


def test_rate_limiter_backoff():
    """Verify that a rate-limited model is skipped during backoff."""
    async def _run():
        limiter = DualModelRateLimiter(
            total_rpm=600,
            models=["model-a", "model-b"],
        )

        # Report model-a as rate-limited for 10 seconds
        limiter.report_rate_limit("model-a", backoff_seconds=10.0)

        # All calls should shift to model-b while model-a is backed off
        m1 = await limiter.wait_and_get_model()
        assert m1 == "model-b"

        m2 = await limiter.wait_and_get_model()
        assert m2 == "model-b"

    asyncio.run(_run())

