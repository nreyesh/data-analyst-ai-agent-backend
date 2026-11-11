"""Command-Line Interface (CLI) for condominium financial data extraction and database population."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
import logging
import sys
from typing import List, Tuple

import httpx
from sqlalchemy import select

from backend.app.database import SessionLocal, engine
from backend.app.extractor.downloader import Downloader
from backend.app.extractor.extractor import CondominiumExtractor, format_error_message
from backend.app.extractor.seed_units import seed_units_from_csv
from backend.app.models import Base, ExpenseReportModel, UnitBillModel, UnitModel

logger = logging.getLogger("backend.extractor.cli")


def setup_logging(verbose: bool = False) -> None:
    """Configure console logging level and format."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress verbose third-party HTTP and internal library logging
    # to avoid publishing download URLs or noisy connection traces
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_month_str(month_str: str) -> Tuple[int, int]:
    """Parse 'YYYY-MM' or 'YYYY-M' into (year, month)."""
    parts = month_str.strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid month format '{month_str}'. Expected 'YYYY-MM' (e.g. '2026-09').")
    return int(parts[0]), int(parts[1])


def get_current_closed_month() -> Tuple[int, int]:
    """Return the most recently closed accounting month (one month behind current date)."""
    today = date.today()
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def generate_month_range(from_str: str, to_str: str) -> List[Tuple[int, int]]:
    """Generate list of (year, month) tuples inclusive between from_str and to_str."""
    start_year, start_month = parse_month_str(from_str)
    end_year, end_month = parse_month_str(to_str)

    months: List[Tuple[int, int]] = []
    curr_y, curr_m = start_year, start_month

    while (curr_y < end_year) or (curr_y == end_year and curr_m <= end_month):
        months.append((curr_y, curr_m))
        curr_m += 1
        if curr_m > 12:
            curr_m = 1
            curr_y += 1

    return months


