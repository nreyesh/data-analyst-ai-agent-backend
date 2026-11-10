"""API routes for synchronization and unit management."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from backend.app.database import get_db
from backend.app.extractor.seed_units import seed_units_from_csv

router = APIRouter(prefix="/sync", tags=["sync"])

@router.post("/seed-units", status_code=status.HTTP_200_OK)
def seed_units(db: Session = Depends(get_db)):
    """Manually trigger seeding of units from CSV."""
    try:
        count = seed_units_from_csv(db=db)
        return {
            "status": "success",
            "total_units_seeded": count,
            "message": f"Successfully seeded {count} units with Kastor mapping IDs."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
