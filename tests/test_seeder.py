import pytest
from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from backend.app.models import UnitModel, Base
from backend.app.extractor.seed_units import seed_units_from_csv


def test_seed_units_from_csv():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        # Seed
        count = seed_units_from_csv(db=db)
        assert count == 420

        # Verify specific unit
        unit = db.scalar(select(UnitModel).where(UnitModel.unit_code == "B-504"))
        assert unit is not None
        assert unit.kastor_id == 336
        assert unit.tower == "B"
        assert unit.unit_number == 504

        # Verify idempotency
        count2 = seed_units_from_csv(db=db)
        assert count2 == 420
