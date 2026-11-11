"""API router for conversational supervisor turns with two-layer security enforcement."""

from __future__ import annotations

import logging
from typing import Any
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

logger = logging.getLogger("backend.app.routes.chat")

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatTurnRequest(BaseModel):
    """Request payload sent by the Chainlit frontend for a single conversational turn."""

    model_config = ConfigDict(extra="ignore")

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

    turn_result, updated_messages, updated_focus = run_supervisor_turn(
        user_message=payload.user_message,
        user_context=verified_ctx,
        messages=payload.messages,
        focus=payload.focus,
        clarification_choice=payload.clarification_choice,
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
    )
