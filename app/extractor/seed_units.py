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
    """Reads mapping CSV and bulk upserts all 420 apartments into the units table."""
    resolved_path = Path(csv_path) if csv_path else _DEFAULT_CSV

    if not resolved_path.exists():
        fallback_container = Path("/app/docs/Mapeo Deptos Sierra Plaza.csv")
        fallback_local = Path("backend/docs/Mapeo Deptos Sierra Plaza.csv")
        if fallback_container.exists():
            resolved_path = fallback_container
        elif fallback_local.exists():
            resolved_path = fallback_local
        else:
            logger.error("Mapping CSV not found at %s", resolved_path)
            return 0

    count = 0
    with open(resolved_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        session = db or SessionLocal()
        try:
            for row in reader:
                unit_code = f"{row['torre']}-{row['depto']}"
                stmt = select(UnitModel).where(UnitModel.unit_code == unit_code)
                unit = session.scalar(stmt)

                if unit:
                    unit.kastor_id = int(row["id"])
                else:
                    unit = UnitModel(
                        unit_code=unit_code,
                        tower=row["torre"],
                        unit_number=int(row["depto"]),
                        kastor_id=int(row["id"]),
                        alicuota=None,
                    )
                    session.add(unit)

                count += 1

            session.commit()
            logger.info("Successfully processed %d units.", count)
        except Exception as e:
            session.rollback()
            logger.error("Seeding failed: %s", e)
            raise e
        finally:
            if db is None:
                session.close()

    return count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_units_from_csv()
