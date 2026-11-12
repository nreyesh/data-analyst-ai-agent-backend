"""Tests for the standalone evaluation benchmark harness and scoring evaluators."""

from __future__ import annotations

import pytest
from backend.tests.eval.run_evals import (
    evaluate_numeric_accuracy,
    evaluate_routing_correctness,
    evaluate_sql_syntax_safety,
    run_evaluation_suite,
)


def test_evaluate_routing_correctness() -> None:
    """Verify route evaluation matches expected routes case-insensitively."""
    assert evaluate_routing_correctness("lookup_unit_bill", "lookup_unit_bill")["passed"] is True
    assert evaluate_routing_correctness("LOOKUP_UNIT_BILL", "lookup_unit_bill")["passed"] is True
    assert evaluate_routing_correctness("lookup_unit_bill", "direct_response")["passed"] is False


def test_evaluate_sql_syntax_safety() -> None:
    """Verify SQL safety validator permits SELECT and rejects mutations."""
    assert evaluate_sql_syntax_safety("SELECT * FROM expense_items WHERE id = 1")["passed"] is True
    assert evaluate_sql_syntax_safety("WITH q AS (SELECT 1) SELECT * FROM q")["passed"] is True
    assert evaluate_sql_syntax_safety("DROP TABLE units")["passed"] is False
    assert evaluate_sql_syntax_safety("DELETE FROM expense_reports")["passed"] is False
    assert evaluate_sql_syntax_safety(None)["passed"] is True


def test_evaluate_numeric_accuracy() -> None:
    """Verify Chilean CLP number extraction and tolerance comparison."""
    # Exact formatted CLP amount: $1.850.000 CLP
    reply = "El gasto total de la cuenta mantención fue de $1.850.000 CLP en agosto 2026."
    res = evaluate_numeric_accuracy(expected_amount=1850000, text_reply=reply)
    assert res["passed"] is True

    # Slightly different within 1%
    reply_close = "El total registrado es de $1.840.000 CLP."
    res_close = evaluate_numeric_accuracy(expected_amount=1850000, text_reply=reply_close, tolerance=0.01)
    assert res_close["passed"] is True

    # Way off
    reply_wrong = "El total registrado es de $500.000 CLP."
    res_wrong = evaluate_numeric_accuracy(expected_amount=1850000, text_reply=reply_wrong)
    assert res_wrong["passed"] is False


def test_run_evaluation_suite_offline() -> None:
    """Verify run_evaluation_suite passes benchmark on standard test cases."""
    success = run_evaluation_suite(use_langsmith=False)
    assert success is True
