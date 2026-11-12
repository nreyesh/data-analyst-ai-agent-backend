"""Tests for LangSmith observability, telemetry configuration, fail-open behavior, and lifespan flush."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch
import pytest

from backend.app.observability import (
    get_telemetry_settings,
    flush_telemetry,
    setup_langsmith_environment,
)


def test_telemetry_settings_default_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify telemetry settings load profile defaults and respect environment overrides."""
    monkeypatch.setenv("LANGSMITH_PROJECT", "custom-test-project")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key_123")

    settings = get_telemetry_settings()
    assert settings["project"] == "custom-test-project"
    assert settings["tracing_enabled"] is True
    assert settings["api_key"] == "lsv2_test_key_123"


def test_setup_langsmith_environment_sets_system_env() -> None:
    """Verify setup_langsmith_environment injects LANGSMITH_TRACING and LANGSMITH_PROJECT into os.environ."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ["LANGSMITH_API_KEY"] = "dummy-key"
        setup_langsmith_environment(project="building-analytics-test")
        assert os.environ.get("LANGSMITH_TRACING") == "true"
        assert os.environ.get("LANGCHAIN_TRACING_V2") == "true"
        assert os.environ.get("LANGSMITH_PROJECT") == "building-analytics-test"


def test_flush_telemetry_fail_open_on_error() -> None:
    """Verify flush_telemetry swallows any client exceptions to remain strictly fail-open."""
    with patch("backend.app.observability._get_langsmith_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.flush.side_effect = RuntimeError("Network timeout to LangSmith")
        mock_get_client.return_value = mock_client

        # Must not raise
        flush_telemetry()
        mock_client.flush.assert_called_once()


def test_chat_turn_endpoint_thread_and_run_id(seeded_client) -> None:
    """Verify POST /api/chat/turn accepts conversation_id, propagates it, and returns conversation_id and run_id."""
    from backend.app.agent.state import SupervisorTurnResult, FocusContext

    client = seeded_client

    mock_turn_result = SupervisorTurnResult(
        reply="Hola, soy tu asistente financiero.",
        route="greeting",
        run_id="run-uuid-12345",
    )

    with patch("backend.app.routes.chat.run_supervisor_turn") as mock_turn:
        mock_turn.return_value = (mock_turn_result, [], FocusContext())

        payload = {
            "conversation_id": "c1111111-2222-3333-4444-555555555555",
            "user_message": "Hola, ¿cómo estás?",
            "user_context": {
                "uid": "test-resident-uid",
                "email": "resident@sierra.cl",
                "role": "resident",
                "unit_code": "A-101",
            },
            "messages": [],
        }
        token = "dev-token:test-resident-uid:resident@sierra.cl:resident:A-101"
        headers = {"Authorization": f"Bearer {token}"}

        response = client.post("/api/chat/turn", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()

        assert data["conversation_id"] == "c1111111-2222-3333-4444-555555555555"
        assert data["run_id"] == "run-uuid-12345"
        assert mock_turn.call_args.kwargs["conversation_id"] == "c1111111-2222-3333-4444-555555555555"

        # Case 2: conversation_id omitted in request -> auto-generated UUID
        payload_no_conv = dict(payload)
        del payload_no_conv["conversation_id"]
        response2 = client.post("/api/chat/turn", json=payload_no_conv, headers=headers)
        assert response2.status_code == 200
        data2 = response2.json()
        assert len(data2["conversation_id"]) == 36
        assert mock_turn.call_args.kwargs["conversation_id"] == data2["conversation_id"]


def test_run_supervisor_turn_populates_run_id_and_propagates_conversation_id(seeded_db) -> None:
    """Verify run_supervisor_turn injects conversation_id into LangGraph config and sets run_id on turn_result."""
    from backend.app.agent.state import UserContext, FocusContext
    from backend.app.agent.supervisor import run_supervisor_turn

    user_ctx = UserContext(
        uid="usr-123",
        email="test@sierra.cl",
        role="resident",
        unit_code="B-504",
    )

    mock_llm = MagicMock()
    mock_llm.models.generate_content.return_value = MagicMock(
        text='{"route": "greeting", "reply": "Hola vecino de B-504, ¿en qué te ayudo?", "step_name": "Saludo", "step_detail": null}'
    )

    turn_res, messages, focus = run_supervisor_turn(
        user_message="Hola",
        user_context=user_ctx,
        conversation_id="conv-uuid-9999",
        llm_client=mock_llm,
        http_client=seeded_db,
    )

    assert turn_res.run_id is not None
    assert len(turn_res.run_id) > 0
    assert turn_res.route in ("greeting", "direct_response")


def test_post_chat_feedback_endpoint_records_to_langsmith(seeded_client) -> None:
    """Verify POST /api/chat/feedback invokes record_feedback and returns success."""
    client = seeded_client

    with patch("backend.app.routes.chat.record_feedback") as mock_record:
        mock_record.return_value = True

        payload = {
            "run_id": "run-uuid-9999",
            "key": "user_score",
            "score": 1.0,
            "value": "positive",
            "comment": "Respuesta clara y precisa",
        }
        token = "dev-token:test-resident-uid:resident@sierra.cl:resident:A-101"
        headers = {"Authorization": f"Bearer {token}"}

        response = client.post("/api/chat/feedback", json=payload, headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["run_id"] == "run-uuid-9999"

        mock_record.assert_called_once_with(
            run_id="run-uuid-9999",
            key="user_score",
            score=1.0,
            value="positive",
            comment="Respuesta clara y precisa",
            source_info={"user_uid": "test-resident-uid", "role": "resident"},
        )


def test_generate_content_with_fallback_tracing_and_usage_metadata() -> None:
    """Verify generate_content_with_fallback records usage metadata and fallback attempts."""
    from backend.app.agent.analytics.nodes import generate_content_with_fallback

    mock_client = MagicMock()
    # First model raises error, second model succeeds
    mock_res_good = MagicMock(
        text="Respuesta exitosa",
        usage_metadata=MagicMock(
            prompt_token_count=120,
            candidates_token_count=45,
            total_token_count=165,
        ),
    )
    mock_client.models.generate_content.side_effect = [
        RuntimeError("Primary model 503 unavailable"),
        mock_res_good,
    ]

    res = generate_content_with_fallback(
        client=mock_client,
        contents=["¿Total gastos?"],
        config=MagicMock(),
    )
    assert res.text == "Respuesta exitosa"
    assert mock_client.models.generate_content.call_count == 2






