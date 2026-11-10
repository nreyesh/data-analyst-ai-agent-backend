# Implementation Plan: Adding `kastor_id` to UnitModel & Cloud-Ready Seeder

## Goal Description
To enable automated monthly and historical PDF downloads from Kastor (`https://dag-ms.kastor.cl/.../boletas/boletas/{kastor_id}/{year}/{month}/1`), we need to:
1. Extend `UnitModel` in PostgreSQL with `kastor_id` (e.g., `336` for `B-504`) and adjust `alicuota` to allow `NULL` until each apartment's first bill is processed.
2. Generate an Alembic migration (`0002_add_kastor_id_to_units.py`) to apply this change to Cloud SQL.
3. Implement a cloud-ready, idempotent seeder script (`backend/app/extractor/seed_units.py`) and CLI runner that reads `backend/docs/Mapeo Deptos Sierra Plaza.csv` and populates all 420 apartments directly into Cloud SQL over the private VPC.
4. Build a high-resilience, dual-model extraction pipeline with 20 RPM combined throughput, checkpoint resume, and Cloud Scheduler integration.

---

## User Review Required

> [!IMPORTANT]
> **Dual-Model Rotation Strategy (20 RPM Combined Throughput)**
> * **Quota per model:** 15 RPM, 500 RPD per model.
> * **Strategy:** Interleave requests between Model A (Gemini Flash Lite 3.5 / 2.5) and Model B (Gemini Flash Lite 3.1 / 2.0).
> * **Effective Pacing:** 1 request every 3 seconds = **20 RPM total** (10 RPM per model).
> * **Execution Time:** A full monthly cycle of 420 apartments completes in **~21 minutes** (down from 35 mins) while staying well under each model's 15 RPM ceiling.
> * **Daily Quota Safety:** 420 requests distributed as ~210 requests per model, well below the 500 RPD free tier limit.

> [!IMPORTANT]
> **Proration Share (`alicuota`) Timing Decision**
> The CSV `Mapeo Deptos Sierra Plaza.csv` contains `id,depto,torre`, but does **not** contain each flat's `alicuota` percentage share (e.g., `0.16736%`).
> 
> **Proposed Solution:** Make `alicuota` nullable (`Decimal | None`) in `UnitModel`. The seeder will register all 420 apartments with their `unit_code`, `tower`, `unit_number`, and `kastor_id`. When the extractor downloads and parses each unit's first monthly bill, it will automatically populate and persist the official `alicuota` extracted from the document.

> [!NOTE]
> **Execution in Google Cloud (Zero HTTP Endpoints for Seeding)**
> As agreed, we will **not** expose an HTTP endpoint for unit seeding.
> Seeding is executed via:
> 1. **CLI Runner**: `python -m backend.app.extractor.seed_units` (runs inside a Cloud Run Job or local dev).
> 2. **Auto-Seed on Startup**: During backend container startup in Cloud Run, if `SELECT count(*) FROM units` is 0, it seeds automatically from the bundled CSV in under 1 second.

---

## Proposed Changes

```
backend/
├── app/
│   ├── models.py                     # [MODIFY] Add kastor_id, make alicuota nullable
│   ├── main.py                       # [MODIFY] Auto-seed on startup if units table empty
│   └── extractor/
│       ├── __init__.py
│       ├── seed_units.py             # [NEW] Idempotent CSV parser & unit upsert logic
│       ├── downloader.py             # [NEW] Fetches building & unit PDFs from Kastor URLs
│       ├── rate_limiter.py           # [NEW] Dual-model token bucket (20 RPM interleaved)
│       ├── extractor.py              # [NEW] Multimodal extraction with dual-model failover
│       └── cli.py                    # [NEW] CLI with flexible target, unit, and date range flags
├── alembic/
│   └── versions/
│       └── 0002_add_kastor_id.py     # [NEW] Migration for kastor_id and nullable alicuota
infra/
└── main.tf                           # [MODIFY] Add Cloud Run Job & Cloud Scheduler trigger
└── tests/
    └── test_seeder.py                # [NEW] Unit test verifying 420 units seeded
```

---

### 1. Database Layer

#### [MODIFY] `backend/app/models.py`
Add `kastor_id` to `UnitModel` and update `alicuota` definition:

```python
class UnitModel(Base):
    """Master record for the 420 condominium apartments ('Unidades')."""

    __tablename__ = "units"

    unit_code: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        comment="Natural primary key e.g. 'B-504'",
    )
    tower: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Tower identifier e.g. 'A', 'B'",
    )
    unit_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Unit number e.g. 504",
    )
    kastor_id: Mapped[int | None] = mapped_column(
        Integer,
        unique=True,
        nullable=True,
        index=True,
        comment="Kastor unit ID (e.g. 336 for Depto 504 Torre B)",
    )
    alicuota: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 5),
        nullable=True,
        default=None,
        comment="Official proration share percentage (populated on first bill extraction)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    bills: Mapped[list[UnitBillModel]] = relationship(
        back_populates="unit",
        passive_deletes="all",
    )

    __table_args__ = (
        UniqueConstraint("tower", "unit_number", name="uq_units_tower_unit"),
        CheckConstraint("alicuota IS NULL OR alicuota > 0", name="chk_units_alicuota_positive"),
        Index("idx_units_tower_number", "tower", "unit_number"),
    )
```

