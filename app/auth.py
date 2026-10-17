from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app import users
from app.users import User

_security = HTTPBearer()
_optional_security = HTTPBearer(auto_error=False)


def require_auth(credentials: HTTPAuthorizationCredentials = Depends(_security)) -> User:
    """Any logged-in user (a valid personal token)."""
    user = users.verify_token(credentials.credentials)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def require_right(right: str):
    """A dependency that lets only users with this right pass (401 without a token, 403 without the right)."""
    def check(user: User = Depends(require_auth)) -> User:
        if not user.can(right):
            raise HTTPException(status_code=403, detail=f"This needs the '{right}' right")
        return user
    return check


def optional_user(credentials: HTTPAuthorizationCredentials | None = Depends(_optional_security)) -> User | None:
    """The user of a valid token, else None; never raises."""
    return users.verify_token(credentials.credentials) if credentials else None
