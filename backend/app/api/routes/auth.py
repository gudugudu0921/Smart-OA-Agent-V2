"""Authentication routes."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.request_context import get_client_ip, get_safe_user_agent
from app.core.security import create_access_token
from app.db.mysql import get_db
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    PasswordChangeResponse,
    TokenResponse,
)
from app.services.audit_service import record_login_audit
from app.services.auth_service import (
    AuthenticationReason,
    authenticate_user_with_reason,
    change_password,
)


router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(
    credentials: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TokenResponse:
    result = authenticate_user_with_reason(
        db,
        credentials.username,
        credentials.password.get_secret_value(),
    )
    user = result.user
    if result.reason is AuthenticationReason.SUCCESS:
        action = "LOGIN_SUCCESS"
    elif result.reason is AuthenticationReason.USER_INACTIVE:
        action = "LOGIN_BLOCKED"
    else:
        action = "LOGIN_FAILED"
    record_login_audit(
        db,
        action=action,
        user_id=user.id if user is not None else None,
        username=credentials.username,
        reason_code=result.reason.value,
        ip_address=get_client_ip(request),
        user_agent=get_safe_user_agent(request),
    )
    if result.reason is not AuthenticationReason.SUCCESS or user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(
        access_token=create_access_token(
            subject=user.id,
            issued_after=user.password_changed_at,
        )
    )


@router.post("/change-password", response_model=PasswordChangeResponse)
def update_own_password(
    payload: ChangePasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PasswordChangeResponse:
    change_password(
        db,
        user=current_user,
        old_password=payload.old_password.get_secret_value(),
        new_password=payload.new_password.get_secret_value(),
        ip_address=get_client_ip(request),
    )
    return PasswordChangeResponse(message="密码修改成功，请重新登录。")