#### [NEW] `backend/alembic/versions/0002_add_kastor_id_to_units.py`
Additive migration to:
1. Add column `kastor_id` `INTEGER` to `units`.
2. Alter column `alicuota` to `nullable=True`.
3. Create unique index on `kastor_id`.
4. Update check constraint to `alicuota IS NULL OR alicuota > 0`.

---

### 2. Seeder Module

#### [NEW] `backend/app/extractor/seed_units.py`
Reads `Mapeo Deptos Sierra Plaza.csv` and performs idempotent bulk upserts:

```python
def seed_units_from_csv(
    csv_path: Path | None = None,
    db: Session | None = None,
) -> int:
    """Reads mapping CSV and bulk upserts all 420 apartments into the units table.
    
    Returns the count of units processed.
    """
```
* Locates CSV at `backend/docs/Mapeo Deptos Sierra Plaza.csv` or `/app/docs/Mapeo Deptos Sierra Plaza.csv` inside container.
* Normalizes unit code as `{torre}-{depto}`.
* Sets `kastor_id = int(row['id'])`.
* Uses PostgreSQL `ON CONFLICT (unit_code) DO UPDATE` so re-running the seeder is safe and idempotent.

---

### 3. Downloader & Dual-Model Rate Limiter

#### [NEW] `backend/app/extractor/downloader.py`
Fetches PDFs directly from Kastor endpoints:
* **Building Report:** `https://dag-ms.kastor.cl/edificio_siplaza/minutas/pdf_mail/{year}-{month}`
* **Unit Bill:** `https://dag-ms.kastor.cl/edificio_siplaza/boletas/boletas/{kastor_id}/{year}/{month}/1`
* Includes HTTP retry logic with backoff for transient network issues.

#### [NEW] `backend/app/extractor/rate_limiter.py`
Interleaves requests between Model A and Model B:
* Target: 20 RPM (1 request every 3.0s).
* Round-robin dispatching: Request $N$ goes to Model A, Request $N+1$ goes to Model B.
* Each model receives 10 RPM (well below the 15 RPM cap).
* If Model A encounters a 429 rate limit, it backs off and temporarily shifts traffic to Model B.

---

### 4. CLI Execution Modes

#### [NEW] `backend/app/extractor/cli.py`
Provides flexible command-line execution:

```bash
# Mode 1: Specific unit or all units
python -m backend.app.extractor.cli --target units --unit B-504 --month 2026-09
python -m backend.app.extractor.cli --target units --unit all --month 2026-09
python -m backend.app.extractor.cli --target units --unit B-504 --from 2025-01 --to 2026-09
python -m backend.app.extractor.cli --target units --unit all --current-month

# Mode 2: Building expenses
python -m backend.app.extractor.cli --target building --month 2026-09
python -m backend.app.extractor.cli --target building --from 2025-01 --to 2026-09
python -m backend.app.extractor.cli --target building --current-month

# Mode 3: Complete monthly sync (Building + All Units)
python -m backend.app.extractor.cli --target all --month 2026-09
```

#### Idempotency & Checkpoint Resume:
* Before downloading each unit, queries: `SELECT 1 FROM unit_bills WHERE unit_code = :code AND period_date = :date`.
* Skips completed units instantly (0 API calls, 0 cost).
* Commits each unit in its own database transaction.

---

### 5. Automated Cloud Monthly Trigger

#### [MODIFY] `infra/main.tf`
Add Cloud Run Job and Cloud Scheduler resource:
1. `google_cloud_run_v2_job.sync_monthly`:
   * Command: `["python", "-m", "backend.app.extractor.cli", "--target", "all", "--current-month"]`
   * Timeout: 3,600s (60 minutes).
2. `google_cloud_scheduler_job.monthly_sync_trigger`:
   * Schedule: `0 2 6 * *` (02:00 AM on the 6th day of every month).
   * Target: Triggers the Cloud Run Job via IAM.

---

## Verification Plan

### Automated Tests
```bash
PYTHONPATH=. pytest backend/tests/test_seeder.py -v
PYTHONPATH=. pytest backend/tests/test_schema_integrity.py -v
```

1. **Verify 420 Units Seeded:** Check count is exactly 420.
2. **Verify Specific Unit Mapping:** Check `UnitModel.get('B-504')` has `tower='B'`, `unit_number=504`, and `kastor_id=336`.
3. **Verify Idempotency:** Running the seeder twice does not raise duplicates or modify existing records.
4. **Verify Migration SQL:** Run `alembic upgrade head --sql` to verify clean PostgreSQL DDL syntax.
5. **Verify Rate Limiter:** Verify interleaved model rotation and token bucket pacing.
