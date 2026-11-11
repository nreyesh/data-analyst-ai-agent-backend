"""Identity verification, credential tampering protection, and 3-strike guardrail enforcement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from typing import Any, Callable
from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.agent.state import UserContext
from backend.app.models import SecurityAuditLogModel

logger = logging.getLogger("backend.agent.security")

BLOCK_DURATION_HOURS = 24
MAX_STRIKES = 3

# Default local development directory (mirrors front/auth.py for local dev/testing)
DEFAULT_DEV_ACCOUNTS: dict[str, dict[str, Any]] = {
    "admin@sierraplaza.cl": {
        "uid": "uid_admin_001",
        "role": "admin",
        "unit_code": None,
    },
    "residente504@sierraplaza.cl": {
        "uid": "uid_res_504",
        "role": "resident",
        "unit_code": "504",
    },
    "residente101@sierraplaza.cl": {
        "uid": "uid_res_101",
        "role": "resident",
        "unit_code": "101",
    },
}


class TokenExpiredException(Exception):
    """Raised when an authentication token has expired (must trigger 401 re-login, never a block)."""


class InvalidCredentialsException(Exception):
    """Raised when a token is forged, invalid, or its claims do not match the claimed user context."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _load_server_role_directory() -> dict[str, dict[str, Any]]:
    """Load server-side user role and unit directory from environment or defaults."""
    directory = {k.lower(): dict(v) for k, v in DEFAULT_DEV_ACCOUNTS.items()}
    raw_json = os.environ.get("DEV_USERS_JSON")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                for email, info in parsed.items():
                    if isinstance(info, dict):
                        directory[email.strip().lower()] = info
        except Exception as exc:
            logger.warning("Failed to parse DEV_USERS_JSON in backend security: %s", exc)
    return directory


def _units_match(token_unit: str | None, claimed_unit: str | None) -> bool:
    """Check whether token unit_code and claimed unit_code represent the same apartment."""
    t_norm = (token_unit or "").strip().upper()
    c_norm = (claimed_unit or "").strip().upper()
    if t_norm == c_norm:
        return True
    if not t_norm or not c_norm:
        return False
    # Allow bare unit number in dev token (e.g. "504") to match canonical "B-504" ONLY if neither specifies a conflicting tower
    if "-" not in t_norm and "-" in c_norm:
        return c_norm.split("-")[-1] == t_norm
    if "-" in t_norm and "-" not in c_norm:
        return t_norm.split("-")[-1] == c_norm
    return False


def check_active_user_block(
    db: Session,
    user_uid: str | None,
    user_email: str | None,
) -> SecurityAuditLogModel | None:
    """Return the active SecurityAuditLogModel block record if the user is currently blocked."""
    now = _utc_now()
    filters = []
    if user_uid:
        filters.append(SecurityAuditLogModel.user_uid == user_uid)
    if user_email:
        filters.append(SecurityAuditLogModel.user_email == user_email.strip().lower())
    if not filters:
        return None

    stmt = (
        select(SecurityAuditLogModel)
        .where(
            or_(*filters),
            SecurityAuditLogModel.is_blocked.is_(True),
            SecurityAuditLogModel.blocked_until.is_not(None),
        )
        .order_by(SecurityAuditLogModel.created_at.desc())
    )
    records = list(db.scalars(stmt).all())
    for rec in records:
        blocked_until = _ensure_aware(rec.blocked_until)
        if blocked_until and blocked_until > now:
            return rec
    return None


