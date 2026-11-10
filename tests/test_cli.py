"""Tests for CLI date utilities and argument parsing."""

from datetime import date
import pytest

from backend.app.extractor.cli import (
    generate_month_range,
    get_current_closed_month,
    parse_month_str,
)


def test_parse_month_str():
    assert parse_month_str("2026-09") == (2026, 9)
    assert parse_month_str("2026-9") == (2026, 9)
    assert parse_month_str("2025-12") == (2025, 12)

    with pytest.raises(ValueError):
        parse_month_str("invalid")


def test_generate_month_range():
    # Single month
    r1 = generate_month_range("2026-05", "2026-05")
    assert r1 == [(2026, 5)]

    # 3 months in same year
    r2 = generate_month_range("2026-01", "2026-03")
    assert r2 == [(2026, 1), (2026, 2), (2026, 3)]

    # Span across year boundary
    r3 = generate_month_range("2025-11", "2026-02")
    assert r3 == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


def test_get_current_closed_month():
    year, month = get_current_closed_month()
    today = date.today()
    if today.month == 1:
        assert year == today.year - 1
        assert month == 12
    else:
        assert year == today.year
        assert month == today.month - 1
