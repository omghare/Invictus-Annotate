"""
Auth — JWT creation/validation, login/logout/me routes,
and FastAPI dependencies for protected endpoints.

Tokens carry the user's token_version ("tv"); bumping it (password change, logout
everywhere, deactivation) revokes every token issued before. Media URLs (<img>, <video>,
downloads) authenticate with an HttpOnly cookie holding the same token, so tokens never
appear in URLs or access logs.
"""
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import bcrypt as _bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from app.config import ACCESS_TOKEN_EXPIRE_HOURS, ALGORITHM, MEDIA_COOKIE, SECRET_KEY
from app.database import get_db
from app.models import ChangeOwnPasswordRequest, LoginRequest, TokenResponse, UserOut

router = APIRouter(tags=["auth"])
security = HTTPBearer(auto_error=False)

# Routes a user who still has to change their password may call
_PASSWORD_CHANGE_ALLOWED = ("/api/auth/me", "/api/auth/me/password", "/api/auth/logout", "/api/auth/session")


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_access_token(user: dict) -> str:
    data = {
        "sub": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "tv": int(user["token_version"] or 0),
        "exp": datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


async def user_for_token(token: Optional[str], db: aiosqlite.Connection) -> Optional[dict]:
    """The active user a token belongs to, or None (bad signature, expired, revoked, deactivated)."""
    payload = decode_token(token) if token else None
    if payload is None:
        return None
    cur = await db.execute("SELECT * FROM users WHERE id = ?", (payload.get("sub"),))
    user = await cur.fetchone()
    if user is None or not user["is_active"] or int(payload.get("tv", 0)) != int(user["token_version"] or 0):
        return None
    return dict(user)


def set_media_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        MEDIA_COOKIE, token, max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        httponly=True, samesite="strict", path="/",
    )


# ── FastAPI dependencies ──────────────────────────────────────────────────────

async def _user_from_token(token: Optional[str], db: aiosqlite.Connection, path: str = "") -> dict:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await user_for_token(token, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    if user["must_change_password"] and path not in _PASSWORD_CHANGE_ALLOWED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Change your password first")
    return user


async def get_current_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Validates Bearer JWT and returns the user row. Raises 401 if invalid."""
    return await _user_from_token(creds.credentials if creds else None, db, request.url.path)


async def get_admin_user(user: dict = Depends(get_current_user)) -> dict:
    """Extends get_current_user — additionally checks is_admin flag."""
    if not user["is_admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


async def get_user_header_or_query(
    request: Request,
    token: Optional[str] = Query(None),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """
    For URLs used directly by <img>, <video> and download links, which cannot send
    headers: the Bearer header, the media cookie, or (legacy links) a ?token= parameter.
    """
    tok = creds.credentials if creds else (request.cookies.get(MEDIA_COOKIE) or token)
    return await _user_from_token(tok, db, request.url.path)


async def get_admin_header_or_query(user: dict = Depends(get_user_header_or_query)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


async def bump_token_version(db: aiosqlite.Connection, user_id: str) -> None:
    """Revoke every token issued to this user so far (caller commits)."""
    await db.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (user_id,))


# ── Login throttling ──────────────────────────────────────────────────────────

_FAIL_WINDOW = 15 * 60
_FAIL_LIMIT = 5
_failures: dict[str, deque] = defaultdict(deque)


def _throttle_key(request: Request, username: str) -> str:
    return f"{request.client.host if request.client else '?'}|{username.lower()}"


def _check_throttle(key: str) -> None:
    q = _failures[key]
    now = time.monotonic()
    while q and now - q[0] > _FAIL_WINDOW:
        q.popleft()
    if len(q) >= _FAIL_LIMIT:
        wait = int(_FAIL_WINDOW - (now - q[0])) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed sign-ins. Try again in {max(1, wait // 60)} min.",
            headers={"Retry-After": str(wait)},
        )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, response: Response,
                db: aiosqlite.Connection = Depends(get_db)):
    username = body.username.strip()
    key = _throttle_key(request, username)
    _check_throttle(key)
    cur = await db.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,))
    user = await cur.fetchone()

    if user is None or not verify_password(body.password, user["password_hash"]):
        _failures[key].append(time.monotonic())
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    _failures.pop(key, None)

    token = create_access_token(dict(user))
    set_media_cookie(response, token)
    return TokenResponse(
        access_token=token,
        user_id=user["id"],
        username=user["username"],
        is_admin=bool(user["is_admin"]),
        must_change_password=bool(user["must_change_password"]),
    )


@router.post("/session")
async def refresh_media_cookie(request: Request, response: Response,
                               user: dict = Depends(get_current_user)):
    """(Re)issue the media cookie for the current Bearer token — pages call this once on load."""
    set_media_cookie(response, request.headers["authorization"].split(" ", 1)[1])
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response, everywhere: bool = Query(False),
                 user: dict = Depends(get_current_user), db: aiosqlite.Connection = Depends(get_db)):
    if everywhere:
        await bump_token_version(db, user["id"])
        await db.commit()
    response.delete_cookie(MEDIA_COOKIE, path="/")
    return {"detail": "Signed out"}


@router.get("/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    return UserOut(**user)


@router.patch("/me/password", response_model=TokenResponse)
async def change_own_password(
    body: ChangeOwnPasswordRequest,
    response: Response,
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Change your password. Other sessions are signed out; this one gets a fresh token."""
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if body.current_password == body.new_password:
        raise HTTPException(status_code=400, detail="New password must be different")
    await db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0, token_version = token_version + 1 "
        "WHERE id = ?",
        (hash_password(body.new_password), user["id"]),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM users WHERE id = ?", (user["id"],))
    fresh = dict(await cur.fetchone())
    token = create_access_token(fresh)
    set_media_cookie(response, token)
    return TokenResponse(access_token=token, user_id=fresh["id"], username=fresh["username"],
                         is_admin=bool(fresh["is_admin"]), must_change_password=False)
