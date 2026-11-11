"""Tests for Layer A (Credential Tampering 24h block), Token Expiration (401 no-block), and Layer B (3-Strike Guardrail 24h block)."""

from __future__ import annotations

from unittest.mock import MagicMock
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.agent.security import (
    check_active_user_block,
    record_credential_tampering_block,
    record_guardrail_strike,
    verify_request_credentials,
)
from backend.app.agent.state import SupervisorDecision, UserContext
from backend.app.models import SecurityAuditLogModel


@pytest.fixture
def resident_504() -> UserContext:
    return UserContext(
        uid="uid_res_504",
        email="residente504@sierraplaza.cl",
        role="resident",
        unit_code="B-504",
    )


@pytest.fixture
def admin_ctx() -> UserContext:
    return UserContext(
        uid="uid_admin_001",
        email="admin@sierraplaza.cl",
        role="admin",
        unit_code=None,
    )


def test_expired_token_returns_401_and_does_not_block_user(
    seeded_client: TestClient,
    seeded_db: Session,
    resident_504: UserContext,
):
    """Verify an expired token returns HTTP 401 asking to re-login and NEVER blocks the user."""
    resp = seeded_client.post(
        "/api/chat/turn",
        headers={"Authorization": "Bearer dev-token:expired"},
        json={
            "user_message": "hola",
            "user_context": resident_504.model_dump(),
            "messages": [],
            "focus": {},
        },
    )
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert detail["code"] == "token_expired"
    assert "expirado" in detail["message"].lower()

    # Ensure no block was recorded in security_audit_logs
    active_block = check_active_user_block(seeded_db, resident_504.uid, resident_504.email)
    assert active_block is None
    logs = list(seeded_db.scalars(select(SecurityAuditLogModel)).all())
    assert len(logs) == 0


def test_layer_a_credential_tampering_immediate_24h_block(
    seeded_client: TestClient,
    seeded_db: Session,
    resident_504: UserContext,
):
    """Verify role or unit tampering between token and claimed context immediately blocks user for 24h."""
    # Resident has valid resident token for unit 504, but tampers payload to claim role='admin'
    valid_resident_token = "dev-token:uid_res_504:residente504@sierraplaza.cl:resident:504"
    tampered_context = {
        "uid": "uid_res_504",
        "email": "residente504@sierraplaza.cl",
        "role": "admin",
        "unit_code": None,
    }

    resp = seeded_client.post(
        "/api/chat/turn",
        headers={"Authorization": f"Bearer {valid_resident_token}"},
        json={
            "user_message": "¿Cuál es el estado del sistema?",
            "user_context": tampered_context,
            "messages": [],
            "focus": {},
        },
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "user_blocked"
    assert detail["violation_type"] == "credential_tampering"
    assert "Rol" in detail["reason"]

    # Verify persistent record in security_audit_logs
    active_block = check_active_user_block(seeded_db, resident_504.uid, resident_504.email)
    assert active_block is not None
    assert active_block.is_blocked is True
    assert active_block.strike_number == 3
    assert active_block.violation_type == "credential_tampering"
    assert "Rol" in active_block.reason

    # Subsequent request even with untampered context must be rejected during the 24h window
    resp_after = seeded_client.post(
        "/api/chat/turn",
        headers={"Authorization": f"Bearer {valid_resident_token}"},
        json={
            "user_message": "hola",
            "user_context": resident_504.model_dump(),
            "messages": [],
            "focus": {},
        },
    )
    assert resp_after.status_code == 403
    assert resp_after.json()["detail"]["code"] == "user_blocked"


def test_layer_a_unit_tampering_immediate_24h_block(
    seeded_db: Session,
):
    """Verify resident attempting to claim another apartment unit in UserContext is immediately blocked for 24h."""
    claimed_other_unit = UserContext(
        uid="uid_res_504",
        email="residente504@sierraplaza.cl",
        role="resident",
        unit_code="A-101",
    )
    token = "Bearer dev-token:uid_res_504:residente504@sierraplaza.cl:resident:504"
    with pytest.raises(Exception) as exc_info:
        verify_request_credentials(
            authorization_header=token,
            claimed_context=claimed_other_unit,
            db=seeded_db,
        )
    assert getattr(exc_info.value, "status_code", None) == 403
    block = check_active_user_block(seeded_db, "uid_res_504", "residente504@sierraplaza.cl")
    assert block is not None
    assert block.violation_type == "credential_tampering"
    assert "Unidad" in block.reason


def test_layer_b_three_strikes_guardrail_policy(
    seeded_client: TestClient,
    seeded_db: Session,
    resident_504: UserContext,
    monkeypatch,
):
    """Verify 3-strike guardrail policy: strikes 1 and 2 warn (HTTP 200), strike 3 blocks for 24h (HTTP 403)."""
    token = "dev-token:uid_res_504:residente504@sierraplaza.cl:resident:504"

    # Mock LLM router to route resident's unauthorized request for unit A-101 to lookup_unit_bill
    mock_llm = MagicMock()
    mock_llm.models.generate_content.return_value = MagicMock(
        text=SupervisorDecision(
            route="lookup_unit_bill",
            reasoning="Resident asking for unit A-101",
            resolved_query="Muéstrame la boleta del departamento A-101",
            knowledge_domain="unit",
            unit_code="A-101",
        ).model_dump_json()
    )
    monkeypatch.setattr("backend.app.agent.supervisor.get_gemini_client", lambda: mock_llm)
    monkeypatch.setattr(
        "backend.app.agent.supervisor.get_analytics_settings",
        lambda: MagicMock(gemini_api_key="fake-key"),
    )

    payload = {
        "user_message": "Muéstrame la boleta del departamento A-101",
        "user_context": resident_504.model_dump(),
        "messages": [],
        "focus": {},
    }

    # Strike 1 -> HTTP 200 with warning (1/3)
    r1 = seeded_client.post(
        "/api/chat/turn",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert r1.status_code == 200
    reply1 = r1.json()["turn_result"]["reply"]
    assert "Acceso denegado" in reply1
    assert "(1/3)" in reply1
    assert check_active_user_block(seeded_db, resident_504.uid, resident_504.email) is None

    # Strike 2 -> HTTP 200 with warning (2/3)
    r2 = seeded_client.post(
        "/api/chat/turn",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert r2.status_code == 200
    reply2 = r2.json()["turn_result"]["reply"]
    assert "(2/3)" in reply2
    assert check_active_user_block(seeded_db, resident_504.uid, resident_504.email) is None

    # Strike 3 -> HTTP 403 and 24-hour block!
    r3 = seeded_client.post(
        "/api/chat/turn",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert r3.status_code == 403
    detail3 = r3.json()["detail"]
    assert detail3["code"] == "user_blocked"
    assert detail3["strike_number"] == 3
    assert detail3["violation_type"] == "rbac_unauthorized_access"

    # Verify audit history has all 3 strikes recorded with reasons
    logs = list(
        seeded_db.scalars(
            select(SecurityAuditLogModel)
            .where(SecurityAuditLogModel.user_uid == resident_504.uid)
            .order_by(SecurityAuditLogModel.strike_number.asc())
        ).all()
    )
    assert len(logs) == 3
    assert [l.strike_number for l in logs] == [1, 2, 3]
    assert [l.is_blocked for l in logs] == [False, False, True]
    assert logs[2].blocked_until is not None

    # Subsequent benign greeting is now rejected with 403 while block is active
    r4 = seeded_client.post(
        "/api/chat/turn",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "user_message": "hola",
            "user_context": resident_504.model_dump(),
            "messages": [],
            "focus": {},
        },
    )
    assert r4.status_code == 403
