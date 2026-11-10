"""SQLAlchemy 2.0 Database configuration and session management for Cloud SQL / PostgreSQL."""

from collections.abc import Generator
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.config import CONFIG, get_database_url

DATABASE_URL = get_database_url()

# Configure pooling tuned for Cloud SQL & Cloud Run
connect_args = {}
engine_kwargs = {
    "pool_pre_ping": CONFIG.get("database", {}).get("pool_pre_ping", True),
}

if DATABASE_URL.startswith("postgresql"):
    db_cfg = CONFIG.get("database", {})
    engine_kwargs.update({
        "pool_size": int(os.getenv("DB_POOL_SIZE", db_cfg.get("pool_size", 10))),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", db_cfg.get("max_overflow", 20))),
        "pool_recycle": int(os.getenv("DB_POOL_RECYCLE", db_cfg.get("pool_recycle", 1800))),
        "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT", db_cfg.get("pool_timeout", 30))),
    })
elif DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False


engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    **engine_kwargs,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base class for all SQLAlchemy ORM models."""
    pass


def get_db() -> Generator[Session, None, None]:
    """Dependency generator that yields a database session and safely closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
