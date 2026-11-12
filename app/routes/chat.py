"""API router for conversational supervisor turns with two-layer security enforcement."""

from __future__ import annotations

import logging
from typing import Any
import uuid
from fastapi import APIRouter, Depends, Header, HTTPException, status

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.agent.security import (
    MAX_STRIKES,
    _ensure_aware,
    record_guardrail_strike,
    verify_request_credentials,
)
from backend.app.agent.state import FocusContext, SupervisorTurnResult, UserContext
from backend.app.agent.supervisor import run_supervisor_turn
from backend.app.database import get_db
from backend.app.observability import record_feedback


logger = logging.getLogger("backend.app.routes.chat")

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatTurnRequest(BaseModel):
    """Request payload sent by the Chainlit frontend for a single conversational turn."""

    model_config = ConfigDict(extra="ignore")

    conversation_id: str | None = Field(
        default=None,
        description="Persistent conversation session UUIDv4",
    )
    user_message: str = Field(default="", description="User's natural language message")
    user_context: UserContext = Field(..., description="Authenticated user context")
    messages: list[Any] = Field(
        default_factory=list,
        description="Ephemeral conversation history (up to 8 messages)",
    )
    focus: FocusContext = Field(
        default_factory=FocusContext,
        description="Sticky conversational focus across turns",
    )
    clarification_choice: str | None = Field(
        default=None,
        description="Optional selected option from a clarification button",
    )


class ChatTurnResponse(BaseModel):
    """Response payload returned to the Chainlit frontend."""

    turn_result: SupervisorTurnResult
    messages: list[dict[str, str]]
    focus: FocusContext
    conversation_id: str = Field(..., description="Active persistent conversation session UUIDv4")
    run_id: str | None = Field(default=None, description="LangSmith trace execution run ID")


class FeedbackRequest(BaseModel):
    """User feedback payload linking a response to LangSmith metrics."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = Field(..., description="Target LangSmith execution run UUID")
    key: str = Field(default="user_score", description="Metric key: user_score, correctness, etc.")
    score: float = Field(..., description="Numeric score (1.0 for positive, 0.0 for negative)")
    value: str | None = Field(default=None, description="Categorical rating or label")
    comment: str | None = Field(default=None, description="Optional user commentary or reason")




def _serialize_messages(messages: list[BaseMessage | dict[str, Any]]) -> list[dict[str, str]]:
    """Serialize LangChain BaseMessage objects into JSON-serializable role/content dicts."""
    serialized: list[dict[str, str]] = []
    for m in messages:
        if isinstance(m, dict):
            role = "assistant" if m.get("role") in ("assistant", "ai") else "user"
            serialized.append({"role": role, "content": str(m.get("content", ""))})
        elif isinstance(m, AIMessage):
            serialized.append({"role": "assistant", "content": str(m.content)})
        else:
            serialized.append({"role": "user", "content": str(getattr(m, "content", m))})
    return serialized


@router.post(
    "/turn",
    response_model=ChatTurnResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute one conversational supervisor turn",
    description="Validates credentials (Layer A), runs the Conversational Supervisor, and enforces the 3-strike guardrail policy (Layer B).",
)
def chat_turn_endpoint(
    payload: ChatTurnRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> ChatTurnResponse:
    """Process a single conversational turn with full credential and guardrail audit logging."""
    # Layer A: Check active 24h block, token expiration (401 without block), and credential tampering (immediate 24h block)
    verified_ctx = verify_request_credentials(
        authorization_header=authorization,
        claimed_context=payload.user_context,
        db=db,
    )

    active_conversation_id = payload.conversation_id or str(uuid.uuid4())

    turn_result, updated_messages, updated_focus = run_supervisor_turn(
        user_message=payload.user_message,
        user_context=verified_ctx,
        messages=payload.messages,
        focus=payload.focus,
        clarification_choice=payload.clarification_choice,
        conversation_id=active_conversation_id,
        http_client=db,
    )


    # Layer B: 3-Strike Policy for RBAC / Scope Guardrail Violations
    if turn_result.security_violation_type:
        strike_record = record_guardrail_strike(
            db=db,
            user_uid=verified_ctx.uid,
            user_email=verified_ctx.email,
            violation_type=turn_result.security_violation_type,
            reason=turn_result.security_violation_reason or turn_result.reply,
        )
        if strike_record.is_blocked:
            blocked_until_iso = (
                _ensure_aware(strike_record.blocked_until).isoformat()  # type: ignore[union-attr]
                if strike_record.blocked_until
                else ""
            )
            block_msg = (
                f"{turn_result.reply}\n\n"
                f"⛔ **Cuenta bloqueada por 24 horas (Intento {strike_record.strike_number}/{MAX_STRIKES}).**\n"
                f"Has superado el límite de intentos no autorizados. "
                f"Tu cuenta permanecerá bloqueada hasta `{blocked_until_iso}`."
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "user_blocked",
                    "reason": strike_record.reason,
                    "violation_type": strike_record.violation_type,
                    "strike_number": strike_record.strike_number,
                    "blocked_until": blocked_until_iso,
                    "message": block_msg,
                },
            )

        warning_banner = (
            f"\n\n⚠️ **Advertencia de Seguridad ({strike_record.strike_number}/{MAX_STRIKES})**: "
            f"Esta consulta activó un control de seguridad o acceso por rol. "
            f"Al tercer intento no autorizado tu cuenta será bloqueada por 24 horas."
        )
        turn_result.reply = f"{turn_result.reply}{warning_banner}"

    return ChatTurnResponse(
        turn_result=turn_result,
        messages=_serialize_messages(updated_messages),
        focus=updated_focus,
        conversation_id=active_conversation_id,
        run_id=turn_result.run_id,
    )


@router.post("/feedback")
def chat_feedback_endpoint(
    payload: FeedbackRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record user feedback (thumbs up/down, comment) for an executed turn run in LangSmith."""
    user_uid = None
    role = None
    if authorization:
        try:
            token_str = authorization.replace("Bearer ", "").strip()
            if token_str.startswith("dev-token:"):
                parts = token_str.split(":")
                if len(parts) >= 4:
                    user_uid = parts[1]
                    role = parts[3]
        except Exception:
            pass

    source_info: dict[str, Any] = {}
    if user_uid:
        source_info["user_uid"] = user_uid
    if role:
        source_info["role"] = role

    recorded = record_feedback(
        run_id=payload.run_id,
        key=payload.key,
        score=payload.score,
        value=payload.value,
        comment=payload.comment,
        source_info=source_info or None,
    )
    return {
        "success": True,
        "run_id": payload.run_id,
        "recorded": recorded,
    }


