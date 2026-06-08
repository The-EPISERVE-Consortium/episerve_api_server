import hmac
import hashlib
from datetime import datetime, timezone, timedelta, date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import _daily_token, require_auth
from app.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_at: datetime


class TokenStatus(BaseModel):
    valid: bool
    expires_at: datetime


def _next_midnight() -> datetime:
    return (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )


@router.post("/token", response_model=TokenResponse)
def get_token(request: TokenRequest) -> TokenResponse:
    valid_username = hmac.compare_digest(request.username, settings.auth_username)
    valid_password = hmac.compare_digest(request.password, settings.auth_password)
    if not (valid_username and valid_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(token=_daily_token(), expires_at=_next_midnight())


@router.get("/status", response_model=TokenStatus, dependencies=[Depends(require_auth)])
def get_status() -> TokenStatus:
    return TokenStatus(valid=True, expires_at=_next_midnight())
