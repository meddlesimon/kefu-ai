"""管理员账号密码登录 + JWT 鉴权。

JWT 与客服 OAuth 用同一 jwt_secret,但 iss 字段不同:
- 客服: iss=kefu-backend
- 管理员: iss=kefu-admin
"""
import time

import bcrypt
import jwt as pyjwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.storage import get_storage

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    username: str
    role: str
    jwt: str


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def make_admin_jwt(username: str, role: str) -> str:
    payload = {
        "username": username,
        "role": role,
        "exp": int(time.time()) + settings.jwt_ttl_seconds,
        "iss": "kefu-admin",
    }
    return pyjwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def get_admin(authorization: str = Header(None)) -> dict:
    """依赖项:验证管理员 JWT,返回 {username, role}。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        if payload.get("iss") != "kefu-admin":
            raise HTTPException(status_code=401, detail="not admin token")
        return {
            "username": payload["username"],
            "role": payload["role"],
        }
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")


@router.post("/api/admin/login", response_model=LoginResponse)
async def admin_login(req: LoginRequest):
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT username, password_hash, role FROM admins WHERE username = ?",
        (req.username.strip(),),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    if not verify_password(req.password, row[1]):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return LoginResponse(
        username=row[0],
        role=row[2],
        jwt=make_admin_jwt(row[0], row[2]),
    )


@router.get("/api/admin/me")
async def admin_me(admin: dict = Depends(get_admin)):
    return admin
