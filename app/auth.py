import hmac
import hashlib
from datetime import date

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

_security = HTTPBearer()


def _daily_token() -> str:
    return hmac.new(
        settings.auth_master_secret.encode(),
        date.today().isoformat().encode(),
        hashlib.sha256,
    ).hexdigest()


def require_auth(credentials: HTTPAuthorizationCredentials = Depends(_security)) -> None:
    if not hmac.compare_digest(credentials.credentials, _daily_token()):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
