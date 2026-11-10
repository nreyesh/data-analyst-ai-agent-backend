"""Idempotent seeder for condominium units mapping."""
import csv
import logging
from pathlib import Path
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from backend.app.models import UnitModel
from backend.app.database import engine, SessionLocal

logger = logging.getLogger("backend.extractor.seed_units")

def seed_units_from_csv(db: Session | None = None) -> int:
    """Reads mapping CSV and bulk upserts all 420 apartments into the units table."""
    
    # Locate CSV inside container or repo
    csv_path = Path("/app/docs/Mapeo Deptos Sierra Plaza.csv")
    if not csv_path.exists():
        csv_path = Path("backend/docs/Mapeo Deptos Sierra Plaza.csv")
    
    if not csv_path.exists():
        logger.error("Mapping CSV not found at %s", csv_path)
        return 0

    count = 0
    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        
        # Use provided session or create new
        session = db or SessionLocal()
        try:
            for row in reader:
                # Normalization: TORRE-DEPTO
                unit_code = f"{row['torre']}-{row['depto']}"
                
                # Check for existence
                stmt = select(UnitModel).where(UnitModel.unit_code == unit_code)
                unit = session.scalar(stmt)
                
                if unit:
                    # Update
                    unit.kastor_id = int(row['id'])
                else:
                    # Create
                    unit = UnitModel(
                        unit_code=unit_code,
                        tower=row['torre'],
                        unit_number=int(row['depto']),
                        kastor_id=int(row['id']),
                        alicuota=None # Nullable until first bill
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
