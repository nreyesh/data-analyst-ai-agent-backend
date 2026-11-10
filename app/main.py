"""Main FastAPI application entrypoint with healthcheck and database connectivity."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.config import CONFIG
from backend.app.database import engine, get_db

logger = logging.getLogger("backend.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager for application startup and shutdown."""
    logger.info("Starting Building Analytics Backend service...")
    yield
    logger.info("Shutting down Building Analytics Backend service.")
    engine.dispose()


def create_app() -> FastAPI:
    """FastAPI application factory."""
    app = FastAPI(
        title="Building Analytics Backend API",
        version="1.0.0",
        description="Persistent PostgreSQL storage and analytics API for Chilean condominium financial reports.",
        lifespan=lifespan,
    )

    @app.get(
        "/",
        summary="Root endpoint",
        tags=["system"],
    )
    def root() -> dict[str, str]:
        return {
            "service": "building-analytics-backend",
            "environment": CONFIG.get("environment", "dev"),
            "status": "running",
        }

    @app.get(
        "/health",
        tags=["system"],
        summary="Service health check",
        status_code=status.HTTP_200_OK,
    )
    def health_check(db: Session = Depends(get_db)) -> JSONResponse:
        try:
            db.execute(text("SELECT 1"))
            db_status = "connected"
        except Exception as e:
            logger.error("Healthcheck DB connectivity error: %s", e)
            db_status = f"unhealthy: {e}"

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "healthy",
                "database": db_status,
                "environment": CONFIG.get("environment", "dev"),
            },
        )

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=8000, reload=True)