async def process_pipeline(args: argparse.Namespace) -> int:
    """Execute the extraction pipeline based on parsed CLI arguments."""
    setup_logging(args.verbose)
    Base.metadata.create_all(engine)
    db = SessionLocal()

    # 1. Resolve target months
    target_months: List[Tuple[int, int]] = []
    if args.current_month:
        target_months = [get_current_closed_month()]
    elif args.month:
        target_months = [parse_month_str(args.month)]
    elif args.from_month and args.to_month:
        target_months = generate_month_range(args.from_month, args.to_month)
    else:
        logger.info("No date specified. Defaulting to most recent closed month (--current-month).")
        target_months = [get_current_closed_month()]

    month_labels = [f"{y}-{m:02d}" for y, m in target_months]
    logger.info("Target periods to process: %s", ", ".join(month_labels))

    # 2. Check / Auto-seed units if empty
    try:
        unit_count = db.query(UnitModel).count()
        if unit_count == 0:
            logger.info("Units master table is empty. Running auto-seeder...")
            seed_units_from_csv(db=db)
    except Exception as exc:
        logger.warning("Could not check or seed units table: %s", exc)

    downloader = Downloader()
    extractor = CondominiumExtractor()

    stats = {
        "building_synced": 0,
        "building_skipped": 0,
        "building_failed": 0,
        "units_synced": 0,
        "units_skipped": 0,
        "units_not_published": 0,
        "units_failed": 0,
    }

    try:
        transport = httpx.AsyncHTTPTransport(retries=3)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True, timeout=30.0) as http_client:
            for year, month in target_months:
                period_iso = f"{year}-{month:02d}-01"
                logger.info("=== Processing Period %s ===", period_iso)

                # -------------------------------------------------------------
                # Target: Building Expenses
                # -------------------------------------------------------------
                if args.target in ("building", "all"):
                    # Check DB checkpoint
                    existing_report = db.scalar(
                        select(ExpenseReportModel.id).where(
                            ExpenseReportModel.period_date == period_iso
                        )
                    )
                    if existing_report and not args.force:
                        logger.info("Building report for %s already exists in DB. Skipping.", period_iso)
                        stats["building_skipped"] += 1
                    else:
                        logger.info("Fetching building PDF for %s...", period_iso)
                        try:
                            pdf_bytes, file_uri = await downloader.download_building_pdf(year, month, client=http_client)
                            report, metadata, gt, val = await extractor.extract_building_report(pdf_bytes)

                            if not args.dry_run:
                                extractor.save_building_report_to_db(
                                    db=db,
                                    report=report,
                                    file_uri=file_uri,
                                    metadata=metadata,
                                    ground_truth=gt,
                                )
                            logger.info("✅ Building report for %s successfully synced.", period_iso)
                            stats["building_synced"] += 1
                        except FileNotFoundError:
                            logger.warning("Building report PDF for %s not published yet (404).", period_iso)
                            stats["building_failed"] += 1
                        except Exception as exc:
                            logger.error("❌ Failed processing building report for %s: %s", period_iso, format_error_message(exc))
                            stats["building_failed"] += 1

                # -------------------------------------------------------------
                # Target: Unit Bills
                # -------------------------------------------------------------
                if args.target in ("units", "all"):
                    # Resolve units to process
                    if args.unit.lower() == "all":
                        stmt = (
                            select(UnitModel.unit_code, UnitModel.kastor_id)
                            .where(UnitModel.kastor_id.is_not(None))
                            .order_by(UnitModel.unit_code)
                        )
                    else:
                        stmt = (
                            select(UnitModel.unit_code, UnitModel.kastor_id)
                            .where(UnitModel.unit_code == args.unit.strip().upper())
                        )

                    unit_records = db.execute(stmt).all()
                    if not unit_records:
                        logger.warning("No matching units with valid kastor_id found for unit='%s'.", args.unit)

                    for u_code, k_id in unit_records:
                        # Check DB checkpoint
                        existing_bill = db.scalar(
                            select(UnitBillModel.id).where(
                                UnitBillModel.unit_code == u_code,
                                UnitBillModel.period_date == period_iso,
                            )
                        )
                        if existing_bill and not args.force:
                            logger.debug("Unit %s period %s already exists in DB. Skipping.", u_code, period_iso)
                            stats["units_skipped"] += 1
                            continue

                        try:
                            pdf_bytes, file_uri = await downloader.download_unit_pdf(
                                kastor_id=k_id,
                                unit_code=u_code,
                                year=year,
                                month=month,
                                client=http_client,
                            )
                            report, metadata, gt, val = await extractor.extract_unit_bill(pdf_bytes)

                            if not args.dry_run:
                                extractor.save_unit_bill_to_db(
                                    db=db,
                                    report=report,
                                    file_uri=file_uri,
                                    metadata=metadata,
                                    ground_truth=gt,
                                )
                            logger.info("✅ Unit %s period %s successfully synced.", u_code, period_iso)
                            stats["units_synced"] += 1
                        except FileNotFoundError:
                            logger.debug("Unit %s bill for %s not published yet (404).", u_code, period_iso)
                            stats["units_not_published"] += 1
                        except Exception as exc:
                            logger.error("❌ Failed processing unit %s for %s: %s", u_code, period_iso, format_error_message(exc))
                            stats["units_failed"] += 1

        logger.info("==================================================")
        logger.info("📊 EXTRACTION PIPELINE SUMMARY")
        logger.info("==================================================")
        logger.info(
            "Building Reports  : ✅ Synced: %d | ⏭️  Skipped: %d | ❌ Failed: %d",
            stats["building_synced"],
            stats["building_skipped"],
            stats["building_failed"],
        )
        logger.info(
            "Unit Bills        : ✅ Synced: %d | ⏭️  Skipped: %d | ⏳ Not Published: %d | ❌ Failed: %d",
            stats["units_synced"],
            stats["units_skipped"],
            stats["units_not_published"],
            stats["units_failed"],
        )
        logger.info("==================================================")

        return 0 if (stats["building_failed"] == 0 and stats["units_failed"] == 0) else 1

    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Condominium Expense PDF Extractor & Database Ingestion CLI"
    )
    parser.add_argument(
        "--target",
        choices=["building", "units", "all"],
        default="all",
        help="Target domain to extract: 'building', 'units', or 'all' (default: all)",
    )
    parser.add_argument(
        "--unit",
        default="all",
        help="Target unit code (e.g. 'B-504') or 'all' for condominium-wide backfill (default: all)",
    )
    parser.add_argument(
        "--month",
        help="Single target month in 'YYYY-MM' format (e.g. '2026-09')",
    )
    parser.add_argument(
        "--from",
        dest="from_month",
        help="Start month in 'YYYY-MM' format for historical range backfill",
    )
    parser.add_argument(
        "--to",
        dest="to_month",
        help="End month in 'YYYY-MM' format for historical range backfill",
    )
    parser.add_argument(
        "--current-month",
        action="store_true",
        help="Automatically target the latest closed billing month (1 month behind today)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract and overwrite existing records in the database",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and extract without committing changes to PostgreSQL",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable detailed debug logging",
    )

    args = parser.parse_args()
    exit_code = asyncio.run(process_pipeline(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
