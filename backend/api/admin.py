from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from typing import List, Optional
from backend.core.config import settings
from backend.core.database import AsyncJsonDB
from backend.core.account_pool import AccountPool, Account

router = APIRouter()

def verify_admin(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split("Bearer ")[1]
    if token != settings.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden: Admin Key Mismatch")
    return token

class UserCreate(BaseModel):
    name: str
    quota: int = 1000000

class User(BaseModel):
    id: str
    name: str
    quota: int
    used_tokens: int

@router.get("/status", dependencies=[Depends(verify_admin)])
async def get_system_status(request: Request):
    # 这里要接入全局引擎的状态
    pool = request.app.state.account_pool
    engine = request.app.state.browser_engine
    return {
        "accounts": pool.status(),
        "browser_engine": {
            "started": engine._started,
            "pool_size": engine.pool_size,
            "queue": engine._pages.qsize()
        }
    }

@router.get("/users", dependencies=[Depends(verify_admin)])
async def list_users(request: Request):
    db: AsyncJsonDB = request.app.state.users_db
    data = await db.get()
    return {"users": data}

@router.post("/users", dependencies=[Depends(verify_admin)])
async def create_user(user: UserCreate, request: Request):
    import uuid
    db: AsyncJsonDB = request.app.state.users_db
    data = await db.get()
    new_user = {
        "id": f"sk-{uuid.uuid4().hex}",
        "name": user.name,
        "quota": user.quota,
        "used_tokens": 0
    }
    data.append(new_user)
    await db.save(data)
    return new_user

@router.get("/accounts", dependencies=[Depends(verify_admin)])
async def list_accounts(request: Request):
    pool: AccountPool = request.app.state.account_pool
    return {"accounts": [a.to_dict() for a in pool.accounts]}

@router.post("/accounts/register", dependencies=[Depends(verify_admin)])
async def register_new_account(request: Request):
    """一键调用浏览器无头注册新千问账号"""
    from backend.services.auth_resolver import register_qwen_account
    from backend.core.store import store
    
    # 简单的频率限制保护
    current = len(store.accounts)
    if current >= 100:
        return {"ok": False, "error": "账号池已满，请先清理死号"}
        
    try:
        acc = await register_qwen_account()
        if acc:
            store.add(acc)
            return {"ok": True, "email": acc.email, "message": "新账号注册成力并已入池"}
        return {"ok": False, "error": "自动化注册失败，可能遇到风控或页面元素改变"}
    except Exception as e:
        return {"ok": False, "error": f"注册发生异常: {str(e)}"}

@router.post("/accounts/{email}/verify", dependencies=[Depends(verify_admin)])
async def verify_account(email: str, request: Request):
    """强制验活或尝试刷新指定账号的 Token"""
    from backend.core.store import store
    from backend.services.qwen_client import client
    from backend.services.auth_resolver import get_fresh_token
    
    acc = next((a for a in store.accounts if a.email == email), None)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
        
    valid = await client.verify_token(acc.token)
    if not valid and acc.password:
        # Token 失效，尝试用密码走 auth_resolver 自动登录拿新 Token
        try:
            new_token = await get_fresh_token(acc.email, acc.password)
            if new_token:
                acc.token = new_token
                valid = await client.verify_token(new_token)
        except Exception as e:
            pass
            
    acc.valid = valid
    store.save()
    return {"ok": True, "email": acc.email, "valid": valid, "message": "验证通过" if valid else "账号已死或密码错误"}

@router.delete("/accounts/{email}", dependencies=[Depends(verify_admin)])
async def delete_account(email: str, request: Request):
    pool: AccountPool = request.app.state.account_pool
    await pool.remove(email)
    return {"status": "success"}
