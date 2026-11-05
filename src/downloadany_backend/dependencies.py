from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import get_db
from .models import AuthSession, User, utcnow
from .security import decode_access_token


bearer = HTTPBearer(auto_error=False)


def client_ip(request: Request, settings: Settings = Depends(get_settings)) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "unknown")[:45]


def current_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> tuple[User, AuthSession]:
    error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="登录状态无效或已过期",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not credentials:
        raise error
    try:
        payload = decode_access_token(credentials.credentials, settings)
        user_id = int(payload["sub"])
        session_id = payload["sid"]
    except (InvalidTokenError, KeyError, ValueError):
        raise error
    session = db.get(AuthSession, session_id)
    user = db.get(User, user_id)
    if (
        not user
        or not user.is_active
        or not session
        or session.user_id != user.id
        or session.revoked_at is not None
        or session.expires_at <= utcnow()
    ):
        raise error
    return user, session


def current_user(auth: tuple[User, AuthSession] = Depends(current_auth)) -> User:
    return auth[0]