def record_credential_tampering_block(
    db: Session,
    user_uid: str,
    user_email: str,
    reason: str,
) -> SecurityAuditLogModel:
    """Immediately block a user for 24 hours due to Layer A Credential / Identity Tampering."""
    now = _utc_now()
    blocked_until = now + timedelta(hours=BLOCK_DURATION_HOURS)
    entry = SecurityAuditLogModel(
        user_uid=user_uid or "unknown_uid",
        user_email=(user_email or "unknown@email").strip().lower(),
        violation_type="credential_tampering",
        strike_number=MAX_STRIKES,
        is_blocked=True,
        blocked_until=blocked_until,
        reason=reason,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    logger.warning(
        "SECURITY BLOCK (24h) - Credential tampering detected for uid='%s' email='%s': %s",
        entry.user_uid,
        entry.user_email,
        reason,
    )
    return entry


def record_guardrail_strike(
    db: Session,
    user_uid: str,
    user_email: str,
    violation_type: str,
    reason: str,
) -> SecurityAuditLogModel:
    """Record a Layer B guardrail/RBAC violation strike and block for 24h on the 3rd strike."""
    now = _utc_now()
    window_start = now - timedelta(hours=BLOCK_DURATION_HOURS)
    clean_email = (user_email or "").strip().lower()

    stmt = (
        select(SecurityAuditLogModel)
        .where(
            or_(
                SecurityAuditLogModel.user_uid == user_uid,
                SecurityAuditLogModel.user_email == clean_email,
            )
        )
        .order_by(SecurityAuditLogModel.created_at.desc())
    )
    all_user_logs = list(db.scalars(stmt).all())
    recent_logs = [
        r
        for r in all_user_logs
        if _ensure_aware(r.created_at) is not None and _ensure_aware(r.created_at) >= window_start  # type: ignore[operator]
    ]

    strike_number = len(recent_logs) + 1
    is_blocked = strike_number >= MAX_STRIKES
    blocked_until = (now + timedelta(hours=BLOCK_DURATION_HOURS)) if is_blocked else None

    entry = SecurityAuditLogModel(
        user_uid=user_uid,
        user_email=clean_email,
        violation_type=violation_type,
        strike_number=strike_number,
        is_blocked=is_blocked,
        blocked_until=blocked_until,
        reason=reason,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    logger.warning(
        "SECURITY STRIKE (%d/%d) - uid='%s' email='%s' type='%s' blocked=%s: %s",
        strike_number,
        MAX_STRIKES,
        user_uid,
        clean_email,
        violation_type,
        is_blocked,
        reason,
    )
    return entry


def _verify_token_claims(
    token: str,
    verify_token_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify Bearer token via custom verifier, dev-token format, or Firebase Admin SDK."""
    clean_token = token.strip()
    if not clean_token:
        raise InvalidCredentialsException("Token de autenticación vacío.")

    # 1. Explicit expired token simulation or marker
    if clean_token in ("expired", "dev-token:expired") or clean_token.startswith("expired:"):
        raise TokenExpiredException("El token de sesión ha expirado.")

    # 2. Custom injected verifier (used in unit tests)
    if verify_token_fn is not None:
        return verify_token_fn(clean_token)

    # 3. Local dev token format: "dev-token:<uid>:<email>:<role>:<unit_code>"
    if clean_token.startswith("dev-token:"):
        parts = clean_token.split(":")
        if len(parts) < 5:
            raise InvalidCredentialsException("Formato de dev-token inválido o manipulado.")
        _, t_uid, t_email, t_role, t_unit = parts[0], parts[1], parts[2], parts[3], parts[4]
        if len(parts) >= 6 and parts[5] == "expired":
            raise TokenExpiredException("El token de desarrollo ha expirado.")

        directory = _load_server_role_directory()
        dir_entry = directory.get(t_email.strip().lower())
        if dir_entry:
            expected_role = dir_entry.get("role")
            expected_unit = dir_entry.get("unit_code")
            expected_uid = str(dir_entry.get("uid") or f"dev_{t_email.strip().lower()}")
            if t_role != expected_role or t_uid != expected_uid or not _units_match(expected_unit, t_unit or None):
                raise InvalidCredentialsException(
                    f"El dev-token fue manipulado y no coincide con el directorio del servidor para '{t_email}'."
                )

        return {
            "uid": t_uid,
            "email": t_email.strip().lower(),
            "role": t_role,
            "unit_code": t_unit if t_unit else None,
        }

    # 4. Firebase Admin SDK verification
    try:
        import firebase_admin
        from firebase_admin import auth as fb_auth

        if not firebase_admin._apps:
            firebase_admin.initialize_app()

        decoded = fb_auth.verify_id_token(clean_token)
        uid = str(decoded.get("uid") or decoded.get("sub") or "")
        email = str(decoded.get("email") or "").strip().lower()
        directory = _load_server_role_directory()
        dir_entry = directory.get(email, {})
        role = decoded.get("role") or dir_entry.get("role")
        unit_code = decoded.get("unit_code") or dir_entry.get("unit_code")
        return {
            "uid": uid,
            "email": email,
            "role": role,
            "unit_code": unit_code,
        }
    except Exception as exc:
        exc_name = type(exc).__name__
        exc_msg = str(exc).lower()
        if "expired" in exc_name.lower() or "expired" in exc_msg:
            raise TokenExpiredException("El token de Firebase ha expirado.") from exc
        raise InvalidCredentialsException(f"Token de autenticación inválido: {exc}") from exc


def verify_request_credentials(
    authorization_header: str | None,
    claimed_context: UserContext,
    db: Session,
    verify_token_fn: Callable[[str], dict[str, Any]] | None = None,
) -> UserContext:
    """Enforce active 24h blocks, handle expired tokens (401 without blocking), and verify identity claims (403 + 24h block on tampering)."""
    # 1. Check if the user is already blocked in `security_audit_logs`
    active_block = check_active_user_block(
        db=db,
        user_uid=claimed_context.uid,
        user_email=claimed_context.email,
    )
    if active_block is not None:
        blocked_until_iso = (
            _ensure_aware(active_block.blocked_until).isoformat()  # type: ignore[union-attr]
            if active_block.blocked_until
            else ""
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "user_blocked",
                "reason": active_block.reason,
                "violation_type": active_block.violation_type,
                "blocked_until": blocked_until_iso,
                "message": (
                    f"⛔ **Cuenta bloqueada temporalmente por 24 horas.**\n\n"
                    f"- **Motivo**: {active_block.reason}\n"
                    f"- **Bloqueo vigente hasta**: `{blocked_until_iso}`"
                ),
            },
        )

    # 2. Extract Bearer token from Authorization header
    if not authorization_header or not authorization_header.strip().lower().startswith("bearer "):
        reason = "Solicitud rechazada: Falta el encabezado Authorization Bearer o es inválido."
        block_entry = record_credential_tampering_block(
            db=db,
            user_uid=claimed_context.uid,
            user_email=claimed_context.email,
            reason=reason,
        )
        blocked_until_iso = _ensure_aware(block_entry.blocked_until).isoformat()  # type: ignore[union-attr]
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "user_blocked",
                "reason": reason,
                "violation_type": "credential_tampering",
                "blocked_until": blocked_until_iso,
                "message": (
                    f"⛔ **Acceso bloqueado por 24 horas por manipulación o ausencia de credenciales.**\n\n"
                    f"- **Motivo**: {reason}\n"
                    f"- **Bloqueo vigente hasta**: `{blocked_until_iso}`"
                ),
            },
        )

    raw_token = authorization_header.strip()[7:].strip()

    # 3. Verify token signature & expiration
    try:
        token_claims = _verify_token_claims(raw_token, verify_token_fn=verify_token_fn)
    except TokenExpiredException as exc:
        # Policy: Expired token MUST NOT block the user — return 401 so frontend prompts re-login
        logger.info("Expired token for user '%s': %s", claimed_context.email, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "token_expired",
                "message": "🔒 Tu sesión ha expirado. Por favor, vuelve a iniciar sesión para continuar.",
            },
        ) from exc
    except InvalidCredentialsException as exc:
        reason = f"Token inválido o falsificado: {exc}"
        block_entry = record_credential_tampering_block(
            db=db,
            user_uid=claimed_context.uid,
            user_email=claimed_context.email,
            reason=reason,
        )
        blocked_until_iso = _ensure_aware(block_entry.blocked_until).isoformat()  # type: ignore[union-attr]
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "user_blocked",
                "reason": reason,
                "violation_type": "credential_tampering",
                "blocked_until": blocked_until_iso,
                "message": (
                    f"⛔ **Acceso bloqueado por 24 horas por manipulación de credenciales.**\n\n"
                    f"- **Motivo**: {reason}\n"
                    f"- **Bloqueo vigente hasta**: `{blocked_until_iso}`"
                ),
            },
        ) from exc

    # 4. Compare verified token claims against claimed UserContext sent by frontend
    token_uid = str(token_claims.get("uid") or "")
    token_email = str(token_claims.get("email") or "").strip().lower()
    token_role = str(token_claims.get("role") or "")
    token_unit = token_claims.get("unit_code")

    mismatches: list[str] = []
    if token_uid and token_uid != claimed_context.uid:
        mismatches.append(f"UID ('{claimed_context.uid}' != '{token_uid}')")
    if token_email and token_email != claimed_context.email.strip().lower():
        mismatches.append(f"Email ('{claimed_context.email}' != '{token_email}')")
    if token_role != claimed_context.role:
        mismatches.append(f"Rol ('{claimed_context.role}' != '{token_role}')")
    if token_role == "resident" and not _units_match(token_unit, claimed_context.unit_code):
        mismatches.append(f"Unidad ('{claimed_context.unit_code}' != '{token_unit}')")

    if mismatches:
        reason = (
            "Discrepancia de identidad detectada entre el token autenticado y el contexto enviado: "
            + ", ".join(mismatches)
        )
        block_entry = record_credential_tampering_block(
            db=db,
            user_uid=token_uid or claimed_context.uid,
            user_email=token_email or claimed_context.email,
            reason=reason,
        )
        blocked_until_iso = _ensure_aware(block_entry.blocked_until).isoformat()  # type: ignore[union-attr]
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "user_blocked",
                "reason": reason,
                "violation_type": "credential_tampering",
                "blocked_until": blocked_until_iso,
                "message": (
                    f"⛔ **Acceso bloqueado por 24 horas por discrepancia de credenciales.**\n\n"
                    f"- **Motivo**: {reason}\n"
                    f"- **Bloqueo vigente hasta**: `{blocked_until_iso}`"
                ),
            },
        )

    # Return canonical verified UserContext
    return UserContext(
        uid=token_uid or claimed_context.uid,
        email=token_email or claimed_context.email.strip().lower(),
        role=token_role,  # type: ignore[arg-type]
        unit_code=claimed_context.unit_code if token_role == "resident" else None,
    )
