"""Tests for Downloader storage paths and fallback mechanics."""

from pathlib import Path
import pytest

from backend.app.extractor.downloader import Downloader


def test_downloader_local_fallback(tmp_path: Path):
    """Verify that Downloader saves PDF to local disk when GCS is unavailable."""
    downloader = Downloader(local_fallback_dir=tmp_path)
    downloader._gcs_available = False  # force local fallback

    dummy_pdf = b"%PDF-1.7\nSample PDF stream"

    # Test building path
    saved_building = downloader.save_pdf(dummy_pdf, "building/2026-09.pdf")
    expected_building = tmp_path / "building" / "2026-09.pdf"
    assert expected_building.is_file()
    assert expected_building.read_bytes() == dummy_pdf
    assert str(expected_building.resolve()) == saved_building

    # Test unit path
    saved_unit = downloader.save_pdf(dummy_pdf, "units/B-504/2026-09.pdf")
    expected_unit = tmp_path / "units" / "B-504" / "2026-09.pdf"
    assert expected_unit.is_file()
    assert expected_unit.read_bytes() == dummy_pdf
    assert str(expected_unit.resolve()) == saved_unit
