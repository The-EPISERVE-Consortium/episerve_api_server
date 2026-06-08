import hmac
import hashlib
from datetime import datetime, timezone, timedelta, date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.auth import _daily_token
from app.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_at: datetime


@router.post("/token", response_model=TokenResponse)
def get_token(request: TokenRequest) -> TokenResponse:
    valid_username = hmac.compare_digest(request.username, settings.auth_username)
    valid_password = hmac.compare_digest(request.password, settings.auth_password)
    if not (valid_username and valid_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    tomorrow_midnight = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    return TokenResponse(token=_daily_token(), expires_at=tomorrow_midnight)
