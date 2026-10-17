from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import login_guard, users
from app.auth import require_auth

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    username: str  # the user's email address
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_at: datetime


class TokenStatus(BaseModel):
    valid: bool
    expires_at: datetime
    email: str
    name: str
    rights: list[str]


@router.post("/token", response_model=TokenResponse)
def get_token(request: TokenRequest, http: Request) -> TokenResponse:
    ip = login_guard.client_ip(http.headers.get("x-forwarded-for"), http.client.host if http.client else None)
    try:
        user = login_guard.authenticate(request.username, request.password, ip)
    except login_guard.Locked as locked:
        raise HTTPException(status_code=429, detail="Too many failed logins; try again later",
                            headers={"Retry-After": str(locked.retry_after)})
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(token=users.issue_token(user), expires_at=users.token_expires_at())


@router.get("/status", response_model=TokenStatus)
def get_status(user: users.User = Depends(require_auth)) -> TokenStatus:
    return TokenStatus(valid=True, expires_at=users.token_expires_at(), email=user.email, name=user.name,
                       rights=sorted(user.rights))
