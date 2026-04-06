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
    
    from backend.core.config import API_KEYS, settings as backend_settings
    
    # 允许使用默认管理员 Key (ADMIN_KEY) 或者任何已生成的 API_KEYS 作为管理凭证
    if token != backend_settings.ADMIN_KEY and token not in API_KEYS:
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
    
    # queue 是当前队列里空闲(准备好)的 page 数量，真正的排队数应该是 pool_size - qsize 或者有其他请求在等待，这里修正前端展示逻辑
    free_pages = engine._pages.qsize()
    in_use = engine.pool_size - free_pages
    
    return {
        "accounts": pool.status(),
        "browser_engine": {
            "started": engine._started,
            "pool_size": engine.pool_size,
            "free_pages": free_pages,
            "queue": in_use if in_use > 0 else 0  # 这里暂且把正在被占用的页面数当做并发压力展示
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
    # 模拟原始 FastAPI 序列化，包含运行时状态
    accs = []
    for a in pool.accounts:
        d = a.to_dict()
        d["valid"] = a.valid
        d["inflight"] = a.inflight
        d["rate_limited_until"] = a.rate_limited_until
        accs.append(d)
    return {"accounts": accs}

@router.post("/accounts/register", dependencies=[Depends(verify_admin)])
async def register_new_account(request: Request):
    """一键调用浏览器无头注册新千问账号"""
    import logging
    from backend.services.auth_resolver import register_qwen_account
    from backend.core.account_pool import AccountPool
    pool: AccountPool = request.app.state.account_pool
    
    log = logging.getLogger("backend.api.admin")
    
    client_ip = request.client.host if request.client else "127.0.0.1"
    log.info(f"[Register] 管理员触发注册，来源IP: {client_ip}")
    
    # 简单的频率限制保护
    current = len(pool.accounts)
    if current >= 100:
        return {"ok": False, "error": "账号池已满，请先清理死号"}
        
    try:
        acc = await register_qwen_account()
        if acc:
            await pool.add(acc)
            log.info(f"[Register] 注册成功: {acc.email}（当前账号数: {len(pool.accounts)}/100）")
            return {"ok": True, "email": acc.email, "message": "新账号注册成功并已入池"}
        return {"ok": False, "error": "自动化注册失败，可能遇到风控或页面元素改变"}
    except Exception as e:
        return {"ok": False, "error": f"注册发生异常: {str(e)}"}

@router.post("/verify", dependencies=[Depends(verify_admin)])
async def verify_all_accounts(request: Request):
    """验证所有账号的有效性"""
    from backend.core.account_pool import AccountPool
    from backend.services.qwen_client import QwenClient
    import logging
    
    log = logging.getLogger("qwen2api.admin")
    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client
    
    results = []
    for acc in pool.accounts:
        is_valid = await client.verify_account(acc)
        if not is_valid and acc.password:
            log.info(f"[Verify] {acc.email} token失效，尝试自动刷新...")
            is_valid = await client.auth_resolver.refresh_token(acc)
            
        acc.valid = is_valid
        if not is_valid:
            pool.mark_invalid(acc)
        else:
            await pool.add(acc) # Save updated status
            
        results.append({"email": acc.email, "valid": is_valid, "refreshed": not is_valid})
            
    return {"ok": True, "results": results}

@router.post("/accounts/{email}/activate", dependencies=[Depends(verify_admin)])
async def activate_account(email: str, request: Request):
    """单独激活某个账号"""
    from backend.services.auth_resolver import activate_account as activate_logic
    from backend.core.account_pool import AccountPool
    import asyncio
    
    pool: AccountPool = request.app.state.account_pool
    acc = next((a for a in pool.accounts if a.email == email), None)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
        
    # 防止并发点击：检查一个运行时标志
    if getattr(acc, "_is_activating", False):
        return {"ok": False, "error": "该账号正在激活中，请勿重复点击"}
        
    try:
        setattr(acc, "_is_activating", True)
        success = await activate_logic(acc)
        if success:
            acc.valid = True
            acc.activation_pending = False
            await pool.add(acc) # 这会触发覆盖保存
            return {"ok": True, "message": "Account activated successfully"}
        return {"ok": False, "error": "未能找到激活链接或获取Token"}
    finally:
        setattr(acc, "_is_activating", False)

@router.post("/accounts/{email}/verify", dependencies=[Depends(verify_admin)])
async def verify_account(email: str, request: Request):
    """单独验证某个账号的有效性"""
    from backend.services.qwen_client import QwenClient
    from backend.core.account_pool import AccountPool
    import logging
    
    log = logging.getLogger("qwen2api.admin")
    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client
    
    acc = next((a for a in pool.accounts if a.email == email), None)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
        
    is_valid = await client.verify_account(acc)
    if not is_valid and acc.password:
        log.info(f"[Verify] {acc.email} token失效，尝试自动刷新...")
        is_valid = await client.auth_resolver.refresh_token(acc)
        
    acc.valid = is_valid
    if not is_valid:
        pool.mark_invalid(acc)
    else:
        await pool.add(acc) # Save updated status
        
    return {"ok": True, "valid": is_valid}

@router.delete("/accounts/{email}", dependencies=[Depends(verify_admin)])
async def delete_account(email: str, request: Request):
    from backend.core.account_pool import AccountPool
    pool: AccountPool = request.app.state.account_pool
    await pool.remove(email)
    return {"ok": True}

@router.get("/settings", dependencies=[Depends(verify_admin)])
async def get_settings():
    from backend.core.config import MODEL_MAP
    # 从 settings.py 所在的同级导入 VERSION，避免循环导入或未定义报错
    from backend.core.config import settings as backend_settings
    
    # 强制将 dict 转换，确保能被 JSON 序列化
    safe_map = {k: v for k, v in MODEL_MAP.items()}
    return {
        "version": "2.0.0",
        "max_inflight_per_account": backend_settings.MAX_INFLIGHT_PER_ACCOUNT,
        "model_aliases": safe_map
    }

@router.put("/settings", dependencies=[Depends(verify_admin)])
async def update_settings(data: dict):
    from backend.core.config import MODEL_MAP
    if "max_inflight_per_account" in data:
        settings.MAX_INFLIGHT_PER_ACCOUNT = data["max_inflight_per_account"]
    if "model_aliases" in data:
        MODEL_MAP.clear()
        MODEL_MAP.update(data["model_aliases"])
    return {"ok": True}

@router.get("/keys", dependencies=[Depends(verify_admin)])
async def get_keys():
    from backend.core.config import API_KEYS
    return {"keys": list(API_KEYS)}

@router.post("/keys", dependencies=[Depends(verify_admin)])
async def generate_key():
    from backend.core.config import API_KEYS, save_api_keys
    import uuid
    new_key = f"sk-qwen-{uuid.uuid4().hex[:20]}"
    API_KEYS.add(new_key)
    save_api_keys(API_KEYS)
    return {"ok": True, "key": new_key}

@router.delete("/keys/{key}", dependencies=[Depends(verify_admin)])
async def delete_key(key: str):
    from backend.core.config import API_KEYS, save_api_keys
    if key in API_KEYS:
        API_KEYS.remove(key)
        save_api_keys(API_KEYS)
    return {"ok": True}
