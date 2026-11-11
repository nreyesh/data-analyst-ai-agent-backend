"""Idempotent seeder for condominium units mapping."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.database import SessionLocal
from backend.app.models import UnitModel

logger = logging.getLogger("backend.extractor.seed_units")

_DEFAULT_CSV = (
    Path(__file__).resolve().parent.parent.parent
    / "docs"
    / "Mapeo Deptos Sierra Plaza.csv"
)


def seed_units_from_csv(
    csv_path: Path | str | None = None,
    db: Session | None = None,
) -> int:
    """Reads mapping CSV and bulk upserts all apartments into the units table."""
    path = Path(csv_path) if csv_path else _DEFAULT_CSV
    if not path.is_file():
        path = Path("/app/docs/Mapeo Deptos Sierra Plaza.csv")
    if not path.is_file():
        logger.error("Mapping CSV not found at %s", path)
        return 0

    session = db or SessionLocal()
    try:
        # Preload all existing units in 1 query to avoid 400+ sequential roundtrips
        existing = {u.unit_code: u for u in session.scalars(select(UnitModel))}
        count = 0

        with open(path, mode="r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                unit_code = f"{row['torre']}-{row['depto']}"
                kastor_id = int(row["id"])
                unit = existing.get(unit_code)

                if unit:
                    unit.kastor_id = kastor_id
                else:
                    session.add(
                        UnitModel(
                            unit_code=unit_code,
                            tower=row["torre"],
                            unit_number=int(row["depto"]),
                            kastor_id=kastor_id,
                            alicuota=None,
                        )
                    )
                count += 1

        session.commit()
        logger.info("Successfully seeded %d units.", count)
        return count
    except Exception as e:
        session.rollback()
        logger.error("Seeding failed: %s", e)
        raise
    finally:
        if db is None:
            session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_units_from_csv()
