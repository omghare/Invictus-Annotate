"""
Auth — JWT creation/validation, login/logout/me routes,
and FastAPI dependencies for protected endpoints.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import bcrypt as _bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from app.config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_HOURS
from app.database import get_db, now_iso, new_id
from app.models import LoginRequest, TokenResponse, UserOut

router = APIRouter(tags=["auth"])
security = HTTPBearer(auto_error=False)


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_access_token(payload: dict) -> str:
    data = payload.copy()
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    data["exp"] = expire
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ── FastAPI dependencies ──────────────────────────────────────────────────────

async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Validates Bearer JWT and returns the user row. Raises 401 if invalid."""
    token: Optional[str] = None

    if creds:
        token = creds.credentials

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    cur = await db.execute("SELECT * FROM users WHERE id = ?", (payload["sub"],))
    user = await cur.fetchone()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return dict(user)


async def get_admin_user(user: dict = Depends(get_current_user)) -> dict:
    """Extends get_current_user — additionally checks is_admin flag."""
    if not user["is_admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: aiosqlite.Connection = Depends(get_db)):
    cur = await db.execute("SELECT * FROM users WHERE username = ?", (body.username.strip(),))
    user = await cur.fetchone()

    if user is None or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    token = create_access_token({
        "sub": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
    })

    return TokenResponse(
        access_token=token,
        user_id=user["id"],
        username=user["username"],
        is_admin=bool(user["is_admin"]),
    )


@router.get("/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    return UserOut(**user)
